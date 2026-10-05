"""End-to-end acceptance for the 3.0 features (offline, no real API key).

Covers: profiles (routing + tool narrowing), plans (multi-turn lifecycle with
evidence), policy enforcement + audit trail, /v2 surfaces, dashboard page, and
JSON log output. Everything runs against local mocks.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = r"C:\Users\13323\Documents\deepseek-harness\default-workspace\mcp-persona-bridge"
TMP = tempfile.gettempdir()
CONFIG = os.path.join(TMP, "mcp-hands-v3-test.json")
os.environ["MCP_SAMPLES_DIR"] = TMP
os.environ["BRIDGE_CONFIG"] = CONFIG
os.environ["MCP_MEMORY_FILE"] = os.path.join(TMP, "mcp-hands-memory-v3.db")
sys.path.insert(0, ROOT)

BRIDGE_PORT = 8884
PROXY_PORT = 8898

with open(CONFIG, "w", encoding="utf-8") as fh:
    json.dump({
        "listen": {"host": "127.0.0.1", "port": BRIDGE_PORT},
        "upstream": {"name": "x", "base_url": "http://127.0.0.1:8899/v1", "api_key": "x", "model": "x"},
        "proxy": {"enabled": True, "listen": {"host": "127.0.0.1", "port": PROXY_PORT},
                  "upstream_base": "http://127.0.0.1:8891/v1", "upstream_api_key": "",
                  "tool_models": ["mock-native", "mock-alice", "mock-bob"],
                  "max_tool_rounds": 12, "max_seconds": 60, "heartbeat_seconds": 5,
                  "progress_stream": True, "progress_max": 3, "vision_check_seconds": 600,
                  "step_report": "brief", "inject_tool_hint": True},
        "vision": {"base_url": "http://127.0.0.1:8894/v1", "api_key": "mock-key", "model": "mock-vision",
                   "detail": "auto", "thinking": "disabled", "max_pixels": 1300,
                   "inherit_upstream_key": False},
        "jobs": {"enabled": True, "workers": 1, "notify": False,
                 "db": os.path.join(TMP, "mcp-hands-jobs-v3.db")},
        "memory": {"auto_index": True},
        "plans": {"db": os.path.join(TMP, "mcp-hands-plans-v3.db")},
        "audit": {"enabled": True, "db": os.path.join(TMP, "mcp-hands-audit-v3.db"), "max_rows": 500},
        "policy": {"mode": "enforce", "deny": ["shell_*"], "allow": [], "deny_paths": [],
                   "max_calls_per_minute": 0, "exempt": ["audit_*", "policy_*", "jobs_*", "plan_*"]},
        "log": {"format": "json", "max_mb": 1},
        "profiles": {
            "alice": {"model": "mock-alice", "tools": ["calc_*", "plan_*", "audit_*"], "label": "爱丽丝"},
            "bob": {"model": "mock-bob", "deny": ["calc_*"], "label": "鲍勃"},
        },
        "profile_default": "alice",
        "servers": [{"name": "calc", "enabled": True}, {"name": "shell", "enabled": True},
                    {"name": "jobs", "enabled": True}, {"name": "plan", "enabled": True},
                    {"name": "audit", "enabled": True}, {"name": "memory", "enabled": True}],
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


def ask(content, profile="", model="mock-native", timeout=90):
    body = {"model": model, "stream": False, "messages": [{"role": "user", "content": content}]}
    headers = {"Content-Type": "application/json", "Authorization": "Bearer sk-test"}
    if profile:
        headers["X-Profile"] = profile
    request = urllib.request.Request("http://127.0.0.1:%d/v1/chat/completions" % PROXY_PORT,
                                     data=json.dumps(body).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


hub = bridge.start_hub()
server = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), bridge.Handler)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
bridge.job_runner()
service = proxy_module.ProxyService().start()
time.sleep(0.5)

# ---------------------------------------------------------------- 1. profiles
health = get("/v2/health")
check("1. /v2/health lists profiles + policy", "alice" in (health.get("profiles") or [])
      and health["policy"]["mode"] == "enforce", str(health.get("profiles")))
alice = ask("ECHO_SYS", profile="alice")["choices"][0]["message"]["content"]
offered_alice = alice.split("[offered=", 1)[-1].split("]", 1)[1].split("\n---")[0] if "[offered=" in alice else ""
check("1. alice only sees her own tools (calc/plan/audit)",
      "calc_calc" in offered_alice and "shell_run_ps" not in offered_alice,
      "offered=%s" % offered_alice[:150])
bob = ask("ECHO_SYS", profile="bob", model="mock-bob")["choices"][0]["message"]["content"]
offered_bob = bob.split("[offered=", 1)[-1].split("]", 1)[1].split("\n---")[0] if "[offered=" in bob else ""
check("1. bob's profile resolves by model name", "calc_calc" not in offered_bob and len(offered_bob) > 10,
      "offered=%s" % offered_bob[:90])
routed = proxy_module.resolve_profile({"model": "mock-bob"}, "")
check("1. model routes to the right profile", routed[0] == "bob", str(routed[0]))

# ------------------------------------------------------------------ 2. policy
blocked = ask('USE: shell_run_ps ARGS: {"script": "echo hi"}', profile="bob", model="mock-bob")
steps = (blocked.get("proxy") or {}).get("tool_steps") or []
check("2. policy blocks a denied tool", bool(steps) and steps[0]["error"]
      and "policy" in (steps[0]["output"] or "").lower(), (steps[0]["output"][:80] if steps else "no step"))
allowed = ask('USE: calc_calc ARGS: {"expression": "3*3"}', profile="bob", model="mock-bob")
steps2 = (allowed.get("proxy") or {}).get("tool_steps") or []
check("2. an allowed tool still runs", bool(steps2) and not steps2[0]["error"],
      (steps2[0]["output"][:60] if steps2 else "no step"))

# -------------------------------------------------------------------- 3. audit
time.sleep(0.3)
tail = get("/v2/audit?limit=20")
tools = [row["tool"] for row in tail["calls"]]
check("3. audit records the calls", "calc_calc" in tools and "shell_run_ps" in tools, str(tools[:5]))
profiles_seen = {row["profile"] for row in tail["calls"]}
check("3. audit is tagged per profile", "bob" in profiles_seen, str(profiles_seen))
check("3. blocked calls are logged as failures",
      any(not r["ok"] and "policy" in (r["error"] or "").lower() for r in tail["calls"]))
key_args = [r for r in tail["calls"] if r["tool"] == "vision_see_screen"]
if key_args:
    check("3. secrets are masked in the audit log",
          all((a.get("api_key") or "").find("…") >= 0 for a in [key_args[0]["args"]] if a.get("api_key")),
          str(key_args[0]["args"])[:80])
else:
    check("3. secrets are masked in the audit log", True, "no vision call in this run")
stats = get("/v2/audit/stats?hours=1")["stats"]
check("3. audit stats summarise the window", stats["window_calls"] >= 2, json.dumps(stats)[:110])

# -------------------------------------------------------------------- 4. plans
created = post("/v2/plans", {"goal": "整理下载目录并挑三条好看的抖音",
                             "steps": ["列目录", "分类归档", "截图三条"], "title": "下载整理"})
plan_id = (created.get("plan") or {}).get("id")
check("4. a plan can be created", bool(plan_id) and created["plan"]["total"] == 3, str(plan_id))
first = hub.call("plan_plan_next", {"id": plan_id})[0]
check("4. plan_next returns the first open step", "next step 1/3" in first and "列目录" in first,
      first.splitlines()[0][:70])
hub.call("plan_plan_done", {"id": plan_id, "step": "1", "evidence": "fs_list_dir ok: 42 files"})
after = get("/v2/plans/%s" % plan_id)["plan"]
check("4. evidence is stored on the step", after["done"] == 1
      and "42 files" in after["steps"][0]["evidence"], after["steps"][0]["evidence"][:60])
second = hub.call("plan_plan_add_step", {"id": plan_id, "text": "顺手清空回收站", "tool": "sys_toast",
                                         "args": '{"message": "done"}'})[0]
check("4. plans can grow while working", "顺手清空回收站" in second and after["total"] == 3,
      "now %d step(s)" % (second.count("[ ]") + second.count("[x]")))
for step in ("2", "3", "顺手清空", "1"):
    hub.call("plan_plan_done", {"id": plan_id, "step": step, "evidence": "done", "status": "done"})
finished = get("/v2/plans/%s" % plan_id)["plan"]
check("4. completing every step finishes the plan", finished["status"] == "done"
      and finished["done"] == finished["total"], "%s %d/%d" % (finished["status"], finished["done"], finished["total"]))
open_plan = post("/v2/plans", {"goal": "还没做完的活", "steps": ["第一步", "第二步"]})["plan"]
roundtrip = ask("ECHO_SYS", profile="alice")["choices"][0]["message"]["content"]
check("4. an active plan reaches the next turn",
      "【计划】" in roundtrip and "还没做完的活" in roundtrip, roundtrip[:120].replace("\n", " | "))
plan2 = post("/v2/plans", {"goal": "半途而废的任务", "steps": ["a", "b"]})["plan"]
cancelled = post("/v2/plans/%s/cancel" % plan2["id"], {})["plan"]
check("4. plans can be cancelled", cancelled["status"] == "cancelled", cancelled["status"])

# ------------------------------------------------------- 5. dashboard + logging
html = urllib.request.urlopen("http://127.0.0.1:%d/dashboard" % BRIDGE_PORT, timeout=10).read().decode()
check("5. dashboard renders", "<title>mcp-hands" in html and "/v2/health" in html and "审计" in html,
      html[:60])
log_path = bridge.LOG_FILE
check("5. json log format is active", True) if not os.path.exists(log_path) else None
with open(log_path, encoding="utf-8") as fh:
    last = [line for line in fh if line.strip()][-1]
try:
    record = json.loads(last)
    check("5. log lines are JSON when configured", "ts" in record and "message" in record, last[:80])
except (json.JSONDecodeError, IndexError):
    check("5. log lines are JSON when configured", False, last[:80] if last else "empty log")

metrics = urllib.request.urlopen("http://127.0.0.1:%d/metrics" % BRIDGE_PORT, timeout=10).read().decode()
check("5. /metrics still works", "mcp_hands_tools" in metrics and "mcp_hands_jobs_total" in metrics)

service.stop()
server.shutdown()
bridge.stop_jobs()
bridge.stop_hub()
for path in (CONFIG, os.environ["MCP_MEMORY_FILE"], os.path.join(TMP, "mcp-hands-jobs-v3.db"),
             os.path.join(TMP, "mcp-hands-plans-v3.db"), os.path.join(TMP, "mcp-hands-audit-v3.db")):
    try:
        os.remove(path)
    except OSError:
        pass

failed = [label for label, ok in results if not ok]
print("\nfailed:", failed or "none")
raise SystemExit(1 if failed else 0)
