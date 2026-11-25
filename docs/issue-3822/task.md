# Issue #3822 任务拆解（task）

| # | 切片 | 交付物 | 验收 |
|---|---|---|---|
| 1 | 前缀归一 + 本地查表诊断 | `engine_model.split_model_prefix`、`read_opencode_model_config` 的 `local_issues`、`validate_engine_model` 合并文案 | `tests/test_issue_3822_engine_model_prefix.py` 全绿 |
| 2 | hermes fallback 覆盖 | `read_hermes_model_config` 读 `fallback_providers`/`fallback_model`；降级解析器支持块序列 | `tests/test_issue_3822_hermes_fallback.py` 全绿（含无 pyyaml 对拍） |
| 3 | 24h 累积升级 | `transient_retry.record_model_config_error`、`_do_requeue` 保留计数器、`[MODEL_CONFIG_ALERT]` 日志签名 | `tests/runtime/test_issue_3822_model_config_alert.py` 全绿 |
| 4 | 监控面接线 | `public.alerts_task_model_config_alert`、`alerts._repo_metrics` 的 `task_model_config_alert`、种子规则 | `tests/insight/test_issue_3822_obs_model_config_alert.py` 全绿 |
| 5 | 回归 | #1986 三文件 + #2545 + 既有 transient_retry 用例 | 全绿不回归 |
| 6 | 文档 + PR | docs/issue-3822/{spec,plan,task}.md、PR（base `arch-weekly-11-ljt`） | PR 创建并在 Issue 评论告知 + A2A QA |

## 顺序与依赖

1 → 2 可并行（同文件不同函数段，先写测试后实现，逐切片提交）；3 依赖 1 的 `is_model_not_found_error`（已存在）；
4 依赖 3 的 `MODEL_CONFIG_ALERT` 签名常量（由 `public` 侧独立常量承接，避免跨域 import 内部模块）。

## 不做（边界）

- 不改动网关清单、不改引擎默认模型（轨 A / 运维）。
- 不改 #1986 已实现的分类短路与探活逻辑。
- 不做生产发布（轨 B）。