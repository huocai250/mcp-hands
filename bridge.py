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
import hmac
import json
import os
import re
import secrets
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
APP_VERSION = "4.1.1"
APP_AUTHOR = "huocai250"
APP_URL = "https://github.com/huocai250/mcp-hands"
APP_REPO = "huocai250/mcp-hands"
APP_LICENSE = "MIT"
APP_TAGLINE = "把手机里的人设接上电脑的 380 个工具：会看图、能出声、能干长活、能被审计"


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

from audit import AuditStore, Policy  # noqa: E402
from devices import DeviceStore  # noqa: E402
from jobs import JobRunner, JobStore  # noqa: E402
from mcp_client import ToolHub  # noqa: E402
from outbox import Outbox  # noqa: E402
from plans import PlanStore  # noqa: E402
from voice import VoiceQueue  # noqa: E402


#: Flags whose *next* token is a value, not a file to talk to.
VALUE_FLAGS = ("--config", "--limit", "--backup", "--restore", "--devices-approve",
               "--devices-revoke", "--jobs-run", "--name", "--port", "--mcp-server")


def _opt(name, default=None):
    if name in ARGV:
        index = ARGV.index(name)
        if index + 1 < len(ARGV):
            return ARGV[index + 1]
    return default


def _positional_args():
    """Bare arguments only.

    Bug fix (4.0.1): this used to be "the first argument that is not a flag", which
    meant the *value* of any flag became the config path - `--backup D:\\x.zip`,
    `--limit 20`, `--devices-approve pend_x` all elected a bogus config and then wrote
    a default config to that path.
    """
    out = []
    skip_next = False
    for token in ARGV[1:]:
        if skip_next:
            skip_next = False
            continue
        if token in VALUE_FLAGS:
            skip_next = True
            continue
        if token.startswith("--"):
            continue
        out.append(token)
    return out


LOG_LOCK = threading.Lock()
LOG_FILE = os.path.join(BASE_DIR, "bridge.log")
LOG_SINKS = []

