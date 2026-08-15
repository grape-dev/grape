# new-version2 用例验收计划

> 版本：v1.0
> 日期：2026-08-08
> 配套：`implementation-plan.md`（实施顺序）/ `agent-runtime-event-tech-impl.md`（技术细节）/ `concurrency-design.md`（高并发）
> 说明：按里程碑 M0→M4 组织验收用例。每条用例给出**层级、场景、前置、步骤、预期**。所有里程碑需满足 §七「有序升级检查单」才能进入下一阶段。

---

## 一、验收分层定义

| 层级 | 代号 | 执行方式 | 工具 |
|---|---|---|---|
| 单元 | UT | 直接调用函数/类 | pytest（`tests/test_*.py`，`DEVOPS_DB_PATH` 隔离） |
| 集成 | IT | 起 FastAPI `TestClient` 打 API | pytest + `tests/conftest.py` |
| 端到端 | E2E | 起真实 server + 本地/远端 RuntimeAgent，注入 webhook/chat 事件 | pytest（slow marker）/ 手工脚本 |
| UI | UI | 浏览器操作 | Playwright（`ui_*.py`） |
| 安全 | SEC | 认证/授权/脱敏 | pytest + 手工 |
| 迁移 | MIG | 旧库升级 / 旧数据兼容 | 脚本 + 手工 |

---

## 一·五、高并发专项用例（见 `concurrency-design.md`，跨 M0–M3 执行）

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| CC-1 | MIG | SQLite WAL 生效 | 起双进程（3000+8080），`PRAGMA journal_mode` 查询 | 返回 `wal`；`sessions.db-wal` 文件存在 |
| CC-2 | MIG | 双进程并发写无锁错误 | 两进程同时高频写 events/agent_tasks | 日志无 `database is locked`；busy_timeout 兜底 |
| CC-3 | UT | claim 原子抢单 | N 线程同时 claim 同一 runtime 的任务 | 仅 1 个任务被置 `dispatched`，无重复 |
| CC-4 | UT | 有界线程池 | 入队 > max_concurrent 任务 | worker 并发 ≤ max_concurrent；其余保持 queued |
| CC-5 | IT | 回报异步批量 | 大量 messages 上报 | 入队即返回；`_ingestion_loop` 攒批（≤50 条/200ms）落库；无 event loop 阻塞 |
| CC-6 | IT | 队列满降级 | 塞满 `_report_queue`(2000) | 降级同步写，不丢数据 |
| CC-7 | E2E | 并发任务吞吐 | 压测 ≥100 events/s 注入 | 入队不丢、无锁错、无线程爆炸 |
| CC-8 | UI | 轮询节流 | 500 任务并发轮询 | 1s+jitter 间隔，无超时无放大 |

---

## 二、M0 术语重构（backend → runtime）

> 验收标准：`python -m pytest tests/ -v` 全绿；`execution_mode=direct` 行为与 main 一致。

| ID | 层级 | 场景 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|---|
| M0-01 | UT | 模块迁移后导入 | M0 完成 | `from src.runtime.executor import stream_runtime, stream_agent` | 均可导入；`stream_agent` 为兼容别名（`yield from` 转发） |
| M0-02 | UT | 符号更名 | M0 完成 | `register_runtime`/`get_runtime`/`discover_runtimes` 调用 | 与旧 `register_backend` 等行为等价（回归 `test_agent_executor`） |
| M0-03 | MIG | `backend_agents` 表迁移 | 旧库含 backend_agents 数据 | 跑 `init_database()`；查表 | 表更名 `agent_runtimes`，数据（含 `pip_package`）完整；重复执行幂等 |
| M0-04 | MIG | 引用列改名 | 旧库含 `backend_id/backend_session_id/default_backend_id/backend` 列 | 跑迁移；`PRAGMA table_info` | 列已改名 `runtime_id/runtime_session_id/default_runtime_id/runtime`；无旧列残留；幂等 |
| M0-05 | MIG | 老库无新列 | 全新库 | 跑 `init_database()` | 不报错，新列按默认值建立 |
| M0-06 | IT | 后端列表 API | M0 完成 | `GET /api/settings/agent-runtimes` | 200，返回 `{"runtimes": [...]}`；旧路径 `/api/settings/backend-agents` 仍可用（301 或别名） |
| M0-07 | IT | 刷新 API | M0 完成 | `POST /api/settings/agent-runtimes/refresh` | 200；`discover_runtimes` 结果落库 |
| M0-08 | UI | 导航与文案 | server 起 | 点侧栏 → Runtime 页 | `#nav-runtimes` 高亮，标题 "Agent Runtimes"，页面渲染成功 |
| M0-09 | UI | 旧测试回归 | M0 完成 | 跑 `test_api_routes.TestSettings`（`test_backend_agents` 已更名） | 断言键 `runtimes` 通过 |
| M0-10 | UT | VCS 层未动 | M0 完成 | `from src.vcs import ...`；跑 `test_github_normalize.py` | `VcsBackend/GitCodeBackend/GitHubBackend` 命名不变，测试绿 |

