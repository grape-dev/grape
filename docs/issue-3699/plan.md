# Issue #3699 方案设计

## 1. 主控中继：给「禁用态」一个确定性标记

`src/domains/runtime/routes/runtime.py::runtime_traces_relay` 未配置目标时：

```python
_RELAY_DISABLED_HEADER = "X-Grape-Trace-Relay"

if not OTLP_RELAY_TARGET:
    _warn_relay_disabled_once()          # 进程内一次性 WARNING（提示配 OTLP_RELAY_TARGET）
    return _RelayResponse(status_code=503,
                          content='{"detail":"trace relay disabled"}',   # body 契约不变
                          media_type="application/json",
                          headers={_RELAY_DISABLED_HEADER: "disabled"})
```

- body / 状态码保持原契约（Spec：不改保护分支本身），只加**可分类的响应头**；
- 上游 5xx 透传路径**不带**该头 → 下游日志分级不会误伤真实的 LGTM 故障；
- 一次性告警用模块级布尔标志（进程内去重），13 台 worker × 每 5s 一批也只提示一次/进程。

## 2. 访问日志分级：精确匹配禁用标记才降噪

`src/api/server_common.py::_attach_request_log`（C 进程实际使用）与 legacy
`src/api/middleware.py::install_request_middleware` 是同一套分级逻辑的两份实现，**两处同步改**
（否则单进程兼容模式仍会刷 ERROR）：

```python
relay_disabled = False
try:
    response = await call_next(request)
    status = response.status_code
    ...
    relay_disabled = (status == 503
                      and response.headers.get("x-grape-trace-relay") == "disabled")
finally:
    ...
    elif status >= 500:
        if relay_disabled:
            _log.debug(msg, extra=extra)     # 配置缺失的 feature-off，非事故（同 /faro/collect 口径）
        else:
            _log.error(msg, extra=extra)
```

- 判据是**路径 + 503 + 标记**三者合一：只有中继自己产出的禁用态被降级；
- `relay_disabled` 在 `try` 前初始化，`call_next` 抛异常时仍走 `status=500 → ERROR`；
- `/faro/collect` 的既有 503 降级分支保持不动。

## 3. worker 侧 exporter：禁用态一次性告警 + 指数退避

`src/kernel/otel.py` 新增（全部为进程内状态 + 可注入 HTTP seam，便于单测）：

```python
class _RelayBackoffPolicy:
    """503+trace relay disabled 的进程级退避状态（线程安全）。"""
    def in_window(self, now=None) -> bool      # 静默窗口内 → 直接丢弃本批，不发请求
    def note_disabled(self, now=None) -> bool  # True = 本次应输出（首次）WARNING；delays: 30s→60s→…→900s
    def note_success(self) -> None             # 2xx 复位（延迟回 baseline、允许再次告警）

def relay_failure_kind(status, body) -> str    # "disabled" | "retryable" | "fatal"

class _RelaySpanExporter:                     # 鸭子类型实现 export/shutdown/force_flush
    """替代 SDK OTLPSpanExporter 的 worker 中继 exporter（单一 POST + 自主退避）。"""
```

> 不继承 SDK `SpanExporter`：内核模块 import 期只依赖标准库（worker 包可能未装
> opentelemetry），`BatchSpanProcessor` 仅按 export/shutdown 协议调用。

行为矩阵：

| 上游结果 | 处理 | 日志级别 |
|---|---|---|
| 2xx | `SUCCESS`，`note_success()` 复位退避 | 无 |
| 503 且 body 含 `trace relay disabled` | 标记禁用：窗口内后续批次不再发请求；**首次**提示 | `WARNING` 一次 |
| 网络错误 / 429 / 502 / 其他 5xx | 指数退避重试（≤3 次，受 timeout 预算约束），失败返回 `FAILURE` | 重试 `WARNING`，最终 `ERROR` |
| 其他 4xx | 立即 `FAILURE`（不可重试） | `ERROR` |

- 编码复用 SDK 的 `encode_spans`（与官方 exporter 同源），仅替换传输与重试策略；
- `init_otel()` 的中继分支改用 `_RelaySpanExporter`，构造失败（例如 SDK 版本差异）时**回退**
  到原 `OTLPSpanExporter`，保证 trace 通道不因降噪改造而整体失效；
- 主控/CLI 的 gRPC→HTTP 直连分支不变；
- `requests` 由 `opentelemetry-exporter-otlp-proto-http` 传递依赖提供（其 HTTP transport 即 requests），
  worker 包无需新增依赖。

## 4. 测试 seam

| 切片 | seam | 断言 |
|---|---|---|
| 访问日志分级 | 最小 FastAPI app + `_attach_request_log` / `install_request_middleware` | 带标记 503 无 ERROR；无标记 503 仍有 ERROR |
| 中继禁用响应 | `runtime_traces_relay`（TestClient + 依赖覆盖） | 503 + `X-Grape-Trace-Relay: disabled`，body 仍为 `trace relay disabled` |
| 禁用分支 | `_RelaySpanExporter(post=fake)` + `_RelayBackoffPolicy` | 1 次 WARNING 无 ERROR；窗口内第二次 export 不发请求；退避递增有上限 |
| 可重试分支 | 同上（fake post 返回 502 / 抛连接错误） | 重试发生、最终 ERROR 存在（未被降噪） |
| 正常分支 | 同上（fake post 返回 200） | `SUCCESS`、退避状态复位 |

## 5. 交付

- 分支 `feat/issue-3699` → PR base `arch-weekly-11-ljt`；
- TDD 红/绿分提交：先 `test(#3699)` 失败测试，再 `feat(#3699)` 实现；
- 建 PR 后 Issue 评论告知 + A2A 通知 QA 审查。