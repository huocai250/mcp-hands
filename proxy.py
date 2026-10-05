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

# The key the app last sent us. The GUI's "test vision" button reuses it, because a
# direct hub call bypasses this proxy and therefore has no key of its own.
_LAST_KEY = {"token": "", "at": 0.0}


def remember_key(auth):
    token = (auth or "").replace("Bearer ", "").strip()
    if token and token.lower() != "aiyu":
        _LAST_KEY["token"] = token
        _LAST_KEY["at"] = time.time()
    return token


def last_key():
    """(token, age_seconds) of the most recent app request, or ("", None)."""
    if not _LAST_KEY["token"]:
        return "", None
    return _LAST_KEY["token"], time.time() - _LAST_KEY["at"]


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


TOOL_HINT = (
    "你在操作一台真实的 Windows 电脑，下面这些工具会立即执行，并返回真实结果。硬性规则：\n"
    "1) 要动手就直接发起工具调用；不要用文字描述调用、不要写 JSON 或代码块假装调用，"
    "也不要写 <||DSML||...> 这类标记（那是坏掉的调用，用户会看到一堆乱码）。\n"
    "2) 只有工具**真的返回了结果**才算完成；没返回就不许说「已完成」，也不许凭猜测描述屏幕内容。\n"
    "3) 万一原生调用不可用，只能用下面这种行格式作后备（一行一个调用，不要方括号）：\n"
    "TOOL: 工具名\n参数名=值\nEND\n"
    "4) 一次请求里你可以连着调度很多个动作（最多 %(rounds)s 轮 / %(seconds)s 秒），"
    "做够了再用中文汇报；到上限我会把你这轮做过的每一步列给用户看。")

# Sent only on the last round, when tools are withheld so the model must speak in prose.
FINAL_NUDGE = {"role": "user", "content": "（系统：本轮工具调用额度已用完，请直接用一两句中文总结你刚才做了什么、"
                                         "画面/页面现在是什么状态，不要再输出任何工具调用、标记或标记语言。）"}

_VISION_CACHE = {"key": "", "at": 0.0, "ok": False, "detail": ""}


def vision_status_line(auth):
    """Tell the model whether it can actually see, so it never bluffs about the screen."""
    cfg = proxy_cfg().get("vision") or bridge.CFG.get("vision") or {}
    token = (auth or "").replace("Bearer ", "").strip()
    key = cfg.get("api_key") or (token if cfg.get("inherit_upstream_key", True) and token.lower() != "aiyu" else "")
    if not key:
        return ("视觉状态：**未接通**（没有可用的视觉 key）。你现在看不见屏幕，vision_* 工具会报错——"
                "不要编造画面内容，直接告诉用户「我现在看不见」。")
    ttl = float(proxy_cfg().get("vision_check_seconds", 600) or 0)
    fresh = ttl > 0 and _VISION_CACHE["key"] == key and (time.time() - _VISION_CACHE["at"]) < ttl
    if not fresh:
        try:
            out, err = bridge.start_hub().call("vision_vision_probe", {"api_key": key})
        except Exception as exc:  # noqa: BLE001
            out, err = "%s: %s" % (type(exc).__name__, exc), True
        _VISION_CACHE.update({"key": key, "at": time.time(), "ok": not err,
                              "detail": ((out or "").splitlines() or [""])[-1][:90]})
        log("  vision check: %s %s" % ("ok" if not err else "FAILED", _VISION_CACHE["detail"]))
    if _VISION_CACHE["ok"]:
        return ("视觉状态：已接通（%s）。要看屏幕就直接调 vision_see_screen / vision_read_screen_text。"
                % (cfg.get("model") or "vision"))
    return ("视觉状态：**不通**（%s）。vision_* 会失败——不要编造画面内容，直接告诉用户「我现在看不见」。"
            % _VISION_CACHE["detail"])


def step_report(steps, mode="brief", limit=8):
    """The honesty block: every real tool result of this turn, laid out for the user."""
    if not steps or mode == "off":
        return ""
    lines = ["", "— 这轮电脑侧的真实回执 —"]
    for index, step in enumerate(steps[:limit], 1):
        output = step.get("output") or ""
        if mode == "full":
            text = output[:400].replace("\n", " | ")
        else:
            text = ((output.splitlines() or [""])[0])[:140]
        lines.append("%d. %s %s：%s" % (index, step["tool"], "失败" if step.get("error") else "成功", text))
    if len(steps) > limit:
        lines.append("…（本轮共 %d 步）" % len(steps))
    return "\n".join(lines)


