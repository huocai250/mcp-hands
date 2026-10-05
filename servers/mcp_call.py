"""MCP server: call -- the voice channel (4.1).

The chat client cannot play audio inline, so this server talks to the **voice page**
(`http://<pc>:8890/voice`) the user keeps open on the phone: the page speaks every line
queued here with the phone's own text-to-speech, and posts back what the user says.

So a "voice call" is: she queues a line -> the phone says it out loud; the user talks ->
`call_listen` reads what was recognised. No audio files to open, no extra app.
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

API = (os.environ.get("MCP_BRIDGE_API") or "http://127.0.0.1:8877").rstrip("/")

srv = Server("call")


def _api(path, payload=None, method="GET", timeout=30):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(API + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}"), ""
    except urllib.error.HTTPError as exc:
        return {}, "HTTP %s: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:200])
    except Exception as exc:  # noqa: BLE001
        return {}, "%s: %s" % (type(exc).__name__, exc)


@srv.tool("call_say", "Speak a line out loud on the user's phone (they keep the voice page open). "
                      "Use this instead of sending a voice file.",
          {"type": "object", "properties": {"text": {"type": "string"},
                                            "interrupt": {"type": "boolean", "default": False,
                                                          "description": "cut off whatever is playing"},
                                            "rate": {"type": "number", "default": 1.0}},
           "required": ["text"]})
def call_say(text, interrupt=False, rate=1.0):
    data, error = _api("/v2/voice/say", {"text": text, "interrupt": bool(interrupt), "rate": rate},
                       method="POST")
    if error:
        return "could not queue the line: %s" % error
    line = data.get("line") or {}
    return ("queued #%s for the phone to speak (%d chars)\nnote=手机上的语音页会自动念出来；"
            "如果用户还没打开语音页，提醒他打开语音页并点「开始通话」"
            % (line.get("seq"), len(str(text or ""))))


@srv.tool("call_listen", "What the user said out loud on the voice page (the phone's speech "
                          "recognition). Call this before replying when a call is going on.",
          {"type": "object", "properties": {"mark": {"type": "boolean", "default": True,
                                                     "description": "mark them as read"}},
           "required": []})
def call_listen(mark=True):
    data, error = _api("/v2/voice?kind=heard&since=0")
    if error:
        return "could not read the voice channel: %s" % error
    lines = [item for item in (data.get("lines") or [])
             if not item.get("spoken") or item.get("kind") != "say"]
    heard = [item for item in (data.get("lines") or []) if item.get("kind") == "heard" and not item.get("read")]
    if not heard:
        stats = data.get("stats") or {}
        return ("用户还没在语音页里说话（语音页状态：%s）" % json.dumps(stats, ensure_ascii=False))
    if mark:
        for item in heard:
            _api("/v2/voice/read", {"seq": item.get("seq")}, method="POST")   # mark as read
    return "\n".join("用户说(#%s)：%s" % (item.get("seq"), item.get("text")) for item in heard)


@srv.tool("call_state", "Is the voice page connected, how many lines were spoken, anything unread.",
          {"type": "object", "properties": {}, "required": []})
def call_state():
    data, error = _api("/v2/voice?since=0")
    if error:
        return "could not read the voice channel: %s" % error
    stats = dict(data.get("stats") or {})
    stats["url"] = "%s/voice" % API
    return json.dumps(stats, ensure_ascii=False, indent=2)


@srv.tool("call_clear", "Empty the voice queue (use when starting a fresh call).",
          {"type": "object", "properties": {}, "required": []})
def call_clear():
    data, error = _api("/v2/voice/clear", {}, method="POST")
    return ("could not clear: %s" % error) if error else "cleared %s line(s)" % data.get("cleared", 0)


SAMPLES = {"call_state": {}}


def build():
    return srv


if __name__ == "__main__":
    srv.run()
