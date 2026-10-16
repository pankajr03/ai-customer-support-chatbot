<#
.SYNOPSIS
    Start the application in demo mode, for showing it to clients.

.DESCRIPTION
    The same as scripts/start-dev.ps1 (setup check, backend, wait for readiness,
    frontend), with demo settings:

      - DEMO_MODE=true: the page says it is a demo with fictional sample data, and how
        answers are produced right now (as the backend reports it);
      - the sample knowledge base (data/faqs.json): a fictional online store;
      - a separate demo database (data/demo.db), so demo conversations never mix with
        development data;
      - one-click example questions that show an answer, a clarifying question and the
        safe fallback;
      - LLM_ENABLED=false unless -WithLlm: answers are the approved FAQ text, word for
        word, and labelled so. With -WithLlm the OpenAI settings in .env are used (a key
        is required, and requests are paid).

    The variables are set only for the two new windows; this terminal is left as it was.
    See docs/demo-guide.md.

.EXAMPLE
    .\scripts\start-demo.ps1

.EXAMPLE
    .\scripts\start-demo.ps1 -WithLlm -BackendPort 8001 -FrontendPort 8502
#>
param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 8501,
    # Use the OpenAI settings in .env (LLM_ENABLED, OPENAI_API_KEY) instead of turning the
    # LLM off. Paid requests.
    [switch]$WithLlm
)

$ErrorActionPreference = "Stop"
$examples = '["How can I track my order?","What is your return policy?","I want my money back",' +
    '"Do you ship internationally?","How do I reset my password?","Do you price match competitors?"]'
$demo = @{
    DEMO_MODE         = "true"
    DATABASE_URL      = "sqlite:///data/demo.db"
    EXAMPLE_QUESTIONS = $examples
}
if (-not $WithLlm) { $demo["LLM_ENABLED"] = "false" }

# Set for the windows start-dev.ps1 opens (they inherit this process's environment),
# then restore this terminal's values.
$saved = @{}
foreach ($name in $demo.Keys) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    [Environment]::SetEnvironmentVariable($name, $demo[$name], "Process")
}
try {
    & (Join-Path $PSScriptRoot "start-dev.ps1") -BackendPort $BackendPort -FrontendPort $FrontendPort
    $code = $LASTEXITCODE
} finally {
    foreach ($name in $saved.Keys) {
        [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process")
    }
}
if ($code -ne 0) { exit $code }
if ($WithLlm) {
    Write-Host "Demo started with the LLM settings from .env (paid OpenAI requests if enabled)."
} else {
    Write-Host "Demo started with the LLM off: answers are approved FAQ text, labelled as such."
}
Write-Host "Walk-through for the terminal: python scripts/demo_walkthrough.py --url http://127.0.0.1:$BackendPort"
