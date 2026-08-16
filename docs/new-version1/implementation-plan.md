# new-version2 实施计划（有序升级）

> 版本：v1.0
> 日期：2026-08-08
> 前置：`agent-runtime-design.md` / `agent-runtime-event-full-design.md` / `agent-runtime-event-tech-impl.md`（均按 main@13709ee 校准）
> 说明：本文只定义**做什么、按什么顺序做、每步如何验收与回滚**。用例验收细则见 `acceptance-plan.md`。

---

## 一、升级原则

1. **行为不回归优先**：每一步必须满足"上游功能不回退"，以 `python -m pytest tests/ -v` 全绿 + 对应里程碑验收用例为门禁。
2. **开关先行，灰度切换**：引入 `execution_mode` 功能开关（见 §三），默认保持现有 `direct` 行为，改造完成后按里程碑逐级切换，任一环节异常可一键回退。
3. **本地先闭环，远端后接入**：M1/M2 先在 bot 进程内（`service` 直调，不经 HTTP）把「入队 → 执行 → 回报 → 档案」闭环跑通；M3 才暴露 `/api/runtime/*` HTTP 协议供远端边端接入。
4. **一条任务队列，双通道复用**：事件档案由任务回报协议驱动（`messages/complete/fail`），**不引入** `/api/events/report`，避免双上报通道。
5. **VCS 层不动**：`VcsBackend`/`GitCodeBackend`/`GitHubBackend`（`src/vcs/`）术语与逻辑一律不迁移。
6. **DB 一次迁移、幂等、可备份**：迁移前必须备份 `data/sessions.db`；迁移脚本幂等（`PRAGMA table_info` 前置检查）。
7. **编码约束**：所有文件读写 `encoding='utf-8'`；**严禁用 PowerShell 写含中文文件**（见 `DEPLOYMENT-REMOTE.md` §6.1 教训）。

---

## 二、里程碑总览

| 阶段 | 主题 | 形态 | 结束门禁（DoD） |
|---|---|---|---|
| **M0** | 术语重构 backend→runtime | 纯更名，行为不变 | 全量 pytest 绿；`execution_mode=direct` 回归一致 || **M1** | 控制平面 + 事件档案模块 | 新增能力，不切换 | 单测绿；direct 本地链路手工跑通 |
| **M2** | 回报协议驱动 event.json + 执行通道收敛 | 事件/chat 切「本机 direct / 远端入队」 | queue_remote 全链路 E2E；`direct` 可一键回退 |
| **M3** | 远端 RuntimeAgent + 凭据 | `/api/runtime/*` HTTP + 边端进程 | 远端 RuntimeAgent 端到端；`queue_remote` 验证 |
| **M4** | 前端 | 管理页 + 绑定 + chat 轮询 + Event Detail | Playwright UI + 手工验收 |
| **M5** | 后续演进（WS 唤醒 / 任务级 token / mTLS / GC） | **本期不做** | — |

> 里程碑编号对齐 `agent-runtime-event-full-design.md` §十三。M5 明确排除，避免范围蔓延。

---

## 三、功能开关与切换策略（有序升级核心）

**开关**：环境变量 `DEVOPS_EXECUTION_MODE` + `meta` 表键 `execution_mode`（env 优先），取值：

| 值 | 含义 | 启用时机 |
|---|---|---|
| `auto`（默认） | 按绑定引擎宿主自动推导：本机引擎（`credential_id=0`）→ `direct`；远端 worker（`credential_id>0`）→ `queue_remote` | 升级初始即默认 |
| `direct` | 事件/chat 直接 `stream_agent` 就地执行 | 兼容/强制 |
| `queue_remote` | 入队 → 远端（或本机独立进程）`RuntimeAgent` 边端经 `/api/runtime/*` claim 执行 | M3 灰度 |

> `queue_local` 已废除（本机引擎一律 direct，见 `engine-default-selection-design.md` §9）；白名单仅为 auto/direct/queue_remote。

