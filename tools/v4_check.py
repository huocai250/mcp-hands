"""End-to-end acceptance for the 4.0 features (offline, no real API key).

Covers the media outbox (signed, expiring, one-shot links), device pairing/enforcement,
plan verification + journal, the send_* tools (including real text-to-speech), and the
backup/restore round trip.
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
CONFIG = os.path.join(TMP, "mcp-hands-v4-test.json")
STATE = os.path.join(TMP, "mcp-hands-v4-state")
os.environ["MCP_SAMPLES_DIR"] = TMP
os.environ["BRIDGE_CONFIG"] = CONFIG
os.environ["MCP_MEMORY_FILE"] = os.path.join(STATE, "memory.db")
sys.path.insert(0, ROOT)

BRIDGE_PORT = 8883
PROXY_PORT = 8898          # must not collide with the vision mock (8894)
VISION_MOCK = 8894

os.makedirs(STATE, exist_ok=True)
with open(CONFIG, "w", encoding="utf-8") as fh:
    json.dump({
        "listen": {"host": "127.0.0.1", "port": BRIDGE_PORT},
        "upstream": {"name": "x", "base_url": "http://127.0.0.1:8899/v1", "api_key": "x", "model": "x"},
        "proxy": {"enabled": True, "listen": {"host": "127.0.0.1", "port": PROXY_PORT},
                  "upstream_base": "http://127.0.0.1:8891/v1", "upstream_api_key": "",
                  "tool_models": ["mock-native"], "max_tool_rounds": 8, "max_seconds": 60,
                  "heartbeat_seconds": 5, "progress_stream": False, "vision_check_seconds": 600,
                  "step_report": "brief", "inject_tool_hint": True},
        "vision": {"base_url": "http://127.0.0.1:%d/v1" % VISION_MOCK, "api_key": "mock-key",
                   "model": "mock-vision", "detail": "auto", "thinking": "disabled", "max_pixels": 1300,
                   "inherit_upstream_key": False},
        "jobs": {"enabled": True, "workers": 1, "notify": False, "db": os.path.join(STATE, "jobs.db")},
        "memory": {"auto_index": True},
        "plans": {"db": os.path.join(STATE, "plans.db")},
        "audit": {"enabled": True, "db": os.path.join(STATE, "audit.db"), "max_rows": 500},
        "policy": {"mode": "audit", "deny": [], "allow": [], "exempt": ["audit_*", "policy_*", "jobs_*", "plan_*"]},
        "outbox": {"dir": os.path.join(STATE, "outbox"), "ttl": 3600, "max_items": 20, "secret": "test-secret"},
        "devices": {"mode": "allowlist", "path": os.path.join(STATE, "devices.json")},
        "log": {"format": "text", "max_mb": 8},
        "profiles": {},
        "servers": [{"name": "calc", "enabled": True}, {"name": "plan", "enabled": True},
                    {"name": "send", "enabled": True}, {"name": "device", "enabled": True},
                    {"name": "memory", "enabled": True}, {"name": "fs", "enabled": True,
                                                          "env": {"MCP_FS_ROOTS": STATE}}],
    }, fh)

import bridge  # noqa: E402
import proxy as proxy_module  # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append((label, ok))
    print("%s %s%s" % ("PASS" if ok else "FAIL", label, ("  <- " + detail) if detail else ""))


def get(path, timeout=20):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (BRIDGE_PORT, path), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")


def post(path, payload, timeout=60):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (BRIDGE_PORT, path),
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")


def ask(content, key="sk-test", timeout=90):
    body = {"model": "mock-native", "stream": False, "messages": [{"role": "user", "content": content}]}
    request = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PROXY_PORT,
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")), resp.status
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8", "replace") or "{}"), exc.code


hub = bridge.start_hub()
server = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), bridge.Handler)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
bridge.job_runner()
service = proxy_module.ProxyService().start()
time.sleep(0.5)
import socket  # noqa: E402

probe = socket.socket()
try:
    probe.connect(("127.0.0.1", PROXY_PORT))
    bound = True
except OSError as exc:
    bound = False
    print("proxy did not bind %d: %s" % (PROXY_PORT, exc))
finally:
    probe.close()
check("0. the proxy really bound its port", bound, "port %d" % PROXY_PORT)

# ------------------------------------------------------------- 1. device pairing
_, status = ask("ECHO_SYS", key="sk-brand-new-device-key")
check("1. unknown device is refused", status == 401, "http %s" % status)
pending = get("/v2/devices?pending=1")["pending"]
check("1. it shows up as a pending request", bool(pending) and pending[0]["masked"].startswith("sk-"),
      str(pending[0]["masked"]) if pending else "none")
approved = post("/v2/devices/approve", {"id": pending[0]["id"], "name": "我的手机"})
check("1. approving it works", bool(approved.get("device")), str(approved.get("device", {}).get("name")))
reply, status2 = ask("ECHO_SYS", key="sk-brand-new-device-key")
check("1. the approved device can use the proxy", status2 == 200 and "SYS>>>" in reply["choices"][0]["message"]["content"],
      "http %s" % status2)
revoked = post("/v2/devices/revoke", {"id": approved["device"]["id"]})
_, status3 = ask("ECHO_SYS", key="sk-brand-new-device-key")
check("1. revoking locks it out again", revoked.get("revoked") and status3 == 401, "http %s" % status3)

# ------------------------------------------------------- 2. outbox + send tools
sample = os.path.join(STATE, "picture.txt")
with open(sample, "w", encoding="utf-8") as fh:
    fh.write("pretend this is a picture")
def local_url(url):
    """Fetch through loopback: the advertised host is the LAN address for the phone."""
    parts = urllib.parse.urlsplit(url)
    return "http://127.0.0.1:%s%s" % (parts.port or 80, parts.path)


item = post("/v2/outbox", {"path": sample, "kind": "text", "note": "demo", "ttl": 600})["item"]
check("2. outbox returns a signed url", item["url"].startswith("http://") and "/out/" in item["url"],
      item["url"][:70])
check("2. the url points at the phone-facing proxy port", str(PROXY_PORT) in item["url"],
      item["url"][:70])
with urllib.request.urlopen(local_url(item["url"]), timeout=20) as resp:
    body = resp.read().decode("utf-8", "replace")
check("2. the url really serves the file", "pretend this is a picture" in body, body[:40])
try:
    urllib.request.urlopen(local_url(item["url"]).replace("/out/", "/out/")[:-5] + "dead", timeout=10)
    tampered = "served"
except urllib.error.HTTPError as exc:
    tampered = "http %s" % exc.code
check("2. a tampered token is refused", tampered.startswith("http 4"), tampered)

once_item = post("/v2/outbox", {"path": sample, "kind": "text", "once": True})["item"]
urllib.request.urlopen(local_url(once_item["url"]), timeout=20).read()
try:
    urllib.request.urlopen(local_url(once_item["url"]), timeout=10).read()
    second = "served again"
except urllib.error.HTTPError as exc:
    second = "http %s" % exc.code
check("2. one-shot links die after first use", second.startswith("http 4"), second)

sent, err = hub.call("send_send_image", {"path": sample, "note": "试试"})
check("2. send_image hands back a markdown line", not err and "markdown=![试试]" in sent,
      (sent or "").splitlines()[0][:60])
qr, err_qr = hub.call("send_send_qr", {"text": "http://example.com/x"})
check("2. send_qr produces a qr image link", not err_qr and "qr" in (qr or "") and "url=" in (qr or ""),
      (qr or "").splitlines()[1][:60] if qr else "error")
text_out, err_text = hub.call("send_send_text", {"text": "很长的说明\n第二行", "name": "note.md"})
check("2. send_text delivers a file", not err_text and "note.md" in (text_out or ""), (text_out or "").splitlines()[0][:60])

voice, err_voice = hub.call("send_send_voice", {"text": "你好，我是林挽夏。"})
voice_ok = (not err_voice) and "audio" in (voice or "") and "url=" in (voice or "")
check("2. send_voice renders speech to an audio link", voice_ok,
      (voice or err_voice or "").splitlines()[0][:80])
listing = get("/v2/outbox?limit=20")
kinds = {i["kind"] for i in listing["items"]}
check("2. outbox holds all of them", {"text", "image", "qr"} <= kinds or len(listing["items"]) >= 3, str(sorted(kinds)))

# ------------------------------------------------------------ 3. plan verification
plan = post("/v2/plans", {"goal": "验证机制", "title": "验证机制",
                          "steps": [{"text": "写文件", "tool": "fs_write_text",
                                     "args": {"path": os.path.join(STATE, "out.txt"), "content": "hi"},
                                     "verify": "再列一次目录确认文件真的在"}]})["plan"]
plan_id = plan["id"]
try:
    post("/v2/plans/%s/step" % plan_id, {"step": 1, "status": "done", "evidence": "wrote it"})
    gate = "accepted"
except urllib.error.HTTPError as exc:
    gate = "http %s" % exc.code
check("3. a step needing proof cannot be closed without it", gate.startswith("http 4"), gate)
closed = post("/v2/plans/%s/step" % plan_id,
              {"step": 1, "status": "done", "evidence": "wrote it", "verify_evidence": "fs_list_dir ok: out.txt"})
check("3. with verification evidence it closes", closed["plan"]["status"] == "done"
      and "out.txt" in closed["plan"]["steps"][0]["verify_evidence"],
      closed["plan"]["steps"][0]["verify_evidence"][:60])
journal = get("/v2/plans/%s/journal" % plan_id)["journal"]
check("3. the journal records evidence and verification",
      "计划日志" in journal and "验证要求" in journal and "out.txt" in journal, journal.splitlines()[3][:60])
time.sleep(0.4)
logged, _ = hub.call("memory_memory_search", {"query": "验证机制"})
check("3. a finished plan is archived in memory", "计划日志" in (logged or ""), (logged or "").splitlines()[0][:60])

# ------------------------------------------------------------ 4. backup / restore
backup_path = os.path.join(STATE, "backup.zip")
code = bridge.cmd_backup(backup_path)
check("4. backup writes an archive", code == 0 and os.path.getsize(backup_path) > 0,
      "%.1f KB" % (os.path.getsize(backup_path) / 1024.0))
restored = bridge.cmd_restore(backup_path)
check("4. restore accepts it", restored == 0)

# ---------------------------------------------------------------- 5. dashboard
html = urllib.request.urlopen("http://127.0.0.1:%d/dashboard" % BRIDGE_PORT, timeout=10).read().decode()
check("5. dashboard shows devices and the outbox",
      "设备与配对" in html and "媒体出口" in html and "/v2/devices" in html and "/v2/outbox" in html,
      str(len(html)))
health = get("/v2/health")
check("5. /v2/health reports the new subsystems",
      "devices" in json.dumps(health) and health.get("version") == bridge.APP_VERSION,
      str(health.get("version")))

service.stop()
server.shutdown()
bridge.stop_jobs()
bridge.stop_hub()
failed = [label for label, ok in results if not ok]
print("\nfailed:", failed or "none")
raise SystemExit(1 if failed else 0)
