"""Bug hunt for 4.0: probe the paths that reasoning flagged as risky.

Each probe prints WHAT IS WRONG (or ok). Nothing here is a happy-path demo: the point
is to make the suspicious cases fail loudly so they can be fixed.

  1. does a non-tool model (relay path) bypass the device allowlist?
  2. can send_text be talked into writing outside %TEMP% with a crafted name?
  3. do generated temp files leak after delivery?
  4. does outbox.add_bytes actually work?
  5. does a malformed policy glob break every tool call?
  6. is audit trimming honoured?
  7. does log rotation fire and stay quiet?
  8. can a crafted backup zip escape the restore target (zip-slip)?
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = r"C:\Users\13323\Documents\deepseek-harness\default-workspace\mcp-persona-bridge"
TMP = tempfile.gettempdir()
STATE = os.path.join(TMP, "mcp-hands-hunt")
CONFIG = os.path.join(STATE, "config.json")
os.environ["MCP_SAMPLES_DIR"] = TMP
os.environ["BRIDGE_CONFIG"] = CONFIG
os.environ["MCP_MEMORY_FILE"] = os.path.join(STATE, "memory.db")
sys.path.insert(0, ROOT)

BRIDGE_PORT = 8881
PROXY_PORT = 8899

os.makedirs(STATE, exist_ok=True)
with open(CONFIG, "w", encoding="utf-8") as fh:
    json.dump({
        "listen": {"host": "127.0.0.1", "port": BRIDGE_PORT},
        "upstream": {"name": "x", "base_url": "http://127.0.0.1:8899/v1", "api_key": "x", "model": "x"},
        "proxy": {"enabled": True, "listen": {"host": "127.0.0.1", "port": PROXY_PORT},
                  "upstream_base": "http://127.0.0.1:8891/v1", "upstream_api_key": "",
                  "tool_models": ["mock-native"], "max_tool_rounds": 4, "max_seconds": 30,
                  "progress_stream": False, "inject_tool_hint": True},
        "jobs": {"enabled": False},
        "audit": {"enabled": True, "db": os.path.join(STATE, "audit.db"), "max_rows": 500},
        "plans": {"db": os.path.join(STATE, "plans.db")},
        "outbox": {"dir": os.path.join(STATE, "outbox"), "ttl": 600},
        "voice": {"path": os.path.join(STATE, "voice.json"), "max_lines": 50},
        "devices": {"mode": "allowlist", "path": os.path.join(STATE, "devices.json")},
        "log": {"format": "text", "max_mb": 1},
        "servers": [{"name": "calc", "enabled": True}, {"name": "send", "enabled": True},
                    {"name": "shell", "enabled": True}],
    }, fh)

import bridge  # noqa: E402
import proxy as proxy_module  # noqa: E402

findings = []


def report(number, title, broken, detail=""):
    findings.append((number, title, broken))
    print("%s probe %s: %s%s" % ("BUG " if broken else "ok  ", number, title,
                                 ("  <- " + detail) if detail else ""))


def get(path, timeout=20):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (BRIDGE_PORT, path), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")


def post(path, payload, timeout=60):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (BRIDGE_PORT, path),
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")


hub = bridge.start_hub()
server = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), bridge.Handler)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
service = proxy_module.ProxyService().start()
time.sleep(0.5)


def ask(model, key="sk-hunt-unknown", stream=False):
    body = {"model": model, "stream": stream, "messages": [{"role": "user", "content": "hello"}]}
    request = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PROXY_PORT,
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:80]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:80]


# 1. relay path vs device allowlist
status_tool, _ = ask("mock-native")
status_relay, _ = ask("some-other-model")
report(1, "device allowlist also gates the relay path (non-tool model)",
       status_relay != 401, "tool model -> %s, relay model -> %s" % (status_tool, status_relay))

# 2. send_text with a crafted name
evil = os.path.join(STATE, "escaped.txt")
if os.path.exists(evil):
    os.remove(evil)
out, err = hub.call("send_send_text", {"text": "hunt", "name": os.path.join("..", "escaped.txt")})
report(2, "send_text keeps its scratch file inside the temp folder",
       os.path.exists(os.path.join(os.path.dirname(STATE), "escaped.txt")),
       (out or err or "").splitlines()[0][:70])

# 3. generated temp files after delivery
before = set(os.listdir(TMP))
hub.call("send_send_qr", {"text": "hunt-check"})
after = set(os.listdir(TMP))
leaked = [n for n in (after - before) if n.startswith("mcp-hands-qr-")]
report(3, "generated media does not linger in %TEMP% after delivery", bool(leaked), ", ".join(leaked[:3]))

# 4. outbox.add_bytes
try:
    record = bridge.outbox().add_bytes(b"hunt-bytes", name="hunt.bin", kind="file")
    resolved, _reason = bridge.outbox().resolve(record["token"])
    ok = bool(resolved) and record["name"] == "hunt.bin" and os.path.getsize(record["stored"]) == 10
    detail = "record ok" if ok else "record looks wrong: %s" % record
except Exception as exc:  # noqa: BLE001
    ok = False
    detail = "%s: %s" % (type(exc).__name__, exc)
report(4, "outbox.add_bytes works (or is gone)", not ok, detail)

# 4b. nested secrets must not reach the audit log
masked = bridge.audit_store()
masked.record("probe_http", {"headers": {"Authorization": "Bearer sk-abcdef1234567890xyz"},
                             "url": "https://x/?token=sk-abcdef1234567890xyz"}, True, "", 1, 2)
row = [item for item in masked.tail(limit=5) if item["tool"] == "probe_http"][0]
leaked = "sk-abcdef1234567890xyz" in json.dumps(row["args"], ensure_ascii=False)
report(10, "nested / embedded secrets are masked in the audit log", leaked,
       json.dumps(row["args"], ensure_ascii=False)[:110])

# 5. malformed policy pattern
policy = bridge.Policy({"mode": "enforce", "deny": ["[broken"]})
try:
    allowed, reason = policy.check("calc_calc", {})
    ok = allowed
    detail = "allowed=%s reason=%s" % (allowed, reason)
except Exception as exc:  # noqa: BLE001
    ok = False
    detail = "%s: %s" % (type(exc).__name__, exc)
report(5, "a malformed policy pattern does not break tool calls", not ok, detail)

# 6. audit trimming
store = bridge.audit_store()
for index in range(700):
    store.record("probe_tool", {"n": index}, True, "", 1, 2)
count = store.stats()["total"]
report(6, "audit honours max_rows", count > 520, "rows=%d (max 500)" % count)

# 7. log rotation
log_path = bridge.LOG_FILE
big = os.path.join(os.path.dirname(log_path), os.path.basename(log_path))
with open(big, "w", encoding="utf-8") as fh:
    fh.write("x" * (1_200_000))
try:
    bridge.log("rotation probe")
    rotated = os.path.exists(big + ".1") and os.path.getsize(big) < 200_000
    detail = "log=%d backup=%s" % (os.path.getsize(big), os.path.exists(big + ".1"))
except Exception as exc:  # noqa: BLE001
    rotated, detail = False, "%s: %s" % (type(exc).__name__, exc)
report(7, "log rotation fires past log.max_mb", not rotated, detail)

# 8. zip-slip through a crafted backup
import zipfile
crafted = os.path.join(STATE, "evil.zip")
outside = os.path.join(os.path.dirname(STATE), "escaped-outbox.txt")
if os.path.exists(outside):
    os.remove(outside)
with zipfile.ZipFile(crafted, "w") as zf:
    zf.writestr("outbox/../../%s" % os.path.basename(outside), "pwned")
    zf.writestr("backup.json", "{}")
bridge.cmd_restore(crafted)
report(8, "restore refuses to write outside the install (zip-slip)", os.path.exists(outside), outside)

# 9. control API protection when the bridge is reachable from the LAN
saved_control = dict(bridge.CFG.get("control") or {})
saved_listen = dict(bridge.CFG.get("listen") or {})
bridge.CFG["listen"] = dict(saved_listen, host="0.0.0.0")
bridge.CFG["control"] = {"token": "", "require_for_lan": True}
bridge.CFG["control"]["token"] = "hunt-token-123"


def control_get(path, token=""):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (BRIDGE_PORT, path),
                                     headers={"X-Control-Token": token} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


no_token = control_get("/v2/devices")
with_token = control_get("/v2/devices", "hunt-token-123")
health_open = control_get("/v2/health")
report(9, "exposed control API demands a token (health stays open)",
       not (no_token == 401 and with_token == 200 and health_open == 200),
       "no token -> %s, token -> %s, health -> %s" % (no_token, with_token, health_open))
bridge.CFG["listen"] = saved_listen
bridge.CFG["control"] = saved_control

# 11. CLI: a flag value must never be mistaken for the config path
import subprocess  # noqa: E402

cli_dir = os.path.join(STATE, "cli")
os.makedirs(cli_dir, exist_ok=True)
zip_target = os.path.join(cli_dir, "backup-target.zip")
for leftover in (zip_target, os.path.join(cli_dir, "3")):
    if os.path.exists(leftover):
        os.remove(leftover)
# Run through gui.py (the frozen exe's entry point) *and* with no inherited config, so
# the "which argument is the config" logic is exercised for real.
cli_env = {key: value for key, value in os.environ.items() if key != "BRIDGE_CONFIG"}
cli_env["PYTHONIOENCODING"] = "utf-8"
cli = [sys.executable, os.path.join(ROOT, "gui.py"), "--backup", zip_target]
done = subprocess.run(cli, capture_output=True, cwd=cli_dir, timeout=180, env=cli_env)
output = (done.stdout or b"").decode("utf-8", "replace") + (done.stderr or b"").decode("utf-8", "replace")
made_zip = os.path.exists(zip_target) and zipfile.is_zipfile(zip_target)
made_config_instead = os.path.exists(zip_target) and not zipfile.is_zipfile(zip_target)
report(11, "CLI: `--backup <path>` (via the exe entry point, no config env) writes a backup there",
       not made_zip or made_config_instead,
       ("zip ok, %d entries" % len(zipfile.ZipFile(zip_target).namelist())) if made_zip and not made_config_instead
       else output.strip()[:110])

bare = os.path.join(cli_dir, "3")
cli2 = [sys.executable, os.path.join(ROOT, "gui.py"), os.path.join(ROOT, "configs", "bridge.config.mock.json"),
        "--plans", "--limit", "3"]
done2 = subprocess.run(cli2, capture_output=True, cwd=cli_dir, timeout=180, env=cli_env)
report(12, "CLI: `--limit 3` does not leave a file named '3' behind",
       os.path.exists(bare), bare)

# 13. an unrecognised flag must fail loudly instead of opening the GUI (looked hung)
cli3 = [sys.executable, os.path.join(ROOT, "gui.py"), "--devices-approve", "pend_nope"]
done3 = subprocess.run(cli3, capture_output=True, cwd=cli_dir, timeout=180, env=cli_env)
out3 = ((done3.stdout or b"").decode("utf-8", "replace") + (done3.stderr or b"").decode("utf-8", "replace")).strip()
report(13, "CLI: `--devices-approve <id>` runs headless (no window), reporting the unknown id",
       done3.returncode != 0 and "no pending device" not in out3,
       "exit=%s out=%s" % (done3.returncode, out3.splitlines()[0][:80] if out3 else ""))

cli4 = [sys.executable, os.path.join(ROOT, "gui.py"), "--totally-unknown"]
done4 = subprocess.run(cli4, capture_output=True, cwd=cli_dir, timeout=180, env=cli_env)
out4 = ((done4.stdout or b"").decode("utf-8", "replace") + (done4.stderr or b"").decode("utf-8", "replace")).strip()
report(14, "CLI: an unknown flag exits with an error instead of opening a window",
       done4.returncode != 2 or "unknown option" not in out4,
       "exit=%s out=%s" % (done4.returncode, out4.splitlines()[0][:70] if out4 else ""))

# 15. both web pages must be valid JavaScript (a stray quote used to freeze the console)
import re  # noqa: E402

node = os.environ.get("DSH_NODE") or r"C:\Users\13323\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
pages = {
    "dashboard": bridge.DASHBOARD_HTML.replace("__VERSION__", "x").replace("__BRIDGE__", "http://x").replace("__TOKEN__", ""),
    "voice": bridge.VOICE_HTML,
}
bad_js = []
for name, html in pages.items():
    for index, block in enumerate(re.findall(r"(?s)<script>(.*?)</script>", html)):
        path = os.path.join(STATE, "%s_%d.js" % (name, index))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(block)
        if os.path.isfile(node):
            check = subprocess.run([node, "--check", path], capture_output=True, timeout=60)
            if check.returncode != 0:
                bad_js.append("%s:%s" % (name, (check.stderr or b"").decode("utf-8", "replace").strip().splitlines()[:1]))
report(15, "the dashboard and voice pages contain valid JavaScript", bool(bad_js),
       "; ".join(bad_js) or "node --check passed for every script block")

# 16. the voice channel round-trip
said = post("/v2/voice/say", {"text": "你好，我是林挽夏"})
line = said.get("line") or {}
pending_before = get("/v2/voice?since=0")["lines"]
post("/v2/voice/spoken", {"seq": line.get("seq")})
pending_after = get("/v2/voice?since=0")["lines"]
heard = post("/v2/voice/heard", {"text": "听到了"})
unread = get("/v2/voice?since=0&kind=heard")["lines"]
checks = {
    "seq assigned": bool(line.get("seq")),
    "unspoken line is pending": any(l.get("seq") == line.get("seq") for l in pending_before),
    "spoken line leaves the queue": not any(l.get("seq") == line.get("seq") for l in pending_after),
    "heard line is stored": (heard.get("line") or {}).get("kind") == "heard",
    "heard line shows up unread": any(l.get("seq") == (heard.get("line") or {}).get("seq") for l in unread),
}
bad = [name for name, ok in checks.items() if not ok]
report(16, "voice queue: say -> pending -> spoken -> heard round-trip works", bool(bad),
       ("failed: %s" % ", ".join(bad)) if bad else ", ".join("%s=ok" % name for name in checks))
try:
    page = urllib.request.urlopen("http://127.0.0.1:%d/voice" % BRIDGE_PORT, timeout=10).read().decode("utf-8")
    voice_page_ok = "speechSynthesis" in page and "/v2/voice/next" in page
except Exception as exc:  # noqa: BLE001
    page, voice_page_ok = str(exc), False
report(17, "the voice page reaches the phone with TTS wired up", not voice_page_ok, page[:60])

# 18. sending an image must answer with the bare URL and nothing else (measured against
# the real client: markdown is shown as dead text, a bare URL is fetched and drawn)
img = os.path.join(STATE, "pic.png")
with open(img, "wb") as fh:
    fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 40)
delivered, err_img = hub.call("send_send_image", {"path": img, "note": "测试图"})
lines = [line for line in (delivered or "").splitlines() if line.strip()]
first = lines[0] if lines else ""
report(18, "send_image answers with exactly one bare URL",
       not (len(lines) == 1 and first.startswith("http") and "/out/" in first
            and "url=" not in first and "![" not in first),
       "lines=%d first=%s" % (len(lines), first[:80]))

# 19. the fs sandbox boundary is visible without guessing
health = get("/v2/health")
report(19, "/v2/health reports the fs roots and the voice channel",
       not ("fs_roots" in health and "voice" in health),
       "fs_roots=%s voice_lines=%s" % (health.get("fs_roots"), (health.get("voice") or {}).get("lines")))

# 20. a dead server must come back on its own (and say so), not break the tool set
victim = next((srv for srv in bridge.HUB.servers if srv.name == "calc"), None) if bridge.HUB else None
if victim is not None:
    victim._proc.kill()
    time.sleep(0.8)
    out, err = bridge.HUB.call("calc_calc", {"expression": "1+1"})
    detail = "restarts=%s result=%r" % (victim.restarts, (out or "")[:40])
    report(20, "a crashed server is restarted automatically on the next call",
           not (victim.restarts >= 1 and "2" in (out or "")), detail)
else:
    report(20, "a crashed server is restarted automatically on the next call", True, "no hub to test")

service.stop()
server.shutdown()
bridge.stop_hub()
broken = [item for item in findings if item[2]]
print("\nVERDICT: %d probe(s) found real problems: %s"
      % (len(broken), ", ".join("#%s" % item[0] for item in broken) or "none"))
raise SystemExit(1 if broken else 0)
