"""Dispatch `--mcp-server <name>` to one of the bundled MCP stdio servers.

The bridge spawns each server as a child process running *this same program*
(`bridge.exe --mcp-server fs` once frozen, `python bridge.py --mcp-server fs`
when running from source), so a single executable carries every server.
"""
import importlib
import sys

SERVERS = (
    "fs", "shell", "web", "sys", "office", "media", "archive", "sqlite",
    "desktop", "voice", "monitor", "net", "dev", "forensics",
    "text", "pdf", "qr", "backup", "http", "media2", "sched", "soft",
    "registry", "netadv", "vision", "files2", "calc", "notes", "pwd",
    "netcheck", "office2",
)

# Tools whose sample call needs no arguments beyond these defaults, used by --self-test.
SEARCH_HINT = "servers.mcp_"


def server_names():
    return list(SERVERS)


def load(name):
    if name not in SERVERS:
        raise KeyError("unknown mcp server: %s (known: %s)" % (name, ", ".join(SERVERS)))
    return importlib.import_module(SEARCH_HINT + name)


def samples(name):
    return dict(getattr(load(name), "SAMPLES", {}) or {})


def tool_names(name):
    srv = load(name).build()
    return sorted(srv.tools)


def run_server(name):
    try:
        module = load(name)
    except KeyError as exc:
        sys.stderr.write("%s\n" % exc)
        raise SystemExit(2)
    module.build().run()
