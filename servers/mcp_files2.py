"""MCP server: bulk file organisation - batch rename, duplicate hunting, sorting.

Read-mostly workflow tools. Every mutating tool takes dry_run and defaults to it,
and every listing tool caps its work and says so in the first line of the result.
"""
import datetime
import fnmatch
import hashlib
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

MAX_WALK = 20000      # hard ceiling on files inspected in one call
MAX_HASH = 4000       # hard ceiling on files hashed in one duplicate hunt
HASH_BUF = 1 << 20

DEFAULT_ORG = {
    "images": "jpg jpeg png gif bmp webp tif tiff svg ico heic avif raw cr2 nef psd ai",
    "documents": "pdf doc docx xls xlsx ppt pptx txt md rtf odt ods csv tsv tex epub mobi",
    "archives": "zip rar 7z tar gz bz2 xz tgz cab iso zst lz4",
    "media": "mp3 wav flac aac ogg m4a wma mp4 mkv avi mov wmv flv webm m4v mpg mpeg 3gp",
    "code": "py js ts jsx tsx mjs cjs json html htm css scss xml yml yaml sh ps1 bat cmd c h cpp hpp cs java go rs rb php sql lua pl kt swift ini toml cfg",
    "other": "",
}
DATE_SOURCES = ("mtime", "ctime", "atime", "birth")


# ------------------------------------------------------------------ path helpers
def _abs(path):
    return os.path.abspath(os.path.expanduser(str(path)))


def _inside(child, parent):
    """True when child is parent itself or lives under it (drive-safe)."""
    c, p = os.path.normcase(os.path.abspath(child)), os.path.normcase(os.path.abspath(parent))
    if c == p:
        return True
    try:
        return os.path.commonpath([p, c]) == p
    except ValueError:
        return False


def _needs_guard(src_root, dest):
    """A destination inside the source only loops when the walk can reach it.

    Moving out of a root into that same root's subtree would make the walker
    re-discover what it just moved, so those calls are refused outright.
    """
    if not dest:
        return False
    return _inside(dest, src_root) and os.path.normcase(_abs(dest)) != os.path.normcase(_abs(src_root))


def _collect(root, glob="*", limit=MAX_WALK):
    """Files under root matching a glob (or several, comma separated), sorted.

    Returns (paths, truncated, visited) where visited counts every file seen.
    """
    root = _abs(root)
    if not os.path.isdir(root):
        raise NotADirectoryError(root)
    patterns = [p.strip() for p in str(glob).replace(";", ",").split(",") if p.strip()] or ["*"]
    out, visited, truncated = [], 0, False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            visited += 1
            if visited > MAX_WALK:
                truncated = True
                break
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if not any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat) for pat in patterns):
                continue
            out.append(full)
            if len(out) >= int(limit):
                truncated = True
                break
        if truncated:
            break
    return sorted(out), truncated, visited