---

## 三、M1 控制平面 + 事件档案（新增，不切换执行路径）

> 验收标准：`tests/test_event_archive.py`、`tests/test_runtime.py`、`tests/test_event_store.py` 全绿；`execution_mode` 仍为 `direct`。

### 3.1 事件档案（`src/api/event_archive.py`）

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| M1-A1 | UT | init 建骨架 | `init_event_json(repo_key, seq, evt, agent_type, runtime, skill)` | 文件存在；`pipeline` 含 1 段（seq/agent/runtime/skill/status=running）；`summary` 占位 `Issue #N 处理中，尚无结论` |
| M1-A2 | UT | `#0` 跳过 | `evt.issue_id="#0"` | 不建文件 |
| M1-A3 | UT | append_log 全量 + 脱敏 | 上报 3 条 log（含 token 串） | `logs` 长度=3；token 已替换为 `[REDACTED]` |
| M1-A4 | UT | finalize 覆盖 summary | `output.issue_summary` 非空 | 顶层 `summary` 被覆盖；pipeline 段 `status/exit_code/duration/completed_at` 写回 |
| M1-A5 | UT | finalize 不覆盖 | `output.issue_summary` 为空 | 顶层 `summary` 保持旧值 |
| M1-A6 | UT | 并发写 | 多线程同时 append 同 repo | JSON 不损坏，logs 不丢（每 repo 锁 + 原子替换） |
| M1-A7 | UT | 原子替换 | 写入中途异常 | 文件仍完整可读（`mkstemp`+`os.replace`） |
| M1-A8 | UT | build_issue_context | 档案含 2 段结论 | 输出含各段 `✓/✗` 状态行 + summary，长度 ≤ max_chars |

### 3.2 结构化产出契约（`event_store.py` / `prompt_builder.py`）

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| M1-S1 | UT | parse 合法 JSON 块 | `final_text` 含 `{...actions...}` | 返回 dict |
| M1-S2 | UT | evaluate 优先 JSON | `final_text` 含 JSON 块 | 用 `conclusion` 作为结果，不再走正则 |
| M1-S3 | UT | evaluate 降级 | 无 JSON 块 | 走现有正则（回归 `test_event_store` 既有断言） |
| M1-S4 | UT | prompt 注入输出要求 | `build_prompt(...)` | 末尾含「处理记录输出要求」段，五字段契约存在 |

### 3.3 RuntimeService 与任务队列（`agent_tasks`）

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| M1-R1 | UT | enqueue 必带路由 | 缺 `runtime_id` | 抛错/拒绝；`runtime_id` 为确定路由快照 |
| M1-R2 | UT | enqueue 快照 | 带 `event_seq/issue_id/repo_key` | 入队 status=queued，快照字段落库 |
| M1-R3 | UT | claim 确定路由 | 两个 runtime 各有任务 | 只 claim 到 `runtime_id` 匹配任务（不跨机抢单） |
| M1-R4 | UT | claim 串行槽 D9 | 同 session 已有 running | `NOT EXISTS` 过滤，不返回该 session 任务 |
| M1-R5 | UT | claim 空队 | 无 queued 任务 | 返回 null |
| M1-R6 | UT | 终态幂等 | `complete` 后再 `complete` | 第二次不重复 finalize/update（`update_task_status` 已终态重放安全） |
| M1-R7 | UT | 本地执行闭环（direct） | 触发事件（本机引擎） | `_execute_agent` 就地 `stream_agent` → 终态；events 表正确（不经队列，`queue_local` 已废除） |

