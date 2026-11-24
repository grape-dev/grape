# Issue #4082 方案设计（Plan）

## 1. 测试 seam（编码前先定）

| 切片 | 公开 seam | 说明 |
|------|-----------|------|
| worker id 守卫 | `RuntimeClient.probe_tags()` / `RuntimeClient.claim()` / `heartbeat()` | 复用已有 HTTP 客户端公开方法，不测私有拼串 |
| 注册回填 | `RuntimeClient.register()` 返回值 | 断言非法 id 不再出现在 `runtime_ids` |
| 服务端 400 | HTTP `GET /api/runtime/runtimes/{id}/tags` | 走 TestClient，断言状态码 + body |
| 指标 | `record_runtime_tags_probe_failure()` + `/metrics` 文本 | 断言计数器标签口径 |
| 告警 | `deploy/alerts.yml` 文本解析 | 断言规则名/表达式/labels 存在 |

## 2. worker 侧（`src/domains/runtime/engine/`）

### 2.1 新增 `runtime_id_guard.py`（纯函数 + 结构化错误）

```python
RUNTIME_ID_MISSING = "RUNTIME_ID_MISSING"
PROBE_REASONS = ("id_missing", "unauthorized", "not_found", "no_tags", "other")

def parse_runtime_id(raw) -> int | None      # 仅接受正整数（bool/浮点/字符串数字均收敛）
def is_valid_runtime_id(raw) -> bool
class RuntimeIdMissingError(Exception):
    code = RUNTIME_ID_MISSING
    def to_dict(self) -> dict                # {code, host, runtime_id, context, message}
def missing_runtime_id_error(raw, *, host, context) -> RuntimeIdMissingError
def tag_probe_signal_sink(...)               # 复用 contract_guard.http_alert_sink 同款非抛异常 sink
```

- 守卫只在「**发请求前**」执行，命中即抛 `RuntimeIdMissingError`，**不产生任何 HTTP**。

### 2.2 `client.py`

- `_guard_runtime_id(path, runtime_id)`：`parse_runtime_id` 为空 → 抛 `RuntimeIdMissingError`（带 host/context）。
- `claim` / `claim_by_tag` / `recover_orphans` / `report_skills`：拼 URL 前守卫。
- 新增 `probe_tags(runtime_id) -> (tags, reason)`：
  - id 非法 → `([], "id_missing")`（不发请求）；
  - `_request_raw` 拿到 200 且有标签 → `(tags, "")`；200 空 → `([], "no_tags")`；
  - 401 → `([], "unauthorized")`；404 → `([], "not_found")`；其它/网络 → `([], "network")`；
  - **401 不再抛 `CredentialRevokedError`**（标签探活 401 是配置/归属问题，不是凭据吊销，
    否则 worker 会被误杀；凭据吊销由 heartbeat/register 判定）。
  - `get_tags()` 保留，改为 `probe_tags` 的薄包装（返回 tags），兼容既有调用方/测试 Fake。
- `register()`：只接受正整数 id（去 0 兜底）；**逐条过滤**——部分 id 非法时保留同响应里的合法 id
  并返回 `(合法 ids, probe_url)`，非法项记 `RUNTIME_ID_MISSING` 日志 + `last_register_invalid_ids`，
  由 `_reregister_cycle` 把下次重注册提前到 `RUNTIME_PARTIAL_ID_REREGISTER_BACKOFF`（默认 60s）后补齐
  （PR #2066 审查 Optional ①：不再因单个异常 id 丢弃全部合法 id）。
- `heartbeat()`：过滤非法 `runtime_ids`；过滤后为空 → 不发请求，落 `RUNTIME_ID_MISSING` 并返回 `None`。

### 2.3 `agent.py` 主循环

