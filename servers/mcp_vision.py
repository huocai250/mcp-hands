"""MCP server: vision -- let the persona actually SEE the screen and images.

The persona's brain is a text model, so "look at the screen" is implemented as:
capture image -> downscale -> POST to an OpenAI-compatible *vision* endpoint ->
return the model's description as text the persona can read.

Default endpoint is DeepSeek's own API, because `deepseek-flash` accepts images
(https://api-docs.deepseek.com/guides/vision): base https://api.deepseek.com/v1,
model deepseek-flash, image as a base64 `data:` URL inside a user message,
optional `detail` = low | high | original | auto. Images are auto-resized by the
service to roughly 1300x1300, so uploading more than ~1300 px on the long side
buys nothing; that is the default here.

Any OpenAI-compatible vision endpoint works too (e.g. SiliconFlow + Qwen-VL):
change vision.base_url / vision.model in bridge.config.json.

Configuration arrives as environment variables from the bridge:

  MCP_VISION_BASE_URL   default https://api.deepseek.com/v1
  MCP_VISION_API_KEY    also falls back to DEEPSEEK_API_KEY / SILICONFLOW_API_KEY / VISION_API_KEY
  MCP_VISION_MODEL      default deepseek-flash
  MCP_VISION_DETAIL     default auto
  MCP_VISION_MAX_PIXELS default 1300
  MCP_VISION_TIMEOUT_S  default 120
"""
import base64
import ctypes
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

from PIL import Image, ImageGrab  # noqa: E402

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-flash"
SAMPLE_DIR = os.path.join(sample_dir(), ".aiyu_selftest")

srv = Server("vision")


# ------------------------------------------------------------------- config
def _env(name, default=""):
    return os.environ.get(name) or default


def _cfg(overrides=None):
    overrides = overrides or {}
    key = (overrides.get("api_key") or _env("MCP_VISION_API_KEY") or _env("DEEPSEEK_API_KEY")
           or _env("SILICONFLOW_API_KEY") or _env("VISION_API_KEY"))
    return {
        "base_url": (overrides.get("base_url") or _env("MCP_VISION_BASE_URL", DEFAULT_BASE)).rstrip("/"),
        "api_key": key,
        "model": overrides.get("model") or _env("MCP_VISION_MODEL", DEFAULT_MODEL),
        "detail": overrides.get("detail") or _env("MCP_VISION_DETAIL", "auto"),
        # DeepSeek's flash model thinks by default, and with thinking on a modest max_tokens
        # can be swallowed by reasoning_content, leaving `content` empty. Vision work does not
        # need the chain of thought, so thinking is switched off here.
        "thinking": overrides.get("thinking", _env("MCP_VISION_THINKING", "disabled")),
        "max_pixels": int(_env("MCP_VISION_MAX_PIXELS", "1300")),
        "timeout": float(_env("MCP_VISION_TIMEOUT_S", "120")),
    }


def _prepare(image, max_pixels=0, quality=85):
    """Downscale (long side) and encode as JPEG bytes."""
    limit = int(max_pixels or _cfg()["max_pixels"])
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    width, height = image.size
    longest = max(width, height)
    if limit and longest > limit:
        scale = limit / float(longest)
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=int(quality), optimize=True)
    return buffer.getvalue(), image.size


