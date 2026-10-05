"""MCP server: persistent persona memory - notes, todos and an append-only journal.

Everything lives in one JSON file shaped {"notes": [], "todos": [], "log": []}:
MCP_NOTES_FILE when set, otherwise <sample_dir()>/.mcp_notes.json (created on demand).
Every mutation rewrites the store atomically (temp file + os.replace) and keeps the
previous revision next to it as a .bak copy. Stdlib only, source stays pure ASCII.
"""
import datetime
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

MAX_ROWS = 200
STORE_NAME = ".mcp_notes.json"


# --------------------------------------------------------------------- store I/O
def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _path():
    override = os.environ.get("MCP_NOTES_FILE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(sample_dir(), STORE_NAME)


def _empty():
    return {"notes": [], "todos": [], "log": []}


def _read_json(p):
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load():
    """Read the store; fall back to the .bak revision when the main file is damaged."""
    p = _path()
    if not os.path.isfile(p):
        return _empty()
    try:
        data = _read_json(p)
    except (OSError, ValueError):
        bak = p + ".bak"
        if not os.path.isfile(bak):
            raise
        data = _read_json(bak)
    if not isinstance(data, dict):
        data = _empty()
    for key in ("notes", "todos", "log"):
        if not isinstance(data.get(key), list):
            data[key] = []
    return data


def _save(data):
    p = _path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    if os.path.isfile(p):
        try:
            shutil.copy2(p, p + ".bak")
        except OSError:
            pass  # a missing backup must never block a write
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, p)
    return p


# ----------------------------------------------------------------------- helpers
def _counts(data):
    return "notes=%d todos=%d log=%d" % (len(data["notes"]), len(data["todos"]), len(data["log"]))


def _clip(text, limit=38):
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: max(1, limit - 3)] + "..."


def _limit(value, default=20):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_ROWS))


def _next_id(items, prefix):
    top = 0
    for item in items:
        raw = str(item.get("id") or "")
        if raw[:1] == prefix and raw[1:].isdigit():
            top = max(top, int(raw[1:]))
    return "%s%d" % (prefix, top + 1)


def _find(items, item_id, what):
    """Resolve an id; the alias 'last' means the newest entry."""
    want = str(item_id or "").strip()
    if want.lower() == "last":
        if not items:
            raise KeyError("store has no %s yet" % what)
        return items[-1]
    for item in items:
        if str(item.get("id")) == want:
            return item
    raise KeyError("no %s with id %s" % (what, want))


def _tags(raw):
    if isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw]
    else:
        parts = str(raw or "").replace(";", ",").split(",")
    out = []
    for part in parts:
        tag = part.strip().lower()
        if tag and tag not in out:
            out.append(tag)
    return out


def _stamp(value):
    text = str(value or "")
    return text[:16].replace("T", " ")


def _has_tag(note, tag):
    want = str(tag).strip().lower()
    if not want:
        return True
    return any(want in t for t in (note.get("tags") or []))


srv = Server("notes")


# ------------------------------------------------------------------------- notes
@srv.tool("note_add", "Store a new note and return its id.",
          {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "string", "description": "comma separated tags"}, "source": {"type": "string"}}, "required": ["title"]})
def note_add(title, body="", tags="", source=""):
    data = _load()
    note = {
        "id": _next_id(data["notes"], "n"),
        "title": str(title or "").strip() or "(untitled)",
        "body": str(body or ""),
        "tags": _tags(tags),
        "source": str(source or ""),
        "created": _now(),
        "updated": _now(),
    }
    data["notes"].append(note)
    _save(data)
    return "id=%s\n%s" % (note["id"], _counts(data))


@srv.tool("note_list", "Compact table of notes: id, date, title, tags.",
          {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}, "tag": {"type": "string"}, "order": {"type": "string", "description": "newest|oldest"}}, "required": []})
def note_list(limit=20, tag="", order="newest"):
    data = _load()
    notes = [n for n in data["notes"] if _has_tag(n, tag)]
    if str(order or "newest").strip().lower() != "oldest":
        notes = list(reversed(notes))
    cap = _limit(limit)
    rows = ["id    date              title                                  tags"]
    for note in notes[:cap]:
        rows.append("%-5s %-16s %-38s %s" % (
            note.get("id"), _stamp(note.get("updated") or note.get("created")),
            _clip(note.get("title")), ",".join(note.get("tags") or []) or "-"))
    if len(notes) > cap:
        rows.append("...[truncated: %d of %d shown]" % (cap, len(notes)))
    rows.append("notes=%d matching=%d shown=%d tag=%s order=%s" % (
        len(data["notes"]), len(notes), min(cap, len(notes)), str(tag or "-"), str(order or "newest")))
    return "\n".join(rows)


@srv.tool("note_search", "Case-insensitive search across note title, body and tags.",
          {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 20}}, "required": ["query"]})
