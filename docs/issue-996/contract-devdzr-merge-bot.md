# Issue #996 跨仓集成契约：devdzr-merge-bot 产物清理守卫预检

## 背景与边界

触发场景（PR #526 / 提交 558219a3）：devdzr-merge-bot 在自动合入前提交删除
`docs/issue-N/*`，仓库内已提交的守卫脚本（`scripts/check-*.sh|.py`、
`tests/check-*.sh|.py`）仍引用这些路径，导致守卫由绿转红、QA 暂停合入并回
coding，多耗一轮。

本仓（grape）负责**事件侧**：变更事件携带 `payload.files`，并在 prompt /
pipeline events 展示被删文件清单，供 QA 增量复核。
清理动作原先约定由 **devdzr-merge-bot** 执行，因此「删除前守卫预检」需要该仓接线。
复审后本仓补齐了删除链路（Issue #996 R5）：平台现在提供端点，下游可直接调用而无需重写预检：

```
POST /api/git-repo/{owner}/{repo}/issues/{number}/artifacts/clean
  {"dry_run": true, "ref": "", "message": ""}
```

- `dry_run` 默认 true：只返回 `paths` / `guard_impact`，不发 DELETE；
- `guard_impact.breaking=true` → `skipped=paths`、`deleted=[]`（永不绕过）；
- `dry_run=false` 且无命中 → 逐条 `contents DELETE`（带 sha），返回
  `deleted[]` 与 `failed[{path,error}]`，单条失败不中断整批，也不静默吞掉。

## 平台已交付的可复用能力

`src/runtime/cleanup.py`：

- `ArtifactScope(repo_key, ref, issue_number, token="", provider="")`
- `precheck_issue_artifacts(scope, targets=None, scripts=None)`：只读预检，
  返回 `{targets, paths, guard_impact}`；
- `clean_issue_artifacts(scope, targets=None, scripts=None, remover=None)`：
  命中 `guard_impact.breaking=true` 时跳过删除；否则调用注入的 `remover`。

`src/vcs/guard_impact.py`：

- `scan_guard_impact(target_paths, scripts)`：纯函数匹配；
- `fetch_guard_scripts(repo_key, ref, token, provider)`：contents API 拉取守卫文本；
- `list_issue_artifacts(repo_key, ref, issue_number, token, provider)`：列产物。

## 事件数据契约（平台 → 下游）

变更类事件（`push` / `pull_request_merged` / `pull_request_updated`）的
`payload` 可能包含：

```json
{
  "files": {
    "added": [{"path": "src/new.py", "sha": "..."}],
    "modified": [{"path": "src/app.py"}],
    "deleted": [{"path": "docs/issue-996/spec.md"}],
    "truncated": false
  },
  "guard_impact": {"scripts": ["scripts/check-docs.sh"], "breaking": true}
}
```

- `deleted` 全量保留，不随 `truncated` 截断；
- 同一 push 内同一路径的 add/modify→delete 会同时出现在对应桶，deleted 不丢项；
- `guard_impact` 仅由清理动作的生产者写入（本仓提供结构定义，不伪造）。

## devdzr-merge-bot 接线要求

1. **删除前预检**：删除 `docs/issue-N/*` 前，对全部待删路径执行等价的
   `scan_guard_impact`（可移植参考 `src/vcs/guard_impact.py`，纯函数、无平台依赖）。
   守卫脚本范围：`scripts/check-*.sh|.py`、`tests/check-*.sh|.py`；匹配完整路径
   与父目录引用（`docs/issue-N/`、`docs/issue-N`、`docs/issue-N/*`）。
2. **命中即跳过**：任一守卫引用待删路径（`breaking=true`）时**不得删除**，
   保留产物并记录命中脚本列表；不得以「先删再修守卫」方式绕过。
3. **回写影响**：命中时把 `guard_impact{scripts, breaking}` 写入清理事件/
   通知的 payload（与本仓结构一致），使 QA prompt 与 pipeline events 可直接展示；
   未命中不产生 `guard_impact` 字段。
4. **失败保守**：守卫拉取/解析失败时按 fail-open 处理（不阻断合入），但需记录
   告警；禁止因预检失败而静默删除。
5. **可追溯**：清理提交的变更清单应能被 `push` 事件归一（平台侧
   `normalize_files` 已支持 commits.added/modified/removed、files、changes）。

## 验收

- 跨仓接线后，用「守卫引用待删路径」的仓库样本验证：清理被跳过、事件带
  `guard_impact.breaking=true`、QA 无需全量重审；
- 无引用样本验证：清理照常、无 `guard_impact` 字段；
- 本仓 task.md 的 T5 在该接线完成后再勾选。