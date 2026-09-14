# LongFlow optional API setup. Keys stay in process environment, never written to disk.
# Run from any directory: .\tools\api_keys_bootstrap.ps1
[CmdletBinding()]
param(
    [string]$Python = "",
    [int]$Port = 8000
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
if (-not $Python) {
    $venv = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { $Python = $venv }
    else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $cmd) { throw "Python not found. Install Python or supply -Python <path>." }
        $Python = $cmd.Source
    }
}
function Read-Secret([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); $secure.Dispose() }
}
Write-Host "LongFlow optional API setup (Enter keeps existing/default configuration)"
$mode = Read-Host "Configure an OpenAI-compatible model now? [y/N]"
if ($mode -eq "y") {
    $base = Read-Host "Model base URL (required, e.g. your provider's /v1 endpoint)"
    $model = Read-Host "Model name (required)"
    if (-not $base -or -not $model) { throw "Base URL and model name are required." }
    $env:LLM_BASE_URL = $base
    $env:LLM_MODEL = $model
    $env:LLM_API_KEY = Read-Secret "Model API key"
    $env:LONGFLOW_DRIVER = "openai_compatible"
}
$search = Read-Host "Search: [Enter] keep config, [1] Baidu webpage, [2] custom Baidu API"
if ($search -eq "1") {
    $env:LONGFLOW_SEARCH_KIND = "baidu_page"
    $env:LONGFLOW_SEARCH_ENDPOINT = "https://www.baidu.com/s"
} elseif ($search -eq "2") {
    $endpoint = Read-Host "Actual search API endpoint (required)"
    if (-not $endpoint) { throw "A real API endpoint is required." }
    $env:LONGFLOW_SEARCH_KIND = "baidu_api"
    $env:LONGFLOW_SEARCH_ENDPOINT = $endpoint
    $env:LONGFLOW_SEARCH_BAIDU_KEY = Read-Secret "Search API key"
    $header = Read-Host "Auth header name [Authorization]"
    if (-not $header) { $header = "Authorization" }
    $env:LONGFLOW_SEARCH_BAIDU_HEADER = $header
} elseif ($search) { throw "Unknown search selection." }
$env:PYTHONIOENCODING = "utf-8"
Push-Location $root
try {
    & $Python -X utf8 -m longflow.cli serve --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw "LongFlow exited with code $LASTEXITCODE" }
} finally { Pop-Location }
