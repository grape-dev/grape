# Plan: Issue #1731

## 方案概述
在业务基址候选链上叠加一层可用性判定：候选不可用即降级到下一优先级，端口来源显式化。

## 实现路径
1. **探测工具**（`src/prompt_builder.py`）
   - `_probe_business_api(base)`：`/api/health` 优先（要求 `server=ok`），失败再探 `/openapi.json` 端点族是否含 `issues` / `git-repo`。
   - 整体超时默认 1s（`GRAPE_BUSINESS_API_PROBE_TIMEOUT`），结果按基址进程内缓存（默认 60s，`GRAPE_BUSINESS_API_PROBE_TTL`），避免重复探测；缓存设条数上限（64）并做过期/最旧淘汰，长驻进程不无界增长。
2. **候选链求值**（`_resolve_business_base` 返回「基址 + 命中来源」，`_resolve_business_server_url` 为其兼容包装）
   - 显式 `task_business_api_url`（不探测）→ 本机业务端口（探测通过）→ `server_base_url`（探测通过）→ 末尾兜底并记 warning。
   - 命中来源与实际求值同源返回，探测全部失败时标 `unreachable_fallback`，不再误标 `local_business_port`（审计不失真）。
   - locality 查询异常（未知）也先探测本机业务端口，再回退 `server_base_url`，不直接落 runtime 网关。
   - 远程 runtime 保持原逻辑（`server_base_url`），不新增探测；单地址部署（本机）回退到唯一配置地址。
3. **业务端口显式配置**（`src/user/db.py::get_business_api_port`）
   - `task_business_api_port`（server-config.json > meta）> `GRAPE_BUSINESS_PORT` > `_local_port()` 兼容默认。
4. **审计日志**（`resolve_task_server_env`）
   - 输出最终 `GRAPE_SERVER_URL` / `RUNTIME_SERVER_URL` 与业务基址来源分支；地址经 `_redact_url` 抹掉 userinfo 与 query，日志不含 token/密码，env 值本身保持原样。

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 探测方式 | health 优先，失败探 openapi 端点族 | 成本低且能区分 runtime 网关与业务 API |
| 探测失败处理 | 回退下一候选，末尾兜底 | 个别候选不可用不应让任务失败 |
| 显式配置是否探测 | 不探测 | 显式配置体现运维意图，避免额外启动依赖 |

## 回归约束
- 显式 `task_business_api_url` 仍最高优先，且不触发网络探测。
- 单地址部署（`worker_connect_url` 回退 `server_base_url`）行为与改造前等价，不回归 #1577 / #1691。
- 远程 runtime 的解析结果与改造前一致。
- 单地址部署（本机 runtime）与改造前等价：本机候选可达返回同地址，不可达回退唯一配置的 `server_base_url`。