# The 3.0 dashboard: one self-contained page, no external assets, no build step.
DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>mcp-hands __VERSION__ · 控制台</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{margin:0;background:#0f1115;color:#e6e6e6;font:14px/1.6 "Segoe UI",system-ui,sans-serif}
 header{padding:16px 20px;border-bottom:1px solid #23262e;display:flex;gap:14px;align-items:baseline}
 h1{font-size:17px;margin:0;font-weight:600}
 .dim{color:#8b93a1;font-size:12px}
 main{padding:16px 20px;display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}
 section{background:#151922;border:1px solid #23262e;border-radius:10px;padding:12px 14px}
 h2{font-size:13px;margin:0 0 8px;color:#9fb3d1;font-weight:600;letter-spacing:.04em}
 table{width:100%;border-collapse:collapse;font-size:12.5px}
 td,th{text-align:left;padding:3px 6px;border-bottom:1px solid #1d2129;vertical-align:top}
 th{color:#8b93a1;font-weight:500}
 .ok{color:#5ad07a}.bad{color:#ff7a7a}.todo{color:#8b93a1}.doing{color:#ffcf6b}
 pre{margin:0;white-space:pre-wrap;word-break:break-all;font-size:12px;color:#c9d1d9}
</style></head><body>
<header><h1>mcp-hands __VERSION__</h1><span class="dim" id="status">连接中…</span>
<span class="dim">bridge __BRIDGE__</span></header>
<main>
 <section><h2>概览</h2><div id="overview"></div></section>
 <section><h2>设备与配对</h2><div id="devices"></div></section>
 <section><h2>媒体出口（未打开的项目）</h2><div id="outbox"></div></section>
 <section><h2>后台任务</h2><div id="jobs"></div></section>
 <section><h2>计划</h2><div id="plans"></div></section>
 <section><h2>最近工具调用（审计）</h2><div id="audit"></div></section>
</main>
<script>
const API="__BRIDGE__";
const TOKEN="__TOKEN__";
function headers(extra){const h=Object.assign({"Content-Type":"application/json"},extra||{});if(TOKEN){h["X-Control-Token"]=TOKEN;}return h;}
async function j(path){const r=await fetch(API+path,{headers:headers()});return r.ok?r.json():{error:r.status};}
function esc(s){return String(s==null?"":s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function kv(o){return Object.entries(o).map(([k,v])=>`<tr><th>${esc(k)}</th><td>${esc(typeof v==="object"?JSON.stringify(v):v)}</td></tr>`).join("");}
async function tick(){
 try{
  const h=await j("/v2/health");
  document.getElementById("status").textContent="在线 · 运行 "+(h.uptime_s||0)+"s";
  document.getElementById("overview").innerHTML="<table>"+kv({
    "版本":h.version,"工具数":h.metrics&&h.metrics.tool_calls_total!==undefined?h.servers:h.servers,
    "服务数":h.servers,"任务":h.jobs&&h.jobs.by_status,"计划":h.plans&&h.plans.by_status,
    "审计总条数":h.audit&&h.audit.total,"策略模式":h.policy&&h.policy.mode,
    "请求 (prompt/completion tokens)":h.metrics?h.metrics.prompt_tokens_total+"/"+h.metrics.completion_tokens_total:"-"})+"</table>";
  const jobs=await j("/v2/jobs?limit=8");
  document.getElementById("jobs").innerHTML="<table><tr><th>id</th><th>状态</th><th>进度</th><th>标题</th></tr>"+
    (jobs.jobs||[]).map(x=>`<tr><td>${esc(x.id)}</td><td class="${x.status==='done'?'ok':x.status==='failed'?'bad':''}">${esc(x.status)}</td><td>${x.progress}/${x.total}</td><td>${esc(x.title)}</td></tr>`).join("")+"</table>";
  const plans=await j("/v2/plans?limit=8");
  document.getElementById("plans").innerHTML="<table><tr><th>id</th><th>状态</th><th>进度</th><th>目标</th></tr>"+
    (plans.plans||[]).map(p=>`<tr><td>${esc(p.id)}</td><td class="${p.status==='active'?'doing':'ok'}">${esc(p.status)}</td><td>${p.done}/${p.total}</td><td>${esc(p.goal)}</td></tr>`).join("")+"</table>";
  const audit=await j("/v2/audit?limit=12");
  document.getElementById("audit").innerHTML="<table><tr><th>时间</th><th>工具</th><th>结果</th><th>参数</th></tr>"+
    (audit.calls||[]).map(c=>`<tr><td class="dim">${esc((c.when||"").slice(11))}</td><td>${esc(c.tool)}</td><td class="${c.ok?'ok':'bad'}">${c.ok?'ok':'失败 '+(c.ms||0)+'ms'}</td><td><pre>${esc(JSON.stringify(c.args)).slice(0,120)}</pre></td></tr>`).join("")+"</table>";
  const dev=await j("/v2/devices?stats=1");
  let dh="<table><tr><th>状态</th><th>id</th><th>名称/密钥</th><th>最近</th><th>调用</th><th>操作</th></tr>";
  dh+=(dev.devices||[]).map(d=>`<tr><td class="ok">已批准</td><td>${esc(d.id)}</td><td>${esc(d.name)} <span class="dim">${esc(d.masked||'')}</span></td><td class="dim">${d.last_seen?new Date(d.last_seen*1000).toLocaleTimeString():'-'}</td><td>${d.calls||0}</td><td><button onclick="act('revoke','${d.id}')">撤销</button></td></tr>`).join("");
  dh+=(dev.pending||[]).map(p=>`<tr><td class="bad">待批准</td><td>${esc(p.id)}</td><td>${esc(p.masked||'')} <span class="dim">${esc(p.ip||'')}</span></td><td class="dim">${p.seen||1} 次尝试</td><td>-</td><td><button onclick="act('approve','${p.id}')">批准</button> <button onclick="act('reject','${p.id}')">拒绝</button></td></tr>`).join("");
  document.getElementById("devices").innerHTML=dh+"</table><div class='dim'>mode="+esc(dev.mode)+"（off=不校验，allowlist=只允许已批准设备）</div>";
  const box=await j("/v2/outbox?limit=8");
  document.getElementById("outbox").innerHTML="<table><tr><th>id</th><th>类型</th><th>名称</th><th>大小</th><th>取用</th><th>链接</th></tr>"+
    (box.items||[]).map(o=>`<tr><td>${esc(o.id)}</td><td>${esc(o.kind)}</td><td>${esc(o.name)}</td><td>${o.bytes}</td><td>${o.fetches||0}</td><td><a href="${esc(o.url)}" target="_blank">打开</a></td></tr>`).join("")+"</table>";
 }catch(e){document.getElementById("status").textContent="离线（bridge 未运行？）";}
}
async function act(action,id){
  await fetch(API+"/v2/devices/"+action,{method:"POST",headers:headers(),body:JSON.stringify({id:id})});
  tick();
}
tick();setInterval(tick,3000);
</script></body></html>"""


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
    "outbox": {"dir": "", "ttl": 3600, "max_items": 200, "secret": ""},
    "devices": {"mode": "off", "path": "", "salt": ""},
    "voice": {"path": "", "max_lines": 200},
    "control": {"token": "", "require_for_lan": True},
    "plans": {"db": ""},
    "audit": {"enabled": True, "db": "", "max_rows": 20000},
    "policy": {"mode": "audit", "deny": [], "allow": [], "deny_paths": [],
               "max_calls_per_minute": 0, "exempt": ["audit_*", "policy_*", "jobs_*", "plan_*"]},
    "log": {"format": "text", "max_mb": 8},
    "profiles": {},
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
        {"name": "plan", "enabled": True},
        {"name": "audit", "enabled": True},
        {"name": "send", "enabled": True},
        {"name": "device", "enabled": True},
        {"name": "call", "enabled": True},
    ],
}

_elected = _opt("--config") or next(iter(_positional_args()), None)
CONFIG_PATH = os.environ.get("BRIDGE_CONFIG") or _elected or os.path.join(BASE_DIR, "bridge.config.json")


def write_default_config(path=None):
    """Write a starter config - but never onto something that is clearly not a config.

    4.0.1 guard: a mistyped CLI value (e.g. `--backup D:\\x.zip`) must not turn into a
    config file written over the user's file.
    """
    path = path or CONFIG_PATH
    if os.path.splitext(path)[1].lower() not in ("", ".json"):
        raise SystemExit("refusing to write a config to %r (looks like a %s, not a .json config)"
                         % (path, os.path.splitext(path)[1] or "file"))
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        raise SystemExit("refusing to write a config into %r (no such folder)" % parent)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(DEFAULT_CONFIG, fh, ensure_ascii=False, indent=2)
    return path


def _rotate_log():
    """Keep the log small: bridge.log -> bridge.log.1 once it passes log.max_mb.

    Uses globals() because log() can legitimately run while the config module-level
    assignment is still in flight (a missing config logs a line).
    """
    cfg = globals().get("CFG") or {}
    limit_mb = float((cfg.get("log") or {}).get("max_mb", 8) or 0)
    if limit_mb <= 0:
        return
    try:
        if os.path.getsize(LOG_FILE) < limit_mb * 1024 * 1024:
            return
        backup = LOG_FILE + ".1"
        if os.path.exists(backup):
            os.remove(backup)
        os.replace(LOG_FILE, backup)
    except OSError:
        pass


def log(msg):
    """Text (default) or one-JSON-object-per-line output, plus the log file."""
    fmt = str((globals().get("CFG") or {}).get("log", {}).get("format", "text") or "text").lower()
    stamp = time.strftime("%H:%M:%S")
    line = "[%s] %s" % (stamp, msg)
    with LOG_LOCK:
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001 - windowed builds may have no stdout
            pass
        try:
            if fmt == "json":
                record = json.dumps({"ts": time.time(), "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                     "level": "error" if "ERROR" in str(msg).upper() else "info",
                                     "message": msg, "version": APP_VERSION}, ensure_ascii=False)
                _rotate_log()
                with open(LOG_FILE, "a", encoding="utf-8") as fh:
                    fh.write(record + "\n")
            else:
                _rotate_log()
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
    cfg.setdefault("plans", {}).setdefault("db", "")
    audit = cfg.setdefault("audit", {})
    audit.setdefault("enabled", True)
    audit.setdefault("db", "")
    audit.setdefault("max_rows", 20000)
    pol = cfg.setdefault("policy", {})
    pol.setdefault("mode", "audit")
    pol.setdefault("deny", [])
    pol.setdefault("allow", [])
    pol.setdefault("deny_paths", [])
    pol.setdefault("max_calls_per_minute", 0)
    pol.setdefault("exempt", ["audit_*", "policy_*", "jobs_*", "plan_*"])
    log_cfg = cfg.setdefault("log", {})
    log_cfg.setdefault("format", "text")
    log_cfg.setdefault("max_mb", 8)
    cfg.setdefault("profiles", {})
    cfg.setdefault("outbox", {})
    cfg["outbox"].setdefault("ttl", 3600)
    cfg["outbox"].setdefault("max_items", 200)
    cfg.setdefault("devices", {}).setdefault("mode", "off")
    cfg.setdefault("voice", {}).setdefault("max_lines", 200)
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
    "plans.db": "",
    "audit.enabled": True,
    "audit.db": "",
    "audit.max_rows": 20000,
    "policy.mode": "audit",
    "policy.deny": [],
    "policy.allow": [],
    "policy.deny_paths": [],
    "policy.max_calls_per_minute": 0,
    "log.format": "text",
    "log.max_mb": 8,
    "outbox.ttl": 3600,
    "outbox.max_items": 200,
    "devices.mode": "off",
    "voice.max_lines": 200,
    "control.token": "",
    "control.require_for_lan": True,
}
NEW_SERVERS = ("jobs", "memory", "plan", "audit", "send", "device", "call")


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
_AUDIT = None
_POLICY = None
_PLANS = None
_OUTBOX = None
_DEVICES = None
_VOICE = None


def audit_store():
    """3.0: every tool call can be recorded; answers 'what did it actually do'."""
    global _AUDIT
    if _AUDIT is None:
        cfg = CFG.get("audit") or {}
        _AUDIT = AuditStore(cfg.get("db") or os.path.join(BASE_DIR, "audit.db"),
                            max_rows=int(cfg.get("max_rows", 20000) or 20000))
    return _AUDIT


def policy():
    global _POLICY
    if _POLICY is None:
        _POLICY = Policy(CFG.get("policy") or {})
    return _POLICY


def plan_store():
    """3.0: goals split into steps, stored so they survive turns and restarts."""
    global _PLANS
    if _PLANS is None:
        cfg = CFG.get("plans") or {}
        _PLANS = PlanStore(cfg.get("db") or os.path.join(BASE_DIR, "plans.db"))
    return _PLANS


def outbox():
    """4.0: signed, expiring links so the persona can actually hand things to the phone."""
    global _OUTBOX
    if _OUTBOX is None:
        cfg = CFG.get("outbox") or {}
        root = cfg.get("dir") or os.path.join(BASE_DIR, "outbox")
        _OUTBOX = Outbox(root, secret=cfg.get("secret") or "mcp-hands-%s" % os.path.basename(BASE_DIR),
                         default_ttl=cfg.get("ttl") or 3600,
                         max_items=cfg.get("max_items") or 200)
        removed = _OUTBOX.purge()
        if removed:
            log("outbox: cleaned %d expired item(s)" % removed)
    return _OUTBOX


def device_store():
    """4.0: which devices may drive this PC (mode=off keeps the old open behaviour)."""
    global _DEVICES
    if _DEVICES is None:
        cfg = CFG.get("devices") or {}
        _DEVICES = DeviceStore(cfg.get("path") or os.path.join(BASE_DIR, "devices.json"),
                               salt=cfg.get("salt") or "")
    return _DEVICES


def devices_mode():
    return str((CFG.get("devices") or {}).get("mode") or "off").lower()


def control_required():
    """Control API protection is needed whenever the bridge is not loopback-only."""
    cfg = CFG.get("control") or {}
    if cfg.get("require_for_lan", True) is False:
        return False
    host = str((CFG.get("listen") or {}).get("host", "127.0.0.1")).lower()
    return host not in ("127.0.0.1", "localhost", "::1", "")


def control_token(create=True):
    """A stable token for the control API (/v2/*, /dashboard) when exposed to the LAN."""
    cfg = CFG.setdefault("control", {})
    token = str(cfg.get("token") or "")
    if token or not create:
        return token
    token = secrets.token_urlsafe(18)
    cfg["token"] = token
    try:                                   # keep the dashboard URL stable across restarts
        with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
            raw = json.load(fh)
        raw.setdefault("control", {})["token"] = token
        with open(CONFIG_PATH, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return token


def control_ok(handler):
    """True when the caller may use the control API."""
    if not control_required():
        return True
    expected = control_token()
    given = handler.headers.get("X-Control-Token", "")
    if not given:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
        given = (query.get("token") or [""])[0]
    return bool(given) and hmac.compare_digest(str(given), str(expected))


def _guard_tool(name, args):
    return policy().check(name, args)


_ACTIVE = threading.local()


def set_active_profile(name):
    """Which identity this thread is serving, used to tag audit rows."""
    _ACTIVE.profile = str(name or "")


def active_profile():
    return getattr(_ACTIVE, "profile", "")


def _audit_tool(record, profile="", source="chat"):
    cfg = CFG.get("audit") or {}
    if cfg.get("enabled", True) is False:
        return
    record = dict(record)
    record["profile"] = profile or record.get("profile") or active_profile()
    record["source"] = source
    audit_store().record(**record)


def profile_names():
    return sorted((CFG.get("profiles") or {}).keys())


def profile_config(name):
    """One named profile: its own upstream/model/tool rules (3.0 multi-persona)."""
    profiles = CFG.get("profiles") or {}
    if not profiles:
        return {}
    key = str(name or "").strip()
    if key and key in profiles:
        return dict(profiles[key] or {})
    default = CFG.get("profile_default") or next(iter(profiles.values()), {})
    return dict(default or {})


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
                      extra_env=extra_env, guard=_guard_tool, audit=_audit_tool)
        pol = policy().describe()
        log("policy   : mode=%s deny=%s allow=%s" % (pol["mode"], pol["deny"] or "-", pol["allow"] or "all"))
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
    if name in ("audit", "--audit"):
        return cmd_audit(int(_opt("--limit") or 30))
    if name in ("plans", "--plans"):
        return cmd_plans(int(_opt("--limit") or 20))
    if name in ("devices", "--devices"):
        return cmd_devices(int(_opt("--limit") or 30))
    if name in ("devices-approve", "--devices-approve"):
        return cmd_devices_approve(_opt("--devices-approve") or "", _opt("--name") or "")
    if name in ("devices-revoke", "--devices-revoke"):
        return cmd_devices_revoke(_opt("--devices-revoke") or "")
    if name in ("outbox", "--outbox"):
        return cmd_outbox(bool(_opt("--purge")), int(_opt("--limit") or 20))
    if name in ("backup", "--backup"):
        return cmd_backup(_opt("--backup") or "")
    if name in ("restore", "--restore"):
        return cmd_restore(_opt("--restore") or "")
    if name in ("check-update", "--check-update"):
        return cmd_check_update()
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


def voice_queue():
    """4.1: the voice-call channel (outbound lines to speak, inbound speech to read)."""
    global _VOICE
    if _VOICE is None:
        cfg = CFG.get("voice") or {}
        _VOICE = VoiceQueue(cfg.get("path") or os.path.join(BASE_DIR, "voice.json"),
                            max_lines=cfg.get("max_lines") or 200)
    return _VOICE


def lan_ips():
    """Plausible LAN IPv4 addresses, best first (private ranges beat virtual adapters)."""
    import socket

    def score(ip):
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip):
            return 2
        if ip.startswith("127.") or ip.startswith("169.254."):
            return 9
        if ip.startswith("198.18.") or ip.startswith("198.19."):   # benchmarking range: virtual
            return 8
        return 5

    found = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    seen, ordered = set(), []
    for ip in sorted(found, key=score):
        if ip and ip not in seen:
            seen.add(ip)
            ordered.append(ip)
    return ordered or ["127.0.0.1"]


def lan_ip():
    """Best-effort LAN address of this machine (used for phone-reachable URLs)."""
    return lan_ips()[0]


_LAST_HOST = {"host": "", "at": 0.0}


def note_client_host(host):
    """Remember which host:port a client reached us on - the most reliable base URL."""
    text = str(host or "").strip()
    if not text or text.startswith("127.0.0.1") or text.startswith("localhost"):
        return
    _LAST_HOST["host"] = text
    _LAST_HOST["at"] = time.time()


def client_host(fresh_seconds=300):
    if not _LAST_HOST["host"]:
        return ""
    if time.time() - _LAST_HOST["at"] > float(fresh_seconds):
        return ""
    return _LAST_HOST["host"]


def proxy_public_base():
    """The URL the phone should use for pages served by the proxy (voice, outbox)."""
    seen = client_host()
    if seen:
        return "http://%s" % seen
    proxy = CFG.get("proxy") or {}
    return "http://%s:%s" % (lan_ip(), (proxy.get("listen") or {}).get("port", 8890))


def voice_api(handler, method):
    """The voice endpoints, shared by the bridge and by the phone-facing proxy."""
    if not control_ok(handler):
        handler._json({"error": {"message": "voice API needs the X-Control-Token header (or ?token=…)",
                                 "type": "control_token_required"}}, 401)
        return
    parsed = urllib.parse.urlsplit(handler.path)
    query = urllib.parse.parse_qs(parsed.query)
    tail = parsed.path[len("/v2/voice"):].strip("/")
    body = {}
    if method != "GET":
        try:
            raw = handler.rfile.read(int(handler.headers.get("Content-Length") or 0)) or b"{}"
            body = json.loads(raw.decode("utf-8", "replace") or "{}")
        except (ValueError, json.JSONDecodeError):
            body = {}
    if method == "GET" or not tail:
        lines = voice_queue().pending(since=int(query.get("since", ["0"])[0]),
                                      kind=query.get("kind", ["say"])[0])
        handler._json({"lines": lines, "stats": voice_queue().stats()})
        return
    if tail == "say":
        line = voice_queue().say(body.get("text", ""), voice=body.get("voice", ""),
                                 rate=body.get("rate", 1.0), interrupt=body.get("interrupt", False))
        handler._json({"line": line, "stats": voice_queue().stats()})
        return
    if tail == "heard":
        line = voice_queue().heard(body.get("text", ""), source=body.get("source", "phone"))
        log("voice: the user said %r" % (body.get("text", "")[:80]))
        handler._json({"line": line})
        return
    if tail == "spoken":
        handler._json({"ok": voice_queue().mark_spoken(body.get("seq", 0)),
                       "stats": voice_queue().stats()})
        return
    if tail == "read":
        handler._json({"ok": voice_queue().mark_read(body.get("seq", 0)),
                       "stats": voice_queue().stats()})
        return
    if tail == "call":
        voice_queue().call_event(body.get("event", ""), body.get("note", ""))
        handler._json({"stats": voice_queue().stats()})
        return
    if tail == "clear":
        handler._json({"cleared": voice_queue().clear()})
        return
    handler._json({"error": {"message": "unknown voice action"}}, 404)


def serve_voice_page(handler):
    if not control_ok(handler):
        handler._json({"error": {"message": "voice page needs ?token=… (see the bridge log), "
                                            "because the bridge is reachable on the LAN",
                                 "type": "control_token_required"}}, 401)
        return
    body = VOICE_HTML.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def serve_outbox(handler, token):
    """Serve one signed outbox item; shared by the bridge and the proxy handlers."""
    record, target = outbox().resolve(urllib.parse.unquote(token or ""))
    if not record:
        handler._json({"error": {"message": "outbox item unavailable: %s" % target}}, 404)
        return
    types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".webp": "image/webp", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
             ".ogg": "audio/ogg", ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
             ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8",
             ".zip": "application/zip", ".7z": "application/x-7z-compressed",
             ".pdf": "application/pdf", ".json": "application/json"}
    try:
        with open(target, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        handler._json({"error": {"message": "outbox read failed: %s" % exc}}, 500)
        return
    ext = os.path.splitext(target)[1].lower()
    served = outbox().touch(record["id"]) or record
    log("outbox delivered %s (%s, %d bytes) - fetch #%s" % (record["id"], record.get("name"),
                                                            len(blob), served.get("fetches")))
    handler.send_response(200)
    handler.send_header("Content-Type", types.get(ext, "application/octet-stream"))
    handler.send_header("Content-Length", str(len(blob)))
    handler.send_header("Content-Disposition", 'inline; filename="%s"' % os.path.basename(target))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(blob)


VOICE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>mcp-hands 语音通话</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{margin:0;background:#0f1115;color:#e6e6e6;font:15px/1.6 "Segoe UI",system-ui,sans-serif;display:flex;flex-direction:column;height:100vh}
 header{padding:12px 16px;border-bottom:1px solid #23262e;display:flex;gap:10px;align-items:center}
 h1{font-size:16px;margin:0;flex:1}
 .dim{color:#8b93a1;font-size:12px}
 main{flex:1;overflow:auto;padding:12px 16px;display:flex;flex-direction:column;gap:8px}
 .line{max-width:82%;padding:8px 12px;border-radius:12px;background:#1b2130;white-space:pre-wrap}
 .mine{align-self:flex-end;background:#3a2b3f}
 .dimline{opacity:.7;font-size:13px;background:none;padding:2px 0}
 footer{padding:10px 12px;border-top:1px solid #23262e;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 button{background:#2b3245;color:#e6e6e6;border:1px solid #3a4256;border-radius:10px;padding:10px 14px;font-size:15px}
 button.rec{background:#5c1f2b;border-color:#8a2f3d}
 input{flex:1;min-width:140px;background:#151922;color:#e6e6e6;border:1px solid #2b3245;border-radius:10px;padding:10px}
 #status{font-size:12px;color:#8b93a1}
</style></head><body>
<header><h1>语音通话</h1><span id="status" class="dim">未开始</span></header>
<main id="log"></main>
<footer>
  <button id="start">开始通话</button>
  <button id="talk">按住说话</button>
  <input id="text" placeholder="也可以打字，回车发给她">
  <button id="stop">停止朗读</button>
</footer>
<script>
const API=(location.protocol==="http:"?location.origin:"http://127.0.0.1:8890");
const TOKEN=new URLSearchParams(location.search).get("token")||"";
const H={"Content-Type":"application/json"};
if(TOKEN){H["X-Control-Token"]=TOKEN;}
let since=0, started=false, speaking=false, rec=null, mode="";
const log=document.getElementById("log"), status=document.getElementById("status");
function add(text,cls){const d=document.createElement("div");d.className="line "+(cls||"");d.textContent=text;log.appendChild(d);log.scrollTop=log.scrollHeight;}
async function api(path,body){const o={method:body?"POST":"GET",headers:H};if(body){o.body=JSON.stringify(body);}try{const r=await fetch(API+path,o);return r.ok?await r.json():{};}catch(e){return {};}}
function pickVoice(){const vs=speechSynthesis.getVoices()||[];return vs.find(v=>/zh[-_]?CN/i.test(v.lang))||vs.find(v=>/^zh/i.test(v.lang))||null;}
function speak(text,rate){
  if(!("speechSynthesis" in window)){add("（这台设备不支持朗读）","dimline");return;}
  const u=new SpeechSynthesisUtterance(text);const v=pickVoice();
  if(v){u.voice=v;}u.lang=(v&&v.lang)||"zh-CN";u.rate=rate||1;
  u.onstart=()=>{speaking=true;};u.onend=()=>{speaking=false;};
  speechSynthesis.speak(u);
}
async function tick(){
  if(!started){return;}
  const data=await api("/v2/voice/next?since="+since);
  (data.lines||[]).forEach(l=>{
    since=Math.max(since,l.seq||0);
    if(l.interrupt&&window.speechSynthesis){speechSynthesis.cancel();}
    add("她说："+l.text,"");
    speak(l.text,l.rate);
    api("/v2/voice/spoken",{seq:l.seq});
  });
  status.textContent="通话中 · 已收 "+(data.lines||[]).length+" 句"+(speaking?" · 正在说":"");
}
function listen(){
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){add("（这台设备不支持语音识别，可以打字）","dimline");return;}
  rec=new SR();rec.lang="zh-CN";rec.continuous=true;rec.interimResults=false;
  rec.onresult=e=>{const r=e.results[e.results.length-1];if(r.isFinal){const t=r[0].transcript.trim();if(t){add("我说："+t,"mine");api("/v2/voice/heard",{text:t});}}};
  rec.onerror=e=>{add("（识别出错："+e.error+"）","dimline");};
  rec.onend=()=>{if(mode==="listen"){try{rec.start();}catch(e){}}};
  try{rec.start();mode="listen";}catch(e){}
}
document.getElementById("start").onclick=async()=>{
  started=true;
  if(window.speechSynthesis){speechSynthesis.getVoices();}
  speak("通话已接通",1);
  await api("/v2/voice/call",{event:"start"});
  add("（已接通：她想说话时会自动念出来；点「按住说话」可以对她讲）","dimline");
};
document.getElementById("talk").onclick=()=>{
  if(mode==="listen"){mode="";try{rec.stop();}catch(e){}const b=document.getElementById("talk");b.className="";add("（停止收音）","dimline");}
  else{document.getElementById("talk").className="rec";add("（开始收音，再点一次停止）","dimline");listen();}
};
document.getElementById("stop").onclick=()=>{if(window.speechSynthesis){speechSynthesis.cancel();}};
document.getElementById("text").onkeydown=e=>{
  if(e.key==="Enter"&&e.target.value.trim()){const t=e.target.value.trim();e.target.value="";add("我说："+t,"mine");api("/v2/voice/heard",{text:t});}
};
setInterval(tick,2000);tick();
</script></body></html>
"""


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

    def _serve_outbox(self, token):
        """Serve one signed, expiring outbox item (this is how the phone gets media)."""
        serve_outbox(self, token)

    def _dashboard(self):
        """A single self-contained page: servers, jobs, plans, audit, metrics (3.0)."""
        if not control_ok(self):
            hint = ("控制台需要令牌：请用启动日志里带 ?token=… 的地址打开"
                    if "zh" != "en" else "dashboard needs a token")
            body = ("<meta charset='utf-8'><body style='background:#0f1115;color:#e6e6e6;"
                    "font:14px system-ui;padding:24px'>控制台需要令牌：请用启动日志里带 "
                    "<code>?token=…</code> 的地址访问。<br><br>"
                    "%s</body>" % hint).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        html = DASHBOARD_HTML.replace("__VERSION__", APP_VERSION).replace(
            "__BRIDGE__", "http://127.0.0.1:%s" % (CFG.get("listen") or {}).get("port", 8877)).replace(
            "__TOKEN__", control_token() if control_required() else "")
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        if route not in ("health", "") and not control_ok(self):
            self._json({"error": {"message": "control API needs ?token=… (see the bridge log / config "
                                             "control.token) because the bridge is reachable on the LAN",
                                  "type": "control_token_required"}}, 401)
            return
        store = job_store()
        if route in ("health", ""):
            fs_entry = next((s for s in (CFG.get("servers") or []) if s.get("name") == "fs"), {})
            self._json({"ok": True, "version": APP_VERSION, "uptime_s": int(time.time() - STARTED_AT),
                        "jobs": store.stats(), "plans": plan_store().stats(),
                        "audit": audit_store().stats(), "metrics": METRICS,
                        "policy": policy().describe(),
                        "outbox": outbox().stats(),
                        "voice": voice_queue().stats(),
                        "fs_roots": str((fs_entry.get("env") or {}).get("MCP_FS_ROOTS") or "(user home)"),
                        "devices": dict(device_store().stats(), mode=devices_mode()),
                        "profiles": profile_names(),
                        "servers": len(HUB.specs) if HUB else 0})
            return
        if route == "profiles":
            self._json({"profiles": [{"name": name, "config": profile_config(name)} for name in profile_names()]})
            return
        if route == "devices":
            store = device_store()
            pending_only = str(query.get("pending", ["0"])[0]) in ("1", "true", "yes")
            payload = {"mode": devices_mode(), "stats": store.stats()}
            if pending_only:
                payload["pending"] = store.pending()
            else:
                payload["devices"] = store.devices()
                if str(query.get("stats", ["0"])[0]) in ("1", "true", "yes"):
                    payload["pending"] = store.pending()
            self._json(payload)
            return
        if route == "voice":
            lines = voice_queue().pending(since=int(query.get("since", ["0"])[0]),
                                          kind=query.get("kind", ["say"])[0])
            self._json({"lines": lines, "stats": voice_queue().stats()})
            return
        if route == "outbox":
            items = outbox().list(limit=int(query.get("limit", ["20"])[0]))
            base = self._outbox_base()
            for item in items:
                item["url"] = "%s/out/%s" % (base, item["token"])
            self._json({"items": items, "stats": outbox().stats(), "base": base})
            return
        if route == "profile_policy":
            self._json({"policy": policy().describe()})
            return
        if route == "policy":
            self._json({"policy": policy().describe()})
            return
        if route == "audit":
            rows = audit_store().tail(limit=int(query.get("limit", ["50"])[0]),
                                      tool=query.get("tool", [""])[0],
                                      profile=query.get("profile", [""])[0],
                                      only_errors=str(query.get("only_errors", ["0"])[0]) in ("1", "true", "yes"))
            self._json({"calls": rows, "count": len(rows), "stats": audit_store().stats()})
            return
        if route == "audit/stats":
            self._json({"stats": audit_store().stats(since_seconds=float(query.get("hours", ["24"])[0]) * 3600)})
            return
        if route == "plans":
            plans = plan_store().list(status=query.get("status", [""])[0] or None,
                                      limit=int(query.get("limit", ["20"])[0]),
                                      profile=query.get("profile", [""])[0])
            self._json({"plans": plans, "stats": plan_store().stats()})
            return
        if route == "plans/stats":
            self._json({"stats": plan_store().stats()})
            return
        if route.startswith("plans/"):
            parts = [p for p in route.split("/") if p]
            plan_id = parts[1] if len(parts) > 1 else ""
            tail = parts[2] if len(parts) > 2 else ""
            if tail == "journal":
                journal = plan_store().journal(plan_id)
                if journal is None:
                    self._json({"error": {"message": "no such plan"}}, 404)
                    return
                self._json({"journal": journal, "plan": plan_store().get(plan_id)})
                return
            plan = plan_store().get(plan_id)
            if not plan:
                self._json({"error": {"message": "no such plan"}}, 404)
                return
            self._json({"plan": plan})
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

    def _archive_plan(self, plan):
        """A finished plan leaves a journal in long-term memory (4.0)."""
        if (CFG.get("memory") or {}).get("auto_index", True) is False:
            return
        journal = plan_store().journal(plan.get("id"))
        if not journal:
            return
        try:
            start_hub().call("memory_memory_add", {"text": journal[:4000], "tags": "计划日志",
                                                   "source": "plan:%s" % plan.get("id")})
            log("plan %s finished -> journal stored in memory (%d chars)" % (plan.get("id"), len(journal)))
        except Exception as exc:  # noqa: BLE001
            log("could not archive the plan journal: %s" % exc)

    def _outbox_base(self):
        """A phone-reachable base for outbox links.

        The proxy is the phone-facing port (usually bound to 0.0.0.0), while the bridge
        itself may be loopback-only - so outbox links must point at the proxy when it is
        enabled, otherwise the phone would get a 127.0.0.1 URL it cannot open.
        """
        proxy = CFG.get("proxy") or {}
        override = str((CFG.get("outbox") or {}).get("base_url") or "").rstrip("/")
        if override:
            return override
        # Best signal: the host the client actually used to reach us (e.g. the phone
        # calling 172.26.70.161:8890). Falls back to guessing the LAN address.
        seen = client_host()
        if seen:
            return "http://%s" % seen
        if proxy.get("enabled", True) is not False:
            port = (proxy.get("listen") or {}).get("port", 8890)
            return "http://%s:%s" % (lan_ip(), port)
        listen = CFG.get("listen") or {}
        host = listen.get("host", "127.0.0.1")
        port = listen.get("port", 8877)
        return "http://%s:%s" % (lan_ip() if host in ("0.0.0.0", "::", "127.0.0.1") else host, port)

    def _v2_post(self):
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path[len("/v2/"):].strip("/")
        if not control_ok(self):
            self._json({"error": {"message": "control API needs the X-Control-Token header (or ?token=…) "
                                             "because the bridge is reachable on the LAN",
                                  "type": "control_token_required"}}, 401)
            return
        body = self._read_body()
        if route == "voice/say":
            line = voice_queue().say(body.get("text", ""), voice=body.get("voice", ""),
                                     rate=body.get("rate", 1.0), interrupt=body.get("interrupt", False))
            self._json({"line": line, "stats": voice_queue().stats()})
            return
        if route == "voice/heard":
            line = voice_queue().heard(body.get("text", ""), source=body.get("source", "phone"))
            log("voice: user said %r" % (body.get("text", "")[:80]))
            self._json({"line": line})
            return
        if route == "voice/spoken":
            self._json({"ok": voice_queue().mark_spoken(body.get("seq", 0)),
                        "stats": voice_queue().stats()})
            return
        if route == "voice/call":
            voice_queue().call_event(body.get("event", ""), body.get("note", ""))
            self._json({"stats": voice_queue().stats()})
            return
        if route == "voice/clear":
            self._json({"cleared": voice_queue().clear()})
            return
        if route == "outbox":
            path = body.get("path")
            if not path:
                self._json({"error": {"message": "give me a path (and optionally kind/ttl/once/note)"}}, 400)
                return
            try:
                record = outbox().add(path, kind=body.get("kind") or "file",
                                      ttl=body.get("ttl"), once=body.get("once", False),
                                      note=body.get("note", ""))
            except FileNotFoundError:
                self._json({"error": {"message": "file not found: %s" % path}}, 404)
                return
            record = dict(record, url="%s/out/%s" % (self._outbox_base(), record["token"]))
            log("outbox added %s (%s, %d bytes)" % (record["id"], record.get("name"), record["bytes"]))
            self._json({"item": record}, 201)
            return
        if route == "outbox/purge":
            self._json({"removed": outbox().purge()})
            return
        if route.startswith("devices/"):
            action = route.split("/", 1)[1]
            store = device_store()
            if action == "approve":
                device = store.approve(body.get("id", ""), body.get("name", ""))
                if not device:
                    self._json({"error": {"message": "no such pending device"}}, 404)
                    return
                log("device approved: %s (%s)" % (device["id"], device["name"]))
                self._json({"device": device})
                return
            if action == "reject":
                self._json({"rejected": store.reject(body.get("id", ""))})
                return
            if action == "revoke":
                self._json({"revoked": store.revoke(body.get("id", ""))})
                return
            if action == "rename":
                device = store.rename(body.get("id", ""), body.get("name", ""))
                self._json({"device": device} if device else {"error": {"message": "no such device"}},
                           200 if device else 404)
                return
            self._json({"error": {"message": "unknown device action"}}, 404)
            return
        if route == "plans":
            try:
                plan = plan_store().create(goal=body.get("goal", ""), steps=body.get("steps"),
                                           title=body.get("title", ""), profile=body.get("profile", ""))
            except ValueError as exc:
                self._json({"error": {"message": str(exc)}}, 400)
                return
            self._json({"plan": plan}, 201)
            return
        if route.startswith("plans/"):
            parts = [p for p in route.split("/") if p]
            plan_id = parts[1] if len(parts) > 1 else ""
            action = parts[2] if len(parts) > 2 else ""
            extra = parts[3] if len(parts) > 3 else ""
            plan = None
            if action == "step" and extra == "add":
                plan = plan_store().add_step(plan_id, body.get("text", ""), body.get("tool", ""),
                                             args=body.get("args"))
            elif action == "step":
                try:
                    plan = plan_store().mark_step(plan_id, body.get("step", ""), body.get("status", "done"),
                                                  evidence=body.get("evidence", ""), error=body.get("error", ""),
                                                  verify_evidence=body.get("verify_evidence", ""))
                except ValueError as exc:
                    self._json({"error": {"message": str(exc), "type": "verification_required"}}, 400)
                    return
                if plan and plan.get("status") == "done":
                    self._archive_plan(plan)
            elif action == "add":
                plan = plan_store().add_step(plan_id, body.get("text", ""), body.get("tool", ""),
                                             args=body.get("args"))
            elif action == "cancel":
                plan = plan_store().cancel(plan_id)
            else:
                self._json({"error": {"message": "unknown plan action"}}, 404)
                return
            if not plan:
                self._json({"error": {"message": "no such plan or step"}}, 404)
                return
            self._json({"plan": plan})
            return
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
        if self.path.startswith("/out/"):
            self._serve_outbox(self.path[len("/out/"):].split("?")[0])
            return
        if self.path.startswith("/voice"):
            serve_voice_page(self)
            return
        if self.path.startswith("/dashboard"):
            self._dashboard()
            return
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


def cmd_audit(limit=30):
    """--audit: what actually ran, with arguments and durations."""
    rows = audit_store().tail(limit=int(limit))
    stats = audit_store().stats()
    print("audit: total=%d window=%ds calls=%d failures=%d db=%s"
          % (stats["total"], stats["since_seconds"], stats["window_calls"], stats["window_failures"],
             audit_store().path))
    for row in rows:
        print("  %s [%s] %s %sms %s" % (row["when"], "ok" if row["ok"] else "FAILED", row["tool"], row["ms"],
                                        json.dumps(row["args"], ensure_ascii=False)[:110]))
    return 0


def cmd_plans(limit=20):
    store = plan_store()
    print("plans: %s db=%s" % (store.stats(), store.path))
    for plan in store.list(limit=int(limit)):
        print("  %s [%-9s] %d/%d %s" % (plan["id"], plan["status"], plan["done"], plan["total"], plan["title"][:50]))
    return 0


def cmd_devices(limit=30):
    """--devices: who may drive this PC, plus any approval waiting."""
    store = device_store()
    stats = store.stats()
    print("devices: mode=%s registered=%d pending=%d calls=%d file=%s"
          % (devices_mode(), stats["devices"], stats["pending"], stats["calls"], store.path))
    for device in store.devices()[:int(limit)]:
        print("  %s %s [%s] last_seen=%s calls=%s" % (
            device["id"], device.get("name"), device.get("masked"),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(device.get("last_seen") or 0)) if device.get("last_seen") else "-",
            device.get("calls")))
    for item in store.pending():
        print("  PENDING %s key=%s tries=%s ip=%s (approve with --devices-approve %s)"
              % (item["id"], item.get("masked"), item.get("seen"), item.get("ip"), item["id"]))
    if devices_mode() == "off":
        print("  note=devices.mode is 'off': any client that can reach the port may use this PC. "
              "Set it to 'allowlist' to require approval.")
    return 0


def cmd_devices_approve(pending_id, name=""):
    device = device_store().approve(pending_id, name)
    if not device:
        print("no pending device with id %s" % pending_id)
        return 1
    print("approved %s as %r" % (device["id"], device["name"]))
    return 0


def cmd_devices_revoke(device_id):
    ok = device_store().revoke(device_id)
    print("revoked %s" % device_id if ok else "no device with id %s" % device_id)
    return 0 if ok else 1


def cmd_outbox(purge=False, limit=20):
    box = outbox()
    if purge:
        print("purged %d expired item(s)" % box.purge())
    stats = box.stats()
    print("outbox: items=%d bytes=%d kinds=%s dir=%s" % (stats["items"], stats["bytes"],
                                                         stats["kinds"] or "-", box.root))
    for item in box.list(limit=int(limit)):
        print("  %s [%s] %s bytes=%s fetches=%s expires_in=%ss"
              % (item["id"], item.get("kind"), item.get("name"), item.get("bytes"), item.get("fetches"),
                 int(float(item.get("expires") or 0) - time.time())))
    return 0


def cmd_backup(target=""):
    """--backup [zip]: one archive with every piece of state (config, jobs, plans, memory, audit, devices)."""
    import zipfile
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = target or os.path.join(BASE_DIR, "mcp-hands-backup-%s.zip" % stamp)
    if os.path.isdir(target):
        print("refusing to write a backup over the folder %s" % target)
        return 2
    if os.path.isfile(target) and os.path.splitext(target)[1].lower() != ".zip":
        print("refusing to overwrite %s (not a .zip)" % target)
        return 2
    if not os.path.isdir(os.path.dirname(os.path.abspath(target)) or "."):
        print("refusing to write into %s (no such folder)" % os.path.dirname(target))
        return 2
    files = []
    for name in ("bridge.config.json", "jobs.db", "plans.db", "audit.db", "devices.json"):
        path = os.path.join(BASE_DIR, name)
        if os.path.isfile(path):
            files.append(path)
    memory_db = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                             "mcp-hands", "memory.db")
    if os.path.isfile(memory_db):
        files.append(memory_db)
    outbox_dir = os.path.join(BASE_DIR, "outbox")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, os.path.basename(path))
        if os.path.isdir(outbox_dir):
            for root, _dirs, names in os.walk(outbox_dir):
                for name in names:
                    full = os.path.join(root, name)
                    zf.write(full, os.path.relpath(full, BASE_DIR))
        zf.writestr("backup.json", json.dumps({"version": APP_VERSION, "created": time.time(),
                                               "files": [os.path.basename(p) for p in files]},
                                              ensure_ascii=False, indent=2))
    print("backup written: %s (%d file(s), %.1f KB)" % (target, len(files), os.path.getsize(target) / 1024.0))
    return 0


def cmd_restore(source):
    """--restore <zip>: put state back (existing files are kept as .before-restore)."""
    import zipfile
    if not os.path.isfile(source):
        print("no such backup: %s" % source)
        return 1
    home = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    memory_dir = os.path.join(home, "mcp-hands")
    allowed_roots = [os.path.realpath(BASE_DIR), os.path.realpath(memory_dir)]
    restored, refused = [], []
    with zipfile.ZipFile(source) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = os.path.basename(info.filename)
            if not name or name == "backup.json":
                continue
            if info.filename.startswith("outbox/"):
                target = os.path.join(BASE_DIR, info.filename)
            elif name == "memory.db":
                target = os.path.join(memory_dir, name)
            elif name in ("bridge.config.json", "jobs.db", "plans.db", "audit.db", "devices.json"):
                target = os.path.join(BASE_DIR, name)
            else:
                refused.append(info.filename)
                continue
            # zip-slip guard: the resolved target must stay inside one of the two roots
            real = os.path.realpath(target)
            if not any(real == root or real.startswith(root + os.sep) for root in allowed_roots):
                refused.append(info.filename)
                continue
            os.makedirs(os.path.dirname(real) or ".", exist_ok=True)
            if os.path.isfile(real):
                try:
                    os.replace(real, real + ".before-restore")
                except OSError:
                    pass
            with zf.open(info) as src, open(real, "wb") as dst:
                dst.write(src.read())
            restored.append(name)
    print("restored %d file(s): %s" % (len(restored), ", ".join(sorted(set(restored))) or "-"))
    if refused:
        print("refused %d unsafe entry/entries (outside the install): %s"
              % (len(refused), ", ".join(refused[:5])))
    print("note=restart the service so the restored config and databases are picked up")
    return 0


def cmd_check_update():
    """--check-update: compare this build with the newest published release."""
    import urllib.error
    url = "https://api.github.com/repos/%s/releases/latest" % APP_REPO
    try:
        request = urllib.request.Request(url, headers={"User-Agent": APP_NAME, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        print("could not check for updates: %s" % exc)
        return 1
    latest = str(data.get("tag_name") or "").lstrip("v")
    assets = [a.get("browser_download_url") for a in (data.get("assets") or [])]
    print("installed : %s" % APP_VERSION)
    print("published : %s (%s)" % (latest or "?", data.get("published_at")))
    if latest and latest != APP_VERSION:
        print("update available: %s" % data.get("html_url"))
        for asset in assets:
            if asset:
                print("  asset: %s" % asset)
        print("run update.ps1 (or download the asset and replace mcp-hands.exe + _internal)")
    else:
        print("you are on the latest version")
    return 0


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
    if "--audit" in ARGV:
        raise SystemExit(cmd_audit(int(_opt("--limit") or 30)))
    if "--plans" in ARGV:
        raise SystemExit(cmd_plans(int(_opt("--limit") or 20)))
    if "--devices" in ARGV:
        raise SystemExit(cmd_devices(int(_opt("--limit") or 30)))
    if "--devices-approve" in ARGV:
        raise SystemExit(cmd_devices_approve(_opt("--devices-approve") or "", _opt("--name") or ""))
    if "--devices-revoke" in ARGV:
        raise SystemExit(cmd_devices_revoke(_opt("--devices-revoke") or ""))
    if "--outbox" in ARGV:
        raise SystemExit(cmd_outbox(bool(_opt("--purge")), int(_opt("--limit") or 20)))
    if "--backup" in ARGV:
        raise SystemExit(cmd_backup(_opt("--backup") or ""))
    if "--restore" in ARGV:
        raise SystemExit(cmd_restore(_opt("--restore") or ""))
    if "--check-update" in ARGV:
        raise SystemExit(cmd_check_update())
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
    fs_entry = next((s for s in (CFG.get("servers") or []) if s.get("name") == "fs"), {})
    log("fs roots : %s" % ((fs_entry.get("env") or {}).get("MCP_FS_ROOTS") or "(user home)"))
    log("voice    : %s" % ("on - open http://%s:%s/voice on the phone" % (lan_ip(), proxy_port)
                           if (CFG.get("proxy") or {}).get("enabled", True) is not False else "off"))
    dashboard_host = host if host not in ("0.0.0.0", "::", "") else lan_ip()
    if control_required():
        log("console  : http://%s:%d/dashboard?token=%s" % (dashboard_host, port, control_token()))
        log("note     : the bridge is reachable on the LAN, so /v2 and /dashboard need that token")
    else:
        log("console  : http://127.0.0.1:%d/dashboard" % port)
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
