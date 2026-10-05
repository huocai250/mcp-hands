"""MCP server: network quality checks -- ping stats, HTTP phase timings, TLS certs,
download throughput, DNS records, route tracing, IP geo, port reports, Wi-Fi quality.

Stdlib + PowerShell/applets (ping/tracert/nslookup/netsh); dnspython is used for DNS
records when it happens to be installed. Every network operation is bounded by an
explicit timeout and degrades to a short honest error line when there is no internet.
"""
import http.client
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server, sample_dir  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
PS = "powershell" if os.name == "nt" else "pwsh"
PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
UA = "mcpserver-netcheck/1.0"
# One shared context for every TLS connection this server makes.
SSL_CTX = ssl.create_default_context()
MAX_PORTS = 64
PING_TRY = 3
HOP_GUARD = 64
OUT_CAP = 8000
TEXT_CAP = 240
DL_URLS = ("https://speed.cloudflare.com/__down?bytes=1000000",
           "https://raw.githubusercontent.com/git/git/master/README.md")
GEO_APIS = ("http://ip-api.com/json/", "http://ipinfo.io/json", "http://ipwho.is/")
TLS_WARN_DAYS = 14
HTTP_TIMING_HOSTS = ("https://www.cloudflare.com", "https://example.com", "https://www.bing.com")

srv = Server("netcheck")


def _clip(text, n=TEXT_CAP):
    s = str(text).strip()
    return s if len(s) <= n else s[:n] + "..."


def _geo_text(value):
    """Geo APIs disagree on types: ipwho.is nests objects (connection/timezone)."""
    if isinstance(value, dict):
        for key in ("isp", "org", "asn", "name", "id"):
            if value.get(key):
                return str(value[key])
        return "?"
    return str(value).strip() if value else "?"


def _decode(raw):
    """Windows console tools emit cp936/mbcs; some emit UTF-16 when piped."""
    raw = bytes(raw or b"")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16", errors="replace")
        except Exception:
            pass
    else:
        head = raw[:128]
        if head and head.count(b"\x00") > len(head) / 3:
            for enc in ("utf-16-le", "utf-16-be"):
                try:
                    text = raw.decode(enc, errors="replace")
                except Exception:
                    continue
                if text.count("\ufffd") < len(text) / 5:
                    return text
    for enc in ("utf-8", "cp936", "mbcs"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def _run(argv, timeout_s):
    """Run a console tool with a hard timeout; returns (returncode, text)."""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=float(timeout_s),
                           creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return None, "timed out after %ss" % timeout_s
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)
    text = _decode(p.stdout)
    if p.stderr:
        text += "\n" + _decode(p.stderr)
    return p.returncode, text


