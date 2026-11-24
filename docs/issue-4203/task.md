# Issue #4203 — 任务拆解（task）

| # | 切片 | 改动 | 验收 | 提交 |
|---|------|------|------|------|
| T1 | 进程内存指标 | `observability/process_memory.py`（5 gauge + install + collect + log_effective_memory_limit）；`__init__.py` 注册 + on_metrics_scrape 采样；server_tasks 15s 循环复用 | v2/v1/无限额分支采样单测全绿；/metrics 序列可读 | feat: #4203 process memory gauges |
| T2 | 启动自检 | `grape_worker_server.py` lifespan 首段打印生效限额 | 日志含 cgroup 限额与路径；无限额 WARNING | feat: #4203 startup memory limit selfcheck |
| T3 | systemd + launcher | 静态单元 MemoryAccounting/High/Max + RestartSec=1/Steps/MaxDelay；`scripts/start-worker.py` 预 bind（Multiprocess/sockets，非 exec+--fd）；静态单元 ExecStart 改 launcher；`grape-systemd-run.sh` worker 分支 exec launcher；`deploy/systemd/grape-worker-oom.conf` drop-in + 安装脚本落地 | 部署配置断言单测（两条路径 + 参数一致）；launcher bind 单测 | feat: #4203 systemd memory cap and prebind launcher |
| T4 | 告警 | alerts.yml 三条规则 | 规则名/指标名一致性单测 | feat: #4203 oom and memory alerts |
| T5 | 消息 outbox | agent.py flush 网络错误连续失败 → `add_many` 批量落盘（to_thread）+ 落盘不全返回 retry；`WorkerReportQueue.add/_flush/add_many` 返回落盘结果 | outbox 落盘/失败保 batch/幂等重放单测 | feat: #4203 persist undelivered message batches |
| T6 | 回归 + CI | 全部新测试接入 `.github/workflows/lint.yml` + 相关存量测试 | CI 实际执行并绿；无新增失败 | feat: #4203 wire regression tests into CI |

执行顺序 T1→T6，每片红→绿。完成定义：验收口径对应测试断言齐全，自审查 + pre-pr 检查通过后建 PR。