---

## 四、M2 回报协议驱动 event.json + 执行通道收敛（direct | queue_remote）

> 验收标准：`execution_mode=queue_remote`（绑远端引擎）/ 本机 `direct` 下事件/chat/IM 三入口 E2E 全通；`direct` 一键回退通过。`queue_local` 已废除（见 `engine-default-selection-design.md` §9）。

| ID | 层级 | 场景 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|---|
| M2-01 | IT | 事件入队三连 | `queue_remote`（绑远端引擎） | 注入 GitCode webhook（`POST /webhook/gitcode`） | 依次 `append_event`→`init_event_json`→`enqueue_task`；events 有行、event.json 建段、agent_tasks 有 queued 行 |
| M2-02 | E2E | 任务完成写档案 | worker 在线 | 事件走完 claim→执行→complete | event.json pipeline 段 output 写入（actions/conclusion/...）；events 表 `result` 为结论文本；整链 status=completed |
| M2-03 | E2E | 任务失败写档案 | worker 在线 | 注入 exit_code≠0 场景 | pipeline 段 status=failed；整链 failed；events 表同步失败态 |
| M2-04 | E2E | `#0` 不建档案 | push 无 Issue | 触发无 Issue 事件 | 只写 events，不建 event.json、不入队 |
| M2-05 | E2E | 结构化产出解析 | 执行完成 | 检查 result_text 含 JSON 块 | `parse_agent_output_json` 提取五字段写入档案；summary 覆盖 |
| M2-06 | IT | chat 入队 + 轮询 | `queue_remote` | `POST /api/chat/send` | 返回 `task_id`；`GET /api/runtime/tasks/{id}/status` 轮询终态；完成返回 `result_text` |
| M2-07 | IT | chat SSE 分支保留 | `direct` | `POST /api/chat/send/stream` | SSE 流式照常（未回归） |
| M2-08 | E2E | IM 事件入队 | 飞书模拟 | 走 `process_chat_async`/`process_vod_async` | 同 M2-01 三连 + 轮询展示 |
| M2-09 | IT | 取消任务 | running 中 | `cancel_task(task_id)` | 置 `cancelled`；执行侧 kill 子进程；档案段不 finalize 为 completed |
| M2-10 | E2E | 回退验证 | `queue_remote` | 切 `execution_mode=direct` 重启 | 事件/chat 回到就地 `stream_agent`，无档案副作用 |
| M2-11 | MIG | 滞留任务处理 | 升级中遗留 queued | 手工重入队 / 置 cancelled | 队列可清理，不阻塞后续任务 |

---

## 五、M3 远端 RuntimeAgent + 凭据（queue_remote）

> 验收标准：`/api/runtime/*` 协议 + `rta_` 认证 + 本机/远端边端 E2E。

### 5.1 认证与凭据（SEC）

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| M3-C1 | IT | 生成凭据 | `POST /api/settings/runtime-credentials` | 返回 `rta_` 明文（仅一次）；DB 存 sha256 非明文 |
| M3-C2 | IT | 列表不含明文 | `GET /api/settings/runtime-credentials` | 返回列表无 `token_hash`/明文 |
| M3-C3 | IT | 吊销 | `POST .../{cid}/revoke` | `status=revoked`；该 token 立即失效 |
| M3-C4 | SEC | 无 token | 调 `/api/runtime/*` 无 Authorization | 401 |
| M3-C5 | SEC | 伪造/吊销 token | `Bearer rta_<bad>` 或已吊销 | 401 |
| M3-C6 | SEC | user token 拒收 | `Bearer` 用户登录 token 调 `/api/runtime/*` | 401（凭据隔离 D2，仅 rta_ 可过） |