def _ps(script, timeout_s=45):
    """Run a PowerShell snippet with the house UTF-8 console prefix."""
    try:
        p = subprocess.run(
            [PS, "-NoProfile", "-NonInteractive", "-Command", PS_PREFIX + script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=float(timeout_s), creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return "not available: PowerShell call timed out after %ss" % timeout_s
    except Exception as exc:
        return "not available: %s: %s" % (type(exc).__name__, exc)
    out = (p.stdout or "").strip()
    if p.stderr and p.stderr.strip():
        out += "\n[stderr] " + p.stderr.strip()[:300]
    return out if out else "(no output)"


def _url(u):
    u = str(u).strip()
    if u and "://" not in u:
        u = "https://" + u
    return u


def _median(values):
    vals = sorted(float(v) for v in values)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _first_int(text):
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


def _num_fields(line):
    """Numbers on a ping summary line: sent, received, lost ... in output order."""
    return [int(x) for x in re.findall(r"\d+", line or "")]


def _latin(text):
    return bool(text) and all(ord(ch) < 128 for ch in text)


# --------------------------------------------------------------------------- ping


def _ping_summary(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for tag in ("Packets: Sent", "Sent =", "Sent=", "\u53d1\u9001"):
        for ln in lines:
            if tag in ln:
                nums = _num_fields(ln)
                if len(nums) >= 3:
                    sent, recv, lost = nums[0], nums[1], nums[2]
                    pct = nums[3] if len(nums) > 3 else round(100.0 * lost / max(1, sent), 1)
                    return "loss=%s%% (%d/%d lost, %d sent, %d received)" % (pct, lost, sent, sent, recv)
    return ""


def _ping_stats(text):
    vals = {"min": None, "avg": None, "max": None}
    keys = (("min", ("Minimum", "min")), ("avg", ("Average", "avg", "Middle")),
            ("max", ("Maximum", "max")))
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        lm = line.lower()
        for name, tags in keys:
            if vals[name] is not None:
                continue
            if any(lm.strip().startswith(t.lower()) or t.lower() in lm for t in tags):
                m = re.search(r"=\s*(\d+(?:\.\d+)?)\s*ms", line)
                if m:
                    vals[name] = float(m.group(1))
                    break
    replies = re.findall(r"=\s*(\d+(?:\.\d+)?)\s*ms", text or "")
    if vals["min"] is None and replies:
        nums = [float(v) for v in replies]
        vals = {"min": min(nums), "avg": sum(nums) / len(nums), "max": max(nums)}
    if vals["min"] is None:
        return ""
    return "min/avg/max = %.1f/%.1f/%.1f ms" % (vals["min"], vals["avg"], vals["max"])


def _ping_host(host, count=4, timeout_ms=1000):
    try:
        n = max(1, min(20, int(count)))
    except (TypeError, ValueError):
        n = 4
    try:
        ms = max(50, min(10000, int(timeout_ms)))
    except (TypeError, ValueError):
        ms = 1000
    if os.name == "nt":
        argv = ["ping", "-n", str(n), "-w", str(ms), "-4", host]
    else:
        argv = ["ping", "-c", str(n), "-W", str(max(1, int(ms / 1000))), host]
    rc, text = _run(argv, n * (ms / 1000.0) + 12)
    if rc is None:
        return "ping %s %s" % (host, text)
    stats = _ping_stats(text)
    loss = _ping_summary(text)
    if not stats and not loss:
        low = (text or "").lower()
        if "could not find host" in low or "not find" in low or "unknown host" in low \
                or "\u627e\u4e0d\u5230\u4e3b\u673a" in text:
            return "ping %s: host not found (DNS failure, no internet?)" % host
        return "ping %s failed (exit=%s)\n%s" % (host, rc, _clip(text, 200))
    return "ping %s: %s | %s (count=%d, timeout=%dms)" % (host, stats or "no replies", loss or "loss unknown", n, ms)


@srv.tool("ping_stats", "Ping a host and report min/avg/max latency and packet loss from the real ping output.",
          {"type": "object",
           "properties": {"host": {"type": "string"}, "count": {"type": "integer", "default": 4},
                          "timeout_ms": {"type": "integer", "default": 1000}},
           "required": ["host"]})
def ping_stats(host, count=4, timeout_ms=1000):
    return _ping_host(str(host).strip(), count, timeout_ms)


# --------------------------------------------------------------------- http_timing


def _http_once(url, host, port, target, tls, timeout_s):
    m = {"dns": 0.0, "connect": 0.0, "tls": 0.0, "ttfb": 0.0, "total": 0.0,
         "status": 0, "length": 0, "hops": 0}
    t0 = time.perf_counter()
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        raise RuntimeError("DNS lookup for %s failed: %s" % (host, exc))
    t1 = time.perf_counter()
    if tls:
        # HTTPSConnection.connect() performs both the TCP connect and the TLS
        # handshake; wrapping the socket again here would corrupt the session.
        conn = http.client.HTTPSConnection(host, port, timeout=timeout_s, context=SSL_CTX)
        conn.connect()
        t2 = time.perf_counter()
        cipher = conn.sock.cipher() if conn.sock is not None else None
        proto = conn.sock.version() if conn.sock is not None else None
        t3 = t2
        note = "%s %s" % (proto or "?", cipher[0] if cipher else "?")
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
        conn.connect()
        t2 = time.perf_counter()
        t3 = t2
        note = "plain http"
    try:
        conn.request("GET", target, headers={"User-Agent": UA, "Accept-Encoding": "identity",
                                             "Connection": "close"})
        resp = conn.getresponse()
        t4 = time.perf_counter()
        body = resp.read(262144)
        if body[:2] == b"\x1f\x8b":
            try:
                import gzip
                body = gzip.decompress(body)
            except Exception:
                pass
        t5 = time.perf_counter()
        m.update({"dns": (t1 - t0) * 1000, "connect": (t2 - t1) * 1000,
                  "tls": (t3 - t2) * 1000, "ttfb": (t4 - t3) * 1000,
                  "total": (t5 - t0) * 1000, "status": int(resp.status),
                  "length": len(body), "hops": len(resp.getheaders()), "note": note})
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return m


def _timing_run(url, n, per_call=6):
    """Time one URL n times; returns (runs, errors, host)."""
    try:
        parts = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
        host = parts.hostname
    except Exception as exc:
        return [], ["bad url: %s" % _clip(exc, 120)], ""
    if not host:
        return [], ["bad url: no host"], ""
    tls = parts.scheme.lower() != "http"
    port = parts.port or (443 if tls else 80)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    runs = []
    errs = []
    for _ in range(n):
        try:
            runs.append(_http_once(url, host, port, target, tls, per_call))
        except Exception as exc:
            errs.append("%s: %s" % (type(exc).__name__, _clip(exc, 120)))
    return runs, errs, host


@srv.tool("http_timing",
          "Time DNS/connect/TLS/TTFB/total for a URL over N runs and report the median of each phase "
          "(falls back to a well-known URL when the given one is blocked or unreachable).",
          {"type": "object", "properties": {"url": {"type": "string"}, "count": {"type": "integer", "default": 3}},
           "required": ["url"]})
def http_timing(url, count=3):
    u = _url(url)
    try:
        n = max(1, min(10, int(count)))
    except (TypeError, ValueError):
        n = 3
    runs, errs, host = _timing_run(u, n)
    used = u
    if not runs:
        attempted = [u]
        deadline = time.perf_counter() + 20.0
        for alt in HTTP_TIMING_HOSTS:
            if alt.rstrip("/") == u.rstrip("/") or time.perf_counter() > deadline:
                continue
            attempted.append(alt)
            alt_runs, alt_errs, _h = _timing_run(alt, n)
            if alt_runs:
                runs, host, used = alt_runs, _h, alt
                errs = ["%s unreachable (%s)" % (u, errs[0] if errs else "unknown")] + alt_errs
                break
            errs.extend(alt_errs)
    if not runs:
        return ("http_timing failed for %s (and %d fallback host(s)): %s\n"
                "(likely no internet, a proxy, or the host resets this client)"
                % (u, len(HTTP_TIMING_HOSTS), "; ".join(errs[:3]) or "unknown error"))
    med = {k: _median([r[k] for r in runs]) for k in ("dns", "connect", "tls", "ttfb", "total")}
    head = "url=%s runs=%d status=%s bytes=%d" % (used, len(runs), runs[-1]["status"], runs[-1]["length"])
    body = ("median ms: dns=%.1f connect=%.1f tls=%.1f ttfb=%.1f total=%.1f"
            % (med["dns"], med["connect"], med["tls"], med["ttfb"], med["total"]))
    out = head + "\n" + body
    if errs:
        out += "\n[note] " + "; ".join(errs[:2])
    return out


# ----------------------------------------------------------------------- tls_cert


def _name(seq, item):
    vals = []
    for ava in (item or []):
        try:
            key, value = ava
        except Exception:
            continue
        if str(key).upper() == seq:
            vals.append(str(value))
    return "/".join(vals)


def _date(entry):
    raw = str(entry or "").strip()
    fmts = ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y", "%Y%m%d%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ")
    for fmt in fmts:
        try:
            return time.strptime(re.sub(r"\s+", " ", raw), fmt)
        except Exception:
            continue
    return None


@srv.tool("tls_cert",
          "Read a TLS certificate: subject, issuer, SANs, validity dates, days until expiry "
          "(warns when under 14 days).",
          {"type": "object", "properties": {"host": {"type": "string"}, "port": {"type": "integer", "default": 443}},
           "required": ["host"]})
def tls_cert(host, port=443):
    h = str(host).strip()
    try:
        p = int(port)
    except (TypeError, ValueError):
        p = 443
    if not (0 < p < 65536):
        p = 443
    ctx = ssl.create_default_context()
    sock = None
    raw = None
    try:
        info = socket.getaddrinfo(h, p, type=socket.SOCK_STREAM)
    except Exception as exc:
        return "tls_cert %s:%d: DNS failed (%s: %s)" % (h, p, type(exc).__name__, _clip(exc, 140))
    last = ""
    for fam, stype, proto, _cn, sa in info:
        if fam not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            sock = socket.socket(fam, stype, proto)
            sock.settimeout(8)
            sock.connect(sa)
            raw = ctx.wrap_socket(sock, server_hostname=h)
            break
        except Exception as exc:
            last = "%s: %s" % (type(exc).__name__, _clip(exc, 140))
            try:
                if raw is not None:
                    raw.close()
            except Exception:
                pass
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
            sock, raw = None, None
    if raw is None:
        return "tls_cert %s:%d: connect/handshake failed (%s)" % (h, p, last or "no address")
    try:
        cert = raw.getpeercert() or {}
        ver = raw.version()
        cipher = raw.cipher()
    finally:
        try:
            raw.close()
        except Exception:
            pass
    subject = _name("commonName", cert.get("subject")) or "(no CN)"
    issuer = _name("commonName", cert.get("issuer")) or "(unknown issuer)"
    sans = [str(v) for _t, v in (cert.get("subjectAltName") or [])]
    sample = sans[:6]
    head = "tls_cert %s:%d %s %s" % (h, p, ver or "?", cipher[0] if cipher else "?")
    lines = [head, "subject: %s" % subject, "issuer: %s" % issuer,
             "SAN (%d): %s%s" % (len(sans), ", ".join(sample),
                                 " ..." if len(sans) > len(sample) else "")]
    nb = _date(cert.get("notBefore"))
    na = _date(cert.get("notAfter"))
    if na is not None:
        days = int((time.mktime(na) - time.time()) / 86400.0)
        lines.append("notBefore: %s" % (time.strftime("%Y-%m-%d %H:%M:%S", nb) if nb else cert.get("notBefore")))
        lines.append("notAfter: %s" % time.strftime("%Y-%m-%d %H:%M:%S", na))
        lines.append("days_until_expiry: %d" % days)
        if days < 0:
            lines.append("WARNING: certificate is expired (%d days ago)" % abs(days))
        elif days < TLS_WARN_DAYS:
            lines.append("WARNING: expires in %d days (< %d)" % (days, TLS_WARN_DAYS))
    else:
        lines.append("notBefore: %s  notAfter: %s" % (cert.get("notBefore"), cert.get("notAfter")))
    return "\n".join(lines)


# ------------------------------------------------------------------ download_speed


@srv.tool("download_speed",
          "Download a URL (default: a small well-known file) and report throughput in Mbps; "
          "stops reading at bytes_limit.",
          {"type": "object",
           "properties": {"url": {"type": "string", "default": ""},
                          "bytes_limit": {"type": "integer", "default": 8388608},
                          "timeout_s": {"type": "number", "default": 30}},
           "required": []})
def download_speed(url="", bytes_limit=8388608, timeout_s=30):
    try:
        limit = max(4096, int(bytes_limit))
    except (TypeError, ValueError):
        limit = 8388608
    try:
        tmo = min(120.0, max(3.0, float(timeout_s)))
    except (TypeError, ValueError):
        tmo = 30.0
    cands = [_url(url)] if str(url).strip() else list(DL_URLS)
    errors = []
    for u in cands:
        t0 = time.perf_counter()
        got = 0
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=tmo) as r:
                while got < limit and time.perf_counter() - t0 < tmo:
                    chunk = r.read(min(65536, limit - got))
                    if not chunk:
                        break
                    got += len(chunk)
        except Exception as exc:
            errors.append("%s: %s: %s" % (u, type(exc).__name__, _clip(exc, 140)))
            continue
        secs = max(1e-6, time.perf_counter() - t0)
        mbps = (got * 8.0) / secs / 1e6
        return ("url=%s bytes=%d in %.2fs = %.2f Mbps (%.2f MB/s), limit=%d, timeout=%.0fs"
                % (u, got, secs, mbps, mbps / 8.0, limit, tmo))
    return "download_speed not available (no internet or blocked):\n" + "\n".join(errors)


# ---------------------------------------------------------------------- dns_records


def _rr_text(rec, rtype):
    if rtype == "MX":
        host = getattr(rec, "exchange", None)
        if host is None:
            m = re.search(r"([A-Za-z0-9_.-]+\.[A-Za-z]{2,})", str(rec))
            if m:
                host = m.group(1)
        pref = getattr(rec, "preference", None)
        if pref is None:
            pref = getattr(rec, "priority", "")
        return "%s %s" % (pref, host)
    if rtype == "TXT":
        strings = getattr(rec, "strings", None)
        if strings is not None:
            return "".join(s.decode("ascii", "replace") if isinstance(s, bytes) else str(s) for s in strings)
    return str(rec).strip()


def _nslookup(name, rtype, server):
    qt = str(rtype).upper()
    if qt not in ("A", "AAAA", "CNAME", "MX", "TXT", "NS"):
        return "dns_records: unsupported rtype %s (use A/AAAA/CNAME/MX/TXT/NS)" % rtype
    argv = ["nslookup", "-type=%s" % qt]
    if str(server).strip():
        argv.append(str(server).strip())
    argv.append(str(name).strip())
    rc, text = _run(argv, 20)
    if rc is None:
        return "dns_records %s: nslookup %s" % (name, text)
    addrs = []
    for line in (text or "").splitlines():
        line = line.strip()
        low = line.lower()
        if not line or line.startswith("Server:") or line.startswith("Address:") or ":" in line[:12]:
            continue
        if low.startswith(("addresses:", "address:", "\u5730\u5740", "\u522b\u540d", "\u540d\u79f0")):
            continue
        if any(tag in low for tag in ("nameserver", "primary name server", "responsible mail", "serial =",
                                      "refresh =", "retry =", "expire =", "default ttl")):
            continue
        tokens = line.split()
        if len(tokens) >= 2 and tokens[0].isalnum() and not tokens[0].isdigit():
            addrs.append("%s %s" % (tokens[0].lstrip("="), tokens[1]))
        elif len(tokens) == 1:
            addrs.append(tokens[0])
    if not addrs:
        low = (text or "").lower()
        if "can't find" in low or "cant find" in low or "non-existent" in low or "\u627e\u4e0d\u5230" in text:
            return "dns_records %s type=%s: NXDOMAIN / no records" % (name, qt)
        return "dns_records %s type=%s: no records parsed\n%s" % (name, qt, _clip(text, 300))
    out = "dns_records %s type=%s via nslookup (%d record(s))" % (name, qt, len(addrs))
    return out + "\n" + "\n".join(addrs[:24])


@srv.tool("dns_records",
          "Query A/AAAA/CNAME/MX/TXT/NS records via dnspython when present, otherwise via nslookup.",
          {"type": "object",
           "properties": {"name": {"type": "string"}, "rtype": {"type": "string", "default": "A"},
                          "server": {"type": "string", "default": ""}},
           "required": ["name"]})
def dns_records(name, rtype="A", server=""):
    n = str(name).strip()
    qt = (str(rtype).strip().upper() or "A")
    srv_ip = str(server).strip()
    if qt not in ("A", "AAAA", "CNAME", "MX", "TXT", "NS"):
        return "dns_records: unsupported rtype %s (use A/AAAA/CNAME/MX/TXT/NS)" % rtype
    try:
        import dns.resolver  # optional dependency
    except Exception:
        return _nslookup(n, qt, srv_ip)
    try:
        res = dns.resolver.Resolver()
        res.timeout = 4
        res.lifetime = 8
        if srv_ip:
            res.nameservers = [srv_ip]
        answers = res.resolve(n, qt)
        vals = [_rr_text(r, qt) for r in answers]
    except Exception as exc:
        return "dns_records %s type=%s failed: %s: %s" % (n, qt, type(exc).__name__, _clip(exc, 160))
    if not vals:
        return "dns_records %s type=%s: no records" % (n, qt)
    return "dns_records %s type=%s via dnspython (%d record(s))\n%s" % (
        n, qt, len(vals), "\n".join(vals[:24]))


# ----------------------------------------------------------------------- route_trace


ADDR_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4})+|[A-Za-z0-9][A-Za-z0-9._-]*\.[A-Za-z]{2,})\.?$")
BANNED = ("requesttimedout", "timedout", "notavailable", "unreachable", "general", "failure",
          "target", "trace", "complete", "hops", "maximum", "overamaximum", "unknown", "invalid")


