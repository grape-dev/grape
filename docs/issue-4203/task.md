# Issue #4203 — 任务拆解（task）

| # | 切片 | 改动 | 验收 | 提交 |
|---|------|------|------|------|
| T1 | 进程内存指标 | `observability/process_memory.py`（5 gauge + install + collect + log_effective_memory_limit）；`__init__.py` 注册 + on_metrics_scrape 采样；server_tasks 15s 循环复用 | v2/v1/无限额分支采样单测全绿；/metrics 序列可读 | feat: #4203 process memory gauges |
| T2 | 启动自检 | `grape_worker_server.py` lifespan 首段打印生效限额 | 日志含 cgroup 限额与路径；无限额 WARNING | feat: #4203 startup memory limit selfcheck |
| T3 | systemd + launcher | 单元 MemoryAccounting/High/Max + RestartSec=1/Steps/MaxDelay；`scripts/start-worker.py` 预 bind fd；ExecStart 改 launcher | 部署配置断言单测；launcher fd 单测 | feat: #4203 systemd memory cap and prebind launcher |
| T4 | 告警 | alerts.yml 三条规则 | 规则名/指标名一致性单测 | feat: #4203 oom and memory alerts |
| T5 | 消息 outbox | agent.py flush 网络错误连续失败落盘 + 退避续投 | outbox 幂等重放单测 | feat: #4203 persist undelivered message batches |
| T6 | 回归 | 全部新测试 + 相关存量测试 | 无新增失败 | — |

执行顺序 T1→T6，每片红→绿。完成定义：验收口径对应测试断言齐全，自审查 + pre-pr 检查通过后建 PR。
