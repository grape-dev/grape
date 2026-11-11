# Spec — Issue #869 IM 新引擎租约可靠性修复 + 多主控分片接线

> 完整 Spec 由 Issue Agent 发布于 Issue #869 评论（[issue agent] 📋 Spec Document），本文为落地摘录。

## Objective
修复新 IM 引擎分布式租约三根因（P1 正确性缺陷，多实例部署必现重复连接、双回复）：
1. Supervisor 只 `try_acquire` 不 `renew`，TTL 60s 锁过期后本实例仍握 WS 连接；
2. `bot_lease.py` `_now/_future` 写 UTC vs SQL `datetime('now')`（MySQL 翻译为 `NOW()` = 服务器本地 CST）比较口径差 8h，锁出生即过期；
3. `Supervisor.instances` 固定 `[instance_id]`，`list_active_instances` 未接线，多实例哈希环空转。

## Scope
- In：`src/im/engine/supervisor.py`（每轮扫描 batch_renew + list_active_instances 刷新环）、`src/im/bot_lease.py`（租约时间戳写入/比较统一 Python UTC 参数，不动 created_at 历史口径、不动 #365 已修的 heartbeat 写侧）、`src/im/engine_wiring.py`（LegacyCompatLeaseStore 补 list_active_instances 透传）、`tests/im/` 新增用例。
- Out：legacy BotManager 链路、哈希环算法本身、#870 健康指标、数据迁移。

## Acceptance Criteria
- acquire → renew 后 `lease_expires_at` 前移且可被抢占行数=0（SQLite 真后端 + MySQL 方言翻译验证）；
- 两实例环分配不重叠，各方只连归属安装；单实例 channels 仍为全量；
- 心跳过期/未来时间（CST 脏行）实例不进环；
- `pytest tests/im` 全绿（基线 103 passed）。
