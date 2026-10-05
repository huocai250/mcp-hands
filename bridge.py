#!/usr/bin/env python3
"""persona-mcp-bridge -- give the phone App's persona real MCP hands.

The phone App exposes an OpenAI-compatible endpoint that is bound to the
persona's *own* conversation. Two facts, both verified live against it:

  1. native `tools` are passed upstream and `tool_calls` come back, but
     `role:"tool"` results are NOT accepted back (the request then fails with
     "An assistant message with 'tool_calls' must be followed by tool messages"),
     and the orphaned call stays in the App's conversation -> every later
     request 500s until that conversation is cleared;
  2. every request is appended to that conversation (measured: a single
     message landed at index 49 of the upstream payload).

So this bridge never sends `tools` upstream. Instead it runs the tool loop
itself with a plain-text tool protocol, talks to real MCP servers over stdio,
and exposes a normal OpenAI-compatible endpoint to desktop clients.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ------------------------------------------------------------------ branding
APP_NAME = "mcp-hands"
APP_VERSION = "2.0.0"
APP_AUTHOR = "huocai250"
APP_URL = "https://github.com/huocai250/mcp-hands"
APP_LICENSE = "MIT"
APP_TAGLINE = "把手机里的人设接上电脑的 353 个工具：能看图、能读屏幕、能自己干长活"


def version_line():
    return "%s v%s by %s — %s (%s License)" % (APP_NAME, APP_VERSION, APP_AUTHOR, APP_URL, APP_LICENSE)


# Python 3.12 still defaults to the locale code page on piped stdout (cp936 here),
# which turns any Chinese tool output into mojibake. Force UTF-8 for console + log.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else HERE
ARGV = sys.argv[1:]

# `--mcp-server NAME` turns this program into one of the MCP stdio servers. It must
# happen before any logging/config work: stdout is the JSON-RPC channel.
if "--mcp-server" in ARGV:
    from server_host import run_server  # noqa: E402
    run_server(ARGV[ARGV.index("--mcp-server") + 1])
    raise SystemExit(0)

from jobs import JobRunner, JobStore  # noqa: E402
from mcp_client import ToolHub  # noqa: E402


def _opt(name, default=None):
    if name in ARGV:
        index = ARGV.index(name)
        if index + 1 < len(ARGV):
            return ARGV[index + 1]
    return default


LOG_LOCK = threading.Lock()
LOG_FILE = os.path.join(BASE_DIR, "bridge.log")
LOG_SINKS = []


def add_log_sink(fn):
    """Register a callback that receives every log line (used by the GUI)."""
    LOG_SINKS.append(fn)
    return fn

DEFAULT_CONFIG = {
    "listen": {"host": "127.0.0.1", "port": 8877},
    "upstream": {"name": "phone-persona", "base_url": "http://127.0.0.1:8866/v1",
                 "api_key": "aiyu", "model": "aiyu-proxy", "timeout_s": 240, "temperature": None},
    "tool_mode": "text-protocol",
    "always_expose_tools": True,
    "max_tool_rounds": 4,
    "upstream_history": "last_user_only",
    "include_client_system": False,
    "vision": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-flash",
        "detail": "auto",
        "thinking": "disabled",
        "max_pixels": 1300,
        "inherit_upstream_key": True,
    },
    "jobs": {"enabled": True, "workers": 1, "notify": True, "db": ""},
    "memory": {"auto_index": True},
    "servers": [
        {"name": "fs", "enabled": True, "env": {"MCP_FS_ROOTS": os.path.expanduser("~")}},
        {"name": "shell", "enabled": True},
        {"name": "web", "enabled": True},
        {"name": "sys", "enabled": True},
        {"name": "office", "enabled": True},
        {"name": "media", "enabled": True},
        {"name": "archive", "enabled": True},
        {"name": "sqlite", "enabled": True},
        {"name": "desktop", "enabled": True},
        {"name": "voice", "enabled": True},
        {"name": "monitor", "enabled": True},
        {"name": "net", "enabled": True},
        {"name": "dev", "enabled": True},
        {"name": "forensics", "enabled": True},
        {"name": "text", "enabled": True},
        {"name": "pdf", "enabled": True},
        {"name": "qr", "enabled": True},
        {"name": "backup", "enabled": True},
        {"name": "http", "enabled": True},
        {"name": "media2", "enabled": True},
        {"name": "sched", "enabled": True},
        {"name": "soft", "enabled": True},
        {"name": "registry", "enabled": True},
        {"name": "netadv", "enabled": True},
        {"name": "vision", "enabled": True},
        {"name": "files2", "enabled": True},
        {"name": "calc", "enabled": True},
        {"name": "notes", "enabled": True},
        {"name": "pwd", "enabled": True},
        {"name": "netcheck", "enabled": True},
        {"name": "office2", "enabled": True},
        {"name": "jobs", "enabled": True},
        {"name": "memory", "enabled": True},
    ],
}

_elected = _opt("--config") or next((a for a in ARGV if not a.startswith("--")), None)
CONFIG_PATH = os.environ.get("BRIDGE_CONFIG") or _elected or os.path.join(BASE_DIR, "bridge.config.json")


def write_default_config(path=None):
    path = path or CONFIG_PATH
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(DEFAULT_CONFIG, fh, ensure_ascii=False, indent=2)
    return path


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    with LOG_LOCK:
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001 - windowed builds may have no stdout
            pass
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    for sink in list(LOG_SINKS):
        try:
            sink(line)
        except Exception:  # noqa: BLE001 - a broken sink must never break the bridge
            pass


# --------------------------------------------------------------------- config
def load_config():
    if not os.path.exists(CONFIG_PATH):
        write_default_config()
        log("no config found -> wrote default config: %s" % CONFIG_PATH)
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("listen", {"host": "127.0.0.1", "port": 8877})
    cfg.setdefault("max_tool_rounds", 4)
    cfg.setdefault("upstream_history", "last_user_only")
    cfg.setdefault("include_client_system", False)
    cfg.setdefault("always_expose_tools", True)
    # Keep old config files working: new proxy knobs get sane defaults here.
    proxy = cfg.setdefault("proxy", {})
    proxy.setdefault("max_tool_rounds", 12)
    proxy.setdefault("max_seconds", 120)
    proxy.setdefault("heartbeat_seconds", 5)
    proxy.setdefault("vision_check_seconds", 600)
    proxy.setdefault("step_report", "brief")
    proxy.setdefault("progress_stream", True)
    proxy.setdefault("progress_max", 3)
    cfg.setdefault("vision", {}).setdefault("thinking", "disabled")
    jobs = cfg.setdefault("jobs", {})
    jobs.setdefault("enabled", True)
    jobs.setdefault("workers", 1)
    jobs.setdefault("notify", True)
    cfg.setdefault("memory", {}).setdefault("auto_index", True)
    return cfg


# Keys added after a config was first written. `--migrate` fills them in without
# touching anything the user already set.
MIGRATIONS = {
    "vision.base_url": "https://api.deepseek.com/v1",
    "vision.api_key": "",
    "vision.model": "deepseek-flash",
    "vision.detail": "auto",
    "vision.thinking": "disabled",
    "vision.max_pixels": 1300,
    "vision.inherit_upstream_key": True,
    "proxy.enabled": True,
    "proxy.max_tool_rounds": 12,
    "proxy.max_seconds": 120,
    "proxy.heartbeat_seconds": 5,
    "proxy.vision_check_seconds": 600,
    "proxy.step_report": "brief",
    "proxy.progress_stream": True,
    "proxy.progress_max": 3,
    "proxy.inject_tool_hint": True,
    "jobs.enabled": True,
    "jobs.workers": 1,
    "jobs.notify": True,
    "jobs.db": "",
    "memory.auto_index": True,
}
NEW_SERVERS = ("jobs", "memory")


def migrate_config(path=None):
    """Add missing keys/servers to an existing config (keeps a .bak copy)."""
    target = path or CONFIG_PATH
    with open(target, encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    added = []
    for dotted, value in MIGRATIONS.items():
        section, key = dotted.split(".", 1)
        block = cfg.setdefault(section, {})
        if key not in block:
            block[key] = value
            added.append(dotted)
    names = {s.get("name") for s in cfg.get("servers", [])}
    for name in NEW_SERVERS:
        if name not in names:
            cfg.setdefault("servers", []).append({"name": name, "enabled": True})
            added.append("servers.%s" % name)
    if not added:
        return "config already up to date: %s (v%s)" % (target, APP_VERSION)
    shutil.copyfile(target, target + ".bak")
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return "migrated %s\nadded: %s\nbackup: %s.bak" % (target, ", ".join(sorted(added)), target)


CFG = load_config()
UP = CFG["upstream"]
HUB = None
STARTED_AT = time.time()
# Prometheus-style counters exposed at /metrics.
METRICS = {"requests_total": 0, "tool_calls_total": 0, "prompt_tokens_total": 0,
           "completion_tokens_total": 0, "errors_total": 0}
_JOBS = None
_RUNNER = None


def job_store():
    """Durable job queue (2.0): one store per process, SQLite-backed."""
    global _JOBS
    if _JOBS is None:
        cfg = CFG.get("jobs") or {}
        _JOBS = JobStore(cfg.get("db") or os.path.join(BASE_DIR, "jobs.db"))
    return _JOBS


def _job_call_tool(name, args):
    return start_hub().call(name, args)


def _job_notify(job):
    """Reach the user when a background job finishes - the app itself cannot be pushed."""
    cfg = CFG.get("jobs") or {}
    if cfg.get("notify", True) is False or job.get("kind") in ("silent", "test"):
        return
    detail = (job.get("result") or job.get("error") or "").strip().splitlines()
    message = "%s：%s" % ("完成" if job.get("status") == "done" else "出错了",
                          (job.get("title") or job.get("kind") or "后台任务")[:60])
    if detail:
        message += " - " + detail[0][:80]
    try:
        start_hub().call("sys_toast", {"title": "mcp-hands 后台任务", "message": message})
    except Exception as exc:  # noqa: BLE001
        log("job notify failed: %s" % exc)


def job_runner():
    """Background worker pool; started lazily so plain CLI runs stay cheap."""
    global _RUNNER
    if _RUNNER is None:
        cfg = CFG.get("jobs") or {}
        _RUNNER = JobRunner(job_store(), call_tool=_job_call_tool, log=lambda m: log("  " + m),
                            notify=_job_notify, workers=max(1, int(cfg.get("workers", 1) or 1)))
        if cfg.get("enabled", True) is not False:
            _RUNNER.start()
            log("job engine started (%d worker(s), db=%s)" % (_RUNNER.workers, job_store().path))
    return _RUNNER


def stop_jobs():
    global _RUNNER
    if _RUNNER is not None:
        _RUNNER.stop()
        _RUNNER = None


def start_hub():
    """Start every configured MCP server on first use (idempotent)."""
    global HUB
    if HUB is None:
        vision = CFG.get("vision") or {}
        extra_env = None
        if vision:
            extra_env = {"MCP_VISION_BASE_URL": vision.get("base_url", ""),
                         "MCP_VISION_API_KEY": vision.get("api_key", ""),
                         "MCP_VISION_MODEL": vision.get("model", ""),
                         "MCP_VISION_DETAIL": vision.get("detail", ""),
                         "MCP_VISION_THINKING": vision.get("thinking", ""),
                         "MCP_VISION_MAX_PIXELS": vision.get("max_pixels", "")}
        # Servers that talk back to the bridge (jobs) need to know where it listens.
        port = (CFG.get("listen") or {}).get("port", 8877)
        extra_env = dict(extra_env or {})
        extra_env["MCP_BRIDGE_API"] = "http://127.0.0.1:%s" % port
        # 2.0: dropping a new servers/mcp_*.py is enough - anything on disk that the
        # config does not mention is picked up automatically (set "enabled": false to
        # switch one off explicitly).
        configured = [dict(entry) for entry in CFG.get("servers", [])]
        known = {entry.get("name") for entry in configured}
        try:
            from server_host import discover_names
            fresh = [name for name in discover_names() if name not in known]
            for name in fresh:
                configured.append({"name": name, "enabled": True})
            if fresh:
                log("auto-enabled %d newly discovered server(s): %s" % (len(fresh), ", ".join(fresh)))
        except Exception as exc:  # noqa: BLE001
            log("server discovery skipped: %s" % exc)
        HUB = ToolHub(configured, cwd=BASE_DIR, log=lambda m: log("  " + m),
                      entry=None if FROZEN else os.path.join(HERE, "bridge.py"),
                      extra_env=extra_env)
    return HUB


def stop_hub():
    global HUB
    if HUB is not None:
        try:
            HUB.stop()
        except Exception:  # noqa: BLE001
            pass
        HUB = None


def reload_config(path=None):
    """Re-read the config file (used by the GUI before every start)."""
    global CFG, UP, CONFIG_PATH
    stop_hub()
    if path:
        CONFIG_PATH = path
    CFG = load_config()
    UP = CFG["upstream"]
    return CFG


def cmd_version():
    print(version_line())
    print("author : %s" % APP_AUTHOR)
    print("repo   : %s" % APP_URL)
    print("license: %s" % APP_LICENSE)
    print("tagline: %s" % APP_TAGLINE)
    return 0


def run_command(name):
    """Run one of the command-line commands in-process; returns its exit code."""
    if name in ("version", "--version", "-V"):
        return cmd_version()
    if name in ("tools", "--tools"):
        return cmd_tools()
    if name in ("self-test", "self_test", "--self-test"):
        return cmd_selftest()
    if name in ("doctor", "--doctor"):
        return cmd_doctor()
    if name in ("init", "--init"):
        print("config written: %s" % write_default_config(_opt("--config")))
        return 0
    if name in ("migrate", "--migrate"):
        print(migrate_config(_opt("--config")))
        return 0
    if name in ("jobs", "--jobs"):
        return cmd_jobs(int(_opt("--limit") or 20))
    if name in ("jobs-run", "--jobs-run"):
        return cmd_jobs_run(_opt("--jobs-run") or "")
    return 2

# ------------------------------------------------------------- tool protocol
PROTOCOL = """TOOL_PROTOCOL_V1
你现在运行在用户的电脑上，可以通过输出一段工具指令来真的调用电脑上的工具，工具会真的执行并把结果返回给你。

