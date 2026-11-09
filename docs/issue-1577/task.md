# Issue #1577 任务拆解

- [x] T1 `src/user/db.py`：新增 `get_business_api_url()` getter。
- [x] T2 `src/config.py`：`task_business_api_url` 可选配置双写 meta。
- [x] T3 `src/prompt_builder.py`：`_runtime_is_local` + `_resolve_business_server_url`。
- [x] T4 `src/api/event_pipeline.py`：`_enqueue_pipeline_task` 与 `build_task_env` 统一经 `resolve_task_server_env` 注入双基址。
- [x] T5 `src/runtime/agent.py`：本机/内网 worker 覆盖 runtime 基址；单地址部署（scheme+host+port+path 同源）保留业务基址覆盖，双地址保留显式业务基址。
- [x] T6 对话入队四条链路（`src/api/routes/chat.py` / `src/im/handler_async.py` / `src/im/vod_handler_async.py` / `src/runtime/scheduler.py`）注入双基址。
- [x] T7 `src/user/db.py` / `src/api/routes/health.py`：`task_business_api_url` 并入告警与 health。
- [x] T8 `help/usage/agent-environment-variables.md`：补充 `RUNTIME_SERVER_URL` 与注入来源。
- [x] T9 `tests/test_issue_1577_server_url_split.py`：双地址/单地址/显式优先级/异常回退/告警健康单测。
- [x] T10 运行新增与相关回归测试。