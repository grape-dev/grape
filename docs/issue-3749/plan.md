# Issue #3749 方案设计（Plan）

## 1. 中继可观测性（`src/domains/runtime/routes/runtime.py`）

### 1.1 指标注册去重（根因，基线已覆盖）

原方案：删除文件尾部（~L4106）重复的 `grape_trace_relay_requests_total` 注册块，只保留
中继路由旁（~L3116）一处，使 `_relay_total` 成为真实 Counter。

rebase 后：基线 `arch-weekly-11-ljt` 已删除尾部重复块，并把创建失败从静默 `None` 改为
`log.warning` 留痕（`tests/runtime/test_trace_relay_env_and_metrics.py` 断言
`rt._relay_total is not None` 与 `status="200"` 样本可见）。本 PR 只需**保持单次注册**
（新增守卫用例断言 `_PC("grape_trace_relay_requests_total"` 只出现一次），不再重复改这段。

### 1.2 统一打点入口

```python
def _relay_count(status: str) -> None:
    """中继结果打点（未注册/异常时静默，不影响请求路径）。"""
    if _relay_total is None:
        return
    try:
        _relay_total.labels(status=status).inc()
    except Exception:
        pass
```

三个分支统一调用：禁用 `503`、上游透传 `str(r.status_code)`、不可达 `relay_unreachable`。
禁用分支打点是本 Issue 的核心增量（配置缺失可被指标/告警捕获）。

### 1.3 启动自检

```python
_relay_selfcheck_done = False

def trace_relay_selfcheck() -> dict:
    """进程启动期一次自检：读 _relay_target()（按请求现读 env 的同一入口）；
    为空 → 复用 _warn_relay_disabled() 的限频 WARN（并占住 10 分钟窗口，首个请求不再刷）；
    非空 → INFO 打印生效端点；变量误带 /v1/traces → 额外 WARN。
    返回 {enabled, upstream, endpoint, misconfigured}；幂等：同一进程只输出一次。"""
```

- 只在**承接中继端点的进程**调用：四进程的 worker server（`src/api/grape_worker_server.py`）
  与单进程 `src/api/main.py` 的 lifespan 各一次，`try/except` 包裹（自检失败不阻断启动）；
- 端点推导：`endpoint = target.rstrip("/") + "/v1/traces"`（与请求路径同口径），
  误配后缀时 WARN：拼成 `/v1/traces/v1/traces` → 上游 404 → `status="404"` 不命中 503 告警。

## 2. 部署清单（配置面）

| 文件 | 改动 | 说明 |
|------|------|------|
| `.env.example` | 新增注释段 `OTLP_RELAY_TARGET` | 主文档入口，示例 `http://<lgtm-host>:4318`，注明仅主控/worker-server 消费 |
| `deploy/docker-compose.yml` | `grape-worker-server` 的 `environment` 增 `OTLP_RELAY_TARGET=${OTLP_RELAY_TARGET:-}` | 四容器拓扑下中继端点由 worker-server 承接 |
| `deploy/systemd/grape-worker-server.service` | 注释声明变量来源 `/etc/grape/grape.env` 与不配即 503 | 不写死默认值 |
| `scripts/grape-systemd-run.sh` | `if [ -n "${OTLP_RELAY_TARGET:-}" ]; then export ...; fi` | 与既有 `GRAPE_OTLP_ENDPOINT` 同款 opt-in，**不补默认值**（不把「没配」变「配了」） |
| `start-worker.sh` | 同款 opt-in 透传 + 注释 | 远端纯 engine 无需该变量（中继地址由 `RUNTIME_SERVER_URL` 推导）；单机混合部署可透传 |
| `deploy/alerts.yml` | 新增 `GrapeTraceRelayRejected` | `sum(rate(grape_trace_relay_requests_total{status="503"}[5m])) > 0`，`for: 5m`，warning |

## 3. 测试 seam

- 端点 seam：`TestClient(runtime.router)` + 依赖覆盖 `_require_credential`（沿用 #3699 用例）。
- 指标 seam：`insight.observability.registry.get_sample_value(name, labels)`——断言真实 registry
  上的样本值，而非桩对象（可同时证明「指标确实注册成功」）。
- 自检 seam：直接调用 `trace_relay_selfcheck()` + `caplog`，断言一次 WARN / INFO 端点。
- 部署契约 seam：读仓库文件断言配置项/告警规则存在（防「改代码没改清单」回归）。

## 4. 风险与取舍

| 风险 | 处理 |
|------|------|
| 启动自检与请求期各告警一次 → 刷屏 | 自检置 `_relay_disabled_warned = True`，进程内总计一次 |
| 生产未改 env 就发版 | 行为不变（仍 503），但启动日志与 503 指标/告警可立即定位；配置补齐由 SRE 发布侧执行 |
| 承运进程判断错误（把自检接到 mgmt/insight） | 只在 `main.py`（单进程）与 `grape_worker_server.py`（runtime 全量路由）调用，单测断言两处接线 |
| 指标标签爆炸 | status 取值有限（HTTP 码 + `relay_unreachable`），无动态维度 |

## 5. 模块归属（rebase 后修正）

PR #1973 首轮审查的 Optional 曾把观测实现抽到 `src/domains/runtime/otlp_relay.py`。
rebase 到最新基线后该拆分被**撤回**，原因（可验证）：

- 基线已把目标值改为按请求现读 env（`_relay_target()`），而抽出的模块持有
  `OTLP_RELAY_TARGET = os.environ.get(...)` 模块级快照 —— 与基线
  `tests/runtime/test_trace_relay_env_and_metrics.py::test_relay_target_env_is_read_per_request`
  直接冲突（运维改 env 不重启仍 503 的坑会重现）；
- 禁用态提示的 10 分钟限频状态 `_relay_warn_ts` 是请求路径与自检共用的，基线用例
  monkeypatch 的是 `runtime._relay_warn_ts`；跨模块共享限频需要反向依赖，得不偿失。

因此观测实现集中在中继路由同址：`_relay_target()` / `_warn_relay_disabled()`（基线）+
`_relay_count()` / `trace_relay_selfcheck()`（本 Issue 增量），路由模块只承载这一小段
可观测性代码，透传主体不变；两个入口统一 `from src.domains.runtime.routes.runtime import
trace_relay_selfcheck`。

审查 Optional 的另一项（`/v1/traces` 误配 WARN）已实现；Nit（文案不硬编码
`/etc/grape/grape.env`、接线用例改 AST 断言）已实现。