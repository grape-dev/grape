# Issue #3773 任务拆解

- [x] 取证 HERMES_HOME 模式并记录清理范围（共享单文件 → 单库；隔离 → 遍历各 state.db）
- [x] 新增 `state_db_maintenance.py`：完整性校验、备份、按保留窗口裁剪、VACUUM、失败回滚、文件锁、节流、归档重建
- [x] 升级 `context_compaction.check_state_db_size()`：结构化结果 + 触发清理
- [x] `hermes_acp.py`：清理成功继续 resume；清理失败/仍超阈值才 `session/new`，跳过时打 `ACP_RESUME_SKIPPED`
- [x] 指标：worker 心跳上报 + `/metrics` 采样 + 四项指标
- [x] 运维脚本 `scripts/ops/hermes_acp_state_db_cleanup.py`（幂等、dry-run、备份、校验）
- [x] `deploy/alerts.yml` 告警规则
- [x] 测试：保留窗口、最少保留、dry-run、失败回滚、节流、归档、发现多库、指标、脚本幂等