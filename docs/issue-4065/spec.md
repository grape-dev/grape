# Issue #4065 — autopilot 任务结果回帖非数字 ident 恒 400

> Spec 来源：Issue #4065 评论（comment id 203194855，Issue Agent 发布）
> 开发基线：`arch-weekly-11-ljt`

## 1. 背景与现象

生产 Loki 1 小时实测（4 台主控）：

- `Exec agent: failed to post result comment: ... HTTP 400 {"error_message":"参数类型错误"}` 7 条；
- 底层 comment 请求 14 条全 400，ident 全部非数字（`autopilot-as/au/am/ad/bj`、gitee `IK*`）；
- `GET /internal/issues/card status=422` 244 条/小时（同一批非数字 ident）。

结论：autopilot 定时任务跑完，但结果评论、结论、卡片刷新三条链路同时断——用户与看板看不到任何结论。

## 2. 根因（源码定位）

| 位置 | 问题 |
|------|------|
| `src/domains/runtime/pipeline/event_pipeline.py:6390 _post_issue_comment_ledgered` | `issue_num` 直接字符串插值进 `/repos/{o}/{r}/issues/{ident}/comments`，无形态校验 → 非数字 ident 恒 400 |
| `event_pipeline.py:6427 _list_issue_comments_card` | 预检同样直接插值；预检 400 先抛异常，`_post_result_to_issue` 的 try/except 直接吞成一条 WARNING，连补发都不执行 |
| `event_pipeline.py:5397 _post_result_to_issue` | 失败出口只有 `log.warning`，无指标、无连续失败告警 |
| `event_pipeline.py:3354 _issue_num_from` | 只做 `strip/lstrip('#')`，`autopilot-as` 原样返回并作为「真实 issue 号」传入投递链 |
| `src/domains/runtime/routes/internal.py:102 get_issue_card` | `issue_number: int` 由 FastAPI 强转，非数字 ident 直接 422（不可读） |
| `src/api/session_store.py:766 _issue_num_or_ident` | `native_issue_number` 对非法形态原样返回 str → `get_issue_stage` 拿 `autopilot-as` 打卡片端点 → 244/h 的 422 |

autopilot 任务的 `agent_tasks.issue_id` 是统计合成键 `autopilot-<字母>`（`scheduler._autopilot_issue_ref`），**本身不是可回帖的真实 issue 号**。

## 3. 目标 / 非目标

### 目标
1. 投递与预检入口统一 ident 归一与前置校验，非数字 ident 零远程 400；
2. 非数字 ident 短路并落 `RESULT_COMMENT_SKIPPED` 结构化记录（reason/ident/repo/task）；
3. 有真实 `issue_number` 时凭它回帖；没有时结果落卡片/事件流（`RESULT_CARD_FALLBACK` 留痕）；
4. `comment_post_failed_total{reason}` 指标 + 连续失败 N 次告警，替代单条 WARNING 静默；
5. `/internal/issues/card` 先归一：可归一（数字 / 平台 ident / `autopilot-*` 合成键）返回卡片；不可归一（`pr_*`、空、垃圾、超长）返回 400 + 可读原因，不再是裸 422。

### 非目标
- 不改 autopilot 调度、任务编排、执行器；
- 不回填/补发历史已丢失的结果评论；
- 不改 GitCode 平台侧行为；
- 不改 `_issue_num_from` 的全局语义（其它 20+ 处调用依赖它取「原始 ident」）。

## 4. 验收标准

1. 非数字 ident 不产生 `/issues/{ident}/comments` 远程请求，日志出现 `RESULT_COMMENT_SKIPPED`（含 reason/ident/repo/task_id）；
2. 有效数字号时评论投递成功，预检不再因 400 短路；
3. 无有效号的结果落卡片/事件流，且卡片端点对 `autopilot-*` 可读；
4. `comment_post_failed_total{reason}` 可按 reason 聚合，连续失败达阈值（默认 5）触发告警；
5. `/internal/issues/card` 对 `autopilot-*` / `IK*` / `pr_*` 返回可读结果（卡片或 400 + 可读原因），无裸 422；
6. 现有测试无新增失败，新增用例覆盖 `autopilot-*` / `IK*` / `pr_*` 三类 ident。

## 5. 影响面

- `src/kernel/issue_ident.py`（新增卡片/投递口径归一，单一来源）
- `src/kernel/comment_metrics.py`（新增投递指标收集器）
- `src/domains/runtime/pipeline/event_pipeline.py`（投递/预检/路由/留痕/指标）
- `src/domains/runtime/routes/internal.py`（卡片端点归一 + 可读 400）
- `src/api/session_store.py`（卡片主键归一，消除 422 来源）
- `src/domains/insight/observability/__init__.py` + `deploy/alerts.yml`（指标注册与告警规则）
- `tests/runtime/test_issue_4065_*.py`（回归）

## 6. 风险

| 风险 | 缓解 |
|------|------|
| 归一后 gitee `IK*` 被误当非法 ident，破坏既有可用链路 | 投递口径按 provider 判定：`gitee` 允许平台原生大写 ident |
| 卡片端点签名 `int → str` 影响调用方 | Remote/Local/Protocol 三处类型标注同步；数值入参行为不变（仍走 `native_issue_number`） |
| 指标标签基数膨胀 | reason 限枚举（`non_numeric_ident/http_4xx/http_5xx/network/unknown`） |
| 与 #3999 的 stage 解析口径分叉 | 卡片/投递归一共用 `src/kernel/issue_ident.py` 同一实现 |