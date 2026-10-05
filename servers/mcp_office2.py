"""MCP server: Office document editing -- docx tables/replace/merge/markdown,
xlsx sheets/formulas/charts/markdown, pptx images/outline decks, plus office_info.

Builds on python-docx / openpyxl / python-pptx. Line protocols follow the project
convention: ';'-separated rows of ','-separated cells, and 'A1=SUM(B1:B9)' formulas.
Text output is ASCII with \\uXXXX escapes for anything outside Latin-1.
"""
import copy
import os
import re
import sys
import zipfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

import openpyxl  # noqa: E402
from docx import Document  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.table import Table  # noqa: E402
from docx.text.paragraph import Paragraph  # noqa: E402
from openpyxl.chart import BarChart, LineChart, PieChart, Reference  # noqa: E402
from openpyxl.utils.cell import range_boundaries  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.util import Inches  # noqa: E402

MAX_ROWS = 200
TEXT_CAP = 200
DEFAULT_HEADER = "name,city,score"
DEFAULT_ROWS = "name,city,score;alice,oslo,91;bob,lima,78"

srv = Server("office2")

_pil_cache = {}


def _in_path(path):
    """Resolve an input path; a missing input becomes a clear isError result."""
    p = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(p):
        raise FileNotFoundError(path)
    return p


def _out_path(path):
    """Resolve an output path, creating its parent directories."""
    p = os.path.abspath(os.path.expanduser(str(path)))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    return p


