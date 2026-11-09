# Issue #1577 方案设计

## 关键决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 变量语义 | `GRAPE_SERVER_URL`=业务 API，`RUNTIME_SERVER_URL`=runtime | 不改 CLI 消费约定，改动面最小 |
| 业务基址来源 | `task_business_api_url` 显式配置 > `server_base_url` | 故障根因即回连地址指向 runtime 面 |
| 兼容策略 | 单地址回退同值 | 避免存量部署回归 |

## 实现
1. `src/user/db.py` 新增 `get_business_api_url()`：`task_business_api_url`（server-config
   > meta）> `server_base_url`。
2. `src/config.py`：`task_business_api_url` 可选配置双写 meta，未配置不写。
3. `src/prompt_builder.py`：
   - 抽出 `_runtime_is_local(runtime_id)`（credential_id==0 或 last_ip 127.*；查询异常
     返回未知，保留 localhost 异常回退语义）。
   - `_resolve_server_url()` 保持 runtime / 回连语义（`get_worker_connect_url`）。
   - 新增 `_resolve_business_server_url()`：显式 `task_business_api_url` >
     本机 runtime 直连 `127.0.0.1:{port}` > `server_base_url`（未显式配置时本机 runtime
     走回环、远程 runtime 用 `server_base_url`）。
   - 新增 `resolve_task_server_env()`：两项基址统一组装，供事件/重派/对话入队共用。
4. `src/api/event_pipeline.py`：`_enqueue_pipeline_task` 的 env_extra 与 `build_task_env`
   统一经 `resolve_task_server_env()` 注入 `GRAPE_SERVER_URL=业务基址`、
   `RUNTIME_SERVER_URL=runtime 基址`。
   对话入队四条链路（`src/api/routes/chat.py`、`src/im/handler_async.py`、
   `src/im/vod_handler_async.py`、`src/runtime/scheduler.py`）同样经
   `resolve_task_server_env()` 注入。
5. `src/runtime/agent.py`：新增 `_apply_local_worker_base_override()`，本机/内网 worker
   覆盖 `RUNTIME_SERVER_URL` 与 `task["server_url"]`；业务基址缺失或与回连基址同源
   （单地址部署，`_same_url_host` 按 scheme+host+port+path 判定）时仍覆盖为本机直连，
   双地址部署（不同端点）保留显式业务基址。
6. `src/user/db.py` / `src/api/routes/health.py`：`task_business_api_url` 并入
   `get_url_split_warning` / `get_server_base_url_warning` 与 health 输出。

## 风险与缓解
- 业务基址从 worker 不可达 → `task_business_api_url` 显式覆盖。
- 存量部署未配置 → 回退 `server_base_url`，与旧行为等价。
- A2A 回环 → 保留 `task["server_url"]` 覆盖并加回归用例。