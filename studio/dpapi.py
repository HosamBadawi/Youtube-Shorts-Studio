"""Windows DPAPI (CryptProtectData / CryptUnprotectData) via ctypes.

Used to wrap the credential-vault key to the current Windows USER, so the
headless, phone-driven host can decrypt secrets with no prompt while a stolen
copy of the files (off-box, or under a different OS account) is useless.

Pure standard library; :func:`available` is False on non-Windows so callers
fall back to the scrypt(password) wrap.
"""

from __future__ import annotations

import sys


def available() -> bool:
    return sys.platform == "win32"


# CRYPTPROTECT_UI_FORBIDDEN - never show UI (we run headless).
_UI_FORBIDDEN = 0x1


def _crypt(data: bytes, entropy: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _mk(b: bytes) -> _BLOB:
        buf = ctypes.create_string_buffer(b, len(b))
        return _BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = _mk(data)
    ent_blob = _mk(entropy) if entropy else None
    out_blob = _BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(in_blob), None,
            ctypes.byref(ent_blob) if ent_blob else None,
            None, None, _UI_FORBIDDEN, ctypes.byref(out_blob))
    if not ok:
        raise OSError("DPAPI " + ("protect" if protect else "unprotect")
                      + " failed (err %d)" % ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def protect(data: bytes, entropy: bytes = b"") -> bytes:
    """Encrypt ``data`` to the current Windows user (CryptProtectData)."""
    return _crypt(data, entropy, protect=True)


def unprotect(blob: bytes, entropy: bytes = b"") -> bytes:
    """Decrypt a blob produced by :func:`protect` for the same user."""
    return _crypt(blob, entropy, protect=False)