**切换策略**：
- 灰度为**按仓库/Agent** 或**全局**二选一（实现期定：建议全局 + 手工切换，先试点仓库）。
- 回退：`execution_mode` 切回 `direct` 即恢复旧执行路径；`agent_tasks` 中滞留的 queued 任务可手工重新入队或置 `cancelled`。
- chat 侧保留 `send/stream` SSE 分支（`chat.py:182`）不删除，作为 `direct` 模式与轮询模式的切换通道（对齐 tech-impl §七）。

---

## 四、里程碑任务清单

### M0 术语重构（backend → runtime）——纯更名，可独立回滚

**模块迁移**
- [ ] `src/agent/` → `src/runtime/`：`base.py`（`AgentBackend`→`AgentRuntime`）、`executor.py`→`registry.py`+`executor.py`、`opencode.py`→`OpencodeRuntime`、`codearts.py`→`CodeArtsRuntime`。
- [ ] 保留兼容别名：`stream_agent = stream_runtime`（`src/runtime/executor.py`）；旧模块 `src/agent/__init__.py` 导出转发（避免 import 链爆炸，后续清理）。

**符号更名（`src/` 全量）**
- [ ] `register_backend`→`register_runtime`、`get_backend`→`get_runtime`、`discover_backends`→`discover_runtimes`、`_BACKENDS`→`_RUNTIMES`（`executor.py`）。
- [ ] `config.py:109` `get_agent_backend`→`get_agent_runtime`。
- [ ] `user/db.py`：`get_backend_agents`→`get_agent_runtimes`、`save_backend_agents`→`save_agent_runtimes`、`get_backend_id_by_name`→`get_runtime_id_by_name`、`ensure_default_backend`→`ensure_default_runtime`。

**DB 迁移（`db_setup.py` 幂等区，先备份 `data/sessions.db`）**
- [ ] `backend_agents` → `agent_runtimes`（`ALTER TABLE ... RENAME`，保留全部列含 `pip_package`）+ 补充 `credential_id/provider/binary/status/last_seen_at`。
- [ ] 引用列改名（`_rename_column_if_exists` + `PRAGMA table_info` 前置检查）：`devops_agent_session.backend_id→runtime_id`、`.backend_session_id→runtime_session_id`；`agent_type.default_backend_id→default_runtime_id`；`chat_agent_session.backend_id→runtime_id`、`.backend→runtime`；`chat_conversations.backend_session_id→runtime_session_id`；`vod_conversations.backend_session_id→runtime_session_id`；`events.backend→runtime`。

**并发基础（`src/session_manager.py`，见 `concurrency-design.md` §2.1，M0 独立落地）**
- [ ] SQLite 连接加 `PRAGMA journal_mode=WAL` / `busy_timeout=5000` / `synchronous=NORMAL`（跨进程写安全，兜底双进程 3000+8080 竞争）。

**API 层**
- [ ] `settings.py:178/185`：`/api/settings/backend-agents` → `/api/settings/agent-runtimes`（含 `/refresh`）；旧路径保留 301 或同实现别名（≥2 发布周期）。
- [ ] `events.py` / `agents.py` 返回体中 `backend` 字段 → `runtime` 字段。

**前端（`src/api/static/`）**
- [ ] `js/pages/backends.js` → `js/pages/runtimes.js`（`renderBackends`→`renderRuntimes`，`API` 路径同步）。
- [ ] `index.html:61` `#nav-backends` → `#nav-runtimes`，文案 "Backend Runtimes"→"Agent Runtimes"。
- [ ] `app.js:3` VIEWS `'backends'`→`'runtimes'`；`app.js:233-235` 标题与请求路径；`?v=` 缓存版本号提升（`index.html:8`）。

**测试**
- [ ] 更名：`test_agent_executor.py`、`test_api_routes.py`（`test_backend_agents` 断言键 `backends`→`runtimes`）、`test_phase9.py`、`ui_test_playwright.py`（`test_backend_page`）、`ui_scenario_test.py`（`test_s10_backends`）。
- [ ] 排除：`test_github_normalize.py` / `ui_*` 中 VCS/GitHub 相关**不改**。

> 门禁：`python -m pytest tests/ -v` 全绿；`execution_mode=direct` 下行为与 main 一致。

---

