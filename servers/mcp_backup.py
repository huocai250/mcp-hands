"""MCP server: directory backups, snapshots, restore and mirroring (stdlib only)."""
import datetime
import hashlib
import os
import shutil
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

TEMP = os.environ.get("TEMP") or os.environ.get("TMP") or "."
SKIP_DIRS = (".git", "node_modules", "__pycache__", ".venv")
STAMP = "%Y%m%d_%H%M%S"

srv = Server("backup")


def _abs(path):
    return os.path.abspath(os.path.expanduser(str(path)))


def _in_dir(path):
    p = _abs(path)
    if not os.path.isdir(p):
        raise NotADirectoryError(p)
    return p


def _dest_dir(path):
    d = _abs(path)
    os.makedirs(d, exist_ok=True)
    return d


def _safe_label(text):
    keep = [c for c in str(text or "") if c.isalnum() or c in "-_"]
    return "".join(keep)[:24]


def _archive_root(source):
    """Walk root plus an optional single-file member name."""
    src = _abs(source)
    if os.path.isdir(src):
        return src, ""
    if os.path.isfile(src):
        return os.path.dirname(src), os.path.basename(src)
    raise FileNotFoundError(source)


def _entries(root, only=""):
    """Yield (full path, archive name), skipping VCS/dependency/cache directories."""
    if only:
        yield os.path.join(root, only), only
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            yield full, os.path.relpath(full, root).replace(os.sep, "/")


def _unsafe(name):
    """True for absolute names, drive letters and any '..' component."""
    n = str(name).replace("\\", "/").strip()
    if not n or n.startswith("/"):
        return True
    if len(n) > 1 and n[1] == ":":
        return True
    return any(part == ".." for part in n.split("/"))


def _target(dest, name):
    return os.path.join(dest, name.replace("\\", "/").replace("/", os.sep))


def _files_of(archive):
    with zipfile.ZipFile(archive) as zf:
        return [i for i in zf.infolist() if not i.is_dir()]


def _stamp():
    return time.strftime(STAMP)


def _free_name(directory, name):
    """Never clobber a sibling created inside the same second."""
    base, ext = os.path.splitext(name)
    candidate = os.path.join(directory, name)
    seq = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, "%s_%d%s" % (base, seq, ext))
        seq += 1
    return candidate


def _resolve_archive(path):
    """Accept the archive itself or a folder: the newest zip inside wins.

    The self-test cannot know the timestamped name `backup_dir` just produced, so
    every reader also takes an archive directory.
    """
    p = _abs(path)
    if os.path.isfile(p):
        if not p.lower().endswith(".zip"):
            raise ValueError("not a zip archive: %s" % p)
        return p
    if os.path.isdir(p):
        rows = _zip_rows(p)
        if not rows:
            raise FileNotFoundError("no zip archive inside %s" % p)
        return rows[0][2]
    raise FileNotFoundError("no such archive or archive directory: %s" % p)


def _zip_rows(directory):
    rows = []
    for name in os.listdir(directory):
        full = os.path.join(directory, name)
        if not (os.path.isfile(full) and name.lower().endswith(".zip")):
            continue
        try:
            rows.append((os.path.getmtime(full), name, full, os.path.getsize(full)))
        except OSError:
            continue
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return rows


@srv.tool("backup_dir", "Zip a directory (or one file) into a timestamped archive and report its sha256.",
          {"type": "object", "properties": {"source": {"type": "string"}, "dest_dir": {"type": "string", "default": ""}, "label": {"type": "string", "default": ""}}, "required": ["source"]})
