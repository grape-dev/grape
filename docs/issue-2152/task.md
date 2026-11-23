# Task List: Issue #2152

- [x] Task 1: 更新 dsh prepare_env 契约测试
  - Acceptance: 覆盖 cwd 位置、0600、无 token、无 cwd warning、异常 warning。
  - Verify: `python3 -m pytest tests/runtime/test_dsh_runtime.py -q`
  - Files: `tests/runtime/test_dsh_runtime.py`

- [x] Task 2: 更新 executor 清理与会话接线测试
  - Acceptance: 任务结束清理 cwd token；异常路径也清理；无 prepare_env 后端 env 不变。
  - Verify: `python3 -m pytest tests/runtime/test_dsh_runtime.py -q`
  - Files: `tests/runtime/test_dsh_runtime.py`

- [x] Task 3: 实现 dsh cwd 落盘与 git exclude
  - Acceptance: `prepare_env` 设置 cwd 内 `GRAPE_AUTH_FILE` 并注册 exclude；失败 warning 且不阻断。
  - Verify: `python3 -m pytest tests/runtime/test_dsh_runtime.py -q`
  - Files: `src/domains/runtime/engine/dsh.py`

- [x] Task 4: 接入 executor 生命周期清理
  - Acceptance: 通用 CLI 分支传 cwd；正常、异常、超时收尾均清理对应 token 文件。
  - Verify: `python3 -m pytest tests/runtime/test_dsh_runtime.py tests/runtime/test_executor_token_env.py -q`
  - Files: `src/domains/runtime/agents/executor.py`, `tests/runtime/test_dsh_runtime.py`
