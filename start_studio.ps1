# Daily Shorts Studio - one-click launcher for Windows.
# Right-click > Run with PowerShell, or:  powershell -ExecutionPolicy Bypass -File start_studio.ps1
#
# Starts Ollama (if not already running), then the Studio server + Cloudflare
# tunnel. Watch the console for the phone URL.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# 1. Make sure Ollama is up (ignore errors if already running / not installed).
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
        Write-Host "Starting Ollama..." -ForegroundColor Cyan
        Start-Process -WindowStyle Hidden ollama "serve"
        Start-Sleep -Seconds 2
    }
} else {
    Write-Host "Ollama not found on PATH - AI captions/segmenting will be skipped." -ForegroundColor Yellow
}

# 2. Activate venv if present.
if (Test-Path ".\.venv\Scripts\Activate.ps1") {
    . .\.venv\Scripts\Activate.ps1
}

# 3. Launch the studio (server + Cloudflare tunnel).
Write-Host "Starting Daily Shorts Studio..." -ForegroundColor Green
python -m studio -v
