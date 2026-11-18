# Spec: Issue #2706 — pre-push 守卫租约层 fail-open（缺 lock 模块不再阻断 push）

> 来源：user feedback #7415 | 定级：P0（第 8 次同类复发）| 目标分支：arch-weekly-11-zj
> 上游 Spec 见 Issue #2706 评论（issue agent 2026-11-16），本文件为 coding 侧落地方案。

## 现状（arch-weekly-11-zj 布局核验）

- `src/domains/runtime/engine/branch_guard.py` 三处租约函数（`_lease_holder` /
  `acquire_branch_lease` / `release_branch_lease`）已由 #2292 迁移到 canonical
  惰性导入 `from src.kernel import lock as _lock`，但**无任何异常兜底**：部署树缺
  `src/kernel/lock.py`（或 lock 依赖链初始化异常）时 ImportError 直接上抛。
- 钩子路径已有两层 fail-open（`_missing_hook_deps` 自检 + `hook_main` 全量兜底），
  但服务端/直调路径（`branch_lease` 上下文、`guarded_push` 调用方）直接调
  `acquire_branch_lease`，异常会以未捕获形态炸到调用方，push 交付通道被守卫自身
  的包缺陷阻断——违反模块文档第 12 行「守卫自身查询异常 → fail-open + 告警」契约。
- main 布局 `src/runtime/branch_guard.py`（235/252/275 `from src import lock`）同病，
  本分支不携带该文件，同型补丁以文档交付（见 PR body「main 布局同型修复指引」）。

## 目标（AC）

- [x] AC1 租约层导入收敛：三处直接导入统一走 `_lock_module()` 收敛点；
- [x] AC2 fail-open：lock 导入/初始化异常 → 可定位 WARNING（模块名 + 异常原因 +
      修复指引）+ `acquire_branch_lease` 返回 `{"ok": True, "degraded": True}` 按无租约
      放行；`release_branch_lease`/`_lease_holder` 不上抛（release 静默降级、holder=None）；
- [x] AC3 正常环境不回归：lock 可用时 acquire/renew/release 语义与 #993/#2292 完全一致；
- [x] AC4 真实违规仍 fail-closed：租约冲突（`branch_locked`）与 PR merged 阻断不受影响；
- [x] AC5 发布包随包下发 lock 模块：`src/kernel/worker_package.MANIFEST` 已含
      `src/kernel/` + `src/__init__.py`（`src.lock → src.kernel.lock` 一跳别名，#2292
      已交付并回归），本单核验并把旧布局存量部署升级路径写入文档；
- [x] AC6 CI 守卫 import 冒烟：新增回归用例接入 `.github/workflows/windows-smoke.yml`
      linux-backend 门禁（含 `python -c "import ...branch_guard"` 冒烟）；
- [x] AC7 runbook：`--no-verify` 写法保留但标注为「守卫故障期临时规避」，
      见 docs/issue-2706/runbook.md。

## 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 兜底策略 | fail-closed / fail-open+告警 | fail-open+告警 | 与守卫自身文档契约一致；push 通道不可被守卫自身缺陷阻断（#7415 实机复现） |
| 导入收敛 | 三处各自 try/except / `_lock_module()` 收敛点 | 收敛点 | 单点维护，避免兜底逻辑三处漂移（历史 8 次复发的根因之一） |
| 降级返回 | `ok: False` / `ok: True + degraded` | `ok: True + degraded: True` | 调用方（hook `_hook_main`、`branch_lease`）以 `ok` 判定放行；degraded 供观测区分「真持锁」与「降级无租约」 |
| 告警通道 | 仅 log / 仅 stderr / 双通道 | log.warning（带 try/except） | 钩子路径 stderr 已由 `_missing_hook_deps`/`hook_main` 两层覆盖；租约层直调方走任务日志可观测 |

## Out of Scope

- 租约 acquire/renew/release 语义变更；`distributed_locks` 存储层重构；
- 历史同类单批量合并（#1402/#1552/#1917/#2028/#2186/#2292/#2348）。
