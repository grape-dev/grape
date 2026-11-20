# 沙箱设计文档

> 迁移自 `feat/devbox-sandbox-integration` 分支 `docs/weekly-05/`（重写迁移时未随代码迁入，
> 2026-10-20 补迁）。原文件内容未做修改，仅按主题重组目录。
> 代码迁移对照见 [../sandbox-migration-record.md](../sandbox-migration-record.md)。

## 目录结构

### 总设计（本目录）

| 文档 | 主题 | 对应代码入口 |
|------|------|-------------|
| [sandbox-architecture.md](sandbox-architecture.md) | 沙箱体系架构总设计：SandboxManager / ExecutionMode / 三平台抽象 | `src/agent/sandbox/` |
| [cloud-sandbox-engine.md](cloud-sandbox-engine.md) | 云沙箱引擎与注册：cloud_worker 引擎注册、runtime 展开 | `src/runtime/cloud_worker.py` |
| [cloud-sandbox-provider-pluggable-design.md](cloud-sandbox-provider-pluggable-design.md) | **主设计**（1849 行）：Provider 插拔 v3，R1-R17 需求 / 数据模型 / API / scope 隔离 / P1-P11 实施计划 / 验证矩阵 | `src/api/routes/settings.py`（sandbox-* API）、`sandbox_provider_config` / `engine_template_binding` 表 |
| [verification-report.md](verification-report.md) | 三引擎 E2E 验证报告 | — |
| [completion-assessment-2026-10-20.md](completion-assessment-2026-10-20.md) | **完成度评估**（integration2 @ 5ddd9aaa7）：R1-R17 逐项核查 / P1-P11 对照 / 2 个 P0 迁移漏点复盘 / 遗留工作 | — |
| [sandbox-management-enhancements-design.md](sandbox-management-enhancements-design.md) | **增补**（integration2 分支）：模板扫描（adapter 可选能力）/手动检查/编辑/engines 显示/按钮 tooltip——超出原 v3 设计的增量 | `src/api/routes/settings.py`、`src/agent/sandbox/backends/api.py`、`sandbox.js` |

### platform/ — 平台适配层

| 文档 | 主题 | 对应代码入口 |
|------|------|-------------|
| [platform-adapter-design.md](platform/platform-adapter-design.md) | PlatformAdapter v3 契约：build_provider / normalize_db_config / ui_schema / pkgutil 自动发现 | `src/agent/sandbox/backends/api.py` |
| [platform-adapter-implementation-plan.md](platform/platform-adapter-implementation-plan.md) | v3 实施计划 | 同上 |
| [provider-sandbox-concurrency-throttle-design.md](platform/provider-sandbox-concurrency-throttle-design.md) | Provider 维度沙箱并发流控 | `src/agent/sandbox/backends/cloud_base.py` |
| [bola-simplify-design.md](platform/bola-simplify-design.md) | BOLA 简化 + source/scope 规范化 | `src/api/routes/runtime.py` |

### bwrap/ — 本地 bwrap 沙箱

| 文档 | 主题 | 对应代码入口 |
|------|------|-------------|
| [bwrap-sandbox-abstraction-v6.md](bwrap/bwrap-sandbox-abstraction-v6.md) | bwrap 沙箱抽象边界重构 v6 | `src/agent/sandbox/backends/bwrap.py` |
| [bwrap-sandbox-home-redesign.md](bwrap/bwrap-sandbox-home-redesign.md) | HOME 目录重设计 | 同上 |
| [bwrap-per-task-env-decouple-design.md](bwrap/bwrap-per-task-env-decouple-design.md) | per-task 环境变量解耦 | 同上 |
| [bwrap-bridge-perf-fix.md](bwrap/bwrap-bridge-perf-fix.md) | UDS Bridge 并发性能修复 | `src/agent/sandbox/wrappers/` |
| [engine-sandbox-toggle-design.md](bwrap/engine-sandbox-toggle-design.md) | 引擎维度沙箱开关 | `engine_sandbox_toggle` 表 |
| [codearts-bwrap-text-len-zero-fix.md](bwrap/codearts-bwrap-text-len-zero-fix.md) | codearts text_len=0 根因修复 | `codearts_wrapper.py` |

### security/ — 安全与治理

| 文档 | 主题 | 对应代码入口 |
|------|------|-------------|
| [skill-egress-design.md](security/skill-egress-design.md) | Skill 出站域名声明与策略引擎 | `src/agent/policy/` |
| [audit-bugfix-design.md](security/audit-bugfix-design.md) | 全分支深度审查修复方案 | `src/agent/common/audit.py` |
| [worker-execution-and-fixes.md](security/worker-execution-and-fixes.md) | Worker 执行与修复记录 | `src/runtime/cloud_worker.py` |

### frontend/ — 前端

| 文档 | 主题 | 对应代码入口 |
|------|------|-------------|
| [sandbox-page-optimization-design.md](frontend/sandbox-page-optimization-design.md) | 云沙箱页面（双 Tab + Provider 详情） | `src/api/static/js/pages/sandbox.js` |

## 未迁移项

- **非沙箱主题**（事件路由/管道）：event-direct-selfreport-finalize、event-redispatch-terminal-reset、
  fix-agent-env-account-reponame、plugin-broadcast-local-direct-fallback、remove-event-json-unify-events-table、
  issue15-关联仓库无PR定位分析、查询分支失败502 —— main 分支 `docs/weekly-05/` 已有同名或同主题文档
- **archive/ 历史归档**（74 份）：devbox-sandbox 过程稿、issue-156 系列、fixes、skill-egress 分阶段实施、
  verification 过程稿——结论已归一到上述现行设计；需要时从旧分支 `docs/weekly-05/archive/` 查阅
