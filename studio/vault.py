"""Encrypted-at-rest credential vault for social-media logins.

Design (see the publishing-design plan):
- Each field is encrypted with **AES-256-GCM**, with Associated Data
  ``platform|account|field`` bound in, so a stolen ciphertext can't be
  transplanted to another row/field.
- All blobs are encrypted under one random 32-byte **DEK** that never changes
  (so rotating the app password never re-encrypts or loses credentials).
- The DEK is wrapped two independent ways, either of which can unlock it:
    * **Windows DPAPI** (per-user) -> ``dek.dpapi`` — no-prompt boot on the host.
    * **scrypt(app_password)** -> ``dek.scrypt`` — portable recovery on a new box.
- Files live under the gitignored ``secrets/`` dir and are locked down with
  ``icacls`` on Windows.
- If ``cryptography`` isn't installed, the vault constructs **disabled**: storing
  credentials raises a friendly error, and the Edge-profile / saved-session
  publish strategies keep working.

Listing/status never returns passwords/tokens/TOTP secrets; it does surface the
stored *username* (a low-sensitivity handle) so the UI can show which account is
connected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import sqlite3
import struct
import subprocess
import sys
import time
from pathlib import Path

from . import dpapi
from .config import StudioConfig

logger = logging.getLogger(__name__)

_DPAPI_ENTROPY = b"daily-shorts-studio-vault-v1"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 17, 8, 1  # ~128 MB, stronger off-box wrap


class Secret(str):
    """A string whose repr/str hides the value. Use ``.reveal()`` for plaintext."""

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "'***'"

    def __str__(self) -> str:
        return "***"

    def reveal(self) -> str:
        return str.__str__(self)


def totp_now(secret_b32: str, digits: int = 6, period: int = 30) -> str:
    """Compute the current TOTP code from a base32 secret (RFC 6238, SHA-1)."""
    s = secret_b32.strip().replace(" ", "").upper()
    s += "=" * (-len(s) % 8)
    key = base64.b32decode(s)
    counter = int(time.time() // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


class CredentialVault:
    def __init__(self, cfg: StudioConfig) -> None:
        self.cfg = cfg
        self.enabled = False
        self._dek: bytes | None = None
        self._aesgcm = None
        self._db = cfg.vault_path
        self._dek_dpapi = cfg.secrets_dir / "dek.dpapi"
        self._dek_scrypt = cfg.secrets_dir / "dek.scrypt"
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa
        except Exception:
            logger.info("cryptography not installed -> credential vault disabled")
            return
        try:
            cfg.secrets_dir.mkdir(parents=True, exist_ok=True)
            _lockdown(cfg.secrets_dir)
            self._dek = self._load_or_create_dek()
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            self._aesgcm = AESGCM(self._dek)
            self._init_db()
            self.enabled = True
        except Exception as exc:  # pragma: no cover - keep app booting
            logger.warning("vault init failed (%s); credentials disabled", exc)

    # --- key management -----------------------------------------------------
    def _recovery_password(self) -> str:
        return self.cfg.vault_recovery_password or self.cfg.app_password or ""

    def _has_strong_recovery(self) -> bool:
        """A scrypt recovery wrap is only worth writing if the password is real;
        a default/empty password would make a stolen secrets/ dir crackable."""
        pw = self._recovery_password()
        return bool(pw) and pw != "change-me" and len(pw) >= 8

    def _scrypt_key(self, password: str, salt: bytes) -> bytes:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        return Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R,
                      p=_SCRYPT_P).derive(password.encode())

    def _load_or_create_dek(self) -> bytes:
        # 1) DPAPI fast path (Windows, same user).
        if dpapi.available() and self._dek_dpapi.exists():
            try:
                dek = dpapi.unprotect(self._dek_dpapi.read_bytes(), _DPAPI_ENTROPY)
                self._sync_scrypt(dek)  # keep recovery wrap current / drop if weak
                return dek
            except Exception as exc:
                logger.warning("DPAPI unwrap failed (%s); trying scrypt", exc)
        # 2) scrypt(password) recovery path.
        if self._dek_scrypt.exists() and self._recovery_password():
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            blob = self._dek_scrypt.read_bytes()
            salt, nonce, ct = blob[:16], blob[16:28], blob[28:]
            key = self._scrypt_key(self._recovery_password(), salt)
            dek = AESGCM(key).decrypt(nonce, ct, b"dek-wrap")
            if dpapi.available():  # heal the DPAPI wrap for next boot
                self._write_dpapi_wrap(dek)
            return dek
        # 3) first run -> generate + write wrap(s).
        dek = os.urandom(32)
        if dpapi.available():
            self._write_dpapi_wrap(dek)
        # Only write the portable scrypt wrap if the password is strong, OR if
        # DPAPI isn't available (then it's the only way to persist the key).
        if self._has_strong_recovery():
            self._write_scrypt_wrap(dek, self._recovery_password())
        elif not dpapi.available():
            # No OS keystore (DPAPI) AND no strong recovery password: refuse to
            # persist a weak, offline-crackable wrap. The vault stays disabled
            # (credentials must be re-entered each run) until a strong
            # vault_recovery_password is set. On Windows, DPAPI covers this.
            raise RuntimeError(
                "vault needs DPAPI (Windows) or a strong vault_recovery_password "
                "(>=8 chars) — refusing weak at-rest encryption")
        return dek

    def _sync_scrypt(self, dek: bytes) -> None:
        """Keep dek.scrypt in sync with the current recovery password; remove it
        when the password is weak so it can't be cracked off-box."""
        if self._has_strong_recovery():
            self._write_scrypt_wrap(dek, self._recovery_password())
        elif self._dek_scrypt.exists():
            try:
                self._dek_scrypt.unlink()
            except OSError:
                pass

    def _write_dpapi_wrap(self, dek: bytes) -> None:
        self._dek_dpapi.write_bytes(dpapi.protect(dek, _DPAPI_ENTROPY))
        _lockdown(self._dek_dpapi)

    def _write_scrypt_wrap(self, dek: bytes, password: str) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        salt, nonce = os.urandom(16), os.urandom(12)
        ct = AESGCM(self._scrypt_key(password, salt)).encrypt(nonce, dek, b"dek-wrap")
        self._dek_scrypt.write_bytes(salt + nonce + ct)
        _lockdown(self._dek_scrypt)

    def rewrap_password(self, new_password: str) -> None:
        """Re-wrap the DEK for a new recovery password. No credential is touched."""
        if not self.enabled or self._dek is None:
            return
        self._write_scrypt_wrap(self._dek, new_password or "studio")

    # --- db -----------------------------------------------------------------
    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS credentials (
                    platform TEXT, account TEXT,
                    username_enc BLOB, password_enc BLOB, totp_enc BLOB,
                    strategy TEXT, updated_at TEXT,
                    PRIMARY KEY (platform, account))""")
        _lockdown(self._db)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db, timeout=15)
        c.row_factory = sqlite3.Row
        return c

    # --- crypto -------------------------------------------------------------
    def _enc(self, plaintext: str | None, aad: str) -> bytes | None:
        if not plaintext:
            return None
        nonce = os.urandom(12)
        return nonce + self._aesgcm.encrypt(nonce, plaintext.encode(), aad.encode())

    def _dec(self, blob, aad: str) -> str | None:
        if not blob:
            return None
        blob = bytes(blob)
        return self._aesgcm.decrypt(blob[:12], blob[12:], aad.encode()).decode()

    @staticmethod
    def _aad(platform: str, account: str, field: str) -> str:
        return f"{platform}|{account}|{field}"

    # --- public API ---------------------------------------------------------
    def store(self, platform: str, username: str, password: str,
              totp_secret: str = "", strategy: str = "", account: str = "default"
              ) -> None:
        if not self.enabled:
            raise RuntimeError("credentials disabled (pip install cryptography)")
        u = self._enc(username, self._aad(platform, account, "username"))
        p = self._enc(password, self._aad(platform, account, "password"))
        t = self._enc(totp_secret, self._aad(platform, account, "totp"))
        ts = time.strftime("%Y-%m-%d %H:%M")
        with self._conn() as c:
            c.execute(
                """INSERT INTO credentials
                   (platform, account, username_enc, password_enc, totp_enc,
                    strategy, updated_at) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(platform, account) DO UPDATE SET
                    username_enc=excluded.username_enc,
                    password_enc=excluded.password_enc,
                    totp_enc=excluded.totp_enc,
                    strategy=COALESCE(NULLIF(excluded.strategy,''), credentials.strategy),
                    updated_at=excluded.updated_at""",
                (platform, account, u, p, t, strategy, ts))

    def get(self, platform: str, account: str = "default") -> dict | None:
        """Return decrypted credentials wrapped in :class:`Secret`. Transient use
        only — never log or persist these."""
        if not self.enabled:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM credentials WHERE platform=? AND account=?",
                (platform, account)).fetchone()
        if not row:
            return None
        return {
            "username": Secret(self._dec(row["username_enc"],
                                         self._aad(platform, account, "username")) or ""),
            "password": Secret(self._dec(row["password_enc"],
                                         self._aad(platform, account, "password")) or ""),
            "totp_secret": Secret(self._dec(row["totp_enc"],
                                            self._aad(platform, account, "totp")) or ""),
            "strategy": row["strategy"] or "",
        }

    def status(self, platform: str, account: str = "default") -> dict:
        """Non-secret status: does a credential exist + which fields are set."""
        out = {"has_credentials": False, "username": "", "has_password": False,
               "has_totp": False, "strategy": "", "updated_at": ""}
        if not self.enabled:
            return out
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM credentials WHERE platform=? AND account=?",
                (platform, account)).fetchone()
        if not row:
            return out
        out.update(has_credentials=True, has_password=bool(row["password_enc"]),
                   has_totp=bool(row["totp_enc"]), strategy=row["strategy"] or "",
                   updated_at=row["updated_at"] or "")
        # The username is a low-sensitivity handle; show it so the UI is useful.
        try:
            out["username"] = self._dec(row["username_enc"],
                                        self._aad(platform, account, "username")) or ""
        except Exception:
            out["username"] = ""
        return out

    def delete(self, platform: str, account: str = "default") -> None:
        if not self.enabled:
            return
        with self._conn() as c:
            c.execute("DELETE FROM credentials WHERE platform=? AND account=?",
                      (platform, account))

    # --- LLM provider API keys (stored encrypted like any credential) -------
    def set_api_key(self, provider: str, key: str) -> None:
        self.store(f"llm:{provider}", username=provider, password=key,
                   account="apikey")

    def get_api_key(self, provider: str) -> str:
        c = self.get(f"llm:{provider}", account="apikey")
        return c["password"].reveal() if c else ""

    def has_api_key(self, provider: str) -> bool:
        return bool(self.status(f"llm:{provider}", account="apikey")
                    .get("has_password"))

    def delete_api_key(self, provider: str) -> None:
        self.delete(f"llm:{provider}", account="apikey")


def _lockdown(path: Path) -> None:
    """Restrict a secret file/dir to the current user + SYSTEM (NTFS ACLs).

    For a directory the grants are made inheritable ((OI)(CI)) so files created
    inside it afterwards are born locked-down — closing the write-then-restrict
    window for the keys created within the secrets dir."""
    p = Path(path)
    if not p.exists():
        return
    if sys.platform != "win32":
        try:
            os.chmod(p, 0o700 if p.is_dir() else 0o600)
        except OSError:
            pass
        return
    user = os.environ.get("USERNAME", "")
    perm = "(OI)(CI)F" if p.is_dir() else "F"
    try:
        subprocess.run(["icacls", str(p), "/inheritance:r",
                        "/grant:r", f"{user}:{perm}", "/grant:r",
                        f"SYSTEM:{perm}"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # pragma: no cover
        pass
