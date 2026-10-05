"""MCP server: plan -- goals that survive turns and restarts (3.0).

A thin client of the bridge's plan API; the store lives in the bridge so the chat
proxy can put the "next step" into every request.

Rule the persona must follow: a step is only 'done' when a real tool result was
recorded as evidence. Claims without evidence are not completion.
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
TIMEOUT_S = 20

srv = Server("plan")


def _call(path, payload=None, method="GET", timeout=TIMEOUT_S):
    url = API + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}"), ""
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        return {}, "HTTP %s: %s" % (exc.code, detail[:200])
    except Exception as exc:  # noqa: BLE001
        return {}, "%s: %s (is the bridge running? expected at %s)" % (type(exc).__name__, exc, API)


def _fmt(plan):
    if not plan:
        return "(no such plan)"
    lines = ["%s [%s] %s" % (plan.get("id"), plan.get("status"), plan.get("title") or ""),
             "goal: %s" % (plan.get("goal") or "-"),
             "progress: %s/%s done" % (plan.get("done"), plan.get("total"))]
    for index, step in enumerate(plan.get("steps") or [], 1):
        mark = {"todo": "[ ]", "doing": "[~]", "done": "[x]", "failed": "[!]", "skipped": "[-]"}.get(
            step.get("status"), "[ ]")
        lines.append("%s %d. %s%s%s" % (mark, index, step.get("text", ""),
                                        (" (tool=%s)" % step["tool"]) if step.get("tool") else "",
                                        (" [需要验证]" if step.get("verify") else "")))
        if step.get("evidence"):
            lines.append("      evidence: %s" % step["evidence"][:200].replace("\n", " "))
        if step.get("verify_evidence"):
            lines.append("      verified: %s" % step["verify_evidence"][:200].replace("\n", " "))
    return "\n".join(lines)


@srv.tool("plan_create",
          "Create a step-by-step plan for a multi-turn task. Steps are plain text, optionally with a "
          "tool to run for that step. The plan survives restarts; ask plan_next each turn.",
          {"type": "object", "properties": {
              "goal": {"type": "string"},
              "steps": {"type": "string", "default": "",
                        "description": 'JSON array of strings, or of {"text": "...", "tool": "...", "args": {}}'},
              "title": {"type": "string", "default": ""}},
           "required": ["goal"]})
def plan_create(goal, steps="", title=""):
    payload = {"goal": goal, "title": title}
    if steps:
        try:
            parsed = json.loads(steps) if isinstance(steps, str) else steps
        except json.JSONDecodeError as exc:
            return "steps must be JSON (array of strings, or of objects): %s" % exc
        payload["steps"] = parsed
    data, error = _call("/v2/plans", payload, method="POST")
    if error:
        return "could not create the plan: %s" % error
    return "plan created\n%s" % _fmt(data.get("plan"))


@srv.tool("plan_next",
          "The next unfinished step of a plan (index, text, tool and args) - do this one now.",
          {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
def plan_next(id):
    data, error = _call("/v2/plans/%s" % urllib.parse.quote(str(id)))
    if error:
        return "could not read the plan: %s" % error
    plan = data.get("plan") or {}
    index, step = None, None
    for position, item in enumerate(plan.get("steps") or [], 1):
        if item.get("status") in ("todo", "doing"):
            index, step = position, item
            break
    if step is None:
        return "no open step left in %s (status=%s)" % (id, plan.get("status"))
    return ("next step %d/%d: %s\nstatus=%s\ntool=%s\nargs=%s\n"
            "when it is really done, call plan_done with the tool output as evidence."
            % (index, plan.get("total"), step.get("text"), step.get("status"),
               step.get("tool") or "-", json.dumps(step.get("args") or {}, ensure_ascii=False)))


@srv.tool("plan_done",
          "Mark a plan step finished - pass the real tool output as evidence (no evidence, not done). "
          "If the step demanded verification, also pass verify_evidence from a fresh check.",
          {"type": "object", "properties": {"id": {"type": "string"},
                                            "step": {"type": "string", "description": "1-based index or part of the text"},
                                            "evidence": {"type": "string", "default": ""},
                                            "verify_evidence": {"type": "string", "default": "",
                                                                "description": "output of the independent check"},
                                            "status": {"type": "string", "default": "done",
                                                       "description": "done | failed | skipped | doing"},
                                            "error": {"type": "string", "default": ""}},
           "required": ["id", "step"]})
def plan_done(id, step, evidence="", status="done", error="", verify_evidence=""):
    payload = {"step": step, "evidence": evidence, "status": status, "error": error,
               "verify_evidence": verify_evidence}
    data, error_text = _call("/v2/plans/%s/step" % urllib.parse.quote(str(id)), payload, method="POST")
    if error_text:
        return "could not update the step: %s" % error_text
    return _fmt(data.get("plan"))


@srv.tool("plan_journal", "A readable log of a plan:每一步做到哪、证据是什么、验证过了没。",
          {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
def plan_journal(id):
    data, error = _call("/v2/plans/%s/journal" % urllib.parse.quote(str(id)))
    if error:
        return "could not read the plan journal: %s" % error
    return data.get("journal") or "(empty)"


@srv.tool("plan_add_step", "Append a step discovered while working (plans are allowed to grow).",
          {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"},
                                            "tool": {"type": "string", "default": ""},
                                            "args": {"type": "string", "default": ""}},
           "required": ["id", "text"]})
def plan_add_step(id, text, tool="", args=""):
    payload = {"text": text, "tool": tool}
    if args:
        try:
            payload["args"] = json.loads(args) if isinstance(args, str) else args
        except json.JSONDecodeError as exc:
            return "args must be JSON: %s" % exc
    data, error = _call("/v2/plans/%s/step/add" % urllib.parse.quote(str(id)), payload, method="POST")
    if error:
        return "could not add the step: %s" % error
    return _fmt(data.get("plan"))


@srv.tool("plan_status", "Read one plan in full (steps, status, evidence).",
          {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
def plan_status(id):
    data, error = _call("/v2/plans/%s" % urllib.parse.quote(str(id)))
    return ("could not read the plan: %s" % error) if error else _fmt(data.get("plan"))


@srv.tool("plan_list", "List plans (newest first), optionally filtered by status=active|done|cancelled.",
          {"type": "object", "properties": {"status": {"type": "string", "default": ""},
                                            "limit": {"type": "integer", "default": 10}}, "required": []})
def plan_list(status="", limit=10):
    query = "/v2/plans?limit=%d" % max(1, int(limit))
    if status:
        query += "&status=" + urllib.parse.quote(str(status))
    data, error = _call(query)
    if error:
        return "could not list plans: %s" % error
    plans = data.get("plans") or []
    if not plans:
        return "no plans"
    return "plans=%d\n%s" % (len(plans), "\n".join(
        "%s [%s] %s/%s %s" % (p.get("id"), p.get("status"), p.get("done"), p.get("total"),
                              p.get("title") or p.get("goal", "")[:60]) for p in plans))


@srv.tool("plan_cancel", "Cancel a plan (keeps the record, stops it being 'next').",
          {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
def plan_cancel(id):
    data, error = _call("/v2/plans/%s/cancel" % urllib.parse.quote(str(id)), {}, method="POST")
    return ("could not cancel: %s" % error) if error else _fmt(data.get("plan"))


SAMPLES = {"plan_list": {"limit": 3}}


def build():
    return srv


if __name__ == "__main__":
    srv.run()
