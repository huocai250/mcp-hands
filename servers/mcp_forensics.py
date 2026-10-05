"""MCP server: forensics tools over local files and network fixtures (stdlib only).

Scans are capped: at most MCP_FORENSICS_MAX_FILES files and 2 MB per file for the
directory-wide tools (hash_dir, find_secrets, entropy_scan, code_stats-style walks),
and every cap that trims the result is reported in the output.
Optional sandbox: MCP_FORENSICS_ROOTS (pathsep separated); when unset any absolute path is allowed.
"""
import codecs
import datetime
import hashlib
import math
import os
import re
import struct
import subprocess
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

# the bridge reads our stdout as UTF-8; without this, non-ASCII paths or interface
# aliases would be locale-encoded (e.g. cp936) and arrive as mojibake
try:
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
except (AttributeError, OSError, ValueError):
    pass

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
HERE = os.path.dirname(os.path.abspath(__file__))

MAX_OUT = int(os.environ.get("MCP_FORENSICS_MAX_CHARS") or 12000)
MAX_FILE_BYTES = 2_000_000
MAX_FILES = int(os.environ.get("MCP_FORENSICS_MAX_FILES") or 400)
PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"

ROOTS = [os.path.abspath(os.path.expanduser(p)) for p in (os.environ.get("MCP_FORENSICS_ROOTS") or "").split(os.pathsep) if p.strip()]


def _check(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    if not ROOTS:
        return p
    for root in ROOTS:
        try:
            if os.path.commonpath([os.path.normcase(root), os.path.normcase(p)]) == os.path.normcase(root):
                return p
        except ValueError:
            continue  # different drive
    raise PermissionError("path outside allowed roots %s: %s" % (ROOTS, p))


def _clip(text, limit=MAX_OUT):
    s = str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + "\n...[truncated %d chars]" % (len(s) - limit)


def _read(path, limit=MAX_FILE_BYTES):
    """Read at most `limit` bytes; return (data, extra_note, size)."""
    p = _check(path)
    if os.path.isdir(p):
        raise IsADirectoryError(p)
    size = os.path.getsize(p)
    with open(p, "rb") as fh:
        data = fh.read(limit)
    note = ""
    if size > len(data):
        note = "\n...[first %d of %d bytes only]" % (len(data), size)
    return p, data, size, note


def _ps(script, timeout_s=60):
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_PREFIX + script],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=float(timeout_s), creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return "exit=timeout\n[timed out after %ss]" % timeout_s
    except OSError as exc:
        return "exit=-1\n%s" % exc
    out = (p.stdout or "").strip()
    if (p.stderr or "").strip():
        out += ("\n" if out else "") + "[stderr] " + p.stderr.strip()
    return "exit=%s\n%s" % (p.returncode, out)


srv = Server("forensics")