def finalize(message, steps=None):
    """Last line of defence: protocol markup must never reach the phone screen."""
    if not isinstance(message, dict):
        return {"role": "assistant", "content": ""}
    content = message.get("content")
    if isinstance(content, str) and content:
        message = dict(message, content=bridge.strip_dsml(content))
    text = (message.get("content") or "").strip()
    report = step_report(steps or [], proxy_cfg().get("step_report", "brief"))
    if not text:
        text = "（这轮我做到上限了，先停一下；接着来的话说一声。）" if steps else ""
    message = dict(message, content=(text + report).strip())
    message.pop("tool_calls", None)
    return message

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
    text = bridge.strip_dsml(text)
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


def vision_args(name, args, auth):
    """Hand the chat key + configured vision endpoint to the vision tools.

    Path B relays the app's own key, so `see_screen` / `see_image` work without the
    user pasting a key anywhere. Only parameters the tool actually declares are set.
    """
    if not str(name).startswith("vision_"):
        return args
    hub = bridge.start_hub()
    spec = next((s for s in hub.specs if s["name"] == name), None)
    props = ((spec or {}).get("inputSchema") or {}).get("properties") or {}
    cfg = proxy_cfg().get("vision") or bridge.CFG.get("vision") or {}
    token = (auth or "").replace("Bearer ", "").strip()
    if "api_key" in props and not args.get("api_key"):
        if cfg.get("inherit_upstream_key", True) and token and token.lower() != "aiyu":
            args["api_key"] = token
    for key in ("model", "base_url", "detail"):
        if key in props and cfg.get(key) and not args.get(key):
            args[key] = cfg[key]
    return args


