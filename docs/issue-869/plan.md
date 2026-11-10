# Plan — Issue #869

## 技术方案（加法式接线，选 Python 侧时钟统一，规避 mysql_compat 改动）

对照 `docs/clusterDesignDocument/feishu-lease-design.md` UTC_TIMESTAMP CAS 契约，
时钟统一选 **Python 侧 UTC 参数化**（Spec 决策表选项 A 变体）：写入（`_future`）与
比较（`_now`）均由 Python 计算 UTC 串并参数化传入 SQL，替换 `datetime('now')` /
翻译后的 `NOW()`，两后端（SQLite / MySQL）字符串比较语义一致，完全规避
`mysql_compat.py:310` 翻译口径问题（不动该文件）。

### 实现步骤（原子提交）
1. **时钟口径统一**：`bot_lease.try_acquire`（`lease_acquired_at` 与过期比较）与
   `list_expired_or_unheld`（过期比较）改 Python UTC 参数；`renew/batch_renew` 写入
   本已是 `_future`（UTC），无需改。
2. **续约接线**：`Supervisor._scan_once` 开头对 `self._lease_tokens` 调
   `lease_store.batch_renew`（TTL 60s / 扫描 30s = 2 倍余量）；返回 dict 中值为
   False（被抢/失主）→ 走抽取出的 `_yield_channel`（优雅让渡：cancel + disconnect
   + release），复用环迁移路径，不引入新路径。store 异常/口径不识别 → 本轮跳过，
   TTL 余量兜底。
3. **分片接线**：`LegacyCompatLeaseStore` 补 `list_active_instances` 透传；
   `_scan_once` 开头 `_refresh_instances()`：活跃集 ∪ {self}（自身恒在环，实例必然
   在跑），过滤语义（im_enabled=1 + 心跳上下界，CST 脏行不进环）由
   `BotLeaseStore.list_active_instances` 保证；store 未实现该方法（旧替身/旧装配）
   → 保留现状不动，向后兼容。
4. **测试**：新增 `tests/im/test_lease_reliability.py`。

## 测试计划
- 时钟口径：acquire→renew 不可抢占 + expires_at 前移 + acquired_at≈now（SQLite 真后端）；
  过期旧值可抢；执行 SQL 不含 `datetime('now')`；MySQL 翻译后无 `NOW()`/`UTC_TIMESTAMP`。
- 环：两实例不重叠且并集覆盖；单实例全量；10 分钟前心跳/未来时间/im_enabled=0 不进环；
  掉环实例的安装被接管（其租约随 TTL 释放后）。
- 续约：每轮 batch_renew 传 `{inst: token}`；续约失败（token 易主）→ 拆连接 + 释放。
- 回归：`pytest tests/im`（基线 103）+ feishu_async / bot_manager_load / chat_config_im_preserve。

## 风险
- 旧 UTC/CST 混写历史行：只改写入/比较逻辑，不做迁移，随 TTL 自然失效；
- renew 失败处理：走既有优雅让渡路径，无新路径；
- 向后兼容：lease_store 无 list_active_instances 时行为不变（现有 FakeLeaseStore 测试仍绿）。
