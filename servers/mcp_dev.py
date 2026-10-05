"""MCP server: dev tools -- git (git.exe), a Python scratch runner, and text/data helpers.

git calls go through git.exe with an argument list (never a shell string) and a 120s timeout;
every git tool answers "exit=<code>\\n<output>". Optional sandbox: MCP_DEV_ROOTS
(pathsep separated); when unset any absolute path is allowed.
"""
import base64
import datetime
import hashlib
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

# the bridge reads our stdout as UTF-8; without this, non-ASCII paths would be locale-encoded
try:
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
except (AttributeError, OSError, ValueError):
    pass

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
HERE = os.path.dirname(os.path.abspath(__file__))
TEMP = os.environ.get("TEMP") or tempfile.gettempdir()

MAX_OUT = int(os.environ.get("MCP_DEV_MAX_CHARS") or 12000)
GIT_TIMEOUT = 120
PY_MAX = 8000
MAX_FILE_BYTES = 2_000_000
MAX_FILES = 5000

GIT = os.environ.get("MCP_DEV_GIT") or r"D:\Program Files\Git\cmd\git.exe"
if not os.path.isfile(GIT):
    GIT = shutil.which("git") or GIT

ROOTS = [os.path.abspath(os.path.expanduser(p)) for p in (os.environ.get("MCP_DEV_ROOTS") or "").split(os.pathsep) if p.strip()]
ALPHABET = string.ascii_letters + string.digits


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


def _as_text(raw):
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _git(repo, args, timeout_s=GIT_TIMEOUT):
    r = _check(repo)
    if not os.path.isdir(r):
        raise NotADirectoryError(r)
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat",
                "GIT_EDITOR": "true", "EDITOR": "true", "GIT_OPTIONAL_LOCKS": "0"})
    argv = [GIT, "--no-pager", "-c", "color.ui=false", "-C", r] + list(args)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=float(timeout_s), env=env, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return "exit=timeout\n[git timed out after %ss]" % timeout_s
    except OSError as exc:
        return "exit=-1\n%s" % exc
    out = _as_text(p.stdout)
    err = _as_text(p.stderr)
    if err.strip():
        out += ("\n" if out.strip() else "") + err
    return _clip("exit=%d\n%s" % (p.returncode, out.strip()))


srv = Server("dev")


@srv.tool("git_status", "Show short branch + worktree status of a repository.",
          {"type": "object", "properties": {"repo": {"type": "string", "description": "repository directory"}}, "required": ["repo"]})
def git_status(repo):
    return _git(repo, ["status", "--short", "--branch"])


@srv.tool("git_diff", "Show a diff of the worktree, or of the index when staged=true.",
          {"type": "object", "properties": {"repo": {"type": "string"}, "staged": {"type": "boolean", "default": False}, "path": {"type": "string", "default": ""}}, "required": ["repo"]})
def git_diff(repo, staged=False, path=""):
    args = ["diff"]
    if staged:
        args.append("--cached")
    if path:
        args += ["--", str(path)]
    return _git(repo, args)


@srv.tool("git_log", "Show the most recent commits (one line each).",
          {"type": "object", "properties": {"repo": {"type": "string"}, "limit": {"type": "integer", "default": 15}}, "required": ["repo"]})
def git_log(repo, limit=15):
    return _git(repo, ["log", "--oneline", "--decorate", "-n", str(int(limit))])


@srv.tool("git_branches", "List local and remote branches with their upstream state.",
          {"type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]})
def git_branches(repo):
    return _git(repo, ["branch", "-a", "-vv"])


@srv.tool("git_add", "Stage paths (default: everything in the repository).",
          {"type": "object", "properties": {"repo": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}, "default": ["."]}}, "required": ["repo"]})
def git_add(repo, paths=["."]):
    items = [str(p) for p in (paths or ["."])]
    return _git(repo, ["add", "--"] + items)


@srv.tool("git_commit", "Commit staged work (add_all=true stages every change first).",
          {"type": "object", "properties": {"repo": {"type": "string"}, "message": {"type": "string"}, "add_all": {"type": "boolean", "default": True}}, "required": ["repo", "message"]})