### 5.2 协议端点

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| M3-R1 | IT | register 幂等 upsert | 同一凭据重复注册 | 不产生重复行；返回 server 权威 runtime IDs |
| M3-R2 | IT | heartbeat | `POST /api/runtime/heartbeat` | `last_seen_at` 更新；status=online |
| M3-R3 | IT | claim 确定路由 | 两 runtime 各有任务 | 仅返回匹配 runtime_id 的 queued 任务 |
| M3-R4 | IT | start/progress/messages | 按序上报 | status=running；`append_event_archive_log` 落档；progress 可查询 |
| M3-R5 | IT | usage | `POST /tasks/{id}/usage` | `usage_json` 落库 |
| M3-R6 | IT | complete/fail 幂等 | 二次 complete | 不重复 finalize（幂等） |
| M3-R7 | IT | recover-orphans | 存在 dispatched/running 遗留 | 启动时清理为 queued/failed 或按策略重置 |
| M3-R8 | IT | runtime_gone | claim 收到 404 | 边端本地清理 → 幂等重注册 |
| M3-R9 | UT | 健康态派生 | 构造 last_seen_at 各档 | `online/recently_lost(<5min)/offline/about_to_gc(<1d 到 GC)` 四态正确 |

### 5.3 端到端

| ID | 层级 | 场景 | 前置 | 步骤 | 预期 |
|---|---|---|---|---|---|
| M3-E1 | E2E | 本机边端 | 本机装 opencode | `RUNTIME_TOKEN=rta_... python -m runtime.agent`（`server_url=http://localhost:3000`） | 注册→心跳→claim→执行→complete；health 页在线；event.json 完成 |
| M3-E2 | E2E | 远端边端 | worker 机装同仓库 | `RUNTIME_SERVER_URL=https://<server>/` 仅外连 | 同上，穿透 NAT；任务被确定路由到该机 |
| M3-E3 | E2E | 离线任务滞留 | 绑定 runtime 离线 | 入队任务 | 任务保持 queued；health 页显示"等待 runtime 上线"；不派给其他机 |
| M3-E4 | E2E | 并发上限 | `RUNTIME_MAX_CONCURRENT_TASKS` | 超限时不 claim | 占满执行槽则不 claim（防卡 dispatched） |
| M3-E5 | E2E | 终态退避重试 | server 短暂 500 | complete 失败 | 指数退避（4s→…→64s）重试后成功 |

### 5.4 服务端资源交付（技能/凭据/A2A，见 `skill-delivery-remote.md`）

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| SD-1 | E2E | 远端任务带 official 技能 | 远端边端执行带技能任务 | worker 落地 SKILL.md+references，`DEVOPS_SKILLS_DIR` 指向本地 `.skills`，prompt `{{SKILL_DIR}}` 替换成功 |
| SD-2 | E2E | repo/user 技能（gitignore 目录） | 远端执行 repo-scoped 技能 | 随任务 `skill_files` 打包下发，worker 可读，无需跨机同步 |
| SD-3 | IT | 多技能任务 | enqueue 含多个 skill | `skill_files` 含多 key，各自落地；`DEVOPS_SKILLS_DIR` 指向公共父目录 |
| SD-4 | IT | 技能缓存命中 | 同技能重复任务 | content_hash 命中，不重复传输/写盘 |
| SD-5 | E2E | A2A 外呼 | 远端执行含 A2A 调用 | prompt `{{SERVER_URL}}` 替换为服务端可达地址，可调通 `/api/a2a` |
| SD-6 | E2E | 凭据随任务 | worker 无本地 DB | CodeArts AK/SK 与 user credentials 经 `env_extra` 注入成功 |
| SD-7 | UT | 本地模式回归 | `direct`（本机引擎） | 占位符本地也被替换（或保持绝对路径），行为与现状一致 |
| SD-8 | SEC | 技能包脱敏 | 技能内容含敏感串 | 写入 `skill_files` 前统一 `[REDACTED]` 脱敏 |

---

## 六、M4 前端（UI）

