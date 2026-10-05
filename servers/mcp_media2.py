"""MCP server: image composition extras (collage, grid split, borders, palettes,
diff, upscale, animated GIF, EXIF and text boxes) built on Pillow.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

from PIL import Image, ImageChops, ImageDraw, ImageFont  # noqa: E402

FONT_PATH = r"C:\Windows\Fonts\msyh.ttc"
HINT = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.Resampling.LANCZOS
SAMPLE_DIR = os.path.join(sample_dir(), ".aiyu_self_samples", "media2")

srv = Server("media2")


def _in_path(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(p):
        raise FileNotFoundError(path)
    return p


def _out_path(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    return p


def _out_dir(path):
    d = os.path.abspath(os.path.expanduser(str(path)))
    os.makedirs(d, exist_ok=True)
    return d


def _font(size):
    """msyh.ttc when present, otherwise Pillow's built-in bitmap font."""
    try:
        return ImageFont.truetype(FONT_PATH, int(size))
    except Exception:
        try:
            return ImageFont.load_default(int(size))
        except Exception:
            return ImageFont.load_default()


def _color(value, alpha=None):
    s = str(value).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) == 8:
        try:
            rgba = tuple(int(s[i:i + 2], 16) for i in (0, 2, 4, 6))
            return rgba if alpha is None else rgba[:3] + (max(0, min(255, int(alpha))),)
        except ValueError:
            pass
    if len(s) == 6:
        try:
            rgb = tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
            return rgb if alpha is None else rgb + (max(0, min(255, int(alpha))),)
        except ValueError:
            pass
    return (0, 0, 0) if alpha is None else (0, 0, 0, max(0, min(255, int(alpha))))


def _save(im, dst, **kw):
    """JPEG/BMP cannot hold alpha; flatten those modes before saving."""
    if dst.lower().endswith((".jpg", ".jpeg", ".bmp")) and im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.save(dst, **kw)
    return dst


def _done(prefix, dst, size):
    return "%s %s (%dx%d)" % (prefix, dst, size[0], size[1])


def _fit(im, box):
    """Scale to fit inside box keeping the aspect ratio."""
    fitted = im.copy()
    fitted.thumbnail((max(1, int(box[0])), max(1, int(box[1]))), HINT)
    return fitted


def _exif_dict(raw):
    """Image.Exif -> a plain dict, tolerating odd tags and bytes values."""
    out = {}
    try:
        items = raw.items()
    except Exception:  # noqa: BLE001 - some Exif objects are not dict-like
        return out
    for key, value in items:
        if isinstance(value, bytes):
            value = value[:64].decode("utf-8", errors="replace")
        elif not isinstance(value, (int, float, str)):
            try:
                value = list(value)
            except TypeError:
                value = str(value)
        out[str(key)] = value
    return out


@srv.tool("image_collage", "Lay images out in a grid, each scaled into a cell.",
          {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}, "out": {"type": "string"}, "columns": {"type": "integer", "default": 3}, "cell": {"type": "integer", "default": 300}, "background": {"type": "string", "default": "#ffffff"}}, "required": ["paths", "out"]})
