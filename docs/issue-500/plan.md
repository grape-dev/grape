# Plan: Issue #500 — bwrap 告警噪音治理方案

## 方案概述
1. **配置短路（消噪音源头）**：三个调用点在进入 sandbox 导入/包装路径前，先读 `src.config.GRAPE_AGENT_SANDBOX`（与 `sandbox_enabled()` 同语义：直接读 config，规避 sandbox 模块自身导入损坏时短路失效）。关闭态直接走原执行路径，零导入、零日志。
2. **根因修复（导出保证）**：`git mv src/agent/sandbox.py src/agent/sandbox/__init__.py` 转为包布局。部署物按文件覆盖发布时，仓库的 `sandbox/__init__.py` 会覆写部署环境陈旧的同名文件，使 `from src.agent.sandbox import build_bwrap_cmd` 在包布局下必然可导入（补齐 `__all__` 显式导出）。全部 4 处引用均为按名导入，布局迁移零影响（已 grep 核对）。
3. **降级告警一次性（防刷屏）**：调用方模块级 `_bwrap_fail_notified` 标志 + `_note_bwrap_fail_once()`：首次 WARNING，后续 DEBUG。放在调用方（而非 sandbox 模块）是因为该分支本身要覆盖「sandbox 导入失败」场景，此时无法依赖 sandbox 内的助手。

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 关闭态判断位置 | 调用点直接读 `src.config.GRAPE_AGENT_SANDBOX` | sandbox 模块损坏时 `sandbox_enabled()` 不可用；src.config 始终可导入 |
| 根因修复方式 | 模块→包（`sandbox/__init__.py`） | 与部署布局对齐，覆盖陈旧 `__init__.py`，导出一劳永逸 |
| log-once 位置 | 各调用方模块级标志 | 需覆盖 sandbox 导入失败场景，不能依赖 sandbox 自身 |
| 三个调用点同修 | hermes/hwcloud/executor | 同一根因、同一告警文案（`bwrap wrap failed, running unsandboxed`），验收要求「0 条该告警」 |

## 测试 seam（公开接口）
- `HermesACPBackend.stream_execute` / `HwcloudACPBackend.stream_execute`（fake ACP CLI 端到端，捕获 Popen argv + caplog）；
- `stream_runtime(...)`（executor 路径，沿用 test_agent_executor.py 的 FakePopen 先例）；
- `src.agent.sandbox` 包导出（`build_bwrap_cmd` / `sandbox_enabled` / 包布局）。

三态：关闭（导入损坏仍 0 告警）/ 开启成功（包装前缀 + info）/ 开启失败（WARNING 恰 1 条 + DEBUG 降级 + unsandboxed 执行）。
