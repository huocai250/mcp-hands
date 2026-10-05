#!/usr/bin/env python3
"""mcp-hands control panel — a tkinter GUI around bridge.py.  https://github.com/huocai250/mcp-hands (MIT)

Modes (decided from the command line):
  --mcp-server NAME     run one bundled MCP stdio server (used by bridge.py itself)
  --tools/--self-test/--doctor   run a diagnostic in this console, then exit
  (nothing)             open the GUI: start/stop, edit config, live log, one-click tests

Packed as a console exe whose window is hidden at startup, so the GUI looks
native while child processes still get real stdio handles.
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

CONFIG_PATH = os.environ.get("BRIDGE_CONFIG") or os.path.join(BASE_DIR, "bridge.config.json")
os.environ["BRIDGE_CONFIG"] = CONFIG_PATH

ARGV = sys.argv[1:]

if "--mcp-server" in ARGV:
    if FROZEN:
        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
    from server_host import run_server
    run_server(ARGV[ARGV.index("--mcp-server") + 1])
    raise SystemExit(0)

import bridge  # noqa: E402
import proxy as proxy_module  # noqa: E402


def lan_ip():
    """The address the phone should use to reach this PC."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        sock.close()


def hide_console():
    if FROZEN or os.name == "nt":
        try:
            handle = ctypes.windll.kernel32.GetConsoleWindow()
            if handle:
                ctypes.windll.user32.ShowWindow(handle, 0)
        except Exception:  # noqa: BLE001
            pass


CLI_FLAGS = ("--tools", "--self-test", "--doctor", "--version")


def main():
    wanted = [flag for flag in CLI_FLAGS if flag in ARGV]
    if wanted:
        raise SystemExit(bridge.run_command(wanted[0]))
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("tkinter is not available in this build; use the CLI flags instead")
        raise SystemExit(3)
    hide_console()
    app = Console(run_server=any(flag in ARGV for flag in ("--serve", "--autostart", "--proxy")))
    if "--proxy" in ARGV:
        app.proxy_enabled.set(True)
    app.mainloop()


# --------------------------------------------------------------------------- UI
from tkinter import (BOTH, END, HORIZONTAL, LEFT, RIGHT, VERTICAL, W, X, Y, BooleanVar, Canvas,  # noqa: E402
                     StringVar, Tk, Toplevel, filedialog, messagebox, scrolledtext, ttk)

SETTINGS_PATH = os.path.join(BASE_DIR, "gui-settings.json")

ABOUT_TEXT = """%(name)s  v%(version)s

%(tagline)s

作者   : %(author)s
仓库   : %(url)s
协议   : %(license)s License（可自由使用、修改、商用，保留版权声明即可）

当前工具：%(tools)s 个 / %(servers)s 个 MCP server
配置示例：configs/bridge.config.example.json
命令行 : --tools  --self-test  --doctor  --version  --init  --mcp-server <name>"""

SERVERS = ("fs", "shell", "web", "sys", "office", "media", "archive", "sqlite",
           "desktop", "voice", "monitor", "net", "dev", "forensics",
           "text", "pdf", "qr", "backup", "http", "media2", "sched", "soft",
           "registry", "netadv")


class _Writer:
    """Redirect print() from in-process commands into the log pane."""

    def __init__(self, sink):
        self.sink = sink

    def write(self, text):
        for line in str(text).splitlines():
            if line.strip():
                self.sink(line)

    def flush(self):
        pass


