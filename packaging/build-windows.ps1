$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectDir

New-Item -ItemType Directory -Force -Path vendor, release | Out-Null
$Cloudflared = Join-Path $ProjectDir "vendor\cloudflared.exe"
if (-not (Test-Path $Cloudflared)) {
    Write-Host "[Project X] Downloading Cloudflare Tunnel for Windows x64..."
    Invoke-WebRequest `
        -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
        -OutFile $Cloudflared
}

python -m pip install --upgrade -r requirements-build.txt
python -m PyInstaller --noconfirm --clean project-x.spec

$Portable = Join-Path $ProjectDir "release\Project-X-Windows-x64-Portable.zip"
Remove-Item $Portable -Force -ErrorAction SilentlyContinue
Compress-Archive -Path "dist\Project X\*" -DestinationPath $Portable -CompressionLevel Optimal

$IsccCandidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
)
$Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($Iscc) {
    & $Iscc "packaging\project-x.iss"
    Write-Host "[Project X] Built installer and portable ZIP in release\."
} else {
    Write-Warning "Inno Setup 6 was not found; built the portable ZIP only."
}
