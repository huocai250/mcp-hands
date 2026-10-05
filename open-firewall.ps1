param(
    [string]$Ports = "8877,8890",
    [string]$Exe = "",
    [string]$Display = "aiyu-mcp-bridge",
    [switch]$Remove
)
# Let the phone (same LAN) reach the bridge / proxy ports.
# Needs an elevated PowerShell: the control panel launches it with -Verb RunAs,
# or run:  Start-Process powershell -Verb RunAs -ArgumentList '-File','open-firewall.ps1'
$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $elevated) {
    Write-Host "警告：当前不是管理员，添加防火墙规则会失败。"
    Write-Host "请用管理员 PowerShell 重新运行，或在控制台里点「放行防火墙端口」（会弹 UAC）。"
    Write-Host ""
}

foreach ($item in ($Ports -split ',')) {
    $port = $item.Trim()
    if (-not $port) { continue }
    $rule = "$Display $port"
    if ($Remove) {
        netsh advfirewall firewall delete rule name="$rule" | Out-Null
        Write-Host "removed firewall rule: $rule"
        continue
    }
    netsh advfirewall firewall delete rule name="$rule" | Out-Null
    netsh advfirewall firewall add rule name="$rule" dir=in action=allow protocol=TCP localport=$port profile=private,domain | Out-Null
    Write-Host "allowed inbound TCP $port (private/domain profiles)"
}

if ($Exe -and -not $Remove) {
    $rule = "$Display program"
    netsh advfirewall firewall add rule name="$rule" dir=in action=allow program="$Exe" enable=yes profile=private,domain | Out-Null
    Write-Host "program rule added for $Exe"
}

Write-Host ""
Write-Host "done."
