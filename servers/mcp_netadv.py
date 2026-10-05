"""MCP server: network advanced settings -- Wi-Fi profiles, hosts file, portproxy, firewall.

netsh and PowerShell are invoked directly (argv form, no shell), so profile and
rule names with spaces survive intact. Commands that need an elevated shell
(portproxy add/delete, firewall add/delete, dns cache on some builds) return the
raw OS error text instead of pretending to succeed.
"""
import datetime
import ipaddress
import os
import re
import shutil
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
PS = "powershell" if os.name == "nt" else "pwsh"
PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
TIMEOUT_S = 60
MAX = int(os.environ.get("MCP_NETADV_MAX_CHARS") or 12000)
HOSTS = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "drivers", "etc", "hosts")
HOSTS_FALLBACK_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                                  "mcp_netadv", "hosts")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")
# "label : value" on one line only: [ \t] (not \s) so the pattern cannot walk
# across the blank lines that separate the netsh sections.
PROFILE_RE = re.compile(r"^[ \t]*[^:\r\n]{3,}:[ \t]*(\S.*)$", re.M)

srv = Server("netadv")


def _decode(raw):
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


def _run(argv, timeout_s=TIMEOUT_S):
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
    return "exit=%s\n%s" % (p.returncode, _clip(out.strip()))


def _ps(script, timeout_s=TIMEOUT_S):
    return _run([PS, "-NoProfile", "-NonInteractive", "-Command", PS_PREFIX + script], timeout_s)


def _quote(text):
    return str(text).replace("'", "''")


def _wifi_note(text):
    """Flag an obvious 'no adapter' answer.

    netsh localizes its resource strings (and over a pipe it does not follow the
    UTF-8 console setting), so the check looks at the first few lines only and
    accepts both the English and the Chinese wording. The raw netsh text is
    always returned alongside this note.
    """
    head = "\n".join([ln for ln in (text or "").splitlines() if ln.strip()][:3]).lower()
    # A Chinese-locale netsh answers in Chinese; the escapes keep this file ASCII.
    markers = ("not present", "no wireless interface", "not supported on this system",
               "does not exist",
               "\u4e0d\u5b58\u5728",        # does not exist
               "\u6ca1\u6709\u65e0\u7ebf",   # no wireless
               "\u4e0d\u53ef\u7528",         # not available
               "\u672a\u8fd0\u884c")         # not running
    for marker in markers:
        if marker in head:
            return "no wireless adapter reported by netsh wlan\n"
    return ""


def _read_hosts():
    if not os.path.isfile(HOSTS):
        return None, "hosts file not found: %s" % HOSTS
    with open(HOSTS, "rb") as fh:
        return _decode(fh.read()), ""


