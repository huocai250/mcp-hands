"""MCP server: text, regex, CSV/JSON/Markdown and encoding helpers (stdlib only)."""
import csv
import difflib
import html
import io
import json
import os
import re
import sys
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

MAX_FILE = 2000000  # 2 MB cap for text_or_path inputs
ENCODINGS = ("utf-8", "utf-8-sig", "gbk", "cp936", "latin-1")
# Ranges are written as escapes so this source file stays pure ASCII.
CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
CJK_RX = re.compile("[" + CJK + "]")
WORD_RX = re.compile(r"[^_\W]+", re.UNICODE)
SENTENCE_BREAK = "[.!?\u3002\uff01\uff1f]"
SAMPLE_DIR = os.path.join(sample_dir(), ".aiyu_selftest")
SELF_SOURCE = os.path.abspath(__file__)

srv = Server("text")


# ----------------------------------------------------------------- encoding I/O
def _decode_bytes(data):
    """Decode with the first codec that accepts the whole buffer."""
    for enc in ENCODINGS:
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def _read_bytes(path):
    size = os.path.getsize(path)
    if size > MAX_FILE:
        raise ValueError("file larger than %d bytes: %s" % (MAX_FILE, path))
    with open(path, "rb") as fh:
        return fh.read(MAX_FILE)