def _docx_open(path, create=True):
    """Open a .docx, or start a brand-new one when create=True and it does not exist."""
    p = os.path.abspath(os.path.expanduser(str(path)))
    if os.path.isfile(p):
        return p, Document(p)
    if not create:
        raise FileNotFoundError(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    return p, Document()


def _xlsx_open(path, create=True):
    """Open a workbook, or start a brand-new one when create=True and it does not exist."""
    p = os.path.abspath(os.path.expanduser(str(path)))
    if os.path.isfile(p):
        return p, openpyxl.load_workbook(p)
    if not create:
        raise FileNotFoundError(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    return p, openpyxl.Workbook()


def _parse_rows(rows):
    """';'-separated lines of ','-separated cells -> list of list of str."""
    out = []
    for line in str(rows if rows is not None else "").split(";"):
        line = line.strip()
        if not line:
            continue
        out.append([c.strip() for c in line.split(",")])
    return out


def _as_list(rows):
    """Accept the line protocol, a list of rows, or a single row dict/list."""
    if isinstance(rows, str):
        return _parse_rows(rows)
    if isinstance(rows, (list, tuple)):
        out = []
        for row in rows:
            if isinstance(row, (list, tuple)):
                out.append(["" if v is None else str(v) for v in row])
            else:
                out.append(["" if row is None else str(row)])
        return out
    if rows is None:
        return []
    return [[str(rows)]]


def _clip(text, n=TEXT_CAP):
    s = str(text).strip()
    return s if len(s) <= n else s[:n] + "..."


def _cell(value):
    return "" if value is None else str(value)


def _key(text):
    return str(text).strip().lower().replace(" ", "").replace("_", "")


# --------------------------------------------------------------------------- docx


def _set_para_text(para, new):
    """Replace a paragraph's text across its runs, keeping each run's formatting."""
    runs = list(para.runs)
    if not runs:
        para.add_run(new)
        return 0
    spans = [len(r.text or "") for r in runs]
    start = 0
    for i, run in enumerate(runs):
        length = spans[i]
        if start <= len(new) <= start + length or i == len(runs) - 1:
            head = new[:start]
            tail = new[start:]
            run.text = tail
            for j in range(i + 1, len(runs)):
                runs[j].text = ""
            if head:
                runs[0].text = head
            return 1
        start += length
    return 0


def _iter_para_holder(doc):
    yield doc.paragraphs
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                yield cell.paragraphs
    for section in doc.sections:
        for part in (section.header, section.footer):
            if part is None:
                continue
            try:
                yield part.paragraphs
            except Exception:
                continue
            try:
                for tbl in part.tables:
                    for row in tbl.rows:
                        for cell in row.cells:
                            yield cell.paragraphs
            except Exception:
                continue


def _heading_level(para):
    """1-6 for a heading paragraph, 0 otherwise (handles localized style names)."""
    try:
        style = para.style
        name = _key(style.name if style is not None else "")
        style_id = _key(getattr(style, "style_id", "") or "")
    except Exception:
        return 0
    for tag in ("heading", "title", "\u6807\u9898"):
        if tag in style_id or tag in name:
            m = re.search(r"([1-9])", style_id or name)
            if m:
                return min(6, int(m.group(1)))
            if "title" in tag or "\u6807\u9898" == tag:
                return 1
    return 0


def _is_list(para):
    try:
        name = _key(para.style.name if para.style is not None else "")
        sid = _key(getattr(para.style, "style_id", "") or "")
    except Exception:
        return False
    if "listbullet" in sid or "listnumber" in sid:
        return True
    if "listparagraph" in name and ("bullet" in name or "number" in name):
        return True
    if "bullet" in name or "numbered" in name:
        return True
    ppr = getattr(para._p, "pPr", None)
    if ppr is not None and ppr.numPr is not None:
        return True
    return False


def _run_md(run):
    text = run.text or ""
    if not text:
        return ""
    if run.bold and text.strip():
        leader = text[:len(text) - len(text.lstrip())]
        trailer = text[len(text.rstrip()):]
        return leader + "**" + text.strip() + "**" + trailer
    return text


def _para_md(para, lines):
    text = para.text or ""
    if not text.strip():
        return False
    level = _heading_level(para)
    if level:
        lines.append("#" * level + " " + text.strip())
        return True
    if _is_list(para):
        lines.append("- " + text.strip())
        return True
    body = "".join(_run_md(r) for r in para.runs) or text
    lines.append(body.strip())
    return True


def _table_md(tbl, lines):
    rows = []
    for row in tbl.rows:
        rows.append([(c.text or "").replace("\n", " ").strip() for c in row.cells])
    if not rows:
        return
    width = max(len(r) for r in rows)
    for r in rows:
        while len(r) < width:
            r.append("")
    lines.append("| " + " | ".join(rows[0]) + " |")
    lines.append("| " + " | ".join(["---"] * width) + " |")
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")


def _md_lines(block):
    """Markdown for a paragraph holder (doc.paragraphs or a table cell)."""
    out = []
    for para in block:
        _para_md(para, out)
    return out


def _doc_md(doc):
    lines = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            _para_md(Paragraph(child, doc), lines)
        elif tag == "tbl":
            _table_md(Table(child, doc), lines)
    for section in doc.sections:
        try:
            header = section.header
        except Exception:
            header = None
        if header is not None and not getattr(header, "is_linked_to_previous", True):
            found = any((p.text or "").strip() for p in header.paragraphs)
            if found:
                lines.append("")
                lines.append("<!-- header -->")
                lines.extend(_md_lines(header.paragraphs))
        try:
            footer = section.footer
        except Exception:
            footer = None
        if footer is not None and not getattr(footer, "is_linked_to_previous", True):
            found = any((p.text or "").strip() for p in footer.paragraphs)
            if found:
                lines.append("")
                lines.append("<!-- footer -->")
                lines.extend(_md_lines(footer.paragraphs))
    return lines


def _copy_docx_body(src, dst):
    """Append the source body's paragraphs/tables to dst, page break before them."""
    body = dst.element.body
    para = dst.add_paragraph()
    try:
        run = para.add_run()
        br = run._r.makeelement(qn("w:br"), {})
        br.set(qn("w:type"), "page")
        run._r.append(br)
    except Exception:
        pass
    src_body = src.element.body
    for child in list(src_body.iterchildren()):
        if child.tag.split("}")[-1] == "sectPr":
            continue
        body.insert_element_before(copy.deepcopy(child), "w:sectPr")
    return para


def _docx_counts(doc):
    tables = len(doc.tables)
    paras = sum(1 for p in doc.paragraphs if (p.text or "").strip())
    return paras, tables


@srv.tool("docx_add_table",
          "Append a table to a .docx. rows is ';'-separated lines of ','-separated cells.",
          {"type": "object",
           "properties": {"path": {"type": "string"}, "rows": {"type": "string", "default": DEFAULT_ROWS},
                          "header": {"type": "boolean", "default": True},
                          "style": {"type": "string", "default": "Table Grid"}},
           "required": ["path"]})
def docx_add_table(path, rows=DEFAULT_ROWS, header=True, style="Table Grid"):
    p, doc = _docx_open(path)
    grid = _as_list(rows)
    if not grid:
        return "docx_add_table: no rows parsed from %r" % _clip(rows)
    width = max(len(r) for r in grid)
    try:
        tbl = doc.add_table(rows=len(grid), cols=width)
    except Exception as exc:
        return "docx_add_table: cannot build %dx%d table: %s" % (len(grid), width, exc)
    try:
        tbl.style = str(style or "Table Grid")
    except Exception:
        pass
    for i, row in enumerate(grid):
        for j, val in enumerate(row):
            tbl.cell(i, j).text = _cell(val)
    if header:
        for cell in tbl.rows[0].cells:
            for para in cell.paragraphs:
                for run in para.runs:
                    run.bold = True
    doc.save(p)
    return "docx_add_table %s: %dx%d header=%s style=%r" % (p, len(grid), width, bool(header), str(style))


@srv.tool("docx_replace",
          "Replace a string in a .docx across paragraphs, tables and headers/footers "
          "(count=0 means all occurrences).",
          {"type": "object",
           "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string", "default": ""},
                          "count": {"type": "integer", "default": 0}},
           "required": ["path", "old"]})