def git_commit(repo, message, add_all=True):
    out = []
    if add_all:
        out.append("[git add -A]\n" + _git(repo, ["add", "-A"]))
    out.append("[git commit]\n" + _git(repo, ["commit", "-m", str(message)]))
    return _clip("\n".join(out))


@srv.tool("git_show", "Show one object (commit, tag or tree) with its patch.",
          {"type": "object", "properties": {"repo": {"type": "string"}, "ref": {"type": "string", "default": "HEAD"}}, "required": ["repo"]})
def git_show(repo, ref="HEAD"):
    return _git(repo, ["show", "--stat", "--patch", str(ref)])


BLOCKED_TOKENS = ("-i", "--interactive")
BLOCKED_ADD_COMMIT = ("-p", "--patch", "-e", "--edit")


def _guard(args):
    """Validate the extra git arguments: JSON list of strings, no shell, nothing destructive."""
    if isinstance(args, (str, bytes)) or not isinstance(args, list):
        raise ValueError("args must be a JSON list of git arguments, e.g. [\"status\", \"--short\"]")
    if not args:
        raise ValueError("args is empty: pass the git arguments, e.g. [\"log\", \"-n\", \"5\"]")
    if len(args) > 32:
        raise ValueError("too many arguments (%d > 32)" % len(args))
    toks = []
    for a in args:
        if not isinstance(a, str):
            raise ValueError("every git argument must be a string, got %s" % type(a).__name__)
        if "\n" in a or "\r" in a:
            raise ValueError("argument must be a single line: %r" % a[:40])
        if len(a) > 500:
            raise ValueError("argument too long (%d chars)" % len(a))
        toks.append(a)
    low = [t.lower() for t in toks]
    cmd = low[0]
    if cmd == "push":
        raise ValueError("blocked: git_run never runs push (it changes a remote); "
                         "remote work stays with the human operator")
    if cmd == "reset" and "--hard" in low:
        raise ValueError("blocked: reset --hard throws away local work; "
                         "stage it or commit it first, then reset without --hard")
    if cmd == "clean" and any(t.startswith("-") and "f" in t and ("d" in t or "x" in t) for t in low):
        raise ValueError("blocked: clean with force/delete flags deletes untracked files")
    if "filter-branch" in low or "filter-repo" in low:
        raise ValueError("blocked: history rewriting (filter-branch / filter-repo) is out of scope here")
    if cmd == "config" and any(t in ("--global", "--system") for t in low):
        raise ValueError("blocked: changing global or system git config affects every repository on the host")
    if any(t in BLOCKED_TOKENS for t in low) or (cmd in ("add", "commit") and any(t in BLOCKED_ADD_COMMIT for t in low)):
        raise ValueError("blocked: interactive git commands would block the server; pass plain arguments")
    return toks


@srv.tool("git_run", "Run git with a JSON list of extra arguments, e.g. [\"log\", \"-n\", \"5\"]; never a shell string.",
          {"type": "object", "properties": {"repo": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}, "description": "git arguments as a list, e.g. [\"status\", \"--short\"]"}}, "required": ["repo", "args"]})
def git_run(repo, args):
    return _git(repo, _guard(args))


@srv.tool("run_python", "Run a short Python snippet in a temp file and return its stdout/stderr.",
          {"type": "object", "properties": {"code": {"type": "string"}, "timeout_s": {"type": "integer", "default": 30}}, "required": ["code"]})
