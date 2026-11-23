# Spec: Issue #3897 — 生产 Loki 三处 P1 稳定性缺陷

> 来源：2026-11-23 15:28（Asia/Shanghai）生产 Loki（job=grape-prod）日志分析。
> 评级 ready（issue 阶段），本文件为 coding 阶段的实现口径复核，与 Issue/Spec 评论一致。

## 目标

修复生产日志暴露的三处 P1 缺陷，并同批清掉两处噪音/泄漏：

| 组 | 现象 | 目标 |
|---|---|---|
| A | `grape-worker` 每 60s `Autopilot: task 83 exec <id> failed after 0 retries`，无 cause | 失败日志带结构化 cause，可解释 0 retries |
| B | `grape_prod_runtime.external_identities` 不存在（1146），30min 抽样 113 条 | 关系存在/可读、1146 告警归零、启动期可报缺失 |
| C | `channel crashed (unexpected)` + BrokenPipe（1h 11 条）；`Webhook rejected: token mismatch` | 传输层断连按连接级退避重连、不再整通道 ERROR 崩溃；拒绝日志带指纹 |
| D | `RepoBotToken: historical column fallback hit` 3.9 万条/h；`Unclosed client session` | 回退日志降为 0（WARN 按 repo 限频）/ session 关闭 |

## 根因（代码级定位）

- **A**：`scheduler._finalize_autopilot_task` 在 `exit_code != 0` 时只写
  `status=failed`、`error=""`；`_execute_task` 读到空 error 后回落到字面量
  `failed after {max_retry} retries` —— 既无 cause，也不解释为何 max_retry=0 不重试。
- **B**：`src/kernel/persistence/uow.py::uow()` 固定绑定 `get_engine()`（= 进程主库），
  绕过了 `db_registry.route_of("mgmt")` 的域路由。worker/event 进程主库是 runtime 库，
  `external_identities`（owner=mgmt）在 runtime 只是**跨域视图**，而该表不在
  `scripts/view_create_missing_allowlist.txt` 里 → `--repair` 不会补建 → 每次身份解析
  1146（`get_user_identities_with_tokens` 逐次 WARNING）。
- **C**：飞书适配器多数路径已归一为 `ChannelError`，但仍有传输层异常（BrokenPipe 等）
  从 `connect()` 逃逸到 Supervisor 的通用 `except Exception`，被打成
  `channel crashed (unexpected)` 全栈 ERROR；Webhook 拒绝日志只有 repo，无可比对的
  token 指纹。
- **D**：`RepoBotToken._fetch` 每次历史列命中都 `log.warning`（高频解析 → 3.9 万条/h）；
  `FeishuChannel.connect` 内 `FeishuAsync()` 自建 aiohttp session 只用于取 ws endpoint，
  用完从不 close → `Unclosed client session`。

## 测试 seam

- A：`scheduler._autopilot_failure_cause(exit_code, final_text)`、`_retry_skip_reason(...)`
  纯函数 + `_execute_task` 收口日志字段（caplog 断言）。
- B：`uow(domain="mgmt")`（公开 API）、`schema_probe.relation_exists`、
  `db_setup._check_identity_relations`、`grape_account_db` 的 1146 一次性降级。
- C：Supervisor 循环（复用 `tests/runtime/im/test_supervisor_register_conflict.py` 的替身）、
  webhook 验签函数（TestClient）。
- D：`RepoBotToken._fetch` 日志限频、飞书 channel endpoint 取用后 session 关闭。

## 验收

沿用 Issue 验收标准（LogQL + 时间窗 + 前后计数），代码侧补齐：
缺表降级、重连退避、验签拒绝、回退路径四类测试；`pytest` 全绿。
