param(
    [switch]$OneFile,
    [string]$Venv = ".build-venv"
)
# Rebuild aiyu-mcp-bridge.exe. Creates the build venv on first run.
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$py = Join-Path $here "$Venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "creating build venv at $Venv ..."
    & "py" -3.12 -m venv (Join-Path $here $Venv)
    & $py -m pip install --upgrade pip
    & $py -m pip install pyinstaller python-docx openpyxl python-pptx pillow
}

$common = @(
    "--noconfirm", "--clean", "--console", "--name", "aiyu-mcp-bridge",
    "--paths", ".", "--paths", "servers",
    "--hidden-import", "mcpserver",
    "--hidden-import", "tkinter",
    "--hidden-import", "qrcode.image.svg",
    "--hidden-import", "pypdf",
    "--collect-submodules", "servers",
    "--exclude-module", "numpy", "--exclude-module", "pandas",
    "--exclude-module", "matplotlib",
    "--distpath", "release", "--workpath", "build", "--specpath", "."
)
$mode = if ($OneFile) { "--onefile" } else { "--onedir" }

Write-Host "building ($mode) ..."
& $py -m PyInstaller @common $mode gui.py

$target = if ($OneFile) { "release\aiyu-mcp-bridge.exe" } else { "release\aiyu-mcp-bridge\aiyu-mcp-bridge.exe" }
if (Test-Path $target) {
    Write-Host ""
    Write-Host "built: $target"
    & (Join-Path $here $target) --tools | Select-Object -Last 1
} else {
    Write-Error "build failed: $target not found"
}
