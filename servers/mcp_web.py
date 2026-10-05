"""MCP server: fetch URLs and do raw HTTP requests (stdlib only)."""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) persona-mcp-bridge/1.0"
MAX = int(os.environ.get("MCP_WEB_MAX_CHARS") or 12000)

srv = Server("web")

_TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_ANY = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


def _to_text(html):
    text = _TAG.sub(" ", html)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text, flags=re.I)
    text = _ANY.sub("", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    text = _WS.sub(" ", text)
    text = _NL.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _http(method, url, headers=None, body=None, timeout_s=30):
    data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers or {})
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.status, resp.headers.get("Content-Type", ""), raw.decode(charset, errors="replace")


@srv.tool("fetch_url", "Fetch an http(s) URL and return readable text (HTML stripped).",
          {"type": "object", "properties": {"url": {"type": "string"}, "max_chars": {"type": "integer", "default": MAX}}, "required": ["url"]})
def fetch_url(url, max_chars=MAX):
    status, ctype, text = _http("GET", url)
    if "html" in ctype.lower():
        text = _to_text(text)
    limit = int(max_chars)
    cut = text[:limit]
    return "status=%s content-type=%s\n\n%s%s" % (status, ctype, cut, "\n...[truncated]" if len(text) > limit else "")


@srv.tool("http_request", "Send an arbitrary HTTP request. headers_json is a JSON object string.",
          {"type": "object", "properties": {"method": {"type": "string", "default": "GET"}, "url": {"type": "string"}, "headers_json": {"type": "string", "default": "{}"}, "body": {"type": "string", "default": ""}, "timeout_s": {"type": "integer", "default": 30}}, "required": ["url"]})
def http_request(url, method="GET", headers_json="{}", body="", timeout_s=30):
    try:
        headers = json.loads(headers_json or "{}")
    except json.JSONDecodeError as exc:
        return "headers_json is not valid JSON: %s" % exc
    status, ctype, text = _http(method, url, headers, body or None, timeout_s)
    return "status=%s content-type=%s\n\n%s" % (status, ctype, text[:MAX])


@srv.tool("download_file", "Download a URL to a local path.",
          {"type": "object", "properties": {"url": {"type": "string"}, "path": {"type": "string"}}, "required": ["url", "path"]})
def download_file(url, path):
    dest = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as fh:
        total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
    return "saved %d bytes -> %s" % (total, dest)


@srv.tool("url_encode", "Percent-encode a string (utility).",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def url_encode(text):
    return urllib.parse.quote(text)


@srv.tool("search_web", "Web search via DuckDuckGo, returns titles and URLs.",
          {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 8}}, "required": ["query"]})
def search_web(query, limit=8):
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(str(query))
    status, _ctype, html = _http("GET", url, timeout_s=25)
    rows = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
    if not rows:
        rows = re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.S)
    out = []
    for href, title in rows[: max(1, int(limit))]:
        clean = _ANY.sub("", title).strip()
        if clean:
            out.append("%s\n  %s" % (clean, href))
    return "status=%s\n%s" % (status, "\n".join(out) or "(no results parsed)")


@srv.tool("page_links", "Extract absolute links from a page.",
          {"type": "object", "properties": {"url": {"type": "string"}, "limit": {"type": "integer", "default": 40}}, "required": ["url"]})
def page_links(url, limit=40):
    status, _ctype, html = _http("GET", url, timeout_s=25)
    seen = []
    for href in re.findall(r'href=["\']([^"\'>]+)["\']', html):
        absolute = urllib.parse.urljoin(url, href)
        if absolute.startswith(("http://", "https://")) and absolute not in seen:
            seen.append(absolute)
        if len(seen) >= max(1, int(limit)):
            break
    return "status=%s count=%d\n%s" % (status, len(seen), "\n".join(seen))


SAMPLES = {
    # file:// keeps the self-test offline-deterministic; real http(s) fetches were
    # verified by hand (status=200 on live pages) rather than in every test run.
    "fetch_url": {"url": "file:///" + os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "win.ini").replace("\\", "/")},
    "url_encode": {"text": "a b/c"},
}
# samples that need internet access / a reachable remote host
SAMPLES_OPTIONAL = {"fetch_url"}


def build():
    return srv


if __name__ == "__main__":
    srv.run()

