# Tasks: Issue #423

- [x] T1：`_probe_hermes_acp._read_response` 改 deadline 驱动 + 500 行防御上限（src/runtime/agent.py）
- [x] T2：hermes prompt `stopReason=refusal` → `status="refused"` 独立标记
- [x] T3：`_probe_hwcloud_acp` 同步审查（已是 deadline 驱动）+ 补防御上限与 refusal 标记
- [x] T4：新增 tests/test_probe_acp_read_response_423.py（通知 ≥10 行 / refused / EOF / 防御上限 / hwcloud 全流程）
- [x] T5：全量相关测试回归 + 自审查 + 提交 + PR
