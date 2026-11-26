# Tasks: Issue #4445

- [x] T1 `cleanup.py` janitor 实现 + 挂接 sweeper
- [x] T2 心跳列 + 注册式迁移 + db_setup 登记
- [x] T3 janitor 计数并入 worker 心跳指标快照
- [x] T4 `tmp_usage.py` tmpfs 水位 / janitor 指标
- [x] T5 `alerts.yml` 80% / 90% 告警规则
- [x] T6 派发前 `/tmp` 水位门（engine_select + scheduler）
- [x] T7 单测：清理行为 / 水位门 / 迁移
- [x] T8 建 PR + Issue 评论 + A2A QA
- [x] T9 PR #2303 复审 R1：活跃任务归属识别（`qa-<task_id>` 形态）+ 探测失败 fail-closed + 补测
- [x] T10 PR #2303 复审 R2：dry 模式不累加累计指标、日志改 `would remove` + 补测
- [x] T11 PR #2303 复审 R3：worker 节点指标走「心跳落库 → 控制面采样」`grape_node_tmp_*`，
      告警改挂节点通路；`METRICS_ADDR` 已设但缺 `prometheus_client` 时显式 warning
- [x] T12 PR #2303 复审 Optional：前缀收敛（`base[0-9]*` 等）、env 安全解析与模式白名单、
      派发水位 ratios 一次预取复用、未持集群锁节点照跑本机 janitor