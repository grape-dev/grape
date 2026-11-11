# Plan: Issue #2041 — worktree 重置前置保全

## 设计（三段式：可证明丢失风险 → 先归档、再重置、后告知）

```
重置/删除入口（cleanup_issue_worktree / _zombie_issue_dirs rmtree / remove_issue_worktree_if_pushed）
  └─ collect_reset_risk()  前置检查
       ├─ 未推送判定：git ls-remote --heads origin refs/heads/<branch>（远端事实）
       │    ├─ 远端 SHA == HEAD            → unpushed=0（已推送，不看 @{upstream}）
       │    ├─ 远端 SHA != HEAD            → rev-list --count <remote_sha>..HEAD
       │    └─ 远端无该分支 / 探测失败      → @{upstream} → 基线 ref → --not --remotes
       ├─ HEAD 不可解析（unborn/元数据损坏）→ 仍扫 porcelain，按 dirty/untracked 判风险
       ├─ 无风险 → 原路径放行（零副作用，AC-c/AC-d）
       └─ 有风险（未推送 commit 或 白名单外 脏/未跟踪）
            └─ archive_worktree_before_reset()
                 ├─ git bundle create <path> HEAD + git bundle verify （提交链保全）
                 ├─ git diff HEAD --binary → dirty.patch              （未提交改动保全）
                 ├─ untracked 文件逐个拷贝 → untracked/                （新增文件保全）
                 │    └─ 超 10MB / 超 2000 个 / 拷贝失败 → skipped，归档不完整
                 ├─ manifest.json + {agent}-reset.json 记录            （机器可读告知源）
                 ├─ 全部成功 → 放行删除
                 └─ 任一失败或存在 skipped → 拒绝删除，保留 worktree（宁可残留，不可丢改动）
下一轮任务派发（agent._execute_task / direct_executor）
  └─ inject_reset_context()  读 {agent}-reset.json
       ├─ env: GRAPE_WORKTREE_RESET=true / GRAPE_WORKTREE_RESET_INFO=<JSON>
       ├─ prompt: 显式提示块（已归档、不可假定仍在 worktree、恢复命令 + 仅无改动时才 reset）
       └─ 注入后写 consumed_at / consumed_by_task（同一记录只告知一次）
```

## 关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 归档载体 | git bundle（+ 补丁/文件快照）而非 refs/grape-backup | ref 与 worktree 同仓，删除时一并消失不可靠；bundle 独立文件可复制、可 verify、可 fetch |
| bundle 打包范围 | `git bundle create <path> HEAD`（仅 HEAD 链） | linked worktree 与主仓共享 refs，`--all` 会把其它 issue 分支与基线一并打包（体积≈整仓、sweeper 长时间阻塞）；恢复路径只需 HEAD 链 |
| 未推送判定口径 | 远端分支 SHA 优先（`ls-remote`），本地 ref 链仅兜底 | 平台 push 用 `HEAD:refs/heads/<branch>` 不带 `-u`，worktree 分支 upstream 仍是基线分支；用 `@{upstream}..HEAD` 会把「已推送且干净」误判为有未推送内容，每轮 sweep 都产整仓 bundle 并误注入（与 `remove_issue_worktree_if_pushed` 的 `ls-remote` 口径一致） |
| 阻断粒度 | 归档成功才放行；失败拒绝删除 | 保清理流程连续性；失败硬阻断才不丢数据（守卫自身异常同判失败） |
| HEAD 不可解析 | 不早退放行，仍扫 porcelain 判风险；扫描失败则判风险 | 空仓/元数据损坏时原实现 risk=False 直接删除，与同文件「守卫异常视为风险」口径不一致 |
| 未提交改动保全 | `git diff HEAD --binary` + untracked 文件拷贝 | bundle 只含已提交对象，未提交/未跟踪必须单独落盘（Issue #11 同时丢 commit 与新增包目录） |
| 未跟踪文件超限 | 记为 `untracked_skipped` 并 ok=False（拒绝删除） | bundle/补丁都保不住未跟踪文件，"跳过"等于内容永久丢失；manifest 仍落盘供取证 |
| 告知载体 | 归档 record 文件 + 任务派发时 env/prompt 注入 | A2A context 由上一轮 Agent 手工拼装不可控；派发注入与 #516 checkpoint 同模式，是平台可控的机器可读通道 |
| 告知生命周期 | 注入即标记 `consumed_at`/`consumed_by_task`，一次生效 | record 只写不消费会让同一 (repo, issue, agent) 每轮任务都注入「已重置 + reset --hard FETCH_HEAD」，prompt 持续膨胀且可能误弃当轮改动 |
| 记录位置 | workspace repo 目录下 .grape/archive | worktree 会被删除，记录必须在删除路径之外；per-repo 隔离随 worktree 同机（跨机可见性记录 worker 主机名，见风险） |
| 归档权限 | 目录 0700、bundle/补丁/快照/记录 0600 | 未跟踪文件可能含 `.env`/凭据类内容，明文落盘时同机其它任务不可读 |
| 无对比 ref 时 | `rev-list HEAD --not --remotes` 判定，失败视为有风险 | 无 upstream 也无基线 ref 的仓库，宁可多归档一次也不放过 |

