param(
    [string]$Config = "bridge.config.json",
    [int]$Port = 0
)
# Start persona-mcp-bridge. Use -Config bridge.config.mock.json for the offline mock upstream.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$py = $null
foreach ($cand in @(
        (Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"))) {
    if (Test-Path $cand) { $py = $cand; break }
}
if (-not $py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (-not $py) { Write-Error "python not found; install Python or pass one on PATH"; exit 1 }

$cfgPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $here $Config }
if (-not (Test-Path $cfgPath)) { Write-Error "config not found: $cfgPath"; exit 1 }
if ($Port -gt 0) {
    $json = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $json.listen.port = $Port
    $tmp = Join-Path $here ("bridge.config.run.json")
    $json | ConvertTo-Json -Depth 8 | Set-Content -Path $tmp -Encoding UTF8
    $cfgPath = $tmp
}

Write-Host "python : $py"
Write-Host "config : $cfgPath"
Write-Host "starting bridge (Ctrl+C to stop)..."
& $py (Join-Path $here "bridge.py") $cfgPath
