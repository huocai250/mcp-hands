#!/usr/bin/env python3
"""Deterministic upstream that speaks *native* OpenAI function calling.

Used to verify proxy.py offline:
  * last message is a tool result   -> final answer that quotes it
  * user text contains USE:<tool>   -> returns a real tool_calls response
  * user text contains DSMLONLY:    -> returns DeepSeek-style DSML markup as plain text
  * user text contains TEXTUSE:     -> returns the bridge-era text protocol
  * otherwise                       -> plain reply
"""
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_FLAKY = {"count": 0}
_FLAKY_LOCK = threading.Lock()


def last_of(messages, role):
    for message in reversed(messages or []):
        if message.get("role") == role:
            return message
    return {}


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
        self._json({"object": "list", "data": [{"id": "mock-native", "object": "model"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        body = json.loads(raw.decode("utf-8", "replace") or "{}")
        messages = body.get("messages") or []
        joined = "\n".join(m.get("content") or "" for m in messages if isinstance(m.get("content"), str))
        # Flaky mode: fail the first N calls, then behave normally (drives retry tests).
        match = re.search(r"FLAKY:(\d+)", joined)
        if match:
            wanted = int(match.group(1))
            with _FLAKY_LOCK:
                if _FLAKY["count"] < wanted:
                    _FLAKY["count"] += 1
                    data = json.dumps({"error": {"message": "temporary upstream failure"}}).encode()
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
        tool_message = last_of(messages, "tool")
        user_text = last_of(messages, "user").get("content") or ""
        offered = [t["function"]["name"] for t in (body.get("tools") or [])]

        if "CYCLE:" in user_text:
            # Rotate through several tools so distinct progress labels are produced.
            # Form: CYCLE: tool1,tool2,tool3 <total>
            line = [l for l in user_text.splitlines() if "CYCLE:" in l][0]
            spec = line.split("CYCLE:")[1].strip().split()
            tools = [t for t in spec[0].split(",") if t]
            total = int(spec[1]) if len(spec) > 1 else 6
            done = sum(1 for m in messages if m.get("role") == "tool")
            if done < total:
                pick = tools[done % len(tools)]
                message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_%d" % int(time.time() * 1000), "type": "function",
                     "function": {"name": pick, "arguments": "{}"}}]}
            else:
                message = {"role": "assistant", "content": "轮换动作做完了，共 %d 步。" % done}
        elif "LOOP:" in user_text:
            # Keep calling a tool until N tool results exist: exercises long chains and
            # the round budget without needing a real model. Checked first, because a
            # tool result is already in the history after the first round.
            # Form: LOOP: <tool> <n> [json-args]
            line = [l for l in user_text.splitlines() if "LOOP:" in l][0]
            spec = line.split("LOOP:")[1].strip()
            bits = spec.split()
            tool = bits[0]
            target = int(bits[1]) if len(bits) > 1 and bits[1].isdigit() else 3
            args = json.loads(" ".join(bits[2:])) if len(bits) > 2 else {}
            done = sum(1 for m in messages if m.get("role") == "tool")
            if done < target:
                message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_%d" % int(time.time() * 1000), "type": "function",
                     "function": {"name": tool, "arguments": json.dumps(args)}}]}
            else:
                message = {"role": "assistant", "content": "链式动作做完了，共 %d 步。" % done}
        elif tool_message or "TOOL_RESULT" in user_text:
            text = (tool_message.get("content") or user_text).strip()
            content = "工具结果收到啦～\n> %s\n\n[offered_tools=%d]" % (text.splitlines()[0][:200], len(offered))
            message = {"role": "assistant", "content": content}
        elif "TEXTUSE:" in user_text:
            line = [l for l in user_text.splitlines() if "TEXTUSE:" in l][0]
            tool = line.split("TEXTUSE:")[1].strip().split()[0]
            args = {}
            if "ARGS:" in line:
                args = json.loads(line.split("ARGS:")[1].strip())
            body = "\n".join(["%s=%s" % (k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) for k, v in args.items()])
            message = {"role": "assistant", "content": "我去查一下～\nTOOL: %s\n%s\nEND" % (tool, body)}
        elif "INSPECT" in user_text:
            joined = "\n".join((m.get("content") or "") if isinstance(m.get("content"), str) else "" for m in messages)
            message = {"role": "assistant", "content": "INSPECT messages=%d tool_marks=%d protocol_heads=%d system=%d" % (
                len(messages), joined.count("TOOL:") + joined.count("TOOL_PROTOCOL"),
                joined.count("TOOL_PROTOCOL_V1") + joined.count("外部工具协议"),
                sum(1 for m in messages if m.get("role") == "system"))}
        elif "ECHO_SYS" in user_text:
            system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
            message = {"role": "assistant", "content": "SYS>>> %s" % system}
        elif "DSMLONLY:" in user_text:
            # Reproduce DeepSeek emitting tool markup as *text* (no native tool_calls),
            # the exact shape seen in the phone app.
            line = [l for l in user_text.splitlines() if "DSMLONLY:" in l][0]
            tool = line.split("DSMLONLY:")[1].strip().split()[0]
            args = {}
            if "ARGS:" in line:
                args = json.loads(line.split("ARGS:")[1].strip())
            params = "\n".join(
                '<||DSML||parameter name="%s" string="%s">%s</||DSML||parameter>'
                % (k, "true" if isinstance(v, str) else "false", v)
                for k, v in args.items())
            content = ("我用键盘试试～\n<||DSML||calls>\n<||DSML||invoke name=\"%s\">\n%s\n</||DSML||invoke>\n</||DSML||calls>"
                       % (tool, params))
            message = {"role": "assistant", "content": content}
        elif "USE:" in user_text:
            line = [l for l in user_text.splitlines() if "USE:" in l][0]
            tool = line.split("USE:")[1].strip().split()[0]
            args = {}
            if "ARGS:" in line:
                args = json.loads(line.split("ARGS:")[1].strip())
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_%d" % int(time.time() * 1000), "type": "function",
                 "function": {"name": tool, "arguments": json.dumps(args, ensure_ascii=False)}}]}
        else:
            message = {"role": "assistant", "content": "你好呀～（mock-native，收到 %d 个工具）" % len(offered)}

        finish = "tool_calls" if message.get("tool_calls") else "stop"
        self._json({
            "id": "mock-%d" % int(time.time() * 1000),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "mock-native",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8891
    print("mock native-tool upstream on http://127.0.0.1:%d/v1" % port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