@srv.tool("file_hashes", "MD5/SHA1/SHA256 plus size and mtime of one file.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def file_hashes(path):
    p = _check(path)
    if os.path.isdir(p):
        raise IsADirectoryError(p)
    st = os.stat(p)
    digests = {"md5": hashlib.md5(), "sha1": hashlib.sha1(), "sha256": hashlib.sha256()}
    with open(p, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            for h in digests.values():
                h.update(chunk)
    return "\n".join([
        "path=%s" % p,
        "size=%d" % st.st_size,
        "mtime=%s" % datetime.datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
        "md5=%s" % digests["md5"].hexdigest(),
        "sha1=%s" % digests["sha1"].hexdigest(),
        "sha256=%s" % digests["sha256"].hexdigest(),
    ])


@srv.tool("hash_dir", "Hash files under a directory (first 2 MB of each); unreadable files are skipped.",
          {"type": "object", "properties": {"root": {"type": "string"}, "algo": {"type": "string", "default": "sha256"}, "limit": {"type": "integer", "default": 200}}, "required": ["root"]})
def hash_dir(root, algo="sha256", limit=200):
    r = _check(root)
    if not os.path.isdir(r):
        raise NotADirectoryError(r)
    name = str(algo).strip().lower()
    if name not in hashlib.algorithms_available:
        return "unknown algo: %s (try md5, sha1, sha256, sha512)" % algo
    cap = min(int(limit), MAX_FILES)
    clamped = int(limit) > MAX_FILES
    rows = []
    skipped = 0
    partial = 0
    capped = False
    for dirpath, _dirnames, filenames in os.walk(r):
        for filename in sorted(filenames):
            if len(rows) >= cap:
                capped = True
                break
            full = os.path.join(dirpath, filename)
            if not os.path.isfile(full):
                continue
            try:
                st = os.stat(full)
                h = hashlib.new(name)
                with open(full, "rb") as fh:
                    remaining = MAX_FILE_BYTES
                    while remaining > 0:
                        chunk = fh.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        h.update(chunk)
            except OSError:
                skipped += 1
                continue
            flag = ""
            if st.st_size > MAX_FILE_BYTES:
                flag = "  (partial: first %d bytes)" % MAX_FILE_BYTES
                partial += 1
            rows.append("%s  %10d  %s%s" % (h.hexdigest(), st.st_size, os.path.relpath(full, r), flag))
        if capped:
            break
    notes = ["root=%s\nalgo=%s files=%d limit=%d" % (r, name, len(rows), cap)]
    if clamped:
        notes.append("...[limit clamped to the file cap %d]" % MAX_FILES)
    notes += rows
    if skipped:
        notes.append("...skipped %d unreadable file(s)" % skipped)
    if partial:
        notes.append("...%d file(s) larger than %d bytes were hashed partially" % (partial, MAX_FILE_BYTES))
    if capped:
        notes.append("...[truncated: limit %d reached, remaining files not hashed]" % cap)
    return _clip("\n".join(notes))


@srv.tool("strings_extract", "Print printable ASCII runs of at least min_len characters, with file offsets.",
          {"type": "object", "properties": {"path": {"type": "string"}, "min_len": {"type": "integer", "default": 6}, "limit": {"type": "integer", "default": 200}, "encoding": {"type": "string", "default": "latin-1"}}, "required": ["path"]})
def strings_extract(path, min_len=6, limit=200, encoding="latin-1"):
    codec = str(encoding)
    try:
        codecs.lookup(codec)
    except LookupError:
        return "unknown encoding: %s" % encoding
    p, data, size, note = _read(path)
    n = max(1, int(min_len))
    cap = int(limit)
    rx = re.compile(rb"[\x20-\x7e]{%d,}" % n)
    rows = []
    for m in rx.finditer(data):
        if len(rows) >= cap:
            note += "\n...[truncated: limit %d matches reached]" % cap
            break
        rows.append("%08x  %s" % (m.start(), m.group(0).decode(codec, errors="replace")))
    head = "path=%s\nsize=%d shown=%d min_len=%d encoding=%s" % (p, size, len(rows), n, codec)
    return _clip("\n".join([head] + rows) + note, MAX_OUT)


@srv.tool("entropy_scan", "Shannon entropy per block; top 10 blocks and a packed/encrypted verdict.",
          {"type": "object", "properties": {"path": {"type": "string"}, "block": {"type": "integer", "default": 4096}}, "required": ["path"]})
def entropy_scan(path, block=4096):
    p, data, size, note = _read(path)
    size_block = max(16, int(block))
    blocks = []
    for offset in range(0, len(data), size_block):
        chunk = data[offset:offset + size_block]
        counts = [0] * 256
        for byte in chunk:
            counts[byte] += 1
        total = len(chunk)
        ent = 0.0
        printable = 0
        for value, count in enumerate(counts):
            if count:
                prob = count / total
                ent -= prob * math.log(prob, 2)
                if 0x20 <= value <= 0x7e:
                    printable += count
        blocks.append((ent, offset, len(chunk), printable / total))
    if not blocks:
        return "path=%s\nsize=0\n(empty file: nothing to measure)" % p
    mean = sum(b[0] for b in blocks) / len(blocks)
    top = sorted(blocks, key=lambda b: -b[0])[:10]
    if mean >= 7.5:
        verdict = "packed/encrypted (mean entropy %.4f >= 7.5)" % mean
    elif mean >= 6.5:
        verdict = "compressed or mixed (mean entropy %.4f in 6.5..7.5)" % mean
    else:
        verdict = "plain (mean entropy %.4f < 6.5: text/code-like)" % mean
    rows = ["%08x  entropy=%.4f  bytes=%d  printable=%.2f" % (offset, ent, length, pr) for ent, offset, length, pr in top]
    head = "path=%s\nsize=%d block=%d blocks=%d\nmean=%.4f max=%.4f min=%.4f\ntop 10 blocks:" % (
        p, size, size_block, len(blocks), mean, top[0][0], min(b[0] for b in blocks))
    return _clip("\n".join([head] + rows + ["verdict=" + verdict]) + note)


@srv.tool("hex_dump", "Classic offset/hex/ascii dump of a file region.",
          {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "default": 0}, "length": {"type": "integer", "default": 256}}, "required": ["path"]})