class Console(Tk):
    def __init__(self, run_server=False):
        super().__init__()
        self.title("%s · aiyu MCP 桥接控制台" % bridge.APP_NAME)
        self.geometry(self._load_settings().get("geometry") or "860x860")
        self.minsize(520, 380)
        self.server = None
        self.proxy_service = None
        self.log_window = None
        self.log_window_text = None
        self.busy = False
        self.config = bridge.CFG
        self.server_vars = {}
        self._build()
        bridge.add_log_sink(self._log_line)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(400, self._tick)
        if run_server:
            self.after(300, self.start_bridge)

    def on_close(self):
        """Closing the window stops the bridge (and every MCP child) cleanly."""
        if self.server and not messagebox.askyesno("关闭桥接", "关闭窗口会同时停止桥接服务，手机端将无法调用电脑工具。\n\n确定要关闭吗？"):
            return
        try:
            if self.server:
                self.server.shutdown()
                self.server.server_close()
                self.server = None
            if self.proxy_service:
                self.proxy_service.stop()
                self.proxy_service = None
            bridge.stop_hub()
        except Exception:  # noqa: BLE001
            pass
        self._save_settings()
        self.destroy()

    # ------------------------------------------------------------------ layout
    def _build(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except Exception:  # noqa: BLE001
            pass

        head = ttk.Frame(self, padding=(12, 10, 12, 4))
        head.pack(fill=X)
        self.status = ttk.Label(head, text="● 已停止", font=("Microsoft YaHei UI", 15, "bold"), foreground="#b00")
        self.status.pack(side=LEFT)
        self.detail = ttk.Label(head, text="", foreground="#555")
        self.detail.pack(side=LEFT, padx=12)

        addr = ttk.Frame(self, padding=(12, 0, 12, 6))
        addr.pack(fill=X)
        self.addr_var = StringVar()
        ttk.Label(addr, text="客户端填：", foreground="#555").pack(side=LEFT)
        ttk.Label(addr, textvariable=self.addr_var, font=("Consolas", 10)).pack(side=LEFT)
        ttk.Button(addr, text="复制", width=6, command=self.copy_addr).pack(side=LEFT, padx=6)
        ttk.Label(addr, text="(API 类型 OpenAI 兼容，Key 任意，模型 aiyu-proxy)", foreground="#888").pack(side=LEFT)

        bar = ttk.Frame(self, padding=(12, 4))
        bar.pack(fill=X)
        self.start_btn = ttk.Button(bar, text="启动", command=self.start_bridge)
        self.stop_btn = ttk.Button(bar, text="停止", command=self.stop_bridge, state="disabled")
        for widget in (self.start_btn, self.stop_btn):
            widget.pack(side=LEFT, padx=(0, 6))
        for text, cmd in (("工具清单", lambda: self.run_cmd("tools")),
                          ("自检", lambda: self.run_cmd("self-test")),
                          ("体检", lambda: self.run_cmd("doctor")),
                          ("测试对话", self.test_chat),
                          ("关于", self.show_about)):
            ttk.Button(bar, text=text, command=cmd).pack(side=LEFT, padx=(0, 6))
        ttk.Button(bar, text="清空日志", command=self.clear_log).pack(side=RIGHT)
        ttk.Button(bar, text="打开日志", command=self.open_log).pack(side=RIGHT, padx=6)

        # Settings live in a scrollable area inside a draggable split, so shrinking the
        # window never hides a control and the log always stays reachable.
        paned = self.paned = ttk.PanedWindow(self, orient=VERTICAL)
        paned.pack(fill=BOTH, expand=True, padx=12, pady=(2, 6))
        holder = ttk.Frame(paned)
        paned.add(holder, weight=3)
        canvas = Canvas(holder, highlightthickness=0, height=220, yscrollincrement=20)
        scroll = ttk.Scrollbar(holder, orient=VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.pack(side=RIGHT, fill=Y)
        body = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))

        def on_wheel(event):
            target = self.winfo_containing(event.x_root, event.y_root)
            if target is not None and str(target).startswith(str(self.log)):
                return None  # the log pane scrolls itself
            canvas.yview_scroll(int(-event.delta / 120), "units")
            return None

        self.bind_all("<MouseWheel>", on_wheel)

        cfg = ttk.LabelFrame(body, text="配置（改完点保存）", padding=8)
        cfg.pack(fill=X, pady=(6, 4))
        self.fields = {}
        rows = (("base_url", "上游地址"), ("api_key", "API Key"), ("model", "模型名"),
                ("port", "监听端口"), ("roots", "文件沙箱目录"))
        for index, (key, label) in enumerate(rows):
            ttk.Label(cfg, text=label, width=12).grid(row=index, column=0, sticky=W, pady=2)
            var = StringVar()
            self.fields[key] = var
            ttk.Entry(cfg, textvariable=var).grid(row=index, column=1, sticky="ew", pady=2)
        cfg.columnconfigure(1, weight=1)
        bottom = ttk.Frame(cfg)
        bottom.grid(row=len(rows), column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(bottom, text="保存并重启", command=self.save_and_restart).pack(side=LEFT)
        self.autostart = BooleanVar(value=self._autostart_on())
        ttk.Checkbutton(bottom, text="开机自动启动（静默）", variable=self.autostart,
                        command=self.toggle_autostart).pack(side=LEFT, padx=12)
        ttk.Button(bottom, text="打开配置目录", command=lambda: os.startfile(BASE_DIR)).pack(side=RIGHT)

        pin = ttk.LabelFrame(body, text="App 直连代理（把手机 App 的模型服务商指到这里，人设自带原生工具调用）", padding=8)
        pin.pack(fill=X, pady=(2, 4))
        top = ttk.Frame(pin)
        top.pack(fill=X)
        self.proxy_enabled = BooleanVar()
        ttk.Checkbutton(top, text="启用", variable=self.proxy_enabled).pack(side=LEFT)
        ttk.Label(top, text="端口").pack(side=LEFT, padx=(10, 2))
        self.proxy_port = StringVar(value="8890")
        ttk.Entry(top, textvariable=self.proxy_port, width=7).pack(side=LEFT)
        ttk.Label(top, text="上游地址").pack(side=LEFT, padx=(10, 2))
        self.proxy_upstream = StringVar()
        ttk.Entry(top, textvariable=self.proxy_upstream).pack(side=LEFT, fill=X, expand=True)
        row2 = ttk.Frame(pin)
        row2.pack(fill=X, pady=(4, 0))
        self.proxy_url = StringVar()
        ttk.Label(row2, text="App 里填：", foreground="#555").pack(side=LEFT)
        ttk.Label(row2, textvariable=self.proxy_url, font=("Consolas", 10)).pack(side=LEFT)
        ttk.Button(row2, text="复制", width=6, command=self.copy_proxy_url).pack(side=LEFT, padx=6)
        ttk.Button(row2, text="放行防火墙端口", command=self.allow_firewall).pack(side=LEFT)
        ttk.Label(row2, text="（密钥填你自己的上游 key，模型名填上游模型，如 deepseek-flash）",
                  foreground="#888").pack(side=LEFT, padx=6)

        self.proxy_mode = BooleanVar(value=self.config.get("upstream_history") == "full")
        ttk.Checkbutton(cfg, text="代理模式：让手机 App 直接把上游指向本机（这样手机上发消息也能用电脑工具）"
                                 " — 上面的「上游地址」要填真实模型服务（如 https://api.deepseek.com/v1）",
                        variable=self.proxy_mode, command=self._update_addr).grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky=W, pady=(6, 0))

        tools = ttk.LabelFrame(body, text="启用的工具服务", padding=8)
        tools.pack(fill=X, pady=(2, 4))
        for index, name in enumerate(SERVERS):
            var = BooleanVar()
            self.server_vars[name] = var
            ttk.Checkbutton(tools, text=name, variable=var).grid(row=index // 7, column=index % 7, sticky=W, padx=4)

        log_box = ttk.LabelFrame(paned, text="运行日志", padding=6)
        paned.add(log_box, weight=4)
        self.log = scrolledtext.ScrolledText(log_box, height=10, wrap="none", font=("Consolas", 9))
        self.log.pack(fill=BOTH, expand=True)
        ttk.Button(log_box, text="日志单独窗口", command=self.detach_log).pack(anchor="e", pady=(4, 0))
        version = ttk.Label(log_box, text=bridge.version_line(), foreground="#888")
        version.pack(anchor="w")
        version.bind("<Button-1>", lambda _e: webbrowser.open(bridge.APP_URL))
        self._load_config_into_ui()
        self.after(300, self._init_sash)

    def show_about(self):
        """作者 / 仓库 / 协议 信息，链接可点。"""
        hub = bridge.HUB
        text = ABOUT_TEXT % {
            "name": bridge.APP_NAME, "version": bridge.APP_VERSION, "tagline": bridge.APP_TAGLINE,
            "author": bridge.APP_AUTHOR, "url": bridge.APP_URL, "license": bridge.APP_LICENSE,
            "tools": len(hub.specs) if hub else "261+", "servers": len(hub.servers) if hub else len(SERVERS),
        }
        win = Toplevel(self)
        win.title("关于 %s" % bridge.APP_NAME)
        win.geometry("560x320")
        win.transient(self)
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill=BOTH, expand=True)
        ttk.Label(frame, text="%s v%s" % (bridge.APP_NAME, bridge.APP_VERSION),
                  font=("Microsoft YaHei UI", 14, "bold")).pack(anchor=W)
        ttk.Label(frame, text=bridge.APP_TAGLINE, foreground="#555").pack(anchor=W, pady=(2, 10))
        body = ttk.Label(frame, text=text.split("\n", 2)[2], justify=LEFT)
        body.pack(anchor=W)
        link = ttk.Label(frame, text=bridge.APP_URL, foreground="#0a58ca", cursor="hand2")
        link.pack(anchor=W, pady=(8, 0))
        link.bind("<Button-1>", lambda _e: webbrowser.open(bridge.APP_URL))
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill=X, pady=(10, 0))
        ttk.Button(buttons, text="打开 GitHub 仓库", command=lambda: webbrowser.open(bridge.APP_URL)).pack(side=LEFT)
        ttk.Button(buttons, text="复制仓库地址", command=self.copy_repo_url).pack(side=LEFT, padx=8)
        ttk.Button(buttons, text="关闭", command=win.destroy).pack(side=RIGHT)
        self.say("关于：%s" % bridge.version_line())

    def copy_repo_url(self):
        self.clipboard_clear()
        self.clipboard_append(bridge.APP_URL)
        self.say("已复制仓库地址：%s" % bridge.APP_URL)

    def detach_log(self):
        """Open the log in its own resizable window (handy on small screens)."""
        if getattr(self, "log_window", None) and self.log_window.winfo_exists():
            self.log_window.lift()
            return
        win = Toplevel(self)
        win.title("aiyu MCP 运行日志")
        win.geometry("900x500")
        text = scrolledtext.ScrolledText(win, wrap="none", font=("Consolas", 9))
        text.pack(fill=BOTH, expand=True)
        text.insert(END, self.log.get("1.0", END))
        text.see(END)
        self.log_window, self.log_window_text = win, text

    def _load_settings(self):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_settings(self):
        try:
            position = int(self.paned.sashpos(0))
        except Exception:  # noqa: BLE001
            position = 0
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
                json.dump({"geometry": self.geometry(), "sash": position}, fh)
        except OSError:
            pass

    def _init_sash(self):
        """Give the log a fair share of the window and remember the user's split."""
        height = self.paned.winfo_height()
        if height < 80:
            self.after(250, self._init_sash)
            return
        saved = self._load_settings().get("sash") or 0
        target = saved if 40 < saved < height - 80 else int(height * 0.52)
        try:
            self.paned.sashpos(0, target)
        except Exception:  # noqa: BLE001
            pass

    def _load_config_into_ui(self):
        upstream = self.config.get("upstream", {})
        self.fields["base_url"].set(upstream.get("base_url", ""))
        self.fields["api_key"].set(upstream.get("api_key", ""))
        self.fields["model"].set(upstream.get("model", ""))
        self.fields["port"].set(str((self.config.get("listen") or {}).get("port", 8877)))
        roots = ""
        for server in self.config.get("servers", []):
            if server.get("name") == "fs":
                roots = (server.get("env") or {}).get("MCP_FS_ROOTS", "")
        self.fields["roots"].set(roots)
        self.proxy_mode.set(self.config.get("upstream_history") == "full")
        prox = self.config.get("proxy") or {}
        self.proxy_enabled.set(bool(prox.get("enabled", False)))
        self.proxy_port.set(str((prox.get("listen") or {}).get("port", 8890)))
        self.proxy_upstream.set(prox.get("upstream_base") or "https://api.deepseek.com/v1")
        enabled = {s.get("name") for s in self.config.get("servers", []) if s.get("enabled", True) is not False}
        for name, var in self.server_vars.items():
            var.set(name in enabled)
        self._update_addr()

    def _update_addr(self):
        port = self.fields["port"].get().strip() or "8877"
        self.addr_var.set("http://%s:%s/v1" % (self._lan_ip() if self.proxy_mode.get() else "127.0.0.1", port))
        self._update_proxy_url()

    def _update_proxy_url(self):
        self.proxy_url.set("http://%s:%s/v1" % (self._lan_ip(), self.proxy_port.get().strip() or "8890"))

    def copy_proxy_url(self):
        self.clipboard_clear()
        self.clipboard_append(self.proxy_url.get())
        self.say("已复制 App 用地址：%s（模型服务商选「自定义」，密钥填你自己的上游 key）" % self.proxy_url.get())

    def allow_firewall(self):
        """放行入站端口，需要管理员权限，会弹 UAC。"""
        script_path = os.path.join(BASE_DIR, "open-firewall.ps1")
        if not os.path.exists(script_path):
            self.say("找不到 open-firewall.ps1")
            return
        command = ("Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass',"
                   "'-File','%s','-Ports','%s,%s'" % (script_path, self.proxy_port.get().strip() or "8890",
                                                      self.fields["port"].get().strip() or "8877"))
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-Command", command],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.say("已弹出 UAC 窗口：同意后会放行 TCP %s / %s 的入站连接（手机才连得上本机）"
                     % (self.proxy_port.get(), self.fields["port"].get()))
        except Exception as exc:  # noqa: BLE001
            self.say("放行失败：%s" % exc)

    @staticmethod
    def _lan_ip():
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except Exception:  # noqa: BLE001
            return "127.0.0.1"
        finally:
            sock.close()

    # ------------------------------------------------------------------- state
    def _tick(self):
        running = self.server is not None
        hub = bridge.HUB
        tools = len(hub.specs) if hub else 0
        servers = len(hub.servers) if hub else 0
        if self.busy:
            self.status.configure(text="● 启动中…", foreground="#d97706")
        elif running:
            self.status.configure(text="● 运行中", foreground="#15803d")
        else:
            self.status.configure(text="● 已停止", foreground="#b00")
        self.detail.configure(text="工具 %d 个 / 服务 %d 个   %s" % (tools, servers, bridge.CONFIG_PATH))
        self.start_btn.configure(state="disabled" if (running or self.busy) else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.after(1500, self._tick)

    def _log_line(self, line):
        try:
            self.log.insert(END, line + "\n")
            if int(self.log.index("end-1c").split(".")[0]) > 3000:
                self.log.delete("1.0", "200.0")
            self.log.see(END)
            if self.log_window is not None and self.log_window.winfo_exists():
                self.log_window_text.insert(END, line + "\n")
                self.log_window_text.see(END)
        except Exception:  # noqa: BLE001
            pass

    def say(self, text):
        self._log_line("[gui] %s" % text)

    # -------------------------------------------------------------- operations
    def start_bridge(self):
        if self.server or self.busy:
            return
        self.busy = True
        self.say("正在启动：加载配置 → 拉起 MCP 服务 → 监听端口")

        def worker():
            from http.server import ThreadingHTTPServer
            try:
                bridge.log(bridge.version_line())
                bridge.reload_config(CONFIG_PATH)
                host = bridge.CFG["listen"]["host"]
                port = int(bridge.CFG["listen"]["port"])
                bridge.start_hub()
                server = ThreadingHTTPServer((host, port), bridge.Handler)
                server.daemon_threads = True
                self.server = server
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.say("已在 http://%s:%d/v1 监听，客户端照此填写" % (host, port))
                if self.proxy_enabled.get():
                    self.proxy_service = proxy_module.ProxyService().start()
                    if self.proxy_service.running:
                        self.say("App 直连代理已启动：%s （模型服务商选「自定义」→ 填这个地址 + 你自己的上游 key）"
                                 % self.proxy_url.get())
            except OSError as exc:
                self.say("启动失败：%s（端口被占用？换一个端口再试）" % exc)
            except Exception as exc:  # noqa: BLE001
                self.say("启动失败：%s: %s" % (type(exc).__name__, exc))
            finally:
                self.busy = False

        threading.Thread(target=worker, daemon=True).start()

    def stop_bridge(self):
        server, self.server = self.server, None
        if self.proxy_service:
            self.proxy_service.stop()
            self.proxy_service = None
        if server:
            self.say("正在停止…")

            def worker():
                try:
                    server.shutdown()
                    server.server_close()
                except Exception as exc:  # noqa: BLE001
                    self.say("停止时出错：%s" % exc)
                bridge.stop_hub()
                self.say("已停止，所有 MCP 子进程已退出")

            threading.Thread(target=worker, daemon=True).start()

    def run_cmd(self, name):
        self.say("开始执行 %s（输出见下方）" % name)

        def worker():
            old = sys.stdout
            sys.stdout = _Writer(self._log_line)
            try:
                code = bridge.run_command(name)
                self.say("%s 结束，退出码 %s" % (name, code))
            except Exception as exc:  # noqa: BLE001
                self.say("%s 异常：%s: %s" % (name, type(exc).__name__, exc))
            finally:
                sys.stdout = old

        threading.Thread(target=worker, daemon=True).start()

    def test_chat(self):
        if not self.server:
            messagebox.showinfo("提示", "先点「启动」，再测试对话")
            return
        self.say("向本机桥接发一条测试消息…")

        def worker():
            port = self.fields["port"].get().strip() or "8877"
            body = json.dumps({"model": "aiyu-proxy", "stream": False,
                               "messages": [{"role": "user", "content": "现在几点？顺便说一下你有哪些工具"}]}).encode("utf-8")
            req = urllib.request.Request("http://127.0.0.1:%s/v1/chat/completions" % port, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                steps = (data.get("bridge") or {}).get("tool_steps") or []
                self.say("人设回复：%s" % content.replace("\n", " ")[:300])
                self.say("工具调用：%s" % (", ".join("%s(%s)" % (s["tool"], "ERR" if s["error"] else "ok") for s in steps) or "无"))
            except urllib.error.HTTPError as exc:
                self.say("失败 HTTP %s：%s" % (exc.code, exc.read().decode("utf-8", "replace")[:300]))
            except Exception as exc:  # noqa: BLE001
                self.say("失败：%s" % exc)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ config
    def save_config(self):
        cfg = json.loads(json.dumps(self.config))  # deep copy
        cfg.setdefault("upstream", {})
        cfg["upstream"]["base_url"] = self.fields["base_url"].get().strip()
        cfg["upstream"]["api_key"] = self.fields["api_key"].get().strip()
        cfg["upstream"]["model"] = self.fields["model"].get().strip()
        proxy = bool(self.proxy_mode.get())
        cfg["upstream_history"] = "full" if proxy else "last_user_only"
        cfg["include_client_system"] = proxy
        cfg.setdefault("listen", {})["host"] = "0.0.0.0" if proxy else "127.0.0.1"
        try:
            cfg.setdefault("listen", {})["port"] = int(self.fields["port"].get().strip() or 8877)
        except ValueError:
            messagebox.showerror("端口无效", "监听端口必须是数字")
            return False
        prox = cfg.setdefault("proxy", {})
        prox["enabled"] = bool(self.proxy_enabled.get())
        prox.setdefault("listen", {})["host"] = "0.0.0.0"
        try:
            prox["listen"]["port"] = int(self.proxy_port.get().strip() or 8890)
        except ValueError:
            messagebox.showerror("端口无效", "App 直连代理的端口必须是数字")
            return False
        prox["upstream_base"] = self.proxy_upstream.get().strip() or "https://api.deepseek.com/v1"
        prox.setdefault("tool_models", ["deepseek-flash", "deepseek-chat", "deepseek-reasoner"])
        prox.setdefault("max_tool_rounds", 6)
        prox.setdefault("inject_tool_hint", True)
        by_name = {s["name"]: s for s in cfg.get("servers", [])}
        for name, var in self.server_vars.items():
            by_name.setdefault(name, {"name": name})["enabled"] = bool(var.get())
        for name in SERVERS:  # keep server entries stable and ordered
            if name not in by_name:
                cfg.setdefault("servers", []).append({"name": name, "enabled": False})
        by_name = {s["name"]: s for s in cfg["servers"]}
        fs = by_name.get("fs")
        if fs is not None:
            roots = self.fields["roots"].get().strip()
            if roots:
                fs.setdefault("env", {})["MCP_FS_ROOTS"] = roots
        cfg["servers"] = [by_name[name] for name in SERVERS if name in by_name]
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        self.config = cfg
        bridge.reload_config(CONFIG_PATH)
        self._load_config_into_ui()
        self.say("配置已保存：%s" % CONFIG_PATH)
        return True

    def save_and_restart(self):
        if not self.save_config():
            return
        if self.server:
            self.stop_bridge()
            self.after(1200, self.start_bridge)
        else:
            self.start_bridge()

    # --------------------------------------------------------------- autostart
    def _startup_dir(self):
        return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")

    def _lnk_path(self):
        return os.path.join(self._startup_dir(), "aiyu-mcp-bridge.lnk")

    def _autostart_on(self):
        return os.path.exists(self._lnk_path())

    def toggle_autostart(self):
        target = sys.executable
        lnk = self._lnk_path()
        if self.autostart.get():
            script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
                      "$s.TargetPath='%s';$s.WorkingDirectory='%s';$s.WindowStyle=7;"
                      "$s.Arguments='--autostart';$s.Save()" % (lnk, target, BASE_DIR))
            self.say("设置开机自启…")
        else:
            script = "Remove-Item '%s' -Force -ErrorAction SilentlyContinue" % lnk
            self.say("取消开机自启…")
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.autostart.set(self._autostart_on())
            self.say("开机自启：%s" % ("已开启" if self.autostart.get() else "已关闭"))
        except Exception as exc:  # noqa: BLE001
            self.say("设置失败：%s" % exc)

    # ------------------------------------------------------------------- misc
    def copy_addr(self):
        self.clipboard_clear()
        self.clipboard_append(self.addr_var.get())
        self.say("已复制：%s" % self.addr_var.get())

    def clear_log(self):
        self.log.delete("1.0", END)

    def open_log(self):
        path = bridge.LOG_FILE
        if not os.path.exists(path):
            open(path, "a", encoding="utf-8").close()
        os.startfile(path)


if __name__ == "__main__":
    main()