def docx_replace(path, old, new="", count=0):
    p = _in_path(path)
    old_s = str(old)
    new_s = "" if new is None else str(new)
    if not old_s:
        return "docx_replace: 'old' is empty, nothing to do"
    try:
        limit = int(count or 0)
    except (TypeError, ValueError):
        limit = 0
    doc = Document(p)
    seen = set()
    blocks = []
    for holder in _iter_para_holder(doc):
        if id(holder) in seen:
            continue
        seen.add(id(holder))
        blocks.append(holder)
    hits = 0
    for paras in blocks:
        for para in paras:
            if limit and hits >= limit:
                break
            text = para.text or ""
            if old_s not in text:
                continue
            if limit:
                text = text.replace(old_s, new_s, limit - hits)
            else:
                text = text.replace(old_s, new_s)
            hits += _set_para_text(para, text)
        if limit and hits >= limit:
            break
    if hits:
        doc.save(p)
    return "docx_replace %s: %d replacement(s) of %r -> %r (count=%s)" % (p, hits, _clip(old_s, 80), _clip(new_s, 80), count)


@srv.tool("docx_merge",
          "Concatenate several .docx files into one, page break between files, keeping document order.",
          {"type": "object", "properties": {"paths": {"type": "string"}, "out": {"type": "string"}},
           "required": ["paths", "out"]})
def docx_merge(paths, out):
    srcs = [s.strip() for s in re.split(r"[;,\n]+", str(paths)) if s.strip()]
    if not srcs:
        return "docx_merge: no input paths given"
    dst = _out_path(out)
    merged = Document()
    files = []
    paras = 0
    tables = 0
    for i, src in enumerate(srcs):
        sp = _in_path(src)
        sdoc = Document(sp)
        if i:
            _copy_docx_body(sdoc, merged)
        else:
            for child in list(sdoc.element.body.iterchildren()):
                if child.tag.split("}")[-1] == "sectPr":
                    continue
                merged.element.body.insert_element_before(copy.deepcopy(child), "w:sectPr")
        sp_count, tb_count = _docx_counts(sdoc)
        paras += sp_count
        tables += tb_count
        files.append(os.path.basename(sp))
    merged.save(dst)
    return ("docx_merge %s: %d file(s) -> paragraphs=%d tables=%d bytes=%d files=%s"
            % (dst, len(files), paras, tables, os.path.getsize(dst), ", ".join(files)))


@srv.tool("docx_to_markdown", "Export a .docx to Markdown: headings, bullets, bold runs and tables.",
          {"type": "object",
           "properties": {"path": {"type": "string"}, "out": {"type": "string", "default": ""}},
           "required": ["path"]})
def docx_to_markdown(path, out=""):
    p = _in_path(path)
    doc = Document(p)
    lines = _doc_md(doc)
    body = "\n".join(lines)
    if not str(out).strip():
        return "docx_to_markdown %s: headings/bullets/bold/tables=%d line(s)\n%s" % (p, len(lines), body)
    dst = _out_path(out)
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body + ("\n" if body else ""))
    return "docx_to_markdown %s -> %s: lines=%d bytes=%d" % (p, dst, len(lines), os.path.getsize(dst))


# --------------------------------------------------------------------------- xlsx


@srv.tool("xlsx_add_sheet", "Add (or replace) a sheet in a workbook and fill it from row data.",
          {"type": "object",
           "properties": {"path": {"type": "string"}, "name": {"type": "string", "default": "Sheet"},
                          "rows": {"type": "string", "default": DEFAULT_ROWS}},
           "required": ["path"]})