def note_search(query, limit=20):
    data = _load()
    needle = str(query or "").strip().lower()
    if not needle:
        raise ValueError("query must not be empty")
    cap = _limit(limit)
    hits = []
    for note in reversed(data["notes"]):
        title = str(note.get("title") or "").lower()
        body = str(note.get("body") or "").lower()
        tags = " ".join(note.get("tags") or [])
        where = [label for label, hay in (("title", title), ("body", body), ("tags", tags)) if needle in hay]
        if where:
            hits.append("%-5s %-16s %-34s match=%s" % (
                note.get("id"), _stamp(note.get("created")), _clip(note.get("title"), 34), ",".join(where)))
    rows = ["hits=%d of notes=%d shown=%d" % (len(hits), len(data["notes"]), min(cap, len(hits)))]
    rows += hits[:cap]
    if not hits:
        rows.append("(no note matches %s)" % needle)
    return "\n".join(rows)


@srv.tool("note_get", "Read one full note by id ('last' = newest).",
          {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
def note_get(id):
    data = _load()
    note = _find(data["notes"], id, "note")
    return "\n".join([
        "id=%s" % note.get("id"),
        "title=%s" % (note.get("title") or ""),
        "tags=%s" % (",".join(note.get("tags") or []) or "-"),
        "source=%s" % (note.get("source") or "-"),
        "created=%s" % (note.get("created") or ""),
        "updated=%s" % (note.get("updated") or ""),
        "body_chars=%d" % len(note.get("body") or ""),
        "---",
        note.get("body") or "(empty)",
    ])


@srv.tool("note_update", "Edit a note; append=true appends the body instead of replacing it.",
          {"type": "object", "properties": {"id": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}, "tags": {"type": "string"}, "append": {"type": "boolean", "default": False}}, "required": ["id"]})
def note_update(id, title="", body="", tags="", append=False):
    data = _load()
    note = _find(data["notes"], id, "note")
    changed = []
    if str(title or "").strip():
        note["title"] = str(title).strip()
        changed.append("title")
    if str(body or ""):
        if append:
            note["body"] = ((note.get("body") or "") + "\n" + str(body)).strip("\n")
            changed.append("body+")
        else:
            note["body"] = str(body)
            changed.append("body")
    if str(tags or "").strip():
        note["tags"] = _tags(tags)
        changed.append("tags")
    if changed:
        note["updated"] = _now()
    _save(data)
    return "id=%s changed=%s updated=%s\n%s" % (
        note.get("id"), ",".join(changed) or "(none)", note.get("updated") or "-", _counts(data))


@srv.tool("note_delete", "Delete a note by id ('last' = newest).",
          {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
def note_delete(id):
    data = _load()
    note = _find(data["notes"], id, "note")
    data["notes"] = [n for n in data["notes"] if n is not note]
    _save(data)
    return "deleted=%s\n%s" % (note.get("id"), _counts(data))


# ------------------------------------------------------------------------- todos
@srv.tool("todo_add", "Add a todo item and return its id.",
          {"type": "object", "properties": {"text": {"type": "string"}, "due": {"type": "string"}, "priority": {"type": "string", "description": "low|normal|high"}}, "required": ["text"]})
def todo_add(text, due="", priority="normal"):
    data = _load()
    todo = {
        "id": _next_id(data["todos"], "t"),
        "text": str(text or "").strip() or "(empty)",
        "due": str(due or ""),
        "priority": str(priority or "normal").strip().lower() or "normal",
        "status": "open",
        "created": _now(),
        "done_at": "",
    }
    data["todos"].append(todo)
    _save(data)
    return "id=%s\n%s" % (todo["id"], _counts(data))


@srv.tool("todo_list", "List todos: open, done or all.",
          {"type": "object", "properties": {"show": {"type": "string", "description": "open|done|all"}}, "required": []})
def todo_list(show="open"):
    data = _load()
    want = str(show or "open").strip().lower()
    if want not in ("open", "done", "all"):
        raise ValueError("show must be open, done or all (got %r)" % show)
    todos = [t for t in data["todos"] if want == "all" or t.get("status") == want]
    rows = ["id    state  prio   due         text"]
    for todo in todos:
        rows.append("%-5s %-6s %-6s %-11s %s" % (
            todo.get("id"), todo.get("status"), todo.get("priority"),
            _clip(todo.get("due") or "-", 11), _clip(todo.get("text"), 46)))
    open_count = len([t for t in data["todos"] if t.get("status") == "open"])
    done_count = len([t for t in data["todos"] if t.get("status") == "done"])
    rows.append("todos=%d open=%d done=%d shown=%d" % (len(data["todos"]), open_count, done_count, len(todos)))
    return "\n".join(rows)


@srv.tool("todo_done", "Mark a todo as done ('last' = newest).",
          {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
def todo_done(id):
    data = _load()
    todo = _find(data["todos"], id, "todo")
    todo["status"] = "done"
    todo["done_at"] = _now()
    _save(data)
    return "id=%s status=done at=%s\n%s" % (todo.get("id"), todo.get("done_at"), _counts(data))


@srv.tool("todo_delete", "Delete a todo by id ('last' = newest).",
          {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
def todo_delete(id):
    data = _load()
    todo = _find(data["todos"], id, "todo")
    data["todos"] = [t for t in data["todos"] if t is not todo]
    _save(data)
    return "deleted=%s\n%s" % (todo.get("id"), _counts(data))


# ----------------------------------------------------------------------- journal
@srv.tool("memory_log", "Append one line to the permanent journal (long-term memory) and return its id.",
          {"type": "object", "properties": {"text": {"type": "string"}, "kind": {"type": "string", "description": "event|decision|session|milestone"}}, "required": ["text"]})
def memory_log(text, kind="event"):
    data = _load()
    entry = {
        "id": _next_id(data["log"], "l"),
        "kind": str(kind or "event").strip().lower() or "event",
        "text": str(text or "").strip() or "(empty)",
        "created": _now(),
    }
    data["log"].append(entry)
    _save(data)
    return "id=%s\n%s" % (entry["id"], _counts(data))


@srv.tool("memory_recent", "The newest journal entries, so the persona can answer 'where were we'.",
          {"type": "object", "properties": {"limit": {"type": "integer", "default": 10}}, "required": []})
def memory_recent(limit=10):
    data = _load()
    cap = _limit(limit, 10)
    entries = list(reversed(data["log"]))
    rows = ["log=%d shown=%d" % (len(data["log"]), min(cap, len(entries)))]
    for entry in entries[:cap]:
        rows.append("%-16s [%-9s] %s" % (_stamp(entry.get("created")), entry.get("kind"), _clip(entry.get("text"), 70)))
    if not entries:
        rows.append("(journal is empty)")
    return "\n".join(rows)


@srv.tool("notes_export", "Dump notes, todos and the journal to one Markdown file and return its path.",
          {"type": "object", "properties": {"out": {"type": "string", "description": "target path; empty = next to the store"}}, "required": []})
def notes_export(out=""):
    data = _load()
    target = str(out or "").strip() or (os.path.splitext(_path())[0] + ".export.md")
    p = os.path.abspath(os.path.expanduser(target))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    lines = ["# Persona memory", "", "generated: %s" % _now(), "",
             "## Notes (%d)" % len(data["notes"]), ""]
    for note in data["notes"]:
        lines.append("### %s - %s" % (note.get("id"), note.get("title") or "(untitled)"))
        meta = ["created %s" % (note.get("created") or "-")]
        if note.get("updated") and note.get("updated") != note.get("created"):
            meta.append("updated %s" % note.get("updated"))
        if note.get("tags"):
            meta.append("tags " + ", ".join(note.get("tags")))
        if note.get("source"):
            meta.append("source " + _clip(note.get("source"), 60))
        lines.append("_" + " | ".join(meta) + "_")
        lines.append("")
        lines.append(note.get("body") or "(empty)")
        lines.append("")
    lines += ["## Todos (%d)" % len(data["todos"]), ""]
    for todo in data["todos"]:
        box = "x" if todo.get("status") == "done" else " "
        extra = " (prio %s%s)" % (todo.get("priority"), ", due %s" % todo.get("due") if todo.get("due") else "")
        lines.append("- [%s] %s - %s%s" % (box, todo.get("id"), todo.get("text"), extra))
    if not data["todos"]:
        lines.append("(none)")
    lines += ["", "## Journal (%d)" % len(data["log"]), ""]
    for entry in data["log"]:
        lines.append("- %s [%s] %s" % (entry.get("created"), entry.get("kind"), entry.get("text")))
    if not data["log"]:
        lines.append("(none)")
    body = "\n".join(lines).rstrip("\n") + "\n"
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)
    return "path=%s\nlines=%d\nbytes=%d\n%s" % (p, len(body.splitlines()), len(body.encode("utf-8")), _counts(data))


_SAMPLE_EXPORT = os.path.join(os.environ.get("TEMP") or sample_dir(), ".mcp_notes_export.md")

# Declaration order matters: every writer runs before the readers that need it, and
# the readers address entries by the "last" alias so a re-run stays green.
SAMPLES = {
    "note_add": {"title": "self-test note", "body": "first line\nsecond line", "tags": "selftest, memory", "source": "mcp_notes"},
    "note_list": {"limit": 5, "tag": "", "order": "newest"},
    "note_search": {"query": "self-test", "limit": 5},
    "note_update": {"id": "last", "title": "", "body": "appended line", "tags": "", "append": True},
    "note_get": {"id": "last"},
    "todo_add": {"text": "self-test todo", "due": "2025-01-01", "priority": "normal"},
    "todo_list": {"show": "open"},
    "todo_done": {"id": "last"},
    "memory_log": {"text": "mcp_notes self-test ran", "kind": "session"},
    "memory_recent": {"limit": 5},
    "notes_export": {"out": _SAMPLE_EXPORT},
}

# No sample hits the network or needs credentials.
SAMPLES_OPTIONAL = set()


def build():
    return srv


if __name__ == "__main__":
    srv.run()
