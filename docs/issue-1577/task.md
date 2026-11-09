# Issue #1577 任务拆解

- [x] T1 `src/user/db.py`：新增 `get_business_api_url()` getter。
- [x] T2 `src/config.py`：`task_business_api_url` 可选配置双写 meta。
- [x] T3 `src/prompt_builder.py`：`_runtime_is_local` + `_resolve_business_server_url`。
- [x] T4 `src/api/event_pipeline.py`：两处 env 组装注入双基址。
- [x] T5 `src/runtime/agent.py`：本机/内网 worker 仅覆盖 runtime 基址 + 回退。
- [x] T6 `help/usage/agent-environment-variables.md`：补充 `RUNTIME_SERVER_URL` 与语义。
- [x] T7 `tests/test_issue_1577_server_url_split.py`：双地址/单地址/回退单测。
- [x] T8 运行新增与相关回归测试。