def hex_dump(path, offset=0, length=256):
    p = _check(path)
    if os.path.isdir(p):
        raise IsADirectoryError(p)
    size = os.path.getsize(p)
    start = max(0, int(offset))
    want = max(1, min(int(length), 4096))
    with open(p, "rb") as fh:
        fh.seek(start)
        data = fh.read(want)
    rows = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hexa = " ".join("%02x" % b for b in chunk)
        text = "".join(chr(b) if 0x20 <= b <= 0x7e else "." for b in chunk)
        rows.append("%08x  %-47s  |%s|" % (start + i, hexa, text))
    head = "path=%s\nsize=%d offset=%d length=%d shown=%d" % (p, size, start, want, len(data))
    return _clip("\n".join([head] + rows))


MACHINES = {0x014C: "i386", 0x8664: "x64", 0x01C0: "ARM", 0x01C4: "ARMNT", 0xAA64: "ARM64", 0x0200: "IA64"}
SUBSYSTEMS = {0: "unknown", 1: "native", 2: "windows-gui", 3: "windows-console", 5: "os2-console",
              7: "posix-console", 9: "windows-ce", 10: "efi-application", 11: "efi-boot", 12: "efi-rom", 14: "xbox"}
FILE_CHARS = [(0x0001, "RELOCS_STRIPPED"), (0x0002, "EXECUTABLE_IMAGE"), (0x0004, "LINE_NUMS_STRIPPED"),
              (0x0008, "LOCAL_SYMS_STRIPPED"), (0x0010, "AGGRESSIVE_WS_TRIM"), (0x0020, "LARGE_ADDRESS_AWARE"),
              (0x0080, "BYTES_REVERSED_LO"), (0x0100, "32BIT_MACHINE"), (0x0200, "DEBUG_STRIPPED"),
              (0x0400, "REMOVABLE_RUN_FROM_SWAP"), (0x0800, "NET_RUN_FROM_SWAP"), (0x1000, "SYSTEM"),
              (0x2000, "DLL"), (0x4000, "UP_SYSTEM_ONLY"), (0x8000, "BYTES_REVERSED_HI")]
SCN_CHARS = [(0x00000008, "TYPE_NO_PAD"), (0x00000020, "CNT_CODE"), (0x00000040, "CNT_INITIALIZED_DATA"),
             (0x00000080, "CNT_UNINITIALIZED_DATA"), (0x00000100, "LNK_OTHER"), (0x00000200, "LNK_INFO"),
             (0x00000800, "LNK_REMOVE"), (0x00001000, "LNK_COMDAT"), (0x00008000, "GPREL"),
             (0x00020000, "MEM_PURGEABLE"), (0x00040000, "MEM_LOCKED"), (0x00080000, "MEM_PRELOAD"),
             (0x01000000, "LNK_NRELOC_OVFL"), (0x02000000, "MEM_DISCARDABLE"), (0x04000000, "MEM_NOT_CACHED"),
             (0x08000000, "MEM_NOT_PAGED"), (0x10000000, "MEM_SHARED"), (0x20000000, "MEM_EXECUTE"),
             (0x40000000, "MEM_READ"), (0x80000000, "MEM_WRITE")]
