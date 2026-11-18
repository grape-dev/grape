# Issue #2314 任务拆解

> 目标分支 `arch-weekly-11-zj`；canonical 落点 `src/domains/runtime/engine/branch_guard.py`

## T1 判定层：fast-forward 兜底（缺口 ①）

- [ ] `_ancestor_relation(ancestor, descendant, repo_dir)`：`merge-base --is-ancestor`，
      rc 0/1→True/False，其余→None（fail-closed）
- [ ] `check_push_allowed` 新增 `local_sha` / `repo_dir`；祖先关系成立 → `reason=fast_forward`
      + `refresh_expected=remote_sha`；无法判定 → `remote_drift`

## T2 状态层：陈旧 expected 先验自愈（缺口 ②）

- [ ] `reconcile_pending` 新增 `repo_dir`；`pending` 为空且 expected≠remote 时按祖先关系推进

## T3 钩子层：首次以实时远端 head 初始化（缺口 ③）

- [ ] `_initial_expected()` + `_hook_main()` 传 `expected_remote_sha`
- [ ] `_hook_main()` 判定传 `local_sha` / `repo_dir`，放行后前移 expected

## T4 可观测：漂移日志定位字段（缺口 ④）

- [ ] `remote_drift` 返回体含 `expected_updated_at` / `pending_expected_sha` / `pending_prev_sha`
- [ ] 阻断日志（钩子 + `guarded_push`）追加同一组字段

## T5 服务端路径对齐

- [ ] `preflight_push(worktree_dir, local_sha)`、`_resolve_local_head()`、`guarded_push` 传参

## T6 回归用例（验收）

- [ ] ① fast-forward 放行（remote 为 local 祖先）
- [ ] ② 真分叉阻断（无祖先关系）
- [ ] ③ pending 未落地不误伤 + 陈旧 expected 自愈
- [ ] ④ 钩子版本失配检出 / 幂等重装（保持 #2028 实现，不重复开发）
- [ ] ⑤ 首次 push 以实时远端 head 为 expected；漂移日志含三字段；merge-base 异常 fail-closed

## T7 质量门

- [ ] 现有 branch guard 三份测试不回归
- [ ] 自审查（5 轴）+ 开源前置增量检查（无关内容 / 敏感信息 / 英文 commit）