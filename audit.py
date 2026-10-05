"""Audit trail for mcp-hands 3.0.

Every tool call made through the hub can be recorded here: who asked (profile), what
was called, which arguments (secrets masked), whether it worked, how long it took.
Storage is SQLite so it survives restarts and can be queried from the dashboard.

The point is accountability: with 350+ tools that can run shell commands, touch the
registry and drive the mouse, "what did it actually do" must be answerable.
"""
import json
import os
import re
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL DEFAULT 0,
  profile TEXT DEFAULT '',
  tool    TEXT DEFAULT '',
  args    TEXT DEFAULT '',
  ok      INTEGER DEFAULT 1,
  error   TEXT DEFAULT '',
  ms      INTEGER DEFAULT 0,
  chars   INTEGER DEFAULT 0,
  source  TEXT DEFAULT 'chat'
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS calls_tool ON calls(tool);
"""

SECRET_HINTS = ("api_key", "apikey", "authorization", "token", "password", "secret")


def mask_args(args):
    """Never store credentials in the audit log."""
    if not isinstance(args, dict):
        return args
    safe = {}
    for key, value in args.items():
        if any(hint in str(key).lower() for hint in SECRET_HINTS) and isinstance(value, str) and value:
            safe[key] = (value[:6] + "…" + value[-4:]) if len(value) > 12 else "***"
        else:
            safe[key] = value
    return safe


class AuditStore:
    def __init__(self, path, max_rows=20000):
        self.path = path
        self.max_rows = int(max_rows)
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

    def record(self, tool, args=None, ok=True, error="", ms=0, chars=0,
               profile="", source="chat"):
        row = (time.time(), str(profile or ""), str(tool or ""),
               json.dumps(mask_args(args), ensure_ascii=False)[:2000],
               1 if ok else 0, str(error or "")[:500], int(ms), int(chars), str(source or "chat"))
        with self._lock:
            self._conn.execute("INSERT INTO calls (ts, profile, tool, args, ok, error, ms, chars, source) "
                               "VALUES (?,?,?,?,?,?,?,?,?)", row)
            self._conn.commit()
            count = self._conn.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"]
            if count > self.max_rows:
                self._conn.execute("DELETE FROM calls WHERE id IN "
                                   "(SELECT id FROM calls ORDER BY id LIMIT ?)", (count - self.max_rows,))
                self._conn.commit()

    def tail(self, limit=50, tool="", profile="", only_errors=False):
        query = "SELECT * FROM calls WHERE 1=1"
        params = []
        if tool:
            query += " AND tool LIKE ?"
            params.append("%" + str(tool) + "%")
        if profile:
            query += " AND profile = ?"
            params.append(str(profile))
        if only_errors:
            query += " AND ok = 0"
        query += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["args"] = json.loads(item["args"] or "{}")
            except (TypeError, ValueError):
                pass
            item["ok"] = bool(item["ok"])
            item["when"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["ts"] or 0))
            out.append(item)
        return out

    def stats(self, since_seconds=86400):
        cutoff = time.time() - float(since_seconds)
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS n FROM calls").fetchone()["n"]
            window = self._conn.execute("SELECT COUNT(*) AS n, SUM(ok = 0) AS bad FROM calls WHERE ts >= ?",
                                        (cutoff,)).fetchone()
            top = self._conn.execute(
                "SELECT tool, COUNT(*) AS n FROM calls WHERE ts >= ? GROUP BY tool ORDER BY n DESC LIMIT 8",
                (cutoff,)).fetchall()
            by_profile = self._conn.execute(
                "SELECT profile, COUNT(*) AS n FROM calls WHERE ts >= ? GROUP BY profile", (cutoff,)).fetchall()
        return {"total": total,
                "since_seconds": int(since_seconds),
                "window_calls": window["n"] or 0,
                "window_failures": window["bad"] or 0,
                "top_tools": [{"tool": r["tool"], "calls": r["n"]} for r in top],
                "by_profile": [{"profile": r["profile"] or "-", "calls": r["n"]} for r in by_profile]}

    def purge(self, older_than_seconds):
        cutoff = time.time() - float(older_than_seconds)
        with self._lock:
            cursor = self._conn.execute("DELETE FROM calls WHERE ts < ?", (cutoff,))
            self._conn.commit()
        return cursor.rowcount


# --------------------------------------------------------------- policy decisions
def _glob_match(pattern, name):
    """Tiny glob: '*' matches anything, '?' one char; matched case-insensitively."""
    regex = "^" + re.escape(str(pattern)).replace(r"\*", ".*").replace(r"\?", ".") + "$"
    return re.match(regex, str(name), re.IGNORECASE) is not None


class Policy:
    """Decide whether a tool may run. Defaults are permissive; a config can restrict.

        {"mode": "enforce", "deny": ["shell_*"], "allow": [], "max_calls_per_minute": 120}
    """

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.mode = str(cfg.get("mode") or "audit").lower()
        self.deny = [str(p) for p in (cfg.get("deny") or [])]
        self.allow = [str(p) for p in (cfg.get("allow") or [])]
        self.deny_paths = [str(p) for p in (cfg.get("deny_paths") or [])]
        self.rate_limit = int(cfg.get("max_calls_per_minute") or 0)
        self.exempt = [str(p) for p in (cfg.get("exempt") or [])]
        self._calls = []
        self._lock = threading.Lock()

    def describe(self):
        return {"mode": self.mode, "deny": self.deny, "allow": self.allow,
                "deny_paths": self.deny_paths, "max_calls_per_minute": self.rate_limit,
                "exempt": self.exempt}

    def _rate_ok(self, now):
        if not self.rate_limit:
            return True, ""
        with self._lock:
            self._calls = [t for t in self._calls if now - t < 60]
            if len(self._calls) >= self.rate_limit:
                return False, "rate limit: %d calls/minute reached" % self.rate_limit
            self._calls.append(now)
        return True, ""

    def check(self, tool, args=None):
        """Returns (allowed, reason). 'audit' mode only records, never blocks."""
        if any(_glob_match(p, tool) for p in self.exempt):
            return True, ""
        blocked = any(_glob_match(p, tool) for p in self.deny)
        if self.allow:
            blocked = blocked or not any(_glob_match(p, tool) for p in self.allow)
        if self.deny_paths:
            blob = json.dumps(args or {}, ensure_ascii=False)
            for path in self.deny_paths:
                if path and path.lower() in blob.lower():
                    blocked = True
                    break
        rate_ok, reason = True, ""
        if blocked:
            reason = "tool '%s' is not allowed by policy" % tool
        else:
            rate_ok, reason = self._rate_ok(time.time())
        if not blocked and rate_ok:
            return True, ""
        if self.mode == "enforce":
            return False, reason
        return True, ""      # audit mode: log the intent, still run it