### M1 控制平面 + 事件档案模块（新增，不切换执行路径）

**建表（`db_setup.py` `_create_tables()` 新增，迁移与建表分离）**
- [ ] `runtime_credentials`（`token_hash` 唯一，`status` active/revoked）。
- [ ] `agent_runtimes`（如 M0 已存在则跳过建表）。
- [ ] `agent_tasks`（+ `idx_agent_tasks_status/runtime/session/event` 4 索引）。

**事件档案模块（新文件 `src/api/event_archive.py`）**
- [ ] `get_event_json_path` / `_atomic_write` / `_load` / `_redact` / `_now` / `_extract_num` / `_locate_by_seq` / `_trunc_safe`。
- [ ] `init_event_json`（`#0` 跳过；pipeline 段含 `runtime` 字段）。
- [ ] `append_event_archive_log`（增量写 logs，脱敏）。
- [ ] `finalize_event_archive`（写 output、整链 status、覆盖 summary）。
- [ ] `load_event_json` / `build_issue_context`（供 prompt 注入）。
- [ ] 每 repo 一把 `_lock` + 原子替换（写失败仅 `log.warning`，不阻断）。

**流式执行结构化（`src/runtime/executor.py`）**
- [ ] `stream_runtime`：`tool_use` 改 yield 结构化 dict（`{tool, input}`）；`text/reasoning/raw` 全量（不再截断 150/200 字符）；`done` 保留 `text` 全量。
- [ ] 会话失效自动重建逻辑保持（`executor.py:298-328`）。

**结构化产出契约**
- [ ] `event_store.py`：新增 `parse_agent_output_json`（正则提取末尾 JSON 块）；`evaluate_agent_output` 增强——优先 JSON 块（`actions/conclusion/artifacts/next_step/issue_summary`），失败走现有正则降级。
- [ ] `prompt_builder.py`：`build_prompt` 末尾统一追加「处理记录输出要求」段（一处维护，所有 skill 生效）。

**服务端 RuntimeService（新文件 `src/runtime/service.py`）**
- [ ] `enqueue_task(...)`：`agent_session_id`/`runtime_id` 必填（确定路由 D3）、快照 `event_seq/issue_id/repo_key/agent_type/skill/runtime_session_id`。
- [ ] **服务端资源交付**（见 `skill-delivery-remote.md`）：enqueue 时打包技能目录 → `skill_files`；DB 系凭据（user credentials / CodeArts AK-SK）合并 `env_extra`；注入 `server_url`。
- [ ] `claim(runtime_id)`：按 runtime_id 过滤 + **串行槽 D9**（`NOT EXISTS` 同 session 活跃 task）+ **原子抢单**（`UPDATE ... WHERE status='queued' RETURNING`，见 `concurrency-design.md` §2.3）。
- [ ] `complete/fail` 终态幂等（`update_task_status` 已终态重放安全）。
- [ ] 本地 worker：进程内消费**线程池**（`ThreadPoolExecutor(max_workers=RUNTIME_MAX_CONCURRENT_TASKS)`，替代无界 `threading.Thread`），`claim → stream_runtime → report_messages/complete/fail`，**本阶段不经 HTTP**（见 `concurrency-design.md` §2.2）。

**prompt 占位符化（`prompt_builder.py`）**
- [ ] `Skill 文件：{{SKILL_DIR}}/{skill}/SKILL.md`（原绝对路径）；A2A `{{SERVER_URL}}/api/a2a`；远端模式不注入历史文件绝对路径。
- [ ] worker 端落地技能包 + 替换占位符 + env 合并（`agent.py` `_run_task` 前置）。

> 门禁：`test_event_archive.py` / `test_runtime.py` 单测绿；`execution_mode=direct` 仍为主路径，M1 能力只被测试调用。

---

### M2 回报协议驱动 event.json + 执行路径切换