def _note(count, limit, extra=""):
    if count >= int(limit):
        return "truncated=True (cap %d reached; raise limit or narrow the pattern) %s" % (int(limit), extra)
    return "truncated=False"


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_BUF), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_dest(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s (%d)%s" % (stem, i, ext)):
        i += 1
    return "%s (%d)%s" % (stem, i, ext)


def _rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def _ext(name):
    return os.path.splitext(name)[1].lstrip(".").lower()


def _rules(extra_rules):
    """Category -> extensions, with extra_rules lines like 'psd=images' on top."""
    table = dict((category, set(exts.split())) for category, exts in DEFAULT_ORG.items())
    for line in str(extra_rules or "").replace(";", "\n").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        exts, category = line.split("=", 1)
        category = category.strip()
        if category not in table:
            table[category] = set()
        for ext in exts.replace(",", " ").split():
            table[category].add(ext.strip().lstrip(".").lower())
    return table


def _category_for(name, table):
    ext = _ext(name)
    if not ext:
        return "other"
    for category in ("images", "documents", "archives", "media", "code", "other"):
        if ext in table.get(category, ()):
            return category
    for category, exts in table.items():
        if category in ("images", "documents", "archives", "media", "code", "other"):
            continue
        if ext in exts:
            return category
    return "other"


def _run_cap(limit):
    return max(1, min(int(limit), MAX_WALK))


srv = Server("files2")


# ------------------------------------------------------------------------ tools
@srv.tool("rename_batch", "Rename files in place with a regex or literal pattern (dry_run by default).",
          {"type": "object", "properties": {
              "root": {"type": "string", "description": "directory to scan recursively"},
              "pattern": {"type": "string", "description": "regex (default) or literal text to find in the file name"},
              "replacement": {"type": "string", "description": "replacement text; \\1 backrefs work in regex mode"},
              "glob": {"type": "string", "default": "*", "description": "which file names to consider"},
              "dry_run": {"type": "boolean", "default": True},
              "regex": {"type": "boolean", "default": True},
              "limit": {"type": "integer", "default": 500}},
           "required": ["root", "pattern", "replacement"]})
def rename_batch(root, pattern, replacement, glob="*", dry_run=True, regex=True, limit=500):
    cap = _run_cap(limit)
    files, truncated, visited = _collect(root, glob, cap)
    if regex:
        try:
            rx = re.compile(str(pattern))
        except re.error as exc:
            raise ValueError("bad regex %r: %s" % (pattern, exc))
        def translate(name):
            return rx.sub(str(replacement), name)
    else:
        def translate(name):
            return str(name).replace(str(pattern), str(replacement))

    rows, changes, skipped, errors = [], 0, 0, 0
    seen_targets = set()
    for full in files:
        name = os.path.basename(full)
        try:
            new = translate(name)
        except (re.error, IndexError) as exc:
            rows.append("ERROR  %s -> (%s)" % (name, exc))
            errors += 1
            continue
        if new == name:
            rows.append("KEEP   %s (pattern did not match)" % name)
            skipped += 1
            continue
        if not new or new in (".", "..") or new != os.path.basename(new):
            rows.append("SKIP   %s -> %r (invalid file name)" % (name, new))
            skipped += 1
            continue
        target = os.path.join(os.path.dirname(full), new)
        key = os.path.normcase(target)
        if key in seen_targets or (os.path.exists(target) and key != os.path.normcase(full)):
            rows.append("SKIP   %s -> %s (target exists)" % (name, new))
            skipped += 1
            continue
        seen_targets.add(key)
        if dry_run:
            rows.append("PLAN   %s -> %s" % (name, new))
            changes += 1
            continue
        try:
            os.replace(full, target)
            rows.append("RENAMED %s -> %s" % (name, new))
            changes += 1
        except OSError as exc:
            rows.append("ERROR  %s -> %s (%s)" % (name, new, exc))
            errors += 1
    head = ("root=%s mode=%s glob=%s scanned=%d candidates=%d %s changed=%d skipped=%d errors=%d"
            % (_abs(root), "regex" if regex else "literal", glob, visited, len(files),
               _note(len(files), cap), changes, skipped, errors))
    return head + "\n" + ("\n".join(rows[:cap]) if rows else "(no files considered)")


@srv.tool("find_duplicates", "Find duplicate files by size then sha256 and report wasted bytes.",
          {"type": "object", "properties": {
              "root": {"type": "string"},
              "min_size": {"type": "integer", "default": 1, "description": "ignore files smaller than this"},
              "limit": {"type": "integer", "default": 200, "description": "max duplicate groups reported"}},
           "required": ["root"]})
def find_duplicates(root, min_size=1, limit=200):
    files, truncated, visited = _collect(root, "*", MAX_WALK)
    floor = max(0, int(min_size))
    by_size = {}
    for full in files:
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if size < floor:
            continue
        by_size.setdefault(size, []).append(full)
    groups, wasted, hashed, capped = [], 0, 0, False
    for size in sorted(by_size, reverse=True):
        same = by_size[size]
        if len(same) < 2:
            continue
        by_hash = {}
        for full in same:
            if hashed >= MAX_HASH:
                capped = True
                break
            try:
                digest = _sha256(full)
            except OSError:
                continue
            hashed += 1
            by_hash.setdefault(digest, []).append(full)
        for digest, members in sorted(by_hash.items()):
            if len(members) < 2:
                continue
            group_waste = size * (len(members) - 1)
            wasted += group_waste
            groups.append((group_waste, size, digest, members))
    groups.sort(reverse=True)
    shown = groups[: max(1, int(limit))]
    head = ("root=%s scanned=%d hashed=%d groups=%d groups_shown=%d wasted=%.2f MB min_size=%d %s"
            % (_abs(root), visited, hashed, len(groups), len(shown), wasted / 1048576.0, floor,
               _note(len(files), MAX_WALK)))
    if capped:
        head += "\nhash cap %d reached: some size-classes were not fully compared" % MAX_HASH
    body = []
    for group_waste, size, digest, members in shown:
        body.append("group sha256=%s size=%d copies=%d wasted=%.2f MB" % (digest, size, len(members), group_waste / 1048576.0))
        for full in members:
            body.append("  %s" % full)
    return head + "\n" + ("\n".join(body) if body else "(no duplicates)")


@srv.tool("dedupe_move", "Move duplicate copies (keeping one per group) into dest\\duplicates.",
          {"type": "object", "properties": {
              "root": {"type": "string"},
              "dest": {"type": "string", "description": "destination root; files land in <dest>/duplicates"},
              "keep": {"type": "string", "default": "first", "description": "first|last - which copy stays put"},
              "dry_run": {"type": "boolean", "default": True}},
           "required": ["root", "dest"]})
def dedupe_move(root, dest, keep="first", dry_run=True):
    src_root = _abs(root)
    how = str(keep or "first").strip().lower()
    if how not in ("first", "last"):
        raise ValueError("keep must be 'first' or 'last'")
    pool = _abs(dest)
    if _needs_guard(src_root, pool):
        return ("refused: dest %s is inside root %s - moving duplicates in there would be "
                "re-scanned as fresh input and loop" % (pool, src_root))
    files, truncated, visited = _collect(src_root, "*", MAX_WALK)
    by_size = {}
    for full in files:
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        by_size.setdefault(size, []).append(full)
    moves, kept, skipped, hashed = [], 0, 0, 0
    for size in sorted(by_size):
        same = sorted(by_size[size])
        if len(same) < 2:
            continue
        by_hash = {}
        for full in same:
            if hashed >= MAX_HASH:
                skipped += 1
                continue
            try:
                digest = _sha256(full)
            except OSError:
                skipped += 1
                continue
            hashed += 1
            by_hash.setdefault(digest, []).append(full)
        for digest, members in by_hash.items():
            if len(members) < 2:
                continue
            keep_path = members[0] if how == "first" else members[-1]
            kept += 1
            for full in members:
                if full == keep_path:
                    continue
                rel = _rel(src_root, full)
                target = _unique_dest(os.path.join(pool, "duplicates", rel))
                if dry_run:
                    moves.append("PLAN   %s -> %s" % (full, target))
                    continue
                try:
                    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                    shutil.move(full, target)
                    moves.append("MOVED  %s -> %s" % (full, target))
                except OSError as exc:
                    moves.append("ERROR  %s (%s)" % (full, exc))
                    skipped += 1
    head = ("root=%s dest=%s keep=%s mode=%s scanned=%d hashed=%d groups_kept=%d moved=%d skipped=%d %s"
            % (src_root, os.path.join(pool, "duplicates"), how, "dry-run" if dry_run else "live",
               visited, hashed, kept, sum(1 for m in moves if m.startswith(("PLAN", "MOVED"))), skipped,
               _note(len(files), MAX_WALK)))
    return head + "\n" + ("\n".join(moves) if moves else "(no duplicates to move)")


@srv.tool("organize_by_type", "Sort files into images/ documents/ archives/ media/ code/ other/ folders.",
          {"type": "object", "properties": {
              "root": {"type": "string"},
              "dest": {"type": "string", "default": "", "description": "empty = sort inside root"},
              "dry_run": {"type": "boolean", "default": True},
              "extra_rules": {"type": "string", "default": "", "description": "extra mappings, one per line: psd=images"}},
           "required": ["root"]})
def organize_by_type(root, dest="", dry_run=True, extra_rules=""):
    src_root = _abs(root)
    out_root = _abs(dest) if str(dest).strip() else src_root
    if _needs_guard(src_root, out_root):
        return ("refused: dest %s is inside root %s - files moved there would be re-scanned as "
                "fresh input and sorted again forever" % (out_root, src_root))
    table = _rules(extra_rules)
    files, truncated, visited = _collect(src_root, "*", MAX_WALK)
    counts, rows = {}, []
    for full in files:
        category = _category_for(os.path.basename(full), table)
        rel = _rel(src_root, full)
        target = os.path.join(out_root, category, rel)
        counts[category] = counts.get(category, 0) + 1
        if os.path.normcase(full) == os.path.normcase(target):
            rows.append("KEEP   %s (already in %s/)" % (rel, category))
            continue
        target = _unique_dest(target)
        if dry_run:
            rows.append("PLAN   %s -> %s" % (rel, os.path.relpath(target, out_root).replace(os.sep, "/")))
            continue
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            shutil.move(full, target)
            rows.append("MOVED  %s -> %s" % (rel, os.path.relpath(target, out_root).replace(os.sep, "/")))
        except OSError as exc:
            rows.append("ERROR  %s (%s)" % (rel, exc))
    head = ("root=%s dest=%s mode=%s scanned=%d matched=%d %s categories=%s"
            % (src_root, out_root, "dry-run" if dry_run else "live", visited, len(files),
               _note(len(files), MAX_WALK),
               ", ".join("%s=%d" % (k, counts[k]) for k in sorted(counts)) or "none"))
    return head + "\n" + ("\n".join(rows) if rows else "(nothing to sort)")


@srv.tool("organize_by_date", "Move files into YYYY\\MM folders derived from mtime (or ctime/atime/birth).",
          {"type": "object", "properties": {
              "root": {"type": "string"},
              "dest": {"type": "string", "default": "", "description": "empty = sort inside root"},
              "dry_run": {"type": "boolean", "default": True},
              "date_source": {"type": "string", "default": "mtime", "description": "mtime|ctime|atime|birth"}},
           "required": ["root"]})
def organize_by_date(root, dest="", dry_run=True, date_source="mtime"):
    src_root = _abs(root)
    source = str(date_source or "mtime").strip().lower()
    if source not in DATE_SOURCES:
        raise ValueError("date_source must be one of %s" % "|".join(DATE_SOURCES))
    out_root = _abs(dest) if str(dest).strip() else src_root
    if _needs_guard(src_root, out_root):
        return ("refused: dest %s is inside root %s - dated folders created there would be re-scanned "
                "as fresh input and split again forever" % (out_root, src_root))
    files, truncated, visited = _collect(src_root, "*", MAX_WALK)
    counts, rows = {}, []
    for full in files:
        try:
            st = os.stat(full)
        except OSError as exc:
            rows.append("ERROR  %s (%s)" % (_rel(src_root, full), exc))
            continue
        stamp = st.st_birthtime if (source == "birth" and hasattr(st, "st_birthtime")) else getattr(st, "st_%s" % source)
        when = datetime.datetime.fromtimestamp(stamp)
        folder = os.path.join("%04d" % when.year, "%02d" % when.month)
        rel = _rel(src_root, full)
        target = os.path.join(out_root, folder, rel)
        counts[folder] = counts.get(folder, 0) + 1
        if os.path.normcase(full) == os.path.normcase(target):
            rows.append("KEEP   %s (already in %s)" % (rel, folder.replace(os.sep, "/")))
            continue
        target = _unique_dest(target)
        if dry_run:
            rows.append("PLAN   %s [%s] -> %s" % (rel, when.date(), os.path.relpath(target, out_root).replace(os.sep, "/")))
            continue
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            shutil.move(full, target)
            rows.append("MOVED  %s [%s] -> %s" % (rel, when.date(), os.path.relpath(target, out_root).replace(os.sep, "/")))
        except OSError as exc:
            rows.append("ERROR  %s (%s)" % (rel, exc))
    head = ("root=%s dest=%s date_source=%s mode=%s scanned=%d matched=%d %s months=%s"
            % (src_root, out_root, source, "dry-run" if dry_run else "live", visited, len(files),
               _note(len(files), MAX_WALK),
               ", ".join("%s=%d" % (k, counts[k]) for k in sorted(counts)) or "none"))
    return head + "\n" + ("\n".join(rows) if rows else "(nothing to sort)")


@srv.tool("large_files", "Biggest files under a root (sort=size), or the oldest ones (sort=age).",
          {"type": "object", "properties": {
              "root": {"type": "string"},
              "min_mb": {"type": "number", "default": 100},
              "limit": {"type": "integer", "default": 50},
              "sort": {"type": "string", "default": "size", "description": "size|age"}},
           "required": ["root"]})
def large_files(root, min_mb=100, limit=50, sort="size"):
    src_root = _abs(root)
    how = str(sort or "size").strip().lower()
    if how not in ("size", "age"):
        raise ValueError("sort must be 'size' or 'age'")
    floor = int(float(min_mb) * 1048576)
    files, truncated, visited = _collect(src_root, "*", MAX_WALK)
    rows, matched, total = [], 0, 0
    for full in files:
        try:
            st = os.stat(full)
        except OSError:
            continue
        if st.st_size < floor:
            continue
        matched += 1
        total += st.st_size
        rows.append((st.st_size, st.st_mtime, full))
    rows.sort(key=(lambda r: r[0]), reverse=True) if how == "size" else rows.sort(key=lambda r: r[1])
    shown = rows[: max(1, int(limit))]
    head = ("root=%s sort=%s min_mb=%s matched=%d total=%.2f MB scanned=%d %s"
            % (src_root, how, min_mb, matched, total / 1048576.0, visited, _note(len(files), MAX_WALK)))
    body = "\n".join("%10.2f MB  %s  %s" % (size / 1048576.0,
                                            datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
                                            full)
                     for size, mtime, full in shown)
    return head + "\n" + (body or "(no file at or above %.0f MB)" % float(min_mb))


@srv.tool("empty_dirs", "List empty directories (deepest first); remove=true deletes them bottom-up.",
          {"type": "object", "properties": {
              "root": {"type": "string"},
              "remove": {"type": "boolean", "default": False},
              "limit": {"type": "integer", "default": 200}},
           "required": ["root"]})
def empty_dirs(root, remove=False, limit=200):
    src_root = _abs(root)
    if not os.path.isdir(src_root):
        raise NotADirectoryError(src_root)
    cap = max(1, int(limit))
    order = []
    visited = 0
    for dirpath, dirnames, filenames in os.walk(src_root):
        visited += 1
        rel = _rel(src_root, dirpath)
        if rel == ".":
            continue
        order.append((rel.count("/"), dirpath))
    order.sort(key=lambda item: -item[0])

    rows, removed, candidates = [], 0, 0
    for _depth, dirpath in order:
        try:
            if os.listdir(dirpath):
                continue
        except OSError as exc:
            rows.append("ERROR  %s (%s)" % (_rel(src_root, dirpath), exc))
            continue
        candidates += 1
        if candidates > cap:
            continue
        if remove:
            try:
                os.rmdir(dirpath)
                rows.append("REMOVED %s" % _rel(src_root, dirpath))
                removed += 1
            except OSError as exc:
                rows.append("ERROR  %s (%s)" % (_rel(src_root, dirpath), exc))
        else:
            rows.append("EMPTY  %s" % _rel(src_root, dirpath))
    head = ("root=%s mode=%s dirs_scanned=%d empty=%d removed=%d %s"
            % (src_root, "remove" if remove else "list", visited, candidates, removed,
               _note(candidates, cap, "(listing capped)")))
    return head + "\n" + ("\n".join(rows) if rows else "(no empty directories)")


@srv.tool("compare_dirs", "Compare two trees: only-left, only-right, size and mtime mismatches.",
          {"type": "object", "properties": {
              "left": {"type": "string"},
              "right": {"type": "string"},
              "limit": {"type": "integer", "default": 200}},
           "required": ["left", "right"]})
def compare_dirs(left, right, limit=200):
    a_root, b_root = _abs(left), _abs(right)
    table = {}
    truncated = False
    for root, side in ((a_root, 0), (b_root, 1)):
        files, cut, _visited = _collect(root, "*", MAX_WALK)
        truncated = truncated or cut
        for full in files:
            try:
                st = os.stat(full)
            except OSError:
                continue
            entry = table.setdefault(_rel(root, full), [None, None])
            entry[side] = (st.st_size, st.st_mtime)
    cap = max(1, int(limit))
    only_left, only_right, diffs = [], [], []
    for rel in sorted(table):
        a, b = table[rel]
        if a and not b:
            only_left.append(rel)
        elif b and not a:
            only_right.append(rel)
        elif a and b:
            if a[0] != b[0]:
                diffs.append("%s (size %d vs %d)" % (rel, a[0], b[0]))
            elif int(a[1]) != int(b[1]):
                diffs.append("%s (mtime %s vs %s)" % (rel,
                                                      datetime.datetime.fromtimestamp(a[1]).strftime("%Y-%m-%d %H:%M:%S"),
                                                      datetime.datetime.fromtimestamp(b[1]).strftime("%Y-%m-%d %H:%M:%S")))
    head = ("left=%s\nright=%s\nfiles=%d only_left=%d only_right=%d differing=%d %s"
            % (a_root, b_root, len(table), len(only_left), len(only_right), len(diffs),
               _note(len(table), MAX_WALK) if truncated else "truncated=False"))

    def block(title, items):
        if not items:
            return "%s: none" % title
        shown = items[:cap]
        tail = "" if len(items) <= cap else "\n  ...(%d more)" % (len(items) - cap)
        return "%s (%d):\n  %s%s" % (title, len(items), "\n  ".join(shown), tail)

    return "\n".join([head, block("only in left", only_left), block("only in right", only_right),
                      block("size/mtime mismatches", diffs)])


@srv.tool("batch_copy", "Copy (or move) glob-matched files into dest, preserving relative paths.",
          {"type": "object", "properties": {
              "src_root": {"type": "string"},
              "dest": {"type": "string"},
              "glob": {"type": "string", "default": "*"},
              "move": {"type": "boolean", "default": False},
              "overwrite": {"type": "boolean", "default": False},
              "limit": {"type": "integer", "default": 500}},
           "required": ["src_root", "dest"]})
def batch_copy(src_root, dest, glob="*", move=False, overwrite=False, limit=500):
    src = _abs(src_root)
    pool = _abs(dest)
    if _needs_guard(src, pool):
        return ("refused: dest %s is inside src_root %s - copying into the source tree would make the "
                "walker re-copy its own output" % (pool, src))
    cap = _run_cap(limit)
    files, truncated, visited = _collect(src, glob, cap)
    rows, done, skipped = [], 0, 0
    for full in files:
        rel = _rel(src, full)
        target = os.path.join(pool, rel)
        if os.path.normcase(target) == os.path.normcase(full):
            rows.append("KEEP   %s (source and destination are the same file)" % rel)
            skipped += 1
            continue
        if os.path.exists(target) and not overwrite:
            rows.append("SKIP   %s (destination exists; pass overwrite=true)" % rel)
            skipped += 1
            continue
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            if move:
                shutil.move(full, target)
                rows.append("MOVED  %s -> %s" % (rel, pool))
            else:
                shutil.copy2(full, target)
                rows.append("COPIED %s -> %s" % (rel, pool))
            done += 1
        except OSError as exc:
            rows.append("ERROR  %s (%s)" % (rel, exc))
            skipped += 1
    head = ("src=%s dest=%s glob=%s mode=%s scanned=%d matched=%d %s done=%d skipped=%d"
            % (src, pool, glob, "move" if move else "copy", visited, len(files),
               _note(len(files), cap), done, skipped))
    return head + "\n" + ("\n".join(rows) if rows else "(no matching files)")


# ---------------------------------------------------------------------- samples
SAMPLE_DIR = os.path.join(sample_dir(), ".aiyu_selftest", "files2")
SAMPLE_ROOT = os.path.join(SAMPLE_DIR, "tree")
SAMPLE_DEST = os.path.join(SAMPLE_DIR, "dest")
_NAMES = [
    "IMG_0001.jpg", "IMG_0002.jpg", "note.txt", "report.pdf", "bundle.zip",
    "clip.mp3", "script.py", "IMG_0003.png", "data.csv", "empty1.bin",
]
_SPARE = ["archive1.txt", "archive2.txt", "dup.jpg"]


def _sample_seed():
    """Create a small, deterministic tree under sample_dir() for the samples.

    Only ever writes below sample_dir(); safe to re-run because existing files
    are truncated rather than duplicated.
    """
    os.makedirs(os.path.join(SAMPLE_ROOT, "pics"), exist_ok=True)
    os.makedirs(os.path.join(SAMPLE_ROOT, "deep", "deeper"), exist_ok=True)
    for i, name in enumerate(_NAMES):
        sub = "pics" if name.startswith("IMG") else ""
        path = os.path.join(SAMPLE_ROOT, sub, name) if sub else os.path.join(SAMPLE_ROOT, name)
        with open(path, "wb") as fh:
            fh.write(("files2 sample %d - %s\n" % (i, name)).encode("ascii") * (i + 1))
    for name in _SPARE:
        path = os.path.join(SAMPLE_ROOT, name)
        with open(path, "wb") as fh:
            fh.write(("spare %s\n" % name).encode("ascii"))
    # a real duplicate pair: the copy under deep/ is byte-identical to the one
    # under pics/, so the duplicate finder must report one group with two
    # matching sha256 values
    with open(os.path.join(SAMPLE_ROOT, "pics", "IMG_0001.jpg"), "rb") as fh:
        payload = fh.read()
    with open(os.path.join(SAMPLE_ROOT, "deep", "IMG_0001.jpg"), "wb") as fh:
        fh.write(payload)
    os.makedirs(os.path.join(SAMPLE_ROOT, "deep", "deeper", "hollow"), exist_ok=True)
    return SAMPLE_ROOT


SAMPLES = {
    # writer first: everything below reads the tree this seed creates
    "rename_batch": {"root": _sample_seed(), "pattern": r"IMG_(\d+)", "replacement": r"photo_\1",
                     "glob": "*.jpg", "dry_run": True, "regex": True, "limit": 50},
    "find_duplicates": {"root": SAMPLE_ROOT, "min_size": 1, "limit": 20},
    "dedupe_move": {"root": SAMPLE_ROOT, "dest": SAMPLE_DEST, "keep": "first", "dry_run": True},
    "organize_by_type": {"root": SAMPLE_ROOT, "dest": SAMPLE_DEST, "dry_run": True,
                         "extra_rules": "psd=images"},
    "organize_by_date": {"root": SAMPLE_ROOT, "dest": SAMPLE_DEST, "dry_run": True, "date_source": "mtime"},
    "large_files": {"root": SAMPLE_ROOT, "min_mb": 0, "limit": 5, "sort": "size"},
    "empty_dirs": {"root": SAMPLE_ROOT, "remove": False, "limit": 50},
    "compare_dirs": {"left": SAMPLE_ROOT, "right": SAMPLE_DIR, "limit": 20},
    "batch_copy": {"src_root": SAMPLE_ROOT, "dest": SAMPLE_DEST, "glob": "*.txt", "move": False,
                   "overwrite": False, "limit": 50},
}

# Nothing here needs the network or credentials.
SAMPLES_OPTIONAL = set()


def self_test():
    """Run every sample locally and print one line per tool (for --self-test)."""
    failures = 0
    for name in SAMPLES:
        try:
            text = str(srv.tools[name]["fn"](**SAMPLES[name]))
            print("  ok   %-18s %s" % (name, (text.splitlines() or [""])[0][:160]))
        except Exception as exc:  # noqa: BLE001 - report and keep going
            failures += 1
            print("  FAIL %-18s %s: %s" % (name, type(exc).__name__, exc))
    print("files2 selftest: %d ok, %d failed, %d tools" % (len(SAMPLES) - failures, failures, len(srv.tools)))
    return 1 if failures else 0


def build():
    return srv


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        raise SystemExit(self_test())
    srv.run()
