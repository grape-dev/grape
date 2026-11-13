# 需求：阻止引擎报错文本被系统兜底回帖到 Issue（内部信息外泄）

- 单号：待平台分配
- 基线：`origin/weekly-10`（`40be0820a`），本地 `weekly-mys` 已对齐该提交
- 缺陷类型：公开渠道信息外泄（内部引擎/上游错误文本进 Issue/PR 评论）

## 背景

### 现象

任务因引擎或上游报错产出的纯文本（例如 `HTTP 404: Model not found`），被系统「agent 没自己发评论就补发」的兜底逻辑原样发到 Issue，形式如：

```
[VOD Agent] HTTP 404: Model not found
```

已在 grape 仓确认 29 条此类评论，最早 2026-11-04，涉及 #976、#1508、#1011 等 28 个 Issue。此类文本暴露模型名/上游服务商/内部调用链，属对外可见的内部信息。

### 已核实根因（基线 `origin/weekly-10` 逐函数核对，行号仅参考、一律以函数名锚定）

1. `src/api/event_pipeline.py::_post_result_to_issue` 的纯文本兜底只判断「非空且不以 `{`/`[` 开头」，不识别报错文本：`_conclusion` 提取失败后，任何非 JSON 文本都被截断 3000 字直接发出（函数内「conclusion 提取失败时」分支）。
2. 同一兜底在 `_post_result_to_pr`（PR 订阅场景）重复实现，行为一致。
3. 该函数同时被**系统通知**复用：token 过期提示、merge→sre 断链提示、失败摘要等也走同一入口，因此不能简单「一律拒发纯文本」，必须区分「agent 产出」与「系统通知」。
4. 调用点共 7 处（全部在本单范围内逐一分类，见 A 组）：`src/api/event_pipeline.py` 两处（Issue / PR 回帖）、`src/runtime/service.py` 三处（`complete_task` 两处、`fail_task` 一处）、`src/api/pr_merged_sre_retry.py` 一处、`src/token_resolver.py` 一处。

### 关键事实（避免误改）

- `TokenActionHandler.post_issue_comment`（`src/token_resolver.py` 各实现）把调用方给的 `body` 原样发出，**没有**同类兜底，本单不改。
- `parse_agent_output_json`（`src/api/event_store.py`）只认 5 字段 JSON，语义不得改动。
- `agent_tasks.errors` 已有「JSON 数组去重追加」通路（`_update_direct_task` / `update_task_status(errors=…)`，Issue #494），留痕必须复用，不得新增列。

## 改动范围

严格 5 组共 12 项；多改一处即打回。

### A 组：回帖入口区分来源（3 项）

- A1 `_post_result_to_issue` 与 `_post_result_to_pr`（`src/api/event_pipeline.py`）新增**必填**关键字参数 `agent_result: bool`，无默认值——强制 7 个调用点显式表态；`agent_result=False` 时完全走现有逻辑，不套用任何新增过滤。
- A2 7 个调用点按下列分类显式传参，一处不得漏改：
  - `agent_result=True`（agent 产出）：`src/api/event_pipeline.py` Issue 回帖调用点、PR 回帖调用点、`src/runtime/service.py::complete_task` 的两处 fallback。
  - `agent_result=False`（系统通知）：`src/runtime/service.py::fail_task` 的失败摘要回帖、`src/api/pr_merged_sre_retry.py` 断链提示、`src/token_resolver.py::_post_comment_via_provider`。
- A3 `agent_result=True` 时进入 B 组策略；`agent_result=False` 时保持原行为（仍做已有 comment 去重与 3 字占位跳过），不得新增拦截。

### B 组：agent 产出按策略发帖（3 项）

- B1 新增 `src/api/event_pipeline.py::_looks_like_error_text(text) -> bool`，命中任一即判为错误文本（规则固定，不得增删）：
  1. 出现 `HTTP 4xx/5xx` 形态（`HTTP\s*[45]\d\d`）；
  2. 大小写不敏感地包含 `Model not found`、`model_not_found`、`Traceback (most recent call last)`、`ECONNREFUSED`、`ETIMEDOUT`、`rate limit`、`quota exceeded`；
  3. 以 `❌` 开头（`format_stream_failure_result` 的固定前缀）；
  4. 包含 `exit_code=`，或 `HTTP 401`/`HTTP 403`；
  5. 包含 `sockettimeout`、`upstream`、`bad gateway`、`service unavailable`。
- B2 新增策略读取：`GRAPE_RESULT_POST_POLICY` 环境变量，取值 `strict`（默认）/ `clean` / `off`；非法值回落 `strict` 并 `log.warning` 一次（进程内只告警一次）。读取函数 `_result_post_policy()` 放同一文件。
- B3 三模式语义（`agent_result=True` 时生效）：
  - `strict`：只发 `parse_agent_output_json` 提取到的结构化 `conclusion`；无 `conclusion` 一律不发（**包括**看起来正常的纯文本），抑制时按 C 组留痕。
  - `clean`：有 `conclusion` 发 `conclusion`；无 `conclusion` 时，纯文本若 `_looks_like_error_text` 命中则不发，未命中则按现有 3000 字截断发。
  - `off`：`agent_result=True` 一律不发（系统通知不受影响），抑制时留痕。

### C 组：抑制留痕（2 项）

