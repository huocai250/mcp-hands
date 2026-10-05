"""MCP server: share a folder over LAN HTTP so another device (e.g. a phone) can
download files, plus a tiny downloader that pushes fetched files into that share.

Stdlib only. The listing is read-only apart from the `_share` folder; stop the
server (stop_server) when the transfer is done.
"""
import functools
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

TEMP = os.environ.get("TEMP") or os.environ.get("TMP") or "."
SHARE_DIR_NAME = "_share"
UA = "persona-mcp-bridge/1.0"

srv = Server("http")
SERVERS = {}
_LOCK = threading.Lock()


def _abs(path):
    return os.path.abspath(os.path.expanduser(str(path)))


def _root_dir(path):
    p = _abs(path)
    if not os.path.isdir(p):
        raise NotADirectoryError(p)
    return p


def _port(value):
    return max(0, min(65535, int(value)))


def _local_urls(port):
    """Every URL another device could use for this port. [] when the port is 0."""
    p = int(port)
    if p <= 0:
        return []
    seen = []
    lan = []
    loopback = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        ip = info[4][0]
        if not ip or ip in seen:
            continue
        seen.append(ip)
        if ip.startswith("127."):
            loopback.append(ip)
        else:
            lan.append(ip)
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 53))
            ip = probe.getsockname()[0]
        finally:
            probe.close()
        if ip and not ip.startswith("127.") and ip not in lan:
            lan.append(ip)
    except OSError:
        pass
    urls = ["http://%s:%d/" % (ip, p) for ip in lan]
    if not urls:
        urls = ["http://%s:%d/" % (ip or "127.0.0.1", p) for ip in (loopback or ["127.0.0.1"])]
    return urls


def _firewall_hint(port, root):
    """Windows drops inbound LAN connections unless something opened the port."""
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "open-firewall.ps1")
    tip = ["hint: allow inbound TCP %d in Windows Firewall (run open-firewall.ps1 as admin:" % port,
           "      %s -Port %d)" % (script, port)]
    try:
        rules = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=all"],
            capture_output=True, text=True, timeout=20, errors="replace").stdout or ""
    except (OSError, subprocess.SubprocessError) as exc:
        rules = ""
        tip.append("      (firewall probe failed: %s)" % exc)
    if root and root.lower() in rules.lower():
        tip.append("      a rule already mentions this folder")
    return tip


