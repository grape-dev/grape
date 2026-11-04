# 方案：Layer 2 — C 进程内置 trace 中继（worker trace 经主控转发）

（2026-11-05 · 直接 Layer 2，跳过 nginx 临时方案；生产 LGTM 机器不对外）

## 0. 架构

```
worker (0002-0005, 生产含外部 worker)
   │  OTLP/HTTP exporter
   │  POST {RUNTIME_SERVER_URL}/api/runtime/traces        ← 既有 worker↔主控通道，Bearer RUNTIME_TOKEN
   ▼
主控 C 进程 (grape_worker_server :8083)
   │  @router.post("/runtime/traces") + _require_credential 鉴权
   │  httpx 透传 body → OTLP_RELAY_TARGET/v1/traces
   ▼
LGTM 机器 OTLP/HTTP :4318                                   ← dev=192.168.0.39:4318；生产=额外那台，仅主控可达
```

worker 侧零新网络路径、零新增机器地址知识（从 `RUNTIME_SERVER_URL` 推导）；LGTM 机器只对主控开放。

## 1. 主控侧改动（C 进程，~40 行）

### 1.1 中继端点（`src/api/routes/runtime.py` 末尾新增）

```python
# ===== MR-2b（2026-11-05）：worker trace 中继（LGTM 不对 worker 开放，经主控转发）=====
OTLP_RELAY_TARGET = os.environ.get("OTLP_RELAY_TARGET", "").strip()   # 例: http://192.168.0.39:4318

_relay_total = Counter("grape_trace_relay_requests_total", "Trace relay requests forwarded", ["status"], registry=registry)

@router.post("/runtime/traces", operation_id="runtime_traces_relay")
async def runtime_traces_relay(request: Request, credential_id: int = Depends(_require_credential)):
    """OTLP/HTTP trace 中继：透传 worker 的 ExportTraceServiceRequest 给 LGTM。

    - 鉴权：复用 worker 协议的 Bearer token（_require_credential，与 claim 同款）
    - 透传：request.body() 原样转发（protobuf 或 json 由 Content-Type 区分，均无解析）
    - 失败：向上透传 LGTM 的状态码；LGTM 不可达 → 502，worker exporter 异步重试/丢弃，
      业务零影响（与现有断网降级语义一致）
    """
    if not OTLP_RELAY_TARGET:
        return Response(status_code=503, content='{"detail":"trace relay disabled"}',
                        media_type="application/json")
    body = await request.body()
    ctype = request.headers.get("content-type", "application/x-protobuf")
    import httpx
    async with httpx.AsyncClient(timeout=10) as hc:
        try:
            r = await hc.post(f"{OTLP_RELAY_TARGET}/v1/traces", content=body,
                              headers={"Content-Type": ctype})
            _relay_total.labels(status=str(r.status_code)).inc()
            return Response(status_code=r.status_code, content=r.content,
                            media_type=r.headers.get("content-type", "application/x-protobuf"))
        except httpx.HTTPError as e:
            _relay_total.labels(status="relay_unreachable").inc()
            return Response(status_code=502, content='{"detail":"lgmt unreachable"}',
                            media_type="application/json")
```

要点：
- **无解析**：body 透传，protobuf/json 都不过手（中继不感知 span 内容，升级零耦合）
- **鉴权**：`_require_credential` 已在 C 进程挂载的 runtime_router 内，worker 的 token 天然可用
- **可观测**：`grape_trace_relay_requests_total{status}` 进既有 `/metrics`（A 进程抓取，Prometheus 已有告警框架可挂"中继持续 5xx"）

### 1.2 主控 env（两台，`/etc/grape/grape.env` 追加）

```ini
# MR-2b：worker trace 中继目标（LGTM OTLP/HTTP，仅主控可达）
OTLP_RELAY_TARGET=http://192.168.0.39:4318
```

（生产：指向那台额外机器的内网 IP:4318）

### 1.3 nginx 分流（两台主控的 `/etc/nginx/conf.d/grape.conf`）

C 进程正则**加一个 `traces`**：
```
location ~ ^/api/runtime/(register|heartbeat|...|converge-event|traces) {
```
然后 `nginx -s reload`。

