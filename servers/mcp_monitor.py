"""MCP server: host monitoring via PowerShell + CIM.

Every tool returns plain text. Slow CIM providers are called with an explicit
subprocess timeout; missing data (e.g. battery on a desktop) is reported as
'not available' instead of raising.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
PS = "powershell" if os.name == "nt" else "pwsh"
PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
TIMEOUT_S = 60
LEVELS = {"Critical": 1, "Error": 2, "Warning": 3, "Information": 4, "Verbose": 5}

srv = Server("monitor")


def _ps(script, timeout_s=TIMEOUT_S):
    """Run PowerShell; guard slow CIM calls with a hard timeout, never raise."""
    try:
        p = subprocess.run(
            [PS, "-NoProfile", "-NonInteractive", "-Command", PS_PREFIX + script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=float(timeout_s), creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return "not available: PowerShell call timed out after %ss" % timeout_s
    except Exception as exc:
        return "not available: %s" % exc
    out = (p.stdout or "").strip()
    if p.stderr and p.stderr.strip():
        out += "\n[stderr] " + p.stderr.strip()[:400]
    return out if out else "(no output)"


CPU_SNIPPET = ("$c=(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average;"
               "if ($null -eq $c) { $c = (Get-Counter '\\Processor(_Total)\\% Processor Time' "
               "-ErrorAction SilentlyContinue).CounterSamples.CookedValue };"
               "if ($null -eq $c) { 'cpu=not available' } else { 'cpu=' + [int]$c + '%' }")

MEM_SNIPPET = ("$o=Get-CimInstance Win32_OperatingSystem;"
               "$t=[int]($o.TotalVisibleMemorySize/1KB);$f=[int]($o.FreePhysicalMemory/1KB);$u=$t-$f;"
               "'mem_used_mb=' + $u + ' mem_total_mb=' + $t + ' mem_used_pct=' + [int](100*$u/$t)")


@srv.tool("snapshot",
          "One compact health block: CPU load %, memory used/total, top 3 processes by memory, free space per drive "
          "and system uptime.",
          {"type": "object", "properties": {}, "required": []})
def snapshot():
    script = (
        CPU_SNIPPET + ";" + MEM_SNIPPET + ";"
        "'-- top 3 processes by memory --';"
        "Get-Process | Sort-Object WS -Descending | Select-Object -First 3 | ForEach-Object "
        "{ 'proc ' + $_.ProcessName + ' pid=' + $_.Id + ' mb=' + [int]($_.WS/1MB) };"
        "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object "
        "{ 'drive ' + $_.DeviceID + ' free_gb=' + [math]::Round($_.FreeSpace/1GB,1) + ' total_gb=' + [math]::Round($_.Size/1GB,1) };"
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "'uptime=' + [string]([datetime]::Now - $os.LastBootUpTime).ToString('d\\d\\ hh\\:mm')"
    )
    return _ps(script)


@srv.tool("cpu_load",
          "Average CPU load percentage over a few samples (a short sleep between reads smooths the number).",
          {"type": "object", "properties": {"samples": {"type": "integer", "default": 3}}, "required": []})
def cpu_load(samples=3):
    n = max(1, min(20, int(samples)))
    script = ("$v=@();1..%d | ForEach-Object { $v += (Get-CimInstance Win32_Processor | "
              "Measure-Object -Property LoadPercentage -Average).Average; Start-Sleep -Milliseconds 400 };"
              "$a=($v | Measure-Object -Average).Average;"
              "'cpu samples=' + ($v -join ',') + ' avg=' + [int]$a + '%%'") % n
    return _ps(script)


@srv.tool("memory_usage", "Physical memory: used MB, total MB, used percentage. Also reports the page file peak.",
          {"type": "object", "properties": {}, "required": []})
def memory_usage():
    return _ps(MEM_SNIPPET + ";" + "'-- pagefile --';"
               "$o=Get-CimInstance Win32_OperatingSystem;'pagefile_free_mb=' + [int]($o.FreeVirtualMemory/1KB);"
               "$p=Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue;"
               "if ($p) { $p | ForEach-Object { 'pagefile ' + $_.Name + ' used_mb=' + $_.CurrentUsage } } "
               "else { 'pagefile: not available' }")


@srv.tool("top_processes", "Top processes by working set (memory).",
          {"type": "object", "properties": {"limit": {"type": "integer", "default": 10}}, "required": []})
def top_processes(limit=10):
    n = max(1, min(200, int(limit)))
    script = ("Get-Process | Sort-Object WS -Descending | Select-Object -First %d | ForEach-Object "
              "{ $_.ProcessName + ' pid=' + $_.Id + ' mb=' + [int]($_.WS/1MB) + ' threads=' + $_.Threads.Count }") % n
    return _ps(script)


@srv.tool("disk_usage", "Free/total space and used percentage for each fixed drive, plus any mounted volumes.",
          {"type": "object", "properties": {}, "required": []})
def disk_usage():
    return _ps(
        "$d=Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue;"
        "if (-not $d) { 'disks: not available' } else { $d | ForEach-Object { "
        "'drive ' + $_.DeviceID + ' type=' + $_.DriveType + ' free_gb=' + [math]::Round($_.FreeSpace/1GB,1) + "
        "' total_gb=' + [math]::Round($_.Size/1GB,1) + ' used_pct=' + "
        "$(if ($_.Size) { [int](100*($_.Size-$_.FreeSpace)/$_.Size) } else { 'n/a' }) } }"
    )


@srv.tool("battery", "Battery / power status. Reports 'not available' on machines without a battery.",
          {"type": "object", "properties": {}, "required": []})
def battery():
    return _ps(
        "$b=Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue;"
        "if (-not $b) { 'battery: not available (desktop or no battery)'; return };"
        "$b | ForEach-Object { 'name=' + $_.Name + ' charge_pct=' + $_.EstimatedChargeRemaining + "
        "' status=' + $_.BatteryStatus };"
        "$s=Get-CimInstance -ClassName BatteryStatus -Namespace root\\wmi -ErrorAction SilentlyContinue;"
        "if ($s) { 'power_online=' + $s.PowerOnline + ' discharging=' + $s.Discharging } "
        "else { 'power line state: not available' }"
    )


@srv.tool("uptime", "How long the machine has been up, plus the last boot time.",
          {"type": "object", "properties": {}, "required": []})
def uptime():
    return _ps(
        "$o=Get-CimInstance Win32_OperatingSystem;"
        "$u=[datetime]::Now - $o.LastBootUpTime;"
        "'uptime=' + [string]$u.ToString('d\\d\\ hh\\:mm\\:ss') + ' last_boot=' + $o.LastBootUpTime"
    )


@srv.tool("net_adapters", "Network adapters: name, status, IP addresses, link speed and bytes sent/received.",
          {"type": "object", "properties": {}, "required": []})
def net_adapters():
    return _ps(
        "$a=Get-NetAdapter -ErrorAction SilentlyContinue;"
        "if (-not $a) { 'adapters: not available (Get-NetAdapter failed)'; return };"
        "foreach ($n in $a) {"
        "$ip=(Get-NetIPAddress -InterfaceIndex $n.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress -join ',';"
        "$st=Get-NetAdapterStatistics -Name $n.Name -ErrorAction SilentlyContinue;"
        "'adapter ' + $n.Name + ' | ' + $n.InterfaceDescription + ' | status=' + $n.Status + "
        "' | link=' + $n.LinkSpeed + ' | ip=' + $ip + "
        "' | sent_mb=' + $(if ($st) { [int]($st.SentBytes/1MB) } else { 'n/a' }) + "
        "' | recv_mb=' + $(if ($st) { [int]($st.ReceivedBytes/1MB) } else { 'n/a' }) }"
    )


@srv.tool("service_list", "Windows services with name, display name, status and start mode (filtered by substring).",
          {"type": "object", "properties": {"filter": {"type": "string", "default": ""},
                                            "limit": {"type": "integer", "default": 40}}, "required": []})
def service_list(filter="", limit=40):
    needle = str(filter or "").replace("'", "''")
    n = max(1, min(500, int(limit)))
    return _ps(
        "$s=Get-CimInstance Win32_Service -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Name -like '*%s*' -or $_.DisplayName -like '*%s*' } | "
        "Select-Object -First %d;"
        "if (-not $s) { 'services: no match' } else { $s | ForEach-Object { "
        "'service ' + $_.Name + ' | ' + $_.DisplayName + ' | state=' + $_.State + ' | start=' + $_.StartMode } }"
        % (needle, needle, n)
    )


@srv.tool("startup_items", "Programs that start with Windows (registry Run keys + the startup folder).",
          {"type": "object", "properties": {}, "required": []})
def startup_items():
    return _ps(
        "$found=$false;"
        "foreach ($k in @('HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run',"
        "'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run',"
        "'HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Run')) {"
        "if (Test-Path $k) { $p=Get-ItemProperty $k; $p.PSObject.Properties | "
        "Where-Object { $_.Name -notlike 'PS*' } | ForEach-Object { $script:found=$true; "
        "'run ' + $k.Split(':')[0] + ' | ' + $_.Name + ' = ' + $_.Value } } };"
        "$sf=Join-Path $env:APPDATA 'Microsoft\\Windows\\Start Menu\\Programs\\Startup';"
        "if (Test-Path $sf) { Get-ChildItem $sf -File | ForEach-Object { $script:found=$true; 'startup_folder | ' + $_.Name } };"
        "if (-not $found) { 'startup items: none found' }"
    )


@srv.tool("event_log_tail",
          "Most recent Windows event log entries, newest first, each message trimmed to 300 chars. "
          "level is Error/Warning/Information/Critical (use 'any' for no filtering).",
          {"type": "object", "properties": {"log": {"type": "string", "default": "System"},
                                            "count": {"type": "integer", "default": 20},
                                            "level": {"type": "string", "default": "Error"}}, "required": []})
def event_log_tail(log="System", count=20, level="Error"):
    lv = str(level or "any").strip()
    lv = lv[0].upper() + lv[1:].lower() if lv else "Any"
    n = max(1, min(200, int(count)))
    logname = str(log or "System").replace("'", "''")
    # Get-WinEvent has no -Level switch; filtering is done through -FilterHashtable.
    filt = "" if lv.lower() == "any" else "-FilterHashtable @{LogName='%s';Level=%d} " % (logname, LEVELS.get(lv, 2))
    return _ps(
        "$e=Get-WinEvent %s-MaxEvents %d -ErrorAction SilentlyContinue;"
        "if (-not $e) { 'events: not available or no matching entries' } else { $e | ForEach-Object { "
        "$m=($_.Message -replace '\\s+',' '); if ($m.Length -gt 300) { $m=$m.Substring(0,300) + '...' };"
        "'[' + $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss') + '] ' + $_.LevelDisplayName + ' id=' + $_.Id + "
        "' src=' + $_.ProviderName + ' :: ' + $m } }" % (filt, n)
    )


@srv.tool("device_list", "Plug-and-play devices; entries with a non-OK status (problem codes) are highlighted.",
          {"type": "object", "properties": {"filter": {"type": "string", "default": ""}}, "required": []})
def device_list(filter=""):
    needle = str(filter or "").replace("'", "''")
    return _ps(
        "$d=Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Name -like '*%s*' };"
        "if (-not $d) { 'devices: no match or not available' } else { $d | ForEach-Object { "
        "$ok = ($_.Status -eq 'OK' -or $_.ConfigManagerErrorCode -eq 0);"
        "($(if ($ok) { 'ok   ' } else { 'PROBLEM code=' + $_.ConfigManagerErrorCode + ' ' })) + $_.Name + "
        "$(if ($_.DeviceID) { ' | ' + ($_.DeviceID -replace '\\\\','/') } else { '' }) } }" % needle
    )


def build():
    return srv


# Safe samples: all read-only CIM/registry queries.
SAMPLES = {
    "snapshot": {},
    "cpu_load": {"samples": 2},
    "memory_usage": {},
    "top_processes": {"limit": 5},
    "disk_usage": {},
    "battery": {},
    "uptime": {},
    "net_adapters": {},
    "service_list": {"filter": "Winmgmt", "limit": 5},
    "startup_items": {},
    "event_log_tail": {"log": "System", "count": 3, "level": "Error"},
    "device_list": {"filter": "Keyboard"},
}


if __name__ == "__main__":
    srv.run()
