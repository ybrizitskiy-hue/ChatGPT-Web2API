[CmdletBinding()]
param(
    [string]$ApiKey = "",
    [string]$Upstream = "http://127.0.0.1:8080",
    [string]$BridgeUrl = "http://127.0.0.1:8010/v1",
    [string]$Model = "auto",
    [string]$Repository = "https://github.com/ybrizitskiy-hue/ChatGPT-Web2API.git",
    [string]$Branch = "master",
    [switch]$ConfigureOnly,
    [switch]$NoSetDefault,
    [switch]$NoStart,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

function Test-PythonExecutable {
    param([string]$Exe, [string[]]$Prefix = @())
    if (-not $Exe) { return $false }
    try {
        & $Exe @Prefix -c "import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 2)" *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Resolve-Python {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($selector in @("-3.13", "-3.12", "-3.11")) {
            if (Test-PythonExecutable -Exe $launcher.Source -Prefix @($selector)) {
                return [pscustomobject]@{ Exe = $launcher.Source; Prefix = @($selector) }
            }
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and (Test-PythonExecutable -Exe $python.Source)) {
        return [pscustomobject]@{ Exe = $python.Source; Prefix = @() }
    }

    $known = @()
    if ($env:LOCALAPPDATA) {
        $known += Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"
        $known += Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
        $known += Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"
    }
    if ($env:ProgramFiles) {
        $known += Join-Path $env:ProgramFiles "Python313\python.exe"
        $known += Join-Path $env:ProgramFiles "Python312\python.exe"
        $known += Join-Path $env:ProgramFiles "Python311\python.exe"
    }
    foreach ($candidate in $known) {
        if ((Test-Path $candidate) -and (Test-PythonExecutable -Exe $candidate)) {
            return [pscustomobject]@{ Exe = $candidate; Prefix = @() }
        }
    }
    return $null
}

function Resolve-Git {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) { return $git.Source }

    $known = @()
    if ($env:ProgramFiles) { $known += Join-Path $env:ProgramFiles "Git\cmd\git.exe" }
    if (${env:ProgramFiles(x86)}) {
        $known += Join-Path ${env:ProgramFiles(x86)} "Git\cmd\git.exe"
    }
    if ($env:LOCALAPPDATA) {
        $known += Join-Path $env:LOCALAPPDATA "Programs\Git\cmd\git.exe"
    }
    if ($script:Base) { $known += Join-Path $script:Base "mingit\cmd\git.exe" }

    foreach ($candidate in $known) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    return $null
}

function Get-Winget {
    $command = Get-Command winget -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    return $null
}

function Install-Python {
    Write-Step "Python 3.11-3.13 was not found; installing Python 3.13"
    if ($DryRun) {
        Write-Host "DRY RUN: would install Python.Python.3.13 or use the python.org fallback."
        return
    }

    $winget = Get-Winget
    if ($winget) {
        Write-Host "Trying winget..."
        & $winget install --id Python.Python.3.13 -e --source winget --scope user --silent --disable-interactivity --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0 -and (Resolve-Python)) { return }
        Write-Warning "winget did not produce a usable Python; using python.org fallback."
    }

    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
    $version = "3.13.14"
    $installer = Join-Path $script:Downloads "python-$version-$arch.exe"
    $url = "https://www.python.org/ftp/python/$version/python-$version-$arch.exe"
    Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing

    $signature = Get-AuthenticodeSignature -FilePath $installer
    if ($signature.Status -ne "Valid") {
        throw "Downloaded Python installer signature is not valid: $($signature.Status)"
    }

    $arguments = @(
        "/quiet",
        "InstallAllUsers=0",
        "PrependPath=1",
        "Include_launcher=1",
        "Include_pip=1",
        "Include_test=0",
        "Shortcuts=0"
    )
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Python installer exited with code $($process.ExitCode)."
    }
    if (-not (Resolve-Python)) {
        throw "Python installation completed, but Python 3.11-3.13 is not discoverable."
    }
}

function Install-Git {
    Write-Step "Git was not found; installing Git"
    if ($DryRun) {
        Write-Host "DRY RUN: would install Git.Git or use the MinGit fallback."
        return
    }

    $winget = Get-Winget
    if ($winget) {
        Write-Host "Trying winget..."
        & $winget install --id Git.Git -e --source winget --scope user --silent --disable-interactivity --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0 -and (Resolve-Git)) { return }
        Write-Warning "winget did not produce a usable Git; using MinGit fallback."
    }

    $headers = @{
        "User-Agent" = "ChatGPT-Web2API-OpenCode-Installer"
        "Accept" = "application/vnd.github+json"
        "X-GitHub-Api-Version" = "2022-11-28"
    }
    $release = Invoke-RestMethod `
        -Uri "https://api.github.com/repos/git-for-windows/git/releases/latest" `
        -Headers $headers
    $assetPattern = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") {
        '^MinGit-.*-arm64\.zip$'
    } else {
        '^MinGit-.*-64-bit\.zip$'
    }
    $asset = $release.assets |
        Where-Object { $_.name -match $assetPattern } |
        Select-Object -First 1
    if (-not $asset) {
        throw "Could not find a suitable MinGit asset in the latest Git for Windows release."
    }

    $archive = Join-Path $script:Downloads $asset.name
    $destination = Join-Path $script:Base "mingit"
    if (Test-Path $destination) { Remove-Item -Recurse -Force $destination }
    Invoke-WebRequest `
        -Uri $asset.browser_download_url `
        -OutFile $archive `
        -Headers $headers `
        -UseBasicParsing

    if ($asset.digest -and $asset.digest -match '^sha256:(?<hash>[0-9a-fA-F]{64})$') {
        $actualHash = (Get-FileHash -Algorithm SHA256 -Path $archive).Hash
        if ($actualHash -ne $Matches.hash) {
            throw "MinGit SHA256 mismatch."
        }
    }

    Expand-Archive -Path $archive -DestinationPath $destination -Force
    if (-not (Resolve-Git)) {
        throw "MinGit extraction completed, but git.exe is not discoverable."
    }
}

