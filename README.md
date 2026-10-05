# aiyu MCP Bridge

把**手机 App 里的人设**接上**电脑上的 261 个工具**（24 个自研 MCP server），并打包成带图形控制台的单个 exe。

手机里那个角色不再只会聊天——它可以真的建 Word/Excel、读写文件、跑命令、开网页、截图、操作鼠标键盘、管计划任务、查注册表、发二维码、抓网页、备份目录，甚至开一个本地文件服务器让你从手机下载电脑上的文件。

```
┌──────────────┐  OpenAI 兼容   ┌───────────────────────────┐  纯文本对话   ┌───────────────────┐
│ Cherry Studio│ ─────────────▶ │ aiyu-mcp-bridge.exe       │ ───────────▶ │ 手机 App 人设端点  │
│ ChatBox 等   │ ◀───────────── │ 127.0.0.1:8877  (方式 A)  │ ◀─────────── │ PHONE:8866        │
└──────────────┘  带 MCP 工具   └─────────────┬─────────────┘  不发送 tools └───────────────────┘
                                              │                ▲
                    ┌─────────────────────────┴──────────┐     │ 方式 B：App 的模型服务商直接指向本机
                    │  MCP (stdio JSON-RPC) × 24 servers │     │ 0.0.0.0:8890（原生 function calling）
                    └────────────────────────────────────┘     │
       fs shell web sys office media archive sqlite desktop voice monitor net dev
       forensics text pdf qr backup http media2 sched soft registry netadv
```

## 特性

* **两种接入方式**：A) 电脑端任意 OpenAI 兼容客户端经桥接使用人设；B) 让手机 App 自己的聊天直接带上原生工具调用（推荐，聊天记录里不会出现工具痕迹）。
* **261 个工具 / 24 个 server**：文件、命令、网页、Office、图片、PDF、二维码、压缩包、SQLite、桌面自动化、语音、系统监控、网络与安全、开发工具、文本/数据转换、备份、HTTP 文件共享、计划任务、软件安装、注册表、网络高级设置。
* **图形控制台**：启停、改配置、勾选 server、一键自检/体检/测试对话、实时日志、可滚动自适应布局、窗口尺寸记忆、开机自启。
* **单 exe 自包含**：24 个 server 全部编进同一个可执行文件（子进程用 `exe --mcp-server <name>` 复用自己的二进制），不需要额外文件。
* **零第三方依赖的 MCP 实现**：`mcp_client.py` 是手写的 JSON-RPC over stdio 客户端，`servers/mcpserver.py` 是同款服务端骨架——加一个新工具只要 20 行。
* **离线可测**：内置假上游（会说文本协议 / 会说原生 function calling 两种），`--self-test` 能跑 200 项样例调用。

## 快速开始

### 1. 直接用打包好的 exe

从 Releases 下载 `aiyu-mcp-bridge-<ver>.zip`，解压后双击 `aiyu-mcp-bridge.exe`：出现图形控制台 → 点「启动」。首次运行会在同目录生成 `bridge.config.json`。

### 2. 源码运行

```bash
git clone <repo-url>
cd aiyu-mcp-bridge
python -m pip install pypdf qrcode pillow python-docx openpyxl python-pptx
python gui.py --autostart          # 图形控制台 + 自动启动服务
# 或者：
python bridge.py                   # 只跑桥接服务（8877）
python proxy.py                    # 只跑 App 直连代理（8890）
```

`python bridge.py --tools | --self-test | --doctor | --init` 分别列出工具、跑自检、体检、生成默认配置。

### 3. 自己打包 exe

```powershell
powershell -ExecutionPolicy Bypass -File .\build-exe.ps1          # onedir（推荐，启动快）
powershell -ExecutionPolicy Bypass -File .\build-exe.ps1 -OneFile # 单文件
```

产物在 `release\aiyu-mcp-bridge\`。

## 两种接入方式怎么选

| | 方式 A：桥接（8877） | 方式 B：App 直连代理（8890，推荐） |
|---|---|---|
| 谁发起 | 电脑上的客户端（Cherry Studio / ChatBox / SillyTavern…） | 手机 App 自己的聊天 |
| 人在哪聊 | 电脑客户端 | **手机 App 里，还是原来的界面** |
| 工具调用 | 文本协议（模型写 `TOOL: ...`），这些块会留在聊天记录里 | **原生 OpenAI function calling**，聊天里看不到 |
| 需要放行防火墙 | 不需要（电脑主动连手机） | 需要放行入站 TCP 8890（`open-firewall.ps1`，GUI 里有按钮） |
| 上游 | 手机 App 的外部访问端点 | App 把请求转给你，你带着 **App 里填的 key** 请求真实上游 |

**方式 A 配置**：客户端 Base URL `http://127.0.0.1:8877/v1`，API Key 任意，模型 `aiyu-proxy`。

