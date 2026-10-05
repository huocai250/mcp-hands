"""MCP server: host interaction -- time, clipboard, open files, screenshot, toast."""
import base64
import datetime
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
srv = Server("sys")


def _ps(script, timeout_s=60, input_text=None):
    # UTF-8 console output, otherwise non-ASCII (e.g. a localized OS caption) arrives as mojibake
    script = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;" + script
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=float(timeout_s), input=input_text, creationflags=NO_WINDOW,
        )
    except Exception as exc:
        return "error: %s" % exc
    out = (p.stdout or "").strip()
    if p.stderr and p.stderr.strip():
        out += "\n[stderr] " + p.stderr.strip()
    return "exit=%s\n%s" % (p.returncode, out)


@srv.tool("now", "Current local date/time and timezone.",
          {"type": "object", "properties": {}, "required": []})
def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


@srv.tool("clipboard_get", "Read the Windows clipboard as text.",
          {"type": "object", "properties": {}, "required": []})
def clipboard_get():
    return _ps("Get-Clipboard -Raw")


@srv.tool("clipboard_set", "Put text on the Windows clipboard.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def clipboard_set(text):
    return _ps("Set-Clipboard -Value ([Console]::In.ReadToEnd())", input_text=text)


@srv.tool("open_path", "Open a file, folder or URL with the default Windows application.",
          {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]})
def open_path(target):
    t = os.path.expanduser(str(target))
    if os.name == "nt":
        os.startfile(t)  # noqa: S606 - intentional host interaction
    else:
        subprocess.Popen(["xdg-open", t])
    return "opened: %s" % t


@srv.tool("screenshot", "Capture the primary screen to a PNG and return its path.",
          {"type": "object", "properties": {"path": {"type": "string", "default": ""}}, "required": []})
def screenshot(path=""):
    dest = path or os.path.join(os.environ.get("TEMP", "."), "shot_%s.png" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    dest = os.path.abspath(os.path.expanduser(dest))
    script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
        "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
        "$g=[System.Drawing.Graphics]::FromImage($bmp);"
        "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
        "$bmp.Save('%s',[System.Drawing.Imaging.ImageFormat]::Png);"
        "$g.Dispose();$bmp.Dispose();'saved'" % dest.replace("'", "''")
    )
    out = _ps(script, 60)
    if os.path.exists(dest):
        return "saved: %s (%d bytes)" % (dest, os.path.getsize(dest))
    return out


@srv.tool("toast", "Show a Windows balloon/toast notification.",
          {"type": "object", "properties": {"title": {"type": "string"}, "message": {"type": "string"}}, "required": ["message"]})
def toast(title="MCP", message=""):
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.Visible=$true;"
        "$n.ShowBalloonTip(5000,'%s','%s',[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Seconds 6;$n.Dispose()" % (str(title).replace("'", "''"), str(message).replace("'", "''"))
    )
    subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], creationflags=NO_WINDOW)
    return "toast shown: %s" % title


@srv.tool("system_info", "Basic host facts: OS, CPU, memory, disks, current user.",
          {"type": "object", "properties": {}, "required": []})
def system_info():
    return _ps(
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "'user=' + $env:USERNAME + '@' + $env:COMPUTERNAME;"
        "'os=' + $os.Caption + ' ' + $os.Version;"
        "'memory_mb=' + [int]($os.TotalVisibleMemorySize/1KB);"
        "'free_mb=' + [int]($os.FreePhysicalMemory/1KB);"
        "Get-CimInstance Win32_Processor | ForEach-Object { 'cpu=' + $_.Name };"
        "Get-PSDrive -PSProvider FileSystem | ForEach-Object { 'drive=' + $_.Name + ' free_gb=' + [int]($_.Free/1GB) + '/' + [int](($_.Used+$_.Free)/1GB) }"
    )


