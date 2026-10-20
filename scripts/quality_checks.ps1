# Offline, deterministic quality checks (Stage 12, Task 21). Windows PowerShell 5.1 or 7.
#
#   .\scripts\quality_checks.ps1                    # everything below except the audit
#   .\scripts\quality_checks.ps1 -Suite security    # one group: lint, types, unit,
#                                                   # integration, security, ai, coverage
#   .\scripts\quality_checks.ps1 -Audit             # also pip-audit (needs network, see below)
#
# Guarantees of every default step: no paid LLM call, no model download (HF_HUB_OFFLINE),
# no access to data/conversations.db (DATABASE_URL is in memory; tests use temporary
# databases), no secret needed (no OPENAI_API_KEY is read). Opt-in live tests stay off:
# RUN_OPENAI_LIVE_TESTS, RUN_EMBEDDING_MODEL_TESTS and RUN_DEPLOYMENT_TESTS are cleared for
# this run (the 25 Docker deployment tests are reported as skipped: docs/deployment-tests.md).
#
# Exit code: 0 when every required step passed, 1 otherwise. Informational steps (type
# checking) print their result but never fail the run.
#
# Dependency audit (-Audit): pip-audit queries the PyPI/OSV vulnerability database over
# the network (package names and versions only). Install it in a separate environment:
#   python -m venv .audit-venv; .audit-venv\Scripts\python -m pip install pip-audit
#   .audit-venv\Scripts\pip-audit -r requirements.txt
# The script uses the first of: .audit-venv\Scripts\pip-audit.exe, pip-audit on PATH.

param(
    [ValidateSet("all", "lint", "types", "unit", "integration", "security", "ai", "coverage")]
    [string]$Suite = "all",
    [switch]$Audit
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$env:HF_HUB_OFFLINE = "1"
$env:DATABASE_URL = "sqlite://"
$env:PYTHONIOENCODING = "utf-8"
Remove-Item Env:RUN_OPENAI_LIVE_TESTS -ErrorAction SilentlyContinue
Remove-Item Env:RUN_EMBEDDING_MODEL_TESTS -ErrorAction SilentlyContinue
Remove-Item Env:RUN_DEPLOYMENT_TESTS -ErrorAction SilentlyContinue

$results = New-Object System.Collections.Generic.List[object]

function Invoke-Step {
    param([string]$Name, [string[]]$Arguments, [bool]$Required = $true, [string]$Exe = $python)
    Write-Host ""
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    Write-Host "> $Exe $($Arguments -join ' ')"
    $started = Get-Date
    & $Exe @Arguments
    $code = $LASTEXITCODE
    $seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
    if ($code -eq 0) { $status = "PASS" }
    elseif ($Required) { $status = "FAIL" }
    else { $status = "INFO (exit $code, not a gate)" }
    $results.Add([pscustomobject]@{ Step = $Name; Status = $status; Seconds = $seconds })
}

$securityTests = @(
    "tests/test_security_audit.py", "tests/test_frontend_security.py",
    "tests/test_grounding_manipulation.py", "tests/test_audit_contact_views.py",
    "tests/test_ticket_lead_security.py", "tests/integration/test_security.py",
    "tests/test_security_privacy.py", "tests/test_staff_roles.py"
)

if ($Suite -in @("all", "lint")) {
    Invoke-Step "Lint (ruff check)" @("-m", "ruff", "check", "app", "frontend", "tests", "scripts")
    Invoke-Step "Formatting (ruff format --check)" @("-m", "ruff", "format", "--check", "app", "frontend", "tests", "scripts")
}
if ($Suite -in @("all", "types")) {
    Invoke-Step "Type checking (mypy, informational)" @("-m", "mypy") -Required $false
}
if ($Suite -eq "unit") {
    Invoke-Step "Unit tests" @("-m", "pytest", "-q", "--ignore=tests/integration")
}
if ($Suite -eq "integration") {
    Invoke-Step "Integration tests" @("-m", "pytest", "-q", "tests/integration")
}
if ($Suite -eq "security") {
    Invoke-Step "Security tests" (@("-m", "pytest", "-q") + $securityTests)
}
if ($Suite -in @("all", "coverage")) {
    # The whole suite once (unit, integration and security tests), with branch coverage.
    Invoke-Step "All tests with coverage" @(
        "-m", "pytest", "-q", "--cov=app", "--cov=frontend", "--cov-branch",
        "--cov-report=term:skip-covered", "--cov-report=xml:coverage.xml"
    )
}
if ($Suite -in @("all", "ai")) {
    # The prompt-injection suite with the deterministic lexical encoder (no model): exits
    # 1 on any unexpected failure. Retrieval quality needs the real model and is measured
    # by scripts/evaluate_retrieval.py and scripts/evaluate_ai.py, outside this gate.
    Invoke-Step "AI safety evaluation (prompt injection, offline)" @(
        "scripts/evaluate_prompt_injection.py", "--encoder", "lexical"
    )
}
if ($Audit) {
    $auditExe = Join-Path $root ".audit-venv\Scripts\pip-audit.exe"
    if (-not (Test-Path $auditExe)) {
        $found = Get-Command pip-audit -ErrorAction SilentlyContinue
        if ($found) { $auditExe = $found.Source } else { $auditExe = $null }
    }
    if ($auditExe) {
        Invoke-Step "Dependency audit (pip-audit)" @("-r", "requirements.txt") -Exe $auditExe
    } else {
        Write-Host "pip-audit not found: dependency audit NOT RUN (see the header for setup)." -ForegroundColor Yellow
        $results.Add([pscustomobject]@{ Step = "Dependency audit"; Status = "NOT RUN"; Seconds = 0 })
    }
}

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
$results | Format-Table -AutoSize | Out-String | Write-Host
$failed = @($results | Where-Object { $_.Status -eq "FAIL" })
if ($failed.Count -gt 0) {
    Write-Host "FAILED: $($failed.Step -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "All required checks passed." -ForegroundColor Green
exit 0
