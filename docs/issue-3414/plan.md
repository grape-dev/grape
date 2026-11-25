# Plan: Issue #3414

## 改动清单
1. `src/domains/mgmt/routes/settings.py` — `_attach_engine_models`：public 门面
   from-import 失败时降级（失败仅告警、不 500，附加 DB recent_models 照常）。
2. `src/domains/runtime/engine/branch_guard.py` — **本基线已由 #2706 收敛点 canonical**
   （`LEASE_LOCK_MODULE="src.kernel.lock"`、`HOOK_DEP_MODULES=("src.kernel.lock",
   "src.kernel.session_manager")`、`_lock_module()` 惰性导入）；本 PR 无功能变更，
   仅补 3 处 traceability 注释。
3. `tests/test_import_symbol_integrity.py`（新增）— 全仓 `from src.* import sym` / `import src.*` 静态完整性冒烟（模块存在 + 符号已定义；仅 `try.body` 守卫导入跳过）。
4. `tests/runtime/test_worker_package.py` — import 闭包子进程补 `engine_model` 模块与符号。
5. `tests/test_issue_3414_import_degrade_and_hook_deps.py`（新增）— public 导入失败降级 + branch_guard 钩子依赖 canonical 路径/租约三入口回归测试。

## 任务拆解
- [x] 根因确认（grape-kit #6 traceback + 本仓调用链核对）
- [x] T1 服务端降级修复 + 单测
- [x] T2 branch_guard canonical 基线复核 + traceability 注释 + 回归单测
- [x] T3 导入完整性冒烟测试（全仓 + worker 包闭包）
- [x] 全量相关测试回归
- [x] 自审查 → PR → A2A QA

## 风险
- branch_guard 本基线已 canonical，无行为变更；剩余改动仅为注释与测试。
- 降级后 agent-runtimes 端点 model_name 可能为空（残余风险，Issue 已记录）——与 worker 未上报语义一致，派单链路不再 500；根因（陈旧部署包）待后续升级/补齐。