def _ask(images, question, max_tokens=2048, temperature=0.1, overrides=None):
    """images: list of JPEG bytes. Returns the model's text answer."""
    cfg = _cfg(overrides)
    if not cfg["api_key"]:
        raise RuntimeError(
            "vision API key is missing. Either (A) use the app-native proxy — the app's own key is "
            "passed through automatically, or (B) put your DeepSeek key into vision.api_key in "
            "bridge.config.json (or set DEEPSEEK_API_KEY / MCP_VISION_API_KEY) and restart the bridge.")
    content = [{"type": "text", "text": question or "描述这张图片的内容。"}]
    for raw in images:
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
                                      "detail": cfg["detail"]}})
    body = {"model": cfg["model"], "messages": [{"role": "user", "content": content}],
            "max_tokens": int(max_tokens), "temperature": float(temperature), "stream": False}
    if cfg.get("thinking"):
        body["thinking"] = {"type": cfg["thinking"]}

    def send(payload):
        request = urllib.request.Request(
            cfg["base_url"] + "/chat/completions", data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]})
        with urllib.request.urlopen(request, timeout=cfg["timeout"]) as response:
            return json.loads(response.read().decode("utf-8", "replace"))

    try:
        payload = send(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if exc.code == 400 and "thinking" in detail.lower() and "thinking" in body:
            # Some OpenAI-compatible endpoints do not know the DeepSeek-only field.
            body.pop("thinking", None)
            try:
                payload = send(body)
            except urllib.error.HTTPError as exc2:
                detail = exc2.read().decode("utf-8", "replace")
                raise RuntimeError("vision endpoint HTTP %s: %s" % (exc2.code, detail[:400])) from None
        else:
            hint = ""
            if exc.code == 401:
                hint = " (检查 vision.api_key / 上游 key 是否正确)"
            elif exc.code == 404:
                hint = " (检查 vision.model；DeepSeek 上用 deepseek-flash，它才支持图片)"
            elif exc.code == 400 and "image" in detail.lower():
                hint = " (图片格式/大小不被接受：支持 JPEG/PNG/GIF/WebP，单图 <= 32 MiB)"
            raise RuntimeError("vision endpoint HTTP %s: %s%s" % (exc.code, detail[:400], hint)) from None
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("vision endpoint error: %s" % exc) from None
    choices = payload.get("choices") or []
    message = (choices[0].get("message") if choices else {}) or {}
    text = (message.get("content") or "").strip()
    if not text:
        # Thinking mode puts the chain of thought in reasoning_content and can leave
        # `content` empty when max_tokens runs out mid-thought. Never lose the answer.
        reasoning = (message.get("reasoning_content") or "").strip()
        if reasoning:
            log_note = "[vision] answer came back as reasoning_content (thinking mode ran long)"
            return reasoning + "\n" + log_note
        finish = (choices[0].get("finish_reason") if choices else "") or "?"
        raise RuntimeError("vision model returned an empty answer (finish_reason=%s): %s"
                           % (finish, json.dumps(payload, ensure_ascii=False)[:300]))
    return text


def _screen_box(monitor="primary"):
    """Bounding box for 'primary' (or 0/1/2…) and 'all'/'-1' (the virtual screen)."""
    user32 = ctypes.windll.user32
    text = str(monitor).strip().lower()
    if text in ("all", "-1", "virtual"):
        return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
                user32.GetSystemMetrics(76) + user32.GetSystemMetrics(78),
                user32.GetSystemMetrics(77) + user32.GetSystemMetrics(79))
    index = int(text) if text.isdigit() else 0
    rects = []

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    MonitorEnumProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(RECT), ctypes.c_double)

    def callback(_handle, _dc, rect_ptr, _data):
        rect = rect_ptr.contents
        rects.append((rect.left, rect.top, rect.right, rect.bottom))
        return 1

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(callback), 0)
    if not rects:
        return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    return rects[min(index, len(rects) - 1)]


def _grab(box=None):
    return ImageGrab.grab(bbox=box, all_screens=True) if box else ImageGrab.grab(all_screens=True)


def _window_box(title):
    """Bounding box of the first visible window whose title contains `title`."""
    user32 = ctypes.windll.user32
    needle = str(title).strip().lower()
    found = []

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        name = buffer.value.strip()
        if needle and needle not in name.lower():
            return True
        rect = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            if rect.right - rect.left > 40 and rect.bottom - rect.top > 40:
                found.append((name, (rect.left, rect.top, rect.right, rect.bottom)))
                return False
        return True

    user32.EnumWindows(EnumProc(callback), 0)
    return found[0] if found else (None, None)


def _overrides(api_key="", model="", base_url="", detail=""):
    return {"api_key": api_key, "model": model, "base_url": base_url, "detail": detail}


def _stamp(size, milliseconds, model=""):
    return "[vision] model=%s image=%dx%d ms=%d" % (model or _cfg()["model"], size[0], size[1], milliseconds)


# ---------------------------------------------------------------------- tools
@srv.tool("vision_status", "Show the configured vision endpoint/model and whether a key is available.",
          {"type": "object", "properties": {}, "required": []})
