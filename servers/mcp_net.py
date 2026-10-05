"""MCP server: network diagnostics -- DNS, TCP probing, HTTP, local configuration.

Pure stdlib (socket/urllib/concurrent.futures); ping and the Get-Net* queries go
through subprocess. public_ip degrades to the error text when there is no internet.
"""
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
PS = "powershell" if os.name == "nt" else "pwsh"
PS_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
PORT_SCAN_CAP = 256
LAN_HOSTS = 254
LAN_PORTS = (445, 3389, 80, 22)
LAN_BUDGET_S = 8.0

srv = Server("net")


def _ps(script, timeout_s=60):
    try:
        p = subprocess.run(
            [PS, "-NoProfile", "-NonInteractive", "-Command", PS_PREFIX + script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=float(timeout_s), creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return "not available: PowerShell call timed out after %ss" % timeout_s
    except Exception as exc:
        return "not available: %s" % exc
    out = (p.stdout or "").strip()
    if p.stderr and p.stderr.strip():
        out += "\n[stderr] " + p.stderr.strip()[:300]
    return out if out else "(no output)"


def _parse_ports(ports):
    out = []
    for chunk in str(ports or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            p = int(chunk)
        except ValueError:
            continue
        if 0 < p < 65536:
            out.append(p)
    return out[:PORT_SCAN_CAP]


@srv.tool("dns_lookup", "Resolve a hostname to IPv4/IPv6 addresses (getaddrinfo).",
          {"type": "object", "properties": {"host": {"type": "string"}}, "required": ["host"]})
def dns_lookup(host):
    h = str(host).strip()
    try:
        infos = socket.getaddrinfo(h, None)
    except Exception as exc:
        return "dns error for %s: %s: %s" % (h, type(exc).__name__, exc)
    addrs = []
    for fam, _t, _p, _c, sa in infos:
        kind = "v4" if fam == socket.AF_INET else ("v6" if fam == socket.AF_INET6 else str(fam))
        if (kind, sa[0]) not in addrs:
            addrs.append((kind, sa[0]))
    if not addrs:
        return "no addresses for %s" % h
    return "%s resolves to %d address(es)\n%s" % (h, len(addrs), "\n".join("%s %s" % (k, a) for k, a in addrs))


@srv.tool("reverse_dns", "Resolve an IP address back to a hostname (PTR).",
          {"type": "object", "properties": {"ip": {"type": "string"}}, "required": ["ip"]})
def reverse_dns(ip):
    a = str(ip).strip()
    try:
        name, aliases, _addrs = socket.gethostbyaddr(a)
    except Exception as exc:
        return "reverse dns failed for %s: %s: %s" % (a, type(exc).__name__, exc)
    out = "%s -> %s" % (a, name)
    if aliases:
        out += "\naliases: %s" % ", ".join(aliases)
    return out


@srv.tool("tcp_check", "Try one TCP connect and report open/closed/timeout plus latency in ms.",
          {"type": "object", "properties": {"host": {"type": "string"}, "port": {"type": "integer"},
                                            "timeout_s": {"type": "number", "default": 3}}, "required": ["host", "port"]})
def tcp_check(host, port, timeout_s=3):
    h = str(host).strip()
    p = int(port)
    t0 = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(float(timeout_s))
    try:
        s.connect((h, p))
        ms = int((time.time() - t0) * 1000)
        return "open: %s:%d (%d ms)" % (h, p, ms)
    except socket.timeout:
        return "timeout: %s:%d after %ss (filtered or unreachable)" % (h, p, timeout_s)
    except ConnectionRefusedError:
        return "closed: %s:%d refused (%d ms)" % (h, p, int((time.time() - t0) * 1000))
    except Exception as exc:
        return "error: %s:%d %s: %s" % (h, p, type(exc).__name__, exc)
    finally:
        s.close()


@srv.tool("port_scan",
          "Scan a comma-separated port list on one host with concurrent TCP connects (hard cap 256 ports). "
          "Prints which ports are open.",
          {"type": "object", "properties": {"host": {"type": "string"},
                                            "ports": {"type": "string", "default": "22,80,443,3389"},
                                            "timeout_s": {"type": "number", "default": 0.6}}, "required": ["host"]})
def port_scan(host, ports="22,80,443,3389", timeout_s=0.6):
    h = str(host).strip()
    plist = _parse_ports(ports)
    if not plist:
        return "no valid ports in: %s" % ports
    t0 = time.time()
    open_ports = []

    def probe(p):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(float(timeout_s))
        try:
            s.connect((h, p))
            return p
        except Exception:
            return None
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=min(64, len(plist))) as pool:
        for res in as_completed([pool.submit(probe, p) for p in plist]):
            p = res.result()
            if p is not None:
                open_ports.append(p)
    open_ports.sort()
    head = "%s: %d/%d open in %.1fs (timeout %ss/port)" % (h, len(open_ports), len(plist), time.time() - t0, timeout_s)
    if not open_ports:
        return head + "\nno open ports"
    return head + "\nopen: " + ", ".join(str(p) for p in open_ports)


@srv.tool("ping_host", "Ping a host with the OS ping tool and return the reply summary.",
          {"type": "object", "properties": {"host": {"type": "string"}, "count": {"type": "integer", "default": 3}},
           "required": ["host"]})
def ping_host(host, count=3):
    h = str(host).strip()
    n = max(1, min(10, int(count)))
    if os.name == "nt":
        argv = ["ping", "-n", str(n), "-w", "2000", h]
    else:
        argv = ["ping", "-c", str(n), "-W", "2", h]
    try:
        p = subprocess.run(argv, capture_output=True, timeout=float(n * 3 + 5),
                           creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return "ping %s timed out (whole run)" % h
    except Exception as exc:
        return "ping %s failed: %s" % (h, exc)
    raw = p.stdout or b""
    for enc in ("utf-8", "cp936", "mbcs"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            text = raw.decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    keep = [ln for ln in lines if ("TTL=" in ln.upper() or "ms" in ln or "100%" in ln or "loss" in ln.lower())]
    return "exit=%s\n%s" % (p.returncode, "\n".join(keep[:12]) or "\n".join(lines[:8]))


@srv.tool("http_status", "Fetch a URL and report the HTTP status, final URL and body length (10s timeout).",
          {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})
def http_status(url):
    u = str(url).strip()
    if "://" not in u:
        u = "http://" + u
    req = urllib.request.Request(u, headers={"User-Agent": "mcpserver-net/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(4096)
            return "status=%s reason=%s final_url=%s bytes_read=%d server=%s" % (
                r.status, r.reason, r.geturl(), len(body), r.headers.get("Server", "?"))
    except urllib.error.HTTPError as exc:
        return "status=%s reason=%s url=%s (HTTPError, the server answered)" % (exc.code, exc.reason, u)
    except Exception as exc:
        return "http error for %s: %s: %s" % (u, type(exc).__name__, exc)


@srv.tool("public_ip", "Ask a public service for this machine's outward-facing IP. Returns the error text when offline.",
          {"type": "object", "properties": {}, "required": []})
def public_ip():
    endpoints = ["https://api.ipify.org", "https://ifconfig.me/ip", "https://ipinfo.io/ip"]
    errors = []
    for ep in endpoints:
        try:
            req = urllib.request.Request(ep, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=8) as r:
                ip = r.read(128).decode("utf-8", errors="replace").strip()
            if ip:
                return "public_ip=%s (via %s)" % (ip, ep)
            errors.append("%s: empty response" % ep)
        except Exception as exc:
            errors.append("%s: %s: %s" % (ep, type(exc).__name__, exc))
    return "public_ip not available (no internet or blocked):\n" + "\n".join(errors)


@srv.tool("ip_config", "Readable Get-NetIPConfiguration output: adapters, IPv4/IPv6 addresses, gateways, DNS.",
          {"type": "object", "properties": {}, "required": []})
def ip_config():
    return _ps(
        "$c=Get-NetIPConfiguration -ErrorAction SilentlyContinue;"
        "if (-not $c) { 'ip config: not available (Get-NetIPConfiguration failed)' } else {"
        "$c | ForEach-Object {"
        "$ips=($_.IPv4Address.IPAddress -join ','); $gws=($_.IPv4DefaultGateway.NextHop -join ',');"
        "$dns=(Get-DnsClientServerAddress -InterfaceIndex $_.InterfaceIndex -AddressFamily 2 "
        "-ErrorAction SilentlyContinue).ServerAddresses -join ',';"
        "'adapter ' + $_.InterfaceAlias + ' | ' + $_.InterfaceDescription + ' | status=' + $_.NetAdapter.Status + "
        "' | ipv4=' + $ips + ' | gateway=' + $gws + ' | dns=' + $dns } }"
    )


@srv.tool("route_table", "First 40 rows of Get-NetRoute (destination, next hop, interface, metric).",
          {"type": "object", "properties": {}, "required": []})
def route_table():
    return _ps(
        "$r=Get-NetRoute -ErrorAction SilentlyContinue | Select-Object -First 40;"
        "if (-not $r) { 'routes: not available (Get-NetRoute failed)' } else { $r | ForEach-Object { "
        "'route dest=' + $_.DestinationPrefix + ' nexthop=' + $_.NextHop + ' if=' + $_.InterfaceAlias + "
        "' metric=' + $_.RouteMetric + ' proto=' + $_.Protocol } }"
    )


@srv.tool("arp_table", "Get-NetNeighbor output: IP, MAC, state and interface.",
          {"type": "object", "properties": {}, "required": []})
def arp_table():
    return _ps(
        "$n=Get-NetNeighbor -ErrorAction SilentlyContinue | Where-Object { $_.State -ne 'Unreachable' } "
        "| Select-Object -First 60;"
        "if (-not $n) { 'arp: not available (Get-NetNeighbor failed)' } else { $n | ForEach-Object { "
        "'neighbor ' + $_.IPAddress + ' mac=' + $_.LinkLayerAddress + ' state=' + $_.State + ' if=' + $_.InterfaceAlias } }"
    )


@srv.tool("lan_scan",
          "Sweep <subnet>.1-254 for TCP 445/3389/80/22 with threads, an 8s total budget and a 254-host cap; "
          "prints which hosts answered on which port.",
          {"type": "object", "properties": {"subnet": {"type": "string", "default": "192.168.1"},
                                            "timeout_ms": {"type": "integer", "default": 400}}, "required": []})
def lan_scan(subnet="192.168.1", timeout_ms=400):
    base = str(subnet).strip().rstrip(".")
    if base.count(".") == 3:
        base = base.rsplit(".", 1)[0]
    if base.count(".") != 2:
        return "subnet must look like 192.168.1 (got %s)" % subnet
    tmo = max(50, min(3000, int(timeout_ms))) / 1000.0
    deadline = time.time() + LAN_BUDGET_S
    hits = {}
    order = []

    def probe(item):
        host, port = item
        if time.time() > deadline:
            return None
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(tmo)
        try:
            s.connect(("%s.%d" % (base, host), port))
            return (host, port)
        except Exception:
            return None
        finally:
            s.close()

    jobs = [(h, p) for h in range(1, LAN_HOSTS + 1) for p in LAN_PORTS]
    workers = min(128, len(jobs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(probe, j) for j in jobs]
        for fut in as_completed(futures):
            if time.time() > deadline and len(hits) >= 0:
                break
            res = fut.result()
            if res:
                host, port = res
                hits.setdefault(host, []).append(port)

    for h in sorted(hits):
        order.append("host %s.%d ports=%s" % (base, h, ",".join(str(p) for p in sorted(hits[h]))))
    head = "%s.1-254 TCP sweep %s in %.1fs (budget %ds, timeout %dms): %d host(s) answered" % (
        base, ",".join(str(p) for p in LAN_PORTS), LAN_BUDGET_S, LAN_BUDGET_S, int(tmo * 1000), len(hits))
    if not order:
        return head + "\nno hosts answered (try another subnet, or the hosts block these ports)"
    return head + "\n" + "\n".join(order)


def build():
    return srv


# Safe samples: read-only network queries against stable targets.
SAMPLES = {
    "dns_lookup": {"host": "localhost"},
    "reverse_dns": {"ip": "127.0.0.1"},
    "tcp_check": {"host": "127.0.0.1", "port": 135, "timeout_s": 2},
    "port_scan": {"host": "127.0.0.1", "ports": "135,445", "timeout_s": 0.5},
    "ping_host": {"host": "127.0.0.1", "count": 2},
    "http_status": {"url": "http://example.com"},
    "public_ip": {},
    "ip_config": {},
    "route_table": {},
    "arp_table": {},
}


if __name__ == "__main__":
    srv.run()
