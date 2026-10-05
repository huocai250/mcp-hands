param(
    [string]$Exe = "",
    [switch]$Remove
)
# Register (or remove) the bridge so it starts with Windows, minimized.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Exe) {
    $candidates = @(
        (Join-Path $here "release\aiyu-mcp-bridge\aiyu-mcp-bridge.exe"),
        (Join-Path $here "release\aiyu-mcp-bridge.exe"),
        (Join-Path $here "dist\aiyu-mcp-bridge\aiyu-mcp-bridge.exe"),
        (Join-Path $here "dist\aiyu-mcp-bridge.exe")
    )
    $Exe = ($candidates | Where-Object { Test-Path $_ } | Select-Object -First 1)
}
if (-not $Exe) { Write-Error "exe not found; build it first (build-exe.ps1) or pass -Exe"; exit 1 }

$startup = [Environment]::GetFolderPath("Startup")
$lnk = Join-Path $startup "aiyu-mcp-bridge.lnk"

if ($Remove) {
    Remove-Item $lnk -Force -ErrorAction SilentlyContinue
    Write-Host "autostart removed: $lnk"
    exit 0
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath = $Exe
$shortcut.Arguments = "--autostart"
$shortcut.WorkingDirectory = Split-Path $Exe
$shortcut.WindowStyle = 7
$shortcut.Description = "aiyu MCP bridge control panel (starts the bridge on logon)"
$shortcut.Save()
Write-Host "autostart created: $lnk -> $Exe"
Write-Host "it will launch minimized at every logon; use -Remove to undo."
