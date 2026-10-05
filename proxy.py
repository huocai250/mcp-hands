#!/usr/bin/env python3
"""app-native tool proxy — point the phone app's model provider here.

The app speaks plain OpenAI to this service; we attach the same MCP tool catalogue
as **native** OpenAI `tools`, then run the tool loop against a real upstream
(DeepSeek / SiliconFlow / any OpenAI-compatible base) using the API key the app
sends us. The app sees one ordinary assistant reply — no `TOOL:` blocks in the chat.

Requests for models that are not in `tool_models` are relayed untouched
(so a vision/audio helper model keeps working).
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FROZEN = bool(getattr(sys, "frozen", False))
HERE = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else HERE
sys.path.insert(0, HERE)

import bridge  # noqa: E402

LOG_LOCK = threading.Lock()

# The key the app last sent us. The GUI's "test vision" button reuses it, because a
# direct hub call bypasses this proxy and therefore has no key of its own.
_LAST_KEY = {"token": "", "at": 0.0}


def remember_key(auth):
    token = (auth or "").replace("Bearer ", "").strip()
    if token and token.lower() != "aiyu":
        _LAST_KEY["token"] = token
        _LAST_KEY["at"] = time.time()
    return token


def last_key():
    """(token, age_seconds) of the most recent app request, or ("", None)."""
    if not _LAST_KEY["token"]:
        return "", None
    return _LAST_KEY["token"], time.time() - _LAST_KEY["at"]


def log(msg):
    bridge.log(msg)


def proxy_cfg():
    return bridge.CFG.get("proxy") or {}


def upstream_base():
    return (proxy_cfg().get("upstream_base") or "https://api.deepseek.com/v1").rstrip("/")


RETRY_STATUS = (408, 409, 425, 429, 500, 502, 503, 504)


def upstream_post(path, payload, auth, timeout=240, attempts=3, base=None, key=None):
    """POST to the upstream, retrying transient failures with a short backoff."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = (base or upstream_base()).rstrip("/") + path
    headers = {"Content-Type": "application/json",
               "Authorization": auth or ("Bearer " + (key or proxy_cfg().get("upstream_api_key", "")))}
    last = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in RETRY_STATUS or attempt >= attempts:
                raise
            log("  upstream HTTP %s - retry %d/%d in %.1fs" % (exc.code, attempt, attempts, 0.6 * attempt))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt >= attempts:
                raise
            log("  upstream %s - retry %d/%d in %.1fs" % (type(exc).__name__, attempt, attempts, 0.6 * attempt))
        time.sleep(0.6 * attempt)
    raise last if last else RuntimeError("upstream request failed")


