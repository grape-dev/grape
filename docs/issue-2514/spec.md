# Spec: Issue #2514 — coding 工作区与「当前 Issue + 当前基线」严格绑定

> Source: Issue #2514（用户反馈后台 id 6417）Spec 评论 id 200731012。
> Branch: `feat/issue-2514`，base = 场景注入基线 `arch-weekly-11-zj`。

## 背景与实测

- 处理 Issue #2506 时，注入的 coding cwd `issues/2506/coding` 实际检出在 `feat/issue-1588`
  （3 个 commit + 49 个未提交改动），与当前 Issue 无关；Agent 只能现场 `git worktree add`
  另建受控 worktree 才安全开工（task `t_4372179048a1`，seq 24/27/30 实证）。
- 同根工单 #1939 已关闭，其修复 PR #1176（head `4a6a932b`、base `arch-weekly-11`）closed
  未合入，本次按该 PR 的实现蓝本在 `arch-weekly-11-zj` 基线重建。
- #1176 遗留 1 项 Critical：基线闸门 `expected` 被「注入的环境基线」直接覆盖，注入值在远端
  不存在时 `expected` 仍取原始注入名 → `fetch`/`rev-list` 必然失败 → 闸门恒报 `head_unreachable`，
  且 `_default_base_branch` 的回退结果不参与判定（与 #2449 同源）。本单必须修正。

## 目标

服务端在注入 coding 任务 cwd 之前，把 worktree 与**当前 Issue + 当前基线**强绑定：

1. 按 Issue 隔离目录（`issues/{N}/{agent}`），禁止跨 Issue 复用同一工作区；
2. 复用命中「归属不符 / 分支错误 / 树脏」即拒绝复用，存档证据后重建；
3. HEAD 与 `origin/<baseline>` 分叉在进入 Agent 前结构化失败；
4. 任务终态清理工作区，派发前巡检回收超期/跨 Issue 残留且不误删活跃目录。

## 非目标

- coding-runbook Step 0 自检条款（grape-skills 侧，另行配套）；
- `engine/cleanup.py` 仓数据级联清理语义；
- Agent 侧 git 操作路径（仍走 `grape git`）。

## 验收标准

- [ ] 预置检测到错误分支或脏工作区 → 拒绝复用并重建，日志含 `workspace.reuse_refused`
      与 `dirty_files=N`，证据落 `<repo>/artifacts/workspace-<task_id>*-reuse-refused.patch`。
- [ ] HEAD 与 `origin/<baseline>` 分叉 → 进入 Agent 前结构化失败，错误文本含
      `issue` / `path` / `expected_baseline` / `actual_head` / `ahead` / `behind`。
- [ ] `expected_baseline` 恒为**已核实在远端存在**的解析后基线；注入基线不存在时回退到
      默认分支，回退结果进入闸门且可在错误/日志中追溯（#1176 的 Critical 不复现）。
- [ ] 任务终态清理工作区；派发前巡检回收超 TTL 的带标记残留 / 空 orphan，活跃 cwd 白名单保护。
- [ ] 回归覆盖「错误分支 + 脏文件」预置目录，断言不在该目录直接提交而是重建干净工作区。
- [ ] 现有测试全绿。

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| worktree 的 `.git` 只读（#2245/#1891） | Med | 拒绝复用/清理失败仅记 warning、不无限重试，保留原目录 |
| 巡检误删 in-flight 目录 | High | 只回收带归属标记且超 TTL 的目录 + active_dirs 白名单 |
| 基线前移导致 coding 多回合任务分叉失败 | Med | 仅 `ahead>0 && behind>0` 才失败；`ahead>0 && behind=0` 与 `behind>0 && ahead=0`（ff）放行；`GRAPE_WORKSPACE_STRICT=0` 降级 warn |