# Plan: Issue #512 — agent_runtimes 时间口径统一为 UTC

## 方案概述

方案①（写入侧参数化 UTC）+ 方案③（读取侧归一兜底）+ 一次性幂等迁移，不重构 mysql_compat。

## 实现路径

### 1. 写入侧参数化（src/user/db.py::save_agent_runtimes）
- 函数开头计算 `_now_iso = datetime.now(timezone.utc).isoformat()`
- UPDATE：`last_seen_at=datetime('now'), updated_at=datetime('now')` → `last_seen_at=?, updated_at=?`，
  追加 `_now_iso` 两次入参
- INSERT：列清单补 `last_seen_at, updated_at`，值补两个 `_now_iso`
- 效果：MySQL 下不再走 NOW()，写入值 = `2026-10-29T07:30:00.123456+00:00`，与心跳链路一致

### 2. 存量幂等迁移（src/user/db.py::migrate_agent_runtimes_utc_caliber）
- 仅 MySQL backend 生效（sqlite 下 naive 已是 UTC，无超前问题 → 直接返回 0）
- 扫描 `agent_runtimes` 全行，对 `last_seen_at`/`updated_at` 中满足 naive 格式
  （`YYYY-MM-DD HH:MM:SS[.ffffff]`，无 Z/无 offset）且按 UTC 解释后 **超前当前 UTC（容差 60s）** 的值，
  用 `normalize_event_time`（mysql 语义 CST→UTC）改写为带 +00:00 的 UTC ISO
- 幂等性：改写后值带时区偏移，不再命中 naive 正则；重复执行不重复改写
- 挂载点：`save_agent_runtimes` 开头调用——本机直连的周期重探（main.py 启动任务）与
  手动刷新（POST /agent-runtimes/refresh）均经此入口，满足"启动/刷新钩子"

### 3. 读取侧归一兜底（src/api/routes/settings.py::get_agent_runtimes）
- `cached = user_db.get_agent_runtimes(scope=None)` 后对每行 `last_seen_at`/`updated_at`
  过 `normalize_event_time`（该模块已 import）
- 兜底场景：任何残留/未迁移的 naive 值在 MySQL 语义下被正确归一为 UTC；
  带 offset 值幂等原样通过

### 4. 健康判定语义对齐（src/runtime/health.py::parse_dt）
- 无时区值：mysql → 视为 CST(+08:00) → epoch；sqlite → 视为 UTC（行为不变）
- 复用 `src.api.time_utils._db_backend()` 判定；防"未来时间"误判，与归一口径一致

### 5. 测试
- 新增 tests/test_issue_512_runtime_utc.py（atomic marker）：
  - 写入链路 INSERT/UPDATE 产物为 UTC ISO（带 +00:00）
  - `online_runtime_last_seen_not_in_future`：模拟 MySQL CST 超前行，迁移后不超前当前 UTC（转正验收）
  - 迁移幂等：第二次执行不再改动
  - 读取出口：naive CST 行经 get_agent_runtimes 归一为 UTC ISO
- pytest.ini 增加 `atomic` marker 声明（`-m atomic` 可用）

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 迁移误伤正常行 | 仅改写"超前当前 UTC（容差 60s）"的行；sqlite 不执行 |
| 读取归一影响既有解析 | 统一输出 ISO 带 offset，与心跳链路现状一致（normalize_event_time 幂等） |
| 双 backend 语义差异 | sqlite 行为不变；mysql 语义由单测 patch `_db_backend` 覆盖 |