SCN_ALIGN = {0x00100000: "ALIGN_1BYTES", 0x00200000: "ALIGN_2BYTES", 0x00300000: "ALIGN_4BYTES",
             0x00400000: "ALIGN_8BYTES", 0x00500000: "ALIGN_16BYTES", 0x00600000: "ALIGN_32BYTES",
             0x00700000: "ALIGN_64BYTES", 0x00800000: "ALIGN_128BYTES", 0x00900000: "ALIGN_256BYTES",
             0x00A00000: "ALIGN_512BYTES", 0x00B00000: "ALIGN_1024BYTES", 0x00C00000: "ALIGN_2048BYTES",
             0x00D00000: "ALIGN_4096BYTES", 0x00E00000: "ALIGN_8192BYTES"}
DLL_CHARS = [(0x0020, "HIGH_ENTROPY_VA"), (0x0040, "DYNAMIC_BASE"), (0x0080, "FORCE_INTEGRITY"),
             (0x0100, "NX_COMPAT"), (0x0200, "NO_ISOLATION"), (0x0400, "NO_SEH"), (0x0800, "NO_BIND"),
             (0x1000, "APPCONTAINER"), (0x2000, "WDM_DRIVER"), (0x4000, "GUARD_CF"), (0x8000, "TERMINAL_SERVER_AWARE")]


def _flags(value, table, align_mask=0):
    names = [name for bit, name in table if value & bit]
    if align_mask and value & align_mask:
        names.append(SCN_ALIGN.get(value & align_mask, "ALIGN_UNKNOWN"))
    return " | ".join(names) if names else "(none)"


