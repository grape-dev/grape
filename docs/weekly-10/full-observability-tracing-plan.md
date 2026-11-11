# Grape 全链路可观测方案（LGTM 完整版）

（2026-11-05 · 目标：Metrics + Logs + **Traces** 三支柱完整覆盖 grape dev 全系统；代码侵入已获授权）

## 0. 现状与差距

| 支柱 | 现状 | 差距 |
|---|---|---|
| Metrics | ✅ 双主已接入（P0/P1 完成，8 targets + 11 告警） | worker 侧进程级指标未采（引擎心跳在 DB 采样里，粗粒度） |
| Logs | ✅ 双主 JSON 日志 → Loki | worker 日志未接（journald + 本地文件）；**无 trace_id 关联** |
| Traces | ❌ **零**——Tempo OTLP 端口开着（4317/4318）但无人推送 | 全部：API span、任务生命周期 span、引擎执行 span、A2A/事件 span、日志关联 |
| 依赖中间件 | MySQL（外部 RDS）、OBS、GitCode、飞书 | 中间件客户端 span/指标（慢查询、外部 API 延迟） |

**贯穿主键（本方案的地基）**：grape 已有确定性 trace 体系 `trace_id = tr_<sha1(task_id)[:16]>`、`pipeline_trace_id = tr_<sha1(repo_key|issue_id)[:16]>`（trace.py）。方案不做第二套 id——**把这套 id 升级为 OTel trace context 的业务标签**，与平台驾驶舱（DB 重建视图）天然对齐。

## 1. 埋点拓扑（谁发什么）

```
【入口层】                                    【执行层】
A 进程 (grape_server :8081)                   workers 0002-0005 (runtime-agent)
B 进程 (grape_event_server :8082)             └─ 每任务: engine 进程 (hermes/opencode/
C 进程 (grape_worker_server :8083)               codearts/hwcloud 子进程)
webhook (:8080)                               DB: MySQL(59) · OBS · GitCode API · 飞书
     │                                              │
     ▼ OTel SDK (FastAPI middleware)                ▼ OTel SDK (手动 span)
   HTTP span ──► Tempo :4317 ◄── 任务/引擎/外部调用 span
     │                                              │
     └── 日志注入 trace_id ──► promtail ──► Loki ◄──┘ (TraceID 字段关联 = Grafana 互跳)
```

## 2. 分四步落地（每步独立可用，风险递增）

### Step 1 · OTel 基座 + HTTP spans（A/B/C/webhook 四入口）——「先看见流量」

- 依赖：`opentelemetry-sdk` + `opentelemetry-exporter-otlp` + `opentelemetry-instrumentation-fastapi`（进 requirements，pip 装）
- 新增 `src/observability/otel.py`（单一初始化源，A/B/C/webhook 四入口 import）：
  - `OTEL_EXPORTER_OTLP_ENDPOINT=http://192.168.0.39:4317`（grape.env 注入，grape-skills 生产拓扑同款 env 切换）
  - **Sampler：父头部优先（tracecontext 透传）+ 尾部采样策略**（healthy 基础上 1/10 抽样、error 全采、慢请求全采——防 Tempo 14 天盘打爆；先从固定 10% 起步）
  - Resource：`service.name=grape-server-{a|b|c|webhook}`、`service.version`、`host`
- FastAPI instrumentation：自动 HTTP span（method/route/status/duration），**剔除 health/metrics 路径**
- 验证：Grafana Explore → Tempo 出现 `grape-server-a` 服务；红点（错误率）/延迟直方图可用

### Step 2 · 任务生命周期 spans（主链路，最重要）——「看见一个任务的一生」

核心：**task_id → OTel Trace 的映射**用现有确定性 id 体系（trace.py），在生命周期各环节创建带属性的 span，并跨进程传递：