def xlsx_add_sheet(path, name="Sheet", rows=DEFAULT_ROWS):
    p, wb = _xlsx_open(path)
    grid = _as_list(rows)
    title = str(name or "Sheet")[:31] or "Sheet"
    if title in wb.sheetnames:
        try:
            del wb[title]
        except Exception:
            pass
    ws = wb.create_sheet(title)
    for row in grid:
        ws.append(list(row))
    wb.save(p)
    return "xlsx_add_sheet %s: sheet=%r rows=%d cols=%d sheets=%d" % (
        p, ws.title, len(grid), max([len(r) for r in grid] or [0]), len(wb.sheetnames))


@srv.tool("xlsx_set_formulas",
          "Set formulas in a workbook. cells is ';'- or newline-separated 'A1=SUM(B1:B9)' entries.",
          {"type": "object",
           "properties": {"path": {"type": "string"}, "cells": {"type": "string"},
                          "sheet": {"type": "string", "default": ""}},
           "required": ["path", "cells"]})
def xlsx_set_formulas(path, cells, sheet=""):
    p, wb = _xlsx_open(path)
    pairs = []
    for chunk in re.split(r"[;\n]+", str(cells)):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        ref, formula = chunk.split("=", 1)
        ref = ref.strip().lstrip("=").strip()
        formula = formula.strip()
        if not ref or not formula:
            continue
        pairs.append((ref, formula))
    if not pairs:
        return "xlsx_set_formulas: no 'A1=FORMULA' entries parsed from %r" % _clip(cells, 120)
    ws = wb[str(sheet)] if str(sheet).strip() and str(sheet) in wb.sheetnames else wb[wb.sheetnames[0]]
    applied = []
    for ref, formula in pairs:
        try:
            ws[ref] = formula if formula.startswith("=") else "=" + formula
        except Exception as exc:
            applied.append("%s: FAILED (%s)" % (ref, exc))
            continue
        applied.append("%s=%s" % (ref, ws[ref].value))
    wb.save(p)
    wb2 = openpyxl.load_workbook(p)
    ws2 = wb2[str(sheet)] if str(sheet).strip() and str(sheet) in wb2.sheetnames else wb2[wb2.sheetnames[0]]
    check = "; ".join("%s=%s" % (ref, ws2[ref].value) for ref, _f in pairs[:4])
    wb2.close()
    return "xlsx_set_formulas %s: sheet=%r cells=%d applied=%s\nreload check: %s" % (
        p, ws.title, len(pairs), "; ".join(applied[:4]), check)


CHART_KINDS = {"bar": BarChart, "barh": BarChart, "line": LineChart, "pie": PieChart}


@srv.tool("xlsx_chart",
          "Create a bar/line/pie chart from a data range and embed it in the workbook "
          "(saved to out when out differs from path; title auto-filled when empty).",
          {"type": "object",
           "properties": {"path": {"type": "string"}, "out": {"type": "string", "default": ""},
                          "kind": {"type": "string", "default": "bar"},
                          "data_range": {"type": "string", "default": ""}, "title": {"type": "string", "default": ""}},
           "required": ["path"]})
