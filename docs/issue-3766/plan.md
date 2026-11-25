# Plan: Issue #3766

## 方案概述
在任务业务 API 基址解析链上增加端点级可达性校验：以 `/api/issues/mine`（业务 router 真实注册路由）的真实响应识别 admin 前端接管签名，按候选链回退到可用业务基址，并在无可用候选时告警/阻断。

## 实现路径
1. **探测原语**（`src/kernel/prompt_builder.py`）
   - `_http_json_status(url, timeout) -> (status, payload)`：非 2xx 也读响应体，超时/断连返回 `(0, None)`。
   - `_probe_business_issues(base, timeout)`：`/api/issues/mine` 2xx JSON、401/403 鉴权挑战 → 可用；301/302/307/308/404/405、5xx、HTML → 不可用。
   - `_probe_business_api`：health → openapi → issues 三级探测，沿用 60s 缓存与 1s 总超时。
2. **候选链求值**（`_business_candidate_chain` / `_resolve_business_base`）
   - 候选顺序：显式 `task_business_api_url` → 本机业务端口（仅本机/未知 locality）→ `server_base_url`（独立取自 `get_server_base_url()`，不复用 `get_business_api_url()`，避免显式存在时被去重丢弃）→ 显式 `worker_connect_url`（与 server_base_url 不同时）；按规范化 URL 去重。
   - 返回首个通过探测的候选（来源标签真实）；全部失败时优先回退显式/server_base_url，标 `unreachable_fallback`。
   - 远程 runtime（`_runtime_is_local=False`）不加入本机回环候选。
3. **守护模式**
   - `GRAPE_BUSINESS_API_GUARD`：`warn`（默认）/`strict`/`off`。
   - `warn`：`error` 日志含被检候选、脱敏地址与修复指引（`task_business_api_url` / 网关路由）。
   - `strict`：抛 `BusinessApiGuardError` 阻断注入。
   - `off`：保留 Issue #1731 旧语义（显式不探测、远程直接用对外基址）。
4. **配置告警**
   - `get_url_split_warning` 将 `task_business_api_url` 纳入回环地址告警，提示改为对外可访问业务 API。
5. **文档**
   - `configs/server-config.example.json` 增加 `_comment_issue_3766`；
   - `help/usage/agent-environment-variables.md` 增加注入守护说明。

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 探测端点 | `/api/health` + `/openapi.json` + `/api/issues/mine` | 覆盖业务服务健康、路由族与 Skill 实际调用端点 |
| 鉴权响应判定 | 401/403 JSON 视为可用 | 端点存在仅缺 token，与 admin 前端 301/404 明确区分 |
| 失败默认行为 | 回退 + error 告警（不阻断） | 避免注入校验本身造成全量任务不可用 |
| 严格阻断 | `strict` 显式开启 | 高风险/受控环境可按需 fail-closed |

## 回归约束
- `GRAPE_BUSINESS_API_GUARD=off` 时与 Issue #1731 / #1577 / #1858 行为一致。
- `resolve_a2a_base` 不触发业务 API 探测（A2A 控制面独立）。
- 远程 runtime 不注入本机回环业务基址。
- 探测缓存条数上限与 TTL 语义不变，长驻进程不无界增长。
- 仅改动本次需求相关文件；不提交运维脚本/日志/真实配置。