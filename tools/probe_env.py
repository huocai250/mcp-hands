"""Diagnostic: ask the fs server itself which roots it believes are allowed."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("BRIDGE_CONFIG", os.path.join(ROOT, "configs", "bridge.config.mock.json"))
sys.path.insert(0, ROOT)

import bridge  # noqa: E402

hub = bridge.start_hub()
for target in (os.path.expanduser("~"), ROOT):
    out, err = hub.call("fs_file_info", {"path": target})
    print("%-60s -> %s %s" % (target, "ERR" if err else "ok", out.strip()[:220]))

module = __import__("servers.mcp_fs", fromlist=["ROOTS", "SAMPLES"])
print("in-process ROOTS      :", module.ROOTS)
print("in-process _SAMPLE_ROOT:", getattr(module, "_SAMPLE_ROOT", None))
print("in-process SAMPLES     :", {k: v.get("path") or v.get("root") for k, v in module.SAMPLES.items()})
hub.stop()