def backup_dir(source, dest_dir="", label=""):
    root, only = _archive_root(source)
    dest = _dest_dir(dest_dir) if str(dest_dir).strip() else _dest_dir(sample_dir())
    tag = _safe_label(label)
    base_name = os.path.basename(root) if not only else os.path.splitext(only)[0]
    name = "%s%s_%s.zip" % (base_name, ("_" + tag) if tag else "", _stamp())
    dst = _free_name(dest, name)
    count = total = skipped = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for full, arc in _entries(root, only):
            try:
                size = os.stat(full).st_size
            except OSError:
                skipped += 1
                continue
            try:
                zf.write(full, arc)
            except OSError:
                skipped += 1
                continue
            count += 1
            total += size
    digest = hashlib.sha256()
    with open(dst, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return "archive=%s\nsource=%s\nfiles=%d\nbytes=%d\nzip_bytes=%d\nsha256=%s\nskipped=%d" % (
        dst, root, count, total, os.path.getsize(dst), digest.hexdigest(), skipped)


@srv.tool("list_backups", "List backup zips newest first, reading only the zip central directory.",
          {"type": "object", "properties": {"archive_dir": {"type": "string"}, "limit": {"type": "integer", "default": 20}}, "required": ["archive_dir"]})
def list_backups(archive_dir, limit=20):
    d = _in_dir(archive_dir)
    rows = _zip_rows(d)
    cap = max(1, int(limit))
    lines = []
    for mtime, name, full, size in rows[:cap]:
        try:
            count = "files=%d" % len(_files_of(full))
        except zipfile.BadZipFile:
            count = "files=(bad zip)"
        lines.append("%s  %10d bytes  %s  %s" % (
            datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"), size, count, name))
    if len(rows) > cap:
        lines.append("...[truncated at %d of %d archives]" % (cap, len(rows)))
    return "dir=%s\narchives=%d\n%s" % (d, len(rows), "\n".join(lines) if lines else "(no zip archives)")


@srv.tool("restore_backup", "Extract a backup zip (or the newest zip in a folder), skipping absolute and '..' members.",
          {"type": "object", "properties": {"archive": {"type": "string"}, "dest_dir": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}, "required": ["archive", "dest_dir"]})
def restore_backup(archive, dest_dir, overwrite=False):
    src = _resolve_archive(archive)
    dest = _dest_dir(dest_dir)
    written = kept = skipped = 0
    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if _unsafe(name):
                skipped += 1
                continue
            target = _target(dest, name)
            if name.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            if os.path.exists(target) and not overwrite:
                kept += 1
                continue
            os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
            with zf.open(info) as fh, open(target, "wb") as out:
                shutil.copyfileobj(fh, out)
            written += 1
    return "restored=%d kept=%d skipped_unsafe=%d\narchive=%s\ndest=%s" % (
        written, kept, skipped, src, dest)


@srv.tool("verify_backup", "Decompress every member of a zip (or the newest zip in a folder) to test its CRC.",
          {"type": "object", "properties": {"archive": {"type": "string"}}, "required": ["archive"]})
def verify_backup(archive):
    src = _resolve_archive(archive)
    bad = []
    total = 0
    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            total += 1
            try:
                with zf.open(info) as fh:
                    while fh.read(1 << 20):
                        pass
            except Exception as exc:  # noqa: BLE001 - a corrupt member can fail in many ways
                bad.append("%s: %s" % (info.filename, exc))
    if bad:
        return "archive=%s\nmembers=%d\nbad=%d\n%s" % (src, total, len(bad), "\n".join(bad[:40]))
    return "archive=%s\nmembers=%d\nbad=0\ncrc=ok" % (src, total)


@srv.tool("prune_backups", "Report (dry_run=true) or delete the oldest zips, keeping the `keep` newest.",
          {"type": "object", "properties": {"archive_dir": {"type": "string"}, "keep": {"type": "integer", "default": 5}, "dry_run": {"type": "boolean", "default": True}}, "required": ["archive_dir"]})
def prune_backups(archive_dir, keep=5, dry_run=True):
    d = _in_dir(archive_dir)
    rows = _zip_rows(d)
    floor = max(0, int(keep))
    freed = 0
    lines = []
    for _mtime, name, full, size in rows[floor:]:
        if not dry_run:
            try:
                os.remove(full)
            except OSError as exc:
                lines.append("FAILED %s: %s" % (name, exc))
                continue
        freed += size
        lines.append("%s  %10d bytes  %s" % ("deleted" if not dry_run else "would delete", size, name))
    kept = [row[1] for row in rows[:floor]]
    return "dir=%s\narchives=%d\nkeep=%d\ndry_run=%s\nfreeable=%d bytes\n%s\nkept: %s" % (
        d, len(rows), floor, bool(dry_run), freed,
        "\n".join(lines) if lines else "(nothing to prune)",
        ", ".join(kept) if kept else "(none)")


@srv.tool("mirror_dir", "Copy only changed files (same size+mtime => skipped) into dest.",
          {"type": "object", "properties": {"source": {"type": "string"}, "dest": {"type": "string"}, "delete_extra": {"type": "boolean", "default": False}}, "required": ["source", "dest"]})
def mirror_dir(source, dest, delete_extra=False):
    src, dst = _in_dir(source), _dest_dir(dest)
    same_dir = os.path.normcase(src) == os.path.normcase(dst)
    copied = skipped = removed = 0
    keep = set()
    for dirpath, _dirnames, filenames in os.walk(src):
        rel_dir = os.path.relpath(dirpath, src)
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            keep.add(os.path.normcase(rel))
            target = os.path.join(dst, rel)
            try:
                st = os.stat(full)
                if os.path.isfile(target):
                    ts = os.stat(target)
                    if ts.st_size == st.st_size and abs(ts.st_mtime - st.st_mtime) < 2.0:
                        skipped += 1
                        continue
                os.makedirs(os.path.dirname(target) or dst, exist_ok=True)
                shutil.copy2(full, target)
                copied += 1
            except OSError:
                continue
    if delete_extra and not same_dir:
        for dirpath, dirnames, filenames in os.walk(dst, topdown=False):
            rel_dir = os.path.relpath(dirpath, dst)
            for name in filenames:
                rel = name if rel_dir == "." else os.path.join(rel_dir, name)
                if os.path.normcase(rel) in keep:
                    continue
                try:
                    os.remove(os.path.join(dirpath, name))
                    removed += 1
                except OSError:
                    continue
            for name in dirnames:
                try:
                    os.rmdir(os.path.join(dirpath, name))
                except OSError:
                    continue
    return "source=%s\ndest=%s\ncopied=%d\nskipped=%d\nremoved=%d" % (src, dst, copied, skipped, removed)


@srv.tool("snapshot_file", "Copy one file with a timestamp suffix, then keep only the `keep` newest snapshots.",
          {"type": "object", "properties": {"path": {"type": "string"}, "dest_dir": {"type": "string", "default": ""}, "keep": {"type": "integer", "default": 10}}, "required": ["path"]})
def snapshot_file(path, dest_dir="", keep=10):
    src = _abs(path)
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    dest = _dest_dir(dest_dir) if str(dest_dir).strip() else os.path.dirname(src)
    if os.path.normcase(dest) == os.path.normcase(os.path.dirname(src)):
        stem, ext = os.path.splitext(os.path.basename(src))
        prefix, suffix = stem + ".", ext
    else:
        prefix, suffix = os.path.basename(src) + ".", ""
    dst = _free_name(dest, prefix + _stamp() + suffix)
    shutil.copy2(src, dst)
    siblings = []
    for name in os.listdir(dest):
        if not name.startswith(prefix):
            continue
        full = os.path.join(dest, name)
        if os.path.isfile(full):
            try:
                siblings.append((os.path.getmtime(full), full))
            except OSError:
                continue
    siblings.sort(reverse=True)
    floor = max(1, int(keep))
    pruned = 0
    for _mtime, full in siblings[floor:]:
        if os.path.normcase(full) == os.path.normcase(dst):
            continue
        try:
            os.remove(full)
            pruned += 1
        except OSError:
            continue
    return "snapshot=%s\nbytes=%d\ndest=%s\nsnapshots=%d\npruned=%d" % (
        dst, os.path.getsize(dst), dest, len(siblings), pruned)


@srv.tool("disk_usage_report", "Per path: file count, total bytes and the 5 largest files.",
          {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}}, "required": ["paths"]})
def disk_usage_report(paths=[]):
    if not paths:
        raise ValueError("paths must list at least one file or directory")
    blocks = []
    for item in paths:
        p = _abs(item)
        if os.path.isfile(p):
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            blocks.append("%s\n  type=file files=1 total=%d bytes\n  largest:\n%12d  %s" % (p, size, size, p))
            continue
        if not os.path.isdir(p):
            blocks.append("%s\n  missing" % p)
            continue
        total = count = 0
        biggest = []
        for dirpath, _dirnames, filenames in os.walk(p):
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                total += size
                count += 1
                biggest.append((size, full))
        biggest.sort(reverse=True)
        top = "\n".join("%12d  %s" % (s, f) for s, f in biggest[:5]) or "  (empty)"
        blocks.append("%s\n  type=dir files=%d total=%d bytes (%.2f MB)\n  largest:\n%s" % (
            p, count, total, total / 1048576.0, top))
    return "\n\n".join(blocks)


_SELF = os.path.join(sample_dir(), ".aiyu_self_samples", "backup")
_SRC = os.path.join(_SELF, "src")
_ARC = os.path.join(_SELF, "arc")
_RESTORE = os.path.join(_SELF, "restore")
_MIRROR = os.path.join(_SELF, "mirror")
_SNAP_DIR = os.path.join(_SELF, "snapshots")
_SNAP_SRC = os.path.join(_SRC, "notes.txt")
SAMPLES_OPTIONAL = set()


def _seed():
    """Lay down a tiny source tree under sample_dir() so the samples are self-contained."""
    files = {
        os.path.join(_SRC, "readme.txt"): "backup self-test source\n",
        _SNAP_SRC: "snapshot me\n",
        os.path.join(_SRC, "data", "nested.txt"): "nested payload\n",
        os.path.join(_SRC, "__pycache__", "junk.pyc"): "excluded from the archive\n",
    }
    for full, text in files.items():
        if os.path.isfile(full):
            continue
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass


try:
    os.makedirs(_ARC, exist_ok=True)
    _seed()
    _SEEDED = True
except OSError:
    _SEEDED = False

# All samples live under sample_dir(); the readers are non-destructive (prune keeps
# dry_run=true) and verify/restore accept the archive folder because the zip name
# carries a timestamp the self-test cannot predict.
SAMPLES = {
    "backup_dir": {"source": _SRC, "dest_dir": _ARC, "label": "selftest"},
    "list_backups": {"archive_dir": _ARC, "limit": 10},
    "verify_backup": {"archive": _ARC},
    "restore_backup": {"archive": _ARC, "dest_dir": _RESTORE, "overwrite": True},
    "prune_backups": {"archive_dir": _ARC, "keep": 2, "dry_run": True},
    "mirror_dir": {"source": _SRC, "dest": _MIRROR, "delete_extra": False},
    "snapshot_file": {"path": _SNAP_SRC, "dest_dir": _SNAP_DIR, "keep": 2},
    "disk_usage_report": {"paths": [_SRC]},
}
if not _SEEDED:
    SAMPLES_OPTIONAL = {"snapshot_file"}


def build():
    return srv


if __name__ == "__main__":
    srv.run()
