# Issue #3749 需求文档（Spec）

> 来源：Issue #3749 正文 + Issue Agent Spec（评论 202830792）+ VOD 收敛结论。
> 基线分支：`arch-weekly-11-ljt`；开发分支：`feat/issue-3749`。

## 1. 问题

`POST /api/runtime/traces` 在运行实例恒返回 503 `{"detail":"trace relay disabled"}`：

- 近 1 小时 87/87 次请求全部 503，来源为 10+ 个 worker 出口 IP（UA `OTel-OTLP-Exporter-Python/1.45.2`），属集群级而非单机；
- worker 进程侧同口径 ERROR 约 70 条，duration 1–26ms（立即拒绝，非上游超时）。

根因是**部署漏配**：`OTLP_RELAY_TARGET` 只存在于代码与测试，`.env.example`、
`deploy/docker-compose.yml`、systemd 运行期 env、`start-worker.sh`、`deploy/alerts.yml`
均无该配置项/告警 → 运行实例变量为空，中继整体关闭，worker 的 span 全部被拒。

## 2. 编码阶段新发现（同一链路的指标缺陷，已由基线修复）

> rebase 复核（2026-11-24）：该缺陷在基线 `arch-weekly-11-ljt` 已由 `tests/runtime/test_trace_relay_env_and_metrics.py`
> 的同一修复覆盖（尾部重复注册块已删、`_relay_total` 创建失败改为留 WARN、`OTLP_RELAY_TARGET`
> 改为按请求现读 env）。本 Issue 的增量因此收敛为「禁用态打点 + 启动自检 + 部署清单/告警」。
> 以下为当时的代码事实，保留作根因记录。


`src/domains/runtime/routes/runtime.py` 中 `grape_trace_relay_requests_total` **被注册了两次**
（中继路由旁 ~L3116 与文件尾部 ~L4106），两次都挂同一个 `insight.observability.registry`：

- 第二次 `Counter(...)` 抛 `DuplicateTimeseries: Duplicated timeseries in CollectorRegistry`；
- 该异常被 `except Exception: _relay_total = None` 吞掉，于是模块级 `_relay_total` **恒为 None**；
- 结果：中继指标**从未真正上报过**（转发成功分支也不打点），Issue 要求的「503 持续 5min 告警」
  在当前代码上不可能生效。

同时禁用态（`OTLP_RELAY_TARGET` 为空）分支**完全不打点**，配置缺失只能靠人工翻日志发现。

## 3. 目标与验收

目标：让 worker 的 OTLP/HTTP span 经主控中继进入 LGTM，`service.name=grape-worker` 的
trace 补齐根 span；配置缺失/不可达具备启动提示、指标与告警。

验收标准：

- [ ] `.env.example`、`deploy/docker-compose.yml`（worker-server）、systemd 运行期 env 注入处、
  `start-worker.sh` 均含/说明 `OTLP_RELAY_TARGET`（示例值 `http://<lgtm-host>:4318`，不含 `/v1/traces`）；
- [ ] 进程启动时对中继配置自检一次：为空 → WARN 一次并说明 503 语义与配置方法；
  非空 → INFO 打印生效端点 `<target>/v1/traces`；
- [ ] 禁用态 503 分支计入 `grape_trace_relay_requests_total{status="503"}`；
- [ ] `grape_trace_relay_requests_total` 单次注册、`/metrics` 可见（转发成功计 `status="200"`）；
- [ ] `deploy/alerts.yml` 含 503 持续 5min 告警，文案指明「OTLP_RELAY_TARGET 未配置或 LGTM 不可达」；
- [ ] 不改变禁止分支的 body/状态码契约（`{"detail":"trace relay disabled"}` + `X-Grape-Trace-Relay: disabled`）。

## 4. 范围

In scope：中继端点可观测性修复（指标/自检）、部署清单配置补齐、告警规则、单测。

Out of scope：#3743 前端 RUM（`/api/faro/collect` 405）、#3570 kernel 埋点、LGTM/Tempo 自身部署；
运行期真机验证（起服务 + curl 中继 + Tempo 抽查根 span）由 sre 环节承接。

## 5. 基线与 rebase 适配（2026-11-24）

基线 `arch-weekly-11-ljt` 在本 PR 创建后前进 409 个提交，其中同链路的修复已落地：

- `src/domains/runtime/routes/runtime.py::_relay_target()` 按**请求现读** `OTLP_RELAY_TARGET`
  （不再有模块级常量快照），配套用例 `tests/runtime/test_trace_relay_env_and_metrics.py`；
- 同名 Counter 只注册一处，创建失败改为 `log.warning` 留痕，不再静默 `None`；
- 禁用态提示改为 `_warn_relay_disabled()` 的 **10 分钟限频**（`_relay_warn_ts`），
  用例 `tests/runtime/test_issue_3699_trace_relay_noise.py` 断言「两次请求仅一条 WARNING」。

因此 rebase 后的实现口径：

1. 保留基线的按请求读 env 与限频告警，**不引入模块级 env 快照**（否则基线用例直接红）；
2. 中继观测实现继续留在 `routes/runtime.py`：`_relay_count()`、`trace_relay_selfcheck()`
   与基线 `_relay_target()`/`_warn_relay_disabled()` 同址，避免跨模块共享限频状态；
   审查 Optional 的「抽模块」在基线下会把 env 快照重新引入，故**不采纳**，改为在路由内集中；
3. 自检与请求期共用限频窗口：自检 WARN 后占住窗口，首个禁用态请求不再重复 WARN；
4. 新增内容：禁用态 `status="503"` 打点、启动自检（含 `/v1/traces` 误配 WARN）、
   五个部署配置面、两条告警规则、两个回归用例文件并接入 `linux-backend` 门禁。
