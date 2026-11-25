# Issue #2923 任务拆解

- [x] T1 `branch_guard._guard_stage_failed` 明细降 debug，收集器仅记 stage
- [x] T2 `_emit_fail_open_alert` 改为单条可读中文提示，不回流原始异常文案
- [x] T3 `install-worker.sh` 冒烟自检覆盖 vcs / branch_guard / lock / `src.kernel.otel`
- [x] T4 `install-worker.sh` 增加包 VERSION 与运行时指纹一致性校验
- [x] T5 打包闭包守卫断言 MANIFEST 含 vcs / lock 入口
- [x] T6 新增 `tests/runtime/test_issue_2923_branch_guard_degrade_noise.py`
- [x] T7 更新 `tests/test_issue_2890_branch_guard_aggregate_warning.py` 输出契约
- [x] T8 回归 branch guard 相关测试（#993 / #2028 / #2890 / #2923 / S112）
## QA 复审修复（PR #1560）

- [x] F1 `_manifest_version` 改用相对归档路径 + 内容哈希，修复安装期版本校验恒误报
- [x] F2 新增真实执行测试：路径无关指纹、`_version_consistency_check` 相等/不等/跳过分支
- [x] F3 移除 2 处新增静默 `except: pass`（`_safe_debug` 显式返回），ratchet 回到基线
- [x] F4 `_missing_hook_deps` / `_fail_open` / `_hook_entry_code` 收敛原始 ImportError 泄漏
- [x] F5 服务端 preflight 恢复 WARNING 语义（仅钩子内降 debug）
- [x] F6 `_smoke_check` 调用点后移至建 venv + 依赖安装之后，覆盖全新安装
- [x] F7 Spec 同步 canonical `src/domains/runtime/vcs/` 与上述验收口径

## QA 第二轮复审修复（PR #1560, head cb712144e）

- [x] F8 自检埋点改 import 零第三方依赖的 canonical `src.kernel.otel`，移除会触发 `prometheus_client` 的 `observability.otel` 转发壳
- [x] F9 新增真实执行自检测试：干净 venv（无第三方依赖）下自检通过、缺模块时自检失败
- [x] F10 同步 `_smoke_check` venv 守卫注释与后移后的调用点