**方式 B 配置**：手机 App → 服务提供商「自定义」→ API URL 填 `http://<电脑局域网IP>:8890/v1`，API 密钥填你自己的上游 key，模型填 `deepseek-flash` 之类。电脑 IP 与端口在 GUI 里直接显示并有复制按钮。

## 配置

完整示例见 `configs/bridge.config.example.json`，关键字段：

| 字段 | 说明 |
|---|---|
| `upstream.base_url / api_key / model` | 方式 A 要指向手机 App 的外部访问端点 |
| `listen.host / port` | 桥接监听地址（默认只监听 127.0.0.1） |
| `upstream_history` | `last_user_only`（只发最新一条，避免历史膨胀）或 `full`（代理模式，转发整段历史） |
| `proxy.enabled` | 是否开启方式 B 的直连代理 |
| `proxy.tool_models` | **只有这些模型**会被挂上工具，其它模型（视觉/听觉辅助）原样转发 |
| `proxy.upstream_base` | 方式 B 转发到哪个真实上游（默认 `https://api.deepseek.com/v1`） |
| `max_tool_rounds` | 一次请求内最多几轮工具往返 |
| `servers[].enabled` | 每个 server 可单独关掉 |
| `servers[].env.MCP_FS_ROOTS` | 文件工具沙箱根目录（`;` 分隔，支持 `%USERPROFILE%` 这类环境变量） |

## 工具清单（261 / 24 servers）

| server | 数量 | 能力 |
|---|---|---|
| `fs` | 15 | 列目录/读写/复制/移动/删除/全局搜索/正则搜索/尾部读取/替换/哈希/目录树/占用统计 |
| `shell` | 6 | cmd 与 PowerShell、启动进程、列举/结束进程、which |
| `web` | 6 | 抓网页（HTML 转文本）、任意 HTTP 请求、下载、DuckDuckGo 搜索、抽取页面链接 |
| `sys` | 15 | 剪贴板、截图、通知气泡、环境变量、已装软件、Wi-Fi 信息、壁纸、锁屏、关机/重启、系统信息 |
| `office` | 12 | Word/Excel/PPT/CSV 创建与读取、改单元格、CSV→XLSX |
| `media` | 11 | 图片缩放/裁剪/旋转/转格式/缩略图/压缩/灰度/写字/平铺水印/合成 PDF |
| `archive` | 8 | zip/tar 创建、追加、列举、解压、单文件解压（防目录穿越） |
| `sqlite` | 7 | 查询/执行（仅 SELECT/WITH/PRAGMA）、表结构、导入导出 CSV、库统计 |
| `desktop` | 11 | 枚举/聚焦/关闭窗口、**输入任意 Unicode 文本**、按键组合、鼠标移动点击、区域截图 |
| `voice` | 7 | SAPI 朗读、发音人列表、播放音频、播放/上一首/下一首、音量、蜂鸣、语音+气泡 |
| `monitor` | 12 | CPU/内存/磁盘/电池/开机时长快照、Top 进程、服务、启动项、事件日志、设备 |
| `net` | 11 | DNS、TCP 探测、端口扫描、ping、HTTP 状态、公网 IP、网络配置、路由表、ARP、局域网扫描 |
| `dev` | 18 | git 操作（受限）、跑 Python 片段、JSON 美化与取值、base64、哈希、UUID、代码统计 |
| `forensics` | 10 | 文件/目录哈希、strings、熵扫描、hex dump、PE 头解析、密钥扫描（自动打码）、网络连接 |
| `text` | 24 | 字数统计、命名法转换、正则查找替换、diff、Markdown↔HTML、CSV↔JSON↔表格、CSV 统计聚合、**GBK↔UTF-8 转换**、编码自动识别 |
| `pdf` | 10 | 信息/文本提取、合并、拆分、抽页、删页、旋转、写元数据、加密（AES→RC4 自动回退）、解密 |
| `qr` | 7 | 二维码 PNG/SVG/批量、Wi-Fi、vCard、URL |
| `backup` | 8 | 目录时间戳备份、CRC 校验、防逃逸恢复、轮换、增量镜像、单文件快照 |
| `http` | 7 | **本地文件服务器（手机可下载电脑文件）**、共享文件、抓取到共享、局域网地址 |
| `media2` | 12 | 拼图、切九宫格、描边、叠图、主色、图像差异、2 倍放大、GIF 制作/拆帧、EXIF |
| `sched` | 12 | 计划任务增删改查与立即运行、电源计划、保持唤醒、休眠开关 |
| `soft` | 8 | winget 检测/搜索/安装/卸载/可升级/已安装（无 winget 走注册表）、商店页面 |
| `registry` | 8 | 注册表读值/列子键/写值/删值/删键/搜索/导出/备份 |
| `netadv` | 16 | Wi-Fi 配置与连接、hosts 读写还原、DNS 刷新、端口转发、防火墙规则 |