def run_python(code, timeout_s=30):
    fd, path = tempfile.mkstemp(prefix="mcp_dev_", suffix=".py", dir=TEMP)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            body = str(code)
            fh.write(body if body.endswith("\n") else body + "\n")
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            p = subprocess.run([sys.executable, path], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=max(1.0, float(timeout_s)), cwd=TEMP, env=env,
                               creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            return _clip("exit=timeout\n[timeout after %ss]\n%s" % (timeout_s, _as_text(exc.stdout)), PY_MAX)
        except OSError as exc:
            return "exit=-1\n%s" % exc
        out = _as_text(p.stdout)
        err = _as_text(p.stderr)
        if err.strip():
            out += ("\n" if out.strip() else "") + "[stderr]\n" + err
        return _clip("exit=%d\n%s" % (p.returncode, out.strip()), PY_MAX)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _load_json(text):
    """Accept a JSON document or the path of a file holding one."""
    s = str(text)
    note = ""
    if os.path.isfile(s):
        with open(s, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            note = " ...[file truncated to %d bytes]" % MAX_FILE_BYTES
        s = raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
    return json.loads(s), note


@srv.tool("json_pretty", "Pretty-print a JSON string, or a file containing JSON.",
          {"type": "object", "properties": {"text": {"type": "string", "description": "JSON text or a file path"}}, "required": ["text"]})
def json_pretty(text):
    try:
        doc, note = _load_json(text)
    except ValueError as exc:
        return "json error: %s" % exc
    except OSError as exc:
        return "read error: %s" % exc
    return _clip(json.dumps(doc, indent=2, ensure_ascii=False) + note)


def _walk_path(doc, key_path):
    cur = doc
    walked = []
    for part in str(key_path).split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            if not part.lstrip("-").isdigit():
                raise LookupError("list needs a numeric index at %s" % ".".join(walked + [part]))
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            if part not in cur:
                raise LookupError("no such key: %s" % ".".join(walked + [part]))
            cur = cur[part]
        else:
            raise LookupError("cannot descend into %s at %s" % (type(cur).__name__, ".".join(walked + [part])))
        walked.append(part)
    return cur, ".".join(walked)


@srv.tool("json_query", "Read one value by dotted path, e.g. a.b.0.c, from a JSON string or file.",
          {"type": "object", "properties": {"text": {"type": "string", "description": "JSON text or a file path"}, "key_path": {"type": "string"}}, "required": ["text", "key_path"]})
def json_query(text, key_path):
    try:
        doc, _note = _load_json(text)
    except ValueError as exc:
        return "json error: %s" % exc
    except OSError as exc:
        return "read error: %s" % exc
    try:
        value, walked = _walk_path(doc, key_path)
    except LookupError as exc:
        return "not found: %s" % exc
    except IndexError:
        return "index out of range: %s" % key_path
    if isinstance(value, str):
        return "%s = %s" % (walked or "<root>", value)
    return "%s = %s" % (walked or "<root>", json.dumps(value, ensure_ascii=False))


@srv.tool("b64_encode", "Base64-encode text (UTF-8).",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def b64_encode(text):
    return base64.b64encode(str(text).encode("utf-8")).decode("ascii")


@srv.tool("b64_decode", "Base64-decode text back to a UTF-8 string.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def b64_decode(text):
    raw = "".join(str(text).split())
    try:
        data = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
    except Exception as exc:
        return "base64 error: %s" % exc
    return data.decode("utf-8", errors="replace")


@srv.tool("hash_text", "Hash a string, e.g. algo=sha256.",
          {"type": "object", "properties": {"text": {"type": "string"}, "algo": {"type": "string", "default": "sha256"}}, "required": ["text"]})
def hash_text(text, algo="sha256"):
    name = str(algo).strip().lower()
    if name not in hashlib.algorithms_available:
        return "unknown algo: %s (try md5, sha1, sha256, sha512)" % algo
    h = hashlib.new(name)
    h.update(str(text).encode("utf-8"))
    return "%s %s" % (name, h.hexdigest())


@srv.tool("uuid_gen", "Generate one or more random UUID4 values.",
          {"type": "object", "properties": {"count": {"type": "integer", "default": 1}}, "required": []})
def uuid_gen(count=1):
    n = int(count)
    if n < 1 or n > 100:
        return "count must be 1..100, got %s" % count
    return "\n".join(str(uuid.uuid4()) for _ in range(n))


@srv.tool("random_string", "Generate a cryptographically random alphanumeric string.",
          {"type": "object", "properties": {"length": {"type": "integer", "default": 24}}, "required": []})
def random_string(length=24):
    n = int(length)
    if n < 1 or n > 4096:
        return "length must be 1..4096, got %s" % length
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


@srv.tool("epoch_to_time", "Convert a unix epoch value to UTC and local time.",
          {"type": "object", "properties": {"value": {"type": ["number", "string"], "description": "unix seconds"}}, "required": ["value"]})
def epoch_to_time(value):
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return "not a number: %r" % (value,)
    try:
        utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        local = datetime.datetime.fromtimestamp(ts).astimezone()
    except (OverflowError, OSError, ValueError) as exc:
        return "out of range: %s" % exc
    return "epoch=%s\nutc=%s\nlocal=%s" % (value, utc.isoformat(timespec="seconds"), local.isoformat(timespec="seconds"))


@srv.tool("code_stats", "Count files/lines/blank lines and list the 10 largest files under a root.",
          {"type": "object", "properties": {"root": {"type": "string"}, "ext": {"type": "string", "default": "", "description": "optional extension filter, e.g. .py"}}, "required": ["root"]})
def code_stats(root, ext=""):
    r = _check(root)
    if not os.path.isdir(r):
        raise NotADirectoryError(r)
    want = str(ext).strip().lower()
    if want and not want.startswith("."):
        want = "." + want
    files = 0
    lines = 0
    blank = 0
    total_bytes = 0
    big = 0
    capped = False
    largest = []
    for dirpath, _dirnames, filenames in os.walk(r):
        for name in filenames:
            if want and not name.lower().endswith(want):
                continue
            full = os.path.join(dirpath, name)
            if files >= MAX_FILES:
                capped = True
                break
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if not os.path.isfile(full):
                continue
            files += 1
            total_bytes += size
            largest.append((size, os.path.relpath(full, r)))
            if size > MAX_FILE_BYTES:
                big += 1
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        lines += 1
                        if not line.strip():
                            blank += 1
            except OSError:
                big += 1
        if capped:
            break
    largest.sort(key=lambda row: (-row[0], row[1]))
    head = "root=%s\nfiles=%d bytes=%d lines=%d blank=%d\nfilter=*%s\nlargest:" % (
        r, files, total_bytes, lines, blank, want or "")
    rows = ["%10d  %s" % (size, rel) for size, rel in largest[:10]]
    notes = []
    if big:
        notes.append("line counts skipped for %d file(s) larger than %d bytes or unreadable" % (big, MAX_FILE_BYTES))
    if capped:
        notes.append("...[file cap %d reached, remaining files not counted]" % MAX_FILES)
    return _clip("\n".join([head] + rows + notes))


# --- self-test samples: read-only, or writing only under %TEMP%; destructive tools are omitted ---
def _sample_repo():
    """First candidate that really is a git work tree, else the first candidate that exists."""
    home = os.path.expanduser("~")
    cands = [os.environ.get("MCP_DEV_SAMPLE_REPO") or "",
             os.path.join(home, "Documents"),
             os.path.join(home, "Desktop"),
             os.path.join(TEMP, "mcp_dev_sample_repo"),
             os.getcwd()]
    for cand in cands:
        if cand and os.path.isdir(os.path.join(cand, ".git")):
            return cand
    for cand in cands:
        if cand and os.path.isdir(cand):
            return cand
    return os.getcwd()


_SAMPLE_REPO = _sample_repo()

SAMPLES = {
    "git_status": {"repo": _SAMPLE_REPO},
    "git_diff": {"repo": _SAMPLE_REPO, "staged": False, "path": ""},
    "git_log": {"repo": _SAMPLE_REPO, "limit": 3},
    "git_branches": {"repo": _SAMPLE_REPO},
    "git_show": {"repo": _SAMPLE_REPO, "ref": "HEAD"},
    "git_run": {"repo": _SAMPLE_REPO, "args": ["status", "--short"]},
    "run_python": {"code": "print(6*7)"},
    "json_pretty": {"text": "{\"b\": 1, \"a\": [1, 2]}"},
    "json_query": {"text": "{\"a\": {\"b\": [10, 20, {\"c\": \"x\"}]}}", "key_path": "a.b.2.c"},
    "b64_encode": {"text": "hello world"},
    "b64_decode": {"text": "aGVsbG8gd29ybGQ="},
    "hash_text": {"text": "abc"},
    "uuid_gen": {"count": 2},
    "random_string": {"length": 12},
    "epoch_to_time": {"value": 1700000000},
    "code_stats": {"root": sample_dir(), "ext": ".py"},
}


def build():
    return srv


if __name__ == "__main__":
    srv.run()
