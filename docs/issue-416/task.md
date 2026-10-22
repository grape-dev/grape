# Task: Issue #416 任务拆解

- [x] T1 executor.py：`_StderrTailBuffer` 尾采环形缓冲 + `_run_once` 接入（stderr 尾巴 ≥2000B、rc 上报）
- [x] T2 hermes_acp.py：stderr 尾采接入、`hermes exited rc=N` 上报、done stderr 去除 500B 截断
- [x] T3 hermes_acp.py：initialize 超时 HERMES_INIT_TIMEOUT 配置化 + HERMES_INIT_RETRIES 重试
- [x] T4 hermes_acp.py：`_build_overlay_home` Windows symlink 失败降级复制（hermes-acp-home 崩溃根因排查支撑）
- [x] T5 runtime/agent.py：worker 失败上报 stderr hint 保尾 2000B
- [x] T6 测试：tests/test_hermes_stderr_tail_and_init_retry.py（stderr 尾采 / rc 上报 / init 重试 / 配置助手）
- [x] T7 自审查 + 提交 + PR