def vision_status():
    cfg = _cfg()
    return "\n".join([
        "base_url=%s" % cfg["base_url"],
        "model=%s" % cfg["model"],
        "detail=%s" % cfg["detail"],
        "thinking=%s" % (cfg["thinking"] or "endpoint default"),
        "max_pixels=%d" % cfg["max_pixels"],
        "api_key=%s" % ("set (%d chars)" % len(cfg["api_key"]) if cfg["api_key"] else "NOT SET"),
        "note=DeepSeek 的 deepseek-flash 支持图片；走 App 直连代理时会自动沿用 App 里的 key",
    ])


@srv.tool("vision_probe", "Send a tiny test image to verify the vision endpoint and key really work.",
          {"type": "object", "properties": {"api_key": {"type": "string", "default": ""},
                                            "model": {"type": "string", "default": ""}},
           "required": []})
def vision_probe(api_key="", model=""):
    image = Image.new("RGB", (96, 96), (30, 90, 200))
    raw, size = _prepare(image, max_pixels=512)
    started = time.time()
    answer = _ask([raw], "这张图是什么颜色？只回答颜色名。", max_tokens=64,
                  overrides=_overrides(api_key, model, detail="low"))
    return "%s\nanswer=%s" % (_stamp(size, int((time.time() - started) * 1000), model), answer[:200])


@srv.tool("see_image", "Look at an image file on this PC and answer a question about it.",
          {"type": "object", "properties": {"path": {"type": "string"},
                                            "question": {"type": "string", "default": "详细描述这张图片的内容"},
                                            "detail": {"type": "string", "default": ""},
                                            "max_pixels": {"type": "integer", "default": 0},
                                            "api_key": {"type": "string", "default": ""}},
           "required": ["path"]})
def see_image(path, question="详细描述这张图片的内容", detail="", max_pixels=0, api_key=""):
    target = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(target):
        raise FileNotFoundError(target)
    with Image.open(target) as image:
        raw, size = _prepare(image, max_pixels)
    started = time.time()
    answer = _ask([raw], question, overrides=_overrides(api_key, detail=detail))
    return "%s\n%s" % (_stamp(size, int((time.time() - started) * 1000)), answer)


@srv.tool("see_screen", "Look at the PC screen right now (the persona cannot see it otherwise). monitor: primary|all|0|1|2",
          {"type": "object", "properties": {"question": {"type": "string", "default": "描述屏幕上现在显示的内容，有哪些窗口和重要信息"},
                                            "monitor": {"type": "string", "default": "primary"},
                                            "detail": {"type": "string", "default": ""},
                                            "max_pixels": {"type": "integer", "default": 0},
                                            "api_key": {"type": "string", "default": ""}},
           "required": []})
def see_screen(question="描述屏幕上现在显示的内容，有哪些窗口和重要信息", monitor="primary",
               detail="", max_pixels=0, api_key=""):
    image = _grab(_screen_box(monitor))
    raw, size = _prepare(image, max_pixels)
    started = time.time()
    answer = _ask([raw], question, overrides=_overrides(api_key, detail=detail))
    return "%s\n%s" % (_stamp(size, int((time.time() - started) * 1000)), answer)


@srv.tool("see_window", "Look at one window (matched by part of its title) and describe what it shows.",
          {"type": "object", "properties": {"title": {"type": "string"},
                                            "question": {"type": "string", "default": "这个窗口是什么程序？里面显示了什么内容"},
                                            "detail": {"type": "string", "default": ""},
                                            "api_key": {"type": "string", "default": ""}},
           "required": ["title"]})
def see_window(title, question="这个窗口是什么程序？里面显示了什么内容", detail="", api_key=""):
    name, box = _window_box(title)
    if box is None:
        raise RuntimeError("no visible window matches %r (use desktop_list_windows to list them)" % title)
    image = _grab(box)
    raw, size = _prepare(image, 0)
    started = time.time()
    answer = _ask([raw], question, overrides=_overrides(api_key, detail=detail))
    return "window=%s\n%s\n%s" % (name, _stamp(size, int((time.time() - started) * 1000)), answer)


@srv.tool("see_clipboard", "Look at the image currently on the Windows clipboard (if any).",
          {"type": "object", "properties": {"question": {"type": "string", "default": "这是什么图？描述内容"},
                                            "api_key": {"type": "string", "default": ""}},
           "required": []})
