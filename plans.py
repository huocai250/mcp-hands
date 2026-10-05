"""Plans for mcp-hands 3.0: a goal that outlives a single chat turn.

The phone app can only hold one request, and a real task ("整理下载目录并挑出三条
好看的抖音") needs many steps with memory of what is already done. A plan is stored
in SQLite: goal + ordered steps + evidence per step. Each turn the persona asks for
the next step, does it, records what actually happened, and the plan survives even if
the app, the bridge or the PC restarts.

Evidence is the point: a step is not "done" unless a real tool result was recorded.
"""
import json
import os
import sqlite3
import threading
import time
import uuid

STEP_STATUSES = ("todo", "doing", "done", "failed", "skipped")
PLAN_STATUSES = ("active", "done", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
  id      TEXT PRIMARY KEY,
  title   TEXT DEFAULT '',
  goal    TEXT DEFAULT '',
  profile TEXT DEFAULT '',
  status  TEXT DEFAULT 'active',
  steps   TEXT DEFAULT '[]',
  created REAL DEFAULT 0,
  updated REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS plans_status ON plans(status, created);
"""


class PlanStore:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _decode(row):
        if row is None:
            return None
        plan = dict(row)
        try:
            plan["steps"] = json.loads(plan.get("steps") or "[]")
        except (TypeError, ValueError):
            plan["steps"] = []
        plan["done"] = sum(1 for s in plan["steps"] if s.get("status") == "done")
        plan["total"] = len(plan["steps"])
        return plan

    @staticmethod
    def _clean_steps(steps):
        out = []
        for item in steps or []:
            if isinstance(item, str):
                out.append({"text": item.strip(), "tool": "", "args": {}, "status": "todo", "evidence": ""})
                continue
            if not isinstance(item, dict):
                continue
            out.append({
                "text": str(item.get("text") or item.get("step") or "").strip(),
                "tool": str(item.get("tool") or ""),
                "args": item.get("args") if isinstance(item.get("args"), dict) else {},
                "status": item.get("status") if item.get("status") in STEP_STATUSES else "todo",
                "evidence": str(item.get("evidence") or ""),
                "updated": time.time(),
            })
        return out

    def _save(self, plan_id, steps):
        with self._lock:
            self._conn.execute("UPDATE plans SET steps = ?, updated = ? WHERE id = ?",
                               (json.dumps(steps, ensure_ascii=False), time.time(), plan_id))
            self._conn.commit()
        return self.get(plan_id)

    # ------------------------------------------------------------------- writes
    def create(self, goal, steps=None, title="", profile=""):
        goal = str(goal or "").strip()
        steps = self._clean_steps(steps)
        if not goal and not steps:
            raise ValueError("a plan needs a goal or at least one step")
        plan_id = "plan_" + uuid.uuid4().hex[:10]
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO plans (id, title, goal, profile, status, steps, created, updated) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (plan_id, str(title or goal[:60]), goal, str(profile or ""), "active",
                 json.dumps(steps, ensure_ascii=False), now, now))
            self._conn.commit()
        return self.get(plan_id)

    def add_step(self, plan_id, text, tool="", args=None):
        plan = self.get(plan_id)
        if not plan:
            return None
        steps = plan["steps"] + self._clean_steps([{"text": text, "tool": tool, "args": args or {}}])
        return self._save(plan_id, steps)

    def mark_step(self, plan_id, index_or_text, status, evidence="", error=""):
        plan = self.get(plan_id)
        if not plan:
            return None
        steps = plan["steps"]
        target = None
        if isinstance(index_or_text, int) or str(index_or_text).isdigit():
            index = int(index_or_text) - 1     # 1-based for humans
            if 0 <= index < len(steps):
                target = steps[index]
        else:
            needle = str(index_or_text).strip()
            target = next((s for s in steps if needle and needle in s.get("text", "")), None)
        if target is None:
            return None
        if status not in STEP_STATUSES:
            return None
        target["status"] = status
        if evidence:
            target["evidence"] = str(evidence)[:2000]
        if error:
            target["evidence"] = ("FAILED: %s" % error)[:2000]
        target["updated"] = time.time()
        plan = self._save(plan_id, steps)
        # A plan with every step settled is finished.
        if plan and all(s["status"] in ("done", "failed", "skipped") for s in plan["steps"]):
            plan = self.update(plan_id, status="done")
        return plan

    def update(self, plan_id, **fields):
        if not fields:
            return self.get(plan_id)
        columns = ", ".join("%s = ?" % key for key in fields)
        with self._lock:
            self._conn.execute("UPDATE plans SET %s, updated = ? WHERE id = ?" % columns,
                               [fields[key] for key in fields] + [time.time(), plan_id])
            self._conn.commit()
        return self.get(plan_id)

    def cancel(self, plan_id):
        return self.update(plan_id, status="cancelled")

    # -------------------------------------------------------------------- reads
    def get(self, plan_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM plans WHERE id = ?", (str(plan_id),)).fetchone()
        return self._decode(row)

    def list(self, status=None, limit=20, profile=""):
        query = "SELECT * FROM plans WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(str(status))
        if profile:
            query += " AND profile = ?"
            params.append(str(profile))
        query += " ORDER BY updated DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._decode(row) for row in rows]

    def next_step(self, plan_id):
        """(index, step) of the first unfinished step, or (None, None)."""
        plan = self.get(plan_id)
        if not plan or plan["status"] != "active":
            return None, None
        for index, step in enumerate(plan["steps"], 1):
            if step.get("status") in ("todo", "doing"):
                return index, step
        return None, None

    def stats(self):
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM plans GROUP BY status").fetchall()
        plans = self.list(limit=200)
        pending_steps = sum(1 for p in plans for s in p["steps"] if s.get("status") in ("todo", "doing"))
        return {"by_status": {r["status"]: r["n"] for r in rows},
                "plans": sum(r["n"] for r in rows), "open_steps": pending_steps}