## 2. worker 侧改动（`src/observability/otel.py`，~30 行）

### 2.1 worker 模式 exporter 分支（init_otel 内）

```python
# MR-2b：worker 进程 → trace 走主控中继（OTLP/HTTP + worker token），不直连 LGTM
def _worker_relay_exporter_kwargs() -> dict | None:
    """worker 进程特征 = RUNTIME_TOKEN 非空。返回 OTLP/HTTP exporter 配置；非 worker → None。"""
    token = os.environ.get("RUNTIME_TOKEN", "").strip()
    server = os.environ.get("RUNTIME_SERVER_URL", "").strip()
    if not (token and server):
        return None
    return {
        "endpoint": server.rstrip("/") + "/api/runtime/traces",
        "headers": {"Authorization": f"Bearer {token}"},
        "timeout": 5,
    }
```

`init_otel()` 里 exporter 构造分支：
- worker（`_worker_relay_exporter_kwargs()` 非 None）→ `OTLPSpanExporter(**kwargs)`（HTTP 版）
- 主控/CLI → 维持现状 gRPC 直连 `OTEL_EXPORTER_OTLP_ENDPOINT`

**注意**：worker 不再依赖 `OTEL_EXPORTER_OTLP_ENDPOINT`（env 可以不给）；worker 的 trace 采样独立（worker env `OTEL_SAMPLER_RATIO`，可比主控高——任务级 span 价值大）。

### 2.2 依赖

worker 包 requirements-worker.txt + 主控 requirements：加 `opentelemetry-exporter-otlp-proto-http`（现有三个包之外的新增，pip 已装于 dev venv 验证）。

## 3. 生产形态（rollout 时）

| 组件 | 配置 |
|---|---|
| LGTM 额外机器 | 安全组仅放行**两台主控内网 IP** → 4318；公网全关 |
| 主控 ×2 | `OTLP_RELAY_TARGET=http://<LGTM内网>:4318`；nginx 加 traces 分流 |
| 内部 worker ×13 | 无新 env（RUNTIME_TOKEN/RUNTIME_SERVER_URL 本来就有） |
| 外部用户 worker | 同上——只要能连主控（本来就要连），trace 自动可达 |

## 4. 风险与取舍

1. **中继吞吐**：每 worker 每 5s 一个 batch（BatchSpanProcessor 默认），~KB 级——C 进程 httpx 透传开销微秒级，对比现有 claim/心跳流量可忽略
2. **主控双活**：中继无状态（透传），任一主控可用；ELB/轮询天然分担
3. **LGTM 故障放大面**：中继 502 → worker exporter 丢弃重试（有界队列 2048 span，满则丢——OTel 默认行为），不会反压 worker 业务
4. **鉴权覆盖**：外部 worker 用同一套 token 体系；被吊销的 token 自动失去灌 trace 权限（与 claim 同生共死）
5. **header 透传**：OTLP HTTP 的 `Content-Type`（protobuf/json）透传；`Content-Encoding: gzip`（SDK 默认压缩）原样保留——中继不解压

## 5. 验证口径

1. worker（0002）重启后跑任务 → Tempo 出现 `task.execute` span，`service.name=grape-worker`（resource 用 worker 的 host）
2. 完整瀑布：enqueue(A)→claim(C)→execute(worker)→finish(C) **同一 trace_id**（DB trace_context 串联）
3. 假 token POST `/api/runtime/traces` → 401
4. 停 0004 Tempo → worker 任务正常完成；relay Counter 出现 `relay_unreachable`
5. `grape_trace_relay_requests_total{status="200"}` 在 Grafana 大盘可见

## 6. 实施清单（本机可全部完成）

- [ ] runtime.py：中继端点 + Counter（~40 行）
- [ ] otel.py：worker exporter 分支（~30 行）
- [ ] requirements-worker.txt：+otlp-http 包
- [ ] 主控 env（0007/0001）+ nginx 分流 + reload
- [ ] worker drop-in env（OTEL_SAMPLER_RATIO）
- [ ] E2E 验证（上节 5 条）