@srv.tool("pe_info", "Parse DOS/PE headers with struct: machine, sections, timestamp, entry point, image base, flags.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def pe_info(path):
    p, data, size, note = _read(path)
    if len(data) < 64:
        return "not a PE file: %s is only %d bytes (no DOS header)" % (p, size)
    if data[:2] != b"MZ":
        return "not a PE file: %s has no DOS 'MZ' signature (magic=%s)" % (p, data[:2].hex())
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew < 4 or e_lfanew + 24 > len(data):
        return "not a PE file: %s has e_lfanew=%d outside the header (size=%d)" % (p, e_lfanew, size)
    if data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        return "not a PE file: %s has no 'PE\\0\\0' signature at e_lfanew=%d" % (p, e_lfanew)
    try:
        machine, nsections, timestamp, _symtab, _nsyms, opt_size, chars = struct.unpack_from("<HHIIIHH", data, e_lfanew + 4)
        opt_off = e_lfanew + 24
        magic = struct.unpack_from("<H", data, opt_off)[0]
        if magic == 0x10B:
            kind, base_fmt = "PE32", "<I"
        elif magic == 0x20B:
            kind, base_fmt = "PE32+", "<Q"
        else:
            kind, base_fmt = "unknown", "<I"
        entry = struct.unpack_from("<I", data, opt_off + 16)[0]
        image_base = struct.unpack_from(base_fmt, data, opt_off + (24 if kind == "PE32+" else 28))[0]
        sec_align, file_align = struct.unpack_from("<II", data, opt_off + 32)
        size_image = struct.unpack_from("<I", data, opt_off + 56)[0]
        subsystem = struct.unpack_from("<H", data, opt_off + 68)[0]
        dll_chars = struct.unpack_from("<H", data, opt_off + 70)[0]
    except struct.error as exc:
        return "not a PE file: %s has a truncated optional header (%s)" % (p, exc)
    stamp = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat(timespec="seconds") if timestamp else "0 (not set)"
    rows = ["path=%s" % p, "size=%d" % size, "tier=DOS+PE (%s)" % kind,
            "machine=0x%04x %s" % (machine, MACHINES.get(machine, "unknown")),
            "sections=%d" % nsections,
            "timestamp=%s (unix %d)" % (stamp, timestamp),
            "entry_point=0x%08x" % entry,
            "image_base=0x%x" % image_base,
            "section_alignment=0x%x file_alignment=0x%x size_of_image=0x%x" % (sec_align, file_align, size_image),
            "subsystem=%d %s" % (subsystem, SUBSYSTEMS.get(subsystem, "unknown")),
            "characteristics=0x%04x %s" % (chars, _flags(chars, FILE_CHARS)),
            "dll_characteristics=0x%04x %s" % (dll_chars, _flags(dll_chars, DLL_CHARS)),
            "section table (idx name vsize raw va ptr flags):"]
    table = opt_off + opt_size
    for i in range(nsections):
        off = table + i * 40
        if off + 40 > len(data):
            rows.append("  %2d  <section header %d beyond the %d bytes read>" % (i, i, len(data)))
            continue
        name, vsize, vaddr, raw_size, raw_ptr = struct.unpack_from("<8sIIII", data, off)
        scn = struct.unpack_from("<I", data, off + 36)[0]
        rows.append("  %2d  %-8s vsize=0x%08x raw=0x%08x va=0x%08x ptr=0x%08x  %s" % (
            i, name.rstrip(b"\0").decode("latin-1"), vsize, raw_size, vaddr, raw_ptr, _flags(scn, SCN_CHARS, 0x00F00000)))
    return _clip("\n".join(rows) + note)


SECRET_RULES = [
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("assignment", re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*\S{6,}")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
]


def _mask(value):
    v = str(value)
    if len(v) <= 6:
        return v[:1] + "..."
    return v[:4] + "..." + v[-4:]


def _mask_match(rule, matched):
    if rule == "assignment":
        sep = "=" if "=" in matched else ":"
        key, _, val = matched.partition(sep)
        return key + sep + _mask(val)
    return _mask(matched)


@srv.tool("find_secrets", "Regex-scan text files under a root for keys/tokens/passwords/IPs/emails; values are masked.",
          {"type": "object", "properties": {"root": {"type": "string"}, "limit": {"type": "integer", "default": 50}}, "required": ["root"]})
def find_secrets(root, limit=50):
    r = _check(root)
    if not os.path.isdir(r):
        raise NotADirectoryError(r)
    cap = int(limit)
    hits = []
    scanned = 0
    skipped = 0
    short = 0
    capped = False
    for dirpath, _dirnames, filenames in os.walk(r):
        for filename in sorted(filenames):
            if scanned >= MAX_FILES or len(hits) >= cap:
                capped = True
                break
            full = os.path.join(dirpath, filename)
            if not os.path.isfile(full):
                continue
            try:
                size = os.path.getsize(full)
                with open(full, "rb") as fh:
                    raw = fh.read(MAX_FILE_BYTES)
            except OSError:
                skipped += 1
                continue
            if size > MAX_FILE_BYTES:
                short += 1
            if b"\x00" in raw[:4096]:
                continue  # binary
            scanned += 1
            text = raw.decode("utf-8", errors="replace")
            rel = os.path.relpath(full, r)
            for lineno, line in enumerate(text.splitlines(), 1):
                for rule, rx in SECRET_RULES:
                    for m in rx.finditer(line):
                        hits.append("%s:%d: %s: %s" % (rel, lineno, rule, _mask_match(rule, m.group(0))))
                        if len(hits) >= cap:
                            capped = True
                            break
                    if capped:
                        break
                if capped:
                    break
        if capped:
            break
    notes = ["root=%s\nfiles_scanned=%d (cap %d) hits=%d limit=%d" % (r, scanned, MAX_FILES, len(hits), cap)]
    notes += hits
    if skipped:
        notes.append("...skipped %d unreadable file(s)" % skipped)
    if short:
        notes.append("...%d file(s) larger than %d bytes were scanned partially" % (short, MAX_FILE_BYTES))
    if capped:
        notes.append("...[truncated: limit/cap reached, remaining files not scanned]")
    return _clip("\n".join(notes))


@srv.tool("net_connections", "TCP connections from Get-NetTCPConnection, joined with owning process names.",
          {"type": "object", "properties": {"limit": {"type": "integer", "default": 40}}, "required": []})
def net_connections(limit=40):
    script = (
        "$proc=@{};"
        "Get-Process -ErrorAction SilentlyContinue|ForEach-Object{$proc[[int]$_.Id]=$_.ProcessName};"
        "Get-NetTCPConnection -ErrorAction SilentlyContinue|"
        "Sort-Object State,LocalPort|Select-Object -First " + str(int(limit)) + "|"
        "ForEach-Object{'{0,-11} {1,-23} {2,-23} {3}' -f $_.State,($_.LocalAddress+':'+$_.LocalPort),"
        "($_.RemoteAddress+':'+$_.RemotePort),($proc[[int]$_.OwningProcess]+'#'+$_.OwningProcess)}"
    )
    out = _ps(script, 60)
    if out.startswith("exit=timeout") or out.startswith("exit=-1"):
        return _clip("limit=%d shown=0\n%s" % (int(limit), out))
    body = out.split("\n", 1)[1] if "\n" in out else ""
    rows = [row for row in body.splitlines() if row.strip() and not row.startswith("[stderr]")]
    if not rows:
        return _clip("limit=%d shown=0\n%s" % (int(limit), out))
    head = "limit=%d shown=%d (STATE LOCAL REMOTE PROCESS#PID)" % (int(limit), len(rows))
    return _clip("\n".join([head] + rows))


@srv.tool("lan_hosts", "ARP/neighbor table as: ip, mac, state, interface.",
          {"type": "object", "properties": {"timeout_ms": {"type": "integer", "default": 500}}, "required": []})
def lan_hosts(timeout_ms=500):
    # PowerShell startup dominates: the floor keeps a small timeout_ms usable.
    wait_s = max(15.0, float(timeout_ms) / 1000.0)
    script = (
        "Get-NetNeighbor -ErrorAction SilentlyContinue|"
        "Where-Object{$_.State -ne 'Unreachable' -and $_.LinkLayerAddress -and $_.LinkLayerAddress -ne '00-00-00-00-00-00'}|"
        "Sort-Object State,IPAddress|"
        "ForEach-Object{'{0,-18} {1,-18} {2,-12} {3}' -f $_.IPAddress,$_.LinkLayerAddress,$_.State,$_.InterfaceAlias}"
    )
    out = _ps(script, wait_s)
    rows = [row for row in out.split("\n", 1)[-1].splitlines() if row.strip()]
    source = "Get-NetNeighbor"
    if not rows:
        alt = _ps("arp -a", wait_s)
        source = "arp -a (fallback)"
        for line in alt.splitlines():
            m = re.match(r"\s*(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})\s+(\S+)", line)
            if m:
                rows.append("%-18s %-18s %-12s %s" % (m.group(1), m.group(2), m.group(3), "arp -a"))
    if not rows:
        return _clip("source=%s neighbors=0\ntimeout_ms=%d\n%s" % (source, int(timeout_ms), out))
    return _clip("source=%s neighbors=%d timeout_ms=%d\n%s" % (source, len(rows), int(timeout_ms), "\n".join(rows)))


MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "PNG image", (".png",)),
    (b"\xff\xd8\xff", "JPEG image", (".jpg", ".jpeg", ".jpe")),
    (b"GIF87a", "GIF image", (".gif",)),
    (b"GIF89a", "GIF image", (".gif",)),
    (b"%PDF-", "PDF document", (".pdf",)),
    (b"PK\x03\x04", "ZIP archive", (".zip", ".jar", ".apk", ".epub", ".docx", ".xlsx", ".pptx", ".whl")),
    (b"PK\x05\x06", "ZIP archive (empty)", (".zip",)),
    (b"PK\x07\x08", "ZIP archive (spanned)", (".zip",)),
    (b"\x1f\x8b", "GZIP archive", (".gz", ".tgz")),
    (b"\x7fELF", "ELF binary", (".so", ".elf", "")),
    (b"Rar!\x1a\x07", "RAR archive", (".rar",)),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip archive", (".7z",)),
    (b"SQLite format 3\x00", "SQLite database", (".db", ".sqlite", ".sqlite3")),
    (b"ID3", "MP3 audio (ID3 tag)", (".mp3",)),
    (b"\x00asm", "WebAssembly module", (".wasm",)),
    (b"BM", "BMP image", (".bmp",)),
    (b"\xca\xfe\xba\xbe", "Java class file", (".class",)),
    (b"RIFF", "RIFF container (WAV/AVI/WebP)", (".wav", ".avi", ".webp")),
    (b"\xd0\xcf\x11\xe0", "OLE2 compound file (legacy Office/MSI)", (".doc", ".xls", ".ppt", ".msi")),
]
OOXML_DIRS = {"word/": "OOXML DOCX (Word)", "xl/": "OOXML XLSX (Excel)", "ppt/": "OOXML PPTX (PowerPoint)"}