def _in_path(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(p):
        raise FileNotFoundError(str(path))
    return p


def _out_path(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    return p


def _resolve(text_or_path):
    """An existing file path is read as a file (2 MB cap); anything else is literal text."""
    raw = "" if text_or_path is None else str(text_or_path)
    if raw and len(raw) <= 4096 and "\x00" not in raw:
        try:
            is_file = os.path.isfile(raw)
        except (OSError, ValueError):
            is_file = False
        if is_file:
            p = os.path.abspath(raw)
            text, _enc = _decode_bytes(_read_bytes(p))
            return text.lstrip("\ufeff"), p
    return raw, ""


# ------------------------------------------------------------- text primitives
def _tokens(text):
    """Split into words: camelCase humps split, CJK characters kept one by one."""
    parts = [p for p in re.split(r"[^0-9A-Za-z" + CJK + "]+", str(text)) if p]
    out = []
    for part in parts:
        out.extend(re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+|[" + CJK + "]", part))
    return out


def _delimiter_for(text):
    head = "\n".join(str(text).split("\n")[:5])
    best, best_n = ",", 0
    for cand in (",", "\t", ";", "|"):
        n = head.count(cand)
        if n > best_n:
            best, best_n = cand, n
    return best


def _rows(text, delimiter=None):
    body = str(text).replace("\r\n", "\n").replace("\r", "\n")
    delim = delimiter if (delimiter and len(str(delimiter)) == 1) else _delimiter_for(body)
    rows = []
    for row in csv.reader(io.StringIO(body), delimiter=delim):
        if not row or not any(str(c).strip() for c in row):
            continue
        rows.append([str(c) for c in row])
    return rows, delim


def _header(row):
    seen = {}
    out = []
    for i, name in enumerate(row):
        clean = str(name).strip() or "col%d" % (i + 1)
        if clean in seen:
            seen[clean] += 1
            clean = "%s_%d" % (clean, seen[clean])
        else:
            seen[clean] = 1
        out.append(clean)
    return out


def _num(value):
    f = float(value)
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return ("%.4f" % f).rstrip("0").rstrip(".")


def _json_objects(text_or_path):
    """Normalize JSON into (header, rows) so csv / markdown writers share one path."""
    text, _src = _resolve(text_or_path)
    obj = json.loads(text)
    if isinstance(obj, dict):
        obj = [obj]
    if not isinstance(obj, list) or not obj:
        raise ValueError("expected a JSON object or a non-empty array of objects")
    if all(isinstance(item, dict) for item in obj):
        header = []
        for item in obj:
            for key in item:
                if key not in header:
                    header.append(key)
        rows = [[item.get(key, "") for key in header] for item in obj]
        return header, rows
    if all(isinstance(item, (list, tuple)) for item in obj):
        width = max(len(item) for item in obj)
        header = ["col%d" % (i + 1) for i in range(width)]
        rows = [[item[i] if i < len(item) else "" for i in range(width)] for item in obj]
        return header, rows
    raise ValueError("expected a JSON object or array of objects/arrays")


# ------------------------------------------------------------------- HTML <-> MD
class _MarkdownWriter(HTMLParser):
    """Small HTML -> Markdown converter: headings, bold, italic, links, lists, code."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.parts = []
        self.pre = False
        self.skip = 0
        self.href = None

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag in ("script", "style", "title"):
            self.skip += 1
        elif self.skip:
            return
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "p":
            self.parts.append("\n\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag in ("strong", "b"):
            self.parts.append("**")
        elif tag in ("em", "i"):
            self.parts.append("*")
        elif tag == "code":
            self.parts.append("" if self.pre else "`")
        elif tag == "pre":
            self.pre = True
            self.parts.append("\n\n```\n")
        elif tag == "a":
            self.href = str(attr.get("href") or "")
            self.parts.append("[")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in ("ul", "ol"):
            self.parts.append("\n")
        elif tag == "hr":
            self.parts.append("\n\n---\n\n")
        elif tag == "blockquote":
            self.parts.append("\n\n> ")
        elif tag == "img":
            self.parts.append("![%s](%s)" % (attr.get("alt") or "", attr.get("src") or ""))
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "title"):
            self.skip = max(0, self.skip - 1)
        elif self.skip:
            return
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "tr", "ul", "ol", "blockquote"):
            self.parts.append("\n")
        elif tag in ("strong", "b"):
            self.parts.append("**")
        elif tag in ("em", "i"):
            self.parts.append("*")
        elif tag == "code" and not self.pre:
            self.parts.append("`")
        elif tag == "pre":
            self.pre = False
            self.parts.append("\n```\n\n")
        elif tag == "a":
            self.parts.append("](%s)" % (self.href or ""))
            self.href = None

    def handle_data(self, data):
        if self.skip:
            return
        self.parts.append(data if self.pre else re.sub(r"[ \t\r\n]+", " ", data))

    def markdown(self):
        out = "".join(self.parts)
        out = re.sub(r"[ \t]+\n", "\n", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out.strip() + "\n"


def _inline(line):
    """Inline Markdown -> HTML. Escape first, then rewrite the markup."""
    out = html.escape(line, quote=True)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", out)
    out = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<em>\1</em>", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', out)
    return out


def _md_body(text):
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, para, code = [], [], []
    in_code, fence, list_tag = False, "", ""

    def flush_para():
        if para:
            out.append("<p>%s</p>" % " ".join(_inline(p) for p in para))
            del para[:]

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append("</%s>" % list_tag)
            list_tag = ""

    for line in lines:
        stripped = line.strip()
        if in_code:
            if stripped.startswith(fence):
                out.append("<pre><code>%s</code></pre>" % html.escape("\n".join(code), quote=True))
                code, in_code, fence = [], False, ""
            else:
                code.append(line)
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush_para()
            close_list()
            in_code, fence = True, stripped[:3]
            continue
        if not stripped:
            flush_para()
            close_list()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            close_list()
            out.append("<h%d>%s</h%d>" % (len(m.group(1)), _inline(m.group(2).strip()), len(m.group(1))))
            continue
        if re.match(r"^([-*_])(\s*\1){2,}\s*$", stripped):
            flush_para()
            close_list()
            out.append("<hr>")
            continue
        m = re.match(r"^([-*+])\s+(.*)$", stripped)
        if m:
            flush_para()
            if list_tag != "ul":
                close_list()
                out.append("<ul>")
                list_tag = "ul"
            out.append("<li>%s</li>" % _inline(m.group(2)))
            continue
        m = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if m:
            flush_para()
            if list_tag != "ol":
                close_list()
                out.append("<ol>")
                list_tag = "ol"
            out.append("<li>%s</li>" % _inline(m.group(2)))
            continue
        m = re.match(r"^>\s?(.*)$", stripped)
        if m:
            flush_para()
            close_list()
            out.append("<blockquote>%s</blockquote>" % _inline(m.group(1)))
            continue
        para.append(stripped)
    if in_code and code:
        out.append("<pre><code>%s</code></pre>" % html.escape("\n".join(code), quote=True))
    flush_para()
    close_list()
    return "\n".join(out)


def _column_index(name, header):
    want = str(name).strip()
    for i, col in enumerate(header):
        if col.lower() == want.lower():
            return i
    if want.isdigit():
        idx = int(want)
        if 0 <= idx < len(header):
            return idx
    raise ValueError("no such column %r (columns: %s)" % (name, ", ".join(header)))


def _first_numeric_column(data, header, skip):
    for i in range(len(header)):
        if i == skip:
            continue
        values = [row[i].strip() for row in data if i < len(row) and row[i].strip()]
        if values and all(_is_float(v) for v in values):
            return i
    return None


def _is_float(value):
    try:
        float(value)
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------------ tools
@srv.tool("word_count", "Count characters, words (CJK per character), lines and paragraphs.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def word_count(text):
    s = "" if text is None else str(text)
    cjk = len(CJK_RX.findall(s))
    rest = len(WORD_RX.findall(CJK_RX.sub(" ", s)))
    lines = len(s.splitlines())
    stripped = s.strip()
    paras = len([p for p in re.split(r"\n[ \t]*\n", stripped) if p.strip()]) if stripped else 0
    return ("chars=%d\nchars_no_spaces=%d\nwords=%d\nwords_latin=%d\nwords_cjk=%d\nlines=%d\nparagraphs=%d"
            % (len(s), len(re.sub(r"\s", "", s)), cjk + rest, rest, cjk, lines, paras))


@srv.tool("case_convert", "Convert case: upper, lower, title, sentence, swap, camel, snake, kebab, pascal.",
          {"type": "object", "properties": {"text": {"type": "string"}, "mode": {"type": "string", "description": "one of upper|lower|title|sentence|swap|camel|snake|kebab|pascal"}}, "required": ["text", "mode"]})
def case_convert(text, mode):
    s = "" if text is None else str(text)
    m = str(mode).strip().lower()
    if m == "upper":
        return s.upper()
    if m == "lower":
        return s.lower()
    if m == "swap":
        return s.swapcase()
    if m == "title":
        return re.sub(r"[^\s]+", lambda mo: mo.group(0)[:1].upper() + mo.group(0)[1:], s)
    if m == "sentence":
        return re.sub(r"(^|" + SENTENCE_BREAK + r"\s+|\n[ \t]*)([a-z])",
                      lambda mo: mo.group(1) + mo.group(2).upper(), s.lower())
    words = [w.lower() for w in _tokens(s)]
    if not words:
        return ""
    if m == "snake":
        return "_".join(words)
    if m == "kebab":
        return "-".join(words)
    if m == "camel":
        return words[0] + "".join(w[:1].upper() + w[1:] for w in words[1:])
    if m == "pascal":
        return "".join(w[:1].upper() + w[1:] for w in words)
    raise ValueError("unknown mode %r (upper|lower|title|sentence|swap|camel|snake|kebab|pascal)" % mode)


@srv.tool("regex_replace", "Regex substitute, reporting how many substitutions happened (count=0 means all).",
          {"type": "object", "properties": {"text": {"type": "string"}, "pattern": {"type": "string"}, "replacement": {"type": "string"}, "count": {"type": "integer", "default": 0}}, "required": ["text", "pattern", "replacement"]})
def regex_replace(text, pattern, replacement, count=0):
    limit = int(count)
    result, hits = re.subn(str(pattern), str(replacement), "" if text is None else str(text), count=limit)
    return "substitutions=%d\n---\n%s" % (hits, result)


@srv.tool("regex_find", "List regex matches with their line/column/offset positions.",
          {"type": "object", "properties": {"text": {"type": "string"}, "pattern": {"type": "string"}, "limit": {"type": "integer", "default": 20}}, "required": ["text", "pattern"]})
def regex_find(text, pattern, limit=20):
    s = "" if text is None else str(text)
    rx = re.compile(str(pattern))
    total = sum(1 for _ in rx.finditer(s))
    rows = []
    for m in rx.finditer(s):
        line = s.count("\n", 0, m.start()) + 1
        col = m.start() - (s.rfind("\n", 0, m.start()) + 1) + 1
        extra = " groups=%s" % (list(m.groups()),) if m.groups() else ""
        rows.append("line %d col %d offset %d: %r%s" % (line, col, m.start(), m.group(0), extra))
        if len(rows) >= max(1, int(limit)):
            break
    head = "matches=%d shown=%d" % (total, len(rows))
    return head + ("\n" + "\n".join(rows) if rows else "\n(no match)")


@srv.tool("diff_text", "Unified diff of two strings (context = lines of context).",
          {"type": "object", "properties": {"left": {"type": "string"}, "right": {"type": "string"}, "context": {"type": "integer", "default": 2}}, "required": ["left", "right"]})
def diff_text(left, right, context=2):
    a = str(left).splitlines(keepends=True)
    b = str(right).splitlines(keepends=True)
    lines = difflib.unified_diff(a, b, "left", "right", n=max(0, int(context)))
    body = "".join(lines)
    return body if body else "(identical)"


@srv.tool("markdown_to_html", "Render Markdown (headings, lists, bold, italic, links, code, hr) to HTML.",
          {"type": "object", "properties": {"text": {"type": "string"}, "title": {"type": "string", "default": ""}}, "required": ["text"]})
def markdown_to_html(text, title=""):
    body = _md_body(text)
    return ("<!DOCTYPE html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n<title>%s</title>\n</head>\n<body>\n%s\n</body>\n</html>"
            % (html.escape(str(title), quote=True), body))


@srv.tool("html_to_markdown", "Convert basic HTML (h1-h6, bold, italic, links, lists, code) to Markdown.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def html_to_markdown(text):
    parser = _MarkdownWriter()
    parser.feed("" if text is None else str(text))
    parser.close()
    return parser.markdown()


@srv.tool("csv_to_json", "CSV (literal text or file) to a JSON array of row objects.",
          {"type": "object", "properties": {"text_or_path": {"type": "string"}, "delimiter": {"type": "string", "default": ","}}, "required": ["text_or_path"]})
def csv_to_json(text_or_path, delimiter=","):
    text, _src = _resolve(text_or_path)
    rows, _delim = _rows(text, delimiter)
    if not rows:
        raise ValueError("no CSV rows found")
    header = _header(rows[0])
    data = [{header[i]: (row[i] if i < len(row) else "") for i in range(len(header))} for row in rows[1:]]
    return json.dumps(data, ensure_ascii=False, indent=2)


@srv.tool("json_to_csv", "JSON array of objects (literal text or file) to CSV text.",
          {"type": "object", "properties": {"text_or_path": {"type": "string"}}, "required": ["text_or_path"]})
def json_to_csv(text_or_path):
    header, rows = _json_objects(text_or_path)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([str(v) for v in row])
    return buf.getvalue().rstrip("\n")


@srv.tool("json_to_md_table", "JSON array of objects (literal text or file) to a Markdown table.",
          {"type": "object", "properties": {"text_or_path": {"type": "string"}}, "required": ["text_or_path"]})
def json_to_md_table(text_or_path):
    header, rows = _json_objects(text_or_path)
    lines = ["| " + " | ".join(str(h) for h in header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(v).replace("|", "\\|") for v in row) + " |")
    return "\n".join(lines)


@srv.tool("md_table_to_json", "Markdown table to a JSON array of row objects.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def md_table_to_json(text):
    rows = []
    for line in str(text).splitlines():
        line = line.strip()
        if "|" not in line:
            if rows:
                break
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
            continue
        rows.append(cells)
    if not rows:
        raise ValueError("no Markdown table found")
    header = _header(rows[0])
    data = [{header[i]: (row[i] if i < len(row) else "") for i in range(len(header))} for row in rows[1:]]
    return json.dumps(data, ensure_ascii=False, indent=2)


@srv.tool("csv_stats", "Per CSV column: count, blanks, unique values and numeric min/max/mean/sum.",
          {"type": "object", "properties": {"text_or_path": {"type": "string"}}, "required": ["text_or_path"]})
def csv_stats(text_or_path):
    text, src = _resolve(text_or_path)
    rows, delim = _rows(text)
    if not rows:
        raise ValueError("no CSV rows found")
    header = _header(rows[0])
    data = rows[1:]
    out = ["source=%s delimiter=%r rows=%d cols=%d" % (src or "(literal)", delim, len(data), len(header))]
    for i, name in enumerate(header):
        values = [(row[i] if i < len(row) else "") for row in data]
        nonblank = [v.strip() for v in values if v.strip()]
        nums = [float(v) for v in nonblank if _is_float(v)]
        line = "  %s: count=%d blanks=%d unique=%d" % (name, len(values), len(values) - len(nonblank), len(set(nonblank)))
        if nums:
            line += " numeric=%d sum=%s min=%s max=%s mean=%s" % (
                len(nums), _num(sum(nums)), _num(min(nums)), _num(max(nums)), _num(sum(nums) / len(nums)))
        out.append(line)
    return "\n".join(out)


@srv.tool("csv_aggregate", "Group a CSV by a column and aggregate a numeric column (sum|mean|min|max|count).",
          {"type": "object", "properties": {"text_or_path": {"type": "string"}, "group_by": {"type": "string"}, "agg": {"type": "string", "default": "sum"}, "value_column": {"type": "string", "default": ""}}, "required": ["text_or_path", "group_by"]})
def csv_aggregate(text_or_path, group_by, agg="sum", value_column=""):
    text, src = _resolve(text_or_path)
    rows, _delim = _rows(text)
    if not rows:
        raise ValueError("no CSV rows found")
    header = _header(rows[0])
    data = rows[1:]
    gi = _column_index(group_by, header)
    how = str(agg or "sum").strip().lower()
    if how not in ("sum", "mean", "min", "max", "count"):
        raise ValueError("unknown agg %r (sum|mean|min|max|count)" % agg)
    vi = None
    if how != "count":
        vi = _column_index(value_column, header) if str(value_column).strip() else _first_numeric_column(data, header, gi)
        if vi is None:
            raise ValueError("no numeric column found; pass value_column")
    buckets = {}
    for row in data:
        key = row[gi] if gi < len(row) else ""
        buckets.setdefault(key, []).append(row[vi] if (vi is not None and vi < len(row)) else "")
    out = ["source=%s agg=%s group_by=%s value_column=%s groups=%d"
           % (src or "(literal)", how, header[gi], "-" if vi is None else header[vi], len(buckets))]
    for key in sorted(buckets):
        values = buckets[key]
        if how == "count":
            result = str(len(values))
        else:
            nums = [float(v.strip()) for v in values if _is_float(v.strip())]
            if not nums:
                result = "(no numeric values)"
            elif how == "sum":
                result = _num(sum(nums))
            elif how == "mean":
                result = _num(sum(nums) / len(nums))
            elif how == "min":
                result = _num(min(nums))
            else:
                result = _num(max(nums))
        out.append("%s=%s" % (key, result))
    return "\n".join(out)


@srv.tool("json_query", "Read a dotted path out of JSON text or a JSON file, e.g. a.b.0.c.",
          {"type": "object", "properties": {"text_or_path": {"type": "string"}, "key_path": {"type": "string"}}, "required": ["text_or_path", "key_path"]})
def json_query(text_or_path, key_path):
    text, _src = _resolve(text_or_path)
    cur = json.loads(text)
    for seg in [s for s in str(key_path).split(".") if s != ""]:
        if isinstance(cur, list):
            cur = cur[int(seg)]
        elif isinstance(cur, dict):
            if seg not in cur:
                raise KeyError("key not found: %s" % seg)
            cur = cur[seg]
        else:
            raise TypeError("cannot descend into %s with %r" % (type(cur).__name__, seg))
    return json.dumps(cur, ensure_ascii=False, indent=2)


@srv.tool("sort_lines", "Sort lines (numeric=sort as numbers, unique=drop duplicates).",
          {"type": "object", "properties": {"text": {"type": "string"}, "reverse": {"type": "boolean", "default": False}, "numeric": {"type": "boolean", "default": False}, "unique": {"type": "boolean", "default": False}}, "required": ["text"]})
def sort_lines(text, reverse=False, numeric=False, unique=False):
    lines = str(text).splitlines() if text is not None else []
    if numeric:
        lines.sort(key=lambda s: (0, float(s), "") if _is_float(s.strip()) else (1, 0.0, s.lower()), reverse=bool(reverse))
    else:
        lines.sort(key=lambda s: s.lower(), reverse=bool(reverse))
    if unique:
        seen = set()
        kept = []
        for line in lines:
            if line not in seen:
                seen.add(line)
                kept.append(line)
        lines = kept
    return "\n".join(lines)


@srv.tool("dedupe_lines", "Drop duplicate lines, keeping the first (or last) occurrence.",
          {"type": "object", "properties": {"text": {"type": "string"}, "keep": {"type": "string", "default": "first"}}, "required": ["text"]})
def dedupe_lines(text, keep="first"):
    lines = str(text).splitlines() if text is not None else []
    how = str(keep or "first").strip().lower()
    if how not in ("first", "last"):
        raise ValueError("keep must be 'first' or 'last'")
    if how == "first":
        seen, out = set(), []
        for line in lines:
            if line not in seen:
                seen.add(line)
                out.append(line)
        return "\n".join(out)
    out = []
    for i, line in enumerate(lines):
        if line not in lines[i + 1:]:
            out.append(line)
    return "\n".join(out)


@srv.tool("split_text", "Split text on a literal marker and return the parts as lines.",
          {"type": "object", "properties": {"text": {"type": "string"}, "marker": {"type": "string"}}, "required": ["text", "marker"]})
def split_text(text, marker):
    if not marker:
        raise ValueError("marker must not be empty")
    return "\n".join(str(text).split(str(marker)))


@srv.tool("join_lines", "Join an array of lines with a separator (default newline).",
          {"type": "object", "properties": {"lines": {"type": "array", "items": {"type": "string"}}, "separator": {"type": "string", "default": "\n"}}, "required": ["lines"]})
def join_lines(lines, separator="\n"):
    return str(separator).join("" if v is None else str(v) for v in (lines or []))


@srv.tool("extract_emails", "Extract unique email addresses from text.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def extract_emails(text):
    hits = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "" if text is None else str(text))
    return "\n".join(dict.fromkeys(hits)) or "(none)"


@srv.tool("extract_urls", "Extract unique http(s):// and www. URLs from text.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def extract_urls(text):
    hits = re.findall(r"(?:https?://|www\.)[^\s<>\"'\)\]]+", "" if text is None else str(text))
    return "\n".join(dict.fromkeys(hits)) or "(none)"


@srv.tool("extract_phones", "Extract unique phone numbers (international and CN mobile/landline).",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def extract_phones(text):
    s = "" if text is None else str(text)
    hits = re.findall(r"(?:\+?86[-\s]?)?1[3-9]\d{9}|\+?\d[\d\s().-]{7,}\d", s)
    return "\n".join(dict.fromkeys(h.strip() for h in hits)) or "(none)"


@srv.tool("extract_ipv4", "Extract unique IPv4 addresses (private ones filtered when include_private=false).",
          {"type": "object", "properties": {"text": {"type": "string"}, "include_private": {"type": "boolean", "default": True}}, "required": ["text"]})
def extract_ipv4(text, include_private=True):
    hits = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "" if text is None else str(text))
    out = []
    for hit in hits:
        parts = [int(p) for p in hit.split(".")]
        if any(p > 255 for p in parts):
            continue
        if not include_private and _is_private(parts):
            continue
        if hit not in out:
            out.append(hit)
    return "\n".join(out) or "(none)"


def _is_private(parts):
    a, b = parts[0], parts[1]
    return (a in (0, 10, 127) or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)
            or (a == 169 and b == 254) or a >= 224)


@srv.tool("encoding_convert", "Convert a text file between encodings (e.g. gbk -> utf-8), creating parent dirs.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "from_encoding": {"type": "string", "default": "gbk"}, "to_encoding": {"type": "string", "default": "utf-8"}}, "required": ["path", "out"]})
def encoding_convert(path, out, from_encoding="gbk", to_encoding="utf-8"):
    src = _in_path(path)
    data = _read_bytes(src)
    text = data.decode(str(from_encoding))
    dst = _out_path(out)
    with open(dst, "w", encoding=str(to_encoding), errors="strict", newline="") as fh:
        fh.write(text)
    return ("%s [%s] -> %s [%s]\nchars=%d bytes_in=%d bytes_out=%d"
            % (src, from_encoding, dst, to_encoding, len(text), len(data), os.path.getsize(dst)))


@srv.tool("read_text_auto", "Read a text file, auto-detecting utf-8 / utf-8-sig / gbk / cp936 / latin-1.",
          {"type": "object", "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer", "default": 20000}}, "required": ["path"]})
def read_text_auto(path, max_chars=20000):
    src = _in_path(path)
    data = _read_bytes(src)
    text, enc = _decode_bytes(data)
    limit = max(1, int(max_chars))
    body = text[:limit]
    return ("path=%s\nencoding=%s\nbytes=%d\nchars=%d\ntruncated=%s\n---\n%s"
            % (src, enc, len(data), len(text), len(text) > limit, body))


_SAMPLE_TEXT = "alpha beta gamma\nsecond line here\n\nlast paragraph."
_CSV_TEXT = "name,dept,qty\nwidget,ops,3\ngadget,ops,5\ndoohickey,lab,2\nbolt,lab,7\n"
_JSON_TEXT = '[{"name": "widget", "qty": 3}, {"name": "bolt", "qty": 7}]'
_MD_TABLE = "| name | qty |\n| --- | --- |\n| widget | 3 |\n| bolt | 7 |"
_HTML_TEXT = ('<h1>Title</h1><p><strong>bold</strong> and <em>italic</em> and '
              '<a href="https://example.com">link</a></p><ul><li>one</li><li>two</li></ul>'
              '<pre><code>x = 1</code></pre>')
_CONVERTED = os.path.join(SAMPLE_DIR, "self_utf8_to_gbk.txt")


def _sample_source():
    """A text file that exists both from source and inside a packed exe.

    __file__ points into the archive once frozen (so it is not on disk); fall back
    to the config file the bridge keeps next to the exe, then to the executable.
    """
    for candidate in (SELF_SOURCE, os.path.join(sample_dir(), "bridge.config.json"),
                      os.path.join(sample_dir(), "README.md"), sys.executable):
        if candidate and os.path.isfile(candidate):
            return candidate
    return SELF_SOURCE


# Declaration order matters: encoding_convert writes the file read_text_auto reads.
SAMPLES = {
    "word_count": {"text": _SAMPLE_TEXT},
    "case_convert": {"text": "hello world sample text", "mode": "title"},
    "regex_replace": {"text": "cat bat rat", "pattern": "([cbr])at", "replacement": "<\\1>", "count": 0},
    "regex_find": {"text": "a1 b22 c333", "pattern": "[a-z](\\d+)", "limit": 5},
    "diff_text": {"left": "alpha\nbeta\ngamma\n", "right": "alpha\nBETA\ngamma\n", "context": 1},
    "markdown_to_html": {"text": "# Title\n\nSome **bold** and *italic* text.\n\n- one\n- two\n", "title": "Sample"},
    "html_to_markdown": {"text": _HTML_TEXT},
    "csv_to_json": {"text_or_path": _CSV_TEXT, "delimiter": ","},
    "json_to_csv": {"text_or_path": _JSON_TEXT},
    "json_to_md_table": {"text_or_path": _JSON_TEXT},
    "md_table_to_json": {"text": _MD_TABLE},
    "csv_stats": {"text_or_path": _CSV_TEXT},
    "csv_aggregate": {"text_or_path": _CSV_TEXT, "group_by": "dept", "agg": "sum", "value_column": "qty"},
    "json_query": {"text_or_path": _JSON_TEXT, "key_path": "1.name"},
    "sort_lines": {"text": "banana\napple\ncherry\n", "reverse": False, "numeric": False, "unique": False},
    "dedupe_lines": {"text": "alpha\nbeta\nalpha\ngamma\n", "keep": "first"},
    "split_text": {"text": "one,two,three", "marker": ","},
    "join_lines": {"lines": ["one", "two", "three"], "separator": "\n"},
    "extract_emails": {"text": "ping a.b@example.com and c@test.org today"},
    "extract_urls": {"text": "see https://example.com/a?b=1 and http://test.org/x"},
    "extract_phones": {"text": "call +86 13800138000 or 010-12345678"},
    "extract_ipv4": {"text": "8.8.8.8 192.168.1.1 127.0.0.1", "include_private": True},
    "encoding_convert": {"path": _sample_source(), "out": _CONVERTED, "from_encoding": "utf-8", "to_encoding": "gbk"},
    "read_text_auto": {"path": _CONVERTED, "max_chars": 400},
}

# Sample tool calls that would need the network or credentials; none here.
SAMPLES_OPTIONAL = set()


def build():
    return srv


if __name__ == "__main__":
    srv.run()
