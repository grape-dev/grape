# Issue #2028 方案设计

## 根因链

```
repogit.ensure_issue_worktree
  → install_pre_push_hook
    → 钩子模板（module_path=src/runtime/branch_guard.py，已随 #699 迁移失效）
      → fail 加载被 except 静默 exit 0（守卫静默 no-op）
      → 即便加载成功，hook_main 内 from src import lock
        → worker 包只发 src/kernel/，缺顶层 src/lock.py → ImportError
          → 异常逃逸出钩子模板（hook_main 在 try 之外）→ 非零退出 → push 全拒
```

## 设计决策

| 决策 | 选项 | 选择 | 理由 |
|---|---|---|---|
| hook_main 异常 | 裸调（现状） / 纳入 fail-open | 纳入 fail-open | #993 既定语义：守卫自身缺陷不得阻断交付 |
| lock 下发层级 | `src/kernel/` / 顶层 `src/lock.py` | 顶层 | `from src import lock` 导入路径决定 |
| 存量钩子升级 | 仅新环境 / 版本指纹重装 | 版本指纹 | 现网存量 worktree 已带缺陷钩子 |
| 模板模块定位 | 拼 `src/runtime/` / 以 `__file__` 为准 | 以 `__file__` 为准 | 布局迁移后 `src/runtime/` 不存在 |
| 租约 holder | 仅进程 holder / `task_id:进程holder` | 两者兼容 | 跨 worker 互斥需 holder 参数，旧 lock 无该参数 |

## 实现要点

1. `HOOK_TEMPLATE_VERSION` + `HOOK_VERSION_LINE`：模板头写入版本；安装时若存量钩子含本守卫
   marker 且版本行一致则幂等返回，否则重写升级；非守卫钩子仍保留为 `pre-push.grape-local`。
2. `_hook_module_path() = abspath(__file__)`；`_hook_root()` 上溯 5 层得到仓库/安装根。
3. `_hook_entry_code()`：加载模块与 `hook_main()` 同一 try/except，异常时 stderr 输出
   `[branch-guard] WARNING ... fail-open ... [module=<path>]` 并 exit 0。
4. `hook_main` 外层再包一层 try/except 兜底；`_hook_main` 入口 `_missing_hook_deps()` 自检
   `src.lock` / `src.kernel.session_manager`，缺失即 fail-open 且告警含缺失模块名。
5. `src/kernel/lock.py` 增加 `holder` 可选参数（缺省 `_holder`），branch_guard 传入
   `task_id:进程holder`；同一任务跨实例互斥、同任务同进程可重入。

## 风险

| 风险 | 缓解 |
|---|---|
| fail-open 误放真实违规 | 真实拦截路径显式 `return 1`，用例覆盖 merged 阻断 |
| holder 参数改动影响其它调用方 | 全部为可选参数，缺省行为不变；相关调用方测试回归 |
| 存量钩子指向已删路径 | module_path 以 `__file__` 为准 + 版本号升级触发重装 |