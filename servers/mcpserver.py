"""Tiny stdio MCP server helper: JSON-RPC 2.0 over newline-delimited stdin/stdout.

Implements just enough of the Model Context Protocol for tool serving:
  initialize / notifications/initialized / tools/list / tools/call / ping
stdout carries protocol messages only; logs go to stderr.
"""
import json
import os
import sys

PROTOCOL_VERSION = "2024-11-05"


def sample_dir():
    """A real, readable directory for self-test samples.

    Must survive PyInstaller freezing, where __file__ points inside the archive
    and the source tree is not on disk: the bridge sets MCP_SAMPLES_DIR, which
    is the packed exe's own folder.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("MCP_SAMPLES_DIR") or "",
        os.path.dirname(here),
        os.environ.get("TEMP") or "",
        os.getcwd(),
    ]
    for cand in candidates:
        if cand and os.path.isdir(cand):
            return cand
    return os.getcwd()


class Server:
    def __init__(self, name, version="1.0.0"):
        self.name = name
        self.version = version
        self.tools = {}

    def tool(self, name, description, schema):
        def deco(fn):
            self.tools[name] = {"description": description, "schema": schema, "fn": fn}
            return fn

        return deco

    def _write(self, obj):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _log(self, msg):
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()

    def _result(self, req_id, result):
        self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id, code, message):
        self._write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    def run(self):
        # The bridge reads our stdout as UTF-8; Windows would otherwise default to cp936
        # and non-ASCII tool output (localized names, Chinese text) would arrive invalid.
        for stream in (sys.stdin, sys.stdout):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                self._log("bad json line: %r" % line[:200])
                continue

            method = msg.get("method")
            req_id = msg.get("id")
            params = msg.get("params") or {}

            if method == "initialize":
                self._result(req_id, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.name, "version": self.version},
                })
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                self._result(req_id, {"tools": [
                    {"name": n, "description": t["description"], "inputSchema": t["schema"]}
                    for n, t in self.tools.items()
                ]})
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                entry = self.tools.get(name)
                if entry is None:
                    self._result(req_id, {"content": [{"type": "text", "text": "unknown tool: %s" % name}], "isError": True})
                    continue
                try:
                    out = entry["fn"](**args)
                    if isinstance(out, dict) and "content" in out:
                        self._result(req_id, out)
                    else:
                        self._result(req_id, {"content": [{"type": "text", "text": str(out)}], "isError": False})
                except TypeError as exc:
                    self._result(req_id, {"content": [{"type": "text", "text": "bad arguments: %s" % exc}], "isError": True})
                except Exception as exc:
                    self._result(req_id, {"content": [{"type": "text", "text": "%s: %s" % (type(exc).__name__, exc)}], "isError": True})
            elif method == "ping":
                self._result(req_id, {})
            elif req_id is not None:
                self._error(req_id, -32601, "method not found: %s" % method)
