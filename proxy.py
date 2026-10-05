#!/usr/bin/env python3
"""app-native tool proxy — point the phone app's model provider here.

The app speaks plain OpenAI to this service; we attach the same MCP tool catalogue
as **native** OpenAI `tools`, then run the tool loop against a real upstream
(DeepSeek / SiliconFlow / any OpenAI-compatible base) using the API key the app
sends us. The app sees one ordinary assistant reply — no `TOOL:` blocks in the chat.

Requests for models that are not in `tool_models` are relayed untouched
(so a vision/audio helper model keeps working).
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FROZEN = bool(getattr(sys, "frozen", False))
HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else HERE
sys.path.insert(0, HERE)

import bridge  # noqa: E402

LOG_LOCK = threading.Lock()


def log(msg):
    bridge.log(msg)


def proxy_cfg():
    return bridge.CFG.get("proxy") or {}


def upstream_base():
    return (proxy_cfg().get("upstream_base") or "https://api.deepseek.com/v1").rstrip("/")


def upstream_post(path, payload, auth, timeout=240):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(upstream_base() + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": auth or ("Bearer " + proxy_cfg().get("upstream_api_key", ""))})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def upstream_open(path, payload, auth, timeout=240):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(upstream_base() + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": auth or ("Bearer " + proxy_cfg().get("upstream_api_key", ""))})
    return urllib.request.urlopen(req, timeout=timeout)


TOOL_HINT = ("你可以调用下面这些工具来真的操作这台 Windows 电脑，工具会立即执行并返回真实结果。"
             "需要时直接发起工具调用，不要用文字描述工具调用、也不要输出 JSON 或代码块来假装调用。")

# The app's own conversation still contains the bridge era text protocol (injected
# instructions, the model's TOOL: blocks, TOOL_RESULT turns). Leaving them in the
# prompt makes the model imitate that style instead of using native tool calls.
_PROTOCOL_HEADS = ("TOOL_PROTOCOL_V1", "[外部工具协议", "[运行环境]", "工具清单（每行")
_TOOL_BLOCK = re.compile(r"(?ms)^[ \t]*TOOL[ \t]*[::][ \t]*[A-Za-z0-9_.\-]+[ \t]*$.*?^[ \t]*END[ \t]*$")
_JSON_BLOCK = re.compile(r"```(?:tool|json|tool_call)\s*\n.*?\n?```", re.S | re.I)
_USER_MARKERS = ("用户消息:", "[用户消息]")


def scrub(text):
    """Remove text-protocol artefacts from one message, keeping the real user text."""
    if not isinstance(text, str):
        return text
    for marker in _USER_MARKERS:
        if marker in text:
            text = text.split(marker, 1)[1]
            break
    else:
        head = text[:200]
        if any(mark in head for mark in _PROTOCOL_HEADS):
            return ""
    text = _TOOL_BLOCK.sub("", text)
    text = _JSON_BLOCK.sub("", text)
    lines = [line for line in text.splitlines() if not line.strip().startswith("TOOL_RESULT")]
    return "\n".join(lines).strip()


def sanitize(messages):
    """Drop protocol contamination so the model sees a normal conversation."""
    cleaned = []
    for message in messages or []:
        content = message.get("content")
        if isinstance(content, str):
            text = scrub(content)
            if not text:
                continue  # whole message was protocol noise
            message = dict(message, content=text)
        cleaned.append(message)
    return cleaned


def native_tools(hub):
    tools = []
    for spec in hub.specs:
        tools.append({"type": "function", "function": {
            "name": spec["name"], "description": spec["description"],
            "parameters": spec.get("inputSchema") or {"type": "object", "properties": {}}}})
    return tools


def run_tool_loop(body, auth):
    """Returns (message, steps, usage). message is an OpenAI assistant message dict."""
    hub = bridge.start_hub()
    model = body.get("model") or "deepseek-chat"
    raw_messages = list(body.get("messages") or [])
    messages = sanitize(raw_messages)
    if len(messages) != len(raw_messages):
        log("  scrubbed %d protocol-contaminated message(s) from history" % (len(raw_messages) - len(messages)))
    if proxy_cfg().get("inject_tool_hint", True):
        messages.insert(0, {"role": "system", "content": TOOL_HINT})
    tools = native_tools(hub)
    extra = {k: v for k, v in body.items() if k in ("temperature", "top_p", "max_tokens", "frequency_penalty", "presence_penalty", "response_format")}
    steps = []
    message = {}
    for round_no in range(int(proxy_cfg().get("max_tool_rounds", 6)) + 1):
        payload = {"model": model, "messages": messages, "stream": False,
                   "tools": tools, "tool_choice": "auto"}
        payload.update(extra)
        data = upstream_post("/chat/completions", payload, auth)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = message.get("tool_calls") or []
        content = message.get("content") or ""
        log("  round %d: finish=%s tool_calls=%d" % (round_no, choice.get("finish_reason"), len(calls)))

        if not calls:
            # Some models still answer with the old text protocol (strong history bias
            # or a weak function-calling model). Detect it and act on it anyway.
            fallback = bridge.parse_tool_calls(content)
            if not fallback:
                return message, steps, data.get("usage") or {}
            log("  model replied with text protocol (%d call(s)) -> executing anyway" % len(fallback))
            messages.append({"role": "assistant", "content": content})
            results = []
            for call in fallback:
                args = bridge.coerce_args(call["name"], call["arguments"])
                output, is_error = hub.call(call["name"], args)
                log("  tool -> %s %s" % (call["name"], json.dumps(args, ensure_ascii=False)[:200]))
                log("  tool <- %s %s (%d chars)" % (call["name"], "ERROR" if is_error else "ok", len(output)))
                steps.append({"tool": call["name"], "arguments": args, "error": is_error, "output": output[:4000]})
                results.append("TOOL_RESULT: %s %s\n%s" % (call["name"], "(failed)" if is_error else "(ok)", output[:6000]))
            messages.append({"role": "user", "content": "\n\n".join(results)})
            continue

        messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
        for call in calls:
            name = (call.get("function") or {}).get("name") or ""
            raw = (call.get("function") or {}).get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {"raw": raw}
            output, is_error = hub.call(name, args)
            log("  tool -> %s %s" % (name, json.dumps(args, ensure_ascii=False)[:200]))
            log("  tool <- %s %s (%d chars)" % (name, "ERROR" if is_error else "ok", len(output)))
            steps.append({"tool": name, "arguments": args, "error": is_error, "output": output[:4000]})
            messages.append({"role": "tool", "tool_call_id": call.get("id") or name,
                             "content": ("[tool failed] " if is_error else "") + output[:6000]})
    return message, steps, {}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mcp-hands/1.0"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------------ helpers
    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, text):
        data = text.encode("utf-8")
        self.wfile.write(("%X\r\n" % len(data)).encode("ascii") + data + b"\r\n")

    def _relay(self, body, auth, path):
        try:
            upstream = upstream_open(path, body, auth)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            self._json({"error": {"message": "upstream HTTP %s: %s" % (exc.code, detail[:600]),
                                  "type": "upstream_error"}}, 502)
            return
        except Exception as exc:  # noqa: BLE001
            self._json({"error": {"message": "upstream error: %s" % exc, "type": "upstream_error"}}, 502)
            return
        self.send_response(upstream.status)
        self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while True:
            block = upstream.read(4096)
            if not block:
                break
            self.wfile.write(("%X\r\n" % len(block)).encode("ascii") + block + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        upstream.close()

    def _reply_stream(self, message, model, usage):
        cid = "chatcmpl-" + uuid.uuid4().hex[:20]
        created = int(time.time())

        def frame(delta, finish=None):
            obj = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self._chunk("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n")

        self._sse_start()
        frame({"role": "assistant", "content": ""})
        reasoning = message.get("reasoning_content")
        if reasoning:
            self._chunk("data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"reasoning_content": reasoning}, "finish_reason": None}]}, ensure_ascii=False) + "\n\n")
        content = message.get("content") or ""
        for index in range(0, len(content), 40):
            frame({"content": content[index:index + 40]})
            time.sleep(0.01)
        frame({}, "stop")
        usage_frame = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                       "choices": [], "usage": usage}
        self._chunk("data: " + json.dumps(usage_frame, ensure_ascii=False) + "\n\n")
        self._chunk("data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")

    # -------------------------------------------------------------------- routes
    def do_GET(self):
        if "models" in self.path:
            models = list(proxy_cfg().get("tool_models") or [])
            try:
                req = urllib.request.Request(upstream_base() + "/models",
                                             headers={"Authorization": self.headers.get("Authorization", "")})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    for item in (json.loads(resp.read().decode("utf-8", "replace")).get("data") or []):
                        if item.get("id") and item["id"] not in models:
                            models.append(item["id"])
            except Exception as exc:  # noqa: BLE001
                log("  models relay failed: %s" % exc)
            self._json({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "aiyu-proxy"} for m in models]})
        elif "health" in self.path:
            hub = bridge.HUB
            self._json({"ok": True, "role": "app-native tool proxy", "upstream": upstream_base(),
                        "tool_models": proxy_cfg().get("tool_models"),
                        "tools": len(hub.specs) if hub else 0})
        else:
            self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8-sig", "replace") or "{}")
        except json.JSONDecodeError as exc:
            self._json({"error": {"message": "bad json: %s" % exc}}, 400)
            return
        auth = self.headers.get("Authorization", "")
        model = body.get("model") or ""
        wanted = proxy_cfg().get("tool_models") or []
        if wanted and model not in wanted:
            log("relay (no tools): model=%s stream=%s" % (model, body.get("stream")))
            self._relay(body, auth, "/chat/completions")
            return
        log("tool request: model=%s messages=%d stream=%s" % (model, len(body.get("messages") or []), body.get("stream")))
        try:
            message, steps, usage = run_tool_loop(body, auth)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            log("  upstream HTTP %s: %s" % (exc.code, detail[:300]))
            self._json({"error": {"message": "upstream HTTP %s: %s" % (exc.code, detail[:600]),
                                  "type": "upstream_error"}}, 502)
            return
        except Exception as exc:  # noqa: BLE001
            log("  failed: %s: %s" % (type(exc).__name__, exc))
            self._json({"error": {"message": "%s: %s" % (type(exc).__name__, exc), "type": "proxy_error"}}, 502)
            return

        if body.get("stream"):
            self._reply_stream(message, model, usage)
            return
        self._json({
            "id": "chatcmpl-" + uuid.uuid4().hex[:20],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "logprobs": None, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "proxy": {"tool_steps": steps},
        })


class ProxyService:
    """Small wrapper so the GUI can start/stop the proxy in-process."""

    def __init__(self):
        self.server = None
        self.thread = None

    def start(self):
        cfg = proxy_cfg()
        host = (cfg.get("listen") or {}).get("host", "0.0.0.0")
        port = int((cfg.get("listen") or {}).get("port", 8890))
        bridge.start_hub()
        self.server = ThreadingHTTPServer((host, port), ProxyHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        log("app-native tool proxy listening on http://%s:%d/v1 (upstream %s, tool models %s)"
            % (host, port, upstream_base(), cfg.get("tool_models")))
        return self

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.server = None
            log("app-native tool proxy stopped")

    @property
    def running(self):
        return self.server is not None


def main():
    bridge.log("=" * 62)
    bridge.log(bridge.version_line())
    bridge.log("app-native tool proxy %s" % ("(packed exe)" if FROZEN else "(source)"))
    bridge.log("config : %s" % bridge.CONFIG_PATH)
    ProxyService().start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