def image_collage(paths, out, columns=3, cell=300, background="#ffffff"):
    if not paths:
        raise ValueError("paths must list at least one image")
    srcs = [_in_path(p) for p in paths]
    dst = _out_path(out)
    cols = max(1, int(columns))
    side = max(1, int(cell))
    rows = (len(srcs) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * side, rows * side), _color(background))
    for index, src in enumerate(srcs):
        with Image.open(src) as im:
            tile = _fit(im.convert("RGBA"), (side, side))
        x = (index % cols) * side + (side - tile.size[0]) // 2
        y = (index // cols) * side + (side - tile.size[1]) // 2
        canvas.paste(tile, (x, y), tile)
    _save(canvas, dst)
    return "%s (images=%d grid=%dx%d)" % (_done("wrote", dst, canvas.size), len(srcs), cols, rows)


@srv.tool("image_split_grid", "Cut an image into rows x cols tiles, written as tile_r1c1.png ...",
          {"type": "object", "properties": {"path": {"type": "string"}, "out_dir": {"type": "string"}, "rows": {"type": "integer", "default": 3}, "cols": {"type": "integer", "default": 3}}, "required": ["path", "out_dir"]})
def image_split_grid(path, out_dir, rows=3, cols=3):
    src = _in_path(path)
    d = _out_dir(out_dir)
    nr = max(1, int(rows))
    nc = max(1, int(cols))
    written = []
    with Image.open(src) as im:
        width, height = im.size
        tile_w = max(1, width // nc)
        tile_h = max(1, height // nr)
        for r in range(nr):
            for c in range(nc):
                box = (c * tile_w, r * tile_h, min(width, (c + 1) * tile_w), min(height, (r + 1) * tile_h))
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                tile = im.crop(box)
                name = "tile_r%dc%d.png" % (r + 1, c + 1)
                _save(tile.convert("RGBA"), os.path.join(d, name))
                written.append(name)
                tile.close()
    return "tiles=%d dir=%s source=%dx%d tile=%dx%d first=%s" % (
        len(written), d, width, height, tile_w, tile_h, written[0] if written else "(none)")


@srv.tool("image_border", "Add a solid border around an image.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "width": {"type": "integer", "default": 10}, "color": {"type": "string", "default": "#000000"}}, "required": ["path", "out"]})
def image_border(path, out, width=10, color="#000000"):
    src, dst = _in_path(path), _out_path(out)
    pad = max(0, int(width))
    with Image.open(src) as im:
        base = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
        canvas = Image.new(base.mode, (base.size[0] + 2 * pad, base.size[1] + 2 * pad), _color(color))
        canvas.paste(base, (pad, pad))
    _save(canvas, dst)
    return "%s width=%d" % (_done("wrote", dst, canvas.size), pad)


@srv.tool("image_compose", "Alpha-blend an overlay onto a base image at (x, y) with opacity 0-100.",
          {"type": "object", "properties": {"base": {"type": "string"}, "overlay": {"type": "string"}, "out": {"type": "string"}, "x": {"type": "integer", "default": 0}, "y": {"type": "integer", "default": 0}, "opacity": {"type": "integer", "default": 100}}, "required": ["base", "overlay", "out"]})
def image_compose(base, overlay, out, x=0, y=0, opacity=100):
    src, over, dst = _in_path(base), _in_path(overlay), _out_path(out)
    pct = max(0, min(100, int(opacity)))
    with Image.open(src) as base_im, Image.open(over) as over_im:
        canvas = base_im.convert("RGBA")
        patch = over_im.convert("RGBA")
        if pct < 100:
            alpha = patch.getchannel("A").point(lambda v: int(v * pct / 100.0))
            patch.putalpha(alpha)
        px, py = int(x), int(y)
        if px < 0 or py < 0 or px + patch.size[0] > canvas.size[0] or py + patch.size[1] > canvas.size[1]:
            raise ValueError("overlay at (%d,%d) %dx%d does not fit the %dx%d base" % (
                px, py, patch.size[0], patch.size[1], canvas.size[0], canvas.size[1]))
        canvas.alpha_composite(patch, (px, py))
    _save(canvas, dst)
    return "%s at (%d,%d) opacity=%d%%" % (_done("wrote", dst, canvas.size), px, py, pct)


@srv.tool("image_palette", "Most frequent colors of an image as hex values with pixel counts.",
          {"type": "object", "properties": {"path": {"type": "string"}, "top": {"type": "integer", "default": 8}}, "required": ["path"]})
def image_palette(path, top=8):
    src = _in_path(path)
    count = max(1, int(top))
    with Image.open(src) as im:
        work = im.convert("RGBA")
        width, height = work.size
        # Fast Octree only accepts RGB, so drop near-transparent pixels and flatten
        # what is left onto white before quantizing.
        solid = Image.new("RGB", (width, height), (255, 255, 255))
        solid.paste(work, (0, 0), work.getchannel("A").point(lambda v: 0 if v < 16 else 255))
        flat = solid
        reduced = flat.quantize(colors=count, method=Image.MEDIANCUT)
        palette = reduced.getpalette() or []
        counts = sorted(reduced.getcolors() or [], reverse=True)
    lines = []
    for hits, index in counts[:count]:
        base = index * 3
        rgb = tuple(palette[base:base + 3]) if len(palette) >= base + 3 else (0, 0, 0)
        lines.append("#%02x%02x%02x  %d px (%.2f%%)" % (
            rgb[0], rgb[1], rgb[2], hits, 100.0 * hits / max(1, width * height)))
    return "path=%s\nsize=%dx%d\n%s" % (src, width, height, "\n".join(lines) or "(no colors)")


@srv.tool("image_compare", "Compare two images: size/mode check, mean absolute difference and a diff image.",
          {"type": "object", "properties": {"path_a": {"type": "string"}, "path_b": {"type": "string"}}, "required": ["path_a", "path_b"]})
def image_compare(path_a, path_b):
    a_path, b_path = _in_path(path_a), _in_path(path_b)
    with Image.open(a_path) as a_im, Image.open(b_path) as b_im:
        if a_im.size != b_im.size:
            return "match=no\nsize_a=%dx%d\nsize_b=%dx%d\nreason=pixel sizes differ" % (
                a_im.size[0], a_im.size[1], b_im.size[0], b_im.size[1])
        if a_im.mode != b_im.mode:
            return "match=no\nmode_a=%s\nmode_b=%s\nsize=%dx%d\nreason=color modes differ" % (
                a_im.mode, b_im.mode, a_im.size[0], a_im.size[1])
        base = a_im.convert("RGB")
        other = b_im.convert("RGB")
        diff = ImageChops.difference(base, other)
    raw = diff.tobytes()
    total = sum(raw)
    width, height = diff.size
    mad = total / float(max(1, len(raw)))
    changed = sum(1 for i in range(0, len(raw), 3) if raw[i] or raw[i + 1] or raw[i + 2])
    dst = _out_path(os.path.splitext(a_path)[0] + "_diff.png")
    _save(diff.point(lambda v: min(255, v * 4)), dst)
    return ("match=%s\nsize=%dx%d\nmean_abs_diff=%.4f (0-255 per channel)\n"
            "pixels_changed=%d/%d (%.2f%%)\ndiff_image=%s (%dx%d)") % (
        "identical" if total == 0 else "different", width, height, mad,
        changed, width * height, 100.0 * changed / max(1, width * height), dst, width, height)


@srv.tool("image_scale_2x", "Upscale an image by a factor with LANCZOS resampling.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "factor": {"type": "integer", "default": 2}}, "required": ["path", "out"]})
def image_scale_2x(path, out, factor=2):
    src, dst = _in_path(path), _out_path(out)
    mult = max(1, int(factor))
    with Image.open(src) as im:
        scaled = im.resize((im.size[0] * mult, im.size[1] * mult), HINT)
    _save(scaled, dst)
    return "%s factor=%d" % (_done("wrote", dst, scaled.size), mult)


@srv.tool("gif_from_images", "Build an animated GIF from images (duration_ms per frame, loop=0 means forever).",
          {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}, "out": {"type": "string"}, "duration_ms": {"type": "integer", "default": 200}, "loop": {"type": "integer", "default": 0}}, "required": ["paths", "out"]})
def gif_from_images(paths, out, duration_ms=200, loop=0):
    if not paths:
        raise ValueError("paths must list at least one image")
    srcs = [_in_path(p) for p in paths]
    dst = _out_path(out)
    frames = []
    try:
        for src in srcs:
            with Image.open(src) as im:
                frames.append(im.convert("RGB"))
        size = frames[0].size
        for index, frame in enumerate(frames):
            if frame.size != size:
                frames[index] = frame.resize(size, HINT)
        frames[0].save(dst, "GIF", save_all=True, append_images=frames[1:],
                       duration=max(10, int(duration_ms)), loop=int(loop), optimize=True)
    finally:
        for frame in frames:
            frame.close()
    return "wrote %s (frames=%d, %dx%d, duration_ms=%d, loop=%d)" % (
        dst, len(srcs), size[0], size[1], max(10, int(duration_ms)), int(loop))


@srv.tool("gif_frames", "Split an animated GIF into PNG frames named frame_01.png ...",
          {"type": "object", "properties": {"path": {"type": "string"}, "out_dir": {"type": "string"}}, "required": ["path", "out_dir"]})
def gif_frames(path, out_dir):
    src = _in_path(path)
    d = _out_dir(out_dir)
    written = []
    with Image.open(src) as im:
        total = int(getattr(im, "n_frames", 1) or 1)
        for index in range(total):
            im.seek(index)
            frame = im.convert("RGBA")
            name = "frame_%02d.png" % (index + 1)
            _save(frame, os.path.join(d, name))
            written.append(name)
            frame.close()
        size = im.size
    return "frames=%d dir=%s source=%s first=%s size=%dx%d" % (
        len(written), d, src, written[0] if written else "(none)", size[0], size[1])


@srv.tool("image_exif_read", "Dump the EXIF tags of an image, plus GPS presence.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def image_exif_read(path):
    src = _in_path(path)
    with Image.open(src) as im:
        info = "%s mode=%s size=%dx%d" % (im.format, im.mode, im.size[0], im.size[1])
        raw = None
        try:
            raw = im.getexif()
        except Exception:  # noqa: BLE001 - formats without EXIF simply have none
            raw = None
        if raw is None:
            return "path=%s\n%s\nexif=(none)" % (src, info)
        tags = _exif_dict(raw)
        gps = {}
        try:
            gps = _exif_dict(raw.get_ifd(0x8825)) if hasattr(raw, "get_ifd") else {}
        except Exception:  # noqa: BLE001
            gps = {}
    if not tags and not gps:
        return "path=%s\n%s\nexif=(empty)" % (src, info)
    rows = ["%s = %s" % (k, v) for k, v in sorted(tags.items())]
    if gps:
        rows.extend("GPS %s = %s" % (k, v) for k, v in sorted(gps.items()))
    return "path=%s\n%s\nexif_tags=%d\ngps_tags=%d\n%s" % (
        src, info, len(tags), len(gps), "\n".join(rows))


@srv.tool("image_exif_strip", "Rewrite an image without its EXIF/metadata blocks.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}}, "required": ["path", "out"]})
def image_exif_strip(path, out):
    src, dst = _in_path(path), _out_path(out)
    with Image.open(src) as im:
        before = 0
        try:
            before = len(_exif_dict(im.getexif()))
        except Exception:  # noqa: BLE001
            before = 0
        fmt = im.format or "PNG"
        clean = im.copy()
    for key in ("exif", "icc_profile", "xmp", "comment", "photoshop"):
        clean.info.pop(key, None)
    clean.info["exif"] = b""
    try:
        _save(clean, dst, exif=b"")
    except (TypeError, ValueError, OSError):
        _save(clean, dst)
    with Image.open(dst) as check:
        after = 0
        try:
            after = len(_exif_dict(check.getexif()))
        except Exception:  # noqa: BLE001
            after = 0
    return "%s format=%s exif_tags %d -> %d" % (_done("wrote", dst, clean.size), fmt, before, after)


@srv.tool("image_add_text_box", "Wrap text into a translucent box drawn at the bottom of an image.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "text": {"type": "string"}, "size": {"type": "integer", "default": 28}, "color": {"type": "string", "default": "#ffffff"}, "box_color": {"type": "string", "default": "#000000cc"}, "margin": {"type": "integer", "default": 8}}, "required": ["path", "out", "text"]})
def image_add_text_box(path, out, text, size=28, color="#ffffff", box_color="#000000cc", margin=8):
    src, dst = _in_path(path), _out_path(out)
    font = _font(size)
    margin_px = max(0, int(margin))
    with Image.open(src) as im:
        canvas = im.convert("RGBA")
        draw = ImageDraw.Draw(canvas, "RGBA")
        width, height = canvas.size
        limit = max(20, width - 2 * margin_px)
        lines = []
        for paragraph in str(text).splitlines() or [""]:
            current = ""
            for word in paragraph.split():
                probe = (current + " " + word).strip()
                try:
                    box = draw.textbbox((0, 0), probe, font=font)
                except Exception:  # noqa: BLE001
                    box = (0, 0, len(probe) * size // 2, size)
                if current and box[2] - box[0] > limit:
                    lines.append(current)
                    current = word
                else:
                    current = probe
            lines.append(current)
        lines = [line for line in lines if line] or [""]
        heights = []
        for line in lines:
            try:
                probe = draw.textbbox((0, 0), line, font=font)
                heights.append(probe[3] - probe[1])
            except Exception:  # noqa: BLE001
                heights.append(size)
        line_height = max(1, max(heights) + max(2, size // 5))
        box_height = line_height * len(lines) + 2 * margin_px
        top = max(0, height - box_height)
        draw.rectangle([0, top, width, height], fill=_color(box_color, alpha=204))
        y = top + margin_px
        for line in lines:
            draw.text((margin_px, y), line, font=font, fill=_color(color, alpha=255))
            y += line_height
    _save(canvas, dst)
    return "%s lines=%d box_height=%d" % (_done("wrote", dst, canvas.size), len(lines), box_height)


def _seed(path, size, color, band=None):
    """Small deterministic sample image; skipped when it already exists."""
    if os.path.isfile(path):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        im = Image.new("RGB", size, color)
        if band:
            draw = ImageDraw.Draw(im)
            draw.rectangle(band, fill=(240, 240, 240))
        im.save(path, "PNG")
        im.close()
    except OSError:
        pass


_SRC_A = os.path.join(SAMPLE_DIR, "src_a.png")
_SRC_B = os.path.join(SAMPLE_DIR, "src_b.png")
_SRC_C = os.path.join(SAMPLE_DIR, "src_c.png")
_OUT = os.path.join(SAMPLE_DIR, "out")
_seed(_SRC_A, (240, 180), (40, 90, 200), (20, 20, 120, 80))
_seed(_SRC_B, (240, 180), (200, 90, 40), (60, 40, 200, 120))
_seed(_SRC_C, (120, 120), (40, 170, 90))
_COLLAGE = os.path.join(_OUT, "collage.png")
_GRID_DIR = os.path.join(_OUT, "grid")
_GIF = os.path.join(_OUT, "anim.gif")
_GIF_FRAMES = os.path.join(_OUT, "gif_frames")
_BORDERED = os.path.join(_OUT, "bordered.png")
_TEXT = os.path.join(_OUT, "text_box.png")

# Ordered: writers before the readers that consume their output; everything lands in
# sample_dir(), so the self-test never writes outside the workspace samples folder.
SAMPLES = {
    "image_collage": {"paths": [_SRC_A, _SRC_B, _SRC_C], "out": _COLLAGE, "columns": 3, "cell": 120, "background": "#ffffff"},
    "image_split_grid": {"path": _COLLAGE, "out_dir": _GRID_DIR, "rows": 3, "cols": 3},
    "image_border": {"path": _SRC_C, "out": _BORDERED, "width": 6, "color": "#000000"},
    "image_compose": {"base": _SRC_A, "overlay": _SRC_C, "out": _OUT + os.sep + "composed.png", "x": 10, "y": 10, "opacity": 60},
    "image_palette": {"path": _SRC_A, "top": 4},
    "image_compare": {"path_a": _SRC_A, "path_b": _SRC_B},
    "image_scale_2x": {"path": _SRC_C, "out": _OUT + os.sep + "scaled.png", "factor": 2},
    "gif_from_images": {"paths": [_SRC_A, _SRC_B, _SRC_C], "out": _GIF, "duration_ms": 120, "loop": 0},
    "gif_frames": {"path": _GIF, "out_dir": _GIF_FRAMES},
    "image_exif_read": {"path": _SRC_A},
    "image_exif_strip": {"path": _SRC_B, "out": _OUT + os.sep + "stripped.png"},
    "image_add_text_box": {"path": _SRC_A, "out": _TEXT, "text": "media2 self-test caption\nsecond wrapped line", "size": 20, "color": "#ffffff", "box_color": "#000000cc", "margin": 6},
}


def build():
    return srv


if __name__ == "__main__":
    srv.run()