function Get-PythonVersionText {
    param($Python)
    $prefix = @($Python.Prefix)
    return (& $Python.Exe @prefix -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
}

$script:Base = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "ChatGPT-Web2API-OpenCode"
} else {
    Join-Path $HOME ".chatgpt-web2api-opencode"
}
$script:Downloads = Join-Path $script:Base "downloads"
$sourceDir = Join-Path $script:Base "source"
$venv = Join-Path $script:Base "venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
New-Item -ItemType Directory -Force -Path $script:Base, $script:Downloads | Out-Null

Write-Host "ChatGPT-Web2API + OpenCode one-click installer" -ForegroundColor Green
Write-Host "Install directory: $script:Base"

$gitExe = Resolve-Git
if (-not $gitExe) {
    Install-Git
    $gitExe = Resolve-Git
}
$pythonCommand = Resolve-Python
if (-not $pythonCommand) {
    Install-Python
    $pythonCommand = Resolve-Python
}

if ($DryRun) {
    Write-Host ""
    Write-Host "DRY RUN completed successfully." -ForegroundColor Green
    exit 0
}

if (-not $gitExe) { throw "Git is required but could not be installed." }
if (-not $pythonCommand) { throw "Python 3.11-3.13 is required but could not be installed." }
Write-Host "Git: $gitExe"
Write-Host "Python: $($pythonCommand.Exe) $(Get-PythonVersionText -Python $pythonCommand)"

Write-Step "Downloading/updating ChatGPT-Web2API"
if (Test-Path (Join-Path $sourceDir ".git")) {
    Invoke-Checked -FilePath $gitExe -Arguments @("-C", $sourceDir, "fetch", "--prune", "origin")
    Invoke-Checked -FilePath $gitExe -Arguments @("-C", $sourceDir, "checkout", "-B", $Branch, "origin/$Branch")
    Invoke-Checked -FilePath $gitExe -Arguments @("-C", $sourceDir, "reset", "--hard", "origin/$Branch")
} else {
    if (Test-Path $sourceDir) { Remove-Item -Recurse -Force $sourceDir }
    Invoke-Checked -FilePath $gitExe -Arguments @(
        "clone", "--branch", $Branch, "--single-branch", $Repository, $sourceDir
    )
}

Write-Step "Creating isolated Python environment"
$pythonPrefix = @($pythonCommand.Prefix)
if (-not (Test-Path $venvPython)) {
    & $pythonCommand.Exe @pythonPrefix -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create Python virtual environment." }
}
Invoke-Checked -FilePath $venvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Checked -FilePath $venvPython -Arguments @(
    "-m", "pip", "install", "--upgrade", $sourceDir
)

Write-Step "Configuring Web2API and OpenCode"
$setupArgs = @(
    "-m", "chatgpt_web2api.opencode_setup", "setup",
    "--non-interactive",
    "--upstream", $Upstream,
    "--bridge-url", $BridgeUrl,
    "--model", $Model
)
if (-not $NoSetDefault) { $setupArgs += "--set-default" }

$oldSecret = $env:W2A_OPENCODE_API_KEY
try {
    if ($ApiKey) { $env:W2A_OPENCODE_API_KEY = $ApiKey }
    & $venvPython @setupArgs
    if ($LASTEXITCODE -ne 0) {
        throw "OpenCode integration setup failed with exit code $LASTEXITCODE."
    }
} finally {
    if ($null -eq $oldSecret) {
        Remove-Item Env:W2A_OPENCODE_API_KEY -ErrorAction SilentlyContinue
    } else {
        $env:W2A_OPENCODE_API_KEY = $oldSecret
    }
}

$keyPath = Join-Path $HOME ".chatgpt-web2api\opencode-api-key"
$launcher = Join-Path $HOME ".chatgpt-web2api\start-opencode-web2api.cmd"
Write-Host ""
Write-Host "Configuration complete." -ForegroundColor Green
Write-Host "OpenCode baseURL: $BridgeUrl"
Write-Host "API key file: $keyPath"
Write-Host "OpenCode reads the key from that file automatically; you do not paste it manually."

if (-not $ConfigureOnly -and -not $NoStart) {
    if (Test-Path $launcher) {
        Write-Step "Starting ChatGPT-Web2API and the OpenCode bridge"
        Start-Process -FilePath "cmd.exe" -ArgumentList @("/k", "`"$launcher`"") | Out-Null
        Write-Host "The service window is starting. Web2API will open its managed Chrome profile."
        Write-Host "Sign in to ChatGPT in that managed Chrome window if prompted."
    } else {
        Write-Warning "Generated launcher was not found at $launcher."
        Write-Warning "Run: $venvPython -m chatgpt_web2api.opencode_setup start"
    }
}

Write-Host ""
Write-Host "DONE" -ForegroundColor Green
Write-Host "1. Sign in to ChatGPT in the Chrome profile opened by Web2API."
Write-Host "2. Restart/open OpenCode."
Write-Host "3. Select the chatgpt-web/$Model provider/model (or use OpenCode model selection)."
Write-Host ""
Write-Host "No OpenAI API key is required. The generated key is only a local bridge password."
