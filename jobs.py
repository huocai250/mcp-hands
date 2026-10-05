"""Durable background jobs for mcp-hands 2.0.

Why: a phone app holds one HTTP request open, so anything long has to happen on the
PC. This module keeps a small SQLite queue of jobs, runs them on worker threads
through the same tool hub the chat uses, and remembers the results until the next
turn - when the proxy hands them to the persona so she can report what happened.

A job is either a single tool call, or a plan of several calls run in order:

    {"tool": "vision_see_screen", "args": {"question": "屏幕上是什么"}}
    {"steps": [{"tool": "desktop_press_keys", "args": {"keys": "pagedown"}},
               {"tool": "sys_screenshot", "args": {}},
               {"tool": "vision_see_image", "args": {"path": "C:/shot.png"}}]}

Nothing here talks to the network: it is storage + threads, so it is unit-testable.
"""
import json
import os
import sqlite3
import threading
import time
import uuid

STATUSES = ("pending", "running", "done", "failed", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id        TEXT PRIMARY KEY,
  kind      TEXT DEFAULT 'task',
  title     TEXT DEFAULT '',
  plan      TEXT DEFAULT '[]',
  status    TEXT DEFAULT 'pending',
  progress  INTEGER DEFAULT 0,
  total     INTEGER DEFAULT 0,
  result    TEXT DEFAULT '',
  error     TEXT DEFAULT '',
  created   REAL DEFAULT 0,
  started   REAL DEFAULT 0,
  finished  REAL DEFAULT 0,
  delivered INTEGER DEFAULT 0,
  notify    INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created);
"""


def _now():
    return time.time()


def _plan_of(tool="", args=None, steps=None, plan=None):
    """Normalise the three accepted shapes into a list of steps."""
    if plan:
        return list(plan)
    if steps:
        return [{"tool": s.get("tool", ""), "args": s.get("args") or {}} for s in steps]
    if tool:
        return [{"tool": tool, "args": args or {}}]
    return []


class JobStore:
    """SQLite-backed job log. One connection, guarded by a lock (thread safe)."""

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

    # ------------------------------------------------------------------ writes
    def add(self, tool="", args=None, title="", kind="task", steps=None, plan=None, notify=True):
        steps = _plan_of(tool, args, steps, plan)
        if not steps:
            raise ValueError("a job needs a tool or a plan of steps")
        job_id = "job_" + uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, kind, title, plan, status, total, created, notify) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (job_id, str(kind or "task"), str(title or steps[0].get("tool", "")),
                 json.dumps(steps, ensure_ascii=False), "pending", len(steps), _now(), 1 if notify else 0))
            self._conn.commit()
        return self.get(job_id)

    def update(self, job_id, **fields):
        if not fields:
            return self.get(job_id)
        columns = ", ".join("%s = ?" % key for key in fields)
        with self._lock:
            self._conn.execute("UPDATE jobs SET %s WHERE id = ?" % columns,
                               [fields[key] for key in fields] + [job_id])
            self._conn.commit()
        return self.get(job_id)

    def mark_delivered(self, ids):
        if not ids:
            return 0
        with self._lock:
            self._conn.executemany("UPDATE jobs SET delivered = 1 WHERE id = ?", [(i,) for i in ids])
            self._conn.commit()
        return len(ids)

    def purge_older_than(self, seconds):
        cutoff = _now() - float(seconds)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM jobs WHERE finished > 0 AND finished < ? AND delivered = 1", (cutoff,))
            self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------- reads
    def get(self, job_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (str(job_id),)).fetchone()
        return self._row(row)

    def list(self, status=None, limit=20):
        query = "SELECT * FROM jobs"
        params = []
        if status:
            marks = ",".join("?" for _ in str(status).split(","))
            query += " WHERE status IN (%s)" % marks
            params.extend([s.strip() for s in str(status).split(",")])
        query += " ORDER BY created DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def next_pending(self):
        """Read the oldest pending job without claiming it (use claim_next to run it)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE status = 'pending' ORDER BY created LIMIT 1").fetchone()
        return self._row(row)

    def claim_next(self):
        """Atomically move the oldest pending job to 'running' and return it.

        Without the atomicity two workers can both read the same row and run the same
        plan twice - the update-with-condition makes that impossible.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM jobs WHERE status = 'pending' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            cursor = self._conn.execute(
                "UPDATE jobs SET status = 'running', started = ? WHERE id = ? AND status = 'pending'",
                (_now(), row["id"]))
            self._conn.commit()
            if cursor.rowcount != 1:
                return None
            claimed = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        return self._row(claimed)

    def pending_delivery(self, limit=5):
        """Finished jobs the persona has not reported yet."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE delivered = 0 AND status IN ('done','failed','cancelled') "
                "ORDER BY finished LIMIT ?", (int(limit),)).fetchall()
        return [self._row(row) for row in rows]

    def stats(self):
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        counts = {row["status"]: row["n"] for row in rows}
        return {"total": sum(counts.values()), "by_status": counts,
                "pending_delivery": len(self.pending_delivery(limit=50))}

    @staticmethod
    def _row(row):
        if row is None:
            return None
        item = dict(row)
        try:
            item["plan"] = json.loads(item.get("plan") or "[]")
        except (TypeError, ValueError):
            item["plan"] = []
        item["notify"] = bool(item.get("notify"))
        item["delivered"] = bool(item.get("delivered"))
        return item


