#!/usr/bin/env python3
"""Deterministic OpenAI-compatible *vision* endpoint for offline testing.

Accepts the same payload the real endpoint does (content blocks with
`image_url` data URLs) and answers with a canned description plus how many
images and how large they were. Used by `configs/bridge.config.mock.json`
(MCP_VISION_BASE_URL=http://127.0.0.1:8894/v1) so `--self-test` can exercise
the whole see_image / see_screen / OCR path without a real API key.
"""
import base64
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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
        self._json({"object": "list", "data": [{"id": "mock-vision", "object": "model"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8", "replace") or "{}")
        auth = self.headers.get("Authorization", "")
        images, text_parts = [], []
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, str):
                text_parts.append(content)
                continue
            for block in content or []:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "image_url":
                    url = (block.get("image_url") or {}).get("url", "")
                    raw = base64.b64decode(url.split(",", 1)[1]) if "," in url else b""
                    images.append(len(raw))
        question = text_parts[-1] if text_parts else ""
        answer = ("[mock-vision] 收到 %d 张图片（字节数 %s），detail=%s，key=%s；你的问题是：%s。"
                  "画面里有一个蓝色方块和一行文字 MOCK-OK。"
                  % (len(images), images, (body.get("messages") or [{}])[0].get("content") and "auto" or "auto",
                     "provided" if auth else "missing", question[:80]))
        if len(images) == 2:
            answer = "[mock-vision] 两张图对比：左边偏红、右边偏蓝，其余内容一致。"
        self._json({
            "id": "mockv-%d" % int(time.time() * 1000),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model") or "mock-vision",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8894
    print("mock vision endpoint on http://127.0.0.1:%d/v1" % port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
