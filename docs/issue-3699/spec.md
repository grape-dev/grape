# Issue #3699 需求文档：worker trace 中继 503 刷屏修复与降噪

> 目标合入分支：`arch-weekly-11-ljt` | 来源：Issue #3699（VOD 日志分析 comment 202756411、Spec comment 202761411）| 2026-11-21

## Objective

- 消除 worker 侧 trace 上报持续产生的 `POST /api/runtime/traces status=503` ERROR 刷屏，
  并在中继不可用时保留一条可执行的一次性提示。
- 恢复 worker trace 经主控中继转发落库（`service_name=grape-worker` 的调用链数据）。
- 业务零影响前提下，让日志噪音不再掩盖真实故障。

## 现象与根因

- 证据（Issue 正文日志，2026-11-21 12:34 UTC，`<node-a>`/`<node-b>`）：
  `level=ERROR component=devops-bot role=worker service_name=grape-worker
  message="POST /api/runtime/traces status=503 duration=1~38ms"`，`user_id/repo_key/issue_id/seq`
  均为空——OTLP 批量导出特征，非业务请求；1~5ms 瞬时返回说明是本地立即拒绝。
- 链路：worker `src/kernel/otel.py` `_worker_relay_exporter_kwargs()` 推
  `{RUNTIME_SERVER_URL}/api/runtime/traces` → 主控 C 进程
  `src/domains/runtime/routes/runtime.py::runtime_traces_relay` → `OTLP_RELAY_TARGET/v1/traces`。
- 根因（高置信）：prod 主控未配置 `OTLP_RELAY_TARGET`，中继端点直接返回
  `503 {"detail":"trace relay disabled"}`（预期保护，不改契约）。
- 噪音来源有两处：
  1. **主控 C 进程访问日志**：`src/api/server_common.py::_attach_request_log`（以及 legacy
     `src/api/middleware.py`）对 `status >= 500` 一律 `ERROR`，中继禁用态每批上报都命中，
     与 Issue 日志的 `request_id`/`duration_ms` 字段完全一致；
  2. **worker 侧 OTLP exporter**：SDK 对 503 视为可重试，5 次指数退避重试后仍以
     `ERROR "Failed to export spans batch ..."` 收尾，属确定性配置态错误，重试无意义。

## 范围

### In Scope

- 主控中继禁用响应加确定性标记（响应头），访问日志对「503 + 标记」降级为
  `debug`（标记仅由中继禁用分支在该路径产出，路径区分隐式成立），并在中继侧进程内
  一次性 `WARNING` 提示配置 `OTLP_RELAY_TARGET`。
- worker 侧中继 exporter 识别 `503 + trace relay disabled`：一次性 `WARNING` + 指数退避
  静默窗口（窗口内不再发起上报），恢复 2xx 后自动复位；网络错误与其他 5xx 保持可重试
  并保留 `ERROR` 收尾（不被误抑制）。
- 单测覆盖：禁用分支（一次性告警 + 退避 + 抑制）、可重试分支、正常分支，以及访问日志
  分级（带标记降级 / 不带标记仍 ERROR）。

### Out of Scope

- prod 主控 `OTLP_RELAY_TARGET` 的注入与重启（部署动作，随发布由 sre 执行验证）。
- 主控「未配置 → 503」契约与 `/faro/collect` 等其他中继端点。
- LGTM/Tempo 容量、鉴权、看板，以及 worker 业务请求逻辑。

## 验收标准

- [ ] 中继禁用场景下，worker 不再持续输出该 ERROR，主控访问日志中该请求降为 debug，
      进程内仅一次 WARNING 提示检查 `OTLP_RELAY_TARGET`；
- [ ] 网络错误、502、其他 5xx 仍按可重试路径重试，最终失败仍输出 ERROR，不被降噪命中；
- [ ] 中继恢复 2xx 后退避状态复位，后续批次正常上报；
- [ ] 新增单测覆盖禁用/可重试/正常/访问日志分级分支，现有测试全绿。