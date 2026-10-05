# Stop the bridge and every MCP server it spawned.
$pattern = 'bridge\.py|mcp-persona-bridge\\servers'
$killed = 0
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match $pattern } | ForEach-Object {
    Write-Host ("stopping pid {0}" -f $_.ProcessId)
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    $killed++
}
Write-Host ("stopped {0} process(es)" -f $killed)
