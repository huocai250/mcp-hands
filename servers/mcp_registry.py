"""MCP server: Windows registry access through the PowerShell provider + reg.exe.

Every tool echoes the exact path it touched, in both PowerShell form (HKLM:\\...)
and native reg.exe form (HKLM\\...). HKLM\\SAM and HKLM\\SECURITY are off limits
by policy. reg save needs the backup privilege, so reg_backup_key reports the raw
OS error text when the shell is not elevated.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
PS = "powershell" if os.name == "nt" else "pwsh"
PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
TIMEOUT_S = 60
MAX = int(os.environ.get("MCP_REGISTRY_MAX_CHARS") or 12000)
LONG_ROOTS = {
    "HKEY_LOCAL_MACHINE": "HKLM",
    "HKEY_CURRENT_USER": "HKCU",
    "HKEY_CLASSES_ROOT": "HKCR",
    "HKEY_USERS": "HKU",
    "HKEY_CURRENT_CONFIG": "HKCC",
}
SHORT_ROOTS = ("HKLM", "HKCU", "HKCR", "HKU", "HKCC")
BLOCKED = ("HKLM\\SAM", "HKLM\\SECURITY")
TYPES = {
    "string": "String",
    "dword": "DWord",
    "qword": "QWord",
    "expandstring": "ExpandString",
    "multistring": "MultiString",
    "binary": "Binary",
}

srv = Server("registry")


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


def _paths(path):
    """Return (powershell_path, native_path) or None when the root is unknown."""
    raw = str(path or "").strip().strip('"').replace("/", "\\")
    if not raw:
        return None
    head, sep, rest = raw.partition(":")
    if sep:
        root, tail = head.strip().upper(), rest.strip("\\")
    else:
        head, sep, rest = raw.partition("\\")
        root, tail = head.strip().upper(), rest.strip("\\")
    short = LONG_ROOTS.get(root, root)
    if short not in SHORT_ROOTS:
        return None
    ps_path = "%s:\\%s" % (short, tail) if tail else "%s:\\" % short
    native = "%s\\%s" % (short, tail) if tail else short
    return ps_path, native


def _blocked(native):
    up = native.upper().rstrip("\\")
    for bad in BLOCKED:
        if up == bad or up.startswith(bad + "\\"):
            return bad
    return ""


def _guard(path):
    """Resolve a path, honouring the SAM/SECURITY policy. Returns (info, error)."""
    resolved = _paths(path)
    if resolved is None:
        return None, "path must start with HKLM|HKCU|HKCR|HKU|HKCC (or HKEY_*), got %r" % path
    ps_path, native = resolved
    bad = _blocked(native)
    if bad:
        return None, "policy: %s is never touched (path=%s)" % (bad, native)
    return (ps_path, native), None


def _head(ps_path, native):
    return "path=%s\nnative=%s" % (ps_path, native)


def _flag(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("", "0", "false", "no", "off", "disable", "disabled")


def _value_expr(value, ptype):
    """Build the PowerShell literal for New-ItemProperty; None when the input is bad."""
    if ptype == "Binary":
        cleaned = re.sub(r"0x|[,\s\-]", "", str(value), flags=re.I)
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2})*", cleaned):
            return None
        items = ", ".join("0x" + cleaned[i:i + 2] for i in range(0, len(cleaned), 2))
        return "[byte[]]@(%s)" % items
    if ptype == "MultiString":
        parts = [ln for ln in re.split(r"\r?\n", str(value))]
        return "[string[]]@(%s)" % ", ".join("'%s'" % _quote(p) for p in parts)
    if ptype in ("DWord", "QWord"):
        try:
            number = int(str(value).strip(), 0)
        except ValueError:
            return None
        if number < 0 or (ptype == "DWord" and number > 0xFFFFFFFF) or (ptype == "QWord" and number > 0xFFFFFFFFFFFFFFFF):
            return None
        return str(number)
    return "'%s'" % _quote(value)


def _ps_read_line(ps_path, native, name):
    script = (
        "$p='%s'; $n='%s';"
        "if (-not (Test-Path -LiteralPath $p)) { 'ERROR: key not found'; exit 1 };"
        "$k=Get-Item -LiteralPath $p -ErrorAction Stop;"
        "if ($n) {"
        "try { $kind=$k.GetValueKind($n).ToString() } catch { $kind='(missing)' };"
        "$d=$k.GetValue($n,$null,'DoNotExpandEnvironmentNames');"
        "if ($null -eq $d) { $d='(missing)' } elseif ($d -is [byte[]]) { $d=($d | ForEach-Object { $_.ToString('x2') }) -join '' };"
        "'value ' + $n + ' | type=' + $kind + ' | data=' + (@($d) -join ',') }"
        "else {"
        "$c=0; foreach ($vn in $k.Property) {"
        "$disp=if ($vn -eq '') { '(default)' } else { $vn };"
        "try { $kind=$k.GetValueKind($vn).ToString() } catch { $kind='(unknown)' };"
        "$d=$k.GetValue($vn,$null,'DoNotExpandEnvironmentNames');"
        "if ($null -eq $d) { $d='(null)' } elseif ($d -is [byte[]]) { $d=($d | ForEach-Object { $_.ToString('x2') }) -join '' };"
        "$c++; 'value ' + $disp + ' | type=' + $kind + ' | data=' + (@($d) -join ',') };"
        "'values=' + $c }"
    ) % (_quote(ps_path), _quote(name))
    return script


@srv.tool("reg_read",
          "Read one value (name given) or list every value of a key (name empty): name, type and data.",
          {"type": "object", "properties": {"path": {"type": "string"},
                                            "name": {"type": "string", "default": ""}}, "required": ["path"]})
def reg_read(path, name=""):
    info, err = _guard(path)
    if err:
        return err
    ps_path, native = info
    return _head(ps_path, native) + "\n" + _ps(_ps_read_line(ps_path, native, str(name or "")), 60)


@srv.tool("reg_write",
          "Create or overwrite one value. type is string|dword|qword|expandstring|multistring|binary "
          "(binary takes hex, multistring splits on newlines). Missing intermediate keys are created.",
          {"type": "object", "properties": {"path": {"type": "string"}, "name": {"type": "string"},
                                            "value": {"type": "string"},
                                            "type": {"type": "string", "default": "string"}}, "required": ["path", "name", "value"]})
def reg_write(path, name, value, type="string"):
    info, err = _guard(path)
    if err:
        return err
    ps_path, native = info
    ptype = TYPES.get(str(type or "string").strip().lower())
    if not ptype:
        return "type must be one of %s" % "|".join(sorted(TYPES))
    if not str(name or ""):
        return "name must not be empty (the unnamed default value is read-only here)"
    expr = _value_expr(value, ptype)
    if expr is None:
        return "value %r is not valid for type %s" % (value, ptype)
    script = (
        "$p='%s'; $n='%s';"
        "if (-not (Test-Path -Path $p)) { New-Item -Path $p -Force | Out-Null };"
        "New-ItemProperty -Path $p -Name $n -PropertyType %s -Value (%s) -Force | Out-Null;"
        "$k=Get-Item -LiteralPath $p;"
        "$kind=$k.GetValueKind($n).ToString();"
        "$d=$k.GetValue($n,$null,'DoNotExpandEnvironmentNames');"
        "if ($d -is [byte[]]) { $d=($d | ForEach-Object { $_.ToString('x2') }) -join '' };"
        "'written ' + $n + ' | type=' + $kind + ' | data=' + (@($d) -join ',')"
    ) % (_quote(ps_path), _quote(name), ptype, expr)
    return _head(ps_path, native) + "\n" + _ps(script, 60)


@srv.tool("reg_delete_value", "Delete one named value from a key (the unnamed default value cannot be removed this way).",
          {"type": "object", "properties": {"path": {"type": "string"}, "name": {"type": "string"}},
           "required": ["path", "name"]})
def reg_delete_value(path, name):
    info, err = _guard(path)
    if err:
        return err
    ps_path, native = info
    script = (
        "$p='%s'; $n='%s';"
        "if (-not (Test-Path -LiteralPath $p)) { 'ERROR: key not found'; exit 1 };"
        "try { Remove-ItemProperty -LiteralPath $p -Name $n -Force -ErrorAction Stop;"
        "if ((Get-Item -LiteralPath $p).Property -contains $n) { 'ERROR: value still present' } else { 'deleted value ' + $n } }"
        "catch { 'ERROR: ' + $_.Exception.Message }"
    ) % (_quote(ps_path), _quote(name))
    return _head(ps_path, native) + "\n" + _ps(script, 60)


@srv.tool("reg_delete_key",
          "Delete a key. recursive=False fails on a key that still has subkeys, recursive=True removes the whole tree.",
          {"type": "object", "properties": {"path": {"type": "string"},
                                            "recursive": {"type": "boolean", "default": False}}, "required": ["path"]})
def reg_delete_key(path, recursive=False):
    info, err = _guard(path)
    if err:
        return err
    ps_path, native = info
    recurse = "-Recurse " if _flag(recursive, False) else ""
    script = (
        "$p='%s';"
        "if (-not (Test-Path -LiteralPath $p)) { 'ERROR: key not found'; exit 1 };"
        "try { Remove-Item -LiteralPath $p %s-Force -ErrorAction Stop;"
        "if (Test-Path -LiteralPath $p) { 'ERROR: key still present' } else { 'deleted key ' + $p } }"
        "catch { 'ERROR: ' + $_.Exception.Message }"
    ) % (_quote(ps_path), recurse)
    return _head(ps_path, native) + "\n" + _ps(script, 60)


@srv.tool("reg_list_subkeys", "List the immediate subkey names of a key, up to limit.",
          {"type": "object", "properties": {"path": {"type": "string"},
                                            "limit": {"type": "integer", "default": 100}}, "required": ["path"]})
def reg_list_subkeys(path, limit=100):
    info, err = _guard(path)
    if err:
        return err
    ps_path, native = info
    n = max(1, min(2000, int(limit)))
    script = (
        "$p='%s';"
        "try { $kids=Get-ChildItem -LiteralPath $p -ErrorAction Stop } catch { 'ERROR: ' + $_.Exception.Message; exit 1 };"
        "if (-not $kids) { 'subkeys=0' } else {"
        "$c=0; $kids | Sort-Object PSChildName | ForEach-Object { if ($script:c -lt %d) { $script:c++; 'subkey ' + $_.PSChildName } };"
        "'subkeys=' + $script:c + ' of ' + @($kids).Count }"
    ) % (_quote(ps_path), n)
    return _head(ps_path, native) + "\n" + _ps(script, 60)


@srv.tool("reg_export", "Export a key to a .reg file with reg.exe (exact reg.exe output is returned).",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}},
           "required": ["path", "out"]})
def reg_export(path, out):
    info, err = _guard(path)
    if err:
        return err
    _ps_path, native = info
    dest = os.path.abspath(os.path.expanduser(str(out)))
    out_text = _run(["reg", "export", native, dest, "/y"], 120)
    size = os.path.getsize(dest) if os.path.isfile(dest) else -1
    tail = "file=%s bytes=%d" % (dest, size) if size >= 0 else "file=%s not created" % dest
    return "path=%s\ncmd: reg export %s \"%s\" /y\n%s\n%s" % (native, native, dest, out_text, tail)


@srv.tool("reg_backup_key",
          "Dump a key into a hive file with reg save (needs the backup privilege: an elevated shell). "
          "Returns the raw OS error text otherwise.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}},
           "required": ["path", "out"]})
def reg_backup_key(path, out):
    info, err = _guard(path)
    if err:
        return err
    _ps_path, native = info
    dest = os.path.abspath(os.path.expanduser(str(out)))
    out_text = _run(["reg", "save", native, dest, "/y"], 120)
    size = os.path.getsize(dest) if os.path.isfile(dest) else -1
    tail = "file=%s bytes=%d" % (dest, size) if size >= 0 else "file=%s not created" % dest
    return "path=%s\ncmd: reg save %s \"%s\" /y\n%s\n%s" % (native, native, dest, out_text, tail)


@srv.tool("reg_search",
          "Walk a key tree and report value names and data containing keyword (substring, case-insensitive). "
          "limit stops the walk early; a big root such as HKLM\\SOFTWARE can be slow.",
          {"type": "object", "properties": {"root": {"type": "string"}, "keyword": {"type": "string"},
                                            "limit": {"type": "integer", "default": 30}}, "required": ["root", "keyword"]})
def reg_search(root, keyword, limit=30):
    info, err = _guard(root)
    if err:
        return err
    ps_path, native = info
    kw = str(keyword or "")
    if not kw:
        return "keyword must not be empty"
    n = max(1, min(500, int(limit)))
    script = (
        "$root='%s'; $kw='%s'; $limit=%d; $found=0;"
        "$keys=@(Get-Item -LiteralPath $root -ErrorAction SilentlyContinue) + "
        "@(Get-ChildItem -LiteralPath $root -Recurse -ErrorAction SilentlyContinue);"
        "foreach ($k in $keys) {"
        "if ($script:found -ge $limit) { break };"
        "foreach ($vn in $k.Property) {"
        "if ($script:found -ge $limit) { break };"
        "$disp=if ($vn -eq '') { '(default)' } else { $vn };"
        "$d=$k.GetValue($vn,$null,'DoNotExpandEnvironmentNames');"
        "if ($d -is [byte[]]) { $d=($d | ForEach-Object { $_.ToString('x2') }) -join '' };"
        "$s=@($d) -join ',';"
        "if ($disp -like ('*'+$kw+'*') -or $s -like ('*'+$kw+'*')) {"
        "$script:found++; 'hit ' + $k.Name + ' | ' + $disp + ' = ' + $s } } };"
        "if ($found -eq 0) { 'no matches for ' + $kw } else { 'matches=' + $found }"
    ) % (_quote(ps_path), _quote(kw), n)
    return _head(ps_path, native) + "\n" + _ps(script, 120)


def build():
    return srv


# Safe samples: read-only queries against keys that exist on every Windows install.
SAMPLES = {
    "reg_read": {"path": "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion"},
    "reg_list_subkeys": {"path": "HKLM\\SOFTWARE\\Microsoft", "limit": 5},
    "reg_search": {"root": "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion", "keyword": "ProgramFiles", "limit": 5},
}
# reg_search walks a tree, so it is the one sample allowed to be slow/fail
SAMPLES_OPTIONAL = {"reg_search"}


if __name__ == "__main__":
    srv.run()
