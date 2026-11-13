# Task — Issue #869

- [x] T1 时钟口径统一：bot_lease.try_acquire / list_expired_or_unheld 改 Python UTC 参数（`src/im/bot_lease.py`）
- [x] T2 续约接线：Supervisor._scan_once → _renew_held_leases（batch_renew + 失败优雅让渡 _yield_channel）（`src/im/engine/supervisor.py`）
- [x] T3 分片接线：LegacyCompatLeaseStore.list_active_instances 透传 + _refresh_instances（`src/im/engine_wiring.py` / `supervisor.py`）
- [x] T4 测试：tests/im/test_lease_reliability.py（时钟口径 4 例 + MySQL 翻译 1 例 + 环/续约 5 例）
- [x] T5 回归：pytest tests/im 113 passed（基线 103，全绿）；feishu_async/bot_manager_load/chat_config_im_preserve 186 passed
- [x] T6 mysql_compat.py：无需改动（Python 侧统一时钟已规避翻译口径问题）
