# Issue #2292 — pre-push 分支守卫缺 `src/kernel/lock.py` 致 push 全被阻断（Spec）

> 前置 Spec 见 Issue #2292 评论 id=200410003（Issue Agent 产出，评级 ready），本文件为编码阶段落地的实现规格，不改变其结论。

## 背景

用户反馈 5479 / 5492 / 5493 / 5508 同一现象：`grape git push` 首次即被 pre-push 钩子拒绝，
栈顶为 `branch_guard.py` 租约路径的 `from src import lock` →
`ImportError: cannot import name 'lock' from 'src'`。部署树（`/opt/grape/src/`）里
canonical 实现只有 `src/kernel/lock.py`，#2010 布局收敛后顶层 `src/lock.py` 文件已不存在
（仅剩 `src/__init__.py` 的一跳别名）。因此：

- 顶层导入在**缺模块/别名未注册**的发行包上必炸；
- 钩子异常逃逸 → 非零退出 → 所有 push 被阻断；
- 唯一出路是 `--no-verify`，分支租约与漂移校验全部失效（真正的静默风险）。

## 目标

1. 租约路径走 canonical `src.kernel.lock`，不依赖顶层别名；
2. 守卫自身异常一律 fail-open，且告警可直接照做（重装/升级 worker 包）；
3. worker 升级冒烟自检能拒绝缺 `lock` / `branch_guard` 的坏包；
4. 与 #2147（PR #1275）同根因同修法，不重复实现。

## 范围

| 类型 | 内容 |
|------|------|
| In | `src/domains/runtime/engine/branch_guard.py`：三处租约导入、`HOOK_DEP_MODULES`、hook 模板告警文案 + 模板版本递增、`_fail_open` 修复指引 |
| In | `scripts/install-worker.sh`：`_smoke_check` 增加 `src.kernel.lock` / `from src import lock`（旧别名）/ 新布局 `branch_guard` 校验 + 诊断 otel 路径纠正 + 修复动作告警 |
| In | `src/kernel/worker_package.py`：MANIFEST 注释如实化（lock 由 `src/kernel/` 覆盖，不要再加不存在的 `src/lock.py`） |
| In | CI（`.github/workflows/windows-smoke.yml`）接入 #2292 回归用例 |
| In | `tests/test_issue_2292_branch_guard_canonical_lock.py`（18 项）+ `tests/test_issue_2028_branch_guard_fail_open.py` 依赖名对齐 |
| Out | #2291 的 `distributed_locks` 缺表（另一根因） |
| Out | #2147 的 PR #1275 合入流程本身（该单推进） |

旧树 `src/runtime/branch_guard.py` 在本基线（`arch-weekly-11-zj`）已随 #699/#2010 移除，
无需同步。

## 验收标准

- [x] 源码中 `from src import lock` 绝迹，租约三函数实测可用（`src.kernel.lock`）
- [x] `HOOK_DEP_MODULES = ("src.kernel.lock", "src.kernel.session_manager")`，自检在完整树上为空
- [x] 模拟缺 lock 的部署树：钩子 exit 0 + `[branch-guard] WARNING` + fail-open + 修复指引
- [x] 真实违规（租约冲突 / PR 已合并）仍 `return 1`
- [x] 钩子模板版本 `2028-04 → 2292-01`（文案变更必须递增，#2028 教训）
- [x] `install-worker.sh` 冒烟：缺 lock / 缺 branch_guard 的包被拒绝升级并指认模块，完整包放行
- [x] 现有相关测试无回归（`tests/test_issue_2028_*`、`tests/arch`、`tests/runtime/test_worker_package.py`）
- [x] 与 #2147 修复点一一对应（导入路径 / 自检依赖 / 冒烟自检 / 发布闭包注释）

## 与 #2147 的对齐关系

同根因、同修法。可复用差异点（本单额外补齐）：

| 项 | #2147（PR #1275） | #2292（本单） |
|----|-------------------|---------------|
| 三处导入 | `from src.kernel import lock` | 同 |
| `HOOK_DEP_MODULES` | `src.kernel.lock` | 同 |
| hook 模板 | 未改（沿用 2028-04） | 增补修复指引 + 版本 `2292-01` |
| `_fail_open` | 未改 | 增补可执行修复指引（双通道） |
| 冒烟自检 | lock + branch_guard | 额外覆盖旧别名 `from src import lock`、纠正 otel 诊断路径 |

合入顺序：任一 PR 先合，另一侧 rebase 后取并集（版本号取更高者 `2292-01`），不双跑两套实现。