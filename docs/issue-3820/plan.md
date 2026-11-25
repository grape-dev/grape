# Issue #3820 方案设计（plan）

## 1. 错误分类（transient_retry.py）

- 新增 `CONFIG_ERROR = "config_error"`，非可重试。
- 新增公共判定 `engine_model.is_model_not_found_error(text)`：
  - `model[\s_-]*(is )?not[\s_-]*found`（大小写不敏感）
  - 且存在 404 状态上下文：`\b404\b` / `Not Found:` / `NotFoundError`。
- `classify_stream_failure` 顺序：auth 401 → **config_error** → upstream_transient → empty_output → task_failure。
  配置错误在瞬时规则之前短路，即便文本同时含 retry 类字样也不重试。
- `retry_transient_task`：非瞬时分支为 `config_error` 追加结构化记录
  `{"error_code": "CONFIG_ERROR", "error_class": "config_error", ...}`（不写 `TRANSIENT_RETRY`，记录里不出现 upstream_transient）。
- `format_stream_failure_result`：标签「模型配置错误（不可重试）」，与 auth_config 同款「未自动重试」文案。
- `fail_task` / direct 执行路径的失败摘要筛选补充 `CONFIG_ERROR` 记录键。

## 2. 公共校验函数（engine_model.py）

- `read_opencode_model_config(config_path=None, env=None)`：解析 `opencode.json`/`opencode.jsonc`
  - 顶层 `model`（`provider/model-id` → 取裸 `model-id`）；
  - `provider.<id>.options.baseURL`、`options.apiKey`（支持 `{env:VAR}` 与 `${VAR}`）；
  - `provider.<id>.models` 键集合 = 该网关已配置模型全集；
  - 返回与 hermes 同构的 `{config_path, default_model, targets[], warnings[]}`，直接复用
    `check_models_against_gateway` / `minimal_model_call`。
- `validate_engine_model(engine_name, ...)`：注册/启动校验，返回
  `{engine, status: ok|misaligned|unreachable|no_config|no_validatable_target|disabled, message, detail}`；
  `message` 含「配置模型名 / 缺失模型 / /v1/models 可用列表」。
- `probe_engine_model_call(engine_name, ...)`：探活最小调用（`max_tokens=1`），失败时再用 `/v1/models`
  富化差异文案；网络不可达 → 不误判。
- `is_model_not_found_error(text)`：分类与探活共用的文本判定。
- 关闭开关：`GRAPE_MODEL_CHECK_DISABLED=1` → 校验返回 `disabled`，不拦截。

## 3. 注册阶段（registry.py + agent.py）

- `registry.validate_registration_model(engine_name, timeout=3.0)` 委托 `validate_engine_model`，供 worker/本机共用。
- worker `_discover_local_runtimes`：`--version` 通过后对 opencode 做校验；
  - `misaligned` → `healthy=False`（服务端 `engine_select` 不会派单 = 拒绝健康注册），`log.error` 输出差异；
  - `unreachable`/`no_config`/`disabled` → 放行（仅告警）。
- worker 启动早期新增 `_check_opencode_model_alignment()`（`main()` 中紧随 `_check_opencode_api_keys()`），
  启动即打印差异，不阻断 worker（同机其它引擎不受影响）。

## 4. 探活（local_probe.py + agent.py）

- `local_probe.probe_local_engines`：opencode `--version` 通过后调 `engine_model.probe_engine_model_call`；
  `model_not_found` → `healthy=False` + `probe_detail.model_call` + `probe_error`（运行时页展示）。
- worker `_probe_engine_worker("opencode")`：先做最小模型调用，失败立即返回 `fail` + 差异；
  通过后继续既有 `opencode run hello` 探活。

## 5. 测试 seam

| 切片 | 公开 seam | 测试 |
|---|---|---|
| 分类 | `classify_stream_failure` / `retry_transient_task` | `tests/runtime/test_issue_3820_config_error_classification.py` |
| 配置解析 + 校验 | `read_opencode_model_config` / `validate_engine_model` / `probe_engine_model_call` | `tests/runtime/test_issue_3820_engine_model_opencode.py` |
| 注册 + 探活 | `registry.validate_registration_model` / `agent._discover_local_runtimes` / `probe_local_engines` | `tests/runtime/test_issue_3820_registration_probe.py` |

全部网络交互通过注入 `fetch`/`post` 替身，测试不出网。

## 6. 提交计划（TDD 红/绿分提交）

1. `test(#3820)`：3 个测试文件（红）。
2. `feat(#3820)`：engine_model 公共校验 + transient_retry 分类短路（绿）。
3. `feat(#3820)`：注册链路与探活接线（绿）。
4. `docs(#3820)`：spec/plan/task。