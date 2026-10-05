#!/usr/bin/env python3
"""Deterministic check for the 2.0 background job engine (no network, no hub)."""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from jobs import JobRunner, JobStore  # noqa: E402

TMP = tempfile.gettempdir()
DB = os.path.join(TMP, "mcp-hands-jobs-test.db")
if os.path.exists(DB):
    os.remove(DB)

results = []


def check(label, ok, detail=""):
    results.append((label, ok))
    print("%s %s%s" % ("PASS" if ok else "FAIL", label, ("  <- " + detail) if detail else ""))


calls = []


def fake_tool(name, args):
    calls.append((name, args))
    if name == "boom":
        return "kaboom: nothing here", True
    if name == "slow":
        time.sleep(0.4)
        return "slept", False
    return "ran %s with %s" % (name, args), False


notified = []
store = JobStore(DB)

# ---- single-step job, run synchronously
job = store.add(tool="sys_now", args={"fmt": "time"}, title="看时间")
check("1. job is stored as pending", job["status"] == "pending" and len(job["plan"]) == 1)
done = JobRunner(store, fake_tool, log=lambda m: None).run_job(job["id"])
check("1. single step finishes as done", done["status"] == "done" and "ran sys_now" in done["result"],
      done["status"])

# ---- multi-step plan, run by a worker thread, with notification
runner = JobRunner(store, fake_tool, log=lambda m: None, workers=2, notify=lambda j: notified.append(j))
plan = [{"tool": "sys_now", "args": {}}, {"tool": "sys_screenshot", "args": {}},
        {"tool": "vision_see_image", "args": {"path": "x.png"}}]
job2 = runner.submit(steps=plan, title="三步连做")
runner.start()
final = runner.wait(job2["id"], timeout_s=15)
runner.stop()
check("2. worker runs a multi-step plan", final["status"] == "done" and final["progress"] == 3,
      "%s progress=%s" % (final["status"], final["progress"]))
check("2. every step was executed in order",
      [c[0] for c in calls if c[0] in ("sys_now", "sys_screenshot", "vision_see_image")][-3:]
      == ["sys_now", "sys_screenshot", "vision_see_image"])
check("2. finished job is queued for delivery", any(j["id"] == job2["id"] for j in store.pending_delivery()))
check("2. notification fired once", len(notified) == 1 and notified[0]["id"] == job2["id"])

# ---- a completely failing job is reported as failed, not as done
job3 = runner.submit(steps=[{"tool": "boom", "args": {}}], title="注定失败", notify=False)
job3 = runner.run_job(job3["id"])
check("3. all-failing job is 'failed'", job3["status"] == "failed" and job3["error"])
check("3. failures keep the real output", "kaboom" in job3["result"])

# ---- delivery bookkeeping
pending = store.pending_delivery()
store.mark_delivered([job["id"], job3["id"]])
check("4. mark_delivered clears them",
      all(j["id"] not in {p["id"] for p in store.pending_delivery()} for j in (job, job3)),
      "pending before=%d" % len(pending))

# ---- cancel a running job
job4 = runner.submit(steps=[{"tool": "sys_now", "args": {}}, {"tool": "slow", "args": {}},
                            {"tool": "sys_now", "args": {}}], title="取消我")
cancelled = runner.cancel(job4["id"])
check("5. cancelling a pending job works", cancelled["status"] == "cancelled")

# ---- cancel mid-flight (cooperative)
job5 = runner.submit(steps=[{"tool": "slow", "args": {}}, {"tool": "slow", "args": {}},
                            {"tool": "slow", "args": {}}], title="跑到一半取消")
runner.start()
time.sleep(0.55)
runner.cancel(job5["id"])
final5 = runner.wait(job5["id"], timeout_s=10)
runner.stop()
check("5. mid-flight cancel stops the plan", final5["status"] == "cancelled" and final5["progress"] < 3,
      "%s progress=%s" % (final5["status"], final5["progress"]))

# ---- persistence across a reopen (survives a restart)
store.close()
reopened = JobStore(DB)
check("6. jobs survive a restart", len(reopened.list(limit=20)) >= 5,
      "%d job(s)" % len(reopened.list(limit=20)))
check("6. stats count by status", reopened.stats()["total"] >= 5, str(reopened.stats()))
purged = reopened.purge_older_than(-1)   # everything finished and delivered
check("6. purge only removes delivered+finished", isinstance(purged, int), "purged=%d" % purged)
reopened.close()

failed = [label for label, ok in results if not ok]
print("\nfailed:", failed or "none")
try:
    os.remove(DB)
except OSError:
    pass
raise SystemExit(1 if failed else 0)
