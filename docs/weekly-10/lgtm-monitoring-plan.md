# Grape dev 环境 LGTM 监控接入方案

（2026-11-04 · 基于实测盘点，非推测）

## 0. 现状盘点（全部实测确认）

| 项 | 状态 | 说明 |
|---|---|---|
| LGTM 栈 | ✅ 已就绪 | build-004（<lan-ip>）上 Prometheus 9090 / Loki 3100 / Tempo 3200 / Grafana 3000 全部存活，从 dev 各节点可达（0007 实测 curl 全通） |
| Grafana 数据源 | ✅ 已预配 | lgtm.yaml 已含 Loki↔Tempo 关联（TraceID 自动跳转）；Prometheus 默认源 |
| Prometheus 抓取 | ⚠️ 只抓自身 | 5 个 job 全是 LGTM 自身（prometheus/grafana/loki/tempo 自监控） |
| **grape 侧埋点** | ✅ **代码已全部就绪，只差开关** | `src/observability/` 自带完整 Prometheus registry：HTTP 请求量/时延/in-flight、后台循环健康（runs/failures/consecutive）、MySQL SHOW STATUS（连接数/线程/中止连接）、业务采样（任务 queued/running/stuck、引擎 online 数）——设 `METRICS_ADDR` 即启用，scrape 时触发 DB 采样 |
| grape 日志 | ✅ JSON 结构化 | `data/logs/devops-bot.log`（按天轮转保留 30 天），字段 ts/level/component/request_id/repo_key/user_id——天然适合 Loki |
| grape 追踪 | ℹ️ 无 OTel | trace.py 是 DB 重建的任务时间线（不依赖写入方配合），Tempo 接入属第二阶段，非必需 |
| 0004 promtail / 各节点 node_exporter | ❌ 未装 | dev 节点无任何 exporter/promtail |

**核心结论：grape 平台自身不需要写一行新代码**——埋点、JSON 日志、DB 采样全都现成，缺的只是「配置接线」。

## 1. 方案总览（三段，按需取用）

```
┌───────────────────────── dev 集群 ─────────────────────────┐
│ 0007(A/B/C 三进程) 0001(A/B/C)  workers 0002-0005          │
│   │ METRICS_ADDR 开关 → 独立 metrics 端口(:9101)            │
│   │ data/logs/devops-bot.log (JSON)                         │
└───┼──────────────────────┬──────────────────────────────────┘
    │ scrape (Prometheus)  │ tail (promtail → push)
    ▼                      ▼
 build-004 LGTM: Prometheus 9090 · Loki 3100 · Tempo 3200 · Grafana 3000
    ▲
    └─ Grafana 看板（HTTP/业务/DB/循环健康 + 日志检索）
```

## 2. 落地步骤（P0：纯配置，零代码，~1 小时）

### 2.1 开启 grape metrics server（双主 A/B/C 进程 + 可选 worker）

`/etc/grape/grape.env` 追加（0007 和 0001）：
```ini
# Prometheus metrics（src/observability，scrape 时触发 MySQL+业务采样）
METRICS_ADDR=0.0.0.0:9101
```
注意：`start_metrics_server()` 是进程内独立 HTTP server，A/B/C 三个进程各自起会**端口冲突**。两个选法：
- **选法 1（推荐）**：只给 A 进程（grape-server.service）配 `METRICS_ADDR`——A 承担路由+定时任务，HTTP/DB/业务指标都在；B/C 是操作子集，其流量在 nginx 分流层统计（或第二阶段给 B/C 用不同端口 9102/9103）
- 选法 2：systemd unit 分进程注入不同 `Environment=METRICS_ADDR=...:910x`（改 unit 文件，动作稍大）

改完 `systemctl restart grape-server`（0007+0001）。

### 2.2 Prometheus 增加抓取 job

`/opt/prometheus/prometheus.yml`（0004）追加，然后 `curl -X POST http://localhost:9090/-/reload`：
```yaml
  - job_name: grape-dev-masters
    static_configs:
      - targets: ['<lan-ip>:9101', '<lan-ip>:9101']
        labels: {env: dev, role: master, host: '0007'}
      - targets: ['<lan-ip>:9101']
        labels: {env: dev, role: master, host: '0001'}
  - job_name: grape-dev-mysql  # 若后续加 mysqld_exporter（DB 在 <lan-ip>）
    static_configs:
      - targets: ['<待定:9104>']
```
（MySQL exporter 见 P1；grape 自带的 SHOW STATUS 指标先覆盖连接/线程面）

