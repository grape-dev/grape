# Issue #996 Spec：变更事件文件清单与产物清理守卫解耦

## 目标
让 push / pull_request_merged / pull_request_updated 事件携带
`files{added,modified,deleted}`，并在清理 `docs/issue-N/*` 产物前评估对已提交守卫
脚本的影响，避免「审查通过 → 清理提交 → 守卫由绿转红 → QA 暂停合入并回 coding」
的一轮返工（PR #526 / 提交 558219a3 场景）。

## 范围
- 事件归一化补齐文件级变更清单，落库 `events.payload.files`；
- payload 缺失时按 provider PR files / commit diff 接口兜底补齐；
- 文件数超阈值时 `truncated=true`，但 deleted 路径全量保留；
- 新增守卫影响预检：清理 `docs/issue-N/*` 前扫描
  `scripts/check-*.sh|.py`、`tests/check-*.sh|.py` 对目标路径的引用；
- 命中则跳过删除并在事件 payload 标注 `guard_impact{scripts,breaking=true}`；
- QA prompt / pipeline events 展示 files 与 guard_impact。

## 非目标
- 第三方合并 bot 的合并/清理策略改造；
- 历史 `docs/issue-*` 目录整改与存量守卫脚本重写；
- 守卫脚本自身语义重构。

## 验收标准
- [x] push / PR merged / PR updated payload 含 `files{added,modified,deleted}`；
- [x] 超阈值 `truncated=true` 且 deleted 全量保留；
- [x] QA 上下文输出被删文件清单，可增量复核；
- [x] 守卫引用待删路径时跳过删除并标注 `guard_impact{breaking=true}`
      —— 平台补上删除链路后本仓内可端到端达成：`POST /api/git-repo/{owner}/{repo}/issues/{number}/artifacts/clean`
      （`dry_run=false` 时才删；命中守卫一律 `skipped` 并回传 `guard_impact`），
      测试 `tests/test_issue_996_artifact_clean.py`；
- [x] 无守卫引用时照常清理、不产生 guard_impact（同上，逐条 contents DELETE，失败按条上报）；
- [x] pipeline 事件可查看 files / guard_impact；
- [x] 现有测试通过 + 新增单测覆盖五类场景。