"""MCP server: image inspection, editing and simple PDF assembly via Pillow."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

FONT_PATH = r"C:\Windows\Fonts\msyh.ttc"
TEMP = os.environ.get("TEMP") or os.environ.get("TMP") or "."
SAMPLE_DIR = os.path.join(TEMP, "aiyu_mcp_samples")

srv = Server("media")


def _in_path(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(p):
        raise FileNotFoundError(path)
    return p


def _out_path(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    return p


def _font(size):
    """msyh.ttc when present, otherwise Pillow's built-in bitmap font."""
    try:
        return ImageFont.truetype(FONT_PATH, int(size))
    except Exception:
        try:
            return ImageFont.load_default(int(size))
        except Exception:
            return ImageFont.load_default()


def _color(value):
    s = str(value).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) == 6:
        try:
            return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
    return (255, 0, 0)


def _save(im, dst, **kw):
    """JPEG/BMP cannot hold alpha; flatten those modes before saving."""
    if dst.lower().endswith((".jpg", ".jpeg", ".bmp")) and im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.save(dst, **kw)
    return dst


def _done(prefix, dst, size):
    return "%s %s (%dx%d)" % (prefix, dst, size[0], size[1])
@srv.tool("image_info", "Report format, mode, pixel size and file size of an image.",
          {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
def image_info(path):
    p = _in_path(path)
    with Image.open(p) as im:
        return "path=%s\nformat=%s\nmode=%s\nsize=%dx%d\nfile_bytes=%d" % (
            p, im.format, im.mode, im.size[0], im.size[1], os.path.getsize(p))


@srv.tool("image_resize", "Resize an image; a 0 dimension is derived keeping aspect ratio.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "width": {"type": "integer", "default": 0}, "height": {"type": "integer", "default": 0}}, "required": ["path", "out"]})
def image_resize(path, out, width=0, height=0):
    src, dst = _in_path(path), _out_path(out)
    w, h = int(width), int(height)
    if w <= 0 and h <= 0:
        raise ValueError("give width and/or height; 0 means derive from the other one")
    with Image.open(src) as im:
        sw, sh = im.size
        if w <= 0:
            w = max(1, round(sw * h / float(sh)))
        if h <= 0:
            h = max(1, round(sh * w / float(sw)))
        resized = im.resize((w, h), Image.LANCZOS)
        _save(resized, dst)
        return _done("wrote", dst, resized.size)


@srv.tool("image_crop", "Crop an image to the box (left, top, right, bottom).",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "left": {"type": "integer"}, "top": {"type": "integer"}, "right": {"type": "integer"}, "bottom": {"type": "integer"}}, "required": ["path", "out", "left", "top", "right", "bottom"]})
def image_crop(path, out, left, top, right, bottom):
    src, dst = _in_path(path), _out_path(out)
    box = (int(left), int(top), int(right), int(bottom))
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError("crop box must satisfy right > left and bottom > top: %s" % (box,))
    with Image.open(src) as im:
        cropped = im.crop(box)
        _save(cropped, dst)
        return _done("wrote", dst, cropped.size)


@srv.tool("image_rotate", "Rotate an image by degrees, expanding the canvas to fit.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "degrees": {"type": "number"}}, "required": ["path", "out", "degrees"]})
def image_rotate(path, out, degrees):
    src, dst = _in_path(path), _out_path(out)
    with Image.open(src) as im:
        rotated = im.rotate(float(degrees), expand=True)
        _save(rotated, dst)
        return _done("wrote", dst, rotated.size)


@srv.tool("image_convert", "Re-encode an image into another format, e.g. fmt='PNG' or 'JPEG'.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "fmt": {"type": "string", "default": "PNG"}}, "required": ["path", "out"]})
def image_convert(path, out, fmt="PNG"):
    src, dst = _in_path(path), _out_path(out)
    target = str(fmt or "PNG").upper()
    with Image.open(src) as im:
        if target in ("JPEG", "JPG", "BMP"):
            im = im.convert("RGB")
        elif target == "PNG" and im.mode in ("P", "LA"):
            im = im.convert("RGBA")
        im.save(dst, format=target)
        return _done("wrote %s" % target, dst, im.size)
@srv.tool("image_thumbnail", "Write a thumbnail whose longest edge is size pixels.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "size": {"type": "integer", "default": 200}}, "required": ["path", "out"]})
def image_thumbnail(path, out, size=200):
    src, dst = _in_path(path), _out_path(out)
    side = max(1, int(size))
    with Image.open(src) as im:
        thumb = im.copy()
        thumb.thumbnail((side, side), Image.LANCZOS)
        _save(thumb, dst)
        return _done("wrote", dst, thumb.size)


@srv.tool("image_compress", "Re-save an image at a lower quality (JPEG/WebP friendly).",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "quality": {"type": "integer", "default": 70}}, "required": ["path", "out"]})
def image_compress(path, out, quality=70):
    src, dst = _in_path(path), _out_path(out)
    q = min(95, max(1, int(quality)))
    before = os.path.getsize(src)
    with Image.open(src) as im:
        work = im.convert("RGB") if im.mode not in ("RGB", "L") else im.copy()
        _save(work, dst, quality=q, optimize=True)
        size = work.size
    after = os.path.getsize(dst)
    return "%s (%dx%d) bytes %d -> %d, quality=%d" % (dst, size[0], size[1], before, after, q)


@srv.tool("image_gray", "Convert an image to 8-bit grayscale.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}}, "required": ["path", "out"]})
def image_gray(path, out):
    src, dst = _in_path(path), _out_path(out)
    with Image.open(src) as im:
        gray = im.convert("L")
        _save(gray, dst)
        return _done("wrote mode=L", dst, gray.size)


@srv.tool("image_text", "Draw text onto an image at (x, y) with a font size and hex color.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "text": {"type": "string"}, "x": {"type": "integer", "default": 10}, "y": {"type": "integer", "default": 10}, "size": {"type": "integer", "default": 36}, "color": {"type": "string", "default": "#ff0000"}}, "required": ["path", "out", "text"]})
def image_text(path, out, text, x=10, y=10, size=36, color="#ff0000"):
    src, dst = _in_path(path), _out_path(out)
    with Image.open(src) as im:
        base = im.convert("RGBA")
        draw = ImageDraw.Draw(base)
        draw.text((int(x), int(y)), str(text), font=_font(size), fill=_color(color))
        _save(base, dst)
        return _done("wrote text %r to" % str(text), dst, base.size)


@srv.tool("images_to_pdf", "Combine images into a single multi-page PDF.",
          {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}}, "out": {"type": "string", "default": ""}}, "required": []})
def images_to_pdf(paths=[], out=""):
    if not out:
        raise ValueError("out must be the target .pdf path")
    if not paths:
        raise ValueError("paths must list at least one image")
    srcs = [_in_path(p) for p in paths]
    dst = _out_path(out)
    pages = []
    try:
        for s in srcs:
            with Image.open(s) as im:
                pages.append(im.convert("RGB"))
        pages[0].save(dst, "PDF", save_all=True, append_images=pages[1:])
        return "wrote %s (pages=%d, page=%dx%d)" % (dst, len(pages), pages[0].size[0], pages[0].size[1])
    finally:
        for page in pages:
            page.close()


@srv.tool("image_watermark_tile", "Tile a translucent text watermark across an image.",
          {"type": "object", "properties": {"path": {"type": "string"}, "out": {"type": "string"}, "text": {"type": "string"}, "size": {"type": "integer", "default": 24}}, "required": ["path", "out", "text"]})
def image_watermark_tile(path, out, text, size=24):
    src, dst = _in_path(path), _out_path(out)
    with Image.open(src) as im:
        base = im.convert("RGBA")
        layer = Image.new("RGBA", base.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(layer)
        font = _font(size)
        label = str(text)
        try:
            box = draw.textbbox((0, 0), label, font=font)
            tw, th = box[2] - box[0], box[3] - box[1]
        except Exception:
            tw, th = font.getsize(label)
        step_x, step_y = max(1, tw + 40), max(1, th + 40)
        for y in range(0, base.size[1], step_y):
            for x in range(0, base.size[0], step_x):
                draw.text((x, y), label, font=font, fill=(220, 30, 30, 90))
        merged = Image.alpha_composite(base, layer)
        _save(merged, dst)
        return "%s (%dx%d, tile step=%dx%d)" % (_done("wrote", dst, merged.size), base.size[0], base.size[1], step_x, step_y)
_OUT = os.path.join(SAMPLE_DIR, "out")
_CANDIDATES = [
    os.path.join(SAMPLE_DIR, "src.png"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Web", "Wallpaper", "Windows", "img0.jpg"),
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Web", "Wallpaper", "Theme1", "img1.jpg"),
]
_SRC = next((c for c in _CANDIDATES if os.path.isfile(c)), "")

# Read-only samples over an existing source image plus writes into TEMP.
SAMPLES = {}
if _SRC:
    SAMPLES.update({
        "image_info": {"path": _SRC},
        "image_resize": {"path": _SRC, "out": os.path.join(_OUT, "resized.png"), "width": 120, "height": 0},
        "image_crop": {"path": _SRC, "out": os.path.join(_OUT, "cropped.png"), "left": 0, "top": 0, "right": 60, "bottom": 60},
        "image_rotate": {"path": _SRC, "out": os.path.join(_OUT, "rotated.png"), "degrees": 90},
        "image_convert": {"path": _SRC, "out": os.path.join(_OUT, "converted.jpg"), "fmt": "JPEG"},
        "image_thumbnail": {"path": _SRC, "out": os.path.join(_OUT, "thumb.png"), "size": 64},
        "image_compress": {"path": _SRC, "out": os.path.join(_OUT, "compressed.jpg"), "quality": 60},
        "image_gray": {"path": _SRC, "out": os.path.join(_OUT, "gray.png")},
        "image_text": {"path": _SRC, "out": os.path.join(_OUT, "text.png"), "text": "aiyu", "x": 5, "y": 5, "size": 28, "color": "#ff0000"},
        "images_to_pdf": {"paths": [_SRC], "out": os.path.join(_OUT, "combined.pdf")},
        "image_watermark_tile": {"path": _SRC, "out": os.path.join(_OUT, "watermarked.png"), "text": "sample", "size": 20},
    })


def build():
    return srv


if __name__ == "__main__":
    srv.run()