def see_clipboard(question="这是什么图？描述内容", api_key=""):
    data = ImageGrab.grabclipboard()
    if data is None:
        raise RuntimeError("clipboard has no image (copy a picture first)")
    if isinstance(data, list):
        paths = [p for p in data if os.path.isfile(str(p))]
        if not paths:
            raise RuntimeError("clipboard holds files but none is an image: %s" % data[:5])
        return see_image(paths[0], question, api_key=api_key)
    raw, size = _prepare(data)
    started = time.time()
    answer = _ask([raw], question, overrides=_overrides(api_key))
    return "%s\n%s" % (_stamp(size, int((time.time() - started) * 1000)), answer)


@srv.tool("read_text_in_image", "OCR: read the text out of an image (screenshot/photo/scan) verbatim.",
          {"type": "object", "properties": {"path": {"type": "string"},
                                            "language": {"type": "string", "default": "中文和英文"},
                                            "api_key": {"type": "string", "default": ""}},
           "required": ["path"]})
def read_text_in_image(path, language="中文和英文", api_key=""):
    target = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(target):
        raise FileNotFoundError(target)
    with Image.open(target) as image:
        raw, size = _prepare(image, 0, quality=92)
    started = time.time()
    answer = _ask([raw], "把图片里的文字原样识别出来（%s），保持换行与数字；不要翻译、不要加解释。"
                          "如果图中没有文字就回答「没有文字」。" % language, max_tokens=2048,
                  overrides=_overrides(api_key, detail="high"))
    return "%s\n%s" % (_stamp(size, int((time.time() - started) * 1000)), answer)


@srv.tool("read_screen_text", "OCR the screen now: read whatever text is currently visible.",
          {"type": "object", "properties": {"monitor": {"type": "string", "default": "primary"},
                                            "language": {"type": "string", "default": "中文和英文"},
                                            "api_key": {"type": "string", "default": ""}},
           "required": []})
def read_screen_text(monitor="primary", language="中文和英文", api_key=""):
    image = _grab(_screen_box(monitor))
    raw, size = _prepare(image, 0, quality=92)
    started = time.time()
    answer = _ask([raw], "把屏幕截图里的文字尽量原样识别出来（%s），按区域分组；不要解释。" % language,
                  max_tokens=2048, overrides=_overrides(api_key, detail="high"))
    return "%s\n%s" % (_stamp(size, int((time.time() - started) * 1000)), answer)


@srv.tool("describe_ui", "Read the screen like an agent: open windows, focus, clickable controls, errors, next step.",
          {"type": "object", "properties": {"monitor": {"type": "string", "default": "primary"},
                                            "task": {"type": "string", "default": ""},
                                            "api_key": {"type": "string", "default": ""}},
           "required": []})
def describe_ui(monitor="primary", task="", api_key=""):
    image = _grab(_screen_box(monitor))
    raw, size = _prepare(image, 0)
    prompt = ("以可操作的视角描述这块屏幕：1) 有哪些窗口/应用；2) 当前焦点在哪个控件；"
              "3) 有哪些按钮、输入框、菜单项，尽量给出相对位置（上/下/左/右、坐标百分比）；"
              "4) 有没有错误提示或待处理事项。")
    if task:
        prompt += " 接下来要做的是：%s，请指出下一步点哪里。" % task
    started = time.time()
    answer = _ask([raw], prompt, overrides=_overrides(api_key))
    return "%s\n%s" % (_stamp(size, int((time.time() - started) * 1000)), answer)


@srv.tool("compare_images", "Show two images to the model and describe the differences.",
          {"type": "object", "properties": {"left": {"type": "string"}, "right": {"type": "string"},
                                            "question": {"type": "string", "default": "这两张图有什么不同？"},
                                            "api_key": {"type": "string", "default": ""}},
           "required": ["left", "right"]})
def compare_images(left, right, question="这两张图有什么不同？", api_key=""):
    raws, sizes = [], []
    for item in (left, right):
        target = os.path.abspath(os.path.expanduser(str(item)))
        if not os.path.isfile(target):
            raise FileNotFoundError(target)
        with Image.open(target) as image:
            raw, size = _prepare(image, 0)
        raws.append(raw)
        sizes.append(size)
    started = time.time()
    answer = _ask(raws, question, max_tokens=1536, overrides=_overrides(api_key))
    return "left=%dx%d right=%dx%d\n%s\n%s" % (
        sizes[0][0], sizes[0][1], sizes[1][0], sizes[1][1],
        _stamp(sizes[0], int((time.time() - started) * 1000)), answer)


