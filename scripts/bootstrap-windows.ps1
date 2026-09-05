<#
Project X Windows bootstrapper.

This script is intentionally self-contained so it can be downloaded and run
from a fresh PowerShell session. It installs only the two public prerequisites
when they are missing, keeps the checkout in %USERPROFILE%\Project-X, and then
hands off to the versioned host code.
#>

[CmdletBinding()]
param(
    [string]$ProjectDir = (Join-Path $env:USERPROFILE "Project-X"),
    [string]$Repository = "https://github.com/abhijaatx/Project-X-Public.git",
    [string]$Pin = "",
    [int]$Port = 5001
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Find-Executable([string[]]$Names) {
    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            return $command.Source
        }
    }
    return $null
}

function Install-WingetPackage([string]$Id) {
    $winget = Find-Executable @("winget.exe", "winget")
    if (-not $winget) {
        throw "winget is unavailable. Install $Id from its official vendor, then run this command again."
    }
    Write-Host "[Project X] Installing $Id..."
    & $winget install --id $Id --exact --source winget `
        --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not install $Id (exit code $LASTEXITCODE)."
    }
    Refresh-ProcessPath
}

Write-Host "[Project X] Checking Windows prerequisites..."
$git = Find-Executable @("git.exe", "git")
if (-not $git) {
    Install-WingetPackage "Git.Git"
    $git = Find-Executable @("git.exe", "git")
}
if (-not $git) {
    throw "Git was not found after installation. Open a new PowerShell session and retry."
}

$pythonLauncher = Find-Executable @("py.exe", "py")
$python = Find-Executable @("python.exe", "python")
if (-not $pythonLauncher -and -not $python) {
    Install-WingetPackage "Python.Python.3.12"
    $pythonLauncher = Find-Executable @("py.exe", "py")
    $python = Find-Executable @("python.exe", "python")
}
if (-not $pythonLauncher -and -not $python) {
    throw "Python was not found after installation. Open a new PowerShell session and retry."
}

$projectParent = Split-Path -Parent $ProjectDir
if ($projectParent) {
    New-Item -ItemType Directory -Force -Path $projectParent | Out-Null
}

if (Test-Path (Join-Path $ProjectDir ".git")) {
    Write-Host "[Project X] Updating $ProjectDir..."
    & $git -C $ProjectDir pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        throw "The existing Project X checkout could not be fast-forwarded. Resolve it manually, then retry."
    }
} elseif (Test-Path $ProjectDir) {
    $items = @(Get-ChildItem -Force -Path $ProjectDir)
    if ($items.Count -gt 0) {
        throw "$ProjectDir exists and is not an empty Project X checkout. Choose another -ProjectDir or move it aside."
    }
    Write-Host "[Project X] Cloning Project X..."
    & $git clone $Repository $ProjectDir
    if ($LASTEXITCODE -ne 0) {
        throw "Could not clone $Repository."
    }
} else {
    Write-Host "[Project X] Cloning Project X..."
    & $git clone $Repository $ProjectDir
    if ($LASTEXITCODE -ne 0) {
        throw "Could not clone $Repository."
    }
}

Set-Location $ProjectDir
$venvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[Project X] Creating the Python environment..."
    if ($pythonLauncher) {
        & $pythonLauncher -3.12 -m venv (Join-Path $ProjectDir ".venv")
        if ($LASTEXITCODE -ne 0) {
            & $pythonLauncher -3 -m venv (Join-Path $ProjectDir ".venv")
        }
    } else {
        & $python -m venv (Join-Path $ProjectDir ".venv")
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        throw "Python could not create the .venv environment."
    }
}

Write-Host "[Project X] Installing capture and media dependencies..."
& $venvPython -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "pip could not be upgraded."
}
& $venvPython -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "Project X dependencies could not be installed."
}

# DXGI Desktop Duplication avoids Windows Graphics Capture's privacy border.
$env:PROJECTX_WINDOWS_CAPTURE_BACKEND = "dxgi"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $ruleName = "Project X Host TCP $Port"
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound `
            -Protocol TCP -LocalPort $Port -Action Allow -Profile Private | Out-Null
    }
    $udpRuleName = "Project X WebRTC UDP"
    $pythonProgram = (Resolve-Path $venvPython).Path
    if (-not (Get-NetFirewallRule -DisplayName $udpRuleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $udpRuleName -Direction Inbound `
            -Protocol UDP -Program $pythonProgram -Action Allow -Profile Private | Out-Null
    }
} else {
    Write-Warning "PowerShell is not elevated; Windows Firewall was not changed. Allow Python on Private networks if the viewer cannot connect."
}

Write-Host "[Project X] Starting the LAN host on port $Port..."
$startArgs = @("start.py", "--no-tunnel", "--host", "0.0.0.0", "--port", $Port)
if ($Pin) {
    $startArgs += @("--pin", $Pin)
}
& $venvPython @startArgs
exit $LASTEXITCODE
