"""Minimal MCP stdio client: spawn a server, handshake, list and call tools.

Protocol: JSON-RPC 2.0, newline-delimited JSON on stdin/stdout
(MCP "stdio" transport). No third-party dependencies.
"""
import itertools
import json
import os
import subprocess
import threading
import time


class McpError(RuntimeError):
    pass


class McpServer:
    def __init__(self, name, argv, env=None, cwd=None, timeout=60):
        self.name = name
        self.argv = list(argv)
        self.env = dict(env or {})
        self.cwd = cwd
        self.timeout = float(timeout)
        self.tools = []
        self.info = {}
        self.log = []
        self._ids = itertools.count(1)
        self._pending = {}
        self._lock = threading.Lock()
        self._proc = None
        self._dead = None

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        env = os.environ.copy()
        env.update({k: os.path.expandvars(str(v)) for k, v in self.env.items()})
        env.setdefault("PYTHONIOENCODING", "utf-8")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        argv = list(self.argv)
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
            cwd=self.cwd, creationflags=creationflags,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        result = self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-hands", "version": "1.0"},
        })
        self.info = (result or {}).get("serverInfo", {})
        self.notify("notifications/initialized", {})
        self.tools = (self.request("tools/list", {}) or {}).get("tools", [])
        return self

    def stop(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

    # ------------------------------------------------------------------ plumbing
    def _pump_stdout(self):
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self.log.append("non-json from %s: %s" % (self.name, line[:200]))
                continue
            req_id = msg.get("id")
            if req_id is None:
                self.log.append("notification from %s: %s" % (self.name, msg.get("method")))
                continue
            with self._lock:
                slot = self._pending.pop(req_id, None)
            if slot:
                slot[1].update(msg)
                slot[0].set()
        self._dead = "server %s closed stdout" % self.name
        with self._lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for ev, box in slots:
            box["error"] = {"message": self._dead}
            ev.set()

    def _pump_stderr(self):
        for line in self._proc.stderr:
            line = line.rstrip()
            if line:
                self.log.append("[%s stderr] %s" % (self.name, line[:400]))

    def _send(self, obj):
        if self._proc is None or self._proc.poll() is not None:
            raise McpError(self._dead or ("server %s is not running" % self.name))
        self._proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def request(self, method, params, timeout=None):
        req_id = next(self._ids)
        ev, box = threading.Event(), {}
        with self._lock:
            self._pending[req_id] = (ev, box)
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        if not ev.wait(timeout or self.timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise McpError("%s: timeout waiting for %s" % (self.name, method))
        if "error" in box:
            raise McpError("%s: %s" % (self.name, box["error"].get("message", box["error"])))
        return box.get("result")

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def call_tool(self, tool, arguments, timeout=None):
        result = self.request("tools/call", {"name": tool, "arguments": arguments or {}}, timeout)
        parts = []
        for item in (result or {}).get("content", []):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "image":
                parts.append("[image:%s %d bytes base64]" % (item.get("mimeType"), len(item.get("data", ""))))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(parts), bool((result or {}).get("isError"))


class ToolHub:
    """Owns every MCP server and exposes a flat, model-friendly tool namespace."""

    def __init__(self, server_configs, cwd=None, log=print, entry=None, extra_env=None,
                 guard=None, audit=None):
        """entry: path to bridge.py so children can re-enter as `--mcp-server NAME`;
        None when frozen (sys.executable is the bundle itself).
        extra_env: variables handed to every child (e.g. MCP_VISION_* from the config).
        guard(name, args) -> (allowed, reason): policy check before every call.
        audit(record): called after every call with tool/args/ok/error/ms/chars."""
        self.log = log
        self.servers = []
        self.aliases = {}
        self.specs = []
        self.entry = entry
        self.extra_env = {k: str(v) for k, v in (extra_env or {}).items() if v not in (None, "")}
        self.guard = guard
        self.audit = audit
        self._start_all(server_configs, cwd)

    def _argv_for(self, raw, cwd):
        import sys
        if raw.get("command"):
            argv = [raw["command"]] + [str(a) for a in raw.get("args", [])]
            if cwd and argv[1:2] and not os.path.isabs(argv[1]):
                argv[1] = os.path.join(cwd, argv[1])
            return argv
        base = [sys.executable]
        if self.entry:
            base.append(self.entry)
        return base + ["--mcp-server", raw["name"]]

    def _start_all(self, server_configs, cwd):
        pending = [raw for raw in server_configs if raw.get("enabled", True) is not False]
        if not pending:
            return
        # Start servers concurrently: each is a process handshake, so serial startup
        # would cost seconds once a dozen servers are configured.
        from concurrent.futures import ThreadPoolExecutor

        def boot(raw):
            env = dict(raw.get("env", {}))
            env.setdefault("MCP_SAMPLES_DIR", cwd or os.getcwd())
            for key, value in self.extra_env.items():
                env.setdefault(key, value)
            cfg = {"name": raw["name"], "argv": self._argv_for(raw, cwd), "env": env,
                   "cwd": raw.get("cwd", cwd), "timeout": raw.get("timeout", 60)}
            return raw, McpServer(**cfg).start()

        with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
            results = list(pool.map(boot, pending))

        for raw, started in results:
            name = raw["name"]
            self.servers.append(started)
            for tool in started.tools:
                exposed = _sanitize("%s_%s" % (name, tool["name"]))
                self.specs.append({
                    "name": exposed,
                    "description": "[%s] %s" % (name, tool.get("description", "")),
                    "inputSchema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                })
                self.aliases[exposed] = (started, tool["name"])
                self.aliases["%s.%s" % (name, tool["name"])] = (started, tool["name"])
                self.aliases["%s__%s" % (name, tool["name"])] = (started, tool["name"])
                self.aliases.setdefault(tool["name"], (started, tool["name"]))
            self.log("mcp server '%s' ready with %d tools" % (name, len(started.tools)))

    def resolve(self, name):
        key = str(name).strip()
        if key in self.aliases:
            return self.aliases[key]
        norm = key.replace(".", "_").replace(":", "_").replace("__", "_")
        for alias, target in self.aliases.items():
            if alias.replace(".", "_").replace("__", "_") == norm:
                return target
        return None

    def spec_for(self, name):
        """The exposed schema of a tool, or None."""
        return next((s for s in self.specs if s["name"] == name), None)

    def call(self, name, arguments):
        target = self.resolve(name)
        if not target:
            return "unknown tool '%s'. available: %s" % (name, ", ".join(s["name"] for s in self.specs)), True
        # 3.0: one choke point for policy decisions and the audit trail.
        if self.guard is not None:
            allowed, reason = self.guard(name, arguments)
            if not allowed:
                self._audit(name, arguments, False, "blocked: %s" % reason, 0.0, 0)
                return "blocked by policy: %s" % reason, True
        server, tool = target
        # Models sometimes invent extra fields; a stray keyword would otherwise turn a
        # perfectly good call into TypeError. Drop anything the schema does not declare.
        spec = self.spec_for(name)
        props = ((spec or {}).get("inputSchema") or {}).get("properties") or {}
        if props and isinstance(arguments, dict):
            dropped = sorted(key for key in arguments if key not in props)
            if dropped:
                arguments = {k: v for k, v in arguments.items() if k in props}
                self.log("tool '%s': ignored undeclared argument(s) %s" % (name, ", ".join(dropped)))
        started = time.perf_counter()
        try:
            text, is_error = server.call_tool(tool, arguments)
        except McpError as exc:
            self._audit(name, arguments, False, str(exc), time.perf_counter() - started, 0)
            return str(exc), True
        except Exception as exc:  # noqa: BLE001 - a broken server must not kill the caller
            self._audit(name, arguments, False, str(exc), time.perf_counter() - started, 0)
            return "%s: %s" % (type(exc).__name__, exc), True
        self._audit(name, arguments, not is_error, "" if not is_error else text, time.perf_counter() - started,
                    len(text or ""))
        return text, is_error

    def _audit(self, name, arguments, ok, error, seconds, chars):
        if self.audit is None:
            return
        try:
            self.audit({"tool": name, "args": arguments, "ok": ok, "error": error,
                        "ms": int(seconds * 1000), "chars": int(chars)})
        except Exception:  # noqa: BLE001 - auditing must never break a call
            pass

    def stop(self):
        for server in self.servers:
            server.stop()


def _sanitize(name):
    out = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in name)
    return out[:64]
