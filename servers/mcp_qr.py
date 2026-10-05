"""MCP server: QR code generation (qrcode + Pillow) for text, URLs, WiFi and vCards."""
import hashlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

import qrcode  # noqa: E402
import qrcode.image.pil  # noqa: E402
import qrcode.image.svg  # noqa: E402

SAMPLE_DIR = os.path.join(sample_dir(), ".aiyu_selftest")

srv = Server("qr")


# ------------------------------------------------------------------- primitives
def _qr(payload, box_size=8, border=2):
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=max(1, int(box_size)),
        border=max(0, int(border)),
        image_factory=qrcode.image.pil.PilImage,
    )
    qr.add_data(str(payload))
    qr.make(fit=True)
    return qr


def _pixels(qr):
    return (qr.modules_count + 2 * qr.border) * qr.box_size


def _out_path(path):
    p = os.path.abspath(os.path.expanduser(str(path)))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    return p


def _slug(payload):
    return hashlib.md5(str(payload).encode("utf-8")).hexdigest()[:8]


def _default_path(payload, suffix, stem="qr"):
    return os.path.join(sample_dir(), "%s_%s%s" % (stem, _slug(payload), suffix))


def _png(payload, out, size=8, border=2, fill="#000000", back="#ffffff", stem="qr"):
    """Write the payload as a PNG and return its summary lines."""
    if not str(payload):
        raise ValueError("payload must not be empty")
    qr = _qr(payload, size, border)
    image = qr.make_image(fill_color=str(fill), back_color=str(back))
    path = _out_path(out or _default_path(payload, ".png", stem))
    image.save(path)
    return ("wrote=%s\nbytes=%d\npng_px=%dx%d\nmodules=%d\nversion=%d\necc=M"
            % (path, os.path.getsize(path), _pixels(qr), _pixels(qr), qr.modules_count, qr.version))


# ------------------------------------------------------------------------ tools
@srv.tool("qr_make", "Render text as a QR PNG (defaults to a file inside the sample dir).",
          {"type": "object", "properties": {"text": {"type": "string"}, "out": {"type": "string", "default": ""}, "size": {"type": "integer", "default": 8}, "border": {"type": "integer", "default": 2}, "fill": {"type": "string", "default": "#000000"}, "back": {"type": "string", "default": "#ffffff"}}, "required": ["text"]})
def qr_make(text, out="", size=8, border=2, fill="#000000", back="#ffffff"):
    return _png(text, out, size, border, fill, back, "qr")


@srv.tool("qr_batch", "Render many QRs from \"name=text\" lines into one directory.",
          {"type": "object", "properties": {"items": {"type": "string"}, "out_dir": {"type": "string", "default": ""}}, "required": ["items"]})
def qr_batch(items, out_dir=""):
    lines = str(items or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    dst = _out_path(out_dir or os.path.join(sample_dir(), "qr_batch"))
    written = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("bad item %r (expected name=text)" % line)
        name, payload = line.split("=", 1)
        name = re.sub(r"[^A-Za-z0-9_-]", "_", name.strip()) or "qr"
        payload = payload.strip()
        if not payload:
            raise ValueError("item %r has an empty payload" % line)
        qr = _qr(payload)
        image = qr.make_image()
        path = _out_path(os.path.join(dst, "%s.png" % name))
        image.save(path)
        written.append("%s <- %d chars, %dx%d px" % (path, len(payload), _pixels(qr), _pixels(qr)))
    if not written:
        raise ValueError("no items found in %r" % items)
    return "wrote=%d\ndir=%s\n%s" % (len(written), dst, "\n".join(written))


@srv.tool("qr_make_svg", "Render text as a vector QR (SvgPathImage) at the given path.",
          {"type": "object", "properties": {"text": {"type": "string"}, "out": {"type": "string"}}, "required": ["text", "out"]})
def qr_make_svg(text, out):
    if not str(text):
        raise ValueError("text must not be empty")
    qr = _qr(text)
    image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    path = _out_path(out)
    data = image.to_string()
    if isinstance(data, (bytes, bytearray)):
        with open(path, "wb") as fh:
            fh.write(data)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(data))
    return ("wrote=%s\nbytes=%d\nformat=svg\nmodules=%d\nversion=%d"
            % (path, os.path.getsize(path), qr.modules_count, qr.version))


@srv.tool("qr_make_wifi", "Render a WiFi join QR using the WIFI: URI scheme.",
          {"type": "object", "properties": {"ssid": {"type": "string"}, "password": {"type": "string"}, "security": {"type": "string", "default": "WPA"}, "out": {"type": "string", "default": ""}}, "required": ["ssid", "password"]})
