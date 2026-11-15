# 方案：Worker trace 经主控转发（生产 LGTM 单台不开放场景）

（2026-11-05 · 回答「能不能通过主控侧转发」：能，且推荐这么做）

## 0. 思路

worker 不直连 LGTM（0004/生产那台额外机器），而是把 OTLP 上报发给它**本来就每 15 秒都在连**的主控（RUNTIME_SERVER_URL，即 C 进程 8083 / nginx 入口）——主控作为 **trace 中继（relay）**转发给 LGTM 的 OTLP 端口。

网络拓扑变化：

```
现在（worker 直连）:                    改造后（主控中继）:
worker ──────────► 0004:4317 (OTLP)     worker ──► 主控(C进程/nginx) ──► LGTM:4318
                                          （复用既有 worker↔主控通道，零新增网络路径）
```

**生产收益**：
- LGTM 机器完全不对外开放（只在主控可达的内网段），worker（含外部用户 worker）永远不需要它的地址
- LGTM 机器换 IP/迁移只改主控一处 env
- 天然鉴权层：中继端点可以要求 worker 的现有凭据（`_require_credential`，与 claim 同款），防止任意人往 Tempo 灌 trace

## 1. 具体实现（两层，推荐 Layer 2）

### Layer 1（管道打通，先做）：nginx 静态反代

worker 的 OTLP/HTTP（`http://<主控入口>/otlp/v1/traces`）→ nginx `location /otlp/` → `proxy_pass http://<LGTM>:4318/v1/traces`。

- 改动：主控两台 nginx 各加一个 location 块（dev 直接 124.70.93.107 nginx；生产加在公网入口 nginx）
- worker 侧：`OTEL_EXPORTER_OTLP_ENDPOINT=http://<RUNTIME_SERVER_URL 宿主>/otlp`（OTLP HTTP exporter）
- 优点：5 分钟接线、零代码
- 缺点：无鉴权（任何能到主控公网的都能灌）、无观测（不知道转发了多少）

### Layer 2（推荐终态）：C 进程内置 relay 端点 + 复用 worker 鉴权

在 `grape_worker_server.py`（C 进程）加一个端点：

```python
@router.post("/runtime/traces")   # nginx 分流规则加一条 → 8083
async def relay_traces(request: Request, credential_id: int = Depends(_require_credential)):
    body = await request.body()          # OTLP/protobuf 或 JSON 透传
    async with httpx.AsyncClient() as hc:
        r = await hc.post(f"{OTLP_RELAY_TARGET}/v1/traces",   # env: http://<lan-ip>:4318
                          content=body,
                          headers={"Content-Type": request.headers.get("content-type", "application/x-protobuf")})
    return Response(status_code=r.status_code)
```

- worker 侧 OTel SDK 配 **OTLP/HTTP exporter**，endpoint 指向主控（可复用 `RUNTIME_SERVER_URL` 推导，连 env 都不用新增）；Header 带 worker 现有 `Authorization: Bearer <RUNTIME_TOKEN>`（agent.py 里已有）——C 进程 `_require_credential` 验明正身
- 主控侧 env：`OTLP_RELAY_TARGET=http://<LGTM内网IP>:4318`（生产那台额外机器，只有主控可达）
- 中继失败：HTTP 5xx 返回给 worker → worker exporter 异步重试/丢弃，业务零影响（与现有断网降级语义一致）
- 顺带在中继里计一个 Counter（`grape_trace_relay_spans_total`）进现有 metrics——转发量可视化

### worker 侧配套改动（MR-2 增补）

`otel.py` 的 init 增加一个 worker 模式分支：当 `RUNTIME_TOKEN` 存在（worker 进程特征）→ exporter 用 OTLP/HTTP 指向 `{RUNTIME_SERVER_URL}/api/runtime/traces`，并把 token 放进 exporter 的 headers 参数（OTel SDK 原生支持 `headers=` 每请求携带）。

## 2. 验证口径

1. worker 重启后跑一个任务 → Tempo 里该任务 trace 出现 `task.execute` span（service=grape-worker-<host>）
2. 断 LGTM（停 0004 tempo）→ worker 任务照常完成（业务零影响）；中继端点计数停止
3. 拿假 token POST `/api/runtime/traces` → 401（鉴权生效）
4. 生产 rollout 时：LGTM 机器安全组只放行两台主控的内网访问，公网全关

## 3. 备选（说明为什么不推荐）

- **每台 worker 配 OTLP 直连 + LGTM 挂公网**：违背"不对外开放"，且外部 worker 需要公网凭据，暴露面大
- **trace 随任务结果捎带（worker 调 complete_task 时把 span JSON 塞 body）**：不实时（任务结束才到）、batch 语义复杂、丢失窗口大——不如 OTLP 原生通道
- **主控起独立 sidecar（如 otel-collector）**：多一个进程运维面；C 进程 relay 20 行代码达到同样效果

## 4. 建议落地顺序

1. dev 先做 Layer 1（nginx 直通）验证 worker span 全链贯通（今天就能看到完整瀑布）
2. Layer 2 作为正式实现进 MR（C 进程 relay + 鉴权 + 计数），替换 Layer 1 的 nginx location
3. 生产 rollout 时 LGTM 单台机器只对主控内网开放，worker 配置零感知（只认 RUNTIME_SERVER_URL）