def _is_hop_line(line):
    m = re.match(r"^(\d{1,3})\s+\S", line)
    if not m:
        return False
    if any(ord(ch) > 127 for ch in line):
        return False
    flat = re.sub(r"[^A-Za-z]", "", re.sub(r"\d+\s*ms", "", line)).lower()
    return not any(tag in flat for tag in BANNED)


def _is_addr(token):
    if not ADDR_RE.match(token):
        return False
    flat = re.sub(r"[^A-Za-z]", "", token).lower()
    return not any(tag in flat for tag in BANNED)


def _hops(text):
    """Parse ping/tracert-style hop lines: '<n>  <latency> ms  <address>'."""
    hops = []
    current = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith("ms") and ("<" in line or "=" in line) and re.search(r"\d+\s*ms", line):
            continue
        if _is_hop_line(line):
            m = re.match(r"^(\d{1,3})\s+(.*)$", line)
            current = {"n": m.group(1), "lat": [], "addr": ""}
            hops.append(current)
            line = m.group(2).strip()
        elif current is None:
            continue
        if not line:
            continue
        times = re.findall(r"([<>]?=?\s*\d+(?:\.\d+)?)\s*ms", line)
        for t in times:
            try:
                current["lat"].append(float(str(t).strip().lstrip("<>=").strip()))
            except ValueError:
                pass
        for token in reversed(line.split()):
            if token.endswith("ms") or _is_addr(token):
                if _is_addr(token):
                    current["addr"] = token
                break
    return hops


