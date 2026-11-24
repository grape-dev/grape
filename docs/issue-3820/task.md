# Issue #3820 任务拆解（task）

| # | 任务 | 产出 | 验收 |
|---|---|---|---|
| 1 | opencode 配置解析与 `/v1/models` 对齐校验（公共函数） | `engine_model.read_opencode_model_config` / `validate_engine_model` / `probe_engine_model_call` / `is_model_not_found_error` | 单测：合法 / misaligned（含差异）/ 不可达放行 / 配置缺失 |
| 2 | `config_error` 分类与重试短路 | `transient_retry.CONFIG_ERROR`、`classify_stream_failure`、`retry_transient_task`、`format_stream_failure_result` | 单测：`Not Found: Model not found` 首次即 config_error、0 重试、errors 无 upstream_transient；既有 transient 用例回归 |
| 3 | 注册阶段校验 | `registry.validate_registration_model` + `agent._discover_local_runtimes`（misaligned → healthy=False）+ 启动告警 | 单测：misaligned 时 opencode 注册 healthy=False |
| 4 | 探活最小模型调用 | `local_probe._opencode_model_call_probe` + `agent._probe_engine_worker` opencode 分支 | 单测：404 → healthy=False + probe_error；200 → healthy=True |
| 5 | 失败摘要键接线 | `service.fail_task` / `direct_executor` 摘要筛选纳入 `CONFIG_ERROR` | 覆盖在 #2 的判死记录断言 |
| 6 | 文档与收尾 | `docs/issue-3820/{spec,plan,task}.md`、PR、Issue 评论、A2A QA | 自审查 + pre-pr 增量检查通过 |

依赖顺序：1 → (2,3,4 并行) → 5 → 6。

TDD 说明：先提交失败测试（`test(#3820)`），再提交最小实现（`feat(#3820)`）。测试 seam 全部走公开函数，网络通过
`fetch`/`post` 注入替身，不出网。