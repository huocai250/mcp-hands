"""MCP server: device -- who is allowed to drive this PC (4.0).

Read-only on purpose: approving a new device or revoking one is a security decision
the *user* makes in the console (`--devices-approve` / dashboard), not something the
persona should be able to do behind their back.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

API = (os.environ.get("MCP_BRIDGE_API") or "http://127.0.0.1:8877").rstrip("/")

srv = Server("device")


def _call(path, timeout=15):
    try:
        with urllib.request.urlopen(API + path, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}"), ""
    except urllib.error.HTTPError as exc:
        return {}, "HTTP %s: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:200])
    except Exception as exc:  # noqa: BLE001
        return {}, "%s: %s (is the bridge running? expected at %s)" % (type(exc).__name__, exc, API)


def _when(value):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value or 0))) or "-"
    except (TypeError, ValueError):
        return "-"


@srv.tool("devices_list", "Devices that are allowed to use this PC (name, when added, last seen, call count).",
          {"type": "object", "properties": {}, "required": []})
def devices_list():
    data, error = _call("/v2/devices")
    if error:
        return "could not read devices: %s" % error
    devices = data.get("devices") or []
    mode = (data.get("stats") or {}) 
    if not devices:
        return "no device is registered yet (mode=%s)\n%s" % (data.get("mode"), json.dumps(mode, ensure_ascii=False))
    lines = ["devices=%d mode=%s" % (len(devices), data.get("mode"))]
    for device in devices:
        lines.append("%s %s [%s] added=%s last_seen=%s calls=%s" % (
            device.get("id"), device.get("name"), device.get("masked"), _when(device.get("created")),
            _when(device.get("last_seen")), device.get("calls")))
    return "\n".join(lines)


@srv.tool("devices_pending", "Devices that tried to connect but are waiting for the user's approval.",
          {"type": "object", "properties": {}, "required": []})
def devices_pending():
    data, error = _call("/v2/devices?pending=1")
    if error:
        return "could not read pending devices: %s" % error
    items = data.get("pending") or []
    if not items:
        return "no pending device request"
    lines = ["pending=%d（approval 由用户在控制台或命令行完成）" % len(items)]
    for item in items:
        lines.append("%s key=%s tries=%s first=%s ip=%s" % (
            item.get("id"), item.get("masked"), item.get("seen"), _when(item.get("first_seen")), item.get("ip")))
    return "\n".join(lines)


@srv.tool("devices_stats", "Device summary: registered, pending, total calls, last activity.",
          {"type": "object", "properties": {}, "required": []})
def devices_stats():
    data, error = _call("/v2/devices?stats=1")
    if error:
        return "could not read device stats: %s" % error
    stats = dict(data.get("stats") or {})
    stats["mode"] = data.get("mode")
    stats["last_seen"] = _when(stats.get("last_seen"))
    return json.dumps(stats, ensure_ascii=False, indent=2)


SAMPLES = {"devices_stats": {}, "devices_list": {}}


def build():
    return srv


if __name__ == "__main__":
    srv.run()