```
enqueue_task(A) ──创建 pipeline_trace root（trace_id=tr_sha1(task_id), 属性:
   task_id/agent_type/skill/repo_key/issue_id/runtime_id）
   └─ W3C tracecontext 注入任务行（agent_tasks 新列 trace_context JSON，或 env 传递）
claim/claim_by_tag(C) ──span "task.claim"（等待时长=created_at→claimed）
worker _execute_task ──span "task.execute"（引擎名/退出码/时长/重试）
   ├─ span "engine.spawn"（Popen 段，~230 行处）+ "engine.stream"（事件流消费）
   ├─ span "skill.materialize"（OBS 拉包） / "repo.worktree"（关联仓 worktree）
   └─ 外部调用 span："gitcode.api" / "obs.request" / "feishu.send"（薄封装现有客户端）
complete_task/fail(C) ──span "task.finish"（结果/exit_code/usage tokens）
```

- 传递方式（不引消息队列）：任务行带 `trace_context`（W3C traceparent 字符串），worker 侧 resume 成同一 trace 的子 span——**A→C→worker 全链一条 trace**
- A2A/事件链：event_pipeline 处理事件时把 `pipeline_trace_id` 作为 span 属性——Grafana 里按 issue 聚出 vod→coding→qa 的流水线视图（Search tag: `pipeline_trace_id`）
- 验证：任意任务在 Tempo 搜索 `tr_xxx` 出完整瀑布（enqueue→claim→execute→engine→finish）

### Step 3 · 日志关联（logs↔traces 互跳激活）——「排障闭环」

- `config.py` JSON formatter 增加 `trace_id`/`span_id` 字段：OTel context 有值时自动注入（`trace.get_current_span()`）；无 context 时回填业务 trace_id（task_id 可算时）
- 效果：lgtm.yaml 里**预配好的** Loki TraceID derivedField 立即生效——Grafana 日志行点 TraceID 直跳 Tempo 瀑布，Tempo 瀑布点日志回跳 Loki（15 分钟排障闭环）
- 顺带：worker 的 journald 日志补 promtail 的 systemd scrape（unit 过滤 runtime-agent）→ worker 侧日志也进 Loki（标签 host/engine）

### Step 4 · 补齐盲区 + 治理

| 盲区 | 手段 |
|---|---|
| worker 进程自身指标 | runtime-agent 挂 metrics server（METRICS_ADDR 已有机制，端口 9101 各 worker 唯一化）+ obs_loop 健康指标 → Prometheus job grape-dev-workers |
| MySQL 深度 | mysqld_exporter（59 主机；慢查询/InnoDB/连接池）——已列 P1 未做 |
| 事件链路完整性 | patrol（A2A 巡检）findings 指标化 + 告警（已有表，补 exporter） |
| Tempo 容量 | 尾部采样调参 + 14d 保留复核；观测 Tempo 自身磁盘（node_exporter 已覆盖 0004） |

## 3. Grafana 终态看板（三张）

1. **Grape Dev — 服务与业务监控**（已有，扩 worker targets）
2. **Grape Dev — 任务全链追踪**：按 service 拓扑图（A→C→worker→engine）、按 pipeline_trace_id 列 issue 流水线、错误任务 trace 直达
3. **Grape Dev — 排障工作台**：Loki 日志（按 host/component/trace_id 过滤）+ 右侧 Tempo 关联面板

## 4. 工作量与顺序（建议两个 MR）

| MR | 内容 | 规模 | 风险 |
|---|---|---|---|
| MR-1（Step 1+3） | OTel 基座 + HTTP span + 日志 trace_id 注入 + worker 日志进 Loki | ~300 行（新模块 otel.py + formatter + env + requirements） | 低：SDK 中间件旁路，失败不致命（try/except 全包） |
| MR-2（Step 2） | 任务生命周期 spans + trace_context 传递 + 外部调用薄封装 | ~600 行（service.py/agent.py/runtime.py 多点） | 中：触核心路径，需按 hunk 审 + 灰度（先 dev 全量，验证后再推 prod） |

Step 4（worker metrics + mysqld_exporter）为运维项不进 MR。

## 5. 风险与取舍（明确列出）

