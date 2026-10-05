"""MCP server: zip / tar archives built on the standard library only."""
import os
import shutil
import sys
import tarfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

TEMP = os.environ.get("TEMP") or os.environ.get("TMP") or "."
SAMPLE_DIR = os.path.join(TEMP, "aiyu_mcp_samples")
TAR_MODES = {"gz": "w:gz", "bz2": "w:bz2", "xz": "w:xz", "plain": "w"}

srv = Server("archive")


def _in_path(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.exists(p):
        raise FileNotFoundError(path)
    return p


def _out_path(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    return p


def _dest_dir(path):
    d = os.path.abspath(os.path.expanduser(str(path)))
    os.makedirs(d, exist_ok=True)
    return d


def _unsafe(name):
    """True for absolute names, drive letters and any '..' component."""
    n = str(name).replace("\\", "/").strip()
    if not n or n.startswith("/"):
        return True
    if len(n) > 1 and n[1] == ":":
        return True
    return any(part == ".." for part in n.split("/"))


def _members(paths, base_dir):
    """Yield (absolute file, archive name) for files and recursive directories."""
    base = os.path.abspath(os.path.expanduser(str(base_dir))) if base_dir else ""
    for item in paths or []:
        p = os.path.abspath(os.path.expanduser(str(item)))
        if os.path.isdir(p):
            root = base or os.path.dirname(p)
            for dirpath, _dirs, files in os.walk(p):
                for name in files:
                    full = os.path.join(dirpath, name)
                    yield full, _arc(os.path.relpath(full, root))
        elif os.path.isfile(p):
            root = base or os.path.dirname(p)
            yield p, _arc(os.path.relpath(p, root) if base else os.path.basename(p))
        else:
            raise FileNotFoundError(item)


def _arc(rel):
    rel = rel.replace(os.sep, "/")
    return os.path.basename(rel) if rel.startswith("..") else rel


def _target(dest, name):
    return os.path.join(dest, name.replace("\\", "/").replace("/", os.sep))


def _size_line(size, name):
    return "%10d  %s" % (int(size), name)
@srv.tool("zip_create", "Create a zip from files and/or directories (directories recurse).",
          {"type": "object", "properties": {"out": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}, "base_dir": {"type": "string", "default": ""}}, "required": ["out"]})
def zip_create(out, paths=[], base_dir=""):
    dst = _out_path(out)
    count = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, arc in _members(paths, base_dir):
            zf.write(full, arc)
            count += 1
    return "created %s (entries=%d, %d bytes)" % (dst, count, os.path.getsize(dst))


@srv.tool("zip_add", "Add files/directories to a zip, creating the archive when missing.",
          {"type": "object", "properties": {"out": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["out"]})
def zip_add(out, paths=[]):
    dst = _out_path(out)
    existed = os.path.exists(dst)
    count = 0
    with zipfile.ZipFile(dst, "a", zipfile.ZIP_DEFLATED) as zf:
        for full, arc in _members(paths, ""):
            zf.write(full, arc)
            count += 1
        total = len(zf.namelist())
    return "%s %s (added=%d, entries=%d)" % ("appended to" if existed else "created", dst, count, total)


@srv.tool("zip_list", "List zip entries as 'size  name' lines.",
          {"type": "object", "properties": {"path": {"type": "string"}, "max_entries": {"type": "integer", "default": 200}}, "required": ["path"]})
def zip_list(path, max_entries=200):
    src = _in_path(path)
    limit = max(1, int(max_entries))
    lines = []
    with zipfile.ZipFile(src) as zf:
        infos = zf.infolist()
        for info in infos[:limit]:
            lines.append(_size_line(info.file_size, info.filename))
    if len(infos) > limit:
        lines.append("...[truncated at %d of %d entries]" % (limit, len(infos)))
    return "entries=%d\n%s" % (len(infos), "\n".join(lines) if lines else "(empty archive)")


@srv.tool("zip_extract", "Extract a zip, skipping absolute and '..' members.",
          {"type": "object", "properties": {"path": {"type": "string"}, "dest": {"type": "string"}}, "required": ["path", "dest"]})
def zip_extract(path, dest):
    src, d = _in_path(path), _dest_dir(dest)
    files = skipped = 0
    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if _unsafe(name):
                skipped += 1
                continue
            target = _target(d, name)
            if name.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target) or d, exist_ok=True)
            with zf.open(info) as fh, open(target, "wb") as fout:
                shutil.copyfileobj(fh, fout)
            files += 1
    return "extracted %d file(s) from %s to %s (skipped %d unsafe)" % (files, src, d, skipped)


@srv.tool("zip_extract_member", "Extract one zip member by exact name.",
          {"type": "object", "properties": {"path": {"type": "string"}, "member": {"type": "string"}, "dest": {"type": "string"}}, "required": ["path", "member", "dest"]})