**回报 ingestion（服务端）**
- [ ] `report_messages`：定位 `event_seq` → `append_event_archive_log`（批量 NDJSON 映射 logs）。
- [ ] **回报异步化**：`_report_queue`(asyncio.Queue, maxsize=2000) + `_ingestion_loop` 攒批（≤50 条/200ms）批量写库；complete/fail 终态同步保证强一致；队列满降级同步写（见 `concurrency-design.md` §2.4）。
- [ ] `complete/fail`：`parse_agent_output_json` → `finalize_event_archive` → `update_event_complete`（同步 events 表 status/exit_code/duration/agent_logs/result）。
- [ ] summary 机制：有 `issue_summary` 才覆盖顶层；无则保持。

**事件管道切换（`event_pipeline.py`）**
- [ ] `run_pipeline` 尾部改造为**触发点三连**：`append_event` → `init_event_json` → `enqueue_task`（`runtime_id` 来自 `devops_agent_session.runtime_id`）。
- [ ] `_execute_agent`（`event_pipeline.py:323`）拆分为「入队侧」+ 移除进程内直接执行（由 worker 承担）。
- [ ] `#0`（无 Issue）只写 events，不入队。

**chat / IM 切换（D5）**
- [ ] `chat.py` `send`（`:125`）改「入队 + 返回 task_id」；`send/stream`（`:182`）保留 SSE 分支，受 `execution_mode` 控制。
- [ ] `GET /api/runtime/tasks/{id}/status` 供轮询（前端 M4 配）。
- [ ] `im/handler_async.py` / `vod_handler_async.py` 同 chat（入队 + 状态轮询）。

**取消/中断**
- [ ] `cancel_task(task_id)`：置 `cancelled`；执行侧 kill 子进程（复用 `_terminate_proc`，`executor.py:57`）。

> 门禁：`queue_remote` 下事件/chat/IM 三入口全链路 E2E 通过；`direct` 一键回退验证。

---

### M3 远端 RuntimeAgent + 凭据

**HTTP 协议层（`/api/runtime/*`，新文件 `src/api/routes/runtime.py`）**
- [ ] `register` / `heartbeat` / `runtimes/{rid}/tasks/claim` / `tasks/{tid}/start|progress|messages|usage|complete|fail|status` / `runtimes/{rid}/recover-orphans`。
- [ ] 认证中间件 `_auth`：`Bearer rta_...` → sha256 → 查 `runtime_credentials(status=active)` → 注入 `credential_id`；仅凭据可过，不开放 user token（D2）。

**凭据管理（`settings.py`）**
- [ ] `POST /runtime-credentials`：生成 `rta_`+`secrets.token_urlsafe(32)`，存 sha256，明文仅返回一次。
- [ ] `POST /runtime-credentials/{cid}/revoke`、`GET /runtime-credentials`（不含 token_hash）。

**边端（新文件 `src/runtime/client.py` + `agent.py`）**
- [ ] `client.py`：外连 HTTP + 指数退避（4s→64s，仅 5xx/408/429 重试）+ `runtime_gone`(404) 重注册；**`requests` 未在 requirements.txt**——用 `aiohttp.ClientSession` 连接池（`TCPConnector(limit=10)`，见 `concurrency-design.md` §2.6）。
- [ ] `agent.py`：`python -m runtime.agent`；`discover_runtimes()` → `register` → `recover_orphans` → 循环 `claim`（带 jitter）→ `_run_task`（落地技能包 + 占位符替换 + `stream_runtime` + 回报，见 `skill-delivery-remote.md` §3.5）。执行用 **`ThreadPoolExecutor(max_workers=max_concurrent)`**（非无限 spawn，见 `concurrency-design.md` §2.2）。env：`RUNTIME_TOKEN` / `RUNTIME_SERVER_URL` / `RUNTIME_POLL_INTERVAL` / `RUNTIME_HEARTBEAT_INTERVAL` / `RUNTIME_MAX_CONCURRENT_TASKS`。

**健康态（新文件 `src/runtime/health.py`）**
- [ ] `derive_runtime_health(status, last_seen_at)`：online / recently_lost(<5min) / offline / about_to_gc(<1d 到 GC)。
- [ ] 心跳写 `last_seen_at`；离线判定 150s 无心跳 → offline（sweeper 由 service 定时执行）。

