# update.ps1 — update mcp-hands from GitHub with a safety net.
# mcp-hands — https://github.com/huocai250/mcp-hands — MIT License
#
#   powershell -ExecutionPolicy Bypass -File .\update.ps1              # check + install
#   powershell -ExecutionPolicy Bypass -File .\update.ps1 -CheckOnly   # just report
#   powershell -ExecutionPolicy Bypass -File .\update.ps1 -Asset <zip> # install a local zip
#
# What it does:
#   1. ask the GitHub API for the newest release (no auth needed);
#   2. compare with the installed version (mcp-hands.exe --version);
#   3. stop the service, back up the current install into .\backup-<stamp>\;
#   4. download the release asset, verify the zip opens, install it;
#   5. restart the service and print the new version;
#   6. on any failure, roll back from the backup.
param(
    [switch]$CheckOnly,
    [string]$Asset = "",
    [string]$Port = "8877",
    [switch]$KeepBackup
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$repo = "huocai250/mcp-hands"
$exe = Join-Path $here "mcp-hands.exe"
if (-not (Test-Path $exe)) { Write-Error "mcp-hands.exe not found next to this script"; exit 2 }

function Get-InstalledVersion {
    $out = & $exe --version 2>$null | Select-Object -First 1
    if ($out -match "v([0-9]+\.[0-9]+\.[0-9]+)") { return $Matches[1] }
    return "0.0.0"
}

$installed = Get-InstalledVersion
Write-Host "installed : $installed"

if ($Asset) {
    $assetPath = $Asset
    $latest = "local:$([IO.Path]::GetFileName($assetPath))"
} else {
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" `
            -Headers @{ "User-Agent" = "mcp-hands-update"; "Accept" = "application/vnd.github+json" } -TimeoutSec 30
    } catch {
        Write-Host "could not reach GitHub: $($_.Exception.Message)"
        exit 1
    }
    $latest = ($release.tag_name -replace "^v", "")
    Write-Host "published : $latest ($($release.published_at))"
    if ($latest -eq $installed) {
        Write-Host "already up to date"
        exit 0
    }
    $assetUrl = ($release.assets | Where-Object { $_.name -like "*windows-x64.zip" } | Select-Object -First 1).browser_download_url
    if (-not $assetUrl) { Write-Host "that release has no windows-x64 asset"; exit 1 }
}

if ($CheckOnly) {
    Write-Host "update available: $latest (run without -CheckOnly to install)"
    exit 0
}

$tmp = Join-Path $env:TEMP ("mcp-hands-update-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

if (-not $Asset) {
    $zip = Join-Path $tmp "mcp-hands.zip"
    Write-Host "downloading $assetUrl ..."
    Invoke-WebRequest -Uri $assetUrl -OutFile $zip -TimeoutSec 600
} else {
    $zip = $assetPath
}

try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::OpenRead($zip).Dispose()
} catch {
    Write-Host "downloaded file is not a valid zip: $($_.Exception.Message)"
    exit 1
}
Write-Host ("zip ok: {0:N1} MB" -f ((Get-Item $zip).Length / 1MB))

$backup = Join-Path $here ("backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
Write-Host "stopping the service ..."
Get-Process mcp-hands -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

Write-Host "backing up to $backup ..."
New-Item -ItemType Directory -Force -Path $backup | Out-Null
foreach ($item in Get-ChildItem $here) {
    if ($item.Name -in @("backup-" + "", "update.ps1")) { continue }
    if ($item.Name -like "backup-*") { continue }
    Copy-Item $item.FullName -Destination $backup -Recurse -Force -ErrorAction SilentlyContinue
}

try {
    Write-Host "installing ..."
    $staging = Join-Path $tmp "staging"
    Expand-Archive -Path $zip -DestinationPath $staging -Force
    $source = Join-Path $staging "mcp-hands"
    if (-not (Test-Path (Join-Path $source "mcp-hands.exe"))) {
        $source = (Get-ChildItem $staging -Directory | Select-Object -First 1).FullName
    }
    if (-not (Test-Path (Join-Path $source "mcp-hands.exe"))) { throw "the archive does not contain mcp-hands.exe" }
    # keep the user's own config/log, replace everything else
    $keep = @("bridge.config.json", "bridge.log", "gui-settings.json", "jobs.db", "plans.db", "audit.db", "devices.json")
    $saved = @{}
    foreach ($name in $keep) {
        $path = Join-Path $here $name
        if (Test-Path $path) { $saved[$name] = [IO.File]::ReadAllBytes($path) }
    }
    robocopy $source $here /MIR /NFL /NDL /NJH /NJS /R:1 /W:1 | Out-Null
    foreach ($name in $saved.Keys) { [IO.File]::WriteAllBytes((Join-Path $here $name), $saved[$name]) }
    if (Test-Path (Join-Path $here "outbox")) { } else { New-Item -ItemType Directory -Force -Path (Join-Path $here "outbox") | Out-Null }
} catch {
    Write-Host "install failed: $($_.Exception.Message) -- rolling back"
    robocopy $backup $here /MIR /NFL /NDL /NJH /NJS /R:1 /W:1 | Out-Null
    Start-Process -FilePath $exe -ArgumentList "--autostart" -WorkingDirectory $here
    exit 1
}

Write-Host "restarting ..."
Start-Process -FilePath (Join-Path $here "mcp-hands.exe") -ArgumentList "--autostart" -WorkingDirectory $here
Start-Sleep -Seconds 15
$now = Get-InstalledVersion
Write-Host "installed : $now"
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v2/health" -TimeoutSec 15
    Write-Host "health    : v$($health.version), servers=$($health.servers)"
} catch {
    Write-Host "health    : not answering yet (check the console log)"
}
if (-not $KeepBackup) { Write-Host "backup kept at $backup (delete it when the new build looks fine)" }
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
exit 0