def run_tool_loop(body, auth):
    """Returns (message, steps, usage). message is an OpenAI assistant message dict."""
    hub = bridge.start_hub()
    model = body.get("model") or "deepseek-chat"
    raw_messages = list(body.get("messages") or [])
    messages = sanitize(raw_messages)
    if len(messages) != len(raw_messages):
        log("  scrubbed %d protocol-contaminated message(s) from history" % (len(raw_messages) - len(messages)))
    if proxy_cfg().get("inject_tool_hint", True):
        cfg_now = proxy_cfg()
        hint = TOOL_HINT % {"rounds": int(cfg_now.get("max_tool_rounds", 40) or 40),
                            "seconds": int(float(cfg_now.get("max_seconds", 420) or 420))}
        hint += "\n" + vision_status_line(auth)
        messages.insert(0, {"role": "system", "content": hint})
    tools = native_tools(hub)
    extra = {k: v for k, v in body.items() if k in ("temperature", "top_p", "max_tokens", "frequency_penalty", "presence_penalty", "response_format")}
    steps = []
    message = {}
    cfg = proxy_cfg()
    rounds = max(1, int(cfg.get("max_tool_rounds", 40) or 40))
    budget = float(cfg.get("max_seconds", 420) or 0)
    started = time.time()
    for round_no in range(rounds + 1):
        elapsed = time.time() - started
        if budget and elapsed > budget:
            log("  time budget %.0fs reached after %d step(s) -> wrapping up" % (budget, len(steps)))
            note = "（我已经连续操作了 %.0f 秒，先停在这里。）" % elapsed
            message = {"role": "assistant",
                       "content": (bridge.strip_dsml((message.get("content") or "").strip()) + "\n" + note).strip()}
            return finalize(message, steps), steps, {}
        last_round = round_no >= rounds
        if last_round:
            # Final round: send no tools at all. With tools still declared (and
            # tool_choice "none") DeepSeek answers with DSML tool markup *as text*,
            # which the phone app then shows to the user.
            payload = {"model": model, "messages": messages + [FINAL_NUDGE], "stream": False}
        else:
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
            # Some models still answer with the old text protocol or with DSML markup
            # (strong history bias, or a weak function-calling model). Act on it anyway.
            fallback = bridge.parse_tool_calls(content)
            if not fallback:
                return finalize(message, steps), steps, data.get("usage") or {}
            log("  model replied with %s (%d call(s)) -> executing anyway"
                % ("DSML markup" if bridge.parse_dsml_calls(content) else "text protocol", len(fallback)))
            messages.append({"role": "assistant", "content": bridge.strip_dsml(content) or content})
            results = []
            for call in fallback:
                args = vision_args(call["name"], bridge.coerce_args(call["name"], call["arguments"]), auth)
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
            args = vision_args(name, args, auth)
            output, is_error = hub.call(name, args)
            log("  tool -> %s %s" % (name, json.dumps(args, ensure_ascii=False)[:200]))
            log("  tool <- %s %s (%d chars)" % (name, "ERROR" if is_error else "ok", len(output)))
            steps.append({"tool": name, "arguments": args, "error": is_error, "output": output[:4000]})
            messages.append({"role": "tool", "tool_call_id": call.get("id") or name,
                             "content": ("[tool failed] " if is_error else "") + output[:6000]})
    # Out of rounds: never hand the app DSML markup or a dangling tool_call.
    log("  round budget exhausted after %d step(s)" % len(steps))
    return finalize(message, steps), steps, {}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mcp-hands/1.0"
    _write_lock = threading.Lock()

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
        with self._write_lock:
            self.wfile.write(("%X\r\n" % len(data)).encode("ascii") + data + b"\r\n")
            self.wfile.flush()

    def _start_heartbeat(self):
        """Keep the SSE connection warm while slow tools (screenshots, vision) run.

        A tool loop can easily take 30-120 s, and the phone app drops the request
        ("思考中断") when nothing arrives for a while. Comment frames are valid SSE,
        are ignored by every client, and reset that idle timer. They also prove to us
        that the socket is still alive.
        """
        interval = float(proxy_cfg().get("heartbeat_seconds", 5) or 0)
        if interval <= 0:
            return None
        stop = threading.Event()

        def beat():
            while not stop.wait(interval):
                try:
                    self._chunk(": keep-alive\n\n")
                except Exception:  # noqa: BLE001 - client hung up; nothing to clean here
                    return

        threading.Thread(target=beat, daemon=True, name="sse-heartbeat").start()
        return stop

    def _run_loop_streaming(self, body, auth, model):
        """Open the SSE stream *before* the tool loop, so the app never sits in silence."""
        self._sse_start()
        self._sse_open = True
        self._chunk("data: " + json.dumps({
            "id": "chatcmpl-" + uuid.uuid4().hex[:20], "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
            ensure_ascii=False) + "\n\n")
        stop = self._start_heartbeat()
        started = time.time()
        try:
            result = run_tool_loop(body, auth)
        finally:
            if stop:
                stop.set()
            log("  tool loop finished in %.1fs (heartbeats %s)"
                % (time.time() - started, "on" if stop else "off"))
        return result

    def _finish_stream(self, error_text):
        """Close an already-open SSE stream with a visible reason instead of silence."""
        cid = "chatcmpl-" + uuid.uuid4().hex[:20]
        created = int(time.time())

        def frame(delta, finish=None):
            self._chunk("data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": "proxy",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}, ensure_ascii=False) + "\n\n")

        try:
            frame({"content": "（电脑侧执行出错：%s）" % error_text[:400]})
            frame({}, "stop")
            self._chunk("data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
        except Exception:  # noqa: BLE001
            pass

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

    def _reply_stream(self, message, model, usage, sse_started=False):
        cid = "chatcmpl-" + uuid.uuid4().hex[:20]
        created = int(time.time())

        def frame(delta, finish=None):
            obj = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self._chunk("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n")

        if not sse_started:
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
        token = remember_key(auth)
        if token:
            log("  remembered the app's key (%s...) for vision calls" % token[:6])
        try:
            self._write_lock = threading.Lock()
            self._sse_open = False
            message, steps, usage = self._run_loop_streaming(body, auth, model) if body.get("stream") \
                else run_tool_loop(body, auth)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            log("  upstream HTTP %s: %s" % (exc.code, detail[:300]))
            if body.get("stream") and self._sse_open:
                self._finish_stream("upstream HTTP %s: %s" % (exc.code, detail[:300]))
                return
            self._json({"error": {"message": "upstream HTTP %s: %s" % (exc.code, detail[:600]),
                                  "type": "upstream_error"}}, 502)
            return
        except Exception as exc:  # noqa: BLE001
            log("  failed: %s: %s" % (type(exc).__name__, exc))
            if body.get("stream") and self._sse_open:
                self._finish_stream("%s: %s" % (type(exc).__name__, exc))
                return
            self._json({"error": {"message": "%s: %s" % (type(exc).__name__, exc), "type": "proxy_error"}}, 502)
            return

        if body.get("stream"):
            self._reply_stream(message, model, usage, sse_started=self._sse_open)
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
