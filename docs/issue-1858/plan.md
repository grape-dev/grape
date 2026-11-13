# Issue #1858 方案设计

## 关键决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 路由方式 | 默认分服务变量，聚合网关可选 | 兼容现网 8081/8082 端口，迁移期可平滑切网关 |
| 变量命名 | `GRAPE_VCS_BASE_URL` / `GRAPE_ISSUE_BASE_URL` / `GRAPE_A2A_BASE_URL` | 与 grape-cli #151 约定对齐 |
| A2A 回退 | `RUNTIME_SERVER_URL`（回连基址） | 控制面语义，不与业务基址互冒充 |
| 兼容策略 | 保留 `GRAPE_SERVER_URL` / `RUNTIME_SERVER_URL` | 旧任务/旁路路径不硬失败 |
| 本机 worker | 同端点跟随本机直连，显式分服务保留 | 避免 NAT 回环，又不覆盖运维显式路由 |

## 实现
1. `src/user/db.py`
   - `get_task_service_api_url_explicit(service)`：读取 `task_vcs_api_url` / `task_issue_api_url` /
     `task_a2a_api_url`（server-config > meta）；
   - `get_task_gateway_base_url()`：读取可选聚合网关 `task_gateway_base_url`。
2. `src/config.py`：启动时把上述四项从 server-config.json 双写 meta，未配置不写。
3. `src/prompt_builder.py`
   - `_resolve_service_base()`：显式服务键 > 网关前缀（`/vcs` `/issue` `/a2a`）>
     vcs/issue 回退业务基址、a2a 回退 `RUNTIME_SERVER_URL`；
   - `_build_service_base_env()` / `resolve_service_base_env()`：输出三面基址与
     `GRAPE_BASE_URL_SOURCES` 来源图；
   - `resolve_task_server_env()` 在原有两项基础上注入三面基址（复用已解析基址，避免重复探测）；
   - `src/service_diagnostics.py`：`service_for_endpoint()` / `format_base_url_candidates()`
     独立承载端点→服务映射与候选 base_url 诊断，供平台自调用告警与回归用例复用；
   - `resolve_a2a_base()`：公开的 A2A 专用解析入口，仅读显式配置/网关/回连基址，
     不触发业务 API 探测。
4. 派发/执行路径
   - `src/runtime/direct_executor.py`：direct 执行 env 复用 `resolve_task_server_env`；
   - `src/api/routes/runtime.py`：worker 执行前 `GET /tasks/{id}/env` 按任务 runtime 下发三面基址，
     覆盖历史任务/旧快照；
   - `src/api/chat_session_env.py`：chat 会话同样注入三面基址；
   - `src/agent/executor.py`：旧快照缺失时仅补缺失键（不覆盖显式值）。
5. 本机/内网 worker：`src/runtime/agent.py::_apply_local_worker_base_override`
   - VCS / Issue 跟随「生效的业务基址」`GRAPE_SERVER_URL`：单地址部署覆盖为 `local_base`，
     分端口部署（业务 8081 / runtime 8083）保留业务基址，绝不改成 runtime 端口；
   - A2A 继续跟随 runtime 直连覆盖为 `local_base`；
   - 显式分服务基址保留，更新来源图。
6. A2A 自调用
   - `src/api/routes/events.py` 重派 A2A、`src/api/routes/issues.py` 恢复续办改用
     `resolve_a2a_base()`；失败日志/响应附 `format_base_url_candidates("/api/a2a")`。
7. runbook 前置校验：worker 执行前对三面基址做缺失显式告警（禁止静默回退 localhost）。

## 风险与缓解
| 风险 | 影响 | 缓解 |
|------|------|------|
| 现网端口/网关版本不一致 | High | 分服务变量为默认、网关为可选开关，双跑迁移 |
| 显式分服务地址被本机覆盖 | Med | 仅在缺失或与回退基址同端点时覆盖 |
| 历史快照缺新变量 | Med | worker env 重拉 + executor 仅补缺失键 |
| env 扩散泄露 | Low | 仅注入地址，不注入 token 类凭据 |