def xlsx_chart(path, out="", kind="bar", data_range="", title=""):
    src = _in_path(path)
    kind_key = _key(kind)
    if kind_key not in CHART_KINDS:
        return "xlsx_chart: unsupported kind %r (use bar/barh/line/pie)" % kind
    spec = str(data_range).strip()
    if not spec:
        probe = openpyxl.load_workbook(src, read_only=True)
        try:
            ws0 = probe[probe.sheetnames[0]]
            spec = "%s!%s" % (ws0.title, ws0.calculate_dimension())
        finally:
            probe.close()
    sheet_name = ""
    rng = spec
    if "!" in spec:
        sheet_name, rng = spec.split("!", 1)
        sheet_name = sheet_name.strip().strip("'")
    try:
        min_col, min_row, max_col, max_row = range_boundaries(rng.replace("$", "").strip())
    except Exception as exc:
        return "xlsx_chart: cannot parse data_range %r: %s" % (data_range, exc)
    if min_col is None:
        return "xlsx_chart: empty data_range %r" % data_range
    dst = _out_path(out) if str(out).strip() else src
    wb = openpyxl.load_workbook(src)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb[wb.sheetnames[0]]
    rows = ws.max_row or 0
    cols = ws.max_column or 0
    if max_row > rows:
        max_row = rows
    if max_col > cols:
        max_col = cols
    if max_row < min_row or max_col < min_col:
        wb.close()
        return "xlsx_chart: range %s is outside the used area (%dx%d) of sheet %r" % (rng, rows, cols, ws.title)
    values = wb.create_sheet("ChartValues")
    values.append(["category", "series"])
    for i in range(min_col, max_col + 1):
        if min_row < max_row:
            col_name = _cell(ws.cell(row=min_row, column=i).value) or "col%d" % i
            for r in range(min_row + 1, max_row + 1):
                cat = _cell(ws.cell(row=r, column=min_col).value)
                if i == min_col:
                    cat = _cell(ws.cell(row=r, column=min_col).value)
                values.append([cat, ws.cell(row=r, column=i).value])
        else:
            values.append([_cell(ws.cell(row=min_row, column=i).value), 1])
    ref = Reference(values, min_col=2, min_row=1, max_row=values.max_row)
    cats = Reference(values, min_col=1, min_row=2, max_row=values.max_row)
    chart = CHART_KINDS[kind_key]()
    chart.add_data(ref, titles_from_data=True)
    chart.set_categories(cats)
    chart.title = str(title or "%s %s of %s" % (ws.title, kind_key, rng))
    chart.height = 8
    chart.width = 16
    target_sheet = "Chart"
    if target_sheet in wb.sheetnames:
        target_sheet = "Chart2"
    charts_ws = wb.create_sheet(target_sheet)
    charts_ws.add_chart(chart, "B2")
    wb.save(dst)
    wb.close()
    check = openpyxl.load_workbook(dst)
    try:
        sheets = list(check.sheetnames)
        ok = target_sheet in sheets
    finally:
        check.close()
    return ("xlsx_chart %s: kind=%s range=%s!%s chart_sheet=%r categories=%d series=%d "
            "reopen_ok=%s sheets=%s" % (dst, kind_key, ws.title, rng, target_sheet,
                                        values.max_row - 1, max(1, max_col - min_col), ok, sheets))


@srv.tool("xlsx_to_markdown", "Export a sheet to a Markdown table.",
          {"type": "object",
           "properties": {"path": {"type": "string"}, "sheet": {"type": "string", "default": ""},
                          "max_rows": {"type": "integer", "default": MAX_ROWS}},
           "required": ["path"]})
def xlsx_to_markdown(path, sheet="", max_rows=MAX_ROWS):
    p = _in_path(path)
    try:
        limit = max(1, int(max_rows))
    except (TypeError, ValueError):
        limit = MAX_ROWS
    wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
    try:
        ws = wb[str(sheet)] if str(sheet).strip() and str(sheet) in wb.sheetnames else wb[wb.sheetnames[0]]
        lines = []
        total = 0
        header_done = False
        for row in ws.iter_rows(values_only=True):
            total += 1
            if len(lines) >= limit + 2:
                continue
            vals = [_cell(v) for v in row]
            while vals and vals[-1] == "":
                vals.pop()
            if not vals:
                continue
            if not header_done:
                lines.append("| " + " | ".join(vals) + " |")
                lines.append("| " + " | ".join(["---"] * len(vals)) + " |")
                header_done = True
            else:
                lines.append("| " + " | ".join(vals) + " |")
        title = ws.title
    finally:
        wb.close()
    note = ""
    if total > limit:
        note = "\n[truncated at %d of %d rows]" % (limit, total)
    if not lines:
        lines = ["(empty sheet)"]
    return "xlsx_to_markdown %s: sheet=%r rows=%d%s\n%s" % (p, title, total, note, "\n".join(lines))


# --------------------------------------------------------------------------- pptx


@srv.tool("pptx_add_image",
          "Insert an image into an existing slide (slide=0 creates a blank slide); "
          "width/height 0 keeps the aspect ratio.",
          {"type": "object",
           "properties": {"path": {"type": "string"}, "image": {"type": "string"},
                          "slide": {"type": "integer", "default": 0}, "left": {"type": "number", "default": 0},
                          "top": {"type": "number", "default": 0}, "width": {"type": "number", "default": 0},
                          "height": {"type": "number", "default": 0}},
           "required": ["path", "image"]})
