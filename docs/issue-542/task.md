# Issue #542 任务拆解

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| 1 | pipeline：`_get_issue_pipeline` 增加 `events_by_issue` 索引参数，调用方传入；PR 标题兜底走索引键 | `src/api/routes/pipeline.py` | done |
| 2 | pipeline：`history_limit` 截断 + `history_total` 响应字段 | `src/api/routes/pipeline.py` | done |
| 3 | issues：`_query_tasks` 支持 limit/offset | `src/api/issues_center.py` | done |
| 4 | issues：`_group_events_by_issue`/`_issue_stage_map` 支持 numbers 收窄；PR 卡片 qa_history 截 50 | `src/api/routes/issues.py` | done |
| 5 | issues：`page`/`per_page` 真分页 + `recent_terminal` 默认终态截断 + 三模式排序语义 | `src/api/routes/issues.py` | done |
| 6 | scenes：branches TTL 缓存（90s）+ refresh=1 + 建分支失效 | `src/api/routes/scenes.py` | done |
| 7 | nginx：gzip(application/json) | `nginx-grape.conf` | done |
| 8 | 测试：索引等价性 / 分页 / 截断 / 缓存 | `tests/test_pipeline.py`、`tests/test_issues_api.py`、`tests/test_scenes_branches_cache.py` | done |
| 9 | 后续拆分：events 归档、扩容评估、慢接口监控埋点（分析文档第三步） | — | follow-up |

验证命令：
- `python -m pytest tests/test_pipeline.py tests/test_issues_api.py tests/test_scenes_branches_cache.py -q`
- 基线既有失败（与本 Issue 无关，`weekly-09-ljt` 上复现）：test_pipeline 4 个 assignee mock 签名失败、`test_pipeline_issues_sorted_latest_first` 401、`test_aggregates_stage_and_status` KeyError、`test_events_empty` 响应形状断言。
