"""MCP server: jobs -- background work that keeps running after the chat reply ends.

This server is a thin client of the bridge's job API (the engine lives in the bridge
process, because it owns the tool hub). It lets the persona hand off long chains -
scroll a feed, batch-convert files, watch something - and collect the result later.

The bridge address comes from MCP_BRIDGE_API (set by the bridge when it spawns
servers); job results are also delivered into the next chat turn automatically.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

API = (os.environ.get("MCP_BRIDGE_API") or "http://127.0.0.1:8877").rstrip("/")
TIMEOUT_S = 20

srv = Server("jobs")


def _call(path, payload=None, method="GET", timeout=TIMEOUT_S):
    """Talk to the bridge; never raise, always return (data, error_text)."""
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


def _fmt_job(job):
    if not job:
        return "(no such job)"
    lines = ["%s [%s] %s" % (job.get("id"), job.get("status"), job.get("title") or job.get("kind")),
             "progress %s/%s" % (job.get("progress"), job.get("total"))]
    if job.get("error"):
        lines.append("error: %s" % job["error"])
    if job.get("result"):
        lines.append("result:\n%s" % job["result"][:3000])
    return "\n".join(lines)


@srv.tool("job_start",
          "Start a background job that keeps running after you answer. Either one tool call, "
          "or a plan of several calls run in order (steps). Use it for long chains: scroll a feed, "
          "watch a folder, convert many files. The result is handed back to you on a later turn.",
          {"type": "object",
           "properties": {"tool": {"type": "string", "default": "", "description": "single tool to call"},
                          "args": {"type": "string", "default": "", "description": "JSON object for that tool"},
                          "steps": {"type": "string", "default": "",
                                    "description": 'JSON array like [{"tool": "x", "args": {...}}, ...]'},
                          "title": {"type": "string", "default": ""},
                          "kind": {"type": "string", "default": "task"},
                          "notify": {"type": "boolean", "default": True,
                                     "description": "pop a Windows notification when it finishes"},
                          "wait": {"type": "boolean", "default": False,
                                   "description": "block until finished (short jobs only)"},
                          "wait_seconds": {"type": "integer", "default": 30}},
           "required": []})
def job_start(tool="", args="", steps="", title="", kind="task", notify=True, wait=False, wait_seconds=30):
    payload = {"title": title, "kind": kind, "notify": bool(notify)}
    if steps:
        try:
            payload["steps"] = json.loads(steps) if isinstance(steps, str) else steps
        except json.JSONDecodeError as exc:
            return "steps must be JSON: %s" % exc
    elif tool:
        payload["tool"] = tool
        if args:
            try:
                payload["args"] = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError as exc:
                return "args must be a JSON object: %s" % exc
    else:
        return "give either tool= or steps="
    data, error = _call("/v2/jobs", payload, method="POST", timeout=30)
    if error:
        return "could not queue the job: %s" % error
    job = data.get("job") or {}
    if wait and job.get("id"):
        waited, error2 = _call("/v2/jobs/%s?wait=%d" % (job["id"], max(1, int(wait_seconds))), timeout=int(wait_seconds) + 15)
        return _fmt_job(waited.get("job")) if not error2 else _fmt_job(job)
    return ("queued %s (%d step(s)). It runs in the background; I will hand you the result on a "
            "later turn, and the user gets a notification when it finishes.\n%s"
            % (job.get("id"), len(job.get("plan") or []), _fmt_job(job)))


@srv.tool("job_list", "List background jobs (newest first). status filters: pending,running,done,failed,cancelled.",
          {"type": "object", "properties": {"status": {"type": "string", "default": ""},
                                            "limit": {"type": "integer", "default": 10}}, "required": []})
def job_list(status="", limit=10):
    query = "/v2/jobs?limit=%d" % max(1, int(limit))
    if status:
        query += "&status=" + urllib.parse.quote(str(status))
    data, error = _call(query)
    if error:
        return "could not list jobs: %s" % error
    jobs = data.get("jobs") or []
    if not jobs:
        return "no jobs"
    return "jobs=%d\n%s" % (len(jobs), "\n".join(
        "%s [%s] %s/%s %s" % (j.get("id"), j.get("status"), j.get("progress"), j.get("total"),
                              j.get("title") or j.get("kind")) for j in jobs))


@srv.tool("job_status", "Status and result of one background job (result appears once it finishes).",
          {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
def job_status(id):
    data, error = _call("/v2/jobs/%s" % urllib.parse.quote(str(id)))
    return ("could not read the job: %s" % error) if error else _fmt_job(data.get("job"))


@srv.tool("job_wait", "Wait up to N seconds for a job to finish, then report it (short jobs only).",
          {"type": "object", "properties": {"id": {"type": "string"}, "seconds": {"type": "integer", "default": 60}},
           "required": ["id"]})
def job_wait(id, seconds=60):
    data, error = _call("/v2/jobs/%s?wait=%d" % (urllib.parse.quote(str(id)), max(1, int(seconds))),
                        timeout=int(seconds) + 15)
    return ("could not wait for the job: %s" % error) if error else _fmt_job(data.get("job"))


@srv.tool("job_cancel", "Cancel a background job (a running job stops before its next step).",
          {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]})
def job_cancel(id):
    data, error = _call("/v2/jobs/%s/cancel" % urllib.parse.quote(str(id)), {}, method="POST")
    return ("could not cancel: %s" % error) if error else _fmt_job(data.get("job"))


@srv.tool("job_stats", "How many jobs are queued, running, done or failed.", {"type": "object", "properties": {}, "required": []})
def job_stats():
    data, error = _call("/v2/jobs/stats")
    return ("could not read job stats: %s" % error) if error else json.dumps(data.get("stats") or {}, ensure_ascii=False)


SAMPLES = {
    "job_stats": {},
    "job_list": {"limit": 3},
}



def build():
    return srv


if __name__ == "__main__":
    srv.run()