@srv.tool("file_type", "Sniff the magic bytes of a file and compare the result with its extension.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def file_type(path):
    p = _check(path)
    if os.path.isdir(p):
        raise IsADirectoryError(p)
    size = os.path.getsize(p)
    with open(p, "rb") as fh:
        head = fh.read(4096)
    kind, expected = None, ()
    if head[:2] == b"MZ":
        if len(head) >= 0x40:
            e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
            if 0 < e_lfanew < len(head) - 4 and head[e_lfanew:e_lfanew + 4] == b"PE\0\0":
                kind, expected = "PE executable (Windows)", (".exe", ".dll", ".sys", ".scr", ".ocx")
            else:
                kind, expected = "DOS MZ executable (not PE)", (".exe", ".com")
    if kind is None:
        for magic, name, exts in MAGIC:
            if head.startswith(magic):
                kind, expected = name, exts
                break
    if kind is None and head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        kind, expected = "MP3 audio (frame sync)", (".mp3",)
    if kind is None and len(head) >= 12 and head[4:8] == b"ftyp":
        kind, expected = "MP4/ISO base media", (".mp4", ".m4a", ".mov", ".3gp")
    if kind and kind.startswith("ZIP"):
        try:
            with zipfile.ZipFile(p) as zf:
                names = zf.namelist()[:500]
            if "[Content_Types].xml" in names:
                sub = None
                for prefix, label in OOXML_DIRS.items():
                    if any(n.startswith(prefix) for n in names):
                        sub = label
                        break
                kind = sub or "OOXML package (unknown subtype)"
                expected = (".docx", ".xlsx", ".pptx")
        except (zipfile.BadZipFile, OSError):
            pass
    ext = os.path.splitext(p)[1].lower()
    if kind is None:
        kind = "unknown"
    if not ext:
        verdict = "no extension to compare"
    elif ext in expected:
        verdict = "extension matches content"
    elif kind == "unknown":
        verdict = "extension %s not recognised from the magic bytes" % ext
    else:
        verdict = "MISMATCH: extension %s but content looks like %s" % (ext, kind)
    return "\n".join([
        "path=%s" % p,
        "size=%d" % size,
        "magic=%s" % head[:16].hex(),
        "type=%s" % kind,
        "declared_extension=%s" % (ext or "(none)"),
        "verdict=%s" % verdict,
    ])


# --- self-test samples: read-only, existing paths only; interactive/destructive tools omitted ---
_SAMPLE_PE = r"C:\Windows\System32\notepad.exe"
if not os.path.isfile(_SAMPLE_PE):
    _SAMPLE_PE = sys.executable

SAMPLES = {
    "file_hashes": {"path": _SAMPLE_PE},
    "hash_dir": {"root": sample_dir(), "algo": "sha256", "limit": 20},
    "strings_extract": {"path": _SAMPLE_PE, "min_len": 8, "limit": 15, "encoding": "latin-1"},
    "entropy_scan": {"path": _SAMPLE_PE, "block": 4096},
    "hex_dump": {"path": _SAMPLE_PE, "offset": 0, "length": 128},
    "pe_info": {"path": _SAMPLE_PE},
    "find_secrets": {"root": sample_dir(), "limit": 10},
    "net_connections": {"limit": 10},
    "lan_hosts": {"timeout_ms": 500},
    "file_type": {"path": _SAMPLE_PE},
}


def build():
    return srv


if __name__ == "__main__":
    srv.run()