1. **性能开销**：OTel 中间件 + span 每请求 ~百微秒级、内存 +30MB/进程；以 dev 流量（<10 QPS）可忽略；采样兜底
2. **trace_context 新列**：agent_tasks 加一列（MySQL DDL，走 db_setup 迁移）；历史任务无 context → span 从 claim 起始（trace.py 的 DB 重建视图仍覆盖历史）
3. **worker 出网到 4317**：实测 0002-0005 → 0004 同 VPC 可达；若个别节点不通，降级为 worker 日志带 trace_id（Step 3 的效果仍在）
4. **Tempo 磁盘**：10% 采样 + 14d 保留，预估 <5GB，0004 磁盘（60% 用）够用；告警规则已含 NodeDiskHigh
5. **多入口进程独立性**：A/B/C/webhook 各自 service.name 区分；B/C 被调场景（nginx 分流）上下文通过 HTTP header 天然透传

## 6. 验收口径

- [ ] Tempo 中 `service.name` 维度出现 4 个入口 + N 个 worker
- [ ] 任取一个完成任务：Tempo 按 task_id/trace_id 搜索出 ≥4 层瀑布（enqueue→claim→execute→engine）
- [ ] Grafana 日志行出现 TraceID 蓝链，点击跳 Tempo 瀑布成功
- [ ] issue 流水线视图：按 pipeline_trace_id 聚合能看到 vod→coding→qa 串联
- [ ] 断网演练：停 Tempo → 业务无感（exporter 异步队列丢弃，零阻塞）；恢复后新 trace 续传

## 7. 调用链监控使用手册（2026-11-05，MR-3 后）

### 7.1 入口
- Grafana → 「Grape Dev — 任务追踪与排障」看板 → 「任务瀑布入口」→ 点 Tempo 链接（Explore）
- 或直接: http://192.168.0.39:3000/explore → 左上数据源选 Tempo

### 7.2 查某个任务的调用链（最常用）
1. 平台任务详情页拿到 task_id（如 t_2020735a54c3）
2. Explore 搜索框输入: `{span.task_id = "t_2020735a54c3"}`
3. 结果点任意一条 → 瀑布图

### 7.3 瀑布里看什么（三层结构）
```
task t_xxx issue enqueue   [grape-server-a]  5ms      ← 入队时刻
 └ task t_xxx execute ...  [grape-task]      78841ms  ← 引擎干活（黑盒已展开，见 7.4）
 └ task t_xxx finish       [grape-server-c]  0ms      ← 终态（exit_code/outcome 属性）
```
- enqueue→execute 起点空隙大 = 排队久（引擎忙/没认领）
- execute 占大头 = 正常（引擎在跑）
- finish 的 outcome=failed → 看属性里的 error_head

### 7.4 MR-3 后的新层次（execute 展开）
execute 期间发生：
- `vcs gitcode.com /repos/...` —— 每次 GitCode/GitHub/GitLab API 调用（建 issue/评论/PR），
  带 vcs.method / http.status_code / vcs.result
- `obs put <key>` —— 附件/文件上传 OBS（带 obs.size）
- `engine <backend>` —— 引擎子进程全程（backend/session/exit_code/text_len/parsed_count）

### 7.5 常用 TraceQL
- `{span.name = "task.finish" && span.outcome = "failed"}` —— 所有失败任务
- `{span.task_id = "t_xxx"}` —— 指定任务
- `{span.dep.kind = "vcs" && span.vcs.result = "http_error"}` —— 所有失败的 VCS 调用

### 7.6 日志 ↔ 瀑布互跳
- Loki 日志行点 TraceID → 瀑布
- 瀑布 span 详情页 "Logs for this span" → 该时间窗日志

### 7.7 注意
- 采样 100%（2026-11-05 起），所有请求都有 trace
- Tempo 查询侧索引有 ~1 分钟延迟：刚发生的调用立刻按 trace_id 直查可能查不到，稍等再查
- worker 侧 execute span 走主控中继（MR-2b），只在任务派到远程 worker 时出现
