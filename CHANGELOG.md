# 更新日志

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
