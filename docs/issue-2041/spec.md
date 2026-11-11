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

- 重置前置检查：未推送判定以远端分支事实为准（`git ls-remote --heads origin
  refs/heads/<branch>` → 远端 SHA==HEAD 即已推送；远端落后则 `rev-list --count
  <remote_sha>..HEAD`；远端无该分支/探测失败才回退 `@{upstream}` → 基线分支
  `origin/<base>` → `origin/main` → `origin/master` → 本地同名 ref → `--not
  --remotes`）+ `git status --porcelain --untracked-files=all`（未提交/未跟踪，
  rename/copy 两列均判）。HEAD 不可解析（unborn/元数据损坏）不再早退放行，仍按
  工作区扫描判风险。
- 命中风险 → 归档到 `{WORKSPACE_DIR}/{provider}/{owner}/{repo}/.grape/archive/{issue}/{timestamp}-{rand}/`：
  `git bundle create worktree.bundle HEAD`（仅 HEAD 链）+ `git bundle verify` 校验 +
  `git diff HEAD --binary` 补丁（未提交改动不在 bundle 内）+ untracked 文件内容拷贝
  + `manifest.json`；归档成功才放行删除，失败或存在未保全的未跟踪文件拒绝
  （宁可残留，不可丢改动）。归档目录 0700、产物 0600。
- 机器可读记录：`.../{issue}/{agent}-reset.json`（原子写、每次覆盖为最新），
  字段 `worktree_reset / archive_complete / lost_commits[] / bundle_path /
  archived_at / reset_reason / unpushed_count / dirty / untracked /
  untracked_skipped[] / head / branch / head_missing / compare_ref / remote_head /
  recovery / worker`，另含 `consumed_at / consumed_by_task`（注入后写入）。
- 下一轮任务注入（worker `_execute_task` + direct `direct_executor`，与
  Issue #516 checkpoint 注入同模式）：
  env `GRAPE_WORKTREE_RESET=true` + env `GRAPE_WORKTREE_RESET_INFO=<record JSON>`
  + prompt 显式提示块（含恢复命令 `git fetch <bundle> && git reset --hard FETCH_HEAD`
  与「仅当本轮工作区无改动时才执行恢复」前置条件）。同一记录只注入一次：注入即
  标记 consumed，后续轮次零注入。
- 僵尸/兜底删除闸支持三种 workdir 布局：`{provider}/{owner}/{repo}/issues/{n}/{agent}`、
  `{owner}/{repo}/issues/{n}/{agent}`、过渡期 `{repo}/{agent}/issues/{n}`；含 `.git`
  但布局无法解析时保守保留（不静默 rmtree）。
- 脏检测白名单：平台轻文件（.git-askpass*.sh、.skills/、.project-info/、.knowledge/、
  .grape-workspace.json）不触发归档。

## Out of Scope

- pre-push branch_guard / distributed_locks 缺表（#2028、#1943 一族）。
- coding-runbook 保全/恢复分支（grape-dev/grape-skills 跨仓跟进）。
- 归档目录的保留期清理任务（记录体积入 manifest，清理策略后续单独立项）。

## 验收（沿用 Spec a-d）

- a) 命中风险时删除被拦截直到归档成功（含未跟踪文件全部保全；超 10MB/超 2000 个/
  拷贝失败视为未保全 → 拒绝删除）；`git fetch <bundle> && git reset --hard
  FETCH_HEAD` 可恢复未推送提交（含新增包目录文件经 untracked 快照恢复）。
- b) 下一轮任务可机器读取 `worktree_reset=true`、`lost_commits[]`、`bundle_path`；
  同一记录只注入一次（consumed 后不再注入）。
- c) 无风险路径行为与现状一致（不产 bundle、无额外阻塞），现有测试全通过。
- d) push 正常路径（远端 SHA==HEAD 且干净）零副作用——按平台真实推送形态
  （`git push origin HEAD:refs/heads/<branch>`，不带 `-u`）验证。

## 审查整改（PR #1228 第 1 轮）

| 审查项 | 整改 |
|---|---|
| Required#1 未跟踪内容静默跳过（>10MB / >2000 / copy 失败） | 跳过项记 `untracked_skipped[]`（reason + path），`archive_complete=false`，`ok=False` 拒绝删除；manifest 仍落盘取证；补三类用例 |
| Required#2 reset record 只写不消费 | 注入后写 `consumed_at` / `consumed_by_task`，同一记录只注入一次；prompt 增加「仅当本轮工作区无改动才 reset」前置条件；补「只注入一次」用例 |
| Required#3 `rev-parse HEAD` 失败即放行 | 不再早退：`head_missing=true` 后仍扫 porcelain 判风险，扫描失败判风险；补「unborn HEAD + 未跟踪文件 → 拒绝删除」用例 |
| Could rename 仅判 X 列 | `xy[0]` 与 `xy[1]` 均判 R/C，未暂存 rename 不再错位；补用例 |
| 建议 归档目录权限 | 归档目录 0700，bundle/补丁/快照/记录 0600 |
| 建议 `bundle --all` 阻塞 sweeper | 收敛为 `git bundle create <path> HEAD`，并提取 `_BUNDLE_CREATE_TIMEOUT` / `_BUNDLE_FETCH_TIMEOUT` 具名常量（rationale 见 plan.md） |
| 架构守护 F1 已推送 worktree 被误判未推送 | 未推送判定改为远端 `ls-remote` 事实优先，`@{upstream}` 仅兜底；补真实 push 形态（不带 `-u`）零副作用用例 + 远端落后用例 |
| 架构守护 F2 过渡期旧布局绕过保全闸 | `parse_issue_workdir` 支持 `{repo}/{agent}/issues/{n}`；含 `.git` 但布局未知时保守保留 |
| 架构守护 I1 `--all` 打包整仓 refs | 同「bundle 范围」整改，`bundle list-heads` 断言只含 HEAD |
| 架构守护 I5 归档明文 | 归档目录/产物权限收紧，plan.md 记录权限要求 |

## 测试

`tests/test_worktree_reset_archive.py`（36 例）：clean / dirty-untracked / unpushed-commit /
已推送（带 upstream 与真实不带 -u 推送形态）/ 远端落后 / 无 upstream 基线回退 /
归档失败拒绝 / bundle verify 失败拒绝 / 守卫异常拒绝 / 未跟踪超限与拷贝失败拒绝 /
注入字段与只注入一次 / consumed 不影响 manifest / 白名单排除 / 僵尸路径三种布局 /
HEAD 不可解析 / 未暂存 rename / 归档权限 / bundle 范围 / 超时常量。运行：`python -m
pytest tests/test_worktree_reset_archive.py tests/runtime/test_repogit.py
tests/runtime/test_issue_752_worktree_guard.py`。