## 写一个自己的 MCP server

新建 `servers/mcp_myserver.py`：

```python
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server

srv = Server("myserver")            # 名字要和 --mcp-server 参数一致


@srv.tool("hello", "Say hello to someone.",
          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
def hello(name):
    return "hello %s" % name


SAMPLES = {"hello": {"name": "world"}}   # --self-test 用的样例参数（可选）

def build():
    return srv


if __name__ == "__main__":
    srv.run()
```

然后在 `server_host.py` 的 `SERVERS` 元组、`gui.py` 的 `SERVERS` 元组、以及配置文件的 `servers` 数组里加上 `myserver`，`--self-test` 就会带上它。协议细节（initialize / tools/list / tools/call）见 `servers/mcpserver.py`，客户端实现见 `mcp_client.py`。

## 实测踩到的三个坑（技术记录）

对接这类「手机端人设端点」时，有三条行为会直接决定架构，都是实测确认的：

1. **方括号内容会被吞掉**。模型输出里所有 `[ ... ]` 片段会被手机端的管线删除——`{"a": [1,2,3], "b": ["x","y"], "c": "中文"}` 回显成 `{"a": , "b": , "c": "中文"}`。所以工具协议**不能依赖 JSON 数组**，本项目用的是无括号的行格式：
   ```
   TOOL: office_xlsx_create
   path=C:\Users\me\Desktop\a.xlsx
   rows=name,qty;apple,3
   END
   ```
2. **端点是「有状态会话」**。外部访问端点每次都把请求**追加**到人设自己的会话（实测只发 1 条消息，它在上游 payload 里排到 `messages[49]`），所以桥接层只发最新一条用户消息（`upstream_history: last_user_only`），避免上下文爆炸。
3. **原生 `tools` 是单行道**。给它发 `tools` 能正常收到 `tool_calls`，但回填 `role:"tool"` 会被上游拒绝（`An assistant message with 'tool_calls' must be followed by tool messages…`），而且那个孤立调用会**永久污染该会话**直到你在 App 里清空。因此桥接层**永不发送 `tools`**；需要原生 function calling 时改用方式 B（请求由电脑代理转发给真实上游）。

顺带一个能用就好的技巧：方式 B 会清洗历史里残留的文本协议痕迹（协议块、`TOOL:` 段、`TOOL_RESULT` 回合），否则模型会照着历史范例继续写文本块；万一它还是写了，代理也能解析出来照常执行。

## 目录结构

```
bridge.py            桥接服务 + CLI（--tools/--self-test/--doctor/--mcp-server）
proxy.py             App 直连代理：给 App 的请求挂原生 tools 并跑工具循环
gui.py               图形控制台（打包入口）
mcp_client.py        零依赖 MCP stdio 客户端 + 工具路由（并行启动 server）
server_host.py       --mcp-server <name> 分发到内置 server
servers/             24 个 MCP server + mcpserver.py（服务端骨架）
configs/             示例配置（example / mock / fullmode）
tools/               开发与诊断脚本（假上游、探针、布局自测）
scripts → 根目录      build-exe.ps1 / run-bridge.ps1 / stop-bridge.ps1 / start-bridge.cmd
                     install-autostart.ps1 / open-firewall.ps1
```

## 常见问题

| 现象 | 处理 |
|---|---|
| 手机发了指令但电脑没反应 | 服务没启动，或方式是 A 但客户端没连；GUI 里看状态灯与日志 |
| 聊天里出现 `TOOL: ...` 文本块 | 还在用方式 A（文本协议）；切到方式 B 就没有了 |
| 桥返回 `upstream HTTP 400 ... tool_calls ...` | 手机端会话被原生 tool_calls 污染 → 在 App 里清空该会话 |
| 手机连不上 8890 | 放行入站端口（`open-firewall.ps1`，需管理员） |
| `--self-test` 有 FAIL | 看那一行提示；多数是样例路径落在沙箱外或端口被占 |
| 换网络后电脑 IP 变了 | 以 GUI 显示的「App 里填」为准（DHCP 会变），或给电脑设静态地址 |

## 安全提示

这些工具是**真的会动你的系统**：`shell` 能执行任意命令，`desktop` 能控制鼠标键盘，`registry`/`netadv` 能改注册表、hosts、防火墙，`soft` 能装/卸软件，`sys.power_action` 能关机重启，`fs.delete` 能删文件。建议：

* 只监听本机（`listen.host` 保持 `127.0.0.1`；方式 B 才需要 `0.0.0.0` 并配合防火墙规则）；
* 用 `MCP_FS_ROOTS` 把文件工具锁在需要的目录里；
* 不需要的 server 在 GUI 里直接取消勾选（`"enabled": false`）；
* 别把 8890/8877 暴露到公网。

## License

MIT，见 [LICENSE](LICENSE)。
