# Plan: Issue #2041 — worktree 重置前置保全

## 设计（三段式：可证明丢失风险 → 先归档、再重置、后告知）

```
重置/删除入口（cleanup_issue_worktree / _zombie_issue_dirs rmtree）
  └─ collect_reset_risk()  前置检查
       ├─ 无风险 → 原路径放行（零副作用，AC-c/AC-d）
       └─ 有风险（未推送 commit 或 白名单外 脏/未跟踪）
            └─ archive_worktree_before_reset()
                 ├─ git bundle create --all + git bundle verify  （提交保全）
                 ├─ git diff HEAD --binary → dirty.patch          （未提交改动保全）
                 ├─ untracked 文件逐个拷贝 → untracked/           （新增文件保全）
                 ├─ manifest.json + {agent}-reset.json 记录       （机器可读告知源）
                 ├─ 全部成功 → 放行删除
                 └─ 任一失败 → 拒绝删除，保留 worktree（宁可残留，不可丢改动）
下一轮任务派发（agent._execute_task / direct_executor）
  └─ inject_reset_context()  读 {agent}-reset.json
       ├─ env: GRAPE_WORKTREE_RESET=true / GRAPE_WORKTREE_RESET_INFO=<JSON>
       └─ prompt: 显式提示块（已归档、不可假定仍在 worktree、恢复命令）
```

## 关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 归档载体 | git bundle（+ 补丁/文件快照）而非 refs/grape-backup | ref 与 worktree 同仓，删除时一并消失不可靠；bundle 独立文件可复制、可 verify、可 fetch |
| 阻断粒度 | 归档成功才放行；失败拒绝删除 | 保清理流程连续性；失败硬阻断才不丢数据（守卫自身异常同判失败） |
| 未提交改动保全 | `git diff HEAD --binary` + untracked 文件拷贝 | bundle 只含已提交对象，未提交/未跟踪必须单独落盘（Issue #11 同时丢 commit 与新增包目录） |
| 告知载体 | 归档 record 文件 + 任务派发时 env/prompt 注入 | A2A context 由上一轮 Agent 手工拼装不可控；派发注入与 #516 checkpoint 同模式，是平台可控的机器可读通道 |
| 记录位置 | workspace repo 目录下 .grape/archive | worktree 会被删除，记录必须在删除路径之外；per-repo 隔离随 worktree 同机（跨机可见性记录 worker 主机名，见风险） |
| 无对比 ref 时 | `rev-list HEAD --not --remotes` 判定，失败视为有风险 | 无 upstream 也无基线 ref 的仓库，宁可多归档一次也不放过 |

## 落点

1. `src/domains/runtime/engine/worktree_guard.py`：新增 #2041 段——
   `collect_reset_risk` / `archive_worktree_before_reset` / `load_reset_record` /
   `reset_record_path` / `guard_issue_workdir_removal`（僵尸路径闸，含路径解析）/
   `inject_reset_context`（注入，便于单测）。
2. `src/domains/runtime/engine/repogit.py`：`bundle_create` / `bundle_verify` /
   `fetch_from_bundle` 封装；`cleanup_issue_worktree` 加守卫（新参 token/base_branch/
   reason/task_id，默认值保持既有调用兼容；返回 True=已删 / False=保留）；
   `remove_issue_worktree_if_pushed` 透传 token/reason。
3. `src/domains/runtime/engine/cleanup.py`：done-issue 两处 sweep 调用适配返回值；
   `_zombie_issue_dirs` rmtree 前过 `guard_issue_workdir_removal`。
4. `src/domains/runtime/engine/agent.py`（worker 入口）+
   `src/domains/runtime/engine/direct_executor.py`（direct 入口）：worktree ensure
   之后调 `inject_reset_context`。
5. `tests/test_worktree_reset_archive.py`：验收 a-d + 失败路径单测。

## 风险与缓解

- 归档占磁盘：bundle 压缩 + 单文件/文件数上限（10MB/2000），manifest 记录
  bundle 体积；保留期清理后续单独立项（Out of Scope）。
- 误阻断合法清理：仅白名单外脏/未推送命中才归档；归档成功即放行，不改变
  最终清理语义。
- 守卫自身异常：一律拒绝删除（保留现场），sweeper 下轮重试，日志可定位。
- worktree 跨机不可访问：record 记录 worker 主机名与绝对路径，恢复命令写入
  prompt/manifest；跨机共享存储属部署层演进（Spec Open Question，本地持久目录+可配置）。