@srv.tool("vision_ask_many", "Ask one question about several images at once (paths separated by commas or newlines).",
          {"type": "object", "properties": {"paths": {"type": "string"}, "question": {"type": "string"},
                                            "api_key": {"type": "string", "default": ""}},
           "required": ["paths", "question"]})
def vision_ask_many(paths, question, api_key=""):
    items = [p.strip() for p in str(paths).replace(",", "\n").splitlines() if p.strip()]
    if not items:
        raise ValueError("no image paths given")
    raws = []
    for item in items[:8]:
        target = os.path.abspath(os.path.expanduser(item))
        if not os.path.isfile(target):
            raise FileNotFoundError(target)
        with Image.open(target) as image:
            raw, _size = _prepare(image, 0)
        raws.append(raw)
    started = time.time()
    answer = _ask(raws, question, max_tokens=1536, overrides=_overrides(api_key))
    return "images=%d\n%s\n%s" % (len(raws), _stamp((0, 0), int((time.time() - started) * 1000)), answer)


@srv.tool("vision_sight_check", "Check whether I can really see right now (call before claiming anything about the screen).",
          {"type": "object", "properties": {"api_key": {"type": "string", "default": ""}}, "required": []})
def vision_sight_check(api_key=""):
    cfg = _cfg()
    if not (api_key or cfg["api_key"]):
        return ("看得见？看不见。原因：没有可用的视觉 key"
                "（走 App 直连代理会自动带上；桥接模式要在 vision.api_key 里填）。")
    try:
        raw, _size = _prepare(Image.new("RGB", (64, 64), (10, 200, 10)), max_pixels=64)
        answer = _ask([raw], "这张图是什么颜色？只回答颜色名。", max_tokens=64,
                      overrides=_overrides(api_key, detail="low"))
    except Exception as exc:  # noqa: BLE001
        return "看得见？看不见。视觉调用失败：%s" % str(exc)[:200]
    return ("看得见。模型 %s 正常应答（%s）。可以放心调用 vision_see_screen / vision_read_screen_text，"
            "并在回答里描述真正看到的东西。" % (cfg["model"], answer.strip()[:40]))


@srv.tool("screenshot_for_vision", "Save a screenshot to a file (for later see_image / comparisons).",
          {"type": "object", "properties": {"out": {"type": "string", "default": ""},
                                            "monitor": {"type": "string", "default": "primary"}},
           "required": []})
def screenshot_for_vision(out="", monitor="primary"):
    image = _grab(_screen_box(monitor))
    if not out:
        out = os.path.join(os.environ.get("TEMP", "."), "screen_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
    target = os.path.abspath(os.path.expanduser(out))
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    image.save(target, format="PNG")
    return "saved=%s size=%dx%d bytes=%d\n提示：用 see_image / read_text_in_image 让模型看它" % (
        target, image.size[0], image.size[1], os.path.getsize(target))


# -------------------------------------------------------------------- samples
def _seed_images():
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    made = []
    for name, color in (("vision_a.png", (200, 60, 60)), ("vision_b.png", (60, 60, 200))):
        path = os.path.join(SAMPLE_DIR, name)
        if not os.path.isfile(path):
            Image.new("RGB", (320, 180), color).save(path)
        made.append(path)
    return made


_IMG_A, _IMG_B = _seed_images()

# Everything that talks to the vision endpoint needs a key, so those samples are optional.
SAMPLES = {
    "vision_status": {},
    "screenshot_for_vision": {"out": os.path.join(SAMPLE_DIR, "screen_sample.png"), "monitor": "primary"},
    "see_image": {"path": _IMG_A, "question": "这张图是什么颜色？"},
    "read_text_in_image": {"path": _IMG_A},
    "compare_images": {"left": _IMG_A, "right": _IMG_B},
    "see_screen": {"question": "屏幕上有哪些窗口？"},
    "describe_ui": {},
    "see_window": {"title": "aiyu MCP", "question": "这个窗口是什么？"},
}

SAMPLES_OPTIONAL = {"see_image", "read_text_in_image", "compare_images", "see_screen",
                    "describe_ui", "see_window"}


def build():
    return srv


if __name__ == "__main__":
    srv.run()