class _Handler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler with directory listing on and silent logging."""

    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        pass

    def version_string(self):
        return "persona-mcp-bridge/1.0"

    def list_directory(self, path):
        # Force the listing: SimpleHTTPRequestHandler skips index-less dirs otherwise.
        return http.server.SimpleHTTPRequestHandler.list_directory(self, path)

    def do_GET(self):
        counter = getattr(self.server, "share_counter", None)
        if counter is not None:
            counter["requests"] += 1
        return http.server.SimpleHTTPRequestHandler.do_GET(self)

    def do_HEAD(self):
        counter = getattr(self.server, "share_counter", None)
        if counter is not None:
            counter["requests"] += 1
        return http.server.SimpleHTTPRequestHandler.do_HEAD(self)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # Windows would otherwise set SO_REUSEADDR, letting a second server bind a port
    # another process already owns; requests then land on whichever socket wins.
    allow_reuse_address = False
    address_family = socket.AF_INET


def _make_server(host, port, root):
    handler = functools.partial(_Handler, directory=root)
    if int(port):
        # Probe first: on Windows SO_REUSEADDR can hide a live listener behind us.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((host, int(port)))
        except OSError as exc:
            return None, "port %s is already in use on %s (%s)" % (port, host, exc)
        finally:
            probe.close()
    try:
        httpd = _Server((host, int(port)), handler)
    except OSError as exc:
        return None, "cannot bind %s:%s -> %s" % (host, port, exc)
    httpd.share_counter = {"requests": 0}
    return httpd, ""


def _is_lan(entry):
    """Loopback-only binds are never share targets: a phone cannot reach them."""
    return entry.get("host", "0.0.0.0") not in ("127.0.0.1", "::1", "localhost")


def _active():
    with _LOCK:
        return dict(SERVERS)


def _newest():
    """Newest LAN-reachable server; loopback-only ones are ignored for sharing."""
    active = _active()
    lan = {p: e for p, e in active.items() if _is_lan(e)}
    pool = lan or active
    if not pool:
        return None, None
    port = max(pool, key=lambda p: pool[p]["started"])
    return port, pool[port]


def _share_root():
    """(server entry, share folder) of the most recently started server."""
    _port_num, entry = _newest()
    if entry is None:
        return None, None
    return entry, os.path.join(entry["root"], SHARE_DIR_NAME)


def _free_name(directory, name):
    base, ext = os.path.splitext(name)
    candidate = os.path.join(directory, name)
    seq = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, "%s_%d%s" % (base, seq, ext))
        seq += 1
    return candidate


def _pick_url(urls):
    for url in urls:
        if "127.0.0.1" not in url:
            return url
    return urls[0] if urls else ""


def _server_urls(entry):
    """URLs that actually reach this bind: loopback-only when bound to loopback."""
    host = entry.get("host", "0.0.0.0")
    port = entry["port"]
    if host in ("127.0.0.1", "::1", "localhost"):
        return ["http://127.0.0.1:%d/" % port]
    return _local_urls(port)


def _entry_url(entry, rel=""):
    return _pick_url(_server_urls(entry)).rstrip("/") + "/" + urllib.parse.quote(rel)


@srv.tool("serve_dir", "Start a background threaded HTTP server over a folder (read-only listing plus the _share folder).",
          {"type": "object", "properties": {"root": {"type": "string"}, "port": {"type": "integer", "default": 8811}, "host": {"type": "string", "default": "0.0.0.0"}}, "required": ["root"]})
def serve_dir(root, port=8811, host="0.0.0.0"):
    r = _root_dir(root)
    want = _port(port)
    bind_host = str(host or "0.0.0.0")
    httpd, err = _make_server(bind_host, want, r)
    if httpd is None and want:
        httpd, err = _make_server(bind_host, 0, r)  # port busy: take an ephemeral one
    if httpd is None:
        return "could not start http server: %s" % err
    actual = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    entry = {"httpd": httpd, "thread": thread, "root": r, "host": bind_host,
             "port": actual, "started": time.time(), "requests": httpd.share_counter}
    with _LOCK:
        SERVERS[actual] = entry
    os.makedirs(os.path.join(r, SHARE_DIR_NAME), exist_ok=True)
    urls = _server_urls(entry)
    lines = ["serving %s" % r, "bind=%s:%d listening=%d" % (bind_host, want, actual),
             "share folder: %s" % os.path.join(r, SHARE_DIR_NAME)]
    lines.extend("url %s (LAN IP: %s)" % (u, urllib.parse.urlsplit(u).hostname) for u in urls)
    if want and actual != want:
        lines.append("note: %d was busy, using %d instead" % (want, actual))
    if bind_host in ("127.0.0.1", "localhost"):
        lines.append("note: bound to loopback only - pass host=0.0.0.0 to reach it from the phone")
    lines.extend(_firewall_hint(actual, r))
    lines.append("share is read-only listing plus _share/; call stop_server(%d) when done" % actual)
    return "\n".join(lines)


@srv.tool("server_status", "List running file servers: port, root, uptime and served request count.",
          {"type": "object", "properties": {}, "required": []})
def server_status():
    active = _active()
    if not active:
        return "no http servers running (start one with serve_dir)"
    lines = ["servers=%d" % len(active)]
    now = time.time()
    for port in sorted(active):
        entry = active[port]
        lines.append("port=%d root=%s uptime=%.0fs requests=%d urls=%s" % (
            port, entry["root"], now - entry["started"], entry["requests"]["requests"],
            ", ".join(_server_urls(entry))))
    return "\n".join(lines)


@srv.tool("stop_server", "Stop one running file server by port, or every server when port=0.",
          {"type": "object", "properties": {"port": {"type": "integer", "default": 0}}, "required": []})
def stop_server(port=0):
    target = _port(port)
    with _LOCK:
        victims = sorted(SERVERS) if target == 0 else ([target] if target in SERVERS else [])
        entries = [SERVERS.pop(p) for p in victims]
    if not victims:
        return "no server on port %d (running: %s)" % (target, sorted(_active()) or "none")
    for entry in entries:
        try:
            entry["httpd"].shutdown()
        except Exception:  # noqa: BLE001 - shutdown must never block the stop path
            pass
        try:
            entry["httpd"].server_close()
        except Exception:  # noqa: BLE001
            pass
    return "stopped %s" % ", ".join("port=%d (%d requests)" % (p, e["requests"]["requests"])
                                    for p, e in zip(victims, entries))


# Screenshots meant for the user's phone live here; one call captures, serves and
# returns a URL, so the assistant never has to hand-roll a file server again.
SHOT_ROOT = os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"), "mcp-hands-shots")


def _grab_screen(monitor="primary"):
    from PIL import ImageGrab
    if str(monitor).strip().lower() in ("all", "-1", "virtual"):
        return ImageGrab.grab(all_screens=True)
    return ImageGrab.grab()


def _shrink(image, max_pixels):
    from PIL import Image
    limit = int(max_pixels or 0)
    width, height = image.size
    if limit and max(width, height) > limit:
        scale = limit / float(max(width, height))
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)
    return image


def _ensure_server(port):
    """Reuse a running server, or start one rooted at the screenshot folder."""
    entry, share = _share_root()
    if entry is not None:
        return entry, share, ""
    os.makedirs(SHOT_ROOT, exist_ok=True)
    started = serve_dir(SHOT_ROOT, port)
    if "serving" not in started:
        return None, None, started
    entry, share = _share_root()
    return entry, share, ""


def _outbox_deliver(path, kind="image", note=""):
    """Hand a file to the bridge outbox and return the phone-facing URL.

    4.3.1 - measured on the real client: it only fetches/render pictures from **its own
    base origin** (the proxy port, e.g. :8890/out/...). A link to this server's own port
    (e.g. :8892/_share/...) is shown as plain text no matter how it is formatted. So the
    file-server tools deliver through the outbox as well, and the outbox URL is what they
    answer with.
    """
    api = (os.environ.get("MCP_BRIDGE_API") or "http://127.0.0.1:8877").rstrip("/")
    payload = json.dumps({"path": os.path.abspath(path), "kind": kind, "note": note},
                         ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(api + "/v2/outbox", data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        return (data.get("item") or {}).get("url") or "", ""
    except Exception as exc:  # noqa: BLE001
        return "", "%s: %s" % (type(exc).__name__, exc)


@srv.tool("share_screenshot",
          "Capture the screen and hand the user a picture link that the chat client can actually "
          "render (delivered through the phone-facing outbox). The folder server is started too, "
          "for browsers on the same Wi-Fi.",
          {"type": "object", "properties": {"monitor": {"type": "string", "default": "primary"},
                                            "port": {"type": "integer", "default": 8811},
                                            "max_pixels": {"type": "integer", "default": 1600},
                                            "name": {"type": "string", "default": ""}},
           "required": []})
def share_screenshot(monitor="primary", port=8811, max_pixels=1600, name=""):
    image = _shrink(_grab_screen(monitor), max_pixels)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    wanted = str(name or "").strip() or ("screen-%s.png" % stamp)
    if not wanted.lower().endswith(".png"):
        wanted += ".png"
    shot_dir = os.path.join(SHOT_ROOT, "chat")
    os.makedirs(shot_dir, exist_ok=True)
    target = _free_name(shot_dir, wanted)
    image.save(target, format="PNG")
    outbox_url, problem = _outbox_deliver(target, "image", note="屏幕截图")
    if outbox_url:
        return outbox_url
    entry, share, start_problem = _ensure_server(port)
    if entry is None:
        return ("could not deliver the screenshot to the chat client (%s) and the folder server "
                "did not start either (%s)" % (problem, start_problem))
    os.makedirs(share, exist_ok=True)
    fallback = _free_name(share, wanted)
    try:
        image.save(fallback, format="PNG")
    except OSError:
        pass
    rel = "/".join([SHARE_DIR_NAME, os.path.basename(fallback)])
    return ("outbox unavailable (%s); this link works in a browser on the same Wi-Fi but the chat "
            "client will show it as text:\n%s" % (problem, _entry_url(entry, rel)))


@srv.tool("share_file", "Hand the user a download link the chat client can actually open "
                        "(delivered through the phone-facing outbox); also copies the file into the "
                        "folder server so a browser can grab it.",
          {"type": "object", "properties": {"path": {"type": "string"}, "name": {"type": "string", "default": ""}}, "required": ["path"]})
def share_file(path, name=""):
    src = _abs(path)
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    ext = os.path.splitext(src)[1].lower()
    kind = {".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
            ".wav": "audio", ".mp3": "audio", ".m4a": "audio",
            ".mp4": "video", ".mkv": "video", ".webm": "video",
            ".zip": "archive", ".7z": "archive", ".rar": "archive"}.get(ext, "file")
    outbox_url, problem = _outbox_deliver(src, kind, note=os.path.basename(src))
    if outbox_url:
        return outbox_url
    entry, share = _share_root()
    if share is None:
        return ("could not deliver to the chat client (%s) and no folder server is running; "
                "start one with serve_dir(root=%r)" % (problem, os.path.dirname(src)))
    os.makedirs(share, exist_ok=True)
    target = _free_name(share, os.path.basename(str(name).strip()) or os.path.basename(src))
    with open(src, "rb") as fh_in, open(target, "wb") as fh_out:
        while True:
            chunk = fh_in.read(1 << 20)
            if not chunk:
                break
            fh_out.write(chunk)
    rel = "/".join([SHARE_DIR_NAME, os.path.basename(target)])
    url = _entry_url(entry, rel)
    return "shared=%s\nurl=%s\nbytes=%d\nport=%d\nstop the server with stop_server(%d) when done" % (
        target, url, os.path.getsize(target), entry["port"], entry["port"])


@srv.tool("download_to_share", "Fetch a URL from the internet and hand the user a link the chat client "
                                "can open (delivered through the phone-facing outbox).",
          {"type": "object", "properties": {"url": {"type": "string"}, "name": {"type": "string", "default": ""}}, "required": ["url"]})
def download_to_share(url, name=""):
    entry, share = _share_root()
    staging = share or os.path.join(SHOT_ROOT, "downloads")
    os.makedirs(staging, exist_ok=True)
    raw_name = str(name or "").strip()
    if not raw_name:
        raw_name = os.path.basename(urllib.parse.urlsplit(str(url)).path) or "download.bin"
    target = _free_name(staging, raw_name)
    req = urllib.request.Request(str(url), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp, open(target, "wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    ext = os.path.splitext(target)[1].lower()
    kind = {".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
            ".wav": "audio", ".mp3": "audio", ".mp4": "video", ".webm": "video"}.get(ext, "file")
    outbox_url, problem = _outbox_deliver(target, kind, note=os.path.basename(target))
    if outbox_url:
        return outbox_url
    if share is None:
        return ("downloaded %d bytes to %s, but could not deliver it to the chat client (%s); "
                "start a folder server with serve_dir(root=%r) if you need the old route"
                % (os.path.getsize(target), target, problem, os.path.dirname(target)))
    rel = "/".join([SHARE_DIR_NAME, os.path.basename(target)])
    out_url = _entry_url(entry, rel)
    return "saved=%s\nurl=%s\nsource=%s\nbytes=%d\nport=%d" % (
        target, out_url, url, os.path.getsize(target), entry["port"])


@srv.tool("file_url", "URL a running server exposes for a path inside its root (nothing is served outside the root).",
          {"type": "object", "properties": {"path": {"type": "string"}, "port": {"type": "integer", "default": 0}}, "required": ["path"]})
def file_url(path, port=0):
    target = _port(port)
    active = _active()
    if not active:
        return "no http server running: start one with serve_dir(root, port) first"
    selected = target if target in active else (max(active) if target == 0 else 0)
    if selected not in active:
        return "no server on port %d (running: %s)" % (target, sorted(active))
    entry = active[selected]
    p = _abs(path)
    root = entry["root"]
    try:
        inside = os.path.commonpath([os.path.normcase(root), os.path.normcase(p)]) == os.path.normcase(root)
    except ValueError:
        inside = False  # different drive
    if not inside:
        return "path=%s\nurl=(outside served root %s)" % (p, root)
    rel = os.path.relpath(p, root).replace(os.sep, "/")
    if rel == ".":
        rel = ""
    return "path=%s\nroot=%s\nurl=%s" % (p, root, _entry_url(entry, rel))


@srv.tool("local_urls", "List every LAN URL for a port: all IPv4 addresses of this machine (URLs only, port must match serve_dir).",
          {"type": "object", "properties": {"port": {"type": "integer", "default": 8811}}, "required": []})
def local_urls(port=8811):
    p = _port(port)
    lines = [
        "port=%d" % p,
        "hostname=%s" % socket.gethostname(),
        "-- all IPv4 addresses of this machine --",
        _all_ipv4(),
    ]
    urls = _server_urls(_active()[p]) if p in _active() else _local_urls(p)
    lines.append("urls for port %d:" % p)
    lines.extend("  " + u for u in urls)
    if p in _active():
        lines.append("  (this port is the running server: root=%s)" % _active()[p]["root"])
    return "\n".join(lines)


def _all_ipv4():
    """Every IPv4 address, via getaddrinfo plus hostname/FQDN resolution."""
    names = [socket.gethostname(), socket.getfqdn()]
    found = []
    for name in names:
        try:
            for info in socket.getaddrinfo(name, None, socket.AF_INET):
                ip = info[4][0]
                if ip not in found:
                    found.append(ip)
        except OSError:
            continue
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 53))
            ip = probe.getsockname()[0]
        finally:
            probe.close()
        if ip not in found:
            found.append(ip)
    except OSError:
        pass
    return "\n".join("  " + ip for ip in found) or "  (none found)"


_SELF = os.path.join(sample_dir(), ".aiyu_self_samples", "http")
_SHARE = os.path.join(_SELF, "share")
_SHARE_FILE = os.path.join(_SHARE, "hello.txt")
# Round trip through the share: fetch this file from a loopback HTTP endpoint, then
# re-share it. The endpoint starts on first use, so a dormant server holds no port.
_ORIGIN_DIR = os.path.join(_SELF, "origin")
_ORIGIN = os.path.join(_ORIGIN_DIR, "hello.txt")
PORT = 8811
_ORIGIN_SERVER = None
_ORIGIN_PORT = 0
_ORIGIN_LOCK = threading.Lock()


def _start_origin():
    """Idempotently start the loopback HTTP source used by the download self-test."""
    global _ORIGIN_SERVER, _ORIGIN_PORT
    with _ORIGIN_LOCK:
        if _ORIGIN_SERVER is not None:
            return _ORIGIN_SERVER
        try:
            os.makedirs(_ORIGIN_DIR, exist_ok=True)
            with open(_ORIGIN, "w", encoding="utf-8") as fh:
                fh.write("http self-test payload\r\n")
        except OSError:
            return None
        httpd, _err = _make_server("127.0.0.1", 0, _ORIGIN_DIR)
        if httpd is None:
            return None
        _ORIGIN_SERVER = httpd
        _ORIGIN_PORT = int(httpd.server_address[1])
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                         daemon=True).start()
        return httpd


try:
    os.makedirs(_SHARE, exist_ok=True)
    with open(_SHARE_FILE, "w", encoding="utf-8") as fh:
        fh.write("http self-test payload\n")
    _SEEDED = True
except OSError:
    _SEEDED = False

# serve_dir opens a real socket on high port 8811 (0.0.0.0 is the default bind, which
# is what makes the URL reachable from a phone). SAMPLES stays quiet: no list, no stop.
SAMPLES = {
    "serve_dir": {"root": _SHARE, "port": PORT, "host": "0.0.0.0"},
    "local_urls": {"port": PORT},
    "server_status": {},
    "share_screenshot": {"max_pixels": 800},
    "share_file": {"path": _SHARE_FILE},
    "download_to_share": {"url": "http://127.0.0.1:0/hello.txt", "name": "fetched.txt"},
    "file_url": {"path": _SHARE_FILE, "port": PORT},
}
SAMPLES_OPTIONAL = set()
if not _SEEDED:
    SAMPLES_OPTIONAL = {"download_to_share"}
    SAMPLES.pop("download_to_share")

# The origin port is only known once it is running: resolve it in place so the
# self-test (which reads this dict at call time) gets the real URL.
_start_origin()
if _ORIGIN_SERVER is None and "download_to_share" in SAMPLES:
    SAMPLES_OPTIONAL.add("download_to_share")
elif _ORIGIN_SERVER is not None:
    SAMPLES["download_to_share"] = {"url": "http://127.0.0.1:%d/hello.txt" % _ORIGIN_PORT,
                                    "name": "fetched.txt"}


def build():
    return srv


if __name__ == "__main__":
    srv.run()
