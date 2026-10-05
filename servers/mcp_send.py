"""MCP server: send -- media outbox tools (4.0): images, voice, files, QR (and a mouth).

Everything funnels through the bridge's outbox: the file gets a short signed URL that
expires, so the phone can open it from the same Wi-Fi without exposing a folder.

`send_screen` replaces hand-rolled file servers; `send_voice` renders Chinese text to a
WAV with the built-in Windows synthesiser, so the persona can actually say something.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

API = (os.environ.get("MCP_BRIDGE_API") or "http://127.0.0.1:8877").rstrip("/")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
PS = "powershell" if os.name == "nt" else "pwsh"
TMP = tempfile.gettempdir()

srv = Server("send")


def _api(path, payload=None, method="GET", timeout=60):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(API + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}"), ""
    except urllib.error.HTTPError as exc:
        return {}, "HTTP %s: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:200])
    except Exception as exc:  # noqa: BLE001
        return {}, "%s: %s (is the bridge running? expected at %s)" % (type(exc).__name__, exc, API)


def _deliver(path, kind, note="", ttl=0, once=False, label="", cleanup=False):
    """Hand a file to the outbox and format the answer the persona should send.

    cleanup=True deletes the *source* file afterwards - that is what generated media
    (screenshots, QR images, speech) needs, otherwise %TEMP% grows without end.
    """
    payload = {"path": os.path.abspath(os.path.expanduser(str(path))), "kind": kind,
               "note": note, "once": bool(once)}
    if ttl:
        payload["ttl"] = float(ttl)
    data, error = _api("/v2/outbox", payload, method="POST")
    if cleanup:
        try:
            os.remove(payload["path"])
        except OSError:
            pass
    if error:
        return "could not deliver %s: %s" % (label or path, error)
    item = data.get("item") or {}
    url = item.get("url") or ""
    if kind in ("image", "qr"):
        # 4.1: lead with the markdown line and keep the raw URL out of the way. The model
        # used to echo "url=…" as chat text, which shows up as an ugly unclickable link
        # instead of the picture.
        return ("![%s](%s)\n"
                "把上面这一行（从 ! 到右括号）整行原样发出去，客户端就会直接显示图片。\n"
                "不要发 url=、不要发“发送成功”之类的回执、不要改这一行。\n"
                "(内部信息，给机器看的: kind=%s bytes=%s id=%s)"
                % (note or "图片", url, item.get("kind"), item.get("bytes"), item.get("id")))
    lines = ["delivered %s" % (label or item.get("name") or path),
             "kind=%s bytes=%s expires_in=%ss" % (item.get("kind"), item.get("bytes"),
                                                  int(float(item.get("expires") or 0) - __import__("time").time())),
             "url=%s" % url]
    if kind == "audio":
        lines.append("tell the user=如果用户开着语音页（/voice），直接用 call_say 让她说话更好；"
                     "这份音频文件只是备份，把链接给用户即可")
    else:
        lines.append("tell the user=把链接给用户，他点开就能拿到文件")
    return "\n".join(lines)


def _grab_screen(monitor="primary"):
    from PIL import ImageGrab, Image
    if str(monitor).strip().lower() in ("all", "-1", "virtual"):
        image = ImageGrab.grab(all_screens=True)
    else:
        image = ImageGrab.grab()
    limit = 1600
    width, height = image.size
    if max(width, height) > limit:
        scale = limit / float(max(width, height))
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)
    return image


@srv.tool("send_screen", "Send the user a screenshot right now (captures, uploads to the outbox, returns a URL).",
          {"type": "object", "properties": {"note": {"type": "string", "default": ""},
                                            "monitor": {"type": "string", "default": "primary"},
                                            "ttl": {"type": "integer", "default": 0},
                                            "once": {"type": "boolean", "default": False}},
           "required": []})
def send_screen(note="", monitor="primary", ttl=0, once=False):
    image = _grab_screen(monitor)
    out = os.path.join(TMP, "mcp-hands-send-%s.png" % __import__("time").strftime("%Y%m%d-%H%M%S"))
    image.save(out, format="PNG")
    return _deliver(out, "image", note=note or "屏幕截图", ttl=ttl, once=once, label="screenshot",
                    cleanup=True)


@srv.tool("send_image", "Send an image file on this PC to the user (returns a URL they can open).",
          {"type": "object", "properties": {"path": {"type": "string"}, "note": {"type": "string", "default": ""},
                                            "ttl": {"type": "integer", "default": 0},
                                            "once": {"type": "boolean", "default": False}},
           "required": ["path"]})
def send_image(path, note="", ttl=0, once=False):
    return _deliver(path, "image", note=note, ttl=ttl, once=once)


@srv.tool("send_file", "Send any file (document, archive, audio, video) to the user as a download link.",
          {"type": "object", "properties": {"path": {"type": "string"}, "note": {"type": "string", "default": ""},
                                            "ttl": {"type": "integer", "default": 0},
                                            "once": {"type": "boolean", "default": False}},
           "required": ["path"]})
def send_file(path, note="", ttl=0, once=False):
    ext = os.path.splitext(str(path))[1].lower()
    kind = {".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
            ".wav": "audio", ".mp3": "audio", ".m4a": "audio", ".ogg": "audio",
            ".mp4": "video", ".mkv": "video", ".webm": "video",
            ".txt": "text", ".md": "text", ".log": "text",
            ".zip": "archive", ".7z": "archive", ".rar": "archive"}.get(ext, "file")
    return _deliver(path, kind, note=note, ttl=ttl, once=once)


@srv.tool("send_text", "Send a chunk of text to the user as a file (handy when the reply would be too long).",
          {"type": "object", "properties": {"text": {"type": "string"}, "name": {"type": "string", "default": "note.md"},
                                            "ttl": {"type": "integer", "default": 0}}, "required": ["text"]})
def send_text(text, name="note.md", ttl=0):
    # Never trust the name: a crafted value like "..\\..\\x.txt" would otherwise write
    # outside the scratch folder.
    safe = os.path.basename(str(name or "note.md").replace("\\", "/")) or "note.md"
    safe = re.sub(r"[<>:\"|?*\x00-\x1f]", "_", safe)[:80] or "note.md"
    out = os.path.join(TMP, "mcp-hands-text-%s-%s" % (__import__("time").strftime("%Y%m%d-%H%M%S"), safe))
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(str(text))
    return _deliver(out, "text", note=safe, ttl=ttl, cleanup=True)


@srv.tool("send_qr", "Turn a URL or some text into a QR image and send it (e.g. to hand a link to a phone).",
          {"type": "object", "properties": {"text": {"type": "string"}, "note": {"type": "string", "default": ""},
                                            "ttl": {"type": "integer", "default": 0}}, "required": ["text"]})
def send_qr(text, note="", ttl=0):
    try:
        import qrcode
    except ImportError:
        return "qrcode is not available in this build"
    out = os.path.join(TMP, "mcp-hands-qr-%s.png" % __import__("time").strftime("%Y%m%d-%H%M%S"))
    qrcode.make(str(text)).save(out)
    return _deliver(out, "qr", note=note or "二维码", ttl=ttl, cleanup=True)


@srv.tool("send_voice", "Say something out loud as an audio file: renders text to speech and sends the link "
                        "(and can play it on this PC as well).",
          {"type": "object", "properties": {"text": {"type": "string"}, "speak_here": {"type": "boolean", "default": False},
                                            "ttl": {"type": "integer", "default": 0}}, "required": ["text"]})
def send_voice(text, speak_here=False, ttl=0):
    body = str(text or "").strip()
    if not body:
        return "send_voice: nothing to say"
    if len(body) > 1200:
        # The synthesiser runs through a command line; a wall of text would be cut off
        # (or fail outright), so say the important part and keep the rest as a note.
        body = body[:1200]
    out = os.path.join(TMP, "mcp-hands-voice-%s.wav" % __import__("time").strftime("%Y%m%d-%H%M%S"))
    safe = body.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.SetOutputToWaveFile('%s');"
        "$s.Speak('%s');"
        "$s.Dispose();"
        "if (Test-Path '%s') { 'wav ok' } else { 'wav missing' }" % (out, safe, out)
    )
    try:
        done = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, timeout=120, creationflags=NO_WINDOW)
        text_out = (done.stdout or b"").decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return "text-to-speech failed: %s" % exc
    if not os.path.isfile(out):
        return "text-to-speech produced no audio: %s" % text_out.strip()[:200]
    result = _deliver(out, "audio", note=body[:40], ttl=ttl, label="voice", cleanup=not speak_here)
    if speak_here:
        play = (
            "Add-Type -AssemblyName System.Media;"
            "$p = New-Object System.Media.SoundPlayer '%s'; $p.PlaySync()" % out
        )
        subprocess.Popen([PS, "-NoProfile", "-NonInteractive", "-Command", play], creationflags=NO_WINDOW)
        result += "\nalso playing on this PC"
    return result


@srv.tool("outbox_list", "What is currently waiting in the outbox (files the user has not opened yet).",
          {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}, "required": []})
def outbox_list(limit=20):
    data, error = _api("/v2/outbox?limit=%d" % max(1, int(limit)))
    if error:
        return "could not read the outbox: %s" % error
    items = data.get("items") or []
    if not items:
        return "outbox is empty"
    lines = ["items=%d" % len(items)]
    for item in items:
        lines.append("%s [%s] %s bytes=%s fetches=%s expires_in=%ss" % (
            item.get("id"), item.get("kind"), item.get("name"), item.get("bytes"), item.get("fetches"),
            int(float(item.get("expires") or 0) - __import__("time").time())))
    return "\n".join(lines)


@srv.tool("outbox_clean", "Delete expired items from the outbox now.",
          {"type": "object", "properties": {}, "required": []})
def outbox_clean():
    data, error = _api("/v2/outbox/purge", {}, method="POST")
    return ("could not clean the outbox: %s" % error) if error else "removed %s item(s)" % data.get("removed", 0)


SAMPLES = {"outbox_list": {"limit": 5}}


def build():
    return srv


if __name__ == "__main__":
    srv.run()
