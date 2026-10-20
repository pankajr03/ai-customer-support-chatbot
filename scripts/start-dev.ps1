<#
.SYNOPSIS
    Start the backend and the Streamlit frontend for local development, each in its
    own PowerShell window.

.DESCRIPTION
    The same as running these from the project root in two terminals (README,
    "Running the application"):

        uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
        streamlit run frontend/app.py

    Steps:
      1. Runs scripts/check_setup.py and stops if the setup has a problem.
      2. Stops if either port is already in use (for example by a running backend).
      3. Opens a window with the backend (SEMANTIC_INDEX_PRELOAD=true unless -NoPreload,
         so the first question does not wait for the search model).
      4. Waits until GET /api/v1/ready answers 200 (the preload can take a few minutes).
      5. Opens a window with the frontend, pointed at that backend.

    Environment variables are set only inside the new windows, never in this one.
    Stop each service with Ctrl+C in its window.

.EXAMPLE
    .\scripts\start-dev.ps1

.EXAMPLE
    .\scripts\start-dev.ps1 -BackendPort 8001 -FrontendPort 8502 -NoPreload
#>
param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 8501,
    # Load the search model on the first question instead of at startup.
    [switch]$NoPreload,
    # Seconds to wait for the backend to become ready before giving up.
    [int]$ReadyTimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$backendUrl = "http://127.0.0.1:$BackendPort"

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "No virtual environment found at $python." -ForegroundColor Red
    Write-Host "Create it first: README, 'Installation'."
    exit 1
}

foreach ($port in @($BackendPort, $FrontendPort)) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        Write-Host "Port $port is already in use (is the app already running?)." -ForegroundColor Red
        Write-Host "Stop it, or choose another port with -BackendPort / -FrontendPort."
        exit 1
    }
}

# 1. Configuration check (BACKEND_API_URL only for this check, then restored).
$savedUrl = $env:BACKEND_API_URL
$env:BACKEND_API_URL = $backendUrl
try {
    & $python (Join-Path $root "scripts\check_setup.py")
    $checkExitCode = $LASTEXITCODE
} finally {
    $env:BACKEND_API_URL = $savedUrl
}
if ($checkExitCode -ne 0) { exit $checkExitCode }

function Start-Window([string]$title, [string]$command) {
    $quotedRoot = $root.Replace("'", "''")
    $quotedPython = $python.Replace("'", "''")
    $script = "`$Host.UI.RawUI.WindowTitle = '$title'; Set-Location -LiteralPath '$quotedRoot'; " +
        $command.Replace("{python}", "& '$quotedPython'")
    Start-Process powershell.exe -WorkingDirectory $root `
        -ArgumentList @("-NoExit", "-NoProfile", "-Command", $script)
}

# 2. Backend.
$preload = if ($NoPreload) { "false" } else { "true" }
Start-Window "Support chat - backend :$BackendPort" (
    "`$env:SEMANTIC_INDEX_PRELOAD = '$preload'; " +
    "{python} -m uvicorn app.main:app --reload --host 127.0.0.1 --port $BackendPort"
)

# 3. Wait for readiness (503 or no connection: not yet).
Write-Host "Waiting for the backend at $backendUrl/api/v1/ready ..."
$deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri "$backendUrl/api/v1/ready" -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        # Not listening yet, still preloading, or not ready.
    }
    Start-Sleep -Seconds 2
}
if (-not $ready) {
    Write-Host "The backend did not become ready within $ReadyTimeoutSeconds s." -ForegroundColor Red
    Write-Host "Check the backend window for the error, then run this script again."
    exit 1
}
Write-Host "Backend ready." -ForegroundColor Green

# 4. Frontend.
Start-Window "Support chat - frontend :$FrontendPort" (
    "`$env:BACKEND_API_URL = '$backendUrl'; " +
    "{python} -m streamlit run frontend/app.py --server.port $FrontendPort"
)
Write-Host "Frontend starting at http://localhost:$FrontendPort (API docs: $backendUrl/docs)."
