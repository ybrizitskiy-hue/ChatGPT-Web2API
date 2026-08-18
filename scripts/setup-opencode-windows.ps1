[CmdletBinding()]
param(
    [string]$RepoUrl = "https://github.com/ybrizitskiy-hue/ChatGPT-Web2API.git",
    [string]$Branch = "master",
    [string]$InstallDir = "$env:LOCALAPPDATA\ChatGPT-Web2API-src",
    [switch]$SkipGitUpdate,
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step([string]$Text) {
    Write-Host "`n==> $Text" -ForegroundColor Cyan
}

function Resolve-Python {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return @{ Exe = $py.Source; Prefix = @("-3.11") }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @{ Exe = $python.Source; Prefix = @() }
    }
    throw "Python 3.11+ was not found. Install it and run this file again."
}

function Invoke-Python($Python, [string[]]$Arguments) {
    & $Python.Exe @($Python.Prefix) @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE"
    }
}

function Convert-SecureStringToPlain([Security.SecureString]$Secure) {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

if ($ValidateOnly) {
    Write-Host "PowerShell bootstrap parsed successfully."
    exit 0
}

Write-Step "Checking prerequisites"
$Python = Resolve-Python
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    throw "Git was not found. Install Git for Windows and run this file again."
}

$versionText = & $Python.Exe @($Python.Prefix) -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0) {
    throw "Could not execute Python."
}
$majorMinor = $versionText.Split('.')
if ([int]$majorMinor[0] -lt 3 -or ([int]$majorMinor[0] -eq 3 -and [int]$majorMinor[1] -lt 11)) {
    throw "Python 3.11+ is required; found $versionText."
}
Write-Host "Python $versionText"

Write-Step "Installing or updating ChatGPT-Web2API"
if (Test-Path "$InstallDir\.git") {
    if (-not $SkipGitUpdate) {
        & $git.Source -C $InstallDir fetch origin
        if ($LASTEXITCODE -ne 0) { throw "git fetch failed" }
        & $git.Source -C $InstallDir checkout $Branch
        if ($LASTEXITCODE -ne 0) { throw "git checkout failed" }
        & $git.Source -C $InstallDir pull --ff-only origin $Branch
        if ($LASTEXITCODE -ne 0) { throw "git pull failed" }
    }
}
elif (Test-Path $InstallDir) {
    throw "Install directory exists but is not a Git checkout: $InstallDir"
}
else {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $InstallDir) | Out-Null
    & $git.Source clone --branch $Branch --single-branch $RepoUrl $InstallDir
    if ($LASTEXITCODE -ne 0) { throw "git clone failed" }
}

$VenvDir = Join-Path $InstallDir ".venv"
if (-not (Test-Path $VenvDir)) {
    Invoke-Python $Python @("-m", "venv", $VenvDir)
}
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "Virtual environment Python was not created at $VenvPython"
}
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
& $VenvPython -m pip install -e $InstallDir
if ($LASTEXITCODE -ne 0) { throw "Package installation failed" }

Write-Step "Configuring OpenCode"
$defaultUpstream = "http://127.0.0.1:8000"
$upstream = Read-Host "Web2API URL [$defaultUpstream]"
if ([string]::IsNullOrWhiteSpace($upstream)) { $upstream = $defaultUpstream }
$secureKey = Read-Host "Web2API API key (leave blank if authentication is disabled)" -AsSecureString
$plainKey = Convert-SecureStringToPlain $secureKey
try {
    $env:W2A_API_KEY = $plainKey
    & $VenvPython -m chatgpt_web2api.opencode_setup --upstream-url $upstream
    if ($LASTEXITCODE -ne 0) { throw "OpenCode configuration failed" }
}
finally {
    Remove-Item Env:W2A_API_KEY -ErrorAction SilentlyContinue
    $plainKey = $null
}

Write-Step "Creating convenient launchers"
$StateDir = Join-Path $env:LOCALAPPDATA "ChatGPT-Web2API"
$Desktop = [Environment]::GetFolderPath("Desktop")
foreach ($name in @("start-opencode-web2api.cmd", "stop-opencode-web2api.cmd")) {
    $source = Join-Path $StateDir $name
    if (Test-Path $source) {
        Copy-Item -Force $source (Join-Path $Desktop $name)
    }
}

Write-Host "`nInstallation complete." -ForegroundColor Green
Write-Host "OpenCode is configured globally. The API key is stored only in the OpenCode config, not in the stack state file."
$startNow = Read-Host "Start Web2API, open ChatGPT for login, and start the OpenCode bridge now? [Y/n]"
if ($startNow -notmatch '^[Nn]') {
    & $VenvPython -m chatgpt_web2api.opencode_stack start --background --open-chatgpt
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "The stack did not become ready. Log in to ChatGPT and run the desktop start launcher again."
    }
}

Write-Host "`nNext: open OpenCode and select the chatgpt-web model configured by the wizard."