def qr_make_wifi(ssid, password, security="WPA", out=""):
    kind = str(security or "WPA").strip().upper()
    if kind not in ("WPA", "WEP", "NOPASS"):
        raise ValueError("security must be WPA, WEP or nopass")
    payload = "WIFI:T:%s;S:%s;P:%s;H:false;;" % (kind, _esc(ssid), _esc(password))
    return "payload=%s\n%s" % (payload, _png(payload, out, stem="qr_wifi"))


@srv.tool("qr_make_vcard", "Render a vCard 3.0 contact QR.",
          {"type": "object", "properties": {"name": {"type": "string"}, "phone": {"type": "string", "default": ""}, "email": {"type": "string", "default": ""}, "org": {"type": "string", "default": ""}, "out": {"type": "string", "default": ""}}, "required": ["name"]})
def qr_make_vcard(name, phone="", email="", org="", out=""):
    if not str(name).strip():
        raise ValueError("name must not be empty")
    lines = ["BEGIN:VCARD", "VERSION:3.0", "N:%s" % _esc(name), "FN:%s" % _esc(name)]
    if str(org):
        lines.append("ORG:%s" % _esc(org))
    if str(phone):
        lines.append("TEL;TYPE=CELL:%s" % _esc(phone))
    if str(email):
        lines.append("EMAIL:%s" % _esc(email))
    lines.append("END:VCARD")
    payload = "\r\n".join(lines) + "\r\n"
    return "payload=%r\n%s" % (payload, _png(payload, out, stem="qr_vcard"))


@srv.tool("qr_make_url", "Render a URL QR, adding https:// when the scheme is missing.",
          {"type": "object", "properties": {"url": {"type": "string"}, "out": {"type": "string", "default": ""}}, "required": ["url"]})
def qr_make_url(url, out=""):
    target = str(url or "").strip()
    if not target:
        raise ValueError("url must not be empty")
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", target):
        target = "https://" + target
    return "payload=%s\n%s" % (target, _png(target, out, stem="qr_url"))


@srv.tool("qr_decode_help", "Explain how to decode a QR (external scanner needed) and echo the payload.",
          {"type": "object", "properties": {"payload": {"type": "string", "default": ""}}, "required": []})
def qr_decode_help(payload=""):
    lines = [
        "decoding requires a scanner: this server only encodes.",
        "options: phone camera; on Windows use pyzbar/opencv (pip install pyzbar opencv-python) or any online scanner.",
        "the payload below is exactly what a correct scan must return; regenerate the PNG with a larger size/border if a scanner struggles.",
    ]
    if str(payload):
        lines.append("payload=%s" % payload)
        lines.append("payload_chars=%d" % len(str(payload)))
    else:
        lines.append("payload=(none supplied; pass payload=... to echo what a scan should return)")
    return "\n".join(lines)


def _esc(value):
    """Escape the separator characters used by the WIFI:/vCard payload syntax."""
    return re.sub(r"([\\;,:\"])", r"\\\1", str(value))


# Every tool here only writes, so declaration order does not matter.
SAMPLES = {
    "qr_make": {"text": "https://example.com/selftest", "out": os.path.join(SAMPLE_DIR, "qr_make.png"), "size": 6, "border": 2, "fill": "#000000", "back": "#ffffff"},
    "qr_batch": {"items": "first=https://example.com/1\nsecond=plain text payload", "out_dir": os.path.join(SAMPLE_DIR, "qr_batch")},
    "qr_make_svg": {"text": "svg payload", "out": os.path.join(SAMPLE_DIR, "qr_make.svg")},
    "qr_make_wifi": {"ssid": "SampleNet", "password": "sample-pass", "security": "WPA", "out": os.path.join(SAMPLE_DIR, "qr_wifi.png")},
    "qr_make_vcard": {"name": "Sample Person", "phone": "+8613800138000", "email": "sample@example.com", "org": "Example Org", "out": os.path.join(SAMPLE_DIR, "qr_vcard.png")},
    "qr_make_url": {"url": "example.com/page", "out": os.path.join(SAMPLE_DIR, "qr_url.png")},
    "qr_decode_help": {"payload": "https://example.com/selftest"},
}

# Sample tool calls that would need the network or credentials; none here.
SAMPLES_OPTIONAL = set()


def build():
    return srv


if __name__ == "__main__":
    srv.run()
