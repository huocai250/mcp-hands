"""MCP server: scheduled tasks and power management (schtasks / ScheduledTasks / powercfg).

Creation tools return the exact schtasks/PowerShell output so the OS verdict is
visible. task_delete and task_run act immediately and are destructive by nature
(there is no confirmation prompt). Commands that need an elevated shell report
the raw OS error text instead of pretending to succeed.
"""
import datetime
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
PS = "powershell" if os.name == "nt" else "pwsh"
# keep non-ASCII output intact instead of falling back to the OEM code page
PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
TIMEOUT_S = 60
CREATE_TIMEOUT_S = 120
MAX = int(os.environ.get("MCP_SCHED_MAX_CHARS") or 12000)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
# Standard Windows power scheme GUIDs behind the balanced|high|saver aliases.
PLANS = {
    "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "high": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
    "saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
}
# ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED, as plain decimal so
# PowerShell does not have to cast a negative int literal into a uint parameter.
AWAKE_ON = 2147483651
AWAKE_CLEAR = 2147483648
AWAKE_MARKER = os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"), "mcp_sched_awake.json")

# Reminders: a one-shot task whose action is a small PowerShell script that pops a
# Windows notification (and optionally speaks it). This is the only way the assistant
# can reach the user without being asked first.
REMIND_PREFIX = "mcp-hands-remind-"
REMIND_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.path.expanduser("~"),
                         "mcp-hands", "reminders")

srv = Server("sched")


def _tr_quote(command):
    """schtasks needs the whole /tr value quoted, with any inner quotes escaped."""
    if '"' in command:
        return '"%s"' % command.replace('"', '\\"')
    if " " in command:
        return '"%s"' % command
    return command


def _remind_at(time_hhmm, text, title, speak, name, human_when):
    label = re.sub(r"[^A-Za-z0-9_.\-]", "-", str(name).strip()) or ("auto-%s" % time_hhmm.replace(":", ""))
    task = REMIND_PREFIX + label
    os.makedirs(REMIND_DIR, exist_ok=True)
    script_path = os.path.join(REMIND_DIR, "%s.ps1" % label)
    safe_title = str(title or "mcp-hands").replace("'", "''")
    safe_text = str(text).replace("'", "''")
    lines = [
        "Add-Type -AssemblyName System.Windows.Forms",
        "$n = New-Object System.Windows.Forms.NotifyIcon",
        "$n.Icon = [System.Drawing.SystemIcons]::Information",
        "$n.Visible = $true",
        "$n.ShowBalloonTip(15000, '%s', '%s', [System.Windows.Forms.ToolTipIcon]::Info)" % (safe_title, safe_text),
        "Start-Sleep -Seconds 15",
        "$n.Dispose()",
    ]
    wants_speak = _flag(speak, False)
    if wants_speak:
        lines.append("$v = New-Object -ComObject SAPI.SpVoice")
        lines.append("$v.Speak('%s') | Out-Null" % safe_text)
    with open(script_path, "w", encoding="utf-8-sig") as fh:
        fh.write("\r\n".join(lines) + "\r\n")
    command = '%s -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "%s"' % (PS, script_path)
    argv = ["schtasks", "/create", "/tn", task, "/tr", _tr_quote(command), "/sc", "ONCE", "/st", time_hhmm, "/f"]
    out = _run(argv, CREATE_TIMEOUT_S)
    ok = "SUCCESS" in out.upper() or "成功" in out
    head = ("reminder set: %s | %s 会弹通知%s" % (task, human_when, "并念出来" if wants_speak else "")) if ok \
        else "reminder NOT created - schtasks refused:"
    return "%s\nscript: %s\ncmd: %s\n%s" % (head, script_path, " ".join(argv), out)


def _decode(raw):
    """Console tools mix UTF-8 and the OEM code page; try UTF-8 first."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "cp936", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _clip(text, head=True):
    if len(text) <= MAX:
        return text
    if head:
        return text[:MAX] + "\n...[truncated %d chars]" % (len(text) - MAX)
    return "...[truncated %d chars]\n" % (len(text) - MAX) + text[-MAX:]


def _run(argv, timeout_s=TIMEOUT_S, tail=False):
    """Run one console command; return 'exit=N' plus output, never raise."""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=float(timeout_s), creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        return "[timeout after %ss]\n%s" % (timeout_s, _decode(exc.stdout))
    except FileNotFoundError as exc:
        return "command not found: %s" % exc
    except Exception as exc:  # noqa: BLE001 - surface the reason as text
        return "%s: %s" % (type(exc).__name__, exc)
    out = _decode(p.stdout)
    err = _decode(p.stderr)
    if err.strip():
        out += "\n[stderr]\n" + err
    return "exit=%s\n%s" % (p.returncode, _clip(out.strip(), head=not tail))


def _ps(script, timeout_s=TIMEOUT_S):
    return _run([PS, "-NoProfile", "-NonInteractive", "-Command", PS_PREFIX + script], timeout_s)


def _quote(text):
    return str(text).replace("'", "''")


def _flag(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("", "0", "false", "no", "off", "disable", "disabled")


def _stamp():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _check_time(value):
    """Normalize 'H:MM' / 'HH:MM' (24h) to 'HH:MM'; return None when invalid."""
    m = TIME_RE.match(str(value or "").strip())
    if not m:
        return None
    return "%02d:%s" % (int(m.group(1)), m.group(2))


@srv.tool("task_list",
          "List scheduled tasks with name, state, next run time and last result. filter is a "
          "case-insensitive substring of the task name or its folder path.",
          {"type": "object", "properties": {"filter": {"type": "string", "default": ""},
                                            "limit": {"type": "integer", "default": 40}}, "required": []})
def task_list(filter="", limit=40):
    needle = _quote(filter)
    n = max(1, min(500, int(limit)))
    script = (
        "$f='%s';"
        "$t=Get-ScheduledTask -ErrorAction SilentlyContinue | "
        "Where-Object { $_.TaskName -like '*'+$f+'*' -or $_.TaskPath -like '*'+$f+'*' } | "
        "Select-Object -First %d;"
        "if (-not $t) { 'no scheduled tasks matched'; return };"
        "$t | ForEach-Object { $i=$_ | Get-ScheduledTaskInfo;"
        "'task ' + $_.TaskPath + $_.TaskName + ' | state=' + $_.State +"
        "' | next=' + $(if ($i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-dd HH:mm') } else { 'n/a' }) +"
        "' | last_result=' + $i.LastTaskResult +"
        "' | last_run=' + $(if ($i.LastRunTime) { $i.LastRunTime.ToString('yyyy-MM-dd HH:mm') } else { 'n/a' }) }"
    ) % (needle, n)
    out = _ps(script, 90)
    if "Get-ScheduledTask" in out and ("not recognized" in out or "CommandNotFound" in out):
        # Old or trimmed image without the ScheduledTasks module: fall back to schtasks.
        return "ScheduledTasks module unavailable, falling back to schtasks\n" + _run(
            ["schtasks", "/query", "/fo", "LIST"], 60)
    return out


@srv.tool("task_create_daily",
          "Create a recurring task. days is a comma list of MON..SUN: all seven days produce "
          "/sc DAILY, a subset produces /sc WEEKLY /d <days>. Returns the exact schtasks output.",
          {"type": "object", "properties": {"name": {"type": "string"}, "time": {"type": "string", "description": "HH:MM 24h"},
                                            "command": {"type": "string"},
                                            "days": {"type": "string", "default": "MON,TUE,WED,THU,FRI,SAT,SUN"}}, "required": ["name", "time", "command"]})
def task_create_daily(name, time, command, days="MON,TUE,WED,THU,FRI,SAT,SUN"):
    tm = _check_time(time)
    if tm is None:
        return "time must look like HH:MM in 24h form, got %r" % time
    chosen = []
    for chunk in str(days or "").replace(";", ",").split(","):
        day = chunk.strip().upper()[:3]
        if not day:
            continue
        if day not in WEEKDAYS:
            return "unknown weekday %r; use MON,TUE,WED,THU,FRI,SAT,SUN" % chunk.strip()
        if day not in chosen:
            chosen.append(day)
    argv = ["schtasks", "/create", "/tn", str(name), "/tr", str(command), "/f"]
    if not chosen or len(chosen) == 7:
        argv += ["/sc", "DAILY", "/st", tm]
    else:
        argv += ["/sc", "WEEKLY", "/d", ",".join(chosen), "/st", tm]
    return "cmd: %s\n%s" % (" ".join(argv), _run(argv, CREATE_TIMEOUT_S))


@srv.tool("task_create_once",
          "Create a one-shot task for today at HH:MM (schtasks /sc ONCE). Returns the exact schtasks output.",
          {"type": "object", "properties": {"name": {"type": "string"}, "time": {"type": "string", "description": "HH:MM 24h"},
                                            "command": {"type": "string"}}, "required": ["name", "time", "command"]})
def task_create_once(name, time, command):
    tm = _check_time(time)
    if tm is None:
        return "time must look like HH:MM in 24h form, got %r" % time
    argv = ["schtasks", "/create", "/tn", str(name), "/tr", str(command), "/sc", "ONCE", "/st", tm, "/f"]
    return "cmd: %s\n%s" % (" ".join(argv), _run(argv, CREATE_TIMEOUT_S))


@srv.tool("task_create_at_logon",
          "Create a task that runs every time the user logs on (schtasks /sc ONLOGON). "
          "Returns the exact schtasks output.",
          {"type": "object", "properties": {"name": {"type": "string"}, "command": {"type": "string"}},
           "required": ["name", "command"]})
def task_create_at_logon(name, command):
    argv = ["schtasks", "/create", "/tn", str(name), "/tr", str(command), "/sc", "ONLOGON", "/f"]
    return "cmd: %s\n%s" % (" ".join(argv), _run(argv, CREATE_TIMEOUT_S))


@srv.tool("remind_in", "Set a reminder that pops a Windows notification (and optionally speaks) after N minutes. "
                       "This is how the assistant can reach the user on its own - the phone app itself has no push channel.",
          {"type": "object", "properties": {"minutes": {"type": "number", "default": 10},
                                            "text": {"type": "string"},
                                            "title": {"type": "string", "default": "mcp-hands"},
                                            "speak": {"type": "boolean", "default": False},
                                            "name": {"type": "string", "default": ""}},
           "required": ["text"]})
def remind_in(minutes=10, text="", title="mcp-hands", speak=False, name=""):
    try:
        minutes = max(0.0, float(minutes))
    except (TypeError, ValueError):
        return "minutes must be a number, got %r" % minutes
    when = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
    return _remind_at(when.strftime("%H:%M"), text, title, speak, name, when.strftime("%Y-%m-%d %H:%M"))


@srv.tool("remind_at", "Set a reminder for a specific clock time today (HH:MM), popping a Windows notification.",
          {"type": "object", "properties": {"time": {"type": "string", "description": "HH:MM 24h"},
                                            "text": {"type": "string"},
                                            "title": {"type": "string", "default": "mcp-hands"},
                                            "speak": {"type": "boolean", "default": False},
                                            "name": {"type": "string", "default": ""}},
           "required": ["time", "text"]})
def remind_at(time, text="", title="mcp-hands", speak=False, name=""):
    tm = _check_time(time)
    if tm is None:
        return "time must look like HH:MM in 24h form, got %r" % time
    return _remind_at(tm, text, title, speak, name, tm)


def _field(block, *labels):
    """Read one field out of a schtasks /fo LIST block, English or localized label."""
    for line in block.splitlines():
        low = line.strip().lower()
        for label in labels:
            if low.startswith(label) and ":" in line:
                return line.split(":", 1)[1].strip()
    return "?"


@srv.tool("remind_list", "List the reminders this assistant has scheduled (tasks created by remind_in / remind_at).",
          {"type": "object", "properties": {}, "required": []})
def remind_list():
    out = _run(["schtasks", "/query", "/fo", "LIST"], 60)
    blocks = [b for b in re.split(r"\r?\n\r?\n", out) if REMIND_PREFIX in b]
    if not blocks:
        return "no reminders are scheduled (looked for task names starting with %s)" % REMIND_PREFIX
    lines = []
    for block in blocks:
        name = _field(block, "taskname", "任务名").lstrip("\\")
        lines.append("%s | next=%s | status=%s" % (name,
                                                   _field(block, "next run time", "下次运行时间"),
                                                   _field(block, "status", "状态")))
    return "reminders=%d\n%s" % (len(lines), "\n".join(lines))


@srv.tool("remind_cancel", "Cancel a reminder created by remind_in / remind_at (by name, or 'all').",
          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
def remind_cancel(name):
    wanted = str(name).strip()
    if wanted.lower() in ("all", "*"):
        listing = remind_list()
        names = [l.split(" |")[0].strip() for l in listing.splitlines()[1:]] if " | " in listing else []
        if not names:
            return listing
        out = []
        for item in names:
            out.append(_run(["schtasks", "/delete", "/tn", item, "/f"], CREATE_TIMEOUT_S))
        return "cancelled %d reminder(s)\n%s" % (len(names), "\n".join(out))
    if not wanted.startswith(REMIND_PREFIX):
        wanted = REMIND_PREFIX + wanted
    return "cmd: schtasks /delete /tn %s /f\n%s" % (wanted, _run(["schtasks", "/delete", "/tn", wanted, "/f"], CREATE_TIMEOUT_S))


@srv.tool("task_delete", "DESTRUCTIVE: delete a scheduled task immediately (schtasks /delete /f). No confirmation prompt.",
          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
def task_delete(name):
    argv = ["schtasks", "/delete", "/tn", str(name), "/f"]
    return "cmd: %s\n%s" % (" ".join(argv), _run(argv, CREATE_TIMEOUT_S))


@srv.tool("task_run", "DESTRUCTIVE: start a scheduled task right now, ignoring its schedule (schtasks /run). "
                      "No confirmation prompt.",
          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
def task_run(name):
    argv = ["schtasks", "/run", "/tn", str(name)]
    return "cmd: %s\n%s" % (" ".join(argv), _run(argv, CREATE_TIMEOUT_S))


@srv.tool("task_enable", "Enable or disable a scheduled task (schtasks /change /enable|/disable).",
          {"type": "object", "properties": {"name": {"type": "string"},
                                            "enable": {"type": "boolean", "default": True}}, "required": ["name"]})
def task_enable(name, enable=True):
    argv = ["schtasks", "/change", "/tn", str(name), "/enable" if _flag(enable, True) else "/disable"]
    return "cmd: %s\n%s" % (" ".join(argv), _run(argv, CREATE_TIMEOUT_S))


@srv.tool("task_details", "Full schtasks /query /v listing for one task: triggers, run-as user, action and state.",
          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
def task_details(name):
    argv = ["schtasks", "/query", "/tn", str(name), "/fo", "LIST", "/v"]
    return "cmd: %s\n%s" % (" ".join(argv), _run(argv, 60))


@srv.tool("power_plan_list", "Every power scheme known to powercfg, the active one, and the balanced/high/saver alias map.",
          {"type": "object", "properties": {}, "required": []})
def power_plan_list():
    out = _run(["powercfg", "/list"], 30)
    active = _run(["powercfg", "/getactivescheme"], 30)
    aliases = "\n".join("alias %s = %s" % (k, v) for k, v in sorted(PLANS.items()))
    return out + "\n-- active scheme --\n" + active + "\n-- aliases --\n" + aliases


@srv.tool("power_plan_set", "Switch the active power plan; plan is balanced|high|saver or a raw scheme GUID. "
                            "Needs an elevated shell on Windows.",
          {"type": "object", "properties": {"plan": {"type": "string", "default": "balanced"}}, "required": ["plan"]})
def power_plan_set(plan):
    key = str(plan or "").strip().lower()
    guid = PLANS.get(key, "")
    if not guid:
        if re.fullmatch(r"[0-9a-fA-F-]{36}", key):
            guid = key
        else:
            return "plan must be balanced|high|saver or a scheme GUID, got %r" % plan
    out = _run(["powercfg", "/setactive", guid], 60)
    return "cmd: powercfg /setactive %s\n%s\n-- active scheme now --\n%s" % (
        guid, out, _run(["powercfg", "/getactivescheme"], 30))


@srv.tool("keep_awake",
          "Ask Windows not to sleep or blank the display for the next N hours by holding "
          "SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED) in a hidden "
          "background PowerShell process. hours=0 (or less) releases every hold this server started. "
          "No elevated shell needed; the hold ends when the process is killed or the timer runs out.",
          {"type": "object", "properties": {"hours": {"type": "number", "default": 1}}, "required": []})
def keep_awake(hours=1):
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return "hours must be a number, got %r" % hours
    if h <= 0:
        return _release_awake()
    seconds = int(min(168.0, max(0.05, h)) * 3600)
    script = (
        "Add-Type -Namespace Aiyu -Name Awake -MemberDefinition "
        "'[DllImport(\"kernel32.dll\",SetLastError=true)]public static extern uint SetThreadExecutionState(uint f);';"
        "$prev=[Aiyu.Awake]::SetThreadExecutionState(%d);"
        "'previous_state=0x' + ('{0:X}' -f $prev);"
        "Start-Sleep -Seconds %d;"
        "[Aiyu.Awake]::SetThreadExecutionState(%d) | Out-Null;"
        "'released'" % (AWAKE_ON, seconds, AWAKE_CLEAR)
    )
    try:
        proc = subprocess.Popen(
            [PS, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", PS_PREFIX + script],
            creationflags=NO_WINDOW, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001
        return "could not start the keep-awake holder: %s: %s" % (type(exc).__name__, exc)
    holds = _load_holds()
    holds.append({"pid": proc.pid, "seconds": seconds, "started": _stamp()})
    _save_holds(holds)
    return ("keep-awake hold started: pid=%d seconds=%d (%.2f h) flags=0x80000003\n"
            "marker=%s holds=%d\nrelease with keep_awake hours=0" % (
                proc.pid, seconds, seconds / 3600.0, AWAKE_MARKER, len(holds)))


def _load_holds():
    try:
        with open(AWAKE_MARKER, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [h for h in data if isinstance(h, dict)]
    except Exception:
        return []


def _save_holds(holds):
    try:
        with open(AWAKE_MARKER, "w", encoding="utf-8") as fh:
            json.dump(holds, fh)
    except Exception:
        pass


def _release_awake():
    holds = _load_holds()
    if not holds:
        return "no keep-awake hold recorded (%s); nothing to release" % AWAKE_MARKER
    lines = []
    for h in holds:
        pid = int(h.get("pid") or 0)
        if pid <= 0:
            continue
        out = _run(["taskkill", "/pid", str(pid), "/f"], 30)
        lines.append("pid=%d -> %s" % (pid, out.replace("\n", " ").strip()))
    _save_holds([])
    if not lines:
        return "hold file %s had no usable pids; cleared" % AWAKE_MARKER
    return "released %d hold(s):\n%s" % (len(lines), "\n".join(lines))


@srv.tool("hibernate_enable", "Turn hibernation (and fast startup) support on or off: powercfg /hibernate on|off. "
                              "Needs an elevated shell.",
          {"type": "object", "properties": {"enable": {"type": "boolean", "default": True}}, "required": []})
def hibernate_enable(enable=True):
    flag = "on" if _flag(enable, True) else "off"
    return "cmd: powercfg /hibernate %s\n%s" % (flag, _run(["powercfg", "/hibernate", flag], 60))


def build():
    return srv


# Safe samples: read-only listings only (no creation, deletion or plan changes).
SAMPLES = {
    "task_list": {"filter": "", "limit": 5},
    "remind_list": {},
    "power_plan_list": {},
    # a stock Windows task, so the sample is portable; still optional because
    # a trimmed image may not ship the Defrag folder
    "task_details": {"name": "\\Microsoft\\Windows\\Defrag\\ScheduledDefrag"},
}
SAMPLES_OPTIONAL = {"task_details"}


if __name__ == "__main__":
    srv.run()
