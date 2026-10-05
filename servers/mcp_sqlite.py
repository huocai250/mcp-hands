"""MCP server: SQLite inspection and editing through the standard library."""
import csv
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

MAX_ROWS = 200
TEMP = os.environ.get("TEMP") or os.environ.get("TMP") or "."
SAMPLE_DIR = os.path.join(TEMP, "aiyu_mcp_samples")

srv = Server("sqlite")


def _in_path(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(p):
        raise FileNotFoundError(path)
    return p


def _out_path(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    return p


def _ident(name, fallback):
    """SQLite-safe identifier derived from a CSV header cell."""
    s = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(name).strip())
    if not s:
        s = fallback
    if s[0].isdigit():
        s = "_" + s
    return s


def _leading_keyword(sql):
    """First SQL keyword, ignoring leading whitespace and comments."""
    text = str(sql)
    while True:
        text = text.lstrip()
        match = re.match(r"^(?:--[^\n]*(?:\n|$)|/\*.*?\*/)", text, flags=re.S)
        if not match or not match.group(0):
            break
        text = text[match.end():]
    words = text.split(None, 1)
    return words[0].strip(";").upper() if words else ""


def _render(cur, limit=MAX_ROWS):
    """Header row, 'a | b | c' body, truncation notice and a row-count footer."""
    cols = [d[0] for d in (cur.description or [])]
    lines = []
    if cols:
        lines.append(" | ".join(str(c) for c in cols))
    rows = 0
    for row in cur:
        rows += 1
        if rows <= limit:
            lines.append(" | ".join("" if v is None else str(v) for v in row))
    if rows > limit:
        lines.append("...[truncated at %d of %d rows]" % (limit, rows))
    lines.append("(%d rows)" % rows)
    return "\n".join(lines)
@srv.tool("sqlite_tables", "List user tables in a database file.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def sqlite_tables(path):
    p = _in_path(path)
    con = sqlite3.connect(p)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    finally:
        con.close()
    return "tables=%d\n%s" % (len(names), "\n".join(names) if names else "(none)")


@srv.tool("sqlite_schema", "Show DDL for one table, or for every object.",
          {"type": "object", "properties": {"path": {"type": "string"}, "table": {"type": "string", "default": ""}}, "required": ["path"]})
def sqlite_schema(path, table=""):
    p = _in_path(path)
    con = sqlite3.connect(p)
    try:
        if table:
            rows = con.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name = ? ORDER BY type", (str(table),)).fetchall()
            if not rows:
                raise FileNotFoundError("no object named %r in %s" % (table, p))
        else:
            rows = con.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name").fetchall()
    finally:
        con.close()
    blocks = ["%s %s:\n%s" % (kind, name, sql) for kind, name, sql in rows]
    return "objects=%d\n\n%s" % (len(blocks), "\n\n".join(blocks) if blocks else "(no schema)")


@srv.tool("sqlite_query", "Run a read-only SELECT / WITH / PRAGMA query and render its rows.",
          {"type": "object", "properties": {"path": {"type": "string"}, "sql": {"type": "string"}, "params": {"type": "array", "items": {}}}, "required": ["path", "sql"]})
def sqlite_query(path, sql, params=[]):
    p = _in_path(path)
    keyword = _leading_keyword(sql)
    if keyword not in ("SELECT", "WITH", "PRAGMA"):
        raise ValueError("sqlite_query accepts only SELECT / WITH / PRAGMA, got %r; use sqlite_execute for writes, DDL or other statements" % (keyword or str(sql)[:40]))
    con = sqlite3.connect(p)
    try:
        cur = con.execute(str(sql), list(params or []))
        if cur.description is None:
            return "(no result set)\n(0 rows)"
        return _render(cur)
    finally:
        con.close()


@srv.tool("sqlite_execute", "Run one writing/DDL statement and report rows affected.",
          {"type": "object", "properties": {"path": {"type": "string"}, "sql": {"type": "string"}, "params": {"type": "array", "items": {}}}, "required": ["path", "sql"]})
def sqlite_execute(path, sql, params=[]):
    p = _out_path(path)
    con = sqlite3.connect(p)
    try:
        before = con.total_changes
        cur = con.execute(str(sql), list(params or []))
        con.commit()
        changed = con.total_changes - before
        rowcount = cur.rowcount
    finally:
        con.close()
    head = (str(sql).strip().splitlines() or [""])[0][:80]
    return "ok: %s\nrowcount=%s total_changes=%d\nfile=%s" % (head, rowcount, changed, p)
@srv.tool("sqlite_import_csv", "Create/fill a table from a CSV file (all columns TEXT).",
          {"type": "object", "properties": {"path": {"type": "string"}, "table": {"type": "string"}, "csv_path": {"type": "string"}, "has_header": {"type": "boolean", "default": True}}, "required": ["path", "table", "csv_path"]})
def sqlite_import_csv(path, table, csv_path, has_header=True):
    db = _out_path(path)
    src = _in_path(csv_path)
    name = _ident(table, "t")
    with open(src, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise ValueError("csv has no rows: %s" % src)
    if has_header:
        header, data = rows[0], rows[1:]
    else:
        header, data = ["c%d" % (i + 1) for i in range(len(rows[0]))], rows
    cols = [_ident(c, "c%d" % (i + 1)) for i, c in enumerate(header)]
    if not cols:
        cols = ["c1"]
    ddl = ", ".join('"%s" TEXT' % c for c in cols)
    insert = 'INSERT INTO "%s" (%s) VALUES (%s)' % (
        name, ", ".join('"%s"' % c for c in cols), ", ".join(["?"] * len(cols)))
    padded = [(list(r[:len(cols)]) + [None] * (len(cols) - len(list(r[:len(cols)])))) for r in data]
    con = sqlite3.connect(db)
    try:
        con.execute('CREATE TABLE IF NOT EXISTS "%s" (%s)' % (name, ddl))
        con.executemany(insert, padded)
        con.commit()
        total = con.execute('SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
    finally:
        con.close()
    return "imported %d row(s) into %s (%d column(s)); table now holds %d row(s)" % (len(padded), name, len(cols), total)


@srv.tool("sqlite_export_csv", "Write the rows of a query to a CSV file.",
          {"type": "object", "properties": {"path": {"type": "string"}, "sql": {"type": "string"}, "csv_path": {"type": "string"}}, "required": ["path", "sql", "csv_path"]})
def sqlite_export_csv(path, sql, csv_path):
    db = _in_path(path)
    dst = _out_path(csv_path)
    con = sqlite3.connect(db)
    count = 0
    cols = []
    try:
        cur = con.execute(str(sql), [])
        cols = [d[0] for d in (cur.description or [])]
        with open(dst, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            if cols:
                writer.writerow(cols)
            for row in cur:
                writer.writerow(["" if v is None else v for v in row])
                count += 1
    finally:
        con.close()
    return "exported %d row(s), %d column(s) -> %s (sql=%s)" % (count, len(cols), dst, str(sql).strip()[:60])


@srv.tool("db_stats", "Report file size, table count, per-table row counts and page usage.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def db_stats(path):
    p = _in_path(path)
    con = sqlite3.connect(p)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        lines = ["file=%s" % p, "file_bytes=%d" % os.path.getsize(p), "tables=%d" % len(names)]
        for name in names[:50]:
            quoted = name.replace('"', '""')
            count = con.execute('SELECT COUNT(*) FROM "%s"' % quoted).fetchone()[0]
            lines.append("  %-28s rows=%d" % (name, count))
        if len(names) > 50:
            lines.append("  ...[%d more table(s)]" % (len(names) - 50))
        page_size = con.execute("PRAGMA page_size").fetchone()[0]
        page_count = con.execute("PRAGMA page_count").fetchone()[0]
        lines.append("page_size=%d page_count=%d" % (page_size, page_count))
    finally:
        con.close()
    return "\n".join(lines)


_DB = os.path.join(SAMPLE_DIR, "sample.db")
_CSV = os.path.join(SAMPLE_DIR, "sample_out.csv")

# Ordered so every reader sees the file the earlier sample produced.
SAMPLES = {
    "sqlite_execute": {"path": _DB, "sql": "CREATE TABLE IF NOT EXISTS sample (id INTEGER, name TEXT)", "params": []},
    "sqlite_tables": {"path": _DB},
    "sqlite_query": {"path": _DB, "sql": "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name", "params": []},
    "sqlite_schema": {"path": _DB, "table": "sample"},
    "sqlite_export_csv": {"path": _DB, "sql": "SELECT * FROM sample", "csv_path": _CSV},
    "sqlite_import_csv": {"path": _DB, "table": "sample", "csv_path": _CSV, "has_header": True},
    "db_stats": {"path": _DB},
}


def build():
    return srv


if __name__ == "__main__":
    srv.run()