class JobRunner:
    """Runs queued jobs on worker threads by calling tools through the hub.

    call_tool(name, args) -> (output, is_error);  notify(job) is optional and is how a
    finished job reaches the user without them asking (a Windows toast, for example).
    """

    def __init__(self, store, call_tool, log=print, notify=None, workers=1,
                 tool_timeout_s=180, max_result_chars=4000):
        self.store = store
        self.call_tool = call_tool
        self.log = log
        self.notify = notify
        self.workers = max(1, int(workers))
        self.tool_timeout_s = float(tool_timeout_s)
        self.max_result_chars = int(max_result_chars)
        self._stop = threading.Event()
        self._threads = []
        self._current = {}
        self._current_lock = threading.Lock()
        self.running = False

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        if self.running:
            return self
        self.running = True
        self._stop.clear()
        for index in range(self.workers):
            thread = threading.Thread(target=self._loop, name="job-worker-%d" % index, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self, wait_s=3):
        self.running = False
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=wait_s)
        self._threads = []
        return self

    def current(self):
        with self._current_lock:
            return dict(self._current)

    # ------------------------------------------------------------------- work
    def submit(self, tool="", args=None, title="", kind="task", steps=None, plan=None,
               notify=True, wait=False, wait_s=60):
        job = self.store.add(tool, args, title, kind, steps, plan, notify)
        self.log("job queued: %s (%s, %d step(s))" % (job["id"], job["title"], len(job["plan"])))
        if wait:
            return self.wait(job["id"], wait_s)
        return job

    def wait(self, job_id, timeout_s=60):
        deadline = _now() + float(timeout_s)
        while _now() < deadline:
            job = self.store.get(job_id)
            if job and job["status"] in ("done", "failed", "cancelled"):
                return job
            time.sleep(0.25)
        return self.store.get(job_id)

    def cancel(self, job_id):
        job = self.store.get(job_id)
        if not job:
            return None
        if job["status"] == "pending":
            return self.store.update(job_id, status="cancelled", finished=_now(),
                                     result="cancelled before it started")
        if job["status"] == "running":
            # Cooperative: the worker checks this flag between steps.
            with self._current_lock:
                self._current[job_id] = "cancel"
            return self.store.update(job_id, error="cancellation requested")
        return job

    def run_job(self, job_id):
        """Run one job (used by the workers, by --jobs run, and by tests)."""
        job = self.store.get(job_id)
        if not job:
            return job
        if job["status"] == "pending":
            job = self.store.update(job_id, status="running", started=_now()) or job
        elif job["status"] != "running":
            return job
        outputs = []
        failures = 0
        for index, step in enumerate(job["plan"]):
            with self._current_lock:
                if self._current.get(job_id) == "cancel":
                    self._current.pop(job_id, None)
                    return self.store.update(job_id, status="cancelled", finished=_now(),
                                             progress=index, result="\n".join(outputs),
                                             error="cancelled by user")
            name = step.get("tool") or ""
            args = step.get("args") or {}
            try:
                output, is_error = self.call_tool(name, args)
            except Exception as exc:  # noqa: BLE001 - a broken step must not kill the queue
                output, is_error = "%s: %s" % (type(exc).__name__, exc), True
            output = (output or "").strip()
            if is_error:
                failures += 1
            outputs.append("[%d/%d] %s %s\n%s" % (index + 1, len(job["plan"]), name,
                                                  "FAILED" if is_error else "ok", output[:self.max_result_chars]))
            self.log("job %s step %d/%d %s -> %s" % (job_id, index + 1, len(job["plan"]), name,
                                                     "ERROR" if is_error else "ok"))
            self.store.update(job_id, progress=index + 1)
        result = "\n\n".join(outputs)
        status = "failed" if failures == len(job["plan"]) else "done"
        error = "" if status == "done" else "%d of %d step(s) failed" % (failures, len(job["plan"]))
        # Notify before the terminal status is visible, so "finished" always means
        # "the notification (if any) has already been sent".
        final = dict(job, status=status, result=result, error=error, progress=len(job["plan"]))
        if self.notify and job["notify"]:
            try:
                self.notify(final)
            except Exception as exc:  # noqa: BLE001
                self.log("job notify failed: %s" % exc)
        job = self.store.update(job_id, status=status, finished=_now(), result=result, error=error)
        self.log("job %s %s in %.1fs" % (job_id, status, (job.get("finished") or 0) - (job.get("started") or 0)))
        return job

    def _loop(self):
        while not self._stop.is_set():
            job = self.store.claim_next()      # atomic: two workers never share a job
            if not job:
                self._stop.wait(0.4)
                continue
            try:
                self.run_job(job["id"])
            except Exception as exc:  # noqa: BLE001
                self.log("job %s crashed: %s" % (job["id"], exc))
                self.store.update(job["id"], status="failed", finished=_now(), error=str(exc))
