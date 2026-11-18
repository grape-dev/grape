# Issue #2314 方案设计

## 根因链

```
pre-push 钩子 _hook_main(repo_key)
  → acquire_branch_lease(repo_key, pushed)                # ③ 未传 expected
      → 分支首次无状态时不写 branch_guard_state
  → reconcile_pending(repo_key, pushed, remote_sha)       # ② pending 为空 → 直接返回陈旧 expected
  → check_push_allowed(expected, remote_sha)              # ① expected != remote 等值比较即 remote_drift
      → 远端 head 只是「上一次 push 已落地的祖先」，属合法 fast-forward → 误阻断
        → 现场只能 --no-verify 旁路，等于绕过守卫
```

## 设计决策

| 决策 | 选项 | 选择 | 理由 |
|---|---|---|---|
| fast-forward 判据 | 等值比较 / 祖先关系 | 祖先关系 | 与 git 对 `refs/heads/*` 非 force 更新的 fast-forward 语义一致 |
| 判据方向 | A：expected 是 remote 祖先 / B：remote 是 local 祖先 / C：B 主 + A 兜底 | C | B 精确覆盖钩子现场；A 覆盖服务端无 local sha 的路径 |
| 放行后是否前移 expected | 不刷新 / 前移到 remote_sha | 前移 | 否则每次 push 都走兜底分支，日志噪声与后续误判风险仍在 |
| merge-base 无法判定 | fail-open / fail-closed | fail-closed | 无法证明是 fast-forward 就不能放行；与旧行为（不一致即阻断）等价，不放大风险 |
| 陈旧 expected 自愈位置 | `check_push_allowed` 内部写库 / `reconcile_pending` | `reconcile_pending` | 保持 `check_push_allowed` 纯判定（不写库），写路径集中在一处 |
| 钩子版本治理 | 重新实现 / 保持 #2028 | 保持 #2028 | `HOOK_TEMPLATE_VERSION` + version line + `_existing_hook_usable()` 已落地，只补回归 |

## 实现要点

1. `_ancestor_relation(ancestor, descendant, repo_dir)`：`git -C <repo_dir> merge-base --is-ancestor`，
   rc=0→True、rc=1→False、其它/异常/无 repo_dir→None（调用方按 fail-closed 处理）。
2. `check_push_allowed(..., local_sha="", repo_dir="")`：
   - `expected` 为空或 = `remote_sha` → `ok` 放行（快路径不变）；
   - 有 `local_sha`：`remote_sha` 是 `local_sha` 祖先 → `fast_forward` 放行 + `refresh_expected=remote_sha`；
   - 无 `local_sha`：`expected` 是 `remote_sha` 祖先 → `fast_forward` 放行 + `refresh_expected`；
   - 其余 → `remote_drift` 阻断，返回体补 `expected_updated_at` / `pending_expected_sha` / `pending_prev_sha`。
3. `reconcile_pending(..., repo_dir="")`：`pending` 为空且 `expected != remote_sha` 时，
   `expected` 是 `remote_sha` 祖先 → 写回 `expected_remote_sha=remote_sha` 并返回（先验自愈）；
   否则返回原值交由判定层阻断（fail-closed）。
4. `_initial_expected(repo_key, head_branch, remote_sha)`：仅当状态表无 expected 且无 pending 时
   返回实时远端 head，供 `acquire_branch_lease(..., expected_remote_sha=...)` 初始化；否则返回
   `None` 表示「不改写」。
5. `_hook_main()`：先算 `repo_dir=_hook_repo_dir()`，获取租约时传入初始 expected；判定时传
   `local_sha` 与 `repo_dir`；放行且 `refresh_expected` 非空则前移 expected；`remote_drift` 阻断
   日志追加定位字段。
6. `preflight_push(..., worktree_dir="", local_sha="")` / `guarded_push()`：把工作树目录与本地
   HEAD 带入判定（`guarded_push` 由 `preflight_push` 内解析 `rev-parse HEAD`，不额外增加
   「block 前的 git 调用」），服务端路径同样具备判据 B 能力；无工作树时退化为判据 A。

## 影响面

| 模块 | 改动 |
|---|---|
| `src/domains/runtime/engine/branch_guard.py` | `_ancestor_relation`、`_initial_expected`、`_hook_repo_dir`、`_resolve_local_head`、`_drift_log_fields`（新增）；`check_push_allowed`、`reconcile_pending`、`preflight_push`、`guarded_push`、`_hook_main`（修改） |
| `tests/runtime/test_issue_2314_branch_guard_fast_forward.py` | 新增回归：① fast-forward 放行 ② 真分叉阻断 ③ pending 未落地不误伤 ④ 版本失配/幂等重装 + 漂移日志字段 + 首次远端 head 初始化 |

## 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 放行过宽，把真分叉误判为 fast-forward | 高 | 只认祖先关系；`merge-base` 无法判定一律 fail-closed |
| 钩子新增 git 调用抬高 push 延迟 | 低 | 仅在 `expected != remote_sha` 时触发一次本地对象库查询 |
| 旧钩子残留导致修复不生效 | 中 | 沿用 #2028 版本指纹 + 幂等重装，本次补回归覆盖 |
| 并发 push 下 expected 前移竞态 | 中 | 前移仅在持租约的钩子进程内进行，沿用现有锁语义 |