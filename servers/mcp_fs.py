"""MCP server: filesystem tools, sandboxed to MCP_FS_ROOTS (pathsep separated)."""
import fnmatch
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

MAX_READ = int(os.environ.get("MCP_FS_MAX_BYTES") or 200000)
MAX_LIST = int(os.environ.get("MCP_FS_MAX_LIST") or 400)

ROOTS = [os.path.abspath(os.path.expanduser(p)) for p in (os.environ.get("MCP_FS_ROOTS") or "").split(os.pathsep) if p.strip()]
if not ROOTS:
    ROOTS = [os.path.abspath(os.path.expanduser("~"))]


def _check(path):
    """Resolve a path and make sure it is really inside an allowed root.

    realpath (not abspath) on purpose: a symlink or a Windows directory junction placed
    inside an allowed folder would otherwise let reads and writes escape the sandbox.
    """
    p = os.path.realpath(os.path.expanduser(str(path)))
    if os.path.isdir(p) or p.endswith(os.sep):
        candidate = p
    else:
        # Writers may target a file that does not exist yet: check its parent too.
        candidate = os.path.dirname(p) or p
    for root in ROOTS:
        real_root = os.path.realpath(root)
        for probe in (p, candidate):
            try:
                if os.path.commonpath([os.path.normcase(real_root), os.path.normcase(probe)]) == os.path.normcase(real_root):
                    return p
            except ValueError:
                continue  # different drive
    raise PermissionError(
        "%s is outside the allowed roots (%s). Ask the user to add that folder in the console "
        "(文件根目录 field) or to MCP_FS_ROOTS in bridge.config.json; call fs_roots to see the list."
        % (p, "; ".join(ROOTS)))


def _drive_of(path):
    return os.path.splitdrive(os.path.abspath(path))[0].upper()


srv = Server("fs")


@srv.tool("fs_roots", "Which folders this tool is allowed to touch (before trying to read somewhere).",
          {"type": "object", "properties": {}, "required": []})
def fs_roots():
    existing = []
    for root in ROOTS:
        existing.append("%s%s" % (root, "" if os.path.isdir(root) else " (missing)"))
    return ("allowed roots (%d):\n%s\nnote=路径沙箱来自配置里的 MCP_FS_ROOTS；需要别的盘/"
            "目录就让用户在控制台的「文件根目录」里加上，或直接改配置后重启。"
            % (len(existing), "\n".join(existing)))


@srv.tool("list_dir", "List entries of a directory (name/type/size).",
          {"type": "object", "properties": {"path": {"type": "string"}, "pattern": {"type": "string", "description": "optional glob filter, e.g. *.py"}}, "required": ["path"]})
def list_dir(path, pattern=""):
    p = _check(path)
    if not os.path.isdir(p):
        raise NotADirectoryError(p)
    rows = []
    with os.scandir(p) as it:
        for e in it:
            if pattern and not fnmatch.fnmatch(e.name, pattern):
                continue
            try:
                size = e.stat().st_size if e.is_file() else 0
            except OSError:
                size = -1
            rows.append("%-4s %10d  %s" % ("DIR" if e.is_dir() else "FILE", size, e.name))
            if len(rows) >= MAX_LIST:
                rows.append("...[truncated at %d entries]" % MAX_LIST)
                break
    return "\n".join(sorted(rows)) or "(empty)"


@srv.tool("read_text", "Read a UTF-8 text file.",
          {"type": "object", "properties": {"path": {"type": "string"}, "max_bytes": {"type": "integer", "default": MAX_READ}}, "required": ["path"]})
def read_text(path, max_bytes=MAX_READ):
    p = _check(path)
    with open(p, "rb") as fh:
        raw = fh.read(int(max_bytes) + 1)
    truncated = len(raw) > int(max_bytes)
    text = raw[:int(max_bytes)].decode("utf-8", errors="replace")
    return text + ("\n...[truncated]" if truncated else "")


