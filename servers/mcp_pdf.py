"""MCP server: PDF tools built on pypdf (info, text, merge/split, pages, security)."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

from pypdf import PdfReader, PdfWriter  # noqa: E402
from pypdf.errors import DependencyError  # noqa: E402

SAMPLE_DIR = os.path.join(sample_dir(), ".aiyu_selftest")

srv = Server("pdf")


# ----------------------------------------------------------------------- paths
def _in_path(path):
    """Resolve an input path; a missing input becomes a clear isError result."""
    p = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(p):
        raise FileNotFoundError(str(path))
    return p


def _out_path(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    return p


def _out_dir(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    os.makedirs(p, exist_ok=True)
    return p


def _summary(out, pages, extra=""):
    size = os.path.getsize(out)
    lines = ["out=%s" % out, "pages=%d" % pages, "bytes=%d" % size]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


# ---------------------------------------------------------------------- reader
def _reader(path, password=None):
    reader = PdfReader(_in_path(path))
    if reader.is_encrypted:
        if password is None or not reader.decrypt(str(password)):
            raise ValueError("PDF is encrypted; a valid password is required: %s" % path)
    return reader


def _write(writer, out):
    if len(writer.pages) == 0:
        raise ValueError("refusing to write a PDF with no pages")
    dst = _out_path(out)
    with open(dst, "wb") as fh:
        writer.write(fh)
    return dst


def _copy(reader, indexes=None):
    writer = PdfWriter()
    pages = reader.pages
    for i in (range(len(pages)) if indexes is None else indexes):
        writer.add_page(pages[i])
    return writer


def _pages(spec, total):
    """Parse "1-3,7" (1-based, inclusive) into 0-based indexes; "" means every page."""
    text = "" if spec is None else str(spec).strip()
    if not text:
        return list(range(total))
    out = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            lo, hi = sorted((int(m.group(1)), int(m.group(2))))
        elif part.isdigit():
            lo = hi = int(part)
        else:
            raise ValueError("bad page range %r (use e.g. 1-3,7)" % part)
        if lo < 1 or hi > total:
            raise ValueError("page range %r out of bounds (document has %d pages)" % (part, total))
        for page in range(lo, hi + 1):
            if page - 1 not in out:
                out.append(page - 1)
    if not out:
        raise ValueError("no pages selected from %r" % spec)
    return out


def _write_encrypted(reader, out, user_password, owner_password):
    """Encrypt and write, preferring AES-256.

    AES needs the `cryptography` package, which may be missing: pypdf only raises
    DependencyError while write() applies the encryption, so every attempt builds a
    fresh writer and the weaker algorithms are tried in order.
    """
    user = str(user_password)
    owner = str(owner_password or user_password)
    attempts = (("AES-256", {"algorithm": "AES-256"}),
                ("RC4-128", {"algorithm": "RC4-128"}),
                ("default", None))
    last = None
    for label, kwargs in attempts:
        writer = _copy(reader)
        try:
            if kwargs is None:
                writer.encrypt(user, owner)
            else:
                writer.encrypt(user, owner, **kwargs)
            dst = _write(writer, out)
            return dst, len(writer.pages), label
        except (DependencyError, ImportError, AttributeError, TypeError, ValueError) as exc:
            last = exc
    raise ValueError("could not encrypt %s (last error: %s)" % (out, last))


# ----------------------------------------------------------------------- tools
@srv.tool("pdf_info", "Page count, metadata, encryption state and page box sizes of a PDF.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def pdf_info(path):
    p = _in_path(path)
    reader = PdfReader(p)
    lines = ["path=%s" % p, "bytes=%d" % os.path.getsize(p), "encrypted=%s" % bool(reader.is_encrypted)]
    try:
        total = len(reader.pages)
    except Exception as exc:  # noqa: BLE001 - encrypted documents refuse page access
        lines.append("pages=unavailable (%s: %s)" % (type(exc).__name__, exc))
        lines.append("hint=pdf_decrypt needs the password first")
        return "\n".join(lines)
    lines.append("pages=%d" % total)
    try:
        meta = dict(reader.metadata or {})
    except Exception:  # noqa: BLE001
        meta = {}
    lines.append("metadata=%d" % len(meta))
    for key in sorted(meta):
        lines.append("  %s=%s" % (key, meta[key]))
    sizes = []
    for i in range(min(3, total)):
        box = reader.pages[i].mediabox
        sizes.append("%.0fx%.0f" % (float(box.width), float(box.height)))
    lines.append("page_sizes(first %d)=%s" % (len(sizes), ", ".join(sizes)))
    return "\n".join(lines)


@srv.tool("pdf_text", "Extract text from the given pages (\"1-3,7\", empty string = all pages).",
          {"type": "object", "properties": {"path": {"type": "string"}, "pages": {"type": "string", "default": ""}, "max_chars": {"type": "integer", "default": 20000}}, "required": ["path"]})
def pdf_text(path, pages="", max_chars=20000):
    reader = _reader(path)
    indexes = _pages(pages, len(reader.pages))
    limit = max(1, int(max_chars))
    blocks = []
    used = 0
    for i in indexes:
        body = (reader.pages[i].extract_text() or "").strip() or "(no extractable text)"
        blocks.append("[page %d]\n%s" % (i + 1, body))
        used += len(blocks[-1])
        if used >= limit:
            break
    body = "\n\n".join(blocks)[:limit]
    return "path=%s\npages=%s of %d\n%s" % (_in_path(path), pages or "all", len(reader.pages), body)


@srv.tool("pdf_merge", "Merge several PDFs into one output file, in the given order.",
          {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}, "out": {"type": "string"}}, "required": ["paths", "out"]})
def pdf_merge(paths, out):
    items = [str(p) for p in (paths or []) if str(p).strip()]
    if not items:
        raise ValueError("paths must list at least one PDF")
    writer = PdfWriter()
    for item in items:
        for page in _reader(item).pages:
            writer.add_page(page)
    dst = _write(writer, out)
    return _summary(dst, len(writer.pages), "merged=%d" % len(items))


@srv.tool("pdf_split", "Split a PDF into files of `every` pages, written into out_dir.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out_dir": {"type": "string"}, "every": {"type": "integer", "default": 1}}, "required": ["path", "out_dir"]})
def pdf_split(path, out_dir, every=1):
    src = _in_path(path)
    reader = PdfReader(src)
    step = max(1, int(every))
    base = os.path.splitext(os.path.basename(src))[0]
    dst_dir = _out_dir(out_dir)
    total = len(reader.pages)
    written = []
    for start in range(0, total, step):
        stop = min(start + step, total)
        writer = PdfWriter()
        for page in reader.pages[start:stop]:
            writer.add_page(page)
        out = os.path.join(dst_dir, "%s_%03d-%03d.pdf" % (base, start + 1, stop))
        with open(out, "wb") as fh:
            writer.write(fh)
        written.append("%s (%d pages)" % (out, stop - start))
    return "source=%s\npages=%d\nevery=%d\nfiles=%d\n%s" % (src, total, step, len(written), "\n".join(written))


@srv.tool("pdf_extract_pages", "Write a new PDF containing only the given pages, e.g. \"1-3,7\".",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "pages": {"type": "string"}}, "required": ["path", "out", "pages"]})
def pdf_extract_pages(path, out, pages):
    reader = PdfReader(_in_path(path))
    indexes = _pages(pages, len(reader.pages))
    writer = _copy(reader, indexes)
    dst = _write(writer, out)
    return _summary(dst, len(writer.pages), "extracted=%s" % ",".join(str(i + 1) for i in indexes))


@srv.tool("pdf_rotate", "Rotate the given pages (empty string = all) by degrees (multiples of 90).",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "degrees": {"type": "integer", "default": 90}, "pages": {"type": "string", "default": ""}}, "required": ["path", "out"]})
def pdf_rotate(path, out, degrees=90, pages=""):
    angle = int(degrees) % 360
    if angle % 90:
        raise ValueError("degrees must be a multiple of 90, got %r" % degrees)
    reader = PdfReader(_in_path(path))
    indexes = _pages(pages, len(reader.pages))
    writer = _copy(reader)
    for i in indexes:
        writer.pages[i].rotate(angle)
    dst = _write(writer, out)
    return _summary(dst, len(writer.pages), "rotated=%s by %d degrees" % (",".join(str(i + 1) for i in indexes), angle))


@srv.tool("pdf_delete_pages", "Write a copy of a PDF without the given pages, e.g. \"2,5-6\".",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "pages": {"type": "string"}}, "required": ["path", "out", "pages"]})
def pdf_delete_pages(path, out, pages):
    reader = PdfReader(_in_path(path))
    total = len(reader.pages)
    drop = set(_pages(pages, total))
    keep = [i for i in range(total) if i not in drop]
    if not keep:
        raise ValueError("cannot delete every page of %s" % path)
    writer = _copy(reader, keep)
    dst = _write(writer, out)
    return _summary(dst, len(writer.pages), "deleted=%s" % ",".join(str(i + 1) for i in sorted(drop)))


@srv.tool("pdf_metadata_write", "Copy a PDF while setting title/author/subject metadata.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "title": {"type": "string", "default": ""}, "author": {"type": "string", "default": ""}, "subject": {"type": "string", "default": ""}}, "required": ["path", "out"]})
def pdf_metadata_write(path, out, title="", author="", subject=""):
    reader = PdfReader(_in_path(path))
    writer = _copy(reader)
    try:
        meta = dict(reader.metadata or {})
    except Exception:  # noqa: BLE001
        meta = {}
    changed = []
    for key, value in (("/Title", title), ("/Author", author), ("/Subject", subject)):
        if str(value):
            meta[key] = str(value)
            changed.append(key)
    if meta:
        writer.add_metadata({str(k): str(v) for k, v in meta.items()})
    dst = _write(writer, out)
    return _summary(dst, len(writer.pages), "set=%s" % (",".join(changed) if changed else "none"))


@srv.tool("pdf_set_password", "Copy a PDF and encrypt it with a user password (AES-256 when available).",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "user_password": {"type": "string"}, "owner_password": {"type": "string", "default": ""}}, "required": ["path", "out", "user_password"]})
def pdf_set_password(path, out, user_password, owner_password=""):
    if not str(user_password):
        raise ValueError("user_password must not be empty")
    reader = PdfReader(_in_path(path))
    if reader.is_encrypted:
        raise ValueError("source PDF is already encrypted: %s" % path)
    dst, pages, algorithm = _write_encrypted(reader, out, user_password, owner_password)
    lock = PdfReader(dst)
    return _summary(dst, pages, "algorithm=%s\nverify_encrypted=%s" % (algorithm, bool(lock.is_encrypted)))


@srv.tool("pdf_decrypt", "Copy an encrypted PDF to a new unencrypted file using its password.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "password": {"type": "string"}}, "required": ["path", "out", "password"]})
def pdf_decrypt(path, out, password):
    src = _in_path(path)
    reader = PdfReader(src)
    if not reader.is_encrypted:
        writer = _copy(reader)
        dst = _write(writer, out)
        return _summary(dst, len(writer.pages), "note=source was not encrypted")
    if not reader.decrypt(str(password)):
        raise ValueError("wrong password for %s" % src)
    writer = _copy(reader)
    dst = _write(writer, out)
    return _summary(dst, len(writer.pages), "decrypted=yes")


def _seed_pdf():
    """The tools here only read or copy existing documents, so the self-test needs one
    real input file: build a 4-page blank sample once, inside the sample dir."""
    path = os.path.join(SAMPLE_DIR, "seed.pdf")
    try:
        if not os.path.isfile(path):
            os.makedirs(SAMPLE_DIR, exist_ok=True)
            writer = PdfWriter()
            for _ in range(4):
                writer.add_blank_page(width=200, height=300)
            with open(path, "wb") as fh:
                writer.write(fh)
    except OSError:
        pass
    return path


SEED = _seed_pdf()

# Declaration order matters: pdf_decrypt reads the file pdf_set_password writes.
SAMPLES = {
    "pdf_info": {"path": SEED},
    "pdf_text": {"path": SEED, "pages": "", "max_chars": 4000},
    "pdf_merge": {"paths": [SEED, SEED], "out": os.path.join(SAMPLE_DIR, "merged.pdf")},
    "pdf_split": {"path": SEED, "out_dir": os.path.join(SAMPLE_DIR, "split"), "every": 2},
    "pdf_extract_pages": {"path": SEED, "out": os.path.join(SAMPLE_DIR, "extracted.pdf"), "pages": "1-2,4"},
    "pdf_rotate": {"path": SEED, "out": os.path.join(SAMPLE_DIR, "rotated.pdf"), "degrees": 90, "pages": "1-2"},
    "pdf_delete_pages": {"path": SEED, "out": os.path.join(SAMPLE_DIR, "deleted.pdf"), "pages": "2"},
    "pdf_metadata_write": {"path": SEED, "out": os.path.join(SAMPLE_DIR, "meta.pdf"), "title": "Sample", "author": "self-test", "subject": "pdf tools"},
    "pdf_set_password": {"path": SEED, "out": os.path.join(SAMPLE_DIR, "locked.pdf"), "user_password": "sample-pw", "owner_password": "sample-owner"},
    "pdf_decrypt": {"path": os.path.join(SAMPLE_DIR, "locked.pdf"), "out": os.path.join(SAMPLE_DIR, "unlocked.pdf"), "password": "sample-pw"},
}

# Sample tool calls that would need the network or credentials; none here.
SAMPLES_OPTIONAL = set()


def build():
    return srv


if __name__ == "__main__":
    srv.run()
