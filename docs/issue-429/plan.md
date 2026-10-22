# Plan: Issue #429 开发方案

> Spec 见 `spec.md`。实现按 TDD：先写静态回归测试（RED）→ 改脚本（GREEN）→ 全量回归。

## 任务拆解（TDD）

### T1 · RED：新增 tests/test_worker_install_ps1_stderr.py
- 6 个静态断言用例（见 spec §6），对当前脚本全部失败（`2>$null` 仍在、无 `Invoke-PipQuiet`、无环境变量）。
- 断言「强不软化」：grape-cli 段必须保留 `$LASTEXITCODE` 判断。

### T2 · GREEN：修改 scripts/install-worker.ps1（单原子提交）
1. 顶部（`$ErrorActionPreference = "Stop"` 后）加 `$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"` + 原因注释。
2. 新增 `Invoke-PipQuiet` 函数（EAP Continue → `& $cmd @args 2>&1 | Out-Null` → 恢复 EAP → 返回 `$LASTEXITCODE`）。
3. 8 个 pip 调用点改走 `Invoke-PipQuiet`（自愈段 5 处 + grape-cli 段 3 处），grape-cli 段保留 `$LASTEXITCODE` 显式判断结构。

### T3 · 回归验证
- `pytest tests/test_worker_install_ps1_stderr.py -v` 全绿。
- `pytest tests/test_runtime_install.py tests/test_windows_worker.py -v` 全绿（既有静态断言 + install.ps1 端点测试）。
- 平衡括号检查（Python 侧 `str.count` 辅助，等效 PowerShell 语法粗检）。

### T4 · 自审查（code-review 五轴）
- 正确性 / 安全 / 性能 / 可维护性 / 测试充分性，重点：EAP 恢复是否所有路径都执行（try/catch/finally 语义）。

### T5 · 提交 + PR + A2A QA
- commit：`fix: #429 install-worker.ps1 pip stderr 终止性错误——PIP_DISABLE_PIP_VERSION_CHECK + EAP 防御层`
- PR base=`功能分支`，head=`feat/issue-429`；Issue 评论告知；A2A → qa。

## 风险与对策

| 风险 | 对策 |
|------|------|
| `Invoke-PipQuiet` 内异常导致 EAP 未恢复 | 恢复语句在 try/catch 之后顺序执行（catch 吞掉非 cmdlet 异常），与 tar 段同构 |
| PS 5.1 下 `@Arguments` splatting 传参错误 | 参数数组字面量显式列出；静态测试断言调用形态 |
| 改动引入新终止性错误 | 只替换调用形态，不动控制流；全量回归 |