[运行环境]
%s

工具清单（每行：名称 | 用途 | 参数，参数名后带 ? 的是可选项）：
%s

调用格式（严格照抄；不要写 JSON，不要使用方括号——方括号里的内容会被系统吞掉）：
TOOL: 工具名
参数名=值
参数名2=值2
END

规则：
- 值不要加引号；路径直接写 C:\\Users\\... 这种形式。
- 列表参数用竖线分隔（如 tags=a|b）；二维表格用分号分行、逗号分列（如 rows=name,qty;apple,3）。
- 同一个参数名写多行会自动合并成列表，适合长段落。
- 一段回复里可以写多个 TOOL 段，每段以 END 结束。
- 工具执行后你会收到一条以 TOOL_RESULT 开头的消息，再据此继续回答。
- 不需要工具时，就像平常一样自然回答（照常使用你的语气和表情包）。
- 工具报错时先读错误信息，改好参数再试；Windows 下没有 ls/cat/grep，列目录用 fs_list_dir。"""


def environment_block():
    import platform
    import socket
    roots = ""
    for server in CFG.get("servers", []):
        if server.get("name") == "fs":
            roots = (server.get("env") or {}).get("MCP_FS_ROOTS", "")
    return "\n".join([
        "- 操作系统: %s %s (Windows 语义)" % (platform.system(), platform.release()),
        "- 主机名 / 用户: %s / %s" % (socket.gethostname(), os.environ.get("USERNAME", "?")),
        "- 现在时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "- 文件工具可访问目录: %s" % (roots.replace(";", " ; ") or os.path.expanduser("~")),
        "- 命令一律用 Windows 语法（优先 PowerShell / cmd），不存在 ls、cat、grep 这类命令。",
    ])


def protocol_block(specs):
    rows = []
    for spec in specs:
        schema = spec.get("inputSchema") or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        params = ", ".join("%s%s" % (name, "" if name in required else "?") for name in props)
        rows.append("- %s | %s | %s" % (spec["name"], spec["description"], params or "(无参数)"))
    return PROTOCOL % (environment_block(), "\n".join(rows))


_FENCE_TOOL = re.compile(r"```(?:tool|json|tool_call)\s*\n(.*?)\n?```", re.S | re.I)
_TAGGED = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S | re.I)
_TOOL_HEAD = re.compile(r"(?mi)^\s*TOOL\s*[::]\s*([A-Za-z0-9_.\-]+)\s*$")
_KV = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_\-]*)\s*[=:]\s*(.*?)\s*$")


def parse_line_calls(text):
    """Bracket-free protocol, the primary one.

    TOOL: office_xlsx_create
    path=C:\\Users\\me\\Desktop\\a.xlsx
    rows=name,qty;apple,3
    END

    The phone app deletes every [ ... ] span from model output, which makes JSON
    arrays unusable, so lists are written with | (1-D) or ; + , (2-D).
    """
    lines = (text or "").splitlines()
    calls = []
    current = None
    for line in lines:
        head = _TOOL_HEAD.match(line)
        if head:
            if current:
                calls.append(current)
            current = {"name": head.group(1), "arguments": {}}
            continue
        if current is None:
            continue
        if line.strip().upper() in ("END", "```"):
            calls.append(current)
            current = None
            continue
        match = _KV.match(line)
        if not match:
            if not line.strip():
                continue
            calls.append(current)
            current = None
            continue
        key, value = match.group(1), match.group(2).strip().strip('"').strip("'")
        if key in current["arguments"]:
            existing = current["arguments"][key]
            current["arguments"][key] = existing + [value] if isinstance(existing, list) else [existing, value]
        else:
            current["arguments"][key] = value
    if current:
        calls.append(current)
    return [c for c in calls if c["name"]]


def parse_json_calls(text):
    """Extract tool calls from a reply. Fenced blocks win; bare JSON lines are
    only considered outside fences (otherwise one call would be counted twice)."""
    text = text or ""
    candidates = []

    for block in _FENCE_TOOL.findall(text):
        candidates.append(block.strip())
    for block in _TAGGED.findall(text):
        candidates.append(block.strip())

    leftover = _TAGGED.sub("", _FENCE_TOOL.sub("", text))
    for line in leftover.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}") and '"name"' in stripped and "arguments" in stripped:
            candidates.append(stripped)

    calls, seen = [], set()
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            inner = re.search(r"\{.*\}", cand, re.S)
            if not inner:
                continue
            try:
                obj = json.loads(inner.group(0))
            except json.JSONDecodeError:
                continue
        for item in (obj if isinstance(obj, list) else [obj]):
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("tool") or item.get("tool_name")
            if not name:
                continue
            args = item.get("arguments", item.get("args", item.get("parameters", {})))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            args = args or {}
            fingerprint = "%s|%s" % (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            calls.append({"name": name, "arguments": args})
    return calls


# DeepSeek can emit its native tool-call syntax as *text* (DSML) when it wants a tool
# but native calling is unavailable, e.g. <||DSML||invoke name="x">…</||DSML||invoke>.
# The phone app renders that as visible text, so it must be parsed or stripped.
_DSML_TAG = re.compile(r"<\s*(/?)\s*[|｜]{1,2}\s*DSML\s*[|｜]{1,2}\s*([A-Za-z_][\w-]*)", re.I)
_DSML_ANY = re.compile(r"<[^>]*?DSML[^>]*?>|<\s*/?\s*(?:calls|invoke|parameter)\b[^>]*>", re.I)
_ATTR = re.compile(r'([A-Za-z_][\w-]*)\s*=\s*"([^"]*)"|([A-Za-z_][\w-]*)\s*=\s*\'([^\']*)\'')


def _normalize_dsml(text):
    """`<||DSML||invoke name="x">` -> `<invoke name="x">` so it can be parsed as XML-ish.

    Models sometimes emit fullwidth angle brackets/quotes when writing this markup by
    hand, so those are folded back to ASCII first. Only DSML-bearing text reaches here.
    """
    for full, ascii_ in (("＜", "<"), ("＞", ">"), ("＂", '"'), ("’", "'"), ("＇", "'")):
        if full in text:
            text = text.replace(full, ascii_)
    return _DSML_TAG.sub(lambda m: "<%s%s" % (m.group(1), m.group(2)), text)


def _attrs(raw):
    found = {}
    for match in _ATTR.finditer(raw or ""):
        key = match.group(1) or match.group(3)
        value = match.group(2) if match.group(1) else match.group(4)
        found[key.lower()] = value
    return found


def parse_dsml_calls(text):
    """Extract calls from DeepSeek's DSML markup. Returns a list of {name, arguments}.

    Tolerant on purpose: the model often emits truncated markup (unclosed invoke or
    parameter tags) or mixes in prose. A call is still recovered from all of those.
    """
    if not isinstance(text, str) or ("DSML" not in text and "<invoke" not in text and "<parameter" not in text):
        return []
    normalized = _normalize_dsml(text)
    calls = []
    pattern = r"(?ms)<invoke\b([^>]*?)/?>(.*?)(?:</invoke>|\Z)|<invoke\b([^>]*?)/>\s*"
    for block in re.finditer(pattern, normalized):
        attrs = block.group(1) if block.group(1) is not None else block.group(3)
        inner = block.group(2) or ""
        name = _attrs(attrs or "").get("name", "").strip()
        if not name:
            continue
        args = {}
        for param in re.finditer(r"(?ms)<parameter\b([^>]*?)/?>(.*?)(?:</parameter>|\Z)|<parameter\b([^>]*?)/>",
                                 inner):
            head = param.group(1) if param.group(1) is not None else param.group(3)
            key = (_attrs(head or "").get("name") or "").strip()
            if not key:
                continue
            raw = param.group(2) or ""
            value = re.sub(r"</?[^>]+>", "", raw).strip()
            # `string="false"` marks a non-string payload (number/bool/object).
            if (_attrs(head or "").get("string") or "true").lower() == "false":
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    try:
                        value = float(value) if "." in value else int(value)
                    except ValueError:
                        pass
            args[key] = value
        calls.append({"name": name, "arguments": args})
    return calls


def strip_dsml(text):
    """Remove all DSML markup so it can never show up as a visible reply."""
    if not isinstance(text, str) or ("DSML" not in text and "<invoke" not in text and "<parameter" not in text):
        return text
    cleaned = re.sub(r"(?ms)<calls\b.*?</calls>", " ", _normalize_dsml(text))
    cleaned = re.sub(r"(?ms)<invoke\b.*?</invoke>", " ", cleaned)
    cleaned = re.sub(r"(?ms)<invoke\b.*\Z", " ", cleaned)      # truncated markup
    cleaned = _DSML_ANY.sub(" ", cleaned)
    cleaned = re.sub(r"(?i)(?m)^[^\n]*(?:\|\s*){2,}\s*DSML[^\n]*$", " ", cleaned)
    cleaned = re.sub(r"<\s*/?\s*(?:invoke|parameter|calls|DSML)\b[^>]*>?", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return "\n".join(line.rstrip() for line in cleaned.splitlines() if line.strip()).strip()


def parse_tool_calls(text):
    """Line protocol first (robust), JSON/DSML as fallbacks for other models."""
    calls = parse_line_calls(text)
    if calls:
        return calls
    calls = parse_json_calls(text)
    if calls:
        return calls
    return parse_dsml_calls(text)


def _cast(value, kind):
    if kind in (None, "string"):
        return value
    text = str(value).strip()
    if kind == "boolean":
        return text.lower() in ("1", "true", "yes", "y", "on")
    if kind == "integer":
        try:
            return int(float(text))
        except ValueError:
            return value
    if kind == "number":
        try:
            return float(text)
        except ValueError:
            return value
    return value


def coerce_args(name, args):
    """Cast line-protocol strings into the types the tool schema declares.

    Arguments the tool does not declare are dropped instead of being passed through:
    models occasionally invent extra fields, and a stray keyword would otherwise make
    an otherwise fine call fail with TypeError.
    """
    hub = start_hub()
    spec = next((s for s in hub.specs if s["name"] == name), None)
    props = ((spec or {}).get("inputSchema") or {}).get("properties") or {}
    out = {}
    for key, value in (args or {}).items():
        if props and key not in props:
            continue
        declared = props.get(key) or {}
        kind = declared.get("type")
        item_kind = (declared.get("items") or {}).get("type")
        if kind == "array":
            items = value if isinstance(value, list) else [str(value)]
            flat = []
            for item in items:
                if item_kind == "array":
                    flat.extend([part.strip() for part in str(item).replace("|", ";").split(";") if part.strip()])
                    continue
                flat.extend([part.strip() for part in str(item).split("|") if part.strip() != "" or item_kind == "string"])
            if item_kind == "array":
                out[key] = [part.split(",") for part in flat]
            else:
                out[key] = [_cast(part, item_kind) for part in flat]
        else:
            out[key] = _cast(value, kind)
    return out


def strip_tool_blocks(text):
    out = _FENCE_TOOL.sub("", text or "")
    out = _TAGGED.sub("", out)
    return out.strip()


# ------------------------------------------------------------------ upstream
def upstream_chat(messages, temperature=None):
    body = {"model": UP["model"], "messages": messages, "stream": False}
    temp = temperature if temperature is not None else UP.get("temperature")
    if temp is not None:
        body["temperature"] = temp
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        UP["base_url"].rstrip("/") + "/chat/completions", data=data, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + UP.get("api_key", "")},
    )
    try:
        with urllib.request.urlopen(req, timeout=UP.get("timeout_s", 180)) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError("upstream HTTP %s: %s" % (exc.code, detail[:600])) from None
    except Exception as exc:
        raise RuntimeError("upstream error: %s" % exc) from None


def reply_text(payload):
    choices = payload.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return msg.get("content") or ""


def client_turn(messages):
    """Which text to forward upstream in client mode."""
    if CFG["upstream_history"] == "full":
        chunks = []
        for m in messages:
            role = m.get("role", "user")
            if role == "system" and not CFG["include_client_system"]:
                continue
            content = m.get("content")
            if isinstance(content, list):  # OpenAI multimodal parts
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            if content:
                chunks.append("%s: %s" % (role, content))
        return "\n".join(chunks)
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            return content or ""
    return ""


def normalise(messages):
    """Keep the client's roles (proxy mode forwards the App's own persona prompt)."""
    out = []
    for m in messages or []:
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if content:
            out.append({"role": role, "content": content})
    return out


# ----------------------------------------------------------------- tool loop
def run_tool_loop(user_text, temperature=None, history=None):
    """One request in, final text out.

    client mode (upstream_history=last_user_only): forward only the newest user
    message, because the phone endpoint keeps the persona conversation itself.

    proxy mode (upstream_history=full): the client IS the phone App, so its whole
    prompt (persona included) is forwarded to the real model provider, with the
    tool protocol appended to the latest user turn.
    """
    hub = start_hub()
    specs = hub.specs if CFG["always_expose_tools"] else []
    proxy = CFG.get("upstream_history") == "full"

    if not specs:
        convo = normalise(history) if (proxy and history) else [{"role": "user", "content": user_text}]
        return reply_text(upstream_chat(convo, temperature)), []

    if proxy and history:
        convo = normalise(history)
        injected = protocol_block(specs) + "\n\n用户消息:\n"
        for index in range(len(convo) - 1, -1, -1):
            if convo[index]["role"] == "user":
                convo[index]["content"] = injected + convo[index]["content"]
                break
        else:
            convo.append({"role": "user", "content": injected + user_text})
    else:
        convo = [{"role": "user", "content": protocol_block(specs) + "\n\n用户消息:\n" + user_text}]
    steps = []
    text = ""
    for round_no in range(int(CFG["max_tool_rounds"]) + 1):
        payload = upstream_chat(convo, temperature)
        text = reply_text(payload)
        calls = parse_tool_calls(text)
        log("  round %d: reply=%d chars, calls=%d%s" % (
            round_no, len(text), len(calls), "" if calls else " | head=%r" % text[:160]))
        if not calls:
            if _FENCE_TOOL.search(text or "") or _TAGGED.search(text or "") or _TOOL_HEAD.search(text or ""):
                log("  WARN tool block present but unparseable, returning raw text: %r" % text[:600])
                return text.strip(), steps
            return strip_tool_blocks(text), steps
        results = []
        for call in calls:
            arguments = coerce_args(call["name"], call["arguments"])
            log("  tool -> %s %s" % (call["name"], json.dumps(arguments, ensure_ascii=False)[:300]))
            output, is_error = hub.call(call["name"], arguments)
            log("  tool <- %s %s (%d chars)" % (call["name"], "ERROR" if is_error else "ok", len(output)))
            steps.append({"tool": call["name"], "arguments": arguments,
                          "error": is_error, "output": output[:4000]})
            results.append("TOOL_RESULT: %s %s\n%s" % (
                call["name"], "(failed)" if is_error else "(ok)", output[:6000]))
        feedback = "\n\n".join(results)
        if proxy:
            convo.append({"role": "assistant", "content": text})
            convo.append({"role": "user", "content": feedback})
        else:
            convo = [{"role": "user", "content": feedback}]
    # round budget exhausted: answer with whatever we have, blocks stripped
    return strip_tool_blocks(text), steps


# ------------------------------------------------------------------- HTTP API
def openai_response(text, model, steps):
    prompt_chars = sum(len(str(s.get("output", ""))) for s in steps)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:20],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": "persona-mcp-bridge",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "logprobs": None, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": max(1, prompt_chars // 4), "completion_tokens": max(1, len(text) // 4),
                  "total_tokens": max(1, (prompt_chars + len(text)) // 4)},
        "bridge": {"tool_steps": steps, "upstream": UP.get("name", "persona")},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mcp-hands/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the console for our own log()

    # ------------------------------------------------------------------ helpers
    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _sse_chunk(self, text):
        data = text.encode("utf-8")
        self.wfile.write(("%X\r\n" % len(data)).encode("ascii") + data + b"\r\n")

    def _sse_end(self):
        self.wfile.write(b"0\r\n\r\n")

    # ----------------------------------------------------------------- v2 (jobs)
    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8-sig", "replace") or "{}")
        except json.JSONDecodeError:
            return {}

    def _v2_get(self):
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path[len("/v2/"):].strip("/")
        query = urllib.parse.parse_qs(parsed.query)
        store = job_store()
        if route in ("health", ""):
            self._json({"ok": True, "version": APP_VERSION, "uptime_s": int(time.time() - STARTED_AT),
                        "jobs": store.stats(), "metrics": METRICS,
                        "servers": len(HUB.specs) if HUB else 0})
            return
        if route == "jobs":
            if str(query.get("pending", ["0"])[0]) in ("1", "true", "yes"):
                jobs = store.pending_delivery(limit=int(query.get("limit", ["5"])[0]))
                if str(query.get("mark", ["0"])[0]) in ("1", "true", "yes"):
                    store.mark_delivered([j["id"] for j in jobs])
                self._json({"jobs": jobs, "count": len(jobs)})
                return
            status = query.get("status", [""])[0] or None
            limit = int(query.get("limit", ["20"])[0])
            self._json({"jobs": store.list(status=status, limit=limit), "stats": store.stats()})
            return
        if route == "jobs/stats":
            self._json({"stats": store.stats(), "running": list(job_runner().current())})
            return
        if route.startswith("jobs/"):
            job_id = route.split("/", 1)[1]
            wait_s = float(query.get("wait", ["0"])[0] or 0)
            job = job_runner().wait(job_id, wait_s) if wait_s > 0 else store.get(job_id)
            if not job:
                self._json({"error": {"message": "no such job"}}, 404)
                return
            self._json({"job": job})
            return
        self._json({"error": {"message": "not found"}}, 404)

    def _v2_post(self):
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path[len("/v2/"):].strip("/")
        body = self._read_body()
        if route == "jobs":
            jobs_cfg = CFG.get("jobs") or {}
            if jobs_cfg.get("enabled", True) is False:
                self._json({"error": {"message": "jobs are disabled in the config"}}, 409)
                return
            try:
                job = job_runner().submit(tool=body.get("tool", ""), args=body.get("args"),
                                          title=body.get("title", ""), kind=body.get("kind", "task"),
                                          steps=body.get("steps"), plan=body.get("plan"),
                                          notify=body.get("notify", True))
            except ValueError as exc:
                self._json({"error": {"message": str(exc)}}, 400)
                return
            self._json({"job": job}, 201)
            return
        if route.startswith("jobs/") and route.endswith("/cancel"):
            job_id = route.split("/")[1]
            job = job_runner().cancel(job_id)
            if not job:
                self._json({"error": {"message": "no such job"}}, 404)
                return
            self._json({"job": job})
            return
        self._json({"error": {"message": "not found"}}, 404)

    # -------------------------------------------------------------------- routes
    def do_GET(self):
        if self.path.startswith("/v2/"):
            self._v2_get()
            return
        if self.path.startswith("/metrics"):
            runner = job_runner()
            hub = HUB
            store = job_store()
            lines = [
                "# mcp-hands %s" % APP_VERSION,
                "mcp_hands_tools %d" % (len(hub.specs) if hub else 0),
                "mcp_hands_servers %d" % (len(hub.servers) if hub else 0),
                "mcp_hands_jobs_total %d" % store.stats()["total"],
                "mcp_hands_jobs_pending_delivery %d" % store.stats()["pending_delivery"],
                "mcp_hands_jobs_running %d" % len(runner.current()),
            ]
            for key, value in (METRICS or {}).items():
                lines.append("mcp_hands_%s %s" % (key, value))
            body = ("\n".join(lines) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/v1/models"):
            now = int(time.time())
            ids = [UP["model"], UP["model"] + "-tools"]
            self._json({"object": "list", "data": [{"id": i, "object": "model", "created": now, "owned_by": "persona-mcp-bridge"} for i in ids]})
        elif self.path.startswith("/health"):
            hub = start_hub()
            self._json({
                "ok": True,
                "frozen_exe": FROZEN,
                "config": CONFIG_PATH,
                "upstream": {"name": UP.get("name"), "base_url": UP["base_url"], "model": UP["model"]},
                "tool_mode": "text-protocol",
                "servers": [{"name": s.name, "tools": [t["name"] for t in s.tools]} for s in hub.servers],
                "tool_count": len(hub.specs),
                "max_tool_rounds": CFG["max_tool_rounds"],
            })
        else:
            self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        if self.path.startswith("/v2/"):
            self._v2_post()
            return
        if not self.path.startswith("/v1/chat/completions"):
            self._json({"error": {"message": "not found"}}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8-sig", "replace") or "{}")
        except json.JSONDecodeError as exc:
            self._json({"error": {"message": "bad json: %s" % exc}}, 400)
            return

        messages = body.get("messages") or []
        user_text = client_turn(messages)
        stream = bool(body.get("stream"))
        model = body.get("model") or UP["model"]
        log("chat request: %d messages, stream=%s, tools=%d, text=%r" % (
            len(messages), stream, len(body.get("tools") or []), user_text[:120]))

        try:
            text, steps = run_tool_loop(user_text, body.get("temperature"), messages)
        except RuntimeError as exc:
            log("  FAILED: %s" % exc)
            self._json({"error": {"message": str(exc), "type": "upstream_error"}}, 502)
            return

        if not stream:
            self._json(openai_response(text, model, steps))
            return

        self._sse_start()
        cid = "chatcmpl-" + uuid.uuid4().hex[:20]
        created = int(time.time())

        def frame(delta, finish=None):
            obj = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self._sse_chunk("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n")

        frame({"role": "assistant", "content": ""})
        step = 40
        for i in range(0, len(text), step):
            frame({"content": text[i:i + step]})
            time.sleep(0.01)
        frame({}, "stop")
        self._sse_chunk("data: [DONE]\n\n")
        self._sse_end()


# ----------------------------------------------------------------- CLI extras
def cmd_tools():
    from server_host import SERVERS, tool_names
    enabled = {s["name"] for s in CFG.get("servers", []) if s.get("enabled", True) is not False}
    total = 0
    for name in SERVERS:
        if name not in enabled:
            continue
        try:
            names = tool_names(name)
        except Exception as exc:  # noqa: BLE001 - surface the reason and keep listing
            print("  %-10s ERROR %s" % (name, exc))
            continue
        total += len(names)
        print("  %-10s %2d  %s" % (name, len(names), ", ".join(names)))
    print("total: %d tools from %d servers" % (total, len(enabled)))
    return 0


def cmd_selftest():
    hub = start_hub()
    from server_host import load
    checked = skipped = 0
    failures = []
    for server in hub.servers:
        # Import the server module with the same environment its child process gets:
        # several servers derive their sample paths from their configured roots.
        raw = next((s for s in CFG.get("servers", []) if s.get("name") == server.name), {})
        for key, value in (raw.get("env") or {}).items():
            os.environ[key] = os.path.expandvars(str(value))
        os.environ.setdefault("MCP_SAMPLES_DIR", BASE_DIR)
        module = load(server.name)
        samples = getattr(module, "SAMPLES", {}) or {}
        optional = set(getattr(module, "SAMPLES_OPTIONAL", set()) or ())
        if not samples:
            print("  --   %-10s no samples declared" % server.name)
            continue
        for tool in samples:  # declaration order matters: writer samples run before their readers
            exposed = "%s_%s" % (server.name, tool)
            text, is_error = hub.call(exposed, samples[tool])
            first = ((text or "").splitlines() or [""])[0][:300]
            if is_error:
                if tool in optional:
                    skipped += 1
                    print("  SKIP %-36s %s" % (exposed, first))
                else:
                    failures.append(exposed)
                    print("  FAIL %-36s %s" % (exposed, first))
            else:
                checked += 1
                print("  ok   %-36s %s" % (exposed, first))
    print("\nselftest: %d ok, %d failed, %d optional-skipped, %d tools registered"
          % (checked, len(failures), skipped, len(hub.specs)))
    if failures:
        print("failed: %s" % ", ".join(failures))
    return 1 if failures else 0


def cmd_doctor():
    import socket
    print("python       : %s" % sys.version.split()[0])
    print("executable   : %s" % sys.executable)
    print("frozen exe   : %s" % FROZEN)
    print("base dir     : %s" % BASE_DIR)
    print("config       : %s%s" % (CONFIG_PATH, "" if os.path.exists(CONFIG_PATH) else "  (missing - will be created)"))
    print("host / user  : %s / %s" % (socket.gethostname(), os.environ.get("USERNAME", "?")))
    print("upstream     : %s  model=%s  key=%s" % (UP["base_url"], UP["model"], "set" if UP.get("api_key") else "-"))
    try:
        req = urllib.request.Request(UP["base_url"].rstrip("/") + "/models",
                                     headers={"Authorization": "Bearer " + UP.get("api_key", "")})
        with urllib.request.urlopen(req, timeout=10) as resp:
            print("upstream /models: HTTP %s  %s" % (resp.status, resp.read().decode("utf-8", "replace")[:160]))
    except Exception as exc:  # noqa: BLE001
        print("upstream /models: FAILED %s" % exc)
    try:
        reply = (reply_text(upstream_chat([{"role": "user", "content": "reply with the single word: ok"}]) or "").replace("\n", " "))
        print("upstream chat   : %s" % (reply[:200] or "(empty reply)"))
    except RuntimeError as exc:
        print("upstream chat   : FAILED %s" % exc)
    hub = start_hub()
    print("servers      : %d running, %d tools registered" % (len(hub.servers), len(hub.specs)))
    for server in hub.servers:
        note = "" if not server.log else "  (log: %s)" % server.log[-1][:80]
        print("  %-10s %2d tools%s" % (server.name, len(server.tools), note))
    return 0


def cmd_jobs(limit=20):
    """--jobs: list the background queue; --jobs-run <id>: run one now (blocking)."""
    store = job_store()
    stats = store.stats()
    print("jobs: total=%d %s pending_delivery=%d db=%s"
          % (stats["total"], stats["by_status"] or {}, stats["pending_delivery"], store.path))
    for job in store.list(limit=limit):
        print("  %s [%-9s] %s/%s %s" % (job["id"], job["status"], job["progress"], job["total"],
                                        (job["title"] or job["kind"])[:60]))
        if job["error"]:
            print("      error: %s" % job["error"][:120])
    return 0


def cmd_jobs_run(job_id):
    job = job_runner().run_job(job_id)
    if not job:
        print("no such job: %s" % job_id)
        return 1
    print("%s -> %s" % (job["id"], job["status"]))
    print((job["result"] or job["error"] or "")[:4000])
    return 0 if job["status"] == "done" else 1


def main():
    if "--version" in ARGV or "-V" in ARGV:
        raise SystemExit(cmd_version())
    if "--tools" in ARGV:
        raise SystemExit(cmd_tools())
    if "--doctor" in ARGV:
        raise SystemExit(cmd_doctor())
    if "--self-test" in ARGV:
        raise SystemExit(cmd_selftest())
    if "--migrate" in ARGV:
        print(migrate_config())
        raise SystemExit(0)
    if "--jobs" in ARGV:
        raise SystemExit(cmd_jobs(int(_opt("--limit") or 20)))
    if "--jobs-run" in ARGV:
        raise SystemExit(cmd_jobs_run(_opt("--jobs-run")))
    if "--init" in ARGV:
        print("config written: %s" % write_default_config(_opt("--config")))
        raise SystemExit(0)

    host = CFG["listen"]["host"]
    port = int(_opt("--port") or CFG["listen"]["port"])
    hub = start_hub()
    runner = job_runner()
    log("=" * 62)
    log("%s" % version_line())
    log("mode     : %s" % ("packed exe" if FROZEN else "source"))
    log("listen   : http://%s:%d/v1" % (host, port))
    log("upstream : %s (%s, model=%s)" % (UP.get("name"), UP["base_url"], UP["model"]))
    log("tools    : %d from %d MCP servers" % (len(hub.specs), len(hub.servers)))
    log("jobs     : %s worker(s), queued=%d" % (runner.workers, job_store().stats()["by_status"].get("pending", 0)))
    log("config   : %s" % CONFIG_PATH)
    log("=" * 62)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_jobs()
        hub.stop()


if __name__ == "__main__":
    main()
