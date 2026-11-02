# Plan — Issue #680

> 与 PR #810 设计文档 `docs/weekly-09/pr-merged-issue-resolution-max-design.md` 对齐；
> 单文件改动（src/api/event_pipeline.py）+ 新测试文件，可整体 revert 回滚。

## 改动方案

### 1. 新增两个纯函数 helper（event_pipeline.py）

- `_norm_issue_id(s)`：issue_id 规范化——剥全部前导 `#`、仅纯数字、编号 0/非数字
  返回 ""，统一单 `#` 前缀。修复 `f"#{num}"` 在 num 已带 `#` 时拼出 `##274`。
- `_max_issue_from_issues(issues, exclude_num)`：issues 列表取编号最大；
  `exclude_num` 跳过混入的 PR 自身编号。

### 2. `resolve_issue_id`

- issues 兜底段：遍历取第一个 → `_max_issue_from_issues`（同型歧义一并收敛）。
- 各 `f"#{num}"` 出口 → `_norm_issue_id(num)`。

### 3. `resolve_pr_issue_id`（优先级重排）

旧：title → issues 第一个 → 分支 → API（title → head.ref）。
新：**分支 issue-N → title #N（保留 #252 自引用跳过）→ issues 最大（跳过 PR 自身
编号）→ PR 详情 API 兜底（head.ref → title → issues 最大，同规则）**。
分支提取同时覆盖 source_branch / ref / pull_request.head.ref（GitHub 形态）。

### 4. `_resolve_event_issue_id(evt, repo_key, api_token="")`（统一入口）

- 每事件只解析一次，结果挂 `evt["_resolved_issue_id"]`；命中缓存直接返回。
- PR 类事件：resolve_pr_issue_id → resolve_issue_id 兜底；其余：resolve_issue_id；
  最终兜底 `evt["issue_id"]`。
- token 补偿：非空结果一经产生即锁定；全空且后续调用带 token 时允许补一次
  PR 详情 API 兜底（`_resolved_issue_id_token_tried` 防重复调用），结果同样写回缓存。

### 5. 7 处调用点全部改为消费统一值

| 位置 | 原逻辑 | 新逻辑 |
|------|--------|--------|
| `_resolve_issue_stage_agent`（阶段路由） | 就地 resolve_pr/resolve_issue | 新增 `evt` 参数 → `_resolve_event_issue_id` |
| `_dispatch_subscription` ×3（no_skill / agent_disabled / 建事件） | `resolve_issue_id or evt.issue_id`（PR 事件会取 issues 第一个） | `_resolve_event_issue_id` |
| `run_pipeline` `_pre_issue_id` | resolve_issue_id → PR 兜底 resolve_pr_issue_id | `_resolve_event_issue_id` |
| sre 特例（PR merged qa→sre） | 带 token 就地解析 | `_resolve_event_issue_id(api_token=...)` |
| `_early_issue_id` | 同上无 token | `_resolve_event_issue_id` |
| 主解析（PR 类/非 PR 类两分支） | 就地解析 | `_resolve_event_issue_id`（PR 分支带 repo token） |

## 测试方案

新增 `tests/test_event_pipeline_pr_issue.py` 六组用例：事故回放（PR #116 三形态）、
优先级顺序（分支>标题>issues 最大）、resolve_issue_id 取最大（Issue #89 行为保持）、
GitHub 形态不变、_norm_issue_id 双井号、统一入口只算一次 + token 补偿。
回归：test_issue_252 / test_qa_skill::TestResolveIssueIdPR / test_issue_89 /
test_pipeline* / test_issue_528 / 426 / 415 / 544 等套件与基线逐一比对（基线
预存失败不在本次范围）。

## 风险与回滚

- 单文件 + 新测试文件，`git revert` 即可回滚。
- 兼容性锚点：#252 自引用跳过、#89 object_attributes 优先、#528 PR 订阅伪
  issue_id、GitHub 无 issues 列表形态——均有测试覆盖。
