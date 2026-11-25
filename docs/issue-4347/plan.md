# Plan: Issue #4347 — 实现方案

## 模块设计

### 新增 `src/domains/runtime/routes/auth_failure_budget.py`（纯进程内，无迁移）

- `AuthFailureBudget`：线程安全（`threading.Lock`）的进程级单例
  `auth_failure_gate`；按 IP 维护滑动窗口双端队列（60s/3600s）+ 封禁状态 +
  日志聚合账本；
- `check_banned(ip) -> int`：请求入口快速路径，封禁期内返回剩余秒数（调用方
  直接 429，不计数）；
- `record_failure(ip, prefix, endpoint) -> FailureOutcome`：记一次失败并判定
  `reject`（401）/ `throttle`（分钟超限 → 429）/ `ban`（小时超限 → 封禁），
  返回 `log_line`（该窗口的聚合摘要，每 IP 每窗口最多 1 条；ban 强制可见）；
- env 阈值读取采用调用期解析（`_env_int`，非法值回退默认并告警，对齐
  `_resolve_probe_fresh_window` 模式）；
- 摘要格式保留 `runtime credential rejected` 前缀：`ip=… window_count=N
  first=… last=… prefixes=… endpoints=… minute=N hour=M action=…`。

### 接线 `src/domains/runtime/routes/runtime.py`

- `_auth_credential` / `_require_credential_ctx` 两个 rta_ 鉴权函数统一接入：
  - 入口先 `check_banned`（封禁期直接 429）；
  - 非 `rta_` 前缀 / 凭据查无两个失败分支走新增统一出口 `_reject_auth_failure`；
- `_reject_auth_failure`：记录预算 → 输出聚合摘要（替代原逐条 WARNING）→
  超限抛 `_auth_throttled_exc`（429 + `Retry-After` 头），否则按原语义抛 401；
- register 路由锁内复核（Issue #417 竞态面）维持原 401 语义，不重复计数。

## 测试方案

- `tests/runtime/conftest.py`：autouse 每用例前后 `auth_failure_gate.reset()`
  ——TestClient 来源 IP 恒为 `testclient`，防跨用例累计误触发 429 殃及既有用例；
- `tests/runtime/test_4347_auth_failure_budget.py`（11 例）：
  - 单测：分钟阈值 10→429、窗口滑出恢复、小时累计 61→封禁、封禁期不计数、
    封禁过期解除、env 阈值覆盖、日志聚合（每窗口 1 条 + 抑制累计 + 首末时间 +
    prefix/endpoint 集合）、ban 强制摘要、按 IP 隔离、reset；
  - HTTP 层（TestClient）：/register 401×N 后 429（Retry-After）、有效凭据
    不受预算影响、封禁期内全端点 429、封禁过期恢复 200。

## 风险与缓解（对齐 Spec）

- 多 worker/出口 NAT 共享 IP 误封：只计鉴权失败 + 429 明确语义 + 封禁期短；
- 4 进程独立计数：阈值按进程量级评估，env 可调；
- 聚合丢单条细节：摘要保留累计次数/首末时间/prefix/endpoints 集合。