@srv.tool("route_trace", "Trace the route to a host with tracert and return hop / address / latency lines.",
          {"type": "object",
           "properties": {"host": {"type": "string"}, "max_hops": {"type": "integer", "default": 16},
                          "timeout_ms": {"type": "integer", "default": 800}},
           "required": ["host"]})
def route_trace(host, max_hops=16, timeout_ms=800):
    h = str(host).strip()
    try:
        nh = max(1, min(HOP_GUARD, int(max_hops)))
    except (TypeError, ValueError):
        nh = 16
    try:
        ms = max(100, min(5000, int(timeout_ms)))
    except (TypeError, ValueError):
        ms = 800
    if os.name == "nt":
        argv = ["tracert", "-d", "-4", "-h", str(nh), "-w", str(ms), h]
    else:
        argv = ["traceroute", "-n", "-m", str(nh), "-w", str(max(1, int(ms / 1000))), h]
    rc, text = _run(argv, nh * (ms / 1000.0) + 25)
    if rc is None:
        return "route_trace %s: %s" % (h, text)
    hops = _hops(text)
    if not hops:
        return "route_trace %s: no hops parsed (exit=%s)\n%s" % (h, rc, _clip(text, 300))
    lines = ["route_trace %s hops=%d max_hops=%d timeout=%dms" % (h, len(hops), nh, ms)]
    for hop in hops[:nh]:
        if hop["lat"]:
            lat = "%.1f/%.1f ms" % (min(hop["lat"]), max(hop["lat"]))
        else:
            lat = "*"
        lines.append("%s  %s  %s" % (hop["n"].ljust(2), lat.ljust(14), hop["addr"] or "*"))
    return "\n".join(lines[:nh + 2])


