"""MCP server: memory -- durable, searchable long-term memory (SQLite FTS5).

The notes server is a scratchpad; this one is the persona's long-term store: every
entry is full-text indexed, survives restarts, and can carry tags plus a source so
she can tell her own memory apart from things she read.

Store location: %LOCALAPPDATA%\\mcp-hands\\memory.db (override with MCP_MEMORY_FILE).
"""
import os
import re
import sqlite3
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

DEFAULT_DB = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                          "mcp-hands", "memory.db")
DB_PATH = os.environ.get("MCP_MEMORY_FILE") or DEFAULT_DB
MAX_TEXT = 4000

srv = Server("memory")
_lock = threading.Lock()
_conn = None


def db():
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(
                "CREATE TABLE IF NOT EXISTS entries ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  text TEXT NOT NULL,"
                "  tags TEXT DEFAULT '',"
                "  source TEXT DEFAULT '',"
                "  created REAL DEFAULT 0,"
                "  hits INTEGER DEFAULT 0);"
                # trigram makes substring search work for CJK (the default tokenizer
                # treats a whole Chinese run as one token); short queries fall back to LIKE
                "CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts "
                "  USING fts5(text, tags, content='entries', content_rowid='id', tokenize='trigram');"
                "CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN"
                "  INSERT INTO entries_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);"
                "END;"
                "CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN"
                "  INSERT INTO entries_fts(entries_fts, rowid, text, tags) "
                "  VALUES('delete', old.id, old.text, old.tags);"
                "END;"
                "CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN"
                "  INSERT INTO entries_fts(entries_fts, rowid, text, tags) "
                "  VALUES('delete', old.id, old.text, old.tags);"
                "  INSERT INTO entries_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);"
                "END;")
            _conn.commit()
        return _conn


def _terms(query):
    return [t for t in re.split(r"\s+", str(query).strip()) if t][:8]


def _fts_query(text):
    """Turn user words into a safe FTS5 query: quote every term, OR them together."""
    terms = [re.sub(r'["\'*()]', "", t) for t in _terms(text)]
    terms = [t for t in terms if t]
    if not terms:
        return ""
    return " OR ".join('"%s"' % t for t in terms)


def _rows_to_text(rows):
    return "\n\n".join(
        "#%d [%s] tags=%s source=%s\n%s" % (r["id"],
                                            time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created"] or 0)),
                                            r["tags"] or "-", r["source"] or "-", r["text"][:400])
        for r in rows)


@srv.tool("memory_search", "Search long-term memory by keywords (works for Chinese substrings and English).",
          {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 8},
                                            "tag": {"type": "string", "default": ""}},
           "required": ["query"]})
def memory_search(query, limit=8, tag=""):
    terms = [re.sub(r'["\'*()%]', "", t) for t in _terms(query)]
    terms = [t for t in terms if t]
    if not terms:
        return "memory_search: give me some keywords"
    limit = max(1, int(limit))
    conn = db()
    rows = []
    match = _fts_query(query)
    if match:
        sql = ("SELECT e.*, bm25(entries_fts) AS score FROM entries_fts "
               "JOIN entries e ON e.id = entries_fts.rowid WHERE entries_fts MATCH ?")
        params = [match]
        if tag:
            sql += " AND e.tags LIKE ?"
            params.append("%" + str(tag) + "%")
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        try:
            with _lock:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        # Short queries (1-2 CJK characters) are below the trigram minimum, and older
        # SQLite builds may not support it at all: fall back to a plain substring match.
        where = " OR ".join("(text LIKE ? OR tags LIKE ?)" for _ in terms)
        params = []
        for term in terms:
            params.extend(["%" + term + "%", "%" + term + "%"])
        sql = "SELECT entries.* FROM entries WHERE " + where
        if tag:
            sql += " AND tags LIKE ?"
            params.append("%" + str(tag) + "%")
        sql += " ORDER BY created DESC LIMIT ?"
        params.append(limit)
        with _lock:
            rows = conn.execute(sql, params).fetchall()
    if not rows:
        return "nothing in memory matches %r" % query
    with _lock:
        conn.executemany("UPDATE entries SET hits = hits + 1 WHERE id = ?", [(r["id"],) for r in rows])
        conn.commit()
    return "matches=%d\n%s" % (len(rows), _rows_to_text(rows))