def _write_hosts(text):
    with open(HOSTS, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _backup_hosts():
    """Copy hosts to hosts.bak.<stamp>; prefer the etc folder, fall back to LOCALAPPDATA.

    Two edits inside the same second must not overwrite each other, so an
    existing name gets a numeric suffix.
    """
    if not os.path.isfile(HOSTS):
        return "", "hosts file not found: %s" % HOSTS
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    error = ""
    for folder in (os.path.dirname(HOSTS), HOSTS_FALLBACK_DIR):
        dest = os.path.join(folder, "hosts.bak.%s" % stamp)
        suffix = 1
        while os.path.exists(dest):
            suffix += 1
            dest = os.path.join(folder, "hosts.bak.%s_%d" % (stamp, suffix))
        try:
            os.makedirs(folder, exist_ok=True)
            # copy (not copy2): the backup keeps its own creation time, so
            # "newest backup" really means the most recently written one
            shutil.copy(HOSTS, dest)
            return dest, ""
        except Exception as exc:  # noqa: BLE001 - try the next location
            error = "%s: %s" % (type(exc).__name__, exc)
    return "", "could not write a backup (%s)" % error


def _newest_backup():
    best = ("", 0.0)
    for folder in (os.path.dirname(HOSTS), HOSTS_FALLBACK_DIR):
        if not os.path.isdir(folder):
            continue
        for entry in os.listdir(folder):
            if entry.startswith("hosts.bak") and entry != "hosts":
                full = os.path.join(folder, entry)
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    continue
                if mtime > best[1]:
                    best = (full, mtime)
    return best[0]


def _valid_ports(*values):
    out = []
    for value in values:
        try:
            port = int(value)
        except (TypeError, ValueError):
            return None
        if not 0 < port < 65536:
            return None
        out.append(port)
    return out


@srv.tool("wifi_profiles", "Saved WLAN profiles (netsh wlan show profiles) with a parsed profile count.",
          {"type": "object", "properties": {}, "required": []})
def wifi_profiles():
    out = _run(["netsh", "wlan", "show", "profiles"], 40)
    names = [m.group(1).strip() for m in PROFILE_RE.finditer(out)]
    return "%scmd: netsh wlan show profiles\nparsed_profiles=%d\n%s" % (_wifi_note(out), len(names), out)


@srv.tool("wifi_profile_details",
          "Full netsh wlan show profile listing for one profile, including the stored key when the shell is elevated.",
          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
def wifi_profile_details(name):
    profile = str(name or "").strip()
    if not profile:
        return "name must not be empty"
    out = _run(["netsh", "wlan", "show", "profile", "name=" + profile, "key=clear"], 40)
    return "%scmd: netsh wlan show profile name=%s key=clear\n%s" % (_wifi_note(out), profile, out)


@srv.tool("wifi_export_profile",
          "Export one profile to <out_dir> as a plain-text (unencrypted) WLAN XML file.",
          {"type": "object", "properties": {"name": {"type": "string"}, "out_dir": {"type": "string"}},
           "required": ["name", "out_dir"]})
def wifi_export_profile(name, out_dir):
    profile = str(name or "").strip()
    if not profile:
        return "name must not be empty"
    folder = os.path.abspath(os.path.expanduser(str(out_dir)))
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return "cannot use %s: %s: %s" % (folder, type(exc).__name__, exc)
    out = _run(["netsh", "wlan", "export", "profile", "name=" + profile, "folder=" + folder], 40)
    files = []
    try:
        files = [os.path.join(folder, f) for f in sorted(os.listdir(folder)) if f.lower().startswith("wi-fi-") or f.lower().endswith(".xml")]
    except OSError:
        pass
    listing = "\n".join("xml %s (%d bytes)" % (f, os.path.getsize(f)) for f in files if os.path.isfile(f))
    return "out_dir=%s\ncmd: netsh wlan export profile name=%s folder=%s\n%s\n%s" % (
        folder, profile, folder, out, listing or "no xml file created")


@srv.tool("wifi_connect",
          "Connect to a saved profile (netsh wlan connect). The radio stays on; the current connection may drop.",
          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
def wifi_connect(name):
    profile = str(name or "").strip()
    if not profile:
        return "name must not be empty"
    out = _run(["netsh", "wlan", "connect", "name=" + profile], 40)
    return "%scmd: netsh wlan connect name=%s\n%s" % (_wifi_note(out), profile, out)


@srv.tool("wifi_status", "Wireless interfaces: state, SSID, band, channel, rates and signal (netsh wlan show interfaces).",
          {"type": "object", "properties": {}, "required": []})
def wifi_status():
    out = _run(["netsh", "wlan", "show", "interfaces"], 40)
    return "%scmd: netsh wlan show interfaces\n%s" % (_wifi_note(out), out)


@srv.tool("hosts_read", "Contents of the Windows hosts file, with its exact path, line count and active entry count.",
          {"type": "object", "properties": {}, "required": []})
def hosts_read():
    text, err = _read_hosts()
    if err:
        return err
    active = 0
    for line in text.splitlines():
        body = line.split("#", 1)[0].strip()
        if body:
            active += 1
    return "path=%s\nlines=%d active_entries=%d\n-- content --\n%s" % (
        HOSTS, len(text.splitlines()), active, _clip(text.strip()))


@srv.tool("hosts_add",
          "Append 'ip hostname...' to the hosts file (needs an elevated shell). A hosts.bak.<stamp> copy of the "
          "current file is written first; hostnames is comma or space separated.",
          {"type": "object", "properties": {"ip": {"type": "string"}, "hostnames": {"type": "string"}},
           "required": ["ip", "hostnames"]})
def hosts_add(ip, hostnames):
    address = str(ip or "").strip()
    try:
        ipaddress.ip_address(address)
    except ValueError:
        return "ip must be a valid IPv4/IPv6 literal, got %r" % ip
    names = [n for n in re.split(r"[,\s]+", str(hostnames or "")) if n]
    if not names:
        return "hostnames must not be empty"
    for n in names:
        if not HOSTNAME_RE.match(n):
            return "invalid hostname %r" % n
    text, err = _read_hosts()
    if err:
        return err
    backup, backup_err = _backup_hosts()
    if backup_err:
        return "path=%s\naborted before writing: %s" % (HOSTS, backup_err)
    line = address + " " + " ".join(names)
    body = text if (not text or text.endswith("\n")) else text + "\n"
    try:
        _write_hosts(body + line + "\n")
    except Exception as exc:  # noqa: BLE001
        return "path=%s\nbackup=%s\nwrite failed: %s: %s" % (HOSTS, backup, type(exc).__name__, exc)
    return "path=%s\nbackup=%s\nappended: %s" % (HOSTS, backup, line)


@srv.tool("hosts_remove",
          "Delete every hosts line that maps a hostname (needs an elevated shell). A hosts.bak.<stamp> copy is "
          "written first; comments and other hosts are preserved. Reports how many lines went away.",
          {"type": "object", "properties": {"hostname": {"type": "string"}}, "required": ["hostname"]})
def hosts_remove(hostname):
    target = str(hostname or "").strip()
    if not target:
        return "hostname must not be empty"
    text, err = _read_hosts()
    if err:
        return err
    keep, removed = [], []
    for line in text.splitlines():
        body = line.split("#", 1)[0].strip()
        tokens = body.split()
        if body and len(tokens) > 1 and any(t.lower() == target.lower() for t in tokens[1:]):
            removed.append(line)
            continue
        keep.append(line)
    if not removed:
        return "path=%s\nno line maps %s; hosts file unchanged (no backup written)" % (HOSTS, target)
    backup, backup_err = _backup_hosts()
    if backup_err:
        return "path=%s\naborted before writing: %s" % (HOSTS, backup_err)
    try:
        _write_hosts("\n".join(keep) + "\n")
    except Exception as exc:  # noqa: BLE001
        return "path=%s\nbackup=%s\nwrite failed: %s: %s" % (HOSTS, backup, type(exc).__name__, exc)
    return "path=%s\nbackup=%s\nremoved %d line(s):\n%s" % (HOSTS, backup, len(removed), "\n".join(removed))


@srv.tool("hosts_reset",
          "Restore the hosts file from the newest hosts.bak copy (the etc folder or %LOCALAPPDATA%\\mcp_netadv\\hosts). "
          "The current file is backed up before it is replaced, so the restore itself can be undone.",
          {"type": "object", "properties": {}, "required": []})
def hosts_reset():
    source = _newest_backup()
    if not source:
        return "no hosts.bak copy found in %s or %s; nothing to restore" % (os.path.dirname(HOSTS), HOSTS_FALLBACK_DIR)
    if not os.path.isfile(HOSTS):
        return "hosts file missing: %s" % HOSTS
    rescue, rescue_err = _backup_hosts()
    if rescue_err:
        return "aborted before restoring: %s" % rescue_err
    try:
        shutil.copyfile(source, HOSTS)
    except Exception as exc:  # noqa: BLE001
        return "path=%s\nrestore from %s failed: %s: %s\ncurrent copy kept at %s" % (
            HOSTS, source, type(exc).__name__, exc, rescue)
    return "path=%s\nrestored from=%s\nprevious version kept at=%s" % (HOSTS, source, rescue)


@srv.tool("dns_flush", "Flush the DNS resolver cache (ipconfig /flushdns).",
          {"type": "object", "properties": {}, "required": []})
def dns_flush():
    return "cmd: ipconfig /flushdns\n%s" % _run(["ipconfig", "/flushdns"], 60)


@srv.tool("portproxy_list", "netsh interface portproxy rules (all families), plus the command that produced them.",
          {"type": "object", "properties": {}, "required": []})
def portproxy_list():
    out = _run(["netsh", "interface", "portproxy", "show", "all"], 40)
    body = out.split("\n", 1)[1].strip() if "\n" in out else ""
    return "cmd: netsh interface portproxy show all\n%s\n%s" % (
        out, "no portproxy rules configured" if not body else "")


@srv.tool("portproxy_add",
          "Add a netsh portproxy v4tov4/v6tov6 rule (listen -> connect). Needs an elevated shell. "
          "Hostnames are resolved to an address because netsh only accepts literals.",
          {"type": "object", "properties": {"listen_port": {"type": "integer"},
                                            "connect_host": {"type": "string"},
                                            "connect_port": {"type": "integer"},
                                            "listen_address": {"type": "string", "default": "0.0.0.0"}},
           "required": ["listen_port", "connect_host", "connect_port"]})
def portproxy_add(listen_port, connect_host, connect_port, listen_address="0.0.0.0"):
    ports = _valid_ports(listen_port, connect_port)
    if ports is None:
        return "listen_port and connect_port must both be 1-65535"
    listen_port, connect_port = ports
    target = str(connect_host or "").strip()
    if not target:
        return "connect_host must not be empty"
    try:
        ipaddress.ip_address(target)
        resolved = target
    except ValueError:
        try:
            resolved = socket.gethostbyname(target)
        except Exception as exc:  # noqa: BLE001
            return "cannot resolve %s: %s: %s" % (target, type(exc).__name__, exc)
    listen = str(listen_address or "0.0.0.0").strip() or "0.0.0.0"
    try:
        family = ipaddress.ip_address(listen).version
    except ValueError:
        return "listen_address must be an IP literal, got %r" % listen_address
    kind = "v6tov6" if family == 6 else "v4tov4"
    argv = ["netsh", "interface", "portproxy", "add", kind,
            "listenaddress=" + listen, "listenport=%d" % listen_port,
            "connectaddress=" + resolved, "connectport=%d" % connect_port]
    note = "" if resolved == target else " (resolved %s -> %s)" % (target, resolved)
    return "cmd: %s%s\n%s" % (" ".join(argv), note, _run(argv, 60))


@srv.tool("portproxy_remove", "Delete the v4tov4 portproxy rule for a listen port (listen address 0.0.0.0). "
                              "Needs an elevated shell.",
          {"type": "object", "properties": {"listen_port": {"type": "integer"}}, "required": ["listen_port"]})
def portproxy_remove(listen_port):
    ports = _valid_ports(listen_port)
    if ports is None:
        return "listen_port must be 1-65535"
    argv = ["netsh", "interface", "portproxy", "delete", "v4tov4", "listenport=%d" % ports[0]]
    return "cmd: %s\n%s" % (" ".join(argv), _run(argv, 60))


@srv.tool("firewall_list_rule",
          "Firewall rules by display name (substring match); an empty name lists the first 30 rules. "
          "Shows direction, action, enabled state, profiles, protocol and ports. Read-only.",
          {"type": "object", "properties": {"display_name": {"type": "string", "default": ""}}, "required": []})
def firewall_list_rule(display_name=""):
    needle = _quote(display_name)
    script = (
        "$n='%s';"
        "if ($n) { $r=Get-NetFirewallRule -DisplayName ('*'+$n+'*') -ErrorAction SilentlyContinue }"
        "else { $r=Get-NetFirewallRule -ErrorAction SilentlyContinue | Select-Object -First 30 };"
        "if (-not $r) { 'no firewall rules matched'; return };"
        "$c=0; $r | ForEach-Object {"
        "$pf=$_ | Get-NetFirewallPortFilter;"
        "$c++; 'rule ' + $_.DisplayName + ' | dir=' + $_.Direction + ' | action=' + $_.Action +"
        "' | enabled=' + $_.Enabled + ' | profile=' + $_.Profile + ' | proto=' + $pf.Protocol +"
        "' | ports=' + ((@($pf.LocalPort) -join ',') + ' -> ' + (@($pf.RemotePort) -join ',')) };"
        "'rules_shown=' + $c"
    ) % needle
    return _ps(script, 90)


@srv.tool("firewall_add_rule",
          "Create an inbound/outbound allow rule (netsh advfirewall firewall add rule). Needs an elevated shell. "
          "port accepts one port, a comma list or 'any'; protocol is TCP|UDP|any.",
          {"type": "object", "properties": {"display_name": {"type": "string"}, "port": {"type": "string"},
                                            "protocol": {"type": "string", "default": "TCP"},
                                            "direction": {"type": "string", "default": "inbound"}}, "required": ["display_name", "port"]})
def firewall_add_rule(display_name, port, protocol="TCP", direction="inbound"):
    rule = str(display_name or "").strip()
    if not rule:
        return "display_name must not be empty"
    spec = str(port or "").strip()
    if not re.fullmatch(r"(?i)any|[0-9]+(\s*,\s*[0-9]+)*", spec):
        return "port must be a number, a comma list or 'any', got %r" % port
    proto = str(protocol or "TCP").strip().upper()
    if proto not in ("TCP", "UDP", "ANY"):
        return "protocol must be TCP, UDP or any, got %r" % protocol
    side = str(direction or "inbound").strip().lower()
    if side not in ("inbound", "outbound", "in", "out"):
        return "direction must be inbound or outbound, got %r" % direction
    argv = ["netsh", "advfirewall", "firewall", "add", "rule", "name=" + rule,
            "dir=" + ("in" if side.startswith("in") else "out"), "action=allow",
            "protocol=" + proto, "localport=" + spec.replace(" ", "")]
    return "cmd: %s\n%s" % (" ".join(argv), _run(argv, 60))


@srv.tool("firewall_remove_rule",
          "Delete firewall rules by exact display name (netsh advfirewall firewall delete rule). "
          "DESTRUCTIVE and needs an elevated shell: every rule with that name goes away. No confirmation prompt.",
          {"type": "object", "properties": {"display_name": {"type": "string"}}, "required": ["display_name"]})
def firewall_remove_rule(display_name):
    rule = str(display_name or "").strip()
    if not rule:
        return "display_name must not be empty"
    argv = ["netsh", "advfirewall", "firewall", "delete", "rule", "name=" + rule]
    return "cmd: %s\n%s" % (" ".join(argv), _run(argv, 60))


def build():
    return srv


# Safe samples: reads only (no hosts edits, no portproxy/firewall changes, no wifi switch).
SAMPLES = {
    "hosts_read": {},
    "wifi_profiles": {},
    "wifi_status": {},
    "portproxy_list": {},
    "firewall_list_rule": {"display_name": ""},
}
# need a wireless adapter (absent on wired-only machines)
SAMPLES_OPTIONAL = {"wifi_profiles", "wifi_status"}


if __name__ == "__main__":
    srv.run()