| ID | 层级 | 场景 | 步骤 | 预期 |
|---|---|---|---|---|
| M4-U1 | UI | Runtime 管理页 | 打开 `/runtimes` | 设备列表按健康四态分组（device_name/心跳/承载 CLI）；在线态徽标正确 |
| M4-U2 | UI | 凭据生成弹窗 | 点 [＋ 生成凭据] | 填名称 → 生成 → 展示 `rta_` 明文一次 + 复制按钮 + 启动命令 |
| M4-U3 | UI | 凭据吊销 | 凭据列表点吊销 | 确认后状态=revoked，不再可选 |
| M4-U4 | UI | 设备任务列表 | 设备点 [查看任务] | 弹窗展示按状态筛选的任务列表（排队/进行中/完成/失败） |
| M4-U5 | UI | Agent 绑定 runtime | Agent 详情 → Config | runtime 下拉仅在线设备可选；保存写 `runtime_id`；离线置灰 |
| M4-U6 | UI | chat 轮询 | 发送消息 | 前端轮询 `GET /api/runtime/tasks/{id}/status`；显示「任务执行中…」→ 完成展示 `result_text` |
| M4-U7 | UI | Event Detail 结构化 | 打开某事件详情 | actions/conclusion/artifacts/next_step 卡片化展示；`archive` 摘要正确 |
| M4-U8 | UI | 导航回归 | 全部侧栏入口 | `#nav-runtimes` 等新导航均可用，无 404；`?v=` 新版本号生效（无缓存旧 JS） |

---

## 七、有序升级检查单（每阶段门禁）

> 任一未过即**不得进入下一阶段**。

| # | 检查项 | M0 | M1 | M2 | M3 | M4 |
|---|---|---|---|---|---|---|
| 1 | `python -m pytest tests/ -v` 全绿（VCS 相关不改） | ✅ | ✅ | ✅ | ✅ | ✅ |
| 2 | 新增测试文件存在且通过（`test_event_archive`/`test_runtime`/`test_event_store`） | — | ✅ | ✅ | ✅ | ✅ |
| 3 | `execution_mode` 开关生效且可切换 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 4 | `direct` 模式行为与 main 一致（回归） | ✅ | ✅ | ✅ | — | — |
| 5 | 对应里程碑 E2E 用例全部通过 | — | ✅* | ✅ | ✅ | — |
| 6 | 数据备份与回滚预案就绪 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 7 | 无 `backend` 术语残留（除 VCS 层/兼容别名） | ✅ | ✅ | ✅ | ✅ | ✅ |
| 8 | 高并发专项（CC-1~CC-8）无阻断项 | ✅(WAL) | ✅(抢单/池) | ✅(异步回报) | ✅(压测) | ✅(轮询节流) |

> \* M1 的 E2E 为「本地 worker 闭环」专项用例（M1-R7），非业务入口。

---

## 八、回归清单（每次发布前跑）

- `tests/test_agent_executor.py`（改名后）——流式执行、超时终止、会话自动重建、`consume_stream_async`。
- `tests/test_api_routes.py`——auth/repos/dashboard/pipeline/agents/events/chat/settings/skills/search/webhook/token。
- `tests/test_phase9.py`——`discover_runtimes` ≥4 引擎、`register_runtime`、VCS `register_vcs_backend`。
- `tests/test_session_manager.py`、`tests/test_chat_handler.py`、`tests/test_feishu_*.py`、`tests/test_im_async_remediation.py`——会话/IM 链路。
- `tests/test_github_normalize.py`——VCS 适配层不改动验证。
- `ui_test_playwright.py` / `ui_scenario_test.py`——前端导航与后端页（已更名 `runtimes`）。
- 迁移专项：旧 `data/sessions.db` 副本升级 → `PRAGMA table_info` 对照 §二 迁移预期。

---

## 九、结论判定

1. **每个里程碑**：本阶段全部用例 + 回归清单通过 → 标记 DoD 完成，进入下一阶段。
2. **全部完成**：M0–M4 门禁全绿 + 部署按 `implementation-plan.md` §五 顺序灰度验证通过 → new-version2 收尾。
3. **任一回退**：切换 `execution_mode` 或恢复备份，记录异常并补充用例后再继续。