- claim 循环改用 `probe_tags`，拿到 `reason` 后：
  - `reason == ""` → 正常（连续失败计数清零）；
  - 其它 reason → 连续失败 +1，达到阈值（`RUNTIME_TAGS_PROBE_ALERT_AFTER`，默认 3）时
    发一次信号（`tag_probe_signal_sink`）+ ERROR 日志；每再累计 N 次再发一次（防刷）；
  - `id_missing` → 立即触发 `_reregister_cycle(force=True)`（退避重试注册，避免每轮空转）；
  - 契约外 reason（`http_403` 等）→ 归 `other` 桶计入连败并上报（原始 reason 落一次日志）；
  - **连败计数按 rid 隔离**（`tags_probe_reporter_for` / `prune_tags_probe_reporters`）：一个
    reporter 只代表一个引擎，重注册换 id 时回收消失 rid 的实例——否则同轮其它引擎的成功会把
    本引擎的连败清零，多引擎主机上告警永不触发（PR #2066 审查 Required）。
- `RuntimeIdMissingError` 在 claim 路径上单独捕获：结构化日志 + 计一次 `id_missing` + force 重注册。

## 3. 服务端（`src/domains/runtime/routes/`）

### 3.1 统一 400：`runtime_id_guard.py` 的 `RuntimeIdGuardRoute(APIRoute)`

- `get_route_handler()` 包装原 handler：若 `path_params["runtime_id"]` 非正整数（`0`/`abc`/`-1`/`1.5`）
  → 直接 `JSONResponse(400, {"code":400,"error":"invalid runtime_id",...})`，**先于鉴权与 FastAPI 参数校验**。
- `runtime.py` 的 `router` 与 `runtime_view.py` 的 `view_router` 均以 `route_class=` 挂载，
  A/C 双进程、单进程模式全部覆盖（`/runtimes/uid/{engine_uid}/*` 无 `runtime_id` 参数，不受影响）。
- 有效 id 的路径零改动（OpenAPI schema 不变，契约测试不需要改基线）。

### 3.2 信号承接 + 指标

- `observability/__init__.py` 新增 `RUNTIME_TAGS_PROBE_FAILURES = Counter("grape_runtime_tags_probe_failure_total", ..., ["host","reason"])`
  与 `record_runtime_tags_probe_failure(host, reason)`（未知 reason → `unknown`，不丢数）。
- reason 口径 = `id_missing` / `unauthorized` / `not_found` / `no_tags` / `other`（契约外失败归并桶，
  原始 reason 随 `raw_reason`/detail 与日志带出）。
- `runtime.py` 新增 `POST /runtime-tags-probe/signal`（rta_ 凭据，worker 协议端点）：
  **不因 reason 非法而拒收**——未知 reason 与 `record_runtime_tags_probe_failure` 同口径归 `unknown`
  计数并留 WARNING（PR #2066 审查 Optional ③：旧行为 400 会让新版本 worker 的新 reason 只落本地
  JSONL、指标缺数）。

### 3.3 告警

- `deploy/alerts.yml` 新增 `GrapeRuntimeTagsProbeFailing`：
  `sum by (host, reason) (increase(grape_runtime_tags_probe_failure_total[15m])) >= 3`，`for: 5m`；
  annotations 给出「未注册(id_missing) / 凭据或归属不对(unauthorized) / 行已删(not_found) /
  已注册但无标签(no_tags) / 契约外失败(other)」的排查指引；`reason="unknown"`（服务端兜底桶）只计数不告警。

## 4. 存量 15 台主机（运维）

- 核对入口：`worker` 日志 `RUNTIME_ID_MISSING` / `probe reason=...`、`/metrics` 的
  `grape_runtime_tags_probe_failure_total{host=...}`；补回真实 `GRAPE_RUNTIME_ID` 后指标归零即验证通过。
- 本条只交付可观测与守卫，不做线上数据变更。

## 5. 风险

| 风险 | 处理 |
|------|------|
| 401 语义调整（probe 不再抛 CredentialRevoked）影响凭据吊销判定 | 吊销仍由 register/heartbeat 的 401 判定并退出进程；probe 401 单独计数 |
| 服务端 400 影响既有调用方 | 仅非法 id 生效；有效 id 路径与 OpenAPI 契约不变 |
| 客户端守卫过严导致完全停探 | 守卫仅拦截非法 id；合法 id 路径行为不变；`id_missing` 触发重注册自愈 |