def pptx_add_image(path, image, slide=0, left=0, top=0, width=0, height=0):
    p = _in_path(path)
    img = _in_path(image)
    prs = Presentation(p)
    try:
        idx = int(slide)
    except (TypeError, ValueError):
        idx = 0
    try:
        lf, tp = float(left or 0), float(top or 0)
    except (TypeError, ValueError):
        lf, tp = 0.0, 0.0
    try:
        wf, hf = float(width or 0), float(height or 0)
    except (TypeError, ValueError):
        wf, hf = 0.0, 0.0
    created = False
    if idx <= 0:
        try:
            sl = prs.slides.add_slide(prs.slide_layouts[6])
        except Exception:
            sl = prs.slides.add_slide(prs.slide_layouts[0])
        idx = len(prs.slides._sldIdLst)
        created = True
    else:
        if idx > len(prs.slides._sldIdLst):
            return "pptx_add_image: slide=%d is out of range (%d slide(s))" % (idx, len(prs.slides._sldIdLst))
        sl = prs.slides[idx - 1]
    kw = {}
    if lf:
        kw["left"] = Inches(lf)
    elif lf == 0:
        kw["left"] = Inches(1.0)
    if tp:
        kw["top"] = Inches(tp)
    else:
        kw["top"] = Inches(1.0)
    if wf and hf:
        kw["width"], kw["height"] = Inches(wf), Inches(hf)
    elif wf:
        kw["width"] = Inches(wf)
    elif hf:
        kw["height"] = Inches(hf)
    pic = sl.shapes.add_picture(img, **kw)
    prs.save(p)
    return ("pptx_add_image %s: %s -> slide %d%s size=%dx%d EMU (%.2f MB), bytes=%d"
            % (p, os.path.basename(img), idx, " (new blank slide)" if created else "",
               int(pic.width or 0), int(pic.height or 0), os.path.getsize(p) / 1e6, os.path.getsize(img)))


def _parse_outline(outline):
    """'# Title' starts a slide, '- bullet' adds a line, anything else joins the last block."""
    blocks = []
    current = None
    for raw in re.split(r"[\r\n]+", str(outline if outline is not None else "")):
        line = raw.strip()
        if not line:
            continue
        level = 0
        content = line
        while content.startswith("#"):
            level += 1
            content = content[1:]
        if line.startswith("#"):
            if current is not None:
                blocks.append(current)
            current = {"title": content.strip(), "bullets": []}
            continue
        if current is None:
            current = {"title": content.strip(), "bullets": []}
            continue
        if line.startswith("-") or line.startswith("*"):
            current["bullets"].append(line[1:].strip())
        elif line.startswith("+"):
            current["bullets"].append(line[1:].strip())
        else:
            current["bullets"].append(line)
    if current is not None:
        blocks.append(current)
    return blocks


@srv.tool("pptx_from_outline", "Build a .pptx from '# Title' / '- bullet' outline lines.",
          {"type": "object", "properties": {"out": {"type": "string"}, "outline": {"type": "string"}},
           "required": ["out", "outline"]})
def pptx_from_outline(out, outline):
    dst = _out_path(out)
    blocks = _parse_outline(outline)
    if not blocks:
        return "pptx_from_outline: no '# Title' / '- bullet' lines parsed"
    prs = Presentation()
    count = 0
    bullets = 0
    for block in blocks:
        title = None
        for layout in prs.slide_layouts:
            try:
                if len(layout.placeholders) and hasattr(layout.placeholders[0], "placeholder_format") \
                        and layout.placeholders[0].placeholder_format.idx == 0:
                    title = layout
                    break
            except Exception:
                continue
        if title is None:
            title = prs.slide_layouts[0]
        slide = prs.slides.add_slide(title)
        try:
            slide.shapes.title.text = block["title"]
        except Exception:
            pass
        title_id = -1
        try:
            if slide.shapes.title is not None:
                title_id = slide.shapes.title.shape_id
        except Exception:
            pass
        body = None
        for shape in slide.placeholders:
            if shape.shape_id != title_id and getattr(shape, "has_text_frame", False):
                body = shape.text_frame
                break
        if body is not None:
            body.clear()
            for i, item in enumerate(block["bullets"]):
                para = body.paragraphs[0] if i == 0 else body.add_paragraph()
                para.text = item
                para.level = 0
                bullets += 1
        count += 1
    prs.save(dst)
    return "pptx_from_outline %s: slides=%d bullets=%d bytes=%d" % (dst, count, bullets, os.path.getsize(dst))


# -------------------------------------------------------------------- office_info


def _media_count(path):
    try:
        with zipfile.ZipFile(path) as zf:
            return sum(1 for n in zf.namelist()
                       if n.startswith(("word/media/", "ppt/media/", "xl/media/")))
    except Exception:
        return 0