### 2.3 日志接 Loki（二选一）

- **选法 A（推荐，改动小）：promtail 部署到两台主控**（0007/0001），tail `data/logs/devops-bot.log`，push 到 `<lan-ip>:3100`。标签：`job=grape-dev, host=<n>, component`（JSON 已带 component 字段，用 json stage 解析提取 level/component/repo_key）
- 选法 B：Loki 直接 `scrape_config` 拉 promtail 的 static 模式（少装两个组件但网络反向依赖，不推荐）

promtail 最小配置（每台主控 `/opt/promtail/promtail-config.yaml`）：
```yaml
server: {http_listen_port: 9080}
positions: {filename: /data/promtail/positions.yaml}
clients: [{url: http://<lan-ip>:3100/loki/api/v1/push}]
scrape_configs:
  - job_name: grape-dev
    static_configs:
      - targets: [localhost]
        labels: {job: grape-dev, host: '0007', env: dev}
        __path__: /root/grape/data/logs/devops-bot.log
    pipeline_stages:
      - json:
          expressions: {level: level, component: component, request_id: request_id, repo_key: repo_key}
      - labels: {level: '', component: ''}
```

### 2.4 Grafana 看板

三块（手动导入或 provisioning dashboards 目录）：
1. **Grape 服务大盘**：`grape_http_requests_total`(by route/status)、`grape_http_request_duration_seconds` 直方图 p95/p99、`grape_http_in_flight_requests`
2. **业务健康**：SAMPLER_TASK_QUEUED/RUNNING/STUCK、SAMPLER_RUNTIME_ONLINE（按 stuck 阈值联动 #470 配置）、`grape_obs_loop_*`（后台循环 runs/failures/consecutive——循环挂掉一眼可见）
3. **DB 面**：DB_THREADS_CONNECTED/RUNNING、DB_MAX_USED_CONNECTIONS 比率、DB_ABORTED_CONNECTS 增速

## 3. P1（可选增强，后续按需）

| 项 | 做法 | 收益 |
|---|---|---|
| node_exporter | 0001-0007 各装一个（二进制 0 依赖），Prometheus 加 job | 磁盘（**生产 181 磁盘 90% 的教训**）/CPU/内存告警面 |
| mysqld_exporter | DB 侧 <lan-ip> 部署 | InnoDB/慢查询/复制等 DB 深度指标 |
| worker 心跳指标 | worker 心跳数据已在 agent_runtimes 表（BusinessSampler 已出 online 数）；如需 per-engine 明细，后续加独立 exporter | 引擎健康分可视化 |
| 告警规则 | Prometheus rule_files：stuck>0、runtime_online 骤降、obs_loop consecutive_failures>3、DB 连接>80% | 把这次测试发现的「静默故障」类问题兜住 |
| Tempo/OTel | grape trace.py 已有确定性 trace_id（tr_<sha1>），如果日志里带 trace_id 可直接命中 Grafana 的 TraceID 跳转；全链路 OTel 属大改，暂缓 | 日志↔追踪互跳 |

## 4. 改动清单与风险

| 改动 | 风险评估 |
|---|---|
| 0007/0001 grape.env 加 METRICS_ADDR | 低：`start_metrics_server` 独立线程 HTTPServer，不挂业务路径；中间件只在启用时挂载。**注意 A/B/C 端口冲突**（见 2.1 选法 1） |
| Prometheus 加 job + reload | 零风险（--web.enable-lifecycle 已具备，热加载） |
| promtail 装两台主控 | 低：只读 tail + push；positions 文件断点续传 |
| Grafana 看板 | 零风险（只读展示） |
| 数据留存 | Loki 7d / Tempo 14d / Prometheus 自身看配置——比现有「journald 10 天+文件 30 天」窗口短，排障历史查询仍以本地文件为准 |

## 5. 验证口径

1. `curl <lan-ip>:9101/metrics | grep grape_` → 看到 http/obs_loop/sampler/db 指标族
2. Prometheus targets 页两个 master 的 `grape-dev-masters` job = UP
3. Grafana Explore → Loki：`{job="grape-dev"} | json | level=WARNING` 实时出日志
4. 人为触发一次告警条件（如停 A 进程 30s）→ obs_loop consecutive_failures 上升被看板捕获
