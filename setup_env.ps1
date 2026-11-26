[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12",
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPath = Join-Path $projectRoot ".venv"
$requirementsPath = Join-Path $projectRoot "requirements.txt"
$venvPython = Join-Path $venvPath "Scripts\python.exe"

Write-Host "Kommand environment setup" -ForegroundColor Cyan
Write-Host "Project: $projectRoot"

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCommand) {
    throw @"
uv is required to download Python and create the environment.

Install it with either:
  winget install --id=astral-sh.uv -e
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

Then run this script again.
"@
}

if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) {
    throw "Requirements file not found: $requirementsPath"
}

if ($Recreate -and (Test-Path -LiteralPath $venvPath)) {
    $resolvedRoot = [System.IO.Path]::GetFullPath($projectRoot)
    $resolvedVenv = [System.IO.Path]::GetFullPath($venvPath)
    $expectedVenv = [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot ".venv"))
    if ($resolvedVenv -ne $expectedVenv -or -not $resolvedVenv.StartsWith($resolvedRoot)) {
        throw "Refusing to remove an unexpected environment path: $resolvedVenv"
    }
    Write-Host "Removing existing .venv..." -ForegroundColor Yellow
    Remove-Item -LiteralPath $resolvedVenv -Recurse -Force
}

Write-Host "Ensuring Python $PythonVersion is available..." -ForegroundColor Cyan
& $uvCommand.Source python install $PythonVersion
if ($LASTEXITCODE -ne 0) {
    throw "uv could not install Python $PythonVersion."
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Host "Creating .venv..." -ForegroundColor Cyan
    & $uvCommand.Source venv $venvPath --python $PythonVersion
    if ($LASTEXITCODE -ne 0) {
        throw "uv could not create the virtual environment."
    }
} else {
    Write-Host "Using existing .venv. Pass -Recreate to replace it."
}

Write-Host "Downloading and installing dependencies..." -ForegroundColor Cyan
& $uvCommand.Source pip install --python $venvPython --requirements $requirementsPath
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed."
}

if ($env:OS -eq "Windows_NT") {
    $vlcVersion = "3.0.24"
    $vlcPath = Join-Path $venvPath "vlc-$vlcVersion"
    if (-not (Test-Path -LiteralPath (Join-Path $vlcPath "libvlc.dll"))) {
        $archive = Join-Path $venvPath "vlc-$vlcVersion-win64.zip"
        Write-Host "Installing portable VLC $vlcVersion for native video playback..." -ForegroundColor Cyan
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "https://download.videolan.org/pub/videolan/vlc/$vlcVersion/win64/vlc-$vlcVersion-win64.zip" -OutFile $archive
            $expected = "fcf30850371ad10c9373cc4f0f4501e7dee49e3e9ae9f20c72fb2661a1ca6323"
            if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
                throw "VLC archive checksum verification failed."
            }
            Expand-Archive -LiteralPath $archive -DestinationPath $venvPath -Force
        } finally {
            if (Test-Path -LiteralPath $archive) {
                Remove-Item -LiteralPath $archive -Force
            }
        }
    }
}

Write-Host "Verifying Kommand Source imports..." -ForegroundColor Cyan
$sourcePath = Join-Path $projectRoot "kommand-source\src"
$centralPath = Join-Path $projectRoot "kommand-central\src"
$commonPath = Join-Path $projectRoot "kommand-common\src"
& $venvPython -c "import sys; sys.path[:0] = [r'$sourcePath', r'$centralPath', r'$commonPath']; import kommand_source.main, kommand_central.main; print('Kommand application imports succeeded.')"
if ($LASTEXITCODE -ne 0) {
    throw "Kommand Source verification failed."
}

Write-Host ""
Write-Host "Environment ready." -ForegroundColor Green
Write-Host "Run Source with:    .\run_source.cmd"
Write-Host "Run Central with:   .\run_central.cmd"
Write-Host ""
Write-Host "Activation is optional. To use Python directly:"
Write-Host "  .\.venv\Scripts\python.exe .\kommand-source\run.py"

