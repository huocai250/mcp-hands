#!/usr/bin/env python3
"""Deterministic fake persona endpoint for offline testing of the tool loop.

Behaviour on the last user message:
  * contains "[TOOL_RESULT]"  -> final answer that quotes the result
  * contains "[USE] <tool>"   -> emits a ```tool block with [ARGS] {...} if present
  * otherwise                 -> plain persona-ish reply

Listens on 127.0.0.1:8899, OpenAI-compatible POST /v1/chat/completions.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def last_user(messages):
    for m in reversed(messages or []):
        if m.get("role") == "user":
            return m.get("content") or ""
    return ""


def _flat(value):
    """Render a sample argument the way the line protocol expects it."""
    if isinstance(value, list):
        if value and isinstance(value[0], list):
            return ";".join(",".join(str(cell) for cell in row) for row in value)
        return "|".join(str(item) for item in value)
    return str(value)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._json({"object": "list", "data": [{"id": "mock-persona", "object": "model"}]})
        else:
            self._json({"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8", "replace") or "{}")
        text = last_user(body.get("messages"))

        uses = [l for l in text.splitlines() if l.startswith("[USE]")]
        argss = [l for l in text.splitlines() if l.startswith("[ARGS]")]

        if text.strip().startswith("TOOL_RESULT"):
            head = text.strip().splitlines()[0]
            content = "收到工具结果啦～\n> %s\n\n现在我知道了 😺" % head[:300]
        elif uses:
            blocks = []
            for i, use in enumerate(uses):
                tool = use.replace("[USE]", "").strip()
                raw = argss[i].replace("[ARGS]", "").strip() if i < len(argss) else "{}"
                lines = ["TOOL: " + tool]
                for key, value in json.loads(raw).items():
                    lines.append("%s=%s" % (key, _flat(value)))
                lines.append("END")
                blocks.append("\n".join(lines))
            content = "我去查一下～\n" + "\n".join(blocks)
        else:
            content = "你好呀～有什么要我做的吗？😺"

        self._json({
            "id": "mock-%d" % int(time.time() * 1000),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "mock-persona",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    print("mock persona on http://127.0.0.1:%d/v1" % port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
