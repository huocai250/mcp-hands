# 更新日志

## v4.0.1 — 一轮真刀真枪的 bug 猎杀（13 处）

4.0.0 发布后我按「先怀疑、再复现、再修」的方式做了一次全量审计：pyflakes 扫全部 63 个文件 + 12 个针对性探针（`tools/bughunt_check.py`，已进仓库当回归用）。**确认并修掉 13 处问题，其中 5 处安全类、1 处破坏性。**

**破坏性 CLI bug（最该早点发现的那个）**

* `mcp-hands.exe --backup D:\x.zip`、`--limit 20`、`--devices-approve pend_x` 这类**带值参数**，它的**值会被当成配置文件路径**：程序于是往那个路径**写了一份默认配置**（实测把 `%TEMP%\post-fix-backup.zip` 写成了 `bridge.config.json` 的内容）。备份/恢复/设备批准/分页这些命令在打包版里因此全都跑不对，而且会**覆盖用户指定的文件**。根因是 `bridge.py` 与 `gui.py` 里同一句「第一个不以 `--` 开头的参数就是配置」；现在只认**真正的裸参数**（跳过所有带值 flag 的值）。另加两道保险：配置文件只允许写成 `.json`，备份目标若已存在且不是 zip 也拒绝覆盖。
* 附带发现：打包版入口是 `gui.py`，而它之前**从来没被这条探针覆盖过**——现在探针直接走 exe 入口、并且清掉继承的配置环境变量来测。

**安全（5 处）**

* **设备白名单可被绕过**：`devices.mode=allowlist` 下，未批准的设备只要请求一个不在 `tool_models` 里的模型，就会走「原样转发」分支而**完全跳过校验**（探针实测 200）。现在设备校验被提到所有分支之前，relay 路径同样返回 401。
* **`--restore` 的 zip-slip**：构造一份含 `outbox/../../../x` 的备份，就能把文件写到安装目录之外（实测写到了 `Documents\deepseek-harness\`）。现在每个条目都会先解析成绝对路径并校验必须落在安装目录或记忆目录内，越界条目被拒绝并打印出来。
* **`fs` 沙箱跟随符号链接/目录联接**：路径检查用的是 `abspath`，所以允许目录里放一个 junction 就能读写到外面（Windows 用户目录里本来就有这类联接）。现在用 `realpath`，并对「尚不存在的目标文件」额外校验其父目录。
* **暴露到局域网时控制 API 无鉴权**：桥接一旦监听 `0.0.0.0`，同网段任何人都能批准设备、取消任务、读审计。新增 `control.token`（首次自动生成并写回配置），`/v2/*`（健康检查除外）与 `/dashboard` 必须带令牌；控制台页面自动携带令牌，启动日志直接打印可用地址。
* **审计日志对嵌套/内嵌密钥不打码**：`{"headers": {"Authorization": "Bearer sk-…"}}` 这种写法以前会明文入库。现在递归打码，并对字符串里出现的 `sk-…` 形态一并打码。

**正确性与资源**

* **生成的媒体在 %TEMP% 无限堆积**：截图/二维码/语音/文本的中间文件交付后从不删除。现在交付完成即清理（`speak_here=true` 需保留文件的场景除外）。
* **`send_text` 的 `name` 可穿越目录**：`..\..\x.txt` 会写到 %TEMP% 之外。现在只取 basename 并过滤非法字符。
* **一次性链接的并发竞态**：两个并发请求可能都取到同一个「用完即删」的链接；现在已取用过的会直接拒绝。
* **`send_voice` 没有长度上限**：超长文本经命令行传给语音合成会被截断甚至失败，现在自动截到 1200 字。
* **`outbox.add_bytes` 是死代码**：它的清理查找永远匹配不到，已重写，并让它真正尊重传入的 `name`。

**杂物**：清掉 7 处无用导入/死变量（`devices.mask_key`、tkinter 三处、`mcp_jobs`/`mcp_netcheck`/`mcp_office2`/`probe_upstream` 等）。pyflakes 现在除了刻意保留的 `import tkinter` 探测外零告警。

**回归**：`tools/bughunt_check.py` **12/12 通过**（源码与打包版入口都覆盖）；`jobs_check` 14/14、`v2_check` 18/18、`v3_check` 21/21、`v4_check` 24/24 全绿；源码与打包版 `--self-test` 均 0 failed。

## v4.0.0 — 她终于有嘴有身份，而这套东西能自己站住

3.0 让长任务有脉络、越权有闸门、做过的事有账可查；4.0 补上**交付**与**产品成熟度**。

**媒体出口（新 server `send`，8 个工具）——「给她一张嘴」**

* `send_screen` / `send_image` / `send_file` / `send_text` / `send_qr` / `send_voice`：截图、图片、任意文件、长文本、二维码、**语音**（Windows 内置语音合成渲染成 WAV，还能同时在电脑上念出来）；
* 所有链接都走 outbox：**HMAC 签名 + 到期时间 + 可选一次即失效**，地址不可猜、不可枚举，过期自动清理；取用次数与时间都记在日志里；
* 替代了之前「自己架文件服务器 + 丢裸链接」的土办法。

**设备配对与白名单（新 server `device`，3 个工具）**

* `devices.mode=allowlist` 后，**未批准的设备会被拒绝（401）并记为待批准**，控制台 /dashboard 上一键批准或拒绝，也可 `--devices-approve` / `--devices-revoke`；
* 密钥只存 salted hash，列表里只显示 `sk-abc…1234`；每台设备的加入时间、最近活动、调用次数都可查；
* 默认 `mode=off`，行为与 3.x 完全一致 —— 想收紧才收紧。

**计划验证与日志**

* 计划步骤可声明「需要验证」：`plan_done` 必须同时给出 `verify_evidence`（用**一次新的**工具读取作为独立证据，例如重新列目录确认文件真的在），否则**拒绝结案**；
* 新增 `plan_journal`：把每一步的状态、证据、验证、工具与参数整理成人类可读的日志；
* 计划完成时日志**自动写入长期记忆**，以后搜得到。

**运维：备份、恢复、自动更新**

* `--backup [zip]` 把配置 + jobs/plans/audit/memory + devices + outbox 打成一个压缩包；`--restore <zip>` 还原（原文件保留为 `.before-restore`）；
* `--check-update` 与 `update.ps1`：查询 GitHub Release → 比对版本 → 停服 → **整目录备份** → 安装 → 重启 → 打印健康状态；任何一步失败自动回滚；坏包在停服之前就会被拒绝；
* 网页控制台新增「设备与配对」「媒体出口」两块，并在页面上直接批准/撤销设备。

## v3.0.0 — 可管控的智能体运行时

2.0 让任务能离手、记忆能落地；3.0 让**长任务有脉络、越权有闸门、做过的事有账可查**。

**多身份路由（profiles）**

* `profiles` 配置块：每个身份可以有自己的上游地址、模型、key、工具白/黑名单；
* 分流顺序：`X-Profile` 请求头 → 模型名 → API key → 默认身份；不配 profiles 时行为与 2.x 完全一致；
* 实测：`alice` 只看到 21 个（calc/plan/audit）工具，`bob` 被禁掉 calc，同一个端口互不干扰。

**计划执行（plan，7 个工具）**

* `plan_create` 把目标拆成有序步骤（可以带工具），存进 SQLite，**跨轮次、跨重启**存活；
* 每轮请求会自动把「你手上计划的下一步」注入提示，她接着做就行；
* `plan_done` **必须附真实工具输出作为证据**——没证据不算完成（这正是她抱怨过的「拿嘴把想做的说成做完的」）；
* 支持 `plan_add_step`（干着干着发现新步骤）、`plan_list` / `plan_status` / `plan_cancel`；全部步骤落定后计划自动完结。

**策略与审计（policy + audit，3 个查询工具）**

* 所有工具调用经过**同一个闸门**（`ToolHub.call`）：`policy.mode` = `audit`（只记不拦，默认）或 `enforce`（拦截）；
* `deny` / `allow` / `deny_paths` / `max_calls_per_minute`，`audit_*`、`policy_*`、`jobs_*`、`plan_*` 默认豁免，避免自己把自己锁死；
* `audit.db` 记录每次调用的身份、工具、参数（**密钥自动打码**）、成功与否、耗时、返回长度；超上限自动裁剪；
* 实测：`enforce` 模式下 `shell_*` 被拦（返回明确原因且记成失败），同一身份下 `calc_*` 照常执行。

**网页控制台与运维**

* `GET /dashboard`：单页、零外部依赖、3 秒自动刷新，显示概览/后台任务/计划/最近审计；
* `GET /v2/health`、`/v2/plans*`、`/v2/audit*`、`/v2/policy`、`/v2/profiles`；
* `log.format=json`（每行一个 JSON 对象）+ `log.max_mb` 自动轮转（`.1` 备份）；
* CLI：`--audit`（最近调用）、`--plans`（计划列表）；GUI 新增「网页控制台」按钮。

## v2.0.0 — 从「一双手」到「一个会干长活的助手」

这一版不是加工具，而是把**架构**补齐：任务可以离手、记忆可以落地、扩展不用改代码。

**新增：后台任务引擎（`jobs`，6 个工具）**

* 长活（翻一整轮推荐流、批量处理文件、盯目录）用 `job_start` 丢给电脑；一次可以提交**一串动作**（steps），由工作线程按顺序执行；
* SQLite 持久队列（`jobs.db`），重启不丢；`job_list` / `job_status` / `job_wait` / `job_cancel` / `job_stats`；
* **跑完自动交回**：结果会在下一轮对话里作为「后台任务回执」注入给她，她必须主动向你交代；同时电脑上弹一条通知（因为 App 没有推送通道）；
* 完成后自动写进长期记忆（可关）。

**新增：长期记忆（`memory`，6 个工具）**

* SQLite + FTS5（`tokenize='trigram'`）持久记忆，**中文子串也能搜**（2 字以内自动回退 LIKE）；
* `memory_add` / `memory_search` / `memory_recent` / `memory_get` / `memory_forget`（支持按内容删除，带确认）/ `memory_stats`；
* 与 `notes`（草稿本）分工：`notes` 记当下的待办，`memory` 记跨会话的东西。

**架构：插件式 server 发现**

* 服务器列表不再写死在代码里：把 `servers/mcp_xxx.py` 放进目录就会被自动发现、自动启用，配置里写 `"enabled": false` 才关；
* 结果：33 个 server / 353 个工具，新增 server 不需要再改 `server_host.py`、GUI、配置三处。

**新增：本地 API 与可观测性**

* `GET /v2/health`（版本、运行时长、任务统计、计数器）、`GET|POST /v2/jobs`、`GET /v2/jobs/<id>?wait=N`、`POST /v2/jobs/<id>/cancel`、`GET /v2/jobs?pending=1&mark=1`；
* `GET /metrics`：工具数、server 数、任务数、请求数、token 计数（Prometheus 文本格式）。

**可靠性**

* 上游 408/429/5xx 自动重试（3 次，指数退避），聊天不会因为一次抖动就失败；
* 工具参数容错下沉到 `ToolHub.call`：模型多塞的未声明参数被忽略而不是报错；
* 任务引擎的认领是**原子**的（修复了两个 worker 抢同一任务导致计划跑两遍的竞态）。

**工具与体验（承接 1.1.x）**

* `--jobs`（看队列）、`--jobs-run <id>`（立即执行）、`--migrate`（给旧配置补齐新键并备份）；
* 控制台新增「后台任务」按钮；关于窗口与 `--version` 显示 2.0.0。

## v1.1.7 — 一段一汇报

* 单轮预算 40 轮/420 秒 → **12 轮/120 秒**，一小段就汇报，不再一口气憋到底；
* 进度行限量 `progress_max`（默认 3），标签改成人话（不会再漏出 `screenshot`、`now` 这类工具名）。

## v1.1.6 — 进度实时可见 + 一键截图

* SSE 在工具循环期间推送可见进度行（原先只有不可见心跳，所以「全部做完才一次性发」）；
* 新增 `http_share_screenshot`：一次调用完成截屏 → 放进文件服务 → 返回手机可打开的 URL 与 markdown 图片行。

## v1.1.5 — 密钥不进日志

* 日志里的 `api_key` 等字段自动打码；打包显式排除 `bridge.config.json` / `bridge.log` / `gui-settings.json`。

## v1.1.4 — 人设工作纪律（七条）+ 定时提醒

* 时间（先查 `sys_now`）、报错先换路重试、不照抄机器噪音、称呼统一、先列待办、先搜记忆、主动开口用电脑通道；
* 新增 `sched.remind_in` / `remind_at` / `remind_list` / `remind_cancel`（到点弹通知，可念出来）。

## v1.1.3 — 坏标记容错与完成回执

* DSML 标记容错（截断/单引号/全角/数字参数）；一次请求可连续调度 40 轮动作；
* 回复末尾附「这轮电脑侧的真实回执」；每次请求注入视觉状态（已接通/未接通）并要求不通时直说看不见。

## v1.1.2 — 标记不再泄漏 + 长任务跑得完

* 解析并执行 DeepSeek 的文字形式工具调用（DSML），绝不再显示给你看；
* 最后一轮撤掉工具让模型说人话；回执降噪。

## v1.1.1 — 视觉空答案修复 + 长任务不断线

* `deepseek-flash` 默认思考模式会把答案吞进 `reasoning_content` → 视觉调用显式 `thinking: disabled`，并回退 `reasoning_content`；
* SSE 每 5 秒保活，长工具循环期间手机端不再「思考中断」。

## v1.1.0 — 视觉识别 + 7 个新 server

* `vision`（看屏幕/看图/OCR/界面理解/两图对比）、`files2`、`calc`、`notes`、`pwd`、`netcheck`、`office2`；
* 335 个工具 / 31 个 server；App 直连代理会自动把 App 的 key 用于视觉调用。

## v1.0.0 — 首次发布

* 把手机 App 里的人设接上电脑的 261 个工具（24 个 MCP server），单 exe + 图形控制台。