def upstream_open(path, payload, auth, timeout=240):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(upstream_base() + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": auth or ("Bearer " + proxy_cfg().get("upstream_api_key", ""))})
    return urllib.request.urlopen(req, timeout=timeout)


TOOL_HINT = """你在操作一台真实的 Windows 电脑，下面这些工具会立即执行并返回真实结果。工作纪律（每一条都来自用户的明确要求，必须遵守）：

【0 调用方式】要动手就直接发起工具调用；不要用文字描述调用、不要写 JSON 或代码块假装调用，也不要写 <||DSML||...> 这类标记（那是坏掉的调用，用户会看到乱码）。万一原生调用不可用，只能用这种行格式作后备：TOOL: 工具名 / 参数名=值 / END。一次请求里你可以连着调度多个动作（最多 %(rounds)s 轮 / %(seconds)s 秒），做够了再汇报。

【1 时间】要报时间、时长、"等了几分钟"，先用 sys_now 取真实时间再开口，不许凭感觉估。

【2 报错】工具返回 ERROR 时，第一反应是换一种方式再试（换工具、换参数、换路径、换端口，必要时并行试两个），最多试 2-3 次；只有全失败了才向用户解释原因并给出替代方案，不要一上来就干解释。

【3 回执】回复里不要照抄机器噪音（exit=0、bytes=、ms=、truncated 之类），用一两句人话总结关键结果就好；机器回执系统会自动附在你的话后面，不需要你复述。

【4 称呼】无论对话还是写 notes/memory/待办，提到用户一律用「你」，不要写成「她」「他」「用户」，同一个人不要两头叫。

【5 待办与下一步】多步任务先用 notes_todo_add 记下待办，做完一步用 notes_todo_done 勾掉；一件事做完要主动给出下一步或问一句，不要原地停住等派活。

【5b 节奏】用户等的是你的反应，不是状态条：一小段动作做完（大约 6-8 步、或一分钟左右）就先说话——汇报看到了什么、给一句评价、说下一步要干什么，然后再继续。别一口气跑到轮次上限才开口，那样对面会觉得你在装死。这一轮最多 %(rounds)s 轮 / %(seconds)s 秒，到点我会替你收尾。

【6 记忆】用户提到「昨天」「上次」「之前」「我说过」之类，先 notes_memory_recent 或 notes_note_search 查一遍再回答，不要凭印象猜。

【7 主动开口】你不能主动在手机 App 里发消息（App 没有推送通道），也绝不许谎称你发过。要主动找用户就走电脑通道：立刻提醒用 sys_toast 或 voice_toast_speak（后者会念出来）；定时提醒用 remind_in(minutes=数字, text="要说的话") 或 remind_at(time="18:30", text="...")；查已排的提醒用 remind_list。

【9 给用户看图】用户要看画面/截图时，直接调 http_share_screenshot（它会截屏、放进已启动的文件服务并返回手机可打开的链接），然后把返回里的 markdown 那一行原样发出来——多数客户端会直接渲染成图片，没有也能点链接。不要自己手搓文件服务器、也不要只丢裸链接。

【8 完成度】只有工具真的返回了结果才算完成；没返回就不许说「已完成」，也不许凭猜测描述屏幕内容。"""

# Sent only on the last round, when tools are withheld so the model must speak in prose.
FINAL_NUDGE = {"role": "user", "content": "（系统：本轮工具调用额度已用完，请直接用一两句中文总结你刚才做了什么、"
                                         "画面/页面现在是什么状态，不要再输出任何工具调用、标记或标记语言。）"}

_VISION_CACHE = {"key": "", "at": 0.0, "ok": False, "detail": ""}


def vision_status_line(auth):
    """Tell the model whether it can actually see, so it never bluffs about the screen."""
    cfg = proxy_cfg().get("vision") or bridge.CFG.get("vision") or {}
    token = (auth or "").replace("Bearer ", "").strip()
    key = cfg.get("api_key") or (token if cfg.get("inherit_upstream_key", True) and token.lower() != "aiyu" else "")
    if not key:
        return ("视觉状态：**未接通**（没有可用的视觉 key）。你现在看不见屏幕，vision_* 工具会报错——"
                "不要编造画面内容，直接告诉用户「我现在看不见」。")
    ttl = float(proxy_cfg().get("vision_check_seconds", 600) or 0)
    fresh = ttl > 0 and _VISION_CACHE["key"] == key and (time.time() - _VISION_CACHE["at"]) < ttl
    if not fresh:
        try:
            out, err = bridge.start_hub().call("vision_vision_probe", {"api_key": key})
        except Exception as exc:  # noqa: BLE001
            out, err = "%s: %s" % (type(exc).__name__, exc), True
        _VISION_CACHE.update({"key": key, "at": time.time(), "ok": not err,
                              "detail": ((out or "").splitlines() or [""])[-1][:90]})
        log("  vision check: %s %s" % ("ok" if not err else "FAILED", _VISION_CACHE["detail"]))
    if _VISION_CACHE["ok"]:
        return ("视觉状态：已接通（%s）。要看屏幕就直接调 vision_see_screen / vision_read_screen_text。"
                % (cfg.get("model") or "vision"))
    return ("视觉状态：**不通**（%s）。vision_* 会失败——不要编造画面内容，直接告诉用户「我现在看不见」。"
            % _VISION_CACHE["detail"])


_RECEIPT_NOISE = re.compile(
    r"^(exit=\d+|ok\b[\s\S]*|\[[^\]]*\]|.*\b(?:bytes|ms|chars|truncated)=\S+.*)$", re.I)
_RECEIPT_DIGITS = re.compile(r"^[\s\d\W_]+$")


def _digest(output, limit=140):
    """Pick the first line that actually says something, not machine noise."""
    text = output or ""
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or _RECEIPT_NOISE.match(candidate) or _RECEIPT_DIGITS.match(candidate):
            continue
        candidate = re.sub(r"\b(?:exit=\d+|bytes=\d+|ms=\d+|chars=\d+|truncated=\w+)\b", "", candidate)
        candidate = re.sub(r"\s{2,}", " ", candidate).strip(" |,-")
        if candidate:
            return candidate[:limit]
    return ((text.splitlines() or [""])[0])[:limit]


# Short, human progress lines so the phone shows something while slow tools run.
# Curated on purpose: exposing a raw tool name reads like a machine log.
PROGRESS_MAP = (
    ("sys_now", "正在看时间"), ("sys_screenshot", "正在截图"), ("screenshot", "正在截图"),
    ("read_screen_region", "正在截图"), ("list_windows", "正在找窗口"),
    ("focus_window", "正在切窗口"), ("mouse_", "正在动手"), ("press_keys", "正在按键"),
    ("vision_read_screen", "正在读屏幕上的字"), ("vision_see_screen", "正在看屏幕"),
    ("vision_sight", "正在确认眼睛"), ("vision_share", "正在整理图片"),
    ("vision_", "正在看图"), ("http_", "正在整理图片"), ("desktop_", "正在操作桌面"),
    ("shell_", "正在执行命令"), ("fs_", "正在读写文件"), ("web_", "正在上网查"),
    ("office", "正在处理文档"), ("media", "正在处理图片"), ("sched_", "正在排提醒"),
    ("remind_", "正在排提醒"), ("notes_", "正在记下来"), ("pwd_", "正在算"),
    ("net", "正在检查网络"), ("registry", "正在读注册表"), ("sqlite", "正在查数据库"),
    ("voice", "正在出声"), ("monitor", "正在看系统状态"),
)


def progress_text(tool_name, error=False):
    for prefix, text in PROGRESS_MAP:
        if str(tool_name).startswith(prefix) or prefix in str(tool_name):
            return "（%s%s…）\n" % (text, "，卡住了换个办法" if error else "")
    return "（正在动手%s…）\n" % ("，卡住了换个办法" if error else "")


def step_report(steps, mode="brief", limit=8):
    """The honesty block: every real tool result of this turn, laid out for the user."""
    if not steps or mode == "off":
        return ""
    lines = ["", "— 这轮电脑侧的真实回执 —"]
    for index, step in enumerate(steps[:limit], 1):
        output = step.get("output") or ""
        if mode == "full":
            text = output[:400].replace("\n", " | ")
        else:
            text = _digest(output)
        lines.append("%d. %s %s：%s" % (index, step["tool"], "失败" if step.get("error") else "成功", text))
    if len(steps) > limit:
        lines.append("…（本轮共 %d 步）" % len(steps))
    return "\n".join(lines)


def finalize(message, steps=None):
    """Last line of defence: protocol markup must never reach the phone screen."""
    if not isinstance(message, dict):
        return {"role": "assistant", "content": ""}
    content = message.get("content")
    if isinstance(content, str) and content:
        message = dict(message, content=bridge.strip_dsml(content))
    text = (message.get("content") or "").strip()
    report = step_report(steps or [], proxy_cfg().get("step_report", "brief"))
    if not text:
        text = "（这轮我做到上限了，先停一下；接着来的话说一声。）" if steps else ""
    message = dict(message, content=(text + report).strip())
    message.pop("tool_calls", None)
    return message

# The app's own conversation still contains the bridge era text protocol (injected
# instructions, the model's TOOL: blocks, TOOL_RESULT turns). Leaving them in the
# prompt makes the model imitate that style instead of using native tool calls.
_PROTOCOL_HEADS = ("TOOL_PROTOCOL_V1", "[外部工具协议", "[运行环境]", "工具清单（每行")
_TOOL_BLOCK = re.compile(r"(?ms)^[ \t]*TOOL[ \t]*[::][ \t]*[A-Za-z0-9_.\-]+[ \t]*$.*?^[ \t]*END[ \t]*$")
_JSON_BLOCK = re.compile(r"```(?:tool|json|tool_call)\s*\n.*?\n?```", re.S | re.I)
_USER_MARKERS = ("用户消息:", "[用户消息]")


def scrub(text):
    """Remove text-protocol artefacts from one message, keeping the real user text."""
    if not isinstance(text, str):
        return text
    for marker in _USER_MARKERS:
        if marker in text:
            text = text.split(marker, 1)[1]
            break
    else:
        head = text[:200]
        if any(mark in head for mark in _PROTOCOL_HEADS):
            return ""
    text = _TOOL_BLOCK.sub("", text)
    text = _JSON_BLOCK.sub("", text)
    text = bridge.strip_dsml(text)
    lines = [line for line in text.splitlines() if not line.strip().startswith("TOOL_RESULT")]
    return "\n".join(lines).strip()


def sanitize(messages):
    """Drop protocol contamination so the model sees a normal conversation."""
    cleaned = []
    for message in messages or []:
        content = message.get("content")
        if isinstance(content, str):
            text = scrub(content)
            if not text:
                continue  # whole message was protocol noise
            message = dict(message, content=text)
        cleaned.append(message)
    return cleaned


_SECRET_HINTS = ("api_key", "apikey", "authorization", "token", "password", "secret")


def mask_args(args):
    """Never let a secret reach the log file (the log is plain text on disk)."""
    if not isinstance(args, dict):
        return args
    safe = {}
    for key, value in args.items():
        flagged = any(hint in str(key).lower() for hint in _SECRET_HINTS)
        if flagged and isinstance(value, str) and value:
            safe[key] = (value[:6] + "…" + value[-4:]) if len(value) > 12 else "***"
        else:
            safe[key] = value
    return safe


def drain_job_results(limit=3):
    """Finished background jobs the persona has not reported yet.

    The job engine lives in the bridge, so this is one small local HTTP call; marking
    them delivered here means each result is reported exactly once.
    """
    port = (bridge.CFG.get("listen") or {}).get("port", 8877)
    url = "http://127.0.0.1:%s/v2/jobs?pending=1&mark=1&limit=%d" % (port, max(1, int(limit)))
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            return (json.loads(resp.read().decode("utf-8", "replace") or "{}").get("jobs") or [])
    except Exception:  # noqa: BLE001 - the bridge may be busy; results simply wait
        return []


def job_report_block(jobs):
    """Human-readable 'what your background jobs did' block for the system hint."""
    if not jobs:
        return ""
    lines = ["【后台任务回执】你之前派出去的任务已经跑完，请在回复里主动向用户交代（不要说不知道、也不要否认做过）："]
    for job in jobs:
        head = "· %s：%s" % (job.get("title") or job.get("kind") or "任务",
                            "完成" if job.get("status") == "done" else job.get("status"))
        body = (job.get("result") or job.get("error") or "").strip()
        lines.append("%s\n%s" % (head, body[:1200] if body else "(没有输出)"))
    return "\n".join(lines)


def auto_index_jobs(hub, jobs):
    """Keep a searchable memory of what the PC did while the user was away."""
    if not (bridge.CFG.get("memory") or {}).get("auto_index", True) or not jobs:
        return
    for job in jobs:
        summary = "%s [%s] %s" % (job.get("title") or job.get("kind"), job.get("status"),
                                  (job.get("result") or job.get("error") or "")[:600])
        try:
            hub.call("memory_memory_add", {"text": summary, "tags": "后台任务",
                                           "source": "job:%s" % job.get("id")})
        except Exception:  # noqa: BLE001
            pass


def note_usage(usage):
    """Accumulate token counters for /metrics and log them once per request."""
    if not usage:
        return
    metrics = bridge.METRICS
    metrics["prompt_tokens_total"] = metrics.get("prompt_tokens_total", 0) + int(usage.get("prompt_tokens") or 0)
    metrics["completion_tokens_total"] = metrics.get("completion_tokens_total", 0) + int(usage.get("completion_tokens") or 0)


def resolve_profile(body=None, auth="", header=""):
    """3.0 multi-persona: pick a profile by header, model name or key.

    Returns (name, config). With no profiles configured this is ("", {}) and nothing
    about the old behaviour changes.
    """
    profiles = bridge.CFG.get("profiles") or {}
    if not profiles:
        return "", {}
    wanted = str(header or "").strip()
    if wanted and wanted in profiles:
        return wanted, dict(profiles[wanted] or {})
    model = str((body or {}).get("model") or "")
    token = (auth or "").replace("Bearer ", "").strip()
    for name, cfg in profiles.items():
        cfg = cfg or {}
        if cfg.get("model") and cfg["model"] == model:
            return name, dict(cfg)
        if cfg.get("api_key") and token and cfg["api_key"] == token:
            return name, dict(cfg)
    default = bridge.CFG.get("profile_default")
    if isinstance(default, str) and default in profiles:
        return default, dict(profiles[default] or {})
    if default in profiles:
        return default, dict(profiles[default] or {})
    name = next(iter(profiles))
    return name, dict(profiles[name] or {})


def profile_allows(config, tool_name):
    """Per-profile tool rules: allow list, then deny list, both glob-ish."""
    import fnmatch
    allow = [str(p) for p in (config.get("tools") or config.get("allow") or [])]
    deny = [str(p) for p in (config.get("deny") or [])]
    if allow and not any(fnmatch.fnmatch(tool_name, pattern) for pattern in allow):
        return False
    return not any(fnmatch.fnmatch(tool_name, pattern) for pattern in deny)


def plan_hint(limit=2):
    """The next open step of the active plans, so long tasks survive across turns."""
    port = (bridge.CFG.get("listen") or {}).get("port", 8877)
    try:
        with urllib.request.urlopen("http://127.0.0.1:%s/v2/plans?status=active&limit=5" % port, timeout=5) as resp:
            plans = (json.loads(resp.read().decode("utf-8", "replace") or "{}").get("plans") or [])
    except Exception:  # noqa: BLE001
        return ""
    lines = []
    for plan in plans[:limit]:
        step = next((s for s in plan.get("steps") or [] if s.get("status") in ("todo", "doing")), None)
        if not step:
            continue
        lines.append("· %s（%s，%s/%s）下一步：%s%s" % (
            plan.get("id"), plan.get("title") or plan.get("goal", "")[:40],
            plan.get("done"), plan.get("total"), step.get("text"),
            ("（工具 %s）" % step["tool"]) if step.get("tool") else ""))
    if not lines:
        return ""
    return ("【计划】你手上还有没做完的计划，请接着做下一步（做完调用 plan_done 并把真实工具输出作为 evidence，"
            "没有证据不算完成；计划全做完再向用户汇总）：\n" + "\n".join(lines))


def native_tools(hub):
    tools = []
    for spec in hub.specs:
        tools.append({"type": "function", "function": {
            "name": spec["name"], "description": spec["description"],
            "parameters": spec.get("inputSchema") or {"type": "object", "properties": {}}}})
    return tools


def vision_args(name, args, auth):
    """Drop undeclared arguments, then hand the chat key + vision config to vision tools.

    Path B relays the app's own key, so `see_screen` / `see_image` work without the
    user pasting a key anywhere. Only parameters the tool actually declares are set.
    """
    hub = bridge.start_hub()
    spec = next((s for s in hub.specs if s["name"] == name), None)
    props = ((spec or {}).get("inputSchema") or {}).get("properties") or {}
    if props and isinstance(args, dict):
        dropped = [key for key in list(args) if key not in props]
        for key in dropped:
            args.pop(key, None)
        if dropped:
            log("  dropped undeclared argument(s) %s for %s" % (", ".join(dropped), name))
    if not str(name).startswith("vision_"):
        return args
    cfg = proxy_cfg().get("vision") or bridge.CFG.get("vision") or {}
    token = (auth or "").replace("Bearer ", "").strip()
    if "api_key" in props and not args.get("api_key"):
        if cfg.get("inherit_upstream_key", True) and token and token.lower() != "aiyu":
            args["api_key"] = token
    for key in ("model", "base_url", "detail"):
        if key in props and cfg.get(key) and not args.get(key):
            args[key] = cfg[key]
    return args


def run_tool_loop(body, auth, on_event=None, profile_name="", profile_cfg=None):
    """Returns (message, steps, usage). message is an OpenAI assistant message dict.

    on_event(tool_name, is_error) is called after every tool execution so the caller
    can push a live progress line to the client instead of staying silent for minutes.
    profile_name/profile_cfg (3.0) narrow the tool list and pick the upstream per identity.
    """
    hub = bridge.start_hub()
    profile_cfg = dict(profile_cfg or {})
    model = profile_cfg.get("model") or body.get("model") or "deepseek-chat"
    raw_messages = list(body.get("messages") or [])
    messages = sanitize(raw_messages)
    if len(messages) != len(raw_messages):
        log("  scrubbed %d protocol-contaminated message(s) from history" % (len(raw_messages) - len(messages)))
    if proxy_cfg().get("inject_tool_hint", True):
        cfg_now = proxy_cfg()
        hint = TOOL_HINT % {"rounds": int(cfg_now.get("max_tool_rounds", 12) or 12),
                            "seconds": int(float(cfg_now.get("max_seconds", 120) or 120))}
        hint += "\n" + vision_status_line(auth)
        hint += ("\n【8b 后台任务】长活（翻一整轮推荐流、批量处理一堆文件、盯着某个目录）"
                 "用 job_start 丢给电脑自己跑，别占着这一轮等：派出去之后先回用户一句，"
                 "任务跑完的结果会在你下一轮开口时自动交给你。job_list 看队列，job_cancel 取消。")
        finished_jobs = drain_job_results()
        if finished_jobs:
            hint += "\n\n" + job_report_block(finished_jobs)
            auto_index_jobs(hub, finished_jobs)
            log("  delivered %d background job result(s) into this turn" % len(finished_jobs))
        messages.insert(0, {"role": "system", "content": hint})
    tools = native_tools(hub)
    if profile_cfg:
        keep = [t for t in tools if profile_allows(profile_cfg, t["function"]["name"])]
        if len(keep) != len(tools):
            log("  profile %r exposes %d/%d tools" % (profile_name, len(keep), len(tools)))
        tools = keep
        plan_block = plan_hint()
        if plan_block:
            messages.insert(0, {"role": "system", "content": plan_block})
    extra = {k: v for k, v in body.items() if k in ("temperature", "top_p", "max_tokens", "frequency_penalty", "presence_penalty", "response_format")}
    steps = []
    message = {}
    cfg = proxy_cfg()
    rounds = max(1, int(cfg.get("max_tool_rounds", 12) or 12))
    budget = float(cfg.get("max_seconds", 120) or 0)
    started = time.time()
    for round_no in range(rounds + 1):
        elapsed = time.time() - started
        if budget and elapsed > budget:
            log("  time budget %.0fs reached after %d step(s) -> wrapping up" % (budget, len(steps)))
            note = "（我已经连续操作了 %.0f 秒，先停在这里。）" % elapsed
            message = {"role": "assistant",
                       "content": (bridge.strip_dsml((message.get("content") or "").strip()) + "\n" + note).strip()}
            return finalize(message, steps), steps, {}
        last_round = round_no >= rounds
        if last_round:
            # Final round: send no tools at all. With tools still declared (and
            # tool_choice "none") DeepSeek answers with DSML tool markup *as text*,
            # which the phone app then shows to the user.
            payload = {"model": model, "messages": messages + [FINAL_NUDGE], "stream": False}
        else:
            payload = {"model": model, "messages": messages, "stream": False,
                       "tools": tools, "tool_choice": "auto"}
        payload.update(extra)
        data = upstream_post("/chat/completions", payload, auth,
                             base=profile_cfg.get("upstream_base"), key=profile_cfg.get("api_key"))
        note_usage(data.get("usage"))
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = message.get("tool_calls") or []
        content = message.get("content") or ""
        log("  round %d: finish=%s tool_calls=%d" % (round_no, choice.get("finish_reason"), len(calls)))

        if not calls:
            # Some models still answer with the old text protocol or with DSML markup
            # (strong history bias, or a weak function-calling model). Act on it anyway.
            fallback = bridge.parse_tool_calls(content)
            if not fallback:
                return finalize(message, steps), steps, data.get("usage") or {}
            log("  model replied with %s (%d call(s)) -> executing anyway"
                % ("DSML markup" if bridge.parse_dsml_calls(content) else "text protocol", len(fallback)))
            messages.append({"role": "assistant", "content": bridge.strip_dsml(content) or content})
            results = []
            for call in fallback:
                args = vision_args(call["name"], bridge.coerce_args(call["name"], call["arguments"]), auth)
                output, is_error = hub.call(call["name"], args)
                log("  tool -> %s %s" % (call["name"], json.dumps(mask_args(args), ensure_ascii=False)[:200]))
                log("  tool <- %s %s (%d chars)" % (call["name"], "ERROR" if is_error else "ok", len(output)))
                steps.append({"tool": call["name"], "arguments": args, "error": is_error, "output": output[:4000]})
                if on_event:
                    on_event(call["name"], is_error)
                results.append("TOOL_RESULT: %s %s\n%s" % (call["name"], "(failed)" if is_error else "(ok)", output[:6000]))
            messages.append({"role": "user", "content": "\n\n".join(results)})
            continue

        messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
        for call in calls:
            name = (call.get("function") or {}).get("name") or ""
            raw = (call.get("function") or {}).get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {"raw": raw}
            args = vision_args(name, args, auth)
            output, is_error = hub.call(name, args)
            log("  tool -> %s %s" % (name, json.dumps(mask_args(args), ensure_ascii=False)[:200]))
            log("  tool <- %s %s (%d chars)" % (name, "ERROR" if is_error else "ok", len(output)))
            steps.append({"tool": name, "arguments": args, "error": is_error, "output": output[:4000],
                          "profile": profile_name})
            if on_event:
                on_event(name, is_error)
            messages.append({"role": "tool", "tool_call_id": call.get("id") or name,
                             "content": ("[tool failed] " if is_error else "") + output[:6000]})
    # Out of rounds: never hand the app DSML markup or a dangling tool_call.
    log("  round budget exhausted after %d step(s)" % len(steps))
    return finalize(message, steps), steps, {}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mcp-hands/1.0"
    _write_lock = threading.Lock()

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------------ helpers
    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, text):
        data = text.encode("utf-8")
        with self._write_lock:
            self.wfile.write(("%X\r\n" % len(data)).encode("ascii") + data + b"\r\n")
            self.wfile.flush()

    def _start_heartbeat(self):
        """Keep the SSE connection warm while slow tools (screenshots, vision) run.

        A tool loop can easily take 30-120 s, and the phone app drops the request
        ("思考中断") when nothing arrives for a while. Comment frames are valid SSE,
        are ignored by every client, and reset that idle timer. They also prove to us
        that the socket is still alive.
        """
        interval = float(proxy_cfg().get("heartbeat_seconds", 5) or 0)
        if interval <= 0:
            return None
        stop = threading.Event()

        def beat():
            while not stop.wait(interval):
                try:
                    self._chunk(": keep-alive\n\n")
                except Exception:  # noqa: BLE001 - client hung up; nothing to clean here
                    return

        threading.Thread(target=beat, daemon=True, name="sse-heartbeat").start()
        return stop

    def _run_loop_streaming(self, body, auth, model, profile_name="", profile_cfg=None):
        """Open the SSE stream *before* the tool loop, so the app never sits in silence."""
        self._sse_start()
        self._sse_open = True
        cid = "chatcmpl-" + uuid.uuid4().hex[:20]
        created = int(time.time())

        def frame(delta, finish=None):
            self._chunk("data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}, ensure_ascii=False) + "\n\n")

        frame({"role": "assistant", "content": ""})
        stop = self._start_heartbeat()
        started = time.time()

        seen = set()
        limit = int(proxy_cfg().get("progress_max", 3) or 0)

        def on_event(tool_name, is_error):
            """Push a visible progress line so the user sees work happening, not silence.

            Capped: a long loop must not turn the chat into a wall of status bubbles -
            the point is to show liveness, then let the real reply arrive.
            """
            if not proxy_cfg().get("progress_stream", True):
                return
            if limit and len(seen) >= limit:
                return
            line = progress_text(tool_name, is_error)
            if line.strip() in seen:
                return
            seen.add(line.strip())
            try:
                frame({"content": line})
            except Exception:  # noqa: BLE001 - client vanished; the loop can still finish
                pass

        try:
            result = run_tool_loop(body, auth, on_event=on_event, profile_name=profile_name,
                                   profile_cfg=profile_cfg)
        finally:
            if stop:
                stop.set()
            log("  tool loop finished in %.1fs (%d progress line(s), heartbeats %s)"
                % (time.time() - started, len(seen), "on" if stop else "off"))
        return result

    def _finish_stream(self, error_text):
        """Close an already-open SSE stream with a visible reason instead of silence."""
        cid = "chatcmpl-" + uuid.uuid4().hex[:20]
        created = int(time.time())

        def frame(delta, finish=None):
            self._chunk("data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": "proxy",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}, ensure_ascii=False) + "\n\n")

        try:
            frame({"content": "（电脑侧执行出错：%s）" % error_text[:400]})
            frame({}, "stop")
            self._chunk("data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
        except Exception:  # noqa: BLE001
            pass

    def _relay(self, body, auth, path):
        try:
            upstream = upstream_open(path, body, auth)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            self._json({"error": {"message": "upstream HTTP %s: %s" % (exc.code, detail[:600]),
                                  "type": "upstream_error"}}, 502)
            return
        except Exception as exc:  # noqa: BLE001
            self._json({"error": {"message": "upstream error: %s" % exc, "type": "upstream_error"}}, 502)
            return
        self.send_response(upstream.status)
        self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/json"))
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while True:
            block = upstream.read(4096)
            if not block:
                break
            self.wfile.write(("%X\r\n" % len(block)).encode("ascii") + block + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        upstream.close()

    def _reply_stream(self, message, model, usage, sse_started=False):
        cid = "chatcmpl-" + uuid.uuid4().hex[:20]
        created = int(time.time())

        def frame(delta, finish=None):
            obj = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self._chunk("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n")

        if not sse_started:
            self._sse_start()
            frame({"role": "assistant", "content": ""})
        reasoning = message.get("reasoning_content")
        if reasoning:
            self._chunk("data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"reasoning_content": reasoning}, "finish_reason": None}]}, ensure_ascii=False) + "\n\n")
        content = message.get("content") or ""
        for index in range(0, len(content), 40):
            frame({"content": content[index:index + 40]})
            time.sleep(0.01)
        frame({}, "stop")
        usage_frame = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                       "choices": [], "usage": usage}
        self._chunk("data: " + json.dumps(usage_frame, ensure_ascii=False) + "\n\n")
        self._chunk("data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")

    # -------------------------------------------------------------------- routes
    def do_GET(self):
        if "models" in self.path:
            models = list(proxy_cfg().get("tool_models") or [])
            try:
                req = urllib.request.Request(upstream_base() + "/models",
                                             headers={"Authorization": self.headers.get("Authorization", "")})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    for item in (json.loads(resp.read().decode("utf-8", "replace")).get("data") or []):
                        if item.get("id") and item["id"] not in models:
                            models.append(item["id"])
            except Exception as exc:  # noqa: BLE001
                log("  models relay failed: %s" % exc)
            self._json({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "aiyu-proxy"} for m in models]})
        elif "health" in self.path:
            hub = bridge.HUB
            self._json({"ok": True, "role": "app-native tool proxy", "upstream": upstream_base(),
                        "tool_models": proxy_cfg().get("tool_models"),
                        "tools": len(hub.specs) if hub else 0})
        else:
            self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8-sig", "replace") or "{}")
        except json.JSONDecodeError as exc:
            self._json({"error": {"message": "bad json: %s" % exc}}, 400)
            return
        auth = self.headers.get("Authorization", "")
        model = body.get("model") or ""
        wanted = proxy_cfg().get("tool_models") or []
        if wanted and model not in wanted:
            log("relay (no tools): model=%s stream=%s" % (model, body.get("stream")))
            self._relay(body, auth, "/chat/completions")
            return
        log("tool request: model=%s messages=%d stream=%s" % (model, len(body.get("messages") or []), body.get("stream")))
        bridge.METRICS["requests_total"] = bridge.METRICS.get("requests_total", 0) + 1
        # 3.0: which identity is asking decides the tool set and the upstream.
        profile_name, profile_cfg = resolve_profile(body, auth, self.headers.get("X-Profile", ""))
        bridge.set_active_profile(profile_name)
        if profile_name:
            log("  profile=%s (tools=%s upstream=%s model=%s)"
                % (profile_name, len(profile_cfg.get("tools") or []) or "all",
                   profile_cfg.get("upstream_base") or "default", profile_cfg.get("model") or model))
        token = remember_key(auth)
        if token:
            log("  remembered the app's key (%s...) for vision calls" % token[:6])
        try:
            self._write_lock = threading.Lock()
            self._sse_open = False
            message, steps, usage = self._run_loop_streaming(body, auth, model, profile_name, profile_cfg) \
                if body.get("stream") \
                else run_tool_loop(body, auth, profile_name=profile_name, profile_cfg=profile_cfg)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            log("  upstream HTTP %s: %s" % (exc.code, detail[:300]))
            if body.get("stream") and self._sse_open:
                self._finish_stream("upstream HTTP %s: %s" % (exc.code, detail[:300]))
                return
            self._json({"error": {"message": "upstream HTTP %s: %s" % (exc.code, detail[:600]),
                                  "type": "upstream_error"}}, 502)
            return
        except Exception as exc:  # noqa: BLE001
            log("  failed: %s: %s" % (type(exc).__name__, exc))
            if body.get("stream") and self._sse_open:
                self._finish_stream("%s: %s" % (type(exc).__name__, exc))
                return
            self._json({"error": {"message": "%s: %s" % (type(exc).__name__, exc), "type": "proxy_error"}}, 502)
            return

        if body.get("stream"):
            self._reply_stream(message, model, usage, sse_started=self._sse_open)
            return
        self._json({
            "id": "chatcmpl-" + uuid.uuid4().hex[:20],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "logprobs": None, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "proxy": {"tool_steps": steps},
        })


class ProxyService:
    """Small wrapper so the GUI can start/stop the proxy in-process."""

    def __init__(self):
        self.server = None
        self.thread = None

    def start(self):
        cfg = proxy_cfg()
        host = (cfg.get("listen") or {}).get("host", "0.0.0.0")
        port = int((cfg.get("listen") or {}).get("port", 8890))
        bridge.start_hub()
        self.server = ThreadingHTTPServer((host, port), ProxyHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        log("app-native tool proxy listening on http://%s:%d/v1 (upstream %s, tool models %s)"
            % (host, port, upstream_base(), cfg.get("tool_models")))
        return self

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.server = None
            log("app-native tool proxy stopped")

    @property
    def running(self):
        return self.server is not None


def main():
    bridge.log("=" * 62)
    bridge.log(bridge.version_line())
    bridge.log("app-native tool proxy %s" % ("(packed exe)" if FROZEN else "(source)"))
    bridge.log("config : %s" % bridge.CONFIG_PATH)
    ProxyService().start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