@srv.tool("office_info",
          "Report type, paragraph/sheet/slide counts, dimensions and embedded media for a docx/xlsx/pptx.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def office_info(path):
    p = _in_path(path)
    size = os.path.getsize(p)
    mtime = datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S")
    ext = os.path.splitext(p)[1].lower()
    lines = ["office_info %s", "ext=%s bytes=%d modified=%s" % (ext, size, mtime)]
    head = lines[0] % p
    kind = ext.lstrip(".") or "unknown"
    if ext == ".docx":
        doc = Document(p)
        paras = sum(1 for para in doc.paragraphs if (para.text or "").strip())
        headings = sum(1 for para in doc.paragraphs if _heading_level(para))
        tables = len(doc.tables)
        rows = sum(len(t.rows) for t in doc.tables)
        cells = 0
        for t in doc.tables:
            for row in t.rows:
                cells += len(row.cells)
        words = sum(len((para.text or "").split()) for para in doc.paragraphs)
        for t in doc.tables:
            for row in t.rows:
                for c in row.cells:
                    words += len((c.text or "").split())
        sec = doc.sections[0] if doc.sections else None
        dim = ""
        if sec is not None:
            dim = " page=%.2fx%.2f in" % (emu_in(sec.page_width), emu_in(sec.page_height))
        lines = [head, "ext=docx bytes=%d modified=%s" % (size, mtime),
                 "paragraphs=%d (non-empty) headings=%d words=%d" % (paras, headings, words),
                 "tables=%d table_rows=%d table_cells=%d media=%d" % (tables, rows, cells, _media_count(p)),
                 "sections=%d%s" % (len(doc.sections), dim)]
        kind = "docx"
    elif ext in (".xlsx", ".xlsm"):
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        try:
            names = list(wb.sheetnames)
            details = []
            for n in names:
                ws = wb[n]
                details.append("%s=%dx%d" % (n, ws.max_row or 0, ws.max_column or 0))
        finally:
            wb.close()
        lines = [head, "ext=%s bytes=%d modified=%s" % (ext, size, mtime),
                 "sheets=%d" % len(names),
                 "dimensions: " + ", ".join(details[:12]),
                 "media=%d" % _media_count(p)]
        kind = "xlsx"
    elif ext == ".pptx":
        prs = Presentation(p)
        slides = len(prs.slides._sldIdLst)
        pictures = 0
        charts = 0
        tables = 0
        texts = 0
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.shape_type == 13 or getattr(shape, "image", None) is not None:
                    pictures += 1
                if getattr(shape, "has_chart", False):
                    charts += 1
                if getattr(shape, "has_table", False):
                    tables += 1
                if getattr(shape, "has_text_frame", False):
                    texts += len([x for x in shape.text_frame.paragraphs if (x.text or "").strip()])
        lines = [head, "ext=pptx bytes=%d modified=%s" % (size, mtime),
                 "slides=%d size=%.2fx%.2f in" % (slides, emu_in(prs.slide_width), emu_in(prs.slide_height)),
                 "pictures=%d (media=%d) charts=%d tables=%d text_paragraphs=%d"
                 % (pictures, _media_count(p), charts, tables, texts)]
        kind = "pptx"
    else:
        lines = [head, "ext=%s bytes=%d modified=%s" % (ext, size, mtime),
                 "unrecognized type: expected .docx, .xlsx or .pptx"]
        kind = "other"
    return "type=%s\n%s" % (kind, "\n".join(lines))


def emu_in(value):
    try:
        return float(value or 0) / Inches(1)
    except Exception:
        return 0.0


def build():
    return srv


SAMPLE_DIR = os.path.join(sample_dir(), ".aiyu_selftest", "office2")
S_DOCX = os.path.join(SAMPLE_DIR, "office2_sample.docx")
S_DOCX2 = os.path.join(SAMPLE_DIR, "office2_sample2.docx")
S_MERGED = os.path.join(SAMPLE_DIR, "office2_merged.docx")
S_MD = os.path.join(SAMPLE_DIR, "office2_sample.md")
S_XLSX = os.path.join(SAMPLE_DIR, "office2_sample.xlsx")
S_CHART = os.path.join(SAMPLE_DIR, "office2_chart.xlsx")
S_XMD = os.path.join(SAMPLE_DIR, "office2_sample.md")
S_PPTX = os.path.join(SAMPLE_DIR, "office2_sample.pptx")
S_OUTLINE = os.path.join(SAMPLE_DIR, "office2_outline.pptx")
S_PNG = os.path.join(SAMPLE_DIR, "office2_image.png")


def _sample_png():
    """Draw the sample image once, inside sample_dir(); returns '' when Pillow is absent."""
    path = S_PNG
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (320, 180), (32, 64, 128))
        draw = ImageDraw.Draw(img)
        draw.rectangle([20, 20, 300, 160], outline=(255, 200, 0), width=3)
        draw.text((40, 80), "office2 sample", fill=(255, 255, 255))
        img.save(path)
        return path
    except Exception:
        return ""


