# Plan: Issue #752 — 冲突残留防护门禁实现方案

> 基线分支: weekly-08-mys | 开发分支: feat/issue-752 | 里程碑: M1 门禁 → M2 保真 → M3 测试

## 1. 新增模块

### 1.1 `src/api/merge_gate.py`（M1 合入门禁）
- 正则：`^<<<<<<< ` / `^>>>>>>> `（硬命中，阻断）；`^=======$`（软命中，告警）。
- `scan_pull_files(files)`：遍历 PR files 响应，逐文件解析 diff patch 的**新增行**（`+` 前缀去掉后匹配），返回 `{hard_hits: {file: n}, soft_hits: {file: n}}`。
- `run_merge_gate(owner, repo, number, p, token)`：经 `git_repo_api._vcs_req` GET `/pulls/{n}/files?per_page=100` 取变更文件并扫描；任何异常 fail-open 返回 `passed`（记 warning）。决策：hard 非空 → `blocked`；仅 soft → `warned`；否则 `passed`。
- `record_gate_log(repo_key, pr_number, phase, decision, hits)`：INSERT `grape_t_merge_gate_log`，best-effort（异常仅告警，不阻断合入）。

### 1.2 `src/runtime/worktree_guard.py`（M2 保真/推送守卫）
- `scan_staged_files(wt_dir)`：`git diff --cached --name-only -z` 列暂存文件，读内容（≤5MB、文本）扫描硬/软标记。
- 指纹：`snapshot_functional_files(wt_dir)` = `git ls-files` 排除产物路径白名单（dist/build/out/node_modules/.skills/.project-info/.knowledge/__pycache__）→ `{rel: sha256}`；`save_baseline(wt_dir)` 写 `<wt_dir>.fidelity.json`（worktree 外兄弟文件）；`verify_fidelity(wt_dir)` 返回 `{modified: [], removed: []}`。
- 全部 git 调用走 `repogit._run_git`（超时/异常安全）。

## 2. 接线

| 挂点 | 文件 | 改动 |
|------|------|------|
| 合入门禁 | `src/api/git_repo_api.py::merge_pull` | 合入前 `run_merge_gate`：blocked → 409 + 日志；warned → 告警放行。合入成功后二次扫描：命中 → alarmed 日志；否则 passed 日志 |
| worktree 基线 | `src/runtime/repogit.py::ensure_issue_worktree` | 建/复用 worktree 后 `save_baseline`（任务开始=清理前指纹） |
| 推送守卫 | `src/runtime/agent.py::_submit_linked_repo_prs`；`src/api/event_pipeline.py` direct linked PR 同段 | `add -A` 后 commit 前 `scan_staged_files`：硬命中 → 跳过 commit/push（result status=blocked）+ 告警；软命中告警。另 `verify_fidelity` 告警（不阻断） |
| 清理保真 | `_cleanup_task_workdir`（agent.py）/ `_cleanup_direct_workdir`（event_pipeline.py） | 卸载 worktree 前 `verify_fidelity`：modified/removed 非空 → 告警日志（AC2 清理前后指纹比对） |
| 建表 | `src/api/db_setup.py` | `_MERGE_GATE_LOG_DDL` + `_NEW_TABLES` 注册 + safe 迁移 `202611030001 create-merge-gate-log-table`（幂等） |

## 3. 表结构
```sql
CREATE TABLE IF NOT EXISTS grape_t_merge_gate_log (
    id         TEXT PRIMARY KEY,        -- uuid（新表主键一律 UUID）
    repo_key   TEXT NOT NULL DEFAULT '',
    pr_number  INTEGER NOT NULL DEFAULT 0,
    phase      TEXT NOT NULL DEFAULT '',     -- pre_merge | post_merge
    decision   TEXT NOT NULL DEFAULT '',     -- blocked | warned | alarmed | passed | error
    hits_json  TEXT DEFAULT '',             -- 命中文件/次数明细
    created_at TEXT DEFAULT ''
)
```

## 4. 兼容性
- `merge_pull` 成功路径返回体不变（`{merged, sha}`），门禁仅在 blocked 时新增 409 响应；
- 所有新逻辑 fail-open（异常告警不阻断存量流水线）；
- 基线文件在 worktree 外，不进 agent 提交。

## 5. 测试（M3）
- `tests/test_issue_752_merge_gate.py`：扫描函数命中/软命中/误报（markdown 下划线）；merge 路由硬命中 409 + 软命中/干净放行 + 合入日志落库断言。
- `tests/test_issue_752_worktree_guard.py`：暂存扫描硬命中阻断语义、指纹快照/篡改/删除检出、产物路径排除、基线在 worktree 外。
- 回归：`tests/test_issue_558_merge_passthrough.py` 全绿（合并透传行为不变）。
