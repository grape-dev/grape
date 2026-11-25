# Issue #3822 方案设计（plan）— 轨 C 代码补强

## 1. 累积告警（transient_retry.py）

### 1.1 计数载体
不新增表/列：计数放在 `agent_tasks.errors` 的结构化条目里（JSON 列表，已有 5 槽保留策略）。

```json
{
  "error_code": "MODEL_CONFIG_ALERT",
  "error_class": "config_error",
  "count": 2,
  "first_seen_at": "2026-11-23T00:28:42+00:00",
  "last_seen_at": "2026-11-23T01:10:03+00:00",
  "escalated": true,
  "escalated_at": "2026-11-23T01:10:03+00:00",
  "reason": "Not Found: Model not found"
}
```

- 同一 task 只保留**一条** `MODEL_CONFIG_ALERT`（每次检测到 `config_error` 就地更新 count/last_seen_at）。
- 窗口：`MODEL_CONFIG_ALERT_WINDOW_SEC = 86400`（24h）。`first_seen_at` 超窗 → 计数重置为 1。
- 升阈值：`MODEL_CONFIG_ALERT_ESCALATE_AT = 2`。首次跨过阈值写 `escalated=true` 并 `log.error`，
  日志前缀固定 `[MODEL_CONFIG_ALERT]`（Loki 可直接 `|= "[MODEL_CONFIG_ALERT]"` 检索）。

### 1.2 接线
`retry_transient_task` 的非瞬时 `CONFIG_ERROR` 分支改为调用
`record_model_config_error(task_id, reason)`（内部：读 errors → 累计 → 写回 → 按阈值升级），
取代原来的单次 `_append_task_error({"error_code": "CONFIG_ERROR"})` 记录。返回值透出 `{count, escalated}`。

### 1.3 requeue 保留
`_do_requeue` 复位 `errors` 时，除写 `TRANSIENT_RETRY` 外**保留**既有 `MODEL_CONFIG_ALERT` 条目
（其余仍归档到事件 `agent_logs`），保证跨轮次累计不丢。

### 1.4 监控面（insight 域）
- `runtime/public`：新增 `alerts_task_model_config_alert(cutoff, signature)`，与既有
  `alerts_task_timeout` 同口径（`status='failed'` + `completed_at >= cutoff` + `result_text/errors LIKE`），
  按 `repo_key` 计数；**额外要求 `escalated: true`**——首次命中（count=1）也会写入
  `MODEL_CONFIG_ALERT` 条目，仅凭签名会把单次 `Model not found` 误算为配置故障（审查 Required）。
- `alerts._repo_metrics()`：新增指标 `task_model_config_alert`（24h 窗口）。
- `SEED_RULES`：新增 `task_model_config_alert`（`level='user'`、`comparator='gt'`、`threshold=0`、
  `severity='critical'`、`for_sec=0`），与 #1985 引擎离线熔断（runtime 在线数指标）语义区分。

## 2. 校验补覆盖（engine_model.py · hermes）

- `read_hermes_model_config` 追加读取 `fallback_providers`（列表或单对象）与旧键 `fallback_model`：
  - 旧键 `fallback_model` 的裸模型名标量写法（`fallback_model: glm-5`）同样纳入（网关回落主网关）；
  - 条目身份 `(provider, model, base_url)` 去重（与 hermes `get_fallback_chain` 同口径）；
  - 网关解析顺序：条目 `base_url`/`api_key` → 条目 `key_env`/`api_key_env` → `providers[provider]`
    （`api` + 无 `api_key` 时取 `api_key`）→ 回落主网关（仅当条目无独立 base_url）；
  - model 为空 / provider 为空 / 非映射条目 → 跳过并记 warning（不阻断）。
- 降级 YAML 解析器（worker 无 pyyaml）新增**块序列**支持：
  - `- key: value` 起始项 + 后续同缩进续行 → 映射项；
  - `- scalar` → 字符串项；
  - `- {inline: flow}` → 流映射项。
  目的：`fallback_providers` 为列表时不再整体「解析失败 → 跳过校验」。
- 不误拒自证：
  - 多网关分组：一个网关 `unreachable` + 另一网关 `ok` → 整体 `unreachable`（放行）；任一网关
    `misaligned` → 整体 `misaligned`（真缺失才拒）；
  - `key_env` 指向未定义环境变量 → 该网关 target 跳过 + warning，整体不判 misaligned。

## 3. 前缀归一 + 本地查表诊断（engine_model.py · opencode）

新增 `split_model_prefix(model, provider_ids)`：

- `model` 含 `/` 且首段 ∈ `provider_ids` → `(首段, 余下裸 id)`；否则 `("", model)`。
- 用途一（default）：`model: opengw/glm-5.3` → 裸 `glm-5.3`。**顶层 `model` 恒为
  `provider/model-id`**：即使 provider 段未在 `provider` 块声明（内置 provider 形态，
  如 `deepseek/<id>`），也剥出裸 id 用于网关比对/探活（`provider_id` 留空，避免
  「provider not found」误告警 → 不把带前缀名发给网关导致 404 误判 unhealthy）。
- 用途二（比对口径）：`models` 键与 default 比较前统一取裸 id，避免「网关清单里有却报缺失」的假差异。

新增两类**本地查表类**诊断（`read_opencode_model_config` 返回 `local_issues`）：

| 诊断 | 触发条件 | 运行期后果 | 处置 |
|---|---|---|---|
| `prefixed_key` | `provider.<pid>.models` 的键 = `<pid>/<id>`（重复 provider 前缀） | opencode 本地按裸 id 查表 → `Model not found: <pid>/<id>` | 注册期判 `misaligned`，文案给出「键改为裸 id」 |
| `undeclared_default` | default 裸 id 不在该 provider 已声明的 `models` 键集（且 `models` 非空）；default 无前缀且只有一个 provider 时按该 provider 判定 | 同上（本地查表未命中） | 注册期判 `misaligned`，文案给出「把该 id 加入 models 或在列表中选一个」 |

`validate_engine_model` 合并输出：网关差异（configured / missing / available）在前，本地查表诊断在后；
两者任一命中均为 `misaligned`；`unreachable` 仍放行（不误拒）。

## 4. 测试 seam

| 切片 | 公开 seam | 测试文件 |
|---|---|---|
| 前缀归一 / 本地查表诊断 | `split_model_prefix`、`read_opencode_model_config`、`validate_engine_model` | `tests/test_issue_3822_engine_model_prefix.py` |
| hermes fallback 覆盖 / 降级解析 / 不误拒 | `read_hermes_model_config`、`check_models_against_gateway`、`validate_engine_model` | `tests/test_issue_3822_hermes_fallback.py` |
| 24h 累计与升级 | `retry_transient_task`、`record_model_config_error`、`_do_requeue` | `tests/runtime/test_issue_3822_model_config_alert.py` |
| 监控面指标与规则 | `alerts._repo_metrics`、`alerts.SEED_RULES`、`public.alerts_task_model_config_alert` | `tests/insight/test_issue_3822_obs_model_config_alert.py` |

全部网络交互通过注入 `fetch`/`post` 替身，测试不出网。

## 5. 提交计划（TDD 红/绿分提交）

1. `test(#3822)`：4 个测试文件（红）。
2. `feat(#3822)`：engine_model 前缀归一 + hermes fallback 覆盖 + 降级解析器序列支持（绿）。
3. `feat(#3822)`：transient_retry 24h 累计升级 + requeue 保留（绿）。
4. `feat(#3822)`：insight 指标/种子规则接线（绿）。
5. `docs(#3822)`：spec/plan/task。