"""Vision path check: capture -> encode -> POST -> text, using the mock vision endpoint.

Run tools/mock_vision.py first (port 8894), then:
    .build-venv\\Scripts\\python.exe tools\\vision_check.py
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.gettempdir()
CONFIG = os.path.join(TMP, "mcp-hands-vision-test.json")

config = {
    "listen": {"host": "127.0.0.1", "port": 8881},
    "upstream": {"name": "mock", "base_url": "http://127.0.0.1:8899/v1", "api_key": "mock",
                 "model": "mock-persona", "timeout_s": 30},
    "vision": {"base_url": "http://127.0.0.1:8894/v1", "api_key": "mock-key",
               "model": "mock-vision", "detail": "auto", "max_pixels": 1300,
               "inherit_upstream_key": False},
    "servers": [{"name": "fs", "enabled": True,
                 "env": {"MCP_FS_ROOTS": TMP + ";" + ROOT}},
                {"name": "vision", "enabled": True}],
}
with open(CONFIG, "w", encoding="utf-8") as fh:
    json.dump(config, fh, ensure_ascii=False, indent=2)
os.environ["BRIDGE_CONFIG"] = CONFIG
os.environ["MCP_SAMPLES_DIR"] = TMP
sys.path.insert(0, ROOT)

import bridge  # noqa: E402
from PIL import Image  # noqa: E402

shot = os.path.join(TMP, "vision_check_shot.png")
img_a = os.path.join(TMP, "vision_check_a.png")
img_b = os.path.join(TMP, "vision_check_b.png")
Image.new("RGB", (400, 220), (200, 60, 60)).save(img_a)
Image.new("RGB", (400, 220), (60, 60, 200)).save(img_b)

CALLS = [
    ("vision_vision_status", {}),
    ("vision_screenshot_for_vision", {"out": shot}),
    ("vision_see_image", {"path": img_a, "question": "这张图是什么颜色？"}),
    ("vision_read_text_in_image", {"path": img_a}),
    ("vision_see_screen", {"question": "屏幕上有哪些窗口？"}),
    ("vision_describe_ui", {"task": "打开浏览器"}),
    ("vision_compare_images", {"left": img_a, "right": img_b}),
    ("vision_vision_ask_many", {"paths": img_a + "\n" + img_b, "question": "分别是什么颜色"}),
    ("vision_vision_probe", {}),
]

hub = bridge.start_hub()
print("tools registered:", len(hub.specs))
fails = 0
for name, args in CALLS:
    started = time.time()
    out, err = hub.call(name, args)
    head = (out or "").splitlines()[0][:110] if out else ""
    if err:
        fails += 1
        print("FAIL %-32s %s" % (name, head or "error"))
    else:
        print("ok   %-32s %5.1fs  %s" % (name, time.time() - started, head))
print("failures:", fails)
hub.stop()
raise SystemExit(1 if fails else 0)
