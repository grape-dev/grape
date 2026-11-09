# Tasks: Issue #494

- T1 `src/agent/failure_attribution.py` 归因文案 helper（超时/进程退出码/失败摘要）— done
- T2 hwcloud_acp / hermes_acp stopReason=timeout → errors 显式「执行超时（Ns）」— done
- T3 executor.py returncode=124/-15 超时归因 + 静默强杀文案统一 — done
- T4 event_pipeline direct 失败收口：errors 合并落 agent_tasks + result 摘要透出 — done
- T5 events.py 详情 API task_errors 字段 — done
- T6 前端失败态 errors 摘要（200 字符截断 + 展开/收起）+ i18n — done
- T7 tests/test_failure_attribution_494.py 单测 + 回归 — done

验证：`python3 -m pytest tests/test_failure_attribution_494.py tests/test_hermes_stderr_tail_and_init_retry.py tests/test_hwcloud_acp.py -q`
