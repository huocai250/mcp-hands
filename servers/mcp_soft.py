"""MCP server: software inventory and installation (winget / registry uninstall keys).

winget runs with --disable-interactivity so it never blocks on a prompt, and
install/uninstall get a 600s budget and return the tail of the output (the last
lines carry the result). When winget is absent or fails, installed-package
listing falls back to the registry uninstall keys.
"""
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
PS = "powershell" if os.name == "nt" else "pwsh"
PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
TIMEOUT_S = 120
INSTALL_TIMEOUT_S = 600
MAX = int(os.environ.get("MCP_SOFT_MAX_CHARS") or 12000)
UNINSTALL_KEYS = (
    "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*",
    "HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*",
    "HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*",
)

srv = Server("soft")


def _decode(raw):
    """winget and other console tools mix UTF-8 and the OEM code page; try UTF-8 first."""
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


def _winget_exe():
    found = shutil.which("winget")
    if found:
        return found
    for cand in (
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps", "winget.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "WindowsApps", "winget.exe"),
        os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "winget.exe"),
    ):
        if cand and os.path.isfile(cand):
            return cand
    return None


def _winget(args, timeout_s=TIMEOUT_S, tail=False):
    exe = _winget_exe()
    if not exe:
        return None, "winget not found (PATH, %LOCALAPPDATA%\\Microsoft\\WindowsApps)"
    return exe, _run([exe] + args, timeout_s, tail=tail)


def _first_line(text):
    return (text or "").splitlines()[0] if text else ""


def _registry_installed(limit):
    script = (
        "$k=@(%s);"
        "Get-ItemProperty $k -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName } | "
        "Sort-Object DisplayName | Select-Object -First %d | ForEach-Object { "
        "'pkg ' + $_.DisplayName + ' | version=' + $_.DisplayVersion + ' | publisher=' + $_.Publisher }"
        % (",".join("'%s'" % k for k in UNINSTALL_KEYS), int(limit))
    )
    return _ps(script, 120)


@srv.tool("winget_available", "Is winget installed, where does it live and which version is it.",
          {"type": "object", "properties": {}, "required": []})
def winget_available():
    exe = _winget_exe()
    if not exe:
        return ("winget: not present (searched PATH and %LOCALAPPDATA%\\Microsoft\\WindowsApps)\n"
                "winget_installed falls back to the registry uninstall keys")
    out = _run([exe, "--version"], 60)
    return "winget: %s\n%s" % (exe, out)


@srv.tool("winget_search", "Search the configured winget sources and return the raw table (limit caps the rows).",
          {"type": "object", "properties": {"query": {"type": "string"},
                                            "limit": {"type": "integer", "default": 15}}, "required": ["query"]})
def winget_search(query, limit=15):
    q = str(query or "").strip()
    if not q:
        return "query must not be empty"
    n = max(1, min(200, int(limit)))
    exe, out = _winget(["search", "--query", q, "--count", str(n),
                        "--accept-source-agreements", "--disable-interactivity"], 180)
    if exe is None:
        return out
    return "cmd: %s search --query %s --count %d\n%s" % (exe, q, n, out)


@srv.tool("winget_install",
          "Install a package by exact winget id. Long running: 600s budget, the tail of the output is returned. "
          "silent=True adds --silent (no installer UI, defaults are used).",
          {"type": "object", "properties": {"package_id": {"type": "string"},
                                            "silent": {"type": "boolean", "default": True}}, "required": ["package_id"]})
def winget_install(package_id, silent=True):
    pid = str(package_id or "").strip()
    if not pid:
        return "package_id must not be empty"
    args = ["install", "--id", pid, "--exact", "--accept-package-agreements",
            "--accept-source-agreements", "--disable-interactivity"]
    if _flag(silent, True):
        args.append("--silent")
    exe, out = _winget(args, INSTALL_TIMEOUT_S, tail=True)
    if exe is None:
        return out
    return "cmd: %s %s\n%s" % (exe, " ".join(args), out)


@srv.tool("winget_uninstall", "Uninstall a package by exact winget id (600s budget, tail of the output).",
          {"type": "object", "properties": {"package_id": {"type": "string"}}, "required": ["package_id"]})
