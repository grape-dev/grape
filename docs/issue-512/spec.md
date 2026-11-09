# Spec: Issue #512 — agent_runtimes.last_seen_at 超前 UTC 约 8h（本地墙钟写入 UTC 口径列）

> 开发实现规格（依据 Issue Agent 已发布 Spec，落地为可执行方案）

## Objective

修复 `agent_runtimes` 表时间口径混写：

- 本机直连刷新链路 `src/user/db.py::save_agent_runtimes` 的 SQL 字面量
  `last_seen_at=datetime('now'), updated_at=datetime('now')` 在 MySQL backend 下经
  `src/mysql_compat.py` 翻译为 `CAST(NOW() AS CHAR)`（会话时区 CST 本地墙钟、无时区标记），
- 与心跳链路（`src/api/routes/runtime.py` 等，写 `datetime.now(timezone.utc).isoformat()`，
  UTC 带 +00:00）同表混存，导致本机直连行的 `last_seen_at` 超前 UTC 约 8h，
  健康窗口（`derive_runtime_health` / `parse_dt` 按 UTC 解析无时区值）误判为"未来时间"。

用户价值：消除跨端 8h 偏差，恢复健康判定准确性，前端显示与心跳链路口径一致。

## 成功标准（Acceptance）

- [ ] MySQL backend 下所有 `last_seen_at` 均不超前当前 UTC
- [ ] `-m atomic -k online_runtime_last_seen_not_in_future` 通过（转正）
- [ ] 存量超前 8h 的行迁移完成且幂等
- [ ] 同表 `updated_at` 口径一并对齐（UTC 带 offset）
- [ ] 现有测试全部通过

## 范围

### In
- 写入侧：`save_agent_runtimes` 参数化 UTC 时间戳（UPDATE 与 INSERT 两条路径，覆盖
  `last_seen_at`/`updated_at`）
- 读取侧：`GET /api/settings/agent-runtimes` 出口 `normalize_event_time` 兜底归一
- 健康判定：`src/runtime/health.py::parse_dt` 无时区值语义与归一口径一致
  （mysql=CST(+08:00)，sqlite=UTC）
- 存量迁移：幂等修正"超前当前 UTC"的行（新增迁移函数，挂入刷新钩子）
- 测试：新增 `online_runtime_last_seen_not_in_future` 及写入→读取全链路单测

### Out
- 不重构 `mysql_compat` 其他方言翻译行为
- 不做全库其他表时间口径全局治理
- 不调整前端时间展示组件

## 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 修复层级 | 写入侧参数化 UTC 为主 + 读取侧归一兜底 + 存量幂等迁移 | 与心跳链路同源口径，跨 SQLite/MySQL 语义统一 |
| 存量脏数据 | 仅迁移"超前当前 UTC"的行 | 避免误伤正常离线行；迁移后值带 offset，天然幂等 |
| updated_at | 同表一并对齐 | 同链路同字面量写入，一并参数化 + 迁移 |

## 涉及文件

| 文件 | 改动 |
|------|------|
| src/user/db.py | save_agent_runtimes 参数化 UTC；新增 migrate_agent_runtimes_utc_caliber（幂等迁移） |
| src/api/routes/settings.py | get_agent_runtimes 出口 normalize_event_time（last_seen_at/updated_at） |
| src/runtime/health.py | parse_dt 无时区语义对齐（backend-aware） |
| tests/test_issue_512_runtime_utc.py | 新增（写入/读取/迁移/转正测试） |
| pytest.ini | 增加 atomic marker 声明 |