# -------------------------------------------------------------------------- ip_geo


@srv.tool("ip_geo",
          "Look up country/region/city/ISP for an IP via a public API; empty ip means this machine's public IP. "
          "Degrades to the error text when offline.",
          {"type": "object", "properties": {"ip": {"type": "string", "default": ""}}, "required": []})
def ip_geo(ip=""):
    addr = str(ip).strip()
    if not addr:
        for ep in ("https://api.ipify.org", "https://ifconfig.me/ip"):
            try:
                req = urllib.request.Request(ep, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=8) as r:
                    addr = r.read(64).decode("utf-8", errors="replace").strip()
                if addr:
                    break
            except Exception:
                addr = ""
    if not addr:
        return "ip_geo not available: could not determine the public IP (no internet?)"
    ip_only = addr
    m = re.match(r"\[?([0-9a-fA-F:.]+)\]?", addr)
    if m:
        ip_only = m.group(1)
    try:
        socket.inet_pton(socket.AF_INET, ip_only)
    except Exception:
        try:
            socket.inet_pton(socket.AF_INET6, ip_only.split("%")[0])
        except Exception:
            try:
                ip_only = socket.gethostbyname(addr)
            except Exception as exc:
                return "ip_geo: %s is not an IP and does not resolve (%s)" % (addr, _clip(exc, 120))
    for base in GEO_APIS:
        try:
            req = urllib.request.Request(base + ip_only, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read(8192).decode("utf-8", errors="replace")
        except Exception:
            continue
        try:
            import json
            data = json.loads(body)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get("status", "")).lower() == "fail":
            continue
        country = data.get("country") or data.get("country_name") or data.get("country_code") or "?"
        region = data.get("regionName") or data.get("region") or data.get("region_name") or "?"
        city = data.get("city") or "?"
        isp = _geo_text(data.get("isp") or data.get("org") or data.get("connection") or data.get("as") or "?")
        out = "ip_geo %s: %s | %s | %s | ISP=%s" % (data.get("query") or data.get("ip") or ip_only,
                                                    country, region, city, isp)
        if data.get("lat") is not None or data.get("latitude") is not None:
            latv = data.get("lat") if data.get("lat") is not None else data.get("latitude")
            lonv = data.get("lon") if data.get("lon") is not None else data.get("longitude")
            out += " | lat=%s lon=%s" % (latv, lonv)
        tz = _geo_text(data.get("timezone") if data.get("timezone") else data.get("time_zone"))
        if tz and tz != "?":
            out += " | tz=%s" % tz
        return out + " (via %s)" % base
    return "ip_geo not available for %s (no internet or every geo API refused)" % ip_only