def _sample_docx():
    """Build the docx sample the readers depend on; returns its path."""
    path = S_DOCX
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        doc = Document()
        doc.add_heading("office2 sample", level=1)
        doc.add_paragraph("alice reviewed the first draft.")
        para = doc.add_paragraph()
        para.add_run("Bold lead ")
        para.add_run("and plain tail.")
        doc.add_paragraph("first bullet", style="List Bullet")
        doc.save(path)
    except Exception:
        return ""
    return path


def _sample_docx2():
    path = S_DOCX2
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        doc = Document()
        doc.add_heading("Second document", level=1)
        doc.add_paragraph("second file body line")
        doc.save(path)
    except Exception:
        return ""
    return path


def _sample_xlsx():
    path = S_XLSX
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Report"
        for row in _parse_rows(DEFAULT_ROWS):
            ws.append(row)
        wb.save(path)
    except Exception:
        return ""
    return path


def _sample_pptx():
    path = S_PPTX
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = "office2 sample"
        body = None
        for shape in slide.placeholders:
            if shape.shape_id != slide.shapes.title.shape_id and getattr(shape, "has_text_frame", False):
                body = shape.text_frame
                break
        if body is not None:
            body.clear()
            body.paragraphs[0].text = "sample bullet"
        prs.save(path)
    except Exception:
        return ""
    return path


SAMPLE_DOCS = {
    "docx_add_table": {"path": S_DOCX, "rows": DEFAULT_ROWS, "header": True, "style": "Table Grid"},
    "docx_replace": {"path": S_DOCX, "old": "alice", "new": "ALICE", "count": 0},
    "docx_to_markdown": {"path": S_DOCX, "out": S_MD},
    "xlsx_add_sheet": {"path": S_XLSX, "name": "Report", "rows": DEFAULT_ROWS},
    "xlsx_set_formulas": {"path": S_XLSX, "cells": "E1=SUM(C2:C3);E2=COUNTA(A2:A3)", "sheet": "Report"},
    "xlsx_to_markdown": {"path": S_XLSX, "sheet": "Report", "max_rows": 20},
    "xlsx_chart": {"path": S_XLSX, "out": S_CHART, "kind": "bar", "data_range": "", "title": "Scores"},
    "pptx_add_image": {"path": S_PPTX, "image": S_PNG, "slide": 0},
    "pptx_from_outline": {"out": S_OUTLINE, "outline": "# Intro;- first point;- second point;# Detail;- one more"},
}

# Ordered so the writers run before the readers that consume their outputs.
SAMPLES = {
    "docx_add_table": dict(SAMPLE_DOCS["docx_add_table"]),
    "docx_replace": dict(SAMPLE_DOCS["docx_replace"]),
    "docx_to_markdown": dict(SAMPLE_DOCS["docx_to_markdown"]),
    "docx_merge": {"paths": "%s;%s" % (S_DOCX, S_DOCX2), "out": S_MERGED},
    "office_info": {"path": S_DOCX},
    "xlsx_add_sheet": dict(SAMPLE_DOCS["xlsx_add_sheet"]),
    "xlsx_set_formulas": dict(SAMPLE_DOCS["xlsx_set_formulas"]),
    "xlsx_to_markdown": dict(SAMPLE_DOCS["xlsx_to_markdown"]),
    "xlsx_chart": dict(SAMPLE_DOCS["xlsx_chart"]),
    "pptx_add_image": dict(SAMPLE_DOCS["pptx_add_image"]),
    "pptx_from_outline": dict(SAMPLE_DOCS["pptx_from_outline"]),
}

SAMPLES_OPTIONAL = {}


def _seed_samples():
    """Make every sample self-contained under sample_dir() before the bridge runs them."""
    _sample_docx()
    _sample_docx2()
    _sample_xlsx()
    _sample_pptx()
    _sample_png()


_seed_samples()


if __name__ == "__main__":
    srv.run()