@srv.tool("read_image_base64", "Read a local image file as base64 (for downstream use).",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def read_image_base64(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    with open(p, "rb") as fh:
        data = fh.read()
    return "path=%s\nbytes=%d\nbase64_prefix=%s" % (p, len(data), base64.b64encode(data[:64]).decode("ascii"))


@srv.tool("lock_screen", "Lock the Windows session.",
          {"type": "object", "properties": {}, "required": []})
def lock_screen():
    subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"], creationflags=NO_WINDOW)
    return "workstation locked"


@srv.tool("power_action", "Power state control: cancel | poweroff | restart | logoff | hibernate (with a delay for the first three).",
          {"type": "object", "properties": {"action": {"type": "string", "default": "cancel"}, "delay_s": {"type": "integer", "default": 60}}, "required": []})
def power_action(action="cancel", delay_s=60):
    table = {"poweroff": "/s", "restart": "/r", "hibernate": "/h", "logoff": "/l"}
    key = str(action).strip().lower()
    if key in ("cancel", "abort"):
        return _ps("shutdown /a")
    if key not in table:
        return "unknown action %r; use cancel|poweroff|restart|logoff|hibernate" % action
    cmd = "shutdown %s /t %d" % (table[key], int(delay_s))
    return _ps(cmd) + "\n(scheduled: %s in %ss; call power_action cancel to abort)" % (key, delay_s)


@srv.tool("set_wallpaper", "Set the desktop wallpaper from an image file.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def set_wallpaper(path):
    import os as _os
    target = _os.path.abspath(_os.path.expanduser(str(path)))
    if not _os.path.exists(target):
        return "file not found: %s" % target
    script = (
        "Add-Type -Namespace W -Name U -MemberDefinition "
        "'[DllImport(\"user32.dll\",SetLastError=true)]public static extern bool SystemParametersInfo(int a,int b,string c,int d);';"
        "[W.U]::SystemParametersInfo(20,0,'%s',3)" % target.replace("'", "''")
    )
    return "wallpaper -> %s\n%s" % (target, _ps(script, 30))


@srv.tool("wifi_info", "Current Wi-Fi interface: SSID, signal, band, receive/transmit rate.",
          {"type": "object", "properties": {}, "required": []})
def wifi_info():
    return _ps("netsh wlan show interfaces")


@srv.tool("installed_apps", "List installed programs from the registry (optional name filter).",
          {"type": "object", "properties": {"filter": {"type": "string", "default": ""}, "limit": {"type": "integer", "default": 60}}, "required": []})
def installed_apps(filter="", limit=60):
    script = (
        "$k='HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*';"
        "Get-ItemProperty $k -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like '*%s*' } | "
        "Select-Object -ExpandProperty DisplayName -Unique | Sort-Object | Select-Object -First %d | Out-String -Width 200"
    ) % (filter, int(limit))
    return _ps(script, 60)


@srv.tool("empty_recycle_bin", "Empty the recycle bin (all drives).",
          {"type": "object", "properties": {}, "required": []})
def empty_recycle_bin():
    return _ps("Clear-RecycleBin -Force -ErrorAction SilentlyContinue; 'done'", 120)


@srv.tool("env_vars", "Show environment variables, optionally filtered by name substring.",
          {"type": "object", "properties": {"filter": {"type": "string", "default": ""}, "limit": {"type": "integer", "default": 60}}, "required": []})
def env_vars(filter="", limit=60):
    script = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
              "Get-ChildItem Env: | Where-Object { $_.Name -like '*%s*' } | "
              "Sort-Object Name | Select-Object -First %d | ForEach-Object { $_.Name + '=' + $_.Value } | Out-String -Width 240"
              ) % (filter, int(limit))
    return _ps(script, 30)


SAMPLES = {
    "now": {},
    "clipboard_get": {},
    "system_info": {},
    "env_vars": {"filter": "TEMP", "limit": 5},
    "installed_apps": {"filter": "python", "limit": 5},
}


def build():
    return srv


if __name__ == "__main__":
    srv.run()

