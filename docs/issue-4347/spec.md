# Spec: Issue #4347 — runtime 鉴权失败 IP 失败预算 + 拒绝日志按 IP 聚合节流

来源：Issue #4347 原始描述 + issue Agent 已发布 Spec（Issue 评论 203661185）。
本文件为 coding 侧的落地摘要，权威需求以 Issue 内 Spec 评论为准。

## Objective

为 runtime 鉴权失败路径增加 **IP 维度失败预算**（每分钟 >10 次 → 429；1 小时累计
>60 次 → 短期封禁）与**按 IP 聚合的日志节流**（每 IP 每分钟最多 1 条摘要），终止
失效 `rta_` 凭据被无限重试刷屏（实测 741 条/小时，占 worker WARN 17%）。

## 现状（缺陷）

- `src/domains/runtime/routes/runtime.py` `_auth_credential`：鉴权失败仅
  `log.warning("runtime credential rejected: prefix=… ip=…")`，失败不落任何
  限流/封禁状态，调用方可无限重试（每 5 秒一次，持续整小时不被拦截）；
- `/api/runtime/register` 等公开注册入口无速率限制，741 次拒绝 vs 369 次
  register 401，说明同一凭据还在 claim/heartbeat 等端点重复试探；
- `_require_credential_ctx`（whoami/sandbox 等端点）失败分支甚至无留痕。

## 期望行为

1. 鉴权失败按 IP 计数（滑动窗口：60s 分钟窗 + 3600s 小时窗）：
   - 分钟窗内 >10 次 → 429（Retry-After 头，指向窗口滑出）；
   - 小时窗内 >60 次 → 短期封禁（默认 600s，env `RUNTIME_AUTH_BAN_SECONDS`），
     封禁期内在请求入口直接 429（不再计数，避免封禁被持续重试无限续期）；
2. 拒绝日志按 IP 聚合：每 IP 每个 60s 窗口最多 1 条摘要（窗口累计次数 + 首末
   时间 + prefix/endpoint 集合 + 自上一条摘要以来被抑制的条数）；摘要保留
   `runtime credential rejected` 前缀，兼容既有 Loki/Grafana 检索；
   ban 是一次性状态迁移，强制可见（不受窗口节流抑制）；
3. 结构化记录 ip / prefix / endpoint；
4. 只计鉴权失败（凭据校验不过）：成功注册/心跳不受影响（多 worker 共享出口
   NAT 不误伤）。

## Scope

- In：`_auth_credential` / `_require_credential_ctx` 失败分支接入预算与聚合；
  新增节流辅助模块 `auth_failure_budget.py`；`tests/runtime/` 单测。
- Out：网关层通用限流（#4181）；按 `rta_` 前缀查询 runtime 归属的管理台/CLI
  （P1-1，后续迭代）；来源 IP 临时封禁与凭据吊销（运维处置动作）。

## 关键设计决策（与 Spec 评论一致）

| 决策 | 选择 | 理由 |
|------|------|------|
| 计数存储 | 进程内存 | 改动最小，重启退化为重新计数，代价可接受 |
| 封禁粒度 | runtime 全鉴权失败路径 | 该凭据同时打 claim/heartbeat 等端点，只封 register 留有绕行面 |
| 阈值配置 | env 可调（`RUNTIME_AUTH_FAIL_MINUTE_LIMIT`/`_HOUR_LIMIT`/`_BAN_SECONDS`/`_MAX_IPS`） | 4 进程独立计数按进程量级评估，可现场调参 |
| 内存上界 | `RUNTIME_AUTH_FAIL_MAX_IPS`（默认 10000），超出清过期项后仍满则整体重置（fail-open） | 计数器不阻塞鉴权主链路 |

## Acceptance Criteria

- [x] 同一 IP 每分钟鉴权失败 >10 次返回 429；1 小时累计 >60 次进入短期封禁；
- [x] 同一 IP 拒绝日志每分钟最多 1 条摘要（含累计次数 + 首末时间）；
- [x] 单测覆盖失败预算计数、429 返回路径、聚合日志节流；相关既有测试不回归
  （基线对照：环境相关失败集合与改动前完全一致）；
- [ ] 线上验收：1 小时窗口内该 IP 凭据拒绝条数下降 >90%（发布后观察）。
