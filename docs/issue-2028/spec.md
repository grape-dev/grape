# Issue #2028 Spec：pre-push 分支守卫运行时缺 lock 模块，push 确定性被阻

> 目标分支：`arch-weekly-11` | 优先级：P0 | 类型：bug

## 目标

修复 `grape-branch-guard` 在 worker 运行时的 `ImportError: cannot import name lock from src`：

1. `src/lock.py` 未随 worker 包下发 → `acquire_branch_lease` 内 `from src import lock` 失败；
2. `hook_main` 内部异常未被钩子模板 fail-open 捕获 → 守卫自身缺陷升级为 fail-closed，所有 push 被拒。

恢复既定语义：**守卫自身异常 fail-open（放行 + stderr 告警），真实违规 fail-closed（返回 1）。**

## 范围

### In Scope

1. `src/kernel/worker_package.py` MANIFEST 补齐顶层 `src/lock.py`。
2. `scripts/release_pack.py` `pack_tools()` manifest_files 同步补齐 `src/lock.py`。
3. `src/domains/runtime/engine/branch_guard.py`：
   - 钩子模板将模块加载与 `hook_main()` 一并纳入 try/except（fail-open + 可定位告警）；
   - 新增 `HOOK_TEMPLATE_VERSION` 指纹，`install_pre_push_hook` 对旧版/缺失指纹的存量钩子重装；
   - 修正钩子 `module_path`：按 #699 迁移后的 canonical 布局
     `src/domains/runtime/engine/branch_guard.py` 定位（原 `src/runtime/` 已不存在）。
4. `src/kernel/lock.py`：`acquire_lock/renew_lock/release_lock` 支持可选 `holder` 参数。
   branch_guard 以 `task_id:进程holder` 作为租约持有者，旧实现无此参数会 `TypeError`，
   租约无法获取（`src.lock` 仅是该模块的兼容别名，二者共享同一实现）。
5. 回归测试：发布闭包含 `lock.py`、fail-open（异常/缺模块）、真实违规仍 fail-closed、
   存量钩子版本升级、幂等安装。

### Out of Scope

- #1923（remote_drift 陈旧租约）、#2024（工作区 git 目录只读）。
- 旧分支提交 `9dfdae101` / `4c04e50d62` 直接 cherry-pick（按当前布局重写）。

## 验收标准

- [ ] 新 worker 包内存在 `src/lock.py`，`from src import lock` 可导入
- [ ] 无 lock 环境下 `hook_main` ImportError 不阻断 push（exit 0 + stderr 明确告警）
- [ ] 真实拦截（租约争用 / PR merged / 远端漂移）仍返回非零
- [ ] `HOOK_TEMPLATE_VERSION` 不匹配的存量钩子被重装；钩子 module_path 实测可加载
- [ ] coding 实跑 `grape git push`（不带 `--no-verify`）成功且租约正常获取
- [ ] 现有相关测试通过 + 新增测试覆盖

关联：#1917（同根因）| 范围外：#1923、#2024