# ---------------------------------------------------------------------- port_report


@srv.tool("port_report",
          "One line per port (open/closed/timeout) with the TCP connect latency in ms for a comma-separated list.",
          {"type": "object",
           "properties": {"host": {"type": "string"},
                          "ports": {"type": "string", "default": "21,22,80,443,3306,3389,5432,6379,8080"}},
           "required": ["host"]})
def port_report(host, ports="21,22,80,443,3306,3389,5432,6379,8080"):
    h = str(host).strip()
    raw = str(ports).strip() or "21,22,80,443,3306,3389,5432,6379,8080"
    plist = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            p = int(chunk)
        except ValueError:
            continue
        if 0 < p < 65536 and p not in plist:
            plist.append(p)
    plist = plist[:MAX_PORTS]
    if not plist:
        return "port_report: no valid ports in %r" % ports
    try:
        sa = socket.getaddrinfo(h, plist[0], type=socket.SOCK_STREAM)[0][4]
        peer = sa[0]
    except Exception as exc:
        return "port_report %s: DNS failed (%s: %s)" % (h, type(exc).__name__, _clip(exc, 140))

    def probe(port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.8)
        t0 = time.time()
        try:
            s.connect((peer, port))
            return port, "open", (time.time() - t0) * 1000.0
        except socket.timeout:
            return port, "timeout", (time.time() - t0) * 1000.0
        except Exception:
            return port, "closed", (time.time() - t0) * 1000.0
        finally:
            try:
                s.close()
            except Exception:
                pass

    results = {}
    with ThreadPoolExecutor(max_workers=min(32, len(plist))) as pool:
        for fut in as_completed([pool.submit(probe, p) for p in plist]):
            try:
                port, state, ms = fut.result()
            except Exception:
                continue
            results[port] = (state, ms)
    opened = [p for p in plist if results.get(p, ("", 0))[0] == "open"]
    head = "port_report %s (%s) ports=%d open=%d" % (h, peer, len(plist), len(opened))
    lines = [head]
    for p in plist:
        state, ms = results.get(p, ("error", 0.0))
        lines.append("%5d  %-7s  %6.0f ms" % (p, state, ms))
    if opened:
        lines.append("open: " + ", ".join(str(p) for p in opened))
    else:
        lines.append("open: none (closed = refused, timeout = filtered/unreachable)")
    return "\n".join(lines)


