# Plan: Issue #3739 实现方案

> 基线冲突处理（rebase 到 arch-weekly-11-ljt）：Issue #4065 已在基线合入，把「非数字 ident
> 可回帖口径」收敛到 `src/kernel/issue_ident.py` 单点并落地结果评论短路 / 卡片兜底 / 指标。
> 本单的判据与短路实现**一律复用 #4065**（不再另立平行判据），本单保留验收语义 + 回归用例锁定。
> 细节见 `spec.md`「基线口径」章节。

## 改动点

### 1. `src/kernel/issue_ident.py` — 判据唯一入口（复用基线，不新增）
- 基线已提供 `deliverable_comment_ident(value, provider)`：数字平台只认数字；gitee `IK*`
  走 `/issues/{ident}/notes` 合法；`autopilot-*` / `pr_*` / 空 / `0` 一律 `""`（不可回帖）。
- 基线已提供 `normalize_card_ident` / `card_ident_number`：卡片主键允许 `autopilot-*`
  （issue 表 TEXT 主键）与 gitee `IK*`，`pr_*` 不可归一。
- 结论：本单**不新增** `is_synthetic_issue_number`（与 #4065 判据重复会导致两套实现漂移）。

### 2. `src/domains/runtime/pipeline/event_pipeline.py` — 回帖入口短路（复用基线）
- `_post_result_to_issue`：`deliverable_comment_ident` 判定不可投递 →
  - 零 `/comments` 外呼（预检 GET 与 POST 都不发）；
  - 先尝试 `_task_real_issue_number` 取真实号（任务/事件 payload 双保险），有则凭真实号回帖；
  - 无真实号 → `_record_result_comment_skipped`（`RESULT_COMMENT_SKIPPED`，可检索）+
    `_record_result_card_fallback`（`RESULT_CARD_FALLBACK`，结论截断 500 字落 `agent_tasks.errors`）。
- `_post_issue_comment_ledgered`：入口归一失败 → `RESULT_COMMENT_SKIPPED` 结构化行 + 返回 `False`，
  不构造 VCS 客户端、不外呼、不记账（调用方按 falsy 走兜底）。

### 3. `src/domains/runtime/routes/internal.py` — 卡片接口非数字号语义（复用基线）
- `issue_number: int → str`（基线已改），FastAPI 不再抛泛化 422；
- 可归一（数字 / gitee `IK*` / `autopilot-*` 合成键）→ 正常查卡片；
- 不可归一（`pr_*` / 空 / 分隔文本 / 超长）→ 400 `invalid_issue_ident` + 可读原因，不进查询层。
- 与原始设计（伪号一律 200 空卡）的差异：`autopilot-*` 是 issue 表真实主键，返回真实卡片才能
  保住卡片刷新链；噪声源 422 已彻底消除，验收目标未变。

### 4. 测试（`tests/runtime/test_issue_3739_autopilot_pseudo_issue_number.py`）
- 判据矩阵：`autopilot-*` / `pr_*` / `#` 前缀 / 空 / `0` / 垃圾形态在数字平台恒不可回帖；
  gitee `IK*`、纯数字（int / str / `#N`）不误伤（#3055 零回归）；
- `_post_result_to_issue` 伪号：`create_client` 换成断言失败桩 → 零外呼；`RESULT_COMMENT_SKIPPED`
  + `RESULT_CARD_FALLBACK` 留痕落库；`RESULT_COMMENT_SKIPPED` 结构化行可检索；
- `_post_result_to_issue` 双保险：事件 payload 带真实号 → 凭真实号回帖；
- 真实号回归：照常 GET + POST + 记账；
- `_post_issue_comment_ledgered` 伪号：返回 `False`、零客户端、零台账、有结构化跳过行；
- 卡片路由：`autopilot-*` → 200 + 卡片（不再 422，且不被短路）；`pr_*` → 400 可读；数字号照常。

## 风险与缓解
- 判据误伤真实号：`deliverable_comment_ident` 由 kernel 单点维护，gitee ident / 数字回归用例锁死；
- 卡片语义：`autopilot-*` 返回真实卡片（而非空卡）是基线既定口径，改动会断卡片刷新链，故不覆盖；
- 留痕被误读为失败：`RESULT_COMMENT_SKIPPED` 语义明确为 skip（非失败），且带 `reason`；
- 跳过可观测性：结构化行 + `comment_metrics` 指标，避免「静默吞异常」回潮。