> 门禁：本地 RuntimeAgent（`server_url=http://localhost:3000`）端到端；远端 worker 机接入验证；绑定 runtime 离线时任务保持 queued 且 health 页可见。

---

### M4 前端（`src/api/static/`）

- [ ] `js/pages/runtimes.js`：设备列表按健康四态分组（device_name + 心跳 + 承载 CLI）+ 凭据管理弹窗（生成/复制 `rta_`/吊销）+ 设备任务列表。
- [ ] `js/pages/agents.js`：Agent 配置页绑定 runtime 下拉（仅在线），写 `PUT /api/agents/{agent}/config` → `runtime_id`。
- [ ] `js/pages/chat.js`：发送 → 返回 `task_id` → `setInterval(1s + jitter)` 轮询 `GET /api/runtime/tasks/{id}/status` → 完成展示 `result_text`（见 `concurrency-design.md` §2.5，500ms→1s 节流）。
- [ ] `js/pages/events.js`：Event Detail 展示结构化 output（actions/conclusion/artifacts/next_step）卡片。
- [ ] `index.html` 导航 + `app.js` VIEWS + `?v=` 提升。

> 门禁：Playwright UI 用例 + 手工验收。

---

## 五、部署与切换顺序

| 步骤 | 动作 | 验证 | 回滚 |
|---|---|---|---|
| 0 | 备份 `data/sessions.db` + 记录 `main` 当前 commit | — | 恢复备份 / `git checkout` |
| 1 | 发布 **M0**（纯更名，`execution_mode=direct`） | 全量测试绿 + 线上 smoke | 恢复上一版本（更名无行为差异） |
| 2 | 发布 **M1**（新增能力，仍 `direct`） | 单测绿 + 冒烟 | 不切换即无影响 |
| 3 | 发布 **M2**，灰度 `queue_remote`（先本机边端，再远端机；本机引擎保持 `direct`） | 事件/chat/IM 端到端 + 档案正确 | 切回 `direct`；滞留任务重入队 |
| 4 | 发布 **M3**，扩远端 worker 规模 | 远端 E2E + 离线滞留可见 | 切回 `direct` |
| 5 | 发布 **M4**（前端） | Playwright + 手工 | 前端独立回滚（`?v=` 恢复） |

> 每步灰度为**可逆操作**，开关存 `meta` 表 `execution_mode`，运维只需改一行键值 + 重启，无需代码变更。

---

## 六、风险与应对

| 风险 | 应对 |
|---|---|
| 术语重构破坏调用 | 保留 `stream_agent` 兼容别名 + 旧 API 路径别名；M0 独立提交可整体回滚 |
| DB 迁移失败 | 迁移前备份 + 幂等脚本；失败仅 `log.warning` 不中断，`_rename_column_if_exists` 兜底 |
| 队列积压 / runtime 离线 | health 页可见 + 手工重入队；`cancel_task` 兜底 |
| 同 session 并发污染 | D9 串行槽（claim 查询排除活跃 session）——M1 起实现并测试 |
| chat 打字机降级 | 500ms 轮询近似；`send/stream` SSE 分支保留，`direct` 模式可切回 |
| `requests` 缺失 | 采用 `aiohttp`（已在依赖）或新增 `requests`，二选一并在 requirements.txt 落实 |
| 档案写失败 | 仅 `log.warning`，events 表为可靠底账 |

---

## 七、工作拆分建议（Git 提交粒度）

1. `M0`：`refactor(runtime): backend→runtime 术语重构（模块/DB/API/前端/测试）` —— 独立可回滚。
2. `M1`：`feat(runtime): agent_tasks 队列 + event_archive 档案 + stream_runtime 结构化` 。
3. `M2`：`feat(runtime): 回报协议驱动 event.json + event_pipeline/chat 切 enqueue（execution_mode=queue_remote / 本机 direct）`。
4. `M3`：`feat(runtime): /api/runtime/* + rta_ 凭据 + RuntimeAgent 边端（queue_remote）`。
5. `M4`：`feat(ui): Runtime 管理页 + Agent 绑定 + chat 轮询 + Event Detail`。

每个里程碑独立提交，便于按 §五 逐级发布与回滚。