@srv.tool("write_text", "Write (or append) text to a file, creating parent dirs.",
          {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean", "default": False}}, "required": ["path", "content"]})
def write_text(path, content, append=False):
    p = _check(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "a" if append else "w", encoding="utf-8") as fh:
        fh.write(content)
    return "wrote %d chars -> %s" % (len(content), p)


@srv.tool("make_dir", "Create a directory (recursive).",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def make_dir(path):
    p = _check(path)
    os.makedirs(p, exist_ok=True)
    return "ok: %s" % p


@srv.tool("move", "Move or rename a path.",
          {"type": "object", "properties": {"src": {"type": "string"}, "dst": {"type": "string"}}, "required": ["src", "dst"]})
def move(src, dst):
    s, d = _check(src), _check(dst)
    os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
    os.replace(s, d)
    return "moved %s -> %s" % (s, d)


@srv.tool("delete", "Delete a file, or a directory when recursive=true.",
          {"type": "object", "properties": {"path": {"type": "string"}, "recursive": {"type": "boolean", "default": False}}, "required": ["path"]})
def delete(path, recursive=False):
    p = _check(path)
    if os.path.isdir(p):
        if not recursive:
            raise IsADirectoryError("pass recursive=true to delete a directory: %s" % p)
        import shutil
        shutil.rmtree(p)
    else:
        os.remove(p)
    return "deleted %s" % p


@srv.tool("glob_search", "Find files by glob pattern under a root, e.g. **/*.md.",
          {"type": "object", "properties": {"pattern": {"type": "string"}, "root": {"type": "string"}, "limit": {"type": "integer", "default": 100}}, "required": ["pattern", "root"]})
def glob_search(pattern, root, limit=100):
    import glob as globmod
    r = _check(root)
    hits = globmod.glob(os.path.join(r, pattern), recursive=True)[: int(limit)]
    return "\n".join(hits) or "(no match)"


@srv.tool("grep_text", "Regex search inside files under a root.",
          {"type": "object", "properties": {"pattern": {"type": "string"}, "root": {"type": "string"}, "glob": {"type": "string", "default": "**/*"}, "max_results": {"type": "integer", "default": 60}}, "required": ["pattern", "root"]})
def grep_text(pattern, root, glob="**/*", max_results=60):
    r = _check(root)
    rx = re.compile(pattern)
    limit = int(max_results)
    out = []
    for dirpath, _dirnames, filenames in os.walk(r):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, r)
            if not fnmatch.fnmatch(rel.replace(os.sep, "/"), glob) and not fnmatch.fnmatch(name, glob):
                continue
            try:
                if os.path.getsize(full) > 2_000_000:
                    continue
                with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            out.append("%s:%d: %s" % (rel, i, line.rstrip()[:300]))
                            if len(out) >= limit:
                                return "\n".join(out) + "\n...[truncated]"
            except (OSError, UnicodeError):
                continue
    return "\n".join(out) or "(no match)"


@srv.tool("file_info", "Stat a path: size, mtime, type.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def file_info(path):
    p = _check(path)
    st = os.stat(p)
    import datetime
    return "path=%s\ntype=%s\nsize=%d\nmtime=%s" % (
        p, "dir" if os.path.isdir(p) else "file", st.st_size,
        datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
    )


@srv.tool("copy", "Copy a file or a directory (recursive).",
          {"type": "object", "properties": {"src": {"type": "string"}, "dst": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}, "required": ["src", "dst"]})
def copy(src, dst, overwrite=False):
    import shutil
    s, d = _check(src), _check(dst)
    if os.path.exists(d) and not overwrite:
        raise FileExistsError("destination exists (pass overwrite=true): %s" % d)
    if os.path.isdir(s):
        shutil.copytree(s, d, dirs_exist_ok=bool(overwrite))
    else:
        os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
        shutil.copy2(s, d)
    return "copied %s -> %s" % (s, d)


@srv.tool("tree", "Print a directory tree up to `depth` levels deep.",
          {"type": "object", "properties": {"path": {"type": "string"}, "depth": {"type": "integer", "default": 2}, "limit": {"type": "integer", "default": 200}}, "required": ["path"]})
def tree(path, depth=2, limit=200):
    root = _check(path)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        level = 0 if rel == "." else rel.count(os.sep) + 1
        if level >= int(depth):
            dirnames[:] = []
        indent = "  " * level
        out.append("%s%s/" % (indent, os.path.basename(dirpath) or root))
        for name in sorted(filenames):
            out.append("%s  %s" % (indent, name))
            if len(out) >= int(limit):
                return "\n".join(out) + "\n...[truncated]"
    return "\n".join(out)


@srv.tool("hash_file", "Hash one file (md5/sha1/sha256) and report its size.",
          {"type": "object", "properties": {"path": {"type": "string"}, "algo": {"type": "string", "default": "sha256"}}, "required": ["path"]})
def hash_file(path, algo="sha256"):
    import hashlib
    p = _check(path)
    digest = hashlib.new(algo)
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return "%s=%s\nsize=%d\npath=%s" % (algo, digest.hexdigest(), os.path.getsize(p), p)


@srv.tool("replace_in_file", "Replace literal text inside a file (count=0 replaces all).",
          {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}, "count": {"type": "integer", "default": 0}}, "required": ["path", "old", "new"]})
def replace_in_file(path, old, new, count=0):
    p = _check(path)
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    hits = text.count(old)
    if not hits:
        return "no occurrence of %r in %s" % (old[:60], p)
    limit = int(count) if int(count) > 0 else -1
    replaced = min(hits, int(count)) if int(count) > 0 else hits
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text.replace(old, new, limit))
    return "replaced %d of %d occurrence(s) in %s" % (replaced, hits, p)