# --------------------------------------------------------------------- wifi_quality


def _parse_iface(text):
    """netsh wlan show interfaces -> list of {key: value} records."""
    records = []
    cur = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        if not key:
            continue
        if key in cur:
            if cur:
                records.append(cur)
            cur = {}
        cur[key] = val
    if cur:
        records.append(cur)
    return records


def _pick(rec, tags):
    """Exact key match first (tag, then hyphen/space variants), substring match as fallback."""
    for tag in tags:
        if tag in rec:
            return rec[tag]
        for key, val in rec.items():
            if key.replace("-", " ").strip().lower() == tag.lower():
                return val
    for tag in tags:
        for key, val in rec.items():
            if tag.lower() in key.lower():
                return val
    return ""


def _pick_token(rec, tag):
    """Match a key whose last whitespace token is tag (avoids 'Receive rate'/'Transmit rate' clashes)."""
    for key, val in rec.items():
        tokens = key.replace("-", " ").split()
        if tokens and tokens[-1].lower() == tag.lower():
            return val
    return ""


def _signal_pct(text):
    m = re.search(r"(\d{1,3})\s*%", text or "")
    if m:
        return max(0, min(100, int(m.group(1))))
    m = re.search(r"(\d{1,3})", text or "")
    if m:
        return max(0, min(100, int(m.group(1))))
    return -1


