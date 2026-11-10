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
- `latest_qa_event`：按 seq 取该 Issue **同一流水线**（与 `pipeline_has_qa_node` 一致的
  scene 口径，`events.scene_id` 过滤）最新 qa 事件（`#N`/`N` 兼容）。
- `classify_qa_conclusion(status, text)`：`fail` / `pass` / `unknown`。
  - `status=failed/cancelled/error` → fail（取消同样视为未通过）；
  - 先剔除否定语境（无/非/0/no + 阻塞/阻断/Critical/Required/失败/failure/fixes…）；
  - “无法通过 / not passed”等否定式通过判 fail；
  - 失败优先（不通过、未通过、阻塞、驳回、整改、需人工复核、失败、❌…）；
  - 其次通过（通过、pass、✅…）；其余 unknown。
- `qa_gate_check`：返回 `{blocked, qa_present, event_seq, status, conclusion, reason}`；
  无 qa 节点 / 无 qa 事件 / unknown / 异常 → `blocked=False`（fail-open）。
- `gate_or_annotate`：统一门禁入口（check + 命中标注），各关闭判定点复用，避免样板散落。
- `annotate_qa_failure`：best-effort Issue 评论（含 QA 结论、事件 seq、关联 PR、
  `<!-- grape-qa-gate seq=… hash=… -->`）；标注前查询已有评论同标记实现跨进程幂等，
  seq=0 时以结论指纹区分不同失败，失败仅告警。

### 2) 关闭判定点接入
- `auto_finalize_issue_on_last_node`：节点全部 completed 后、写 completed 前检查；
  命中 → 不结单、不关 VCS、发失败标注评论，返回 `qa_failed_blocked`。
- `_review_pass` / `_complete_issue_done`：target=done 时检查；命中 → 返回
  `noop:qa_failed`，不置 completed、不落 stage=done。
- `git-repo pulls create`：`related_issue_nums` 任一 issue qa 失败 → 强制
  `auto_close_issues=false` 并标注（不阻断建 PR）。
- `git-repo pulls merge`：关联 Issue 取 PR body 的 `#N` 引用 + PR 详情 `issues`
  关联列表（覆盖 `inner_issue_nums` 显式关联但 body 无 `#N` 的旁路）；qa 失败 →
  409 阻断合入并标注；仅本门禁自身的 409 阻断，PR 详情 404/502 等上游异常 fail-open。

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
## 复审补充（PR #1094 round 3）
- GitHub 建 PR：qa fail 时 `auto_close_issues=false` 无法由 provider 表达，关联降级为
  `Related to #N`（不注入 `Closes #N`），避免经 VCS UI 合入仍自动关闭 Issue。
- 结论文本判定：否定语境补 `not/none` 与「critical/required: none」后置否定，英文失败词
  改为词边界匹配。
- 场景解析异常：`_issue_scene_key` 返回 `None`，`pipeline_has_qa_node` / `latest_qa_event`
  直接放行，不再退化为 `default` 流水线判定。
- 多 Issue PR：建 PR / 合入门禁遍历全部关联 Issue，逐一标注 qa 失败 Issue（409 列出全部）。
- `gate_or_annotate` 增加 `node_failure` 语义，`auto_finalize_issue_on_last_node` 的 qa 节点
  失败分支改走统一入口。

## 复审补充（PR #1094 round 4）
- 中文否定式通过：`_QA_NEG_PASS_RE` 补 `没有/没`，`没有通过/没通过/没有通过` 判 fail；
  `未通过/不通过/无法通过` 保持 fail。
- 中文后置否定：新增 `_QA_NEG_CN_TRAILING_RE`（阻塞/阻断 … 为零/0），否定前缀集合补
  `没` 与 `不通过/未通过`，`阻塞为零/阻塞 0/无不通过项` 不再误判 fail。
- GitHub 自动关闭旁路：`auto_close_issues=false` 时清除调用方 body 自带的
  `Closes/Fixes/Resolves #N` 并降级 `Related to #N`；无 related 列表时也由
  `_gh_auto_close` 清理；`auto_close=true` 时避免重复追加 `Closes`。
- 建 PR 门禁不再依赖 `auto_close_issues`：默认 false 时同样校验并标注 QA 失败 Issue。
- 门禁样板收敛：新增 `collect_qa_failures` / `annotate_qa_failures`，`create_pull` 与
  `merge_pull` 复用；合入门禁抽为 `_pre_merge_qa_gate`。
- 清理不可达的 `_scene_key or "default"` 分支；进程内标注缓存增加上限淘汰。