@srv.tool("tail_lines", "Return the last N lines of a text file.",
          {"type": "object", "properties": {"path": {"type": "string"}, "lines": {"type": "integer", "default": 50}}, "required": ["path"]})
def tail_lines(path, lines=50):
    from collections import deque
    p = _check(path)
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        buf = deque(fh, maxlen=max(1, int(lines)))
    return "".join(buf) or "(empty)"


@srv.tool("dir_size", "Total size and file count of a directory, plus its largest files.",
          {"type": "object", "properties": {"path": {"type": "string"}, "limit_files": {"type": "integer", "default": 20000}, "top": {"type": "integer", "default": 5}}, "required": ["path"]})
def dir_size(path, limit_files=20000, top=5):
    root = _check(path)
    total = 0
    count = 0
    biggest = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            total += size
            count += 1
            biggest.append((size, full))
            if count >= int(limit_files):
                break
        if count >= int(limit_files):
            break
    biggest.sort(reverse=True)
    rows = "\n".join("%12d  %s" % (s, f) for s, f in biggest[: max(1, int(top))])
    return "files=%d\ntotal=%.2f MB\ntruncated=%s\nlargest:\n%s" % (
        count, total / 1048576.0, count >= int(limit_files), rows)


def _inside_root(path):
    try:
        return os.path.commonpath([os.path.normcase(ROOTS[0]), os.path.normcase(os.path.abspath(path))]) == os.path.normcase(ROOTS[0])
    except ValueError:
        return False


def _sample_root():
    for root in ROOTS:
        if os.path.isdir(root):
            return root
    return os.environ.get("TEMP") or os.getcwd()


_SAMPLE_ROOT = _sample_root()
_SAMPLE_DIR = os.path.join(_SAMPLE_ROOT, ".aiyu_selftest")
_SAMPLE_FILE = os.path.join(_SAMPLE_DIR, "sample.txt")


SAMPLES = {
    # writer first: the readers below depend on this file existing
    "write_text": {"path": _SAMPLE_FILE, "content": "aiyu self-test sample\nsecond line\n"},
    "fs_roots": {},
    "list_dir": {"path": _SAMPLE_ROOT},
    "read_text": {"path": _SAMPLE_FILE},
    "file_info": {"path": _SAMPLE_FILE},
    "glob_search": {"pattern": "**/*.py", "root": _SAMPLE_ROOT, "limit": 10},
    "grep_text": {"pattern": "def ", "root": _SAMPLE_ROOT, "glob": "*.py", "max_results": 5},
    "hash_file": {"path": _SAMPLE_FILE},
    "tail_lines": {"path": _SAMPLE_FILE, "lines": 2},
    "dir_size": {"path": _SAMPLE_ROOT, "limit_files": 500, "top": 3},
    "tree": {"path": _SAMPLE_ROOT, "depth": 1, "limit": 15},
}


def build():
    return srv


if __name__ == "__main__":
    srv.run()

