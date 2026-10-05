"""MCP server: run commands / PowerShell on the host, inspect and start processes."""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

MAX = int(os.environ.get("MCP_SHELL_MAX_CHARS") or 12000)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
WORKDIR = os.environ.get("MCP_SHELL_CWD") or os.path.expanduser("~")
# keep non-ASCII output intact instead of falling back to the OEM code page
PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
CMD_PREFIX = "chcp 65001>nul & "

srv = Server("shell")


def _decode(raw):
    """Windows tools mix UTF-8 and the OEM code page; try both."""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "cp936", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _run(argv, timeout_s=60, cwd=None, input_text=None):
    payload = input_text.encode("utf-8") if isinstance(input_text, str) else input_text
    try:
        p = subprocess.run(
            argv, capture_output=True, text=False,
            timeout=float(timeout_s), cwd=cwd or WORKDIR, input=payload, creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        return "[timeout after %ss]\n%s" % (timeout_s, _decode(exc.stdout)[:MAX])
    except FileNotFoundError as exc:
        return "command not found: %s" % exc
    out = _decode(p.stdout)
    err = _decode(p.stderr)
    if err.strip():
        out += "\n[stderr]\n" + err
    if len(out) > MAX:
        out = out[:MAX] + "\n...[truncated %d chars]" % (len(out) - MAX)
    return "exit=%s\n%s" % (p.returncode, out.strip())


@srv.tool("run_cmd", "Run a command through cmd.exe /c (Windows) or /bin/sh -c.",
          {"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}, "timeout_s": {"type": "integer", "default": 60}}, "required": ["command"]})
def run_cmd(command, cwd=None, timeout_s=60):
    argv = ["cmd.exe", "/c", CMD_PREFIX + command] if os.name == "nt" else ["/bin/sh", "-c", command]
    return _run(argv, timeout_s, cwd)


@srv.tool("run_ps", "Run a PowerShell script and return its output.",
          {"type": "object", "properties": {"script": {"type": "string"}, "cwd": {"type": "string"}, "timeout_s": {"type": "integer", "default": 60}}, "required": ["script"]})
def run_ps(script, cwd=None, timeout_s=60):
    exe = "powershell" if os.name == "nt" else "pwsh"
    return _run([exe, "-NoProfile", "-NonInteractive", "-Command", PS_PREFIX + script], timeout_s, cwd)


@srv.tool("start_process", "Launch a program or document without waiting for it.",
          {"type": "object", "properties": {"path": {"type": "string"}, "arguments": {"type": "string", "default": ""}, "cwd": {"type": "string"}}, "required": ["path"]})
def start_process(path, arguments="", cwd=None):
    args = [path] + ([arguments] if arguments else [])
    subprocess.Popen(args, cwd=cwd or WORKDIR, creationflags=NO_WINDOW, close_fds=True)
    return "started: %s %s" % (path, arguments)


@srv.tool("list_processes", "List running processes, optionally filtered by name substring.",
          {"type": "object", "properties": {"filter": {"type": "string", "default": ""}, "limit": {"type": "integer", "default": 40}}, "required": []})
def list_processes(filter="", limit=40):
    script = PS_PREFIX + (
        "Get-Process | Where-Object { $_.ProcessName -like '*%s*' } | "
        "Sort-Object -Property WS -Descending | Select-Object -First %d "
        "ProcessName, Id, @{n='MB';e={[int]($_.WS/1MB)}} | Format-Table -AutoSize | Out-String -Width 200"
    ) % (filter, int(limit))
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], 60)


@srv.tool("kill_process", "Terminate a process by id or exact name.",
          {"type": "object", "properties": {"target": {"type": "string", "description": "pid or process name"}}, "required": ["target"]})
def kill_process(target):
    t = str(target).strip()
    script = PS_PREFIX + ("Stop-Process -Id %s -Force" % t if t.isdigit() else "Stop-Process -Name '%s' -Force" % t)
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], 30)


@srv.tool("which", "Locate an executable on PATH.",
          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
def which(name):
    import shutil
    return shutil.which(name) or "not found: %s" % name


SAMPLES = {
    "which": {"name": "python"},
    "run_cmd": {"command": "echo aiyu-bridge-ok"},
    "run_ps": {"script": "'ps=' + $PSVersionTable.PSVersion.Major"},
    "list_processes": {"limit": 5},
}


def build():
    return srv


if __name__ == "__main__":
    srv.run()

