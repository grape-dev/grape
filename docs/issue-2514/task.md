# Task: Issue #2514 — 工作区与 Issue/基线绑定

## 拆解与状态

- [x] T1 `repogit.py`：归属标记 + `info/exclude`（`.grape-workspace.json`）
- [x] T2 `repogit.py`：复用三道闸门（stale_marker / wrong_branch / dirty）+ 存档重建
- [x] T3 `repogit.py`：`_resolve_base_branch` 基线解析锁定 + `_validate_issue_baseline` 硬校验
      （修正 #1176 Critical：expected 不被注入值覆盖，回退结果进入闸门）
- [x] T4 `repogit.py`：`remove_issue_worktree_if_pushed` 终态清理 + `patrol_workspaces` 巡检
- [x] T5 `agent.py`：派发前闸门阻断（`WorkspaceBaselineError` → `_error`，不进入 Agent）
- [x] T6 `agent.py`：终态清理传 `task_id`；`_run_loop` 派发前巡检 + `main()` 启动巡检
- [x] T7 `direct_executor.py` / `event_pipeline.py`：direct 路径同语义
- [x] T8 `tests/test_issue_2514_workspace_binding.py`：回归（干净直通/ff、脏树拒绝、错误分支、
      无标记残留、分叉结构错误、STRICT=0 降级、终态清理后重建、巡检回收与活跃保护、
      基线回退溯源）

## 验证命令

```bash
python3 -m pytest tests/test_issue_2514_workspace_binding.py -q
python3 -m pytest tests/runtime/test_repogit.py tests/test_worktree_reset_archive.py -q
```

## 遗留

- coding-runbook Step 0 自检条款属 grape-skills 侧，另行配套提交（本单不含）。
## 验证结果（本单实测）

```
tests/test_issue_2514_workspace_binding.py                       18 passed
tests/runtime/test_repogit.py + tests/test_worktree_reset_archive.py  64 passed
tests/arch（含静默 except ratchet / 平台白名单 ratchet）              27 passed
worktree 相关 6 文件合计                                          107 passed
```

- **无回归口径**：`tests/runtime` 全量在「HEAD 干净树（git archive 落地）」与「本单树」各跑一遍，
  逐文件失败集做差集后重跑并比对——失败**测试 id 完全相同**（67 failed / 519 passed 两侧一致），
  差异仅 `[diag] event loop stalled` 的时间戳文本；即本单未引入运行时回归，存量失败与基线无关环境有关。
- **静默 except ratchet**：新增 3 处 `except: pass` 已改为 `log.warning` / 抽 `_drop_fidelity_baseline`
  复用，`tests/arch/test_silent_except_ratchet.py` 2 passed（基线文件未上调）。
- **Critical 自查（PR #1176 遗留）**：`test_injected_baseline_absent_falls_back_visibly` 断言注入
  `arch-weekly-11-zj` 不存在时 `expected` 取回退基线 main、有 `workspace.baseline_fallback` 可溯源、
  且不出现 `head_unreachable`；`test_diverged_error_carries_requested_baseline` 断言分叉错误同时携带
  `expected_baseline=main` 与 `requested_baseline=arch-weekly-11-zj`。
