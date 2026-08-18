[CmdletBinding()]
param(
    [string]$ApiKey = "",
    [string]$Upstream = "http://127.0.0.1:8080",
    [string]$BridgeUrl = "http://127.0.0.1:8010/v1",
    [string]$Model = "auto",
    [switch]$ConfigureOnly,
    [switch]$NoSetDefault
)

$ErrorActionPreference = "Stop"

function Resolve-Python {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return @($py.Source, "-3")
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @($python.Source)
    }
    throw "Python 3.11 or newer is required. Install Python, then run this script again."
}

$pythonCommand = Resolve-Python
$pythonExe = $pythonCommand[0]
$pythonPrefix = @()
if ($pythonCommand.Count -gt 1) {
    $pythonPrefix = $pythonCommand[1..($pythonCommand.Count - 1)]
}

$versionText = & $pythonExe @pythonPrefix -c "import sys; print('.'.join(map(str, sys.version_info[:3]))); raise SystemExit(sys.version_info < (3, 11))"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.11 or newer is required. Found $versionText"
}
Write-Host "Using Python $versionText"

$base = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "ChatGPT-Web2API-OpenCode"
} else {
    Join-Path $HOME ".chatgpt-web2api-opencode"
}
$venv = Join-Path $base "venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
New-Item -ItemType Directory -Force -Path $base | Out-Null

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating isolated environment at $venv"
    & $pythonExe @pythonPrefix -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not upgrade pip." }

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pyproject = Join-Path $repoRoot "pyproject.toml"
if (Test-Path $pyproject) {
    Write-Host "Installing this repository into the isolated environment"
    & $venvPython -m pip install -e $repoRoot
} else {
    Write-Host "Installing the current fork from GitHub"
    & $venvPython -m pip install "https://github.com/ybrizitskiy-hue/ChatGPT-Web2API/archive/refs/heads/master.zip"
}
if ($LASTEXITCODE -ne 0) { throw "Could not install ChatGPT-Web2API." }

$setupArgs = @(
    "-m", "chatgpt_web2api.opencode_setup", "setup",
    "--non-interactive",
    "--upstream", $Upstream,
    "--bridge-url", $BridgeUrl,
    "--model", $Model
)
if (-not $NoSetDefault) {
    $setupArgs += "--set-default"
}
if (-not $ConfigureOnly) {
    $setupArgs += "--start"
}

$oldSecret = $env:W2A_OPENCODE_API_KEY
try {
    if ($ApiKey) {
        $env:W2A_OPENCODE_API_KEY = $ApiKey
    }
    & $venvPython @setupArgs
    exit $LASTEXITCODE
} finally {
    if ($null -eq $oldSecret) {
        Remove-Item Env:W2A_OPENCODE_API_KEY -ErrorAction SilentlyContinue
    } else {
        $env:W2A_OPENCODE_API_KEY = $oldSecret
    }
}
