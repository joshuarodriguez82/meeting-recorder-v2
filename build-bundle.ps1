# Build the self-contained runtime bundle that ships with the installer.
# Run this once after adding or upgrading backend dependencies; the zip
# it produces is what Tauri embeds as a resource.
#
# Outputs: backend-bundle.zip (~500 MB) in the repo root.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 1. Ensure Python 3.11 embeddable is downloaded and unpacked at backend/python/
$pythonDir = "backend/python"
if (-not (Test-Path "$pythonDir/pythonw.exe")) {
    Write-Host "Downloading Python 3.11 embeddable..."
    New-Item -ItemType Directory -Force $pythonDir | Out-Null
    $zip = "$env:TEMP\python-embed.zip"
    Invoke-WebRequest `
        -Uri "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip" `
        -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $pythonDir -Force
    Remove-Item $zip

    # Enable `site` + site-packages path so pip and our deps work
    $pth = "$pythonDir/python311._pth"
    (Get-Content $pth) |
        ForEach-Object { if ($_ -eq "#import site") { "import site" } else { $_ } } |
        Set-Content $pth
    Add-Content $pth "Lib\site-packages"

    # Bootstrap pip
    $getpip = "$env:TEMP\get-pip.py"
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getpip -UseBasicParsing
    & "$pythonDir/python.exe" $getpip --no-warn-script-location
    Remove-Item $getpip

    # Install CPU torch + rest of requirements
    & "$pythonDir/python.exe" -m pip install `
        --index-url https://download.pytorch.org/whl/cpu `
        "torch==2.2.2" "torchaudio==2.2.2"
    & "$pythonDir/python.exe" -m pip install -r backend/requirements-cpu.txt
}

# 2. Clean junk before zipping
Get-ChildItem backend -Recurse -Directory -Filter __pycache__ -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force
if (Test-Path backend/recordings) {
    Remove-Item -Recurse -Force backend/recordings -ErrorAction SilentlyContinue
}
if (Test-Path backend/.env) {
    Remove-Item -Force backend/.env
}

# 3. Build the zip. Use tar.exe (BSD libarchive) — WAY faster than
#    Compress-Archive on large trees and produces cleaner zips.
Write-Host "Zipping backend into backend-bundle.zip (this can take 1-2 minutes)..."
$outZip = "backend-bundle.zip"
if (Test-Path $outZip) { Remove-Item $outZip }

$includes = @(
    "backend/python",
    "backend/config",
    "backend/core",
    "backend/meeting_recorder",
    "backend/models",
    "backend/services",
    "backend/utils",
    "backend/server.py",
    "backend/requirements-cpu.txt"
)
# tar -a deduces the format from .zip extension. Use --format=zip explicitly
# for clarity. -C strips the backend/ prefix from paths inside the archive.
tar.exe -a -c -f $outZip -C backend @(
    "python", "config", "core", "meeting_recorder",
    "models", "services", "utils",
    "server.py", "requirements-cpu.txt"
)

$mb = [int]((Get-Item $outZip).Length / 1MB)
Write-Host "Done. backend-bundle.zip is $mb MB."
