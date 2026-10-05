"""MCP server: audit -- what did it actually do (3.0).

Read-only view of the audit trail plus the active tool policy, so both the persona
and the user can check afterwards which tools ran, with what arguments, and whether
they succeeded.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

API = (os.environ.get("MCP_BRIDGE_API") or "http://127.0.0.1:8877").rstrip("/")

srv = Server("audit")


def _call(path, timeout=15):
    try:
        with urllib.request.urlopen(API + path, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}"), ""
    except urllib.error.HTTPError as exc:
        return {}, "HTTP %s: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:200])
    except Exception as exc:  # noqa: BLE001
        return {}, "%s: %s (is the bridge running? expected at %s)" % (type(exc).__name__, exc, API)


def _fmt(rows):
    if not rows:
        return "no calls recorded yet"
    lines = []
    for row in rows:
        args = row.get("args")
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        lines.append("%s [%s] %s%s %sms\n    %s" % (
            row.get("when"), "ok" if row.get("ok") else "FAILED", row.get("tool"),
            (" · %s" % row.get("profile")) if row.get("profile") else "",
            row.get("ms"), (args or "")[:200]))
        if row.get("error"):
            lines.append("    error: %s" % str(row["error"])[:200])
    return "\n".join(lines)


@srv.tool("audit_tail", "The most recent tool calls from the audit log: tool, arguments, result, duration.",
          {"type": "object", "properties": {"limit": {"type": "integer", "default": 20},
                                            "tool": {"type": "string", "default": ""},
                                            "only_errors": {"type": "boolean", "default": False}},
           "required": []})
def audit_tail(limit=20, tool="", only_errors=False):
    query = "/v2/audit?limit=%d" % max(1, int(limit))
    if tool:
        query += "&tool=" + urllib.parse.quote(str(tool))
    if only_errors:
        query += "&only_errors=1"
    data, error = _call(query)
    if error:
        return "could not read the audit log: %s" % error
    rows = data.get("calls") or []
    return "recent_calls=%d\n%s" % (len(rows), _fmt(rows))


@srv.tool("audit_stats", "Summary of recent activity: call counts, failures, busiest tools, per-profile split.",
          {"type": "object", "properties": {"hours": {"type": "number", "default": 24}}, "required": []})
def audit_stats(hours=24):
    data, error = _call("/v2/audit/stats?hours=%s" % hours)
    if error:
        return "could not read audit stats: %s" % error
    return json.dumps(data.get("stats") or {}, ensure_ascii=False, indent=2)


@srv.tool("policy_status", "Which tools the current policy allows or blocks (and whether it is enforcing).",
          {"type": "object", "properties": {}, "required": []})
def policy_status():
    data, error = _call("/v2/policy")
    if error:
        return "could not read the policy: %s" % error
    policy = data.get("policy") or {}
    lines = ["mode=%s" % policy.get("mode"),
             "deny=%s" % (", ".join(policy.get("deny") or []) or "(none)"),
             "allow=%s" % (", ".join(policy.get("allow") or []) or "(all)"),
             "deny_paths=%s" % (", ".join(policy.get("deny_paths") or []) or "(none)"),
             "max_calls_per_minute=%s" % policy.get("max_calls_per_minute")]
    return "\n".join(lines)


SAMPLES = {"policy_status": {}, "audit_stats": {"hours": 1}, "audit_tail": {"limit": 5}}


def build():
    return srv


if __name__ == "__main__":
    srv.run()
