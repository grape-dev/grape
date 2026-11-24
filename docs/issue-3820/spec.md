# Issue #3820 Spec — opencode 404 `Model not found` 归类 config_error + 启动校验 + 探活最小调用

> 需求来源：Issue #3820 及其评论（VOD 结构化收敛 + Issue Agent Spec，评论 id 202860048）。
> 编码基线：`arch-weekly-11-ljt`（worktree 分支 `feat/issue-3820`）。

## 目标

opencode 引擎在「配置模型名不在网关 key 可用列表」时报 `Not Found: Model not found`（HTTP 404）。该错误当前落入
`upstream_transient` 重试链路，4 个已确认任务各重试 3 次后必败，开发流水线空转。本次修复：

1. 适配层错误归因：404 + `Model not found` → `config_error`（`retryable=false`），首次失败即判死，不进重试；
2. 启动/注册阶段：用配置 key 调 `/v1/models` 校验配置模型名，失败即拒绝健康注册，并输出「配置模型名 vs 可用列表」差异；
3. 探活：增加一次最小模型调用（`max_tokens=1`），运行时页提前暴露同一配置错误。

## 实际代码落点（以目标分支文件为准）

> Issue Agent 的模块路径由降级知识基线推断（`src/runtime/*.py`）；目标分支实际布局为
> `src/domains/runtime/engine/*.py`，对应关系如下。

| Spec 模块 | 实际文件 | 职责 |
|---|---|---|
| `src/runtime/opencode.py` | `src/domains/runtime/engine/opencode.py` + 错误分类收口 `transient_retry.py` | opencode 错误文本在 NDJSON 流中以 `error` 事件上报，分类统一收口在 `classify_stream_failure` |
| `src/runtime/engine_model.py` | `src/domains/runtime/engine/engine_model.py` | 公共校验函数：opencode 配置解析、`/v1/models` 对齐校验、最小模型调用（与 #2545 共用） |
| `src/runtime/transient_retry.py` | `src/domains/runtime/engine/transient_retry.py` | `config_error` 短路，不重试；失败摘要标签 |
| `src/runtime/health.py` | `src/domains/runtime/engine/local_probe.py`（本机直连探活）+ `agent.py::_probe_engine_worker`（worker 探活） | 探活增加最小模型调用并落 `probe_error` |
| `src/runtime/registry.py` | `src/domains/runtime/engine/registry.py` | 注册阶段校验入口（委托 engine_model） |

## 范围

### In
- `config_error` 归类与重试短路（含失败摘要文案）。
- opencode 配置（`~/.config/opencode/opencode.json[jc]`）模型名/网关解析，`/v1/models` 对齐校验与差异输出。
- 注册发现链路（worker `_discover_local_runtimes`）对 opencode 做模型校验，misaligned → `healthy=False`（拒绝健康注册）。
- 本机直连探活（`probe_local_engines`）与 worker 探活（`_probe_engine_worker`）增加最小模型调用，失败原因写入 `probe_error`。

### Out
- hermes 侧同族修复（#2545 已覆盖，本单只共用校验函数）。
- 网关侧模型清单治理、模型名自动改写、已失败任务补偿重跑。

## 验收标准映射

| 验收标准 | 实现 / 测试 |
|---|---|
| 首次失败即 `config_error`、重试 0 次、记录无 `upstream_transient` | `transient_retry.classify_stream_failure` 在瞬时规则前判定；`tests/runtime/test_issue_3820_config_error_classification.py` |
| 启动/注册阶段失败并输出模型名 vs `/v1/models` 可用列表差异 | `engine_model.validate_engine_model` + `agent._discover_local_runtimes`；`tests/test_issue_3820_engine_model_opencode.py`、`tests/test_issue_3820_registration_probe.py` |
| 探活检出并在运行时页展示 | `engine_model.probe_engine_model_call` → `local_probe`/`_probe_engine_worker` 的 `probe_error`；同上测试文件 |
| 不再产生透传式 404 运行时记录 | 注册阶段拒绝 misaligned 引擎 + 分类短路，任务不再进入重试循环 |
| 既有 transient 重试测试与新增测试通过 | `tests/runtime/test_transient_retry.py` 回归 + 新增 3 个测试文件 |

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 文本匹配过宽误判 config_error | 双条件：`model … not found` 短语 **且** 404/`Not Found:` 状态上下文；反例测试（普通业务文本含 "model not found" 不判 config） |
| `/v1/models` 不可达误拒注册 | 仅 `misaligned` 拒绝；`unreachable`/`http_error`/`invalid_response` 记告警并放行（不误伤离线环境） |
| 探活成本/延迟 | `max_tokens=1` + 短超时；仅 opencode 每次探活增加一次小调用 |
| 阻塞主循环 | 注册校验使用 3s 短超时；探活走既有线程池/探活线程 |
| 运维需要临时绕过 | `GRAPE_MODEL_CHECK_DISABLED=1` 关闭校验（显式、可观测） |