@srv.tool("wifi_quality",
          "Read the Wi-Fi adapter state with netsh (SSID, signal, band, channel, rate) and give a short verdict.",
          {"type": "object", "properties": {}, "required": []})
def wifi_quality():
    rc, text = _run(["netsh", "wlan", "show", "interfaces"], 20)
    if rc is None:
        return "wifi_quality: %s" % text
    if "not running" in (text or "").lower() or "\u670d\u52a1\u672a\u8fd0\u884c" in (text or ""):
        return "wifi_quality: WLAN AutoConfig service is not running, no wireless state available"
    records = _parse_iface(text)
    rec = None
    for cand in records:
        ssid = _pick(cand, ("SSID",))
        if ssid and "\u4e0d\u53ef\u7528" not in ssid and "BSSID" not in cand:
            rec = cand
            break
    if rec is None:
        if "disconnected" in (text or "").lower():
            return "wifi_quality: adapter present but not connected (no SSID)"
        return "wifi_quality: no wireless interface reported\n" + _clip(text, 300)
    ssid = _pick(rec, ("SSID",))
    state = _pick(rec, ("State", "\u72b6\u6001"))
    signal = _signal_pct(_pick(rec, ("Signal", "\u4fe1\u53f7")))
    band = _pick(rec, ("Band", "\u9891\u6bb5"))
    channel = _pick(rec, ("Channel", "\u4fe1\u9053"))
    rx = _pick_token(rec, "rate")
    tx = _pick(rec, ("Transmit rate", "\u4f20\u8f93\u901f\u7387"))
    if not rx and tx:
        rx = _pick(rec, ("Receive rate", "\u63a5\u6536\u901f\u7387"))
    rate = "rx=%s tx=%s" % (rx, tx) if (rx and tx) else (rx or tx)
    radio = _pick(rec, ("Radio type", "\u65e0\u7ebf\u7535\u7c7b\u578b"))
    bssid = _pick(rec, ("BSSID",))
    rssi = _pick(rec, ("RSSI",))
    auth = _pick(rec, ("Authentication", "\u8ba4\u8bc1"))
    lines = ["wifi_quality ssid=%s state=%s signal=%s%% band=%s channel=%s" % (
        ssid or "?", state or "?", signal if signal >= 0 else "?", band or radio or "?", channel or "?")]
    if rate:
        lines.append("rate: %s" % rate)
    if rssi:
        lines.append("rssi: %s" % rssi)
    if auth:
        lines.append("auth: %s" % auth)
    if bssid:
        lines.append("bssid: %s" % bssid)
    if signal < 0:
        verdict = "unknown: this build of netsh reported no signal percentage"
    elif signal >= 80:
        verdict = "excellent signal (>=80%): full rate is realistic"
    elif signal >= 60:
        verdict = "good signal (60-79%): stable for browsing and calls"
    elif signal >= 40:
        verdict = "weak signal (40-59%): expect retries and slower throughput"
    else:
        verdict = "poor signal (<40%): move closer to the AP or change channel"
    lines.append("verdict: %s" % verdict)
    if channel or band:
        lines.append("tuning: prefer a 5 GHz channel when available; on 2.4 GHz stick to 1/6/11")
    return "\n".join(lines)


def build():
    return srv


SAMPLE_DIR = os.path.join(sample_dir(), ".aiyu_selftest", "netcheck")

# Ordered by tool name; network tools that need the internet live in SAMPLES_OPTIONAL.
SAMPLES = {
    "ping_stats": {"host": "127.0.0.1", "count": 2, "timeout_ms": 1000},
    "wifi_quality": {},
    "port_report": {"host": "127.0.0.1", "ports": "135,445,8080"},
}

SAMPLES_OPTIONAL = {
    "http_timing": {"url": "https://example.com", "count": 2},
    "tls_cert": {"host": "example.com", "port": 443},
    "download_speed": {"url": "https://speed.cloudflare.com/__down?bytes=262144", "bytes_limit": 262144, "timeout_s": 10},
    "dns_records": {"name": "example.com", "rtype": "A", "server": ""},
    "route_trace": {"host": "1.1.1.1", "max_hops": 8, "timeout_ms": 600},
    "ip_geo": {"ip": "1.1.1.1"},
}


if __name__ == "__main__":
    srv.run()
