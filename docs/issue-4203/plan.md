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
  `max`/巨大值（v1 占位）视为无限额。**混合层级**下 `0::` 存在但 v2 解析为 0
  （memory.max 缺失或 `max`）时继续回退 v1 再判定 unbounded——否则限额在 v1 的机器
  会被误报 unbounded、ratio 告警永不触发。

## 2. 启动自检（grape_worker_server lifespan 首段）

`log_effective_memory_limit()`：读取自身 cgroup 生效限额并 `log.info` 打印
（限额值 + cgroup 路径 + 进程角色）；无限额时打 WARNING。杜绝「限额来源不可见」。

## 3. systemd 单元与托管路径（两条都要覆盖）

静态单元 `deploy/systemd/grape-worker-server.service`：
- `MemoryAccounting=yes`、`MemoryHigh=6G`、`MemoryMax=8G`（16G 主机保守默认，
  注释标注：待 004 dmesg victim 行与实测 RSS 峰值校准，SRE 证据到位后按 host 覆盖）；
- `RestartSec=5→1`，`RestartSteps=5`、`RestartMaxDelaySec=30`（指数退避上限 30s）；
- `ExecStart` 改走 `scripts/start-worker.py` 预 bind launcher（见 §4），监听在
  lifespan 之前就绪 → systemd 重启后窗口从「RestartSec + lifespan 初始化」收敛到秒级。

模板单元 `grape@worker.service`（生产实际形态，ExecStart = `scripts/grape-systemd-run.sh`）：
- `scripts/grape-systemd-run.sh` 的 worker 分支改 exec `scripts/start-worker.py`
  （导出 `GRAPE_WORKER_PORT`/`GRAPE_HOST`/`GRAPE_UVICORN_WORKERS=1` 保持单进程生产语义），
  其它角色仍 exec uvicorn → 预 bind 覆盖到真实启动路径；
- 端口以角色映射（`$port`）为唯一事实源：`GRAPE_WORKER_PORT` 与 `$port` 不一致时
  脚本 fail-fast（exit 6，打印来源），避免 EnvironmentFile 旧值（#3764 前的 38082）
  让 worker 监听 A 端口却按 B 端口向 GSB 注册；
- 脚本对所有角色导出 `GRAPE_PROCESS_ROLE="$role"`：该变量是进程角色唯一来源
  （`table_ownership`/`gsb.caller_domain`/`migration` 守卫均按键映射），
  worker app 的 `setdefault(..., "runtime")` 因此不再把 process 标签写成 runtime；
- 新增 `deploy/systemd/grape-worker-oom.conf`（drop-in 源文件）：MemoryAccounting /
  MemoryHigh=6G / MemoryMax=8G / RestartSec=1 / RestartSteps=5 / RestartMaxDelaySec=30；
  `scripts/install-grape-systemd.sh` 在 worker 角色安装时自动落到
  `/etc/systemd/system/grape@worker.service.d/30-worker-oom.conf`（daemon-reload 已在流程内）；
- 两条路径的字面值由单测断言相等，防其一被改后静默漂移。

## 4. launcher 预 bind（scripts/start-worker.py 新增）

`socket.bind() + listen()` 后把**已 listen 的 socket 对象**交给 uvicorn：
- uvicorn 0.54 的 `--fd` 只适用于 AF_UNIX 包好的 fd，TCP fd 实测报
  `OSError: [Errno 88] Socket operation on non-socket`，故不用 `os.execv` + `--fd`，
  而是对齐 `uvicorn.main.run` 的分支：`workers>1` → `Multiprocess`，`workers==1` →
  `uvicorn.Server(config).run(sockets=[sock])`（uvicorn 自身 bind 之后做的就是这件事，
  单/多 worker 行为一致）；
- 多 worker 构造按 uvicorn 版本自适应（依赖声明是 `uvicorn>=0.23` 未锁版本）：
  ≥0.51 用 `Multiprocess(config, sockets=[sock])`；0.23–0.50 的签名仍是
  `Multiprocess(config, target, sockets)`，缺 `target` 会 `TypeError` 崩溃循环。
  `build_multiprocess()` 用 `inspect.signature` 判定并在 `TypeError` 时按旧签名补
  `target=Server(config).run`，两种签名均有单测；
