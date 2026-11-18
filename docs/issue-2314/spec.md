# Issue #2314 Spec：pre-push 分支守卫用过期租约误判 remote_drift

> 目标分支：`arch-weekly-11-zj` | 优先级：P1 | 类型：bug
> 需求来源：用户反馈 5585（任务 `t_ecad294099b4` / `seq=70358`，仓库 `grape-dev/grape-skills`，分支 `feat/issue-2923`）
> 落点修正：Spec 原文写 `src/runtime/branch_guard.py`，#2028 后 canonical 路径为 `src/domains/runtime/engine/branch_guard.py`

## 目标

pre-push 分支守卫在「普通 fast-forward 推送」场景不再因陈旧租约期望误报 `remote_drift`
阻断推送；真分叉（remote 与本地无祖先关系）仍正常阻断。

## 现状（按目标分支源码逐处核对）

| 编号 | 位置 | 现状 | 期望 |
|---|---|---|---|
| ① | `check_push_allowed()` | `expected != remote_sha` 等值比较即 `remote_drift`，无祖先关系兜底，也未接收 `local_sha` | 远端 head 是待推提交的祖先即视为 fast-forward 放行并前移 expected |
| ② | `reconcile_pending()` | `pending` 为空时直接返回存量 `expected`，陈旧 sha 无推进路径 | 补先验收敛：expected 是 remote 祖先 → 推进到 remote 并清空 pending |
| ③ | `_hook_main()` | `acquire_branch_lease(repo_key, pushed)` 未传 expected；分支首次无状态不以实时远端 head 初始化 | 传 expected；首次无状态以推送时实时远端 head 初始化，禁止沿用历史 sha |
| ④ | `remote_drift` 返回体/阻断日志 | 缺 `expected_updated_at` / `pending_expected_sha` / `pending_prev_sha` | 补齐，使「旧租约」与「真漂移」在日志上可分 |
| ⑤ | 钩子版本治理 | `HOOK_TEMPLATE_VERSION = "2028-04"` + version line + `_existing_hook_usable()` 已由 #2028 落地 | **保持不重复实现**，补版本失配/幂等重装回归覆盖 |

## 验收标准

- [ ] fast-forward 放行：远端 head 是本地待推提交的祖先 → 放行（`reason=fast_forward`），不再 `remote_drift`
- [ ] 真分叉阻断：remote 与 local 无祖先关系 → `reason=remote_drift` 阻断，行为不回归
- [ ] pending 未落地（push 失败）不误伤下一次合法 push
- [ ] 陈旧 expected（previous push 已落地、远端前进）在 `reconcile_pending()` 内先验自愈推进
- [ ] 分支无租约状态时首次 push 以实时远端 head 为 expected，不以历史 sha 作期望
- [ ] `remote_drift` 返回体与阻断日志均含 `expected_updated_at` / `pending_expected_sha` / `pending_prev_sha`
- [ ] 钩子版本失配可检出并幂等重装（回归覆盖，不重复实现 #2028）
- [ ] `merge-base` 无法判定（对象不可达 / git 异常）一律 fail-closed 按漂移阻断
- [ ] `tests/runtime/test_issue_993_branch_guard.py`、`tests/test_issue_2028_branch_guard_fail_open.py`、
      `tests/runtime/test_issue_993_guard_state_prune.py` 现有断言不回归

## 非目标

- 不改租约 TTL、锁语义与并发互斥策略
- 不改「PR 已合并 / 已关闭」阻断分支
- 不做历史 `branch_guard_state` 脏数据回填（由下一轮 push 自然收敛）
- 不含 worker 包发布与批量升级执行本身