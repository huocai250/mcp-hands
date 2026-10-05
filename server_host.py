"""Dispatch `--mcp-server <name>` to one of the bundled MCP stdio servers.

2.0: the server list is **discovered** from `servers/mcp_*.py` instead of being a
hard-coded tuple, so dropping a new file in `servers/` is all it takes to add tools.
`servers/mcpserver.py` is the shared skeleton and is skipped, as is anything whose
module does not expose `build()`.

The bridge spawns each server as a child process running *this same program*
(`mcp-hands.exe --mcp-server fs` once frozen, `python bridge.py --mcp-server fs`
from source), so a single executable carries every server.
"""
import importlib
import os
import pkgutil
import sys

SEARCH_HINT = "servers.mcp_"
_PACKAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "servers")

# Kept only for a stable, readable `--tools` order; discovery does not depend on it.
_PREFERRED_ORDER = (
    "fs", "shell", "web", "sys", "office", "media", "archive", "sqlite",
    "desktop", "voice", "monitor", "net", "dev", "forensics",
    "text", "pdf", "qr", "backup", "http", "media2", "sched", "soft",
    "registry", "netadv", "vision", "files2", "calc", "notes", "pwd",
    "netcheck", "office2", "jobs", "memory",
)


def discover_names():
    """Names of every importable mcp_* server module found next to this file."""
    names = []
    try:
        for finder in pkgutil.iter_modules([_PACKAGE_DIR]):
            if finder.name.startswith("mcp_") and finder.name != "mcpserver":
                names.append(finder.name[len("mcp_"):])
    except OSError:
        pass
    if not names:  # frozen builds without package metadata: fall back to the files
        try:
            for entry in os.listdir(_PACKAGE_DIR):
                if entry.startswith("mcp_") and entry.endswith(".py") and entry != "mcpserver.py":
                    names.append(entry[len("mcp_"):-3])
        except OSError:
            pass
    return names


def _sorted_names(names):
    known = [name for name in _PREFERRED_ORDER if name in names]
    extra = sorted(name for name in names if name not in _PREFERRED_ORDER)
    return tuple(known + extra)


SERVERS = _sorted_names(discover_names())


def server_names():
    return list(SERVERS)


def refresh():
    """Re-scan servers/ (used by --tools and by tests that add a module at runtime)."""
    global SERVERS
    SERVERS = _sorted_names(discover_names())
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