def zip_extract_member(path, member, dest):
    src, d = _in_path(path), _dest_dir(dest)
    name = str(member).replace("\\", "/")
    if _unsafe(name):
        raise ValueError("refusing unsafe member path: %r" % member)
    target = _target(d, name)
    os.makedirs(os.path.dirname(target) or d, exist_ok=True)
    with zipfile.ZipFile(src) as zf:
        try:
            with zf.open(name) as fh, open(target, "wb") as fout:
                shutil.copyfileobj(fh, fout)
        except KeyError:
            raise FileNotFoundError("no such member %r in %s" % (member, src))
    return "extracted member %s -> %s (%d bytes)" % (name, target, os.path.getsize(target))
@srv.tool("tar_create", "Create a tar archive; fmt is gz, bz2, xz or plain.",
          {"type": "object", "properties": {"out": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}, "fmt": {"type": "string", "default": "gz"}}, "required": ["out"]})
def tar_create(out, paths=[], fmt="gz"):
    key = str(fmt or "gz").lower()
    if key not in TAR_MODES:
        raise ValueError("fmt must be one of %s, got %r" % (sorted(TAR_MODES), fmt))
    dst = _out_path(out)
    count = 0
    with tarfile.open(dst, TAR_MODES[key]) as tf:
        for full, arc in _members(paths, ""):
            tf.add(full, arcname=arc)
            count += 1
    return "created %s (fmt=%s entries=%d, %d bytes)" % (dst, key, count, os.path.getsize(dst))


@srv.tool("tar_list", "List tar members as 'size  name' lines.",
          {"type": "object", "properties": {"path": {"type": "string"}, "max_entries": {"type": "integer", "default": 200}}, "required": ["path"]})
def tar_list(path, max_entries=200):
    src = _in_path(path)
    limit = max(1, int(max_entries))
    lines = []
    with tarfile.open(src, "r:*") as tf:
        members = tf.getmembers()
        for member in members[:limit]:
            lines.append(_size_line(member.size, member.name + ("/" if member.isdir() else "")))
    if len(members) > limit:
        lines.append("...[truncated at %d of %d entries]" % (limit, len(members)))
    return "entries=%d\n%s" % (len(members), "\n".join(lines) if lines else "(empty archive)")


@srv.tool("tar_extract", "Extract a tar archive, skipping absolute and '..' members.",
          {"type": "object", "properties": {"path": {"type": "string"}, "dest": {"type": "string"}}, "required": ["path", "dest"]})
def tar_extract(path, dest):
    src, d = _in_path(path), _dest_dir(dest)
    files = skipped = 0
    with tarfile.open(src, "r:*") as tf:
        for member in tf.getmembers():
            name = member.name.replace("\\", "/")
            if _unsafe(name) or member.issym() or member.islnk():
                skipped += 1
                continue
            target = _target(d, name)
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            if not member.isfile():
                skipped += 1
                continue
            os.makedirs(os.path.dirname(target) or d, exist_ok=True)
            handle = tf.extractfile(member)
            if handle is None:
                skipped += 1
                continue
            with handle, open(target, "wb") as fout:
                shutil.copyfileobj(handle, fout)
            files += 1
    return "extracted %d file(s) from %s to %s (skipped %d unsafe)" % (files, src, d, skipped)


_SRC = next((c for c in [os.path.join(SAMPLE_DIR, "src.txt"),
                         os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "win.ini")]
             if os.path.isfile(c)), "")
_ZIP = os.path.join(SAMPLE_DIR, "sample.zip")
_TAR = os.path.join(SAMPLE_DIR, "sample.tar.gz")

# Ordered so the lists/extractions run after the writers; all outputs stay in TEMP.
SAMPLES = {}
if _SRC:
    SAMPLES.update({
        "zip_create": {"out": _ZIP, "paths": [_SRC], "base_dir": os.path.dirname(_SRC)},
        "zip_add": {"out": _ZIP, "paths": [_SRC]},
        "zip_list": {"path": _ZIP, "max_entries": 50},
        "zip_extract": {"path": _ZIP, "dest": os.path.join(SAMPLE_DIR, "unzip")},
        "zip_extract_member": {"path": _ZIP, "member": os.path.basename(_SRC), "dest": os.path.join(SAMPLE_DIR, "unzip_one")},
        "tar_create": {"out": _TAR, "paths": [_SRC], "fmt": "gz"},
        "tar_list": {"path": _TAR, "max_entries": 50},
        "tar_extract": {"path": _TAR, "dest": os.path.join(SAMPLE_DIR, "untar")},
    })


def build():
    return srv


if __name__ == "__main__":
    srv.run()