## 落点

1. `src/domains/runtime/engine/worktree_guard.py`：新增 #2041 段——
   `collect_reset_risk` / `archive_worktree_before_reset` / `load_reset_record` /
   `mark_reset_record_consumed` / `reset_record_path` / `guard_issue_workdir_removal`
   （僵尸路径闸，含新/旧/过渡期三种布局解析）/ `inject_reset_context`（注入，便于单测）。
2. `src/domains/runtime/engine/repogit.py`：`bundle_create` / `bundle_verify` /
   `fetch_from_bundle` 封装；`cleanup_issue_worktree` 加守卫（新参 token/base_branch/
   reason/task_id，默认值保持既有调用兼容；返回 True=已删 / False=保留）；
   `remove_issue_worktree_if_pushed` 透传 token/reason。
3. `src/domains/runtime/engine/cleanup.py`：done-issue 两处 sweep 调用适配返回值；
   `_zombie_issue_dirs` rmtree 前过 `guard_issue_workdir_removal`。
4. `src/domains/runtime/engine/agent.py`（worker 入口）+
   `src/domains/runtime/engine/direct_executor.py`（direct 入口）：worktree ensure
   之后调 `inject_reset_context`。
5. `tests/test_worktree_reset_archive.py`：验收 a-d + 失败路径 + 审查整改用例。

## 归档 bundle 取值（数值证据，review F3）

| 常量 | 取值 | 依据 |
|---|---|---|
| `_BUNDLE_CREATE_TIMEOUT` | 1800s | 仓库默认 git 操作超时 900s；bundle 只含 HEAD 链，体积≈本分支历史，取 2 倍覆盖大仓冷缓存打包，同时不超过 sweeper 15min 周期的 2 倍，避免单 issue 长期占用串行 sweep 线程。超时 → 归档失败 → 保留现场，下轮重试 |
| `_BUNDLE_FETCH_TIMEOUT` | 600s | 从本地 bundle fetch 无网络依赖，600s 足够；超出按异常处理（失败 → 拒绝删除） |
| `_REMOTE_PROBE_TIMEOUT` | 60s | `ls-remote --heads` 单 ref 探测，60s 覆盖慢网络；失败回退本地 ref 链，不改变保守判定 |
| `_MAX_UNTRACKED_FILE_BYTES` | 10MB | 单文件快照上限：超过即不计入保全（ok=False 拒绝删除），避免归档吃掉工作区盘 |
| `_MAX_UNTRACKED_FILES` | 2000 | 单个 issue worktree 的未跟踪文件上限；超出部分无法保全 → ok=False |

## 风险与缓解

- 归档占磁盘：bundle 只含 HEAD 链 + 单文件/文件数上限（10MB/2000），manifest 记录
  bundle 体积；保留期清理后续单独立项（Out of Scope）。
- 误阻断合法清理：仅白名单外脏/真实未推送（远端事实）命中才归档；归档成功即放行，
  不改变最终清理语义；已推送且干净的 worktree 零副作用（单测覆盖真实 push 形态）。
- 归档不完整导致目录长期残留：`untracked_skipped` 明细入 manifest 与日志，便于人工
  处理超限文件后下一轮 sweep 放行。
- 布局未知但含 `.git` 的残留：保守保留 + log.warning（不可证明无未推送内容时不 rmtree）。
- 守卫自身异常：一律拒绝删除（保留现场），sweeper 下轮重试，日志可定位。
- worktree 跨机不可访问：record 记录 worker 主机名与绝对路径，恢复命令写入
  prompt/manifest；跨机共享存储属部署层演进（Spec Open Question，本地持久目录+可配置）。