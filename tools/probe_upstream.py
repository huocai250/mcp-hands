"""Isolate why the persona returns empty content: plain vs protocol-prefixed prompt."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["BRIDGE_CONFIG"] = os.path.join(ROOT, "configs", "bridge.config.mock.json")
sys.path.insert(0, ROOT)

import bridge  # noqa: E402


def ask(text, label):
    payload = bridge.upstream_chat([{"role": "user", "content": text}])
    message = (payload.get("choices") or [{}])[0].get("message") or {}
    content = message.get("content") or ""
    reasoning = message.get("reasoning_content") or ""
    print("%-28s prompt_chars=%5d  content_chars=%4d  reasoning_chars=%4d  finish=%s"
          % (label, len(text), len(content), len(reasoning), (payload.get("choices") or [{}])[0].get("finish_reason")))
    print("    content head: %r" % content[:120])
    return content


ask("你好呀，用一句话回答我", "plain short")
hub = bridge.start_hub()
block = bridge.protocol_block(hub.specs)
ask(block + "\n\n[用户消息]\n你好呀，用一句话回答我", "protocol block + question")
ask(block[:4000] + "\n\n[用户消息]\n你好呀，用一句话回答我", "protocol head 4k + question")
hub.stop()
