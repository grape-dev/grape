# Issue #3822 Spec — 轨 C 代码补强：模型配置故障防复发（前缀/覆盖/二次告警）

> 需求来源：Issue #3822 正文 + Issue Agent Spec（评论 202861672）+ SRE 轨 A/B 结论与轨 C 交办（评论 202862302）。
> 编码基线：`arch-weekly-11-ljt`（worktree 分支 `feat/issue-3822`）。

## 背景与已确认事实

| 事实 | 来源 |
|---|---|
| 网关 BYOK key 的 `/v1/models` 只有 4 个**裸 id**：`deepseek-v4-pro-0813`、`deepseek-v4.1-flash-1017`、`glm-5.3`、`glm-5.3-flash` | 轨 A 两次回读（08:17:10 / 08:27:25 CST，HTTP 200） |
| `opengw/deepseek-v4.1-flash-1017`、`opengw/glm-5.3`（带 provider 前缀写法）→ HTTP 404，body 原文 `Model not found` | 轨 A 1-token 探针 |
| 125 次真实失败两种形态：`Model not found: <provider>/<model>`（8 条，opencode **本地**查表失败）与 `Not Found: Model not found`（117 条，网关 404） | Issue 正文 + 轨 A 对照 |
| #1986 已在 `arch-weekly-11-ljt` 落地：`config_error` 归类、`validate_engine_model` / `probe_engine_model_call`、注册期拒绝 misaligned opencode | Issue Agent 事实核对（3 个测试文件 31 passed） |
| #1986 **未覆盖**：hermes `fallback_providers`（2 个 404 id 从该缺口漏过）、同一 task 24h 内二次 `Model not found` 的累积告警、`opengw/<id>` 前缀写法的判定与诊断 | SRE 轨 C 交办 |

## 目标（轨 C 四项）

1. **累积告警**：同一 task 24h 内 `Model not found` ≥2 次 → 升级「配置故障」告警（与 #1985 引擎离线熔断区分：本条是配置错位反复命中，不是引擎下线），不再静默按瞬时抖动处理。
2. **校验补覆盖**：hermes `fallback_providers` / 旧 `fallback_model` 纳入模型名校验；多网关分组、`{env:VAR}` / `key_env` 占位 key 场景**不误拒**（自证用例）。
3. **前缀归一**：`opengw/<id>` 与 `<id>` 判定为同一模型；带 provider 前缀的 opencode `models` 键 / 未在 provider `models` 声明的 default 模型，注册期即给出可执行诊断（从源头消除「清单里有、却报 Model not found」）。
4. **差异可见**：misaligned 时注册/启动日志与注册响应直接输出「配置 vs 可用列表」差异（#1986 文案已具备，本单补齐前缀/未声明两类诊断并加用例锁定）。

## 范围

### In
- `src/domains/runtime/engine/engine_model.py`：opencode 前缀归一 + 本地查表类诊断；hermes `fallback_providers`/`fallback_model` 读取；降级 YAML 解析器支持块序列（`- ` 项）。
- `src/domains/runtime/engine/transient_retry.py`：同一 task 24h 内 `Model not found` 累积计数、≥2 次升级告警（日志签名 + `agent_tasks.errors` 结构化条目），requeue 时保留计数器。
- `src/domains/runtime/public/__init__.py` + `src/domains/insight/observability/alerts.py`：按仓库聚合「配置故障」失败任务数（24h 窗口）+ 种子告警规则 `task_model_config_alert`。

### Out
- 网关侧模型清单治理、引擎默认模型改写（轨 A 运维职责，已由 SRE 完成本机整改）。
- #1986 已落地的分类短路、探活最小调用、注册拒绝逻辑（本单只补缺口，不重写）。
- 生产发布（轨 B）：`arch-weekly-11-ljt` → 生产 worker 运行包的发布确认。
- #1985 引擎永久离线熔断（不同故障域）。

## 验收标准映射

| 验收标准 | 实现 / 用例 |
|---|---|
| 同一 task 24h 内二次 `Model not found` 触发「配置故障」告警，日志/任务详情可查 | `transient_retry.record_model_config_error`（累计 + `MODEL_CONFIG_ALERT` 条目 + `log.error` 签名）；`tests/runtime/test_issue_3822_model_config_alert.py` |
| 告警在监控面可查（24h 窗口，按仓库） | `alerts._repo_metrics` 的 `task_model_config_alert` + 种子规则；`tests/insight/test_issue_3822_obs_model_config_alert.py` |
| `opengw/<id>` 与 `<id>` 视为同一模型；前缀写法给出可执行诊断 | `engine_model.split_model_prefix` / `read_opencode_model_config` 的 `prefix_issues` / `undeclared_default`；`tests/test_issue_3822_engine_model_prefix.py` |
| hermes `fallback_providers` 模型纳入校验（含无 pyyaml 降级路径） | `read_hermes_model_config` + `_fallback_parse` 序列支持；`tests/test_issue_3822_hermes_fallback.py` |
| 多网关分组 / 占位 key 场景无误拒证据 | 同上测试文件的「不误拒」用例 |
| 既有 #1986 / #2545 用例不回归 | `tests/test_issue_3820_*.py`、`tests/test_engine_model_2545.py`、`tests/runtime/test_transient_retry.py` 回归 |

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 前缀归一导致「带前缀配置」被误判为对齐 → 运行期仍 404 | 归一仅用于**比对口径**；带前缀的 `models` 键与未声明的 default 仍判 `misaligned`（运行期必失败），文案给出「改成裸 id」的可执行建议 |
| 24h 累积计数在 requeue 清空 errors 时丢失 | `_do_requeue` 显式保留 `MODEL_CONFIG_ALERT` 条目（其余照旧归档到事件日志） |
| 新增仓库维度指标引入全表扫描 | 与既有 `task_timeout` 同口径（`status='failed'` + `completed_at` 时间下界 + `LIKE` 签名），不新增扫描形态 |
| 无 pyyaml 降级解析器新增序列支持引入解析回归 | 与 pyyaml 路径做同结果对拍（`TestFallbackParserParity` 扩展） |