- C1 新增 `src/api/event_pipeline.py::_record_post_suppressed(task_id, seq, repo_key, issue_number, agent_type, reason, raw_text)`：把一条 JSON 对象追加进 `agent_tasks.errors`（`error_code='RESULT_POST_SUPPRESSED'`、`policy`、`reason`、`agent_type`、`issue_number`、`raw_text` 截断 500 字、`at` ISO 时间）；`task_id` 为空时按 seq 走 `_update_direct_task` 的 errors 合并通路。复用现有去重追加逻辑，不新增列、不改表结构。
- C2 留痕失败（DB 异常）只 `log.warning`，绝不影响任务终态写入与主流程；同一 `(task_id/seq, raw_text 前 200 字)` 重复抑制只记一次（幂等）。

### D 组：测试（3 项）

- D1 新增 `tests/test_result_post_policy.py`（sqlite 内存库 + 网络打桩，不压生产/远端），覆盖：
  - 判定矩阵：`HTTP 404: Model not found` / `❌ 上游瞬时抖动…` / `Traceback …` / 正常文本 `✅ 已完成：补丁见 PR` 各自在 `strict` / `clean` / `off` 下的「发 / 不发」结果；
  - `agent_result=False` 三种模式下系统通知均照常发送（断言底层 POST 被调用一次、body 原样）。
- D2 回归：`#1508` 场景（错误文本 + 无 conclusion）→ `strict`/`clean` 下均不产生评论，`agent_tasks.errors` 出现 `RESULT_POST_SUPPRESSED`；`#451` 既有断言（Issue 已有 agent 评论 → 系统跳过）保持通过。
- D3 传参完整性：用 AST 或桩函数断言 7 个调用点全部显式传 `agent_result`，不允许依赖默认值。

### E 组：一次性清单（1 项）

- E1 新增 `scripts/report_error_text_comments.py`：只读扫 `agent_tasks` / `events` 中命中 `_looks_like_error_text` 的历史 `result`，输出「可能已外泄」清单（repo_key、issue_number、seq、摘要），供人工判断；**不调用任何删除/编辑接口**（GitCode 历史评论不可删）。

## 明确禁止

1. 不改 `TokenActionHandler.post_issue_comment` 的任何实现（它没有兜底，改了会扩大爆炸面）。
2. 不改 `_post_subtask_completion_to_parent`、`_post_all_subtasks_done` 的回帖内容与语义。
3. 不改 `parse_agent_output_json` 的 5 字段语义与 `_extract_issue_id_from_text`。
4. 不改任何 HTTP 接口、响应字段、前端 JS/CSS、skill 包。
5. 不删除/编辑历史 Issue 评论；不给 GitCode 评论做任何写操作。
6. 不新增依赖、不新增数据库列/表、不顺手重构、不改无关日志级别。
7. 不动重派/重试/去重（方案 B「agent 已发则跳过」）既有逻辑。
8. **不 push、不建 MR、不部署、不重启服务**；只提交到本地 `weekly-mys` 分支并标注「待人工审批」。

## 验收标准

1. 静态核对：`git diff` 只含 A/B/C/D/E 列出的文件（`event_pipeline.py`、`runtime/service.py`、`pr_merged_sre_retry.py`、`token_resolver.py`、新增测试与脚本），改动量在预估区间。
2. `strict`（默认）下构造 `result_text="HTTP 404: Model not found"` 的 agent 产出 → 不发评论，且 `agent_tasks.errors` 新增 `RESULT_POST_SUPPRESSED`（含 raw_text 摘要）。
3. `clean` 下同一输入 → 不发评论；输入 `✅ 已完成：补丁见 PR #12` → 正常发评论，内容仍带 `[<AGENT> Agent]` 前缀与 `<!-- grape-auto-comment -->` 标记。
4. `off` 下 agent 产出零评论；系统通知（`agent_result=False`）在三种模式下均照常发出，内容与现状逐字一致。
5. `GRAPE_RESULT_POST_POLICY=weird` → 回落 `strict` + 一条 WARNING；进程内不重复告警。
6. 7 个调用点全部显式传 `agent_result`（静态断言通过），且 `complete_task` 两处 fallback 为 True、`fail_task` / 断链提示 / `_post_comment_via_provider` 为 False。
7. 回归：`pytest tests/test_result_post_policy.py` 与既有回帖相关用例全绿；`#451` 断言不变。
8. 留痕幂等：同一任务同一错误文本抑制两次 → `agent_tasks.errors` 只有一条 `RESULT_POST_SUPPRESSED`；DB 异常注入时任务终态仍正常写入。
9. 清单脚本在只读模式对存量数据跑一遍，输出条数可人工核对（不引用本单 29 条为期望值）。

## 参考资料

- 现有保护（不得破坏）：`_post_result_to_issue` 内「agent 已发评论则跳过」（方案 B 防一鱼两吃）、scene 推荐结果跳过分支、`<!-- grape-auto-comment -->` 标记与 `_ledger_record_comment` 台账。
- `format_stream_failure_result`（`src/runtime/transient_retry.py`）产出的固定前缀 `❌ ` 是 `fail_task` 系统通知的合法内容，B1 规则 3 命中它但只作用于 `agent_result=True` 路径——`fail_task` 传 False，因此失败摘要照发。这一条是硬约束，实现时不得用「全局拦截 ❌」代替来源区分。
- 规模预估：生产代码约 90–120 行（A 组 ~20、B 组 ~55、C 组 ~35、E 组 ~40 脚本另计），测试约 140–180 行。
- 测试口径：sqlite 内存库 + 网络打桩；不查询/不压生产库与远端 API。
- 数据口径：29 条为历史快照，执行前以预发复核为准；历史评论不可清理，只能出清单。