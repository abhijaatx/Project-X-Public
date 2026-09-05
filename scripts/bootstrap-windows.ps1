<#
Project X Windows bootstrapper.

This script is intentionally self-contained so it can be downloaded and run
from a fresh PowerShell session. It installs only the two public prerequisites
when they are missing, keeps the checkout in %USERPROFILE%\Project-X-Public, and then
hands off to the versioned host code.
#>

[CmdletBinding()]
param(
    [string]$ProjectDir = (Join-Path $env:USERPROFILE "Project-X-Public"),
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

function Test-Python([string]$Executable, [string[]]$PrefixArgs = @()) {
    if (-not $Executable) {
        return $false
    }
    try {
        & $Executable @PrefixArgs -c `
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" `
            *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        # This also rejects the Windows Store python.exe application alias.
        return $false
    }
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Id,
        [switch]$Force
    )
    $winget = Find-Executable @("winget.exe", "winget")
    if (-not $winget) {
        throw "winget is unavailable. Install $Id from its official vendor, then run this command again."
    }
    Write-Host "[Project X] Installing $Id..."
    $wingetArgs = @(
        "install", "--id", $Id, "--exact", "--source", "winget",
        "--accept-source-agreements", "--accept-package-agreements"
    )
    if ($Force) {
        $wingetArgs += "--force"
    }
    & $winget @wingetArgs
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not install $Id (exit code $LASTEXITCODE)."
    }
    Refresh-ProcessPath
}

function Remove-IncompleteVenv([string]$VenvDir, [string]$VenvPython) {
    if ((Test-Path $VenvDir) -and -not (Test-Path $VenvPython)) {
        Write-Host "[Project X] Removing the incomplete Python environment..."
        Remove-Item -LiteralPath $VenvDir -Recurse -Force
    }
}

function New-ProjectVenv(
    [string]$VenvDir,
    [string]$VenvPython,
    [string]$Launcher,
    [bool]$LauncherReady,
    [string]$PythonExecutable,
    [bool]$PythonReady
) {
    Remove-IncompleteVenv $VenvDir $VenvPython

    if ($LauncherReady) {
        Write-Host "[Project X] Trying Python 3.12 through the py launcher..."
        & $Launcher -3.12 -m venv $VenvDir
        if ($LASTEXITCODE -eq 0 -and (Test-Path $VenvPython)) {
            return $true
        }
        Remove-IncompleteVenv $VenvDir $VenvPython

        Write-Host "[Project X] Trying the newest Python from the py launcher..."
        & $Launcher -3 -m venv $VenvDir
        if ($LASTEXITCODE -eq 0 -and (Test-Path $VenvPython)) {
            return $true
        }
        Remove-IncompleteVenv $VenvDir $VenvPython
    }

    if ($PythonReady) {
        Write-Host "[Project X] Trying $PythonExecutable..."
        & $PythonExecutable -m venv $VenvDir
        if ($LASTEXITCODE -eq 0 -and (Test-Path $VenvPython)) {
            return $true
        }
        Remove-IncompleteVenv $VenvDir $VenvPython
    }
    return $false
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
$pythonLauncherReady = Test-Python $pythonLauncher @("-3")
$pythonReady = Test-Python $python
if (-not $pythonLauncherReady -and -not $pythonReady) {
    Install-WingetPackage "Python.Python.3.12"
    $pythonLauncher = Find-Executable @("py.exe", "py")
    $python = Find-Executable @("python.exe", "python")
    $pythonLauncherReady = Test-Python $pythonLauncher @("-3")
    $pythonReady = Test-Python $python
}
if (-not $pythonLauncherReady -and -not $pythonReady) {
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
$venvDir = Join-Path $ProjectDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[Project X] Creating the Python environment..."
    $created = New-ProjectVenv `
        $venvDir $venvPython `
        $pythonLauncher $pythonLauncherReady `
        $python $pythonReady

    if (-not $created) {
        Write-Warning "The existing Python installation cannot create virtual environments."
        Install-WingetPackage "Python.Python.3.12" -Force

        $pythonLauncher = Find-Executable @("py.exe", "py")
        $pythonLauncherReady = Test-Python $pythonLauncher @("-3")
        $python = Find-Executable @("python.exe", "python")
        $officialPythonCandidates = @(
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
            (Join-Path $env:ProgramFiles "Python312\python.exe")
        )
        foreach ($candidate in $officialPythonCandidates) {
            if (Test-Python $candidate) {
                $python = $candidate
                break
            }
        }
        $pythonReady = Test-Python $python

        $created = New-ProjectVenv `
            $venvDir $venvPython `
            $pythonLauncher $pythonLauncherReady `
            $python $pythonReady
    }
    if (-not $created -or -not (Test-Path $venvPython)) {
        throw "Official Python 3.12 could not create the .venv environment. Restart Windows and run the same command again."
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
