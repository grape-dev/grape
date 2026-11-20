# Issue #2925 — 任务拆解（task）

- [x] `pipeline.py`：抽取 `_is_placeholder_title` 单一口径（占位集合 + 前缀）。
- [x] `pipeline.py`：新增 `_backfill_window_titles`（窗口收敛 + 负缓存 + 同口径写回 + 结构化日志）。
- [x] `pipeline.py`：`_get_issue_pipeline` 删除逐条 API 兜底；title_map 命中时跳过冗余 DB 查询。
- [x] `pipeline.py`：`get_pipeline` 接入窗口批量兜底，窗口大小 = `1 + history_offset + history_limit`。
- [x] `event_pipeline.py`：新增 `fetch_issue_titles_batch`（token/client 复用 + 有限并发 + 总 deadline）。
- [x] `event_pipeline.py`：`_vcs_get_issue` 支持复用 client/timeout（向后兼容）。
- [x] `pipeline.py`：issue 表无行但取到真实标题时落进程内真实标题缓存（TTL 同负缓存），后续请求复用标题且 0 外呼。
- [x] 日志口径拆分：`submitted` / `vcs_calls`（实际开始执行）/ `cancelled` / `fetched` / `failed`，不再把 deadline 取消的排队任务计入外呼数。
- [x] 迁移 `202611170043`：`issue.title_fetched_at`（走 `register_migration`，幂等 + strict）。
- [x] 测试 `tests/runtime/test_pipeline_title_backfill_2925.py`（26 条：口径/窗口/写回/负缓存/deadline/token 复用/空 map/跨仓/并发护栏/窗口接线/无行降级/取消不标记/token 计入 deadline/最新仓/标记分片/无行成功分支复用/`submitted` 口径）。
- [x] 测试 `tests/api/test_issue_2925_title_fetched_at_migration.py`（3 条：迁移补列/幂等/strict 失败不盖章）。
- [x] 回归：`tests/runtime/test_pipeline.py`、`test_issue_fetch_via_client.py`、
      `test_w11_gitee_issue_read_card_driven.py`、`test_d29_comment_delivery_card_driven.py`、
      `tests/api/test_migration_track_guard.py`（原有 5/9 条失败为基线既有，改动前后一致）。
- [x] 前后耗时对比（模拟 97 条、单请求延迟 0.3s；本机实测）：
      - 逐条串行：**29.11s**
      - 有界批量（并发 8 / deadline 4s / 超时 3.5s）：**3.90s**，97 条全部回填
      - 热请求（0 candidates）：0 次 VCS 调用