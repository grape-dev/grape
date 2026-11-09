# Issue #1577 Spec：worker 任务派发 env 语义拆分

## 背景
worker 任务内 `GRAPE_SERVER_URL` 被注入为 worker runtime 基址（仅 54 个 runtime 端点），
导致 `grape issues ...` / `grape git-repo ...` 等业务命令报 `No such command`，Agent 需自行
试错 `--base-url` 才能恢复。根因是同一变量承载了两种语义。

## 目标
- `GRAPE_SERVER_URL` 注入主控业务 API 基址（全量 261 端点）。
- `RUNTIME_SERVER_URL` 独立承载 worker runtime / A2A 回连基址。
- worker 执行期不再用 runtime 地址覆盖 `GRAPE_SERVER_URL`。
- 单地址部署向前兼容，env 与改造前等价。

## 范围
- In：`src/prompt_builder.py`、`src/api/event_pipeline.py`、`src/api/routes/chat.py`
  （Web 对话入队同源注入）、`src/im/handler_async.py`（飞书 IM 对话入队同源注入）、
  `src/im/vod_handler_async.py`（VOD 入队）、`src/runtime/scheduler.py`（autopilot 入队）、
  `src/runtime/agent.py`、`src/user/db.py`、`src/api/routes/health.py`、
  `src/config.py`、新增单测、`help/usage/agent-environment-variables.md`。
- Out：grape-cli refresh 告警、grape-skills 文档（另仓承接）。

> 对话入队定义：Web chat、飞书 IM chat、VOD、autopilot 四条入队链路均注入
> `GRAPE_SERVER_URL` / `RUNTIME_SERVER_URL`（统一走 `resolve_task_server_env`），
> 保证远端 worker 上任务内业务命令免 `--base-url` 可用。

## 验收标准
- [ ] 双地址部署：新任务 `GRAPE_SERVER_URL`=业务 API，`RUNTIME_SERVER_URL`=worker runtime。
- [ ] 任务内 `grape issues detail` / `grape git-repo issues get` 免 `--base-url` 可用。
- [ ] 本机/内网 worker A2A 直连覆盖不回归；单地址部署 env 等价。
- [ ] 单测覆盖双地址/单地址/回退，现有测试全绿。