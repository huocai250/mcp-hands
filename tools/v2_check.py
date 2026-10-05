"""End-to-end acceptance for the 2.0 features (offline, no real API key).

Covers: job queue via REST + worker execution + result delivery into a chat turn,
long-term memory, config migration, /metrics, and upstream retry on 503.
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
CONFIG = os.path.join(TMP, "mcp-hands-v2-test.json")
os.environ["MCP_SAMPLES_DIR"] = TMP
os.environ["BRIDGE_CONFIG"] = CONFIG
os.environ["MCP_MEMORY_FILE"] = os.path.join(TMP, "mcp-hands-memory-test.db")
sys.path.insert(0, ROOT)

BRIDGE_PORT = 8885
PROXY_PORT = 8896

with open(CONFIG, "w", encoding="utf-8") as fh:
    json.dump({
        "listen": {"host": "127.0.0.1", "port": BRIDGE_PORT},
        "upstream": {"name": "x", "base_url": "http://127.0.0.1:8899/v1", "api_key": "x", "model": "x"},
        "proxy": {"enabled": True, "listen": {"host": "127.0.0.1", "port": PROXY_PORT},
                  "upstream_base": "http://127.0.0.1:8891/v1", "upstream_api_key": "",
                  "tool_models": ["mock-native"], "max_tool_rounds": 12, "max_seconds": 120,
                  "heartbeat_seconds": 5, "progress_stream": True, "progress_max": 3,
                  "vision_check_seconds": 600, "step_report": "brief", "inject_tool_hint": True},
        "vision": {"base_url": "http://127.0.0.1:8894/v1", "api_key": "mock-key", "model": "mock-vision",
                   "detail": "auto", "thinking": "disabled", "max_pixels": 1300,
                   "inherit_upstream_key": False},
        "jobs": {"enabled": True, "workers": 1, "notify": False,
                 "db": os.path.join(TMP, "mcp-hands-jobs-v2.db")},
        "memory": {"auto_index": True},
        "servers": [{"name": "calc", "enabled": True}, {"name": "jobs", "enabled": True},
                    {"name": "memory", "enabled": True}, {"name": "vision", "enabled": True}],
    }, fh)

import bridge  # noqa: E402
import proxy as proxy_module  # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append((label, ok))
    print("%s %s%s" % ("PASS" if ok else "FAIL", label, ("  <- " + detail) if detail else ""))


def get(path, timeout=15):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (BRIDGE_PORT, path), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")


def post(path, payload, timeout=30):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (BRIDGE_PORT, path),
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")


def ask_proxy(content, timeout=90):
    body = {"model": "mock-native", "stream": False, "messages": [{"role": "user", "content": content}]}
    request = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PROXY_PORT,
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer sk-test"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


hub = bridge.start_hub()
server = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), bridge.Handler)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
runner = bridge.job_runner()
service = proxy_module.ProxyService().start()
time.sleep(0.5)

# ------------------------------------------------------------------ 1. REST shape
health = get("/v2/health")
check("1. /v2/health reports version + jobs", health.get("ok") and health.get("version") == bridge.APP_VERSION,
      str(health)[:120])

# -------------------------------------------------------- 2. queue + run a job
created = post("/v2/jobs", {"steps": [{"tool": "calc_calc", "args": {"expression": "6*7"}},
                                      {"tool": "calc_calc", "args": {"expression": "1+1"}}],
                            "title": "两步计算", "notify": False})
job_id = (created.get("job") or {}).get("id")
check("2. POST /v2/jobs queues a plan", bool(job_id) and len(created["job"]["plan"]) == 2, str(job_id))
final = runner.wait(job_id, timeout_s=20)
check("2. worker executes the plan", final["status"] == "done" and final["progress"] == 2,
      "%s progress=%s" % (final["status"], final["progress"]))
check("2. real tool output is kept", "42" in (final["result"] or ""), (final["result"] or "")[:60])

# ------------------------------- 3. result is delivered into the next chat turn
pending = get("/v2/jobs?pending=1&mark=0")["jobs"]
check("3. finished job is pending delivery", any(j["id"] == job_id for j in pending),
      "%d pending" % len(pending))
sysreply = ask_proxy("ECHO_SYS")["choices"][0]["message"]["content"]
check("3. the chat turn carries the job report", "【后台任务回执】" in sysreply and "两步计算" in sysreply,
      sysreply[-160:].replace("\n", " | "))
after = get("/v2/jobs?pending=1&mark=0")["jobs"]
check("3. it is not delivered twice", all(j["id"] != job_id for j in after),
      "%d still pending" % len(after))

# ------------------------------------------------------------ 4. long-term memory
hub.call("memory_memory_add", {"text": "用户喜欢装机单式的对比，讨厌没有网卡的配置", "tags": "偏好,硬件"})
hub.call("memory_memory_add", {"text": "上周聊过抖音精选的推荐流", "tags": "聊天"})
found, err = hub.call("memory_memory_search", {"query": "装机"})
check("4. memory search finds Chinese text", not err and "网卡" in found, (found or "").splitlines()[0][:80])
recent, err2 = hub.call("memory_memory_recent", {"limit": 5})
check("4. memory_recent lists entries", not err2 and "抖音" in recent, (recent or "").splitlines()[0][:80])
forgot, _ = hub.call("memory_memory_forget", {"contains": "装机", "confirm": True})
gone, _ = hub.call("memory_memory_search", {"query": "装机"})
check("4. forget removes entries", "deleted" in (forgot or "") and "nothing in memory" in (gone or ""),
      (forgot or "").strip()[:60])

# ------------------------------------- 5. job results were auto-indexed to memory
indexed, _ = hub.call("memory_memory_search", {"query": "两步计算"})
check("5. finished jobs are auto-indexed", "两步计算" in (indexed or ""), (indexed or "").splitlines()[0][:80])

# ------------------------------------------------------- 6. metrics + cancelling
metrics = urllib.request.urlopen("http://127.0.0.1:%d/metrics" % BRIDGE_PORT, timeout=10).read().decode()
check("6. /metrics exposes counters", "mcp_hands_tools" in metrics and "mcp_hands_jobs_total" in metrics,
      metrics.splitlines()[0] if metrics else "")
slow = post("/v2/jobs", {"steps": [{"tool": "calc_calc", "args": {"expression": "9*9"}}],
                         "title": "取消我", "notify": False})
cancelled = post("/v2/jobs/%s/cancel" % slow["job"]["id"], {})
check("6. a queued job can be cancelled", (cancelled.get("job") or {}).get("status") == "cancelled",
      str((cancelled.get("job") or {}).get("status")))

# ------------------------------------------------- 7. upstream retry on 503 errors
retry_reply = ask_proxy("FLAKY:2 ECHO_SYS")["choices"][0]["message"]["content"]
check("7. upstream 503 is retried", "SYS>>>" in retry_reply or "工具结果" in retry_reply,
      retry_reply.replace("\n", " | ")[:100])

# ------------------------------------------------------------ 8. config migration
old = os.path.join(TMP, "mcp-hands-old-config.json")
with open(old, "w", encoding="utf-8") as fh:
    json.dump({"listen": {"host": "127.0.0.1", "port": 8877},
               "upstream": {"name": "x", "base_url": "u", "api_key": "k", "model": "m"},
               "servers": [{"name": "fs", "enabled": True}]}, fh)
message = bridge.migrate_config(old)
with open(old, encoding="utf-8") as fh:
    migrated = json.load(fh)
check("8. migrate adds the new keys", "jobs" in migrated and "memory" in migrated and "proxy" in migrated,
      message.splitlines()[0][:90])
check("8. migrate keeps the user's own values", migrated["listen"]["port"] == 8877)
check("8. migrate writes a backup", os.path.exists(old + ".bak"))
check("8. migrate is idempotent", "already up to date" in bridge.migrate_config(old))

service.stop()
server.shutdown()
bridge.stop_jobs()
bridge.stop_hub()
for path in (CONFIG, os.environ["MCP_MEMORY_FILE"], os.path.join(TMP, "mcp-hands-jobs-v2.db"), old,
             old + ".bak", os.path.join(TMP, "mcp-hands-jobs-test.db")):
    try:
        os.remove(path)
    except OSError:
        pass

failed = [label for label, ok in results if not ok]
print("\nfailed:", failed or "none")
raise SystemExit(1 if failed else 0)
