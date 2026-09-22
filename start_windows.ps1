$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir
$env:UV_CACHE_DIR = Join-Path $ProjectDir ".uv-cache"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv is required" }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "Node.js/npm is required" }

uv sync
uv run python -m backend.database.init_db
npm --prefix frontend ci
npm --prefix frontend run build

$process = Start-Process uv -ArgumentList @(
    "run", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8000"
) -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru

for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2 | Out-Null
        Start-Process "http://127.0.0.1:8000"
        Write-Host "CarAnalyzer started. Backend PID: $($process.Id)"
        exit 0
    } catch {
        if ($process.HasExited) { throw "Backend stopped before health check completed" }
        Start-Sleep -Seconds 1
    }
}
throw "Backend did not become healthy within 30 seconds"
