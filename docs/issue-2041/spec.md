# Spec: Issue #2041 — worktree 重置丢失未推送的本地提交（实现规格）

> 承接 Issue Agent 已发布 Spec（评论 200017873，评级 ready）。本文档把 Spec 的
> Affected Modules 映射到本分支（arch-weekly-11）的实际代码入口（Spec 发布时项目
> 知识基线 degraded，模块路径为目录推断，落改前已逐一确认）。

## Objective

worktree 重置/清理流程不静默丢失未推送内容：重置前归档保全（git bundle + 工作区
快照 + manifest），归档成功才放行；重置后以机器可读字段告知下一轮任务，杜绝
「实现已在本地」的过时自述被无校验继承（Issue #11 任务 t_3700b87233c0 实证返工）。

## 实际入口确认（本分支，区别于 Spec 的目录推断）

| Spec 推断路径 | 本分支实际路径 | 结论 |
|---|---|---|
| src/runtime/worktree_guard.py | src/domains/runtime/engine/worktree_guard.py | 守卫落点（已有 #752 推送守卫/保真校验，同文件扩展） |
| src/runtime/repogit.py | src/domains/runtime/engine/repogit.py | bundle 封装 + `cleanup_issue_worktree`（sweeper 无条件删除入口） |
| src/runtime/checkpoint.py | src/domains/runtime/engine/checkpoint.py | 任务进度快照抽象，不复用于归档（语义不同），仅借鉴原子写 |
| src/runtime/cleanup.py | src/domains/runtime/engine/cleanup.py | sweep 入口 + `_zombie_issue_dirs` rmtree（worker 侧无 DB 时唯一删除入口） |
| src/runtime/service.py | —（本分支 service.py 无 worktree 重置/清理入口） | 不改动 |
| src/api/a2a_context.py | src/api/a2a_context.py（A2A 去重键） | 不改动；下一轮告知走任务派发注入（见下） |
| task_activity.py / message_report.py | src/domains/runtime/engine/ 同名文件 | 不改动；机器可读记录由归档 record 承载 |

本分支不存在 `git reset --hard` 型 worktree 重置（生产事故中的 reset 来自更新版
预置代码，不在 arch-weekly-11）；实际会静默丢弃未推送内容的入口为：

1. `repogit.cleanup_issue_worktree`：sweeper 对 done Issue 无条件
   `worktree remove --force` + `rmtree`（未推送提交/脏文件直接丢失）；
2. `cleanup._zombie_issue_dirs` → `shutil.rmtree`：超 TTL 兜底删除（worker 端
   无 DB 表时是唯一清理路径，同样无保全）；
3. `repogit.remove_issue_worktree_if_pushed`：仅核对 HEAD==远程，未提交改动
   仍会被随后的删除丢弃（通过 1 的内部调用触发）。

## In Scope

- 重置前置检查：`git rev-list --count @{upstream}..HEAD`（无 upstream 回退对比
  基线分支 origin/<base> → origin/main → origin/master → 本地同名 ref）+ 
  `git status --porcelain --untracked-files=all`（未提交/未跟踪）。
- 命中风险 → 归档到 `{WORKSPACE_DIR}/{provider}/{owner}/{repo}/.grape/archive/{issue}/{timestamp}-{rand}/`：
  `git bundle create worktree.bundle --all` + `git bundle verify` 校验 +
  `git diff HEAD --binary` 补丁（未提交改动不在 bundle 内）+ untracked 文件内容拷贝
  + `manifest.json`；归档成功才放行删除，失败拒绝（宁可残留，不可丢改动）。
- 机器可读记录：`.../{issue}/{agent}-reset.json`（原子写、每次覆盖为最新），
  字段 `worktree_reset / lost_commits[] / bundle_path / archived_at / reset_reason /
  unpushed_count / dirty / untracked / head / branch / recovery / worker`。
- 下一轮任务注入（worker `_execute_task` + direct `direct_executor`，与
  Issue #516 checkpoint 注入同模式）：
  env `GRAPE_WORKTREE_RESET=true` + env `GRAPE_WORKTREE_RESET_INFO=<record JSON>`
  + prompt 显式提示块（含恢复命令 `git fetch <bundle> && git reset --hard FETCH_HEAD`）。
- 脏检测白名单：平台轻文件（.git-askpass*.sh、.skills/、.project-info/、.knowledge/、
  .grape-workspace.json）不触发归档。

## Out of Scope

- pre-push branch_guard / distributed_locks 缺表（#2028、#1943 一族）。
- coding-runbook 保全/恢复分支（grape-dev/grape-skills 跨仓跟进）。
- 归档目录的保留期清理任务（记录体积入 manifest，清理策略后续单独立项）。

## 验收（沿用 Spec a-d）

- a) 命中风险时删除被拦截直到归档成功；`git fetch <bundle> && git reset --hard
  FETCH_HEAD` 可恢复未推送提交（含新增包目录文件经 untracked 快照恢复）。
- b) 下一轮任务可机器读取 `worktree_reset=true`、`lost_commits[]`、`bundle_path`。
- c) 无风险路径行为与现状一致（不产 bundle、无额外阻塞），现有测试全通过。
- d) push 正常路径（HEAD==upstream/远程且干净）零副作用。

## 测试

`tests/test_worktree_reset_archive.py`：clean / dirty-untracked / unpushed-commit /
upstream 已推送 / 无 upstream 基线回退 / 归档失败拒绝 / bundle verify 失败拒绝 /
守卫异常拒绝 / 注入字段 / 白名单排除 / 僵尸路径解析。运行：`python -m pytest
tests/test_worktree_reset_archive.py tests/runtime/test_repogit.py
tests/runtime/test_issue_752_worktree_guard.py`。