- 端口/host/workers 从 `GRAPE_WORKER_PORT`/`GRAPE_HOST`/`GRAPE_UVICORN_WORKERS` 读取，
  缺省对齐单元（`0.0.0.0:38083`，#3764 端口事实源；mgmt/event/worker/insight =
  38081/38082/38083/38084；workers=2；`GRAPE_WORKER_APP` 可覆盖 ASGI 导入串）；
- bind 失败显式退出非 0（systemd Restart 拉起），不静默换端口。

## 5. 告警（deploy/alerts.yml）

- `GrapeWorkerOOMKilled`（critical）：`increase(node_vmstat_oom_kill[1h]) > 0` —— P1，
  注释说明 victim/host 从 node_exporter + dmesg 取证；
- `GrapeProcessMemoryNearLimit`（warning）：`grape_process_memory_ratio{process="worker"} > 0.8` 持续 10m；
- `GrapeWorkerRSSGrowth`（warning）：30m 内 RSS 增量 > 阈值（`deriv` 为正且 delta 超 512MiB，
  两个子查询均按 `process="worker"` 过滤，避免 mgmt/event/insight 触发时告警名撒谎）。

## 6. 消息回执落盘（src/domains/runtime/engine/agent.py）

- 网络错误连续 N 个 flush 窗口（`GRAPE_WORKER_MSG_FLUSH_PERSIST_AFTER`，默认 3，
  非法/空值安全回落 3）仍失败 → 批次截断后**批量**落 `WorkerReportQueue`
  （`add_many` 一次 flush 一次 jsonl 重写，写放在 `asyncio.to_thread`，不阻塞事件循环），
  全部落盘才清空内存 batch 返回 "ok"；**落盘未全部成功返回 "retry" 保留 batch**
  （`WorkerReportQueue.add/_flush` 返回落盘结果，`add_many` 失败回滚并返回 0），
  outbox 重放带退避（10s 循环 + TTL 24h）。
- 终态回执先落盘再发的路径已存在（`_do_report`：`q.add` → `client.complete/fail` → ack 出队），
  本单不加重复机制。
- 幂等：终态按 run_token（= started_at，即 attempt_epoch）服务端 409 守卫（既有）；
  messages 服务端按 seq 预约回执 + 终态 410 守卫（既有）。

## 7. server_tasks 15s 循环复用

`_server_metrics_loop`（insight 进程，15s）追加一次 `collect_process_memory()`：
无 scrape 时指标也按 15s 刷新（与 BusinessSampler 的 8s TTL 缓存互补）。

## 8. 测试（4 个文件全部接入 CI：`.github/workflows/lint.yml`）

- `tests/insight/test_process_memory_metrics.py`：v2/v1/无限额（`max` 占位与无文件）
  三分支 RSS/limit/ratio/unbounded 采样；**混合层级 v2 为 0 时回退 v1**；engine child
  聚合与消失归零；异常文件不抛；
- `tests/runtime/test_issue_4203_msg_outbox.py`：网络错误连续失败落 outbox、首个窗口
  仍走内存重试、成功后清零计数；**落盘失败返回 retry 且 batch 保留**；`add_many`
  单次 flush 与失败回滚；
- `tests/deploy/test_issue_4203_deploy_config.py`：静态单元含 Memory* / Restart* 且
  ExecStart 指向 launcher；**`grape@worker` 路径覆盖**（`grape-systemd-run.sh` worker
  分支 exec launcher、drop-in 参数与静态单元字面值相等、安装脚本会装 drop-in）；
  alerts.yml 三条规则与指标名一致；
- `tests/scripts/test_issue_4203_start_worker.py`：bind/listen/端口冲突、spawn 子进程
  重跑文件不 bind（真子进程断言）、缺省端口 38083/单进程语义。
