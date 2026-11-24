# Issue #4203 — 方案设计（plan）

## 1. 进程内存采集（src/domains/insight/observability/process_memory.py 新增）

独立 registry（沿用 `observability.__init__.registry`）新增 5 个 gauge：

| 指标 | 标签 | 语义 |
|------|------|------|
| `grape_process_memory_rss_bytes` | process | `/proc/self/status VmRSS` |
| `grape_process_memory_limit_bytes` | process | cgroup v2 `memory.max` / v1 `memory.limit_in_bytes`，无限额上报 0 |
| `grape_process_memory_ratio` | process | rss/limit；无限额 0 |
| `grape_process_memory_limit_unbounded` | process | 无 cgroup 限额 = 1 |
| `grape_engine_child_rss_bytes` | process, engine | 本进程活跃引擎子进程（`agents/executor._active_procs`）按 backend 聚合的 RSS |

- 采样函数 `collect_process_memory()`：`on_metrics_scrape` 钩子调用（拉取时采样），
  复用既有 METRICS_ADDR 链路（Alloy 15s 抓取），无新链路；任何异常吞掉不拖垮 /metrics。
- `install(registry)`：与 kernel metrics 相同的安装模式，由 `observability.__init__` 注册。
- process 标签取 `GRAPE_PROCESS_ROLE`（worker/runtime/event/insight…），缺省 `server`。
- cgroup 路径解析：读 `/proc/self/cgroup` 取相对路径，优先 v2
  `/sys/fs/cgroup/<path>/memory.max`，回退 v1 `/sys/fs/cgroup/memory/<path>/memory.limit_in_bytes`；
  `max`/巨大值（v1 占位）视为无限额。

## 2. 启动自检（grape_worker_server lifespan 首段）

`log_effective_memory_limit()`：读取自身 cgroup 生效限额并 `log.info` 打印
（限额值 + cgroup 路径 + 进程角色）；无限额时打 WARNING。杜绝「限额来源不可见」。

## 3. systemd 单元（deploy/systemd/grape-worker-server.service）

- `MemoryAccounting=yes`、`MemoryHigh=6G`、`MemoryMax=8G`（16G 主机保守默认，
  注释标注：待 004 dmesg victim 行与实测 RSS 峰值校准，SRE 证据到位后按 host 覆盖）；
- `RestartSec=5→1`，`RestartSteps=5`、`RestartMaxDelaySec=30`（指数退避上限 30s）；
- `ExecStart` 改走 `scripts/start-worker.py` 预 bind launcher（见 §4），监听在
  lifespan 之前就绪 → systemd 重启后窗口从「RestartSec + lifespan 初始化」收敛到秒级。

## 4. launcher 预 bind（scripts/start-worker.py 新增）

`socket.bind() + listen()` 后 `os.execv` uvicorn 并传 `--fd`：
- 先 bind 再 exec，uvicorn 直接在既有 fd 上 accept —— 单/多 worker 行为一致；
- 端口/host/workers 从 `GRAPE_WORKER_PORT`/`GRAPE_HOST`/`GRAPE_UVICORN_WORKERS` 读取，
  缺省对齐单元（0.0.0.0:38082, workers=2）；
- bind 失败显式退出非 0（systemd Restart 拉起），不静默换端口。

## 5. 告警（deploy/alerts.yml）

- `GrapeWorkerOOMKilled`（critical）：`increase(node_vmstat_oom_kill[1h]) > 0` —— P1，
  注释说明 victim/host 从 node_exporter + dmesg 取证；
- `GrapeProcessMemoryNearLimit`（warning）：`grape_process_memory_ratio > 0.8` 持续 10m；
- `GrapeWorkerRSSGrowth`（warning）：30m 内 RSS 增量 > 阈值（`deriv` 为正且 delta 超 512MiB）。

## 6. 消息回执落盘（src/domains/runtime/engine/agent.py）

- 网络错误连续 N 个 flush 窗口（`GRAPE_WORKER_MSG_FLUSH_PERSIST_AFTER`，默认 3）仍失败
  → 批次逐条截断后落 `WorkerReportQueue`（kind=messages，#975 既有重放链路续投），
  清空内存 batch 返回 "ok"；outbox 重放带退避（10s 循环 + TTL 24h）。
- 终态回执先落盘再发的路径已存在（`_do_report`：`q.add` → `client.complete/fail` → ack 出队），
  本单不加重复机制。
- 幂等：终态按 run_token（= started_at，即 attempt_epoch）服务端 409 守卫（既有）；
  messages 服务端按 seq 预约回执 + 终态 410 守卫（既有）。

## 7. server_tasks 15s 循环复用

`_server_metrics_loop`（insight 进程，15s）追加一次 `collect_process_memory()`：
无 scrape 时指标也按 15s 刷新（与 BusinessSampler 的 8s TTL 缓存互补）。

## 8. 测试

- `tests/insight/test_process_memory_metrics.py`：v2/v1/无限额（max 占位与无文件）
  三分支 RSS/limit/ratio/unbounded 采样；engine child 聚合；异常文件不抛；
- `tests/runtime/test_issue_4203_msg_outbox.py`：网络错误连续失败落 outbox、
  首个窗口仍走内存重试、成功后不清 outbox 已有项；
- `tests/deploy/test_issue_4203_deploy_config.py`：单元文件含 Memory* / Restart* 指令且
  ExecStart 指向 launcher；alerts.yml 三条规则与指标名一致；
- `tests/scripts/test_issue_4203_start_worker.py`：launcher 参数构造与 fd 传递（端口 0 免冲突）。
