# Issue #2923 方案设计：部署闭包补齐 + 降级去噪

## 方案概述

以「部署闭包完整性 + 部署自检 + 降级输出分层」三条线修复同一根因。

1. **部署闭包**：`MANIFEST` 现有 `src/domains/runtime/vcs/`、`src/kernel/`、`src/__init__.py`
   已覆盖 vcs 与 lock；新增回归断言锁定该闭合，避免再次漂移。
2. **部署自检**：`install-worker.sh` 的 `_smoke_check` 改为逐模块 import
   （config / lock / session_manager / agent / branch_guard / vcs.api_client /
   `src.kernel.otel`），自检模块须零第三方依赖；
   任一缺失即拒绝安装/升级并保留旧代码；调用点后移到建 venv + 安装依赖之后，
   使全新安装同样生效。新增 `_version_consistency_check`，比较包内 `VERSION` 与
   按已解压 `src/` 重算的 `_compute_worker_code_version()`，不一致告警；指纹改为
   **相对归档路径 + 内容**，构建期与安装期同口径，避免恒不相等误报。
3. **降级去噪**：钩子作用域内 `_guard_stage_failed` 明细写 debug、收集器只记
   `stage`；`_emit_fail_open_alert` 只输出一条中文提示。`pr_query` 失败时提示
   「分支基线核验暂不可用，推送已放行（fail-open）」，其它阶段为「分支守卫降级运行，
   推送已放行（fail-open）」。无收集器的服务端 preflight 维持 WARNING，异常可见；
   `_fail_open` / `_hook_entry_code` 兜底也不再输出原始异常文案。退出码与
   fail-open 放行语义不变。

## 关键决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 用户侧输出 | 结构化 JSON（含 attempts 原文）/ 单条中文提示 | 单条中文提示 | 原始 ImportError 易被误判为推送失败 |
| 明细去向 | stderr / debug 日志 | debug 日志 | 保留排障能力，不污染任务日志 |
| 缺模块发现时机 | 运行期 push / 部署自检 | 部署自检 | 升级阶段拦截坏包，避免运行期降级 |
| 版本一致性 | 硬失败 / 告警 | 告警 | 避免非代码文件差异导致升级被拒，仍可提前发现漂移 |

## 影响范围

- `src/domains/runtime/engine/branch_guard.py`：降级输出分层。
- `scripts/install-worker.sh`：import 冒烟自检 + 版本一致性校验。
- `tests/test_issue_2890_branch_guard_aggregate_warning.py`：输出契约随 #2923 更新。
- `tests/runtime/test_issue_2923_branch_guard_degrade_noise.py`：新增回归。

## 风险

- 自检新增模块若在最小安装环境不可 import → 冒烟失败导致升级被拒；所选模块均属
  worker MANIFEST 闭包内，且埋点只走零第三方依赖的 `src.kernel.otel`（不使用会
  触发 `prometheus_client` 的 `src.domains.insight.observability.otel` 转发壳）。
  冒烟自检用干净 venv 真实执行回归，防止再次引入服务端专属依赖。
- 版本指纹比较可能因运行期文件变化产生误报 → 仅告警不阻断。