# Issue #3750 需求文档（Spec）

> 来源：Issue #3750 正文 + VOD Agent 收敛评论（2026-11-22）+ issue agent 可开发性分析（评级 ready）
> 与本仓当前基线的差异：基线已由 Issue #3897 C 组（B）落地**403 日志指纹**（`expected=`/`actual=`
> 的 `sha256:` 前 12 位 + `ip`/`xff`，见 `src/domains/runtime/routes/webhook.py::_token_fingerprint`），
> 因此本 Issue 的代码交付收窄为「**拒绝事实落库 + 按仓库速率告警**」与日志字段补全（`event`/`reason`），
> 不重复实现已闭环的指纹。

## 1. 问题陈述

`grape-dev/grape` 仓库 1 小时内出现 54 次 `Webhook rejected: token mismatch`（403），同窗口正常事件
仍然通过（Note 25 / Issue 14 / MR 8 / Push 7）。`managed_repos` 里期望 token 存在，说明是
「同一仓库配了多条 webhook、残留旧 secret」的配置侧问题。当前代码侧还有两处缺口：

1. **拒绝只是一行日志**：运维只能翻日志文本，无法按窗口做速率统计，也无法按 `(ip, token_fp)`
   反查是哪条残留旧 secret 的 hook 在发事件。
2. **无速率告警**：事件被 403 丢弃且 webhook 不重放，配置错配可以长期静默丢事件，没有任何信号。
3. **日志仍有字段缺口**：403 分支缺 `event`（`X-Gitcode-Event`）与 `reason`（`absent`/`mismatch`）；
   404 unmanaged 分支只有 `repo`，缺 `event`/`ip`。

## 2. 期望行为

### In Scope
1. **403 拒绝日志可诊断化（补全）**：`repo`、`event`、`ip`（附 `xff`）、`expected`/`actual`（`sha256:`
   短指纹）、`reason=absent|mismatch`；404 unmanaged 分支补 `event`/`ip`。
2. **拒绝事实落库** `obs_webhook_rejects`：每次 403 落一行（`repo_key`/`event`/`ip`/`token_fp`/
   `expected_fp`/`reason`/`provider`/`created_at`），支撑速率统计与「哪条旧 secret 在发事件」反查。
3. **速率告警**：同仓库 60s 内 token mismatch > 10 次、持续 5 分钟 → `firing`（`warning`，
   `dim_key=repo:<repo_key>`）；回落 `resolved`；跨仓库不聚合。
4. **GitCode webhook 列表核查与 secret 统一**（管理员手动；本 Issue 只提供定位手段）。

### Out of Scope
- 不做 webhook 重放/补偿队列（平台无事件回溯能力）。
- 不改校验算法（`hmac.compare_digest` 保留，严禁回退 #860）；不改 403 响应体。
- 不做 GitCode hook 配置自动化读写；不改 `managed_repos` 结构与 token 分发。

## 3. 验收标准

- [ ] 403 WARNING 含 `repo/event/ip/expected/actual/reason`，不依赖 INFO 行 + request_id 反查
- [ ] 404 unmanaged 日志含 `event`/`ip`
- [ ] 日志与落库均无完整 token 及任意长度前缀/子串；#860 / #3897 回归用例全绿
- [ ] 403 响应体 `{"code":403,"message":"invalid webhook token"}` 与 `compare_digest` 校验不变
- [ ] 每次 403 落一条 `obs_webhook_rejects`；写库异常/队满不影响 403 返回（有界队列丢弃并计数）
- [ ] 60s 内 >10 次且持续 5 分钟 → firing（`warning`, `repo:<repo_key>`）；回落 resolved；跨仓库不聚合
- [ ] `promtool check rules deploy/alerts.yml` 通过；种子规则可幂等 seed
- [ ] 新增测试覆盖上述行为；存量测试全绿

## 4. 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 指纹形式 | `sha256(token)[:12]` + `sha256:` 前缀 | 复用 #3897 已验收形态，不可逆、恒长、可区分新旧 secret |
| 指纹归属 | 收敛到 insight 的 `webhook_rejects.token_fingerprint`，路由侧保留同名薄包装 | 单一实现，避免落库与日志两套口径漂移 |
| 拒绝事实载体 | 新建 `obs_webhook_rejects` | `obs_log` 按文本检索，无法高效做窗口计数 |
| 写入方式 | 有界队列 + daemon 线程批量落库（队满丢弃计数） | 403 在请求路径上，绝不能阻塞事件循环/拖慢拒绝 |
| 告警维度 | 按仓库（`repo:<repo_key>`） | 全局计数会被正常流量稀释导致漏报 |
| 403 行为 | 保持拒绝 | 安全优先，降级放行等于关闭鉴权 |

## 5. 影响面

| 模块 | 改动类型 |
|------|---------|
| `src/domains/insight/schema_ddl.py` + `src/api/db_setup.py` | 新增 `obs_webhook_rejects` DDL 与建表登记 |
| `src/domains/insight/observability/webhook_rejects.py` | 新增：指纹、拒绝台账写入（有界队列）、窗口计数、留存清理 |
| `src/domains/insight/public/__init__.py` | 对外暴露 `token_fingerprint` / `record_webhook_reject` |
| `src/domains/runtime/routes/webhook.py` | 403 落库 + 日志补 `event`/`reason`；404 补 `event`/`ip` |
| `src/domains/insight/observability/alerts.py` | 新指标 `webhook_token_mismatch` + 种子规则 |
| `src/domains/insight/observability/__init__.py` | Prometheus 计数器 `grape_webhook_token_mismatch_total` |
| `deploy/alerts.yml` | 新增 `GrapeWebhookTokenMismatch` |
| `src/api/main.py` / `src/api/server_tasks.py` | 留存清理挂点 |
| `tests/insight/`、`tests/runtime/`、`tests/deploy/` | 新增用例 |