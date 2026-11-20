# Issue #2292 — 方案设计（plan）

## 修复分层

```
pre-push 钩子进程
  └─ _hook_entry_code（模板，2292-01）
        ├─ 加载 branch_guard（按 __file__ 推导 canonical 路径）
        ├─ hook_main() → try/except → fail-open + 告警 + 修复指引
        └─ _missing_hook_deps() → import src.kernel.lock / src.kernel.session_manager
              └─ acquire_branch_lease / release → from src.kernel import lock
```

三层同时收口，任一层失效都不会阻断交付：

1. **导入层**：租约三函数 `_lease_holder` / `acquire_branch_lease` /
   `release_branch_lease` 改用 `from src.kernel import lock as _lock`，
   与 #2010 布局一致，不再依赖 `src/__init__.py` 的别名 finder。
2. **自检层**：`HOOK_DEP_MODULES` 指向 canonical 路径。此前自检校验 `src.lock`
   （靠别名通过），真缺模块时自检静默放行、到租约处才炸——错误位置与错误信息都不对。
3. **入口层**：`hook_main` 已有 fail-open 兜底（#2028），本次把**可执行修复动作**
   写进同一条 `[branch-guard] WARNING` 通道（stderr 用户可见 + 任务日志结构化），
   并递增模板版本让现网存量 worktree 钩子自愈重写。

## 关键决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 导入路径 | A 打包补 `src/lock.py`；B 改 canonical | **B** | 与 #2147/#2028 一致，顶层别名是过渡物，补文件只会让坏包再复发 |
| 模板版本 | A 不动；B 递增到 `2292-01` | **B** | 模板文案变了（修复指引），不递增则存量钩子永不升级（#2028 教训） |
| 缺模块策略 | A fail-closed 提示重试；B fail-open | **B** | 守卫是**辅助**校验，不能让平台自身缺陷阻断交付；真实违规仍 `return 1` |
| 冒烟自检范围 | A 只 canonical；B 含旧别名 | **B** | 存量导入方（别名）也要保；同时覆盖新布局守卫，坏包在升级前被拦 |
| otel 诊断路径 | A `src/observability/otel`（不存在）；B 域内薄壳 `src.domains.insight.observability.otel`；C canonical `src.kernel.otel` | **C** | 原写法必误报「无关模块缺失」；canonical 内核路径真实存在，安装期也不额外耦合 insight 域 |

## 变更清单

- `src/domains/runtime/engine/branch_guard.py`
  - 3 处 `from src import lock` → `from src.kernel import lock`
  - `HOOK_DEP_MODULES` → canonical 路径
  - `HOOK_TEMPLATE_VERSION` `2028-04` → `2292-01`（含版本历史注释）
  - `HOOK_REPAIR_HINT` 常量；`_fail_open` 与钩子模板告警都输出它
- `scripts/install-worker.sh`
  - `_smoke_check` 主检查新增 `import src.kernel.lock; from src import lock;
    import src.domains.runtime.engine.branch_guard`
  - 诊断分支 import 列表与主检查一致，otel 校验 canonical `src.kernel.otel`
  - 拒绝升级时追加 `Fix:` 行，指明缺哪个模块、下一步做什么
- `src/kernel/worker_package.py`：MANIFEST 注释说明 lock 由 `src/kernel/` 覆盖，
  不要新增不存在的 `src/lock.py` 条目
- `.github/workflows/windows-smoke.yml`：branch guard 步骤接入 #2292 用例
- `tests/test_issue_2292_branch_guard_canonical_lock.py`（18 项）
- `tests/test_issue_2028_branch_guard_fail_open.py`：依赖缺失例改为 canonical 名

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 模板版本未递增 → 存量钩子不升级 | 版本号改 `2292-01`，用例断言 `> 2028-04` |
| fail-open 掩盖真实违规 | 真实违规仍 `return 1`（租约冲突 / PR 已合并两条用例固化） |
| 双基线重复修复漂移 | 与 #2147 逐项对齐，仅增量补修复指引与冒烟别名校验；合入时二选一后 rebase 取并集 |
| 冒烟自检本身误拒好包 | 正控用例（模块齐全 → `smoke_rc=0`）防夹具假阳性 |