def winget_uninstall(package_id):
    pid = str(package_id or "").strip()
    if not pid:
        return "package_id must not be empty"
    args = ["uninstall", "--id", pid, "--exact", "--disable-interactivity"]
    exe, out = _winget(args, INSTALL_TIMEOUT_S, tail=True)
    if exe is None:
        return out
    return "cmd: %s %s\n%s" % (exe, " ".join(args), out)


@srv.tool("winget_upgrade_list", "Packages with an available upgrade, straight from winget upgrade.",
          {"type": "object", "properties": {}, "required": []})
def winget_upgrade_list():
    exe, out = _winget(["upgrade", "--accept-source-agreements", "--disable-interactivity"], 300)
    if exe is None:
        return out
    return "cmd: %s upgrade --accept-source-agreements\n%s" % (exe, out)


@srv.tool("winget_installed",
          "Installed packages: winget list when winget works, otherwise the registry uninstall keys "
          "(DisplayName/DisplayVersion/Publisher). limit caps the rows.",
          {"type": "object", "properties": {"limit": {"type": "integer", "default": 100}}, "required": []})
def winget_installed(limit=100):
    n = max(1, min(1000, int(limit)))
    exe, out = _winget(["list", "--accept-source-agreements", "--disable-interactivity"], 300)
    if exe is not None and _first_line(out).startswith("exit=0"):
        lines = out.splitlines()[1:]
        body = lines[:n]
        note = "" if len(lines) <= n else "\n...[%d more rows]" % (len(lines) - n)
        return "source=winget list (showing %d rows)\n%s%s" % (len(body), "\n".join(body), note)
    reason = out if exe is None else "winget list failed: %s" % _first_line(out)
    return "source=registry uninstall keys (%s)\n%s" % (reason, _registry_installed(n))


@srv.tool("store_app_install",
          "Open a Microsoft Store page in the Store app (Start-Process ms-windows-store:). Accepts a store "
          "product id, an apps.microsoft.com / microsoft.com/store URL, or an ms-windows-store: URI.",
          {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})
def store_app_install(url):
    target = str(url or "").strip()
    if not target:
        return "url must not be empty"
    low = target.lower()
    if low.startswith("ms-windows-store:"):
        uri = target
    elif re.fullmatch(r"[A-Za-z0-9]{12}", target):
        uri = "ms-windows-store://pdp/?productid=" + target.upper()
    elif "apps.microsoft.com" in low or "microsoft.com/store" in low or "microsoft.com/p/" in low:
        m = re.search(r"[A-Za-z0-9]{12}", target)
        uri = "ms-windows-store://pdp/?productid=" + m.group(0).upper() if m else target
    else:
        uri = target
    out = _ps("Start-Process '%s'; 'launched'" % _quote(uri), 60)
    return "cmd: Start-Process '%s'\n%s" % (uri, out)


@srv.tool("default_apps_list",
          "Default programs per file extension (HKCU FileExts UserChoice ProgIds) plus the http/https URL handlers.",
          {"type": "object", "properties": {}, "required": []})
def default_apps_list():
    script = (
        "$k='HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\FileExts';"
        "$c=0;"
        "Get-ChildItem $k -ErrorAction SilentlyContinue | Sort-Object PSChildName | ForEach-Object {"
        "$prog=(Get-ItemProperty ($_.PSPath + '\\UserChoice') -ErrorAction SilentlyContinue).ProgId;"
        "if ($prog) { $script:c++; if ($script:c -le 60) { 'ext ' + $_.PSChildName + ' -> ' + $prog } } };"
        "'extensions mapped=' + $c;"
        "foreach ($p in @('http','https')) {"
        "$u=(Get-ItemProperty ('HKCU:\\Software\\Microsoft\\Windows\\Shell\\Associations\\UrlAssociations\\' + "
        "$p + '\\UserChoice') -ErrorAction SilentlyContinue).ProgId;"
        "if ($u) { 'url ' + $p + ' -> ' + $u } else { 'url ' + $p + ' -> (none)' } }"
    )
    return _ps(script, 120)


def build():
    return srv


# Safe samples: inventory reads only (nothing is installed, removed or launched).
SAMPLES = {
    "winget_available": {},
    "winget_installed": {"limit": 5},
    "default_apps_list": {},
    "winget_search": {"query": "git", "limit": 5},
    "winget_upgrade_list": {},
}
# need the winget source (network) to answer
SAMPLES_OPTIONAL = {"winget_search", "winget_upgrade_list"}


if __name__ == "__main__":
    srv.run()