@srv.tool("memory_add", "Store something worth remembering long-term (survives restarts, full-text searchable).",
          {"type": "object", "properties": {"text": {"type": "string"}, "tags": {"type": "string", "default": ""},
                                            "source": {"type": "string", "default": "conversation"}},
           "required": ["text"]})
def memory_add(text, tags="", source="conversation"):
    body = str(text).strip()
    if not body:
        return "memory_add: nothing to store"
    body = body[:MAX_TEXT]
    conn = db()
    with _lock:
        cursor = conn.execute("INSERT INTO entries (text, tags, source, created) VALUES (?,?,?,?)",
                              (body, str(tags or ""), str(source or ""), time.time()))
        conn.commit()
        total = conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]
    return "stored #%d (%d chars, tags=%r) - memory now holds %d entries" % (
        cursor.lastrowid, len(body), tags or "", total)


@srv.tool("memory_recent", "The most recent memories, newest first.",
          {"type": "object", "properties": {"limit": {"type": "integer", "default": 8}}, "required": []})
def memory_recent(limit=8):
    conn = db()
    with _lock:
        rows = conn.execute("SELECT * FROM entries ORDER BY created DESC LIMIT ?",
                            (max(1, int(limit)),)).fetchall()
    if not rows:
        return "memory is empty"
    return "recent=%d\n%s" % (len(rows), "\n".join(
        "#%d [%s] %s" % (r["id"], time.strftime("%m-%d %H:%M", time.localtime(r["created"] or 0)),
                         r["text"][:120].replace("\n", " ")) for r in rows))


@srv.tool("memory_get", "Read one memory entry in full by its id.", 
          {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]})
def memory_get(id):
    conn = db()
    with _lock:
        row = conn.execute("SELECT * FROM entries WHERE id = ?", (int(id),)).fetchone()
    return "no entry #%s" % id if row is None else "#%d [%s] tags=%r source=%r\n%s" % (
        row["id"], time.strftime("%Y-%m-%d %H:%M", time.localtime(row["created"] or 0)),
        row["tags"], row["source"], row["text"])


@srv.tool("memory_forget", "Delete one memory entry by id, or every entry whose text contains a phrase (confirm=True).",
          {"type": "object", "properties": {"id": {"type": "integer", "default": 0},
                                            "contains": {"type": "string", "default": ""},
                                            "confirm": {"type": "boolean", "default": False}},
           "required": []})
def memory_forget(id=0, contains="", confirm=False):
    conn = db()
    if id:
        with _lock:
            cursor = conn.execute("DELETE FROM entries WHERE id = ?", (int(id),))
            conn.commit()
        return "deleted %d entry/entries (#%s)" % (cursor.rowcount, id)
    if not contains:
        return "give me an id= or contains= to forget something"
    if not confirm:
        with _lock:
            n = conn.execute("SELECT COUNT(*) AS n FROM entries WHERE text LIKE ?",
                             ("%" + str(contains) + "%",)).fetchone()["n"]
        return "%d entry/entries contain %r - call again with confirm=True to delete them" % (n, contains)
    with _lock:
        cursor = conn.execute("DELETE FROM entries WHERE text LIKE ?", ("%" + str(contains) + "%",))
        conn.commit()
    return "deleted %d entry/entries containing %r" % (cursor.rowcount, contains)


@srv.tool("memory_stats", "How much is in long-term memory, plus the most used tags.",
          {"type": "object", "properties": {}, "required": []})
def memory_stats():
    conn = db()
    with _lock:
        total = conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]
        chars = conn.execute("SELECT COALESCE(SUM(LENGTH(text)),0) AS n FROM entries").fetchone()["n"]
        oldest = conn.execute("SELECT MIN(created) AS t FROM entries").fetchone()["t"]
        tags = conn.execute("SELECT tags, COUNT(*) AS n FROM entries WHERE tags <> '' "
                            "GROUP BY tags ORDER BY n DESC LIMIT 5").fetchall()
    return "entries=%d chars=%d file=%s\nsince=%s\ntop tags=%s" % (
        total, chars, DB_PATH,
        time.strftime("%Y-%m-%d", time.localtime(oldest)) if oldest else "-",
        ", ".join("%s(%d)" % (t["tags"], t["n"]) for t in tags) or "-")


# No samples on purpose: a self-test must never write into the user's real memory.
SAMPLES = {}


def build():
    return srv


if __name__ == "__main__":
    srv.run()
