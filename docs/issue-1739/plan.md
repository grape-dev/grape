# Plan: Issue #1739 — QA 结论门禁

## 现状与根因
- 末节点自动结单 `auto_finalize_issue_on_last_node` 只按事件 `status=failed` 判阻断；
  实际 qa Agent 常以 `status=completed` + result 文本“不通过/阻塞”收尾（例如本仓
  qa 事件 seq 62167），门禁识别不到，Issue 仍被置 completed / 关闭。
- Agent 审核收尾 `_review_pass(target=done)` / `_complete_issue_done` 无 QA 校验。
- 网关建 PR 会把 `auto_close_issues` 透传成 GitCode `close_related_issue`；
  合入 PR 亦会触发 VCS 自动关闭，两条路径都未校验 qa 结论。

## 方案
### 1) 统一判定模块 `src/api/qa_gate.py`
- `pipeline_has_qa_node`：读 Issue 场景 → 流水线 nodes，找 enabled 的 `qa` 节点。
- `latest_qa_event`：按 seq 取该 Issue 最新 qa 事件（`#N`/`N` 兼容）。
- `classify_qa_conclusion(status, text)`：`fail` / `pass` / `unknown`。
  - 先剔除否定语境（无/非/0 + 阻塞/阻断/Critical/Required）；
  - 失败优先（不通过、未通过、阻塞、驳回、整改、需人工复核、失败、❌…）；
  - 其次通过（通过、pass、✅…）；其余 unknown。
- `qa_gate_check`：返回 `{blocked, qa_present, event_seq, status, conclusion, reason}`；
  无 qa 节点 / 无 qa 事件 / unknown / 异常 → `blocked=False`（fail-open）。
- `annotate_qa_failure`：best-effort Issue 评论（含 QA 结论、事件 seq、关联 PR、
  `<!-- grape-qa-gate -->`），失败仅告警。

### 2) 关闭判定点接入
- `auto_finalize_issue_on_last_node`：节点全部 completed 后、写 completed 前检查；
  命中 → 不结单、不关 VCS、发失败标注评论，返回 `qa_failed_blocked`。
- `_review_pass` / `_complete_issue_done`：target=done 时检查；命中 → 返回
  `noop:qa_failed`，不置 completed、不落 stage=done。
- `git-repo pulls create`：`related_issue_nums` 任一 issue qa 失败 → 强制
  `auto_close_issues=false` 并标注（不阻断建 PR）。
- `git-repo pulls merge`：从 PR body 提取关联 Issue，qa 失败 → 409 阻断合入并标注；
  内部异常 fail-open，不影响既有合并门禁。

### 3) 测试
`tests/test_issue_1739_qa_close_gate.py`：
- qa fail（status=failed）→ auto_finalize 不关闭、Issue 保持可处理。
- qa fail（completed + “不通过”文本）→ 门禁识别并阻断。
- qa pass → 正常 auto_done 且关闭 VCS。
- 无 qa 节点 → 行为不变（正常关闭）。
- review-pass done 在 qa fail 时 noop。
- 建 PR 在 qa fail 时 auto_close 被强制 false。
- 合入门禁在 qa fail 时 409。

## 影响面
| 文件 | 改动 |
|------|------|
| `src/api/qa_gate.py` | 新增统一 QA 结论判定与标注 |
| `src/api/issues_center.py` | 自动结单 / 审核收尾接入门禁 |
| `src/api/git_repo_api.py` | PR 创建自动关闭、PR 合入门禁接入 |
| `tests/test_issue_1739_qa_close_gate.py` | 新增三类场景与路径测试 |
| `docs/issue-1739/*` | 需求 / 方案 / 任务 |

## 风险与回退
- 误判风险通过“否定语境剔除 + 不可判定放行 + 异常 fail-open”收敛。
- 如门禁导致合入异常，可临时移除 merge 检查点，其余路径独立生效。