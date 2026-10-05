"""End-to-end check for the *app-native* path: does the proxy hand the chat key to vision?

Needs two mocks running first:
    python tools/mock_native.py 8891     # chat upstream that emits native tool_calls
    python tools/mock_vision.py 8894     # vision endpoint that reports whether a key arrived

Then:  python tools/proxy_vision_check.py
It starts the proxy on 127.0.0.1:8892, asks it (with a fake Bearer token) to look at an
image via `vision_see_image`, and prints what the vision endpoint received.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.gettempdir()
CONFIG = os.path.join(TMP, "mcp-hands-proxy-vision-test.json")

img = os.path.join(TMP, "proxy_vision_a.png")
from PIL import Image  # noqa: E402
Image.new("RGB", (360, 200), (200, 60, 60)).save(img)

config = {
    "listen": {"host": "127.0.0.1", "port": 8881},
    "upstream": {"name": "mock", "base_url": "http://127.0.0.1:8899/v1", "api_key": "mock",
                 "model": "mock-persona", "timeout_s": 30},
    "proxy": {"enabled": True, "listen": {"host": "127.0.0.1", "port": 8892},
              "upstream_base": "http://127.0.0.1:8891/v1", "upstream_api_key": "",
              "tool_models": ["mock-native"], "max_tool_rounds": 4, "inject_tool_hint": True},
    "vision": {"base_url": "http://127.0.0.1:8894/v1", "api_key": "",
               "model": "mock-vision", "detail": "auto", "max_pixels": 1300,
               "inherit_upstream_key": True},
    "servers": [{"name": "fs", "enabled": True, "env": {"MCP_FS_ROOTS": TMP}},
                {"name": "vision", "enabled": True}],
}
with open(CONFIG, "w", encoding="utf-8") as fh:
    json.dump(config, fh, ensure_ascii=False, indent=2)
os.environ["BRIDGE_CONFIG"] = CONFIG
os.environ["MCP_SAMPLES_DIR"] = TMP
sys.path.insert(0, ROOT)

import bridge  # noqa: E402
import proxy as proxy_module  # noqa: E402

service = proxy_module.ProxyService().start()
time.sleep(0.5)

body = {"model": "mock-native", "stream": False,
        "messages": [{"role": "user",
                      "content": 'USE: vision_see_image ARGS: {"path": "%s", "question": "这图什么颜色"}' % img.replace("\\", "/")}]}
request = urllib.request.Request("http://127.0.0.1:8892/v1/chat/completions",
                                 data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer sk-fake-app-key-123"})
with urllib.request.urlopen(request, timeout=120) as response:
    payload = json.loads(response.read().decode("utf-8", "replace"))
steps = (payload.get("proxy") or {}).get("tool_steps") or []
print("steps:", [(s["tool"], "ERR" if s["error"] else "ok") for s in steps])
for step in steps:
    print("  output:", (step["output"] or "").splitlines()[0][:200])
content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
print("final reply:", content.replace("\n", " | ")[:200])
key_line = ""
for step in steps:
    for line in (step["output"] or "").splitlines():
        if "key=" in line:
            key_line = line
print("key inherited by the vision call:", "provided" in key_line)
service.stop()
bridge.stop_hub()
raise SystemExit(0 if steps and not steps[0]["error"] else 1)
