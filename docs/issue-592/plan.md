# Plan: Issue #592

## 改动清单

1. `src/runtime/health.py`：新增 `derive_local_runtime_health(installed, healthy)`。
2. `src/api/routes/dashboard.py`：`agent_engines` 状态派生按 `credential_id` 分流（local → 探活口径，remote → 心跳口径）。
3. `src/api/routes/settings.py`：`_engine_health` 本机分支复用 `derive_local_runtime_health`。
4. `src/runtime/engine_select.py`：`query_engines` local 档无 capabilities 标签的行默认 `all`（数量偏少修复）。
5. `tests/test_issue_592_engine_modal_local.py`：新增测试（参考 test_issue_227 模式）。

## 不改动

- 前端弹窗组件（数据源修正后展示自然正确）。
- 远端引擎心跳口径 / 探活循环周期 / 派发链路。

## 风险

- 本机直连口径放宽可能把「探活不健康」的行显示为 offline（符合预期：探活真实结果）。
- local 无标签默认 all：与注册默认值及派发广播语义一致，远端行不受影响（仅 local 档生效）。
