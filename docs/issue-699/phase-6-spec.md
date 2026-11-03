# Phase 6 Spec — 部署双模式验证（Issue #699）

> 权威设计：docs/weekly-09/grape-unified-implementation-design.md **v2.2** §12 Phase 6 + §14 TC-6.1~TC-6.5

## 目标

交付四进程部署的**双模式**运行能力并完成验证：

1. **单机模式**（`GRAPE_MODE=single` + `DEVOPS_DB_BACKEND=sqlite`）：四进程同主机，
   SQLite + 表前缀（`mgmt_` / `rt_` / `ins_`）模拟三 schema，WAL + busy_timeout 防
   `database is locked`。
2. **集群模式**（`GRAPE_MODE=cluster` + `DEVOPS_DB_BACKEND=mysql`）：MySQL 三 schema
   （`grape_mgmt` / `grape_runtime` / `grape_insight`），RemoteProvider 经 `/internal/*`
   跨进程访问。
3. **Docker Compose**：四应用容器 + nginx 反代容器（原单容器 `backend` 布局按设计改造）。
4. **部署文档 / profile**：`configs/profiles/single.conf`、`cluster.conf`、
   `scripts/dev_four_process.sh`。
5. **全量回归**：pytest 全量（仓库无 `verify_all.sh`，以 pytest 为准）。

## v2.2 基线变更（本阶段必须对齐）

设计文档 v2.1→v2.2（weekly-09-ljt commit 616e757ad）：

- `chat_conversations` / `chat_messages` / `chat_agent_session` 三表归属
  **从 grape_mgmt 迁至 grape_runtime**（mgmt 核心 48→45 张、runtime 22→25 张）。
- 跨域方向反转：**Runtime 写**、**Mgmt 经 RuntimeService 读**（`get_chat_conversations()`
  / `get_chat_messages()`）。
- 实施落点：
  - `src/kernel/db.py` 表注册表三表移入 `_RUNTIME_TABLES`（迁移脚本
    `scripts/migrate_schemas.py` 按注册表自动跟随）。
  - `RuntimeService` 契约新增 `get_chat_conversations()` / `get_chat_messages()`，
    Local/Remote 实现同步填充；Worker 进程 `/internal/chat/*` 端点暴露（集群通道）。
  - nginx `/api/chat/` 分流到 Worker（Phase 4 已就绪）保持一致——chat 执行端点归 Worker。

## 范围外

- MySQL 真机集群验证（本环境无 MySQL 实例）：交付脚本 + 静态校验 + profile，真机验收
  留待运维环境（PR 描述注明）。
- systemd 运行态链路（本环境非 systemd 托管）：四 service 单元静态校验（Phase 4 TC-4.8
  已覆盖，本阶段 TC-6.4 复核）+ 等价脚本 `dev_four_process.sh` 验证四进程独立启停。
- Docker 运行态验证（本环境无 docker）：compose 结构静态校验（YAML 解析 + 服务/端口/
  卷/healthcheck/nginx 挂载断言）。

## 交付物

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/kernel/db.py` | 修改 | chat 三表迁入 `_RUNTIME_TABLES`（v2.2） |
| `src/domains/contracts/runtime_service.py` | 修改 | 新增 `get_chat_conversations` / `get_chat_messages` 抽象方法 |
| `src/domains/contracts/local/runtime_local.py` | 修改 | Local 实现（`t_runtime('chat_*')` 前缀/schema 无感） |
| `src/domains/contracts/remote/runtime_remote.py` | 修改 | Remote 实现（`/internal/chat/*`） |
| `src/domains/runtime/routes/internal.py` | 修改 | Worker 暴露 `/internal/chat/conversations{,/messages}` |
| `scripts/dev_four_process.sh` | 新建 | 四进程开发模式启动（TC-6.1/6.2 验证命令入口） |
| `configs/profiles/single.conf` | 新建 | 单机模式 profile |
| `configs/profiles/cluster.conf` | 新建 | 集群模式 profile |
| `deploy/docker-compose.yml` | 修改 | 单容器 → 四容器 + nginx |
| `deploy/nginx-grape-compose.conf` | 新建 | compose 版 nginx 分流（upstream 指服务名） |
| `tests/test_issue_699_phase6_deployment.py` | 新建 | TC-6.1~TC-6.5 自动化验收 |
| `docs/issue-699/phase-6-{spec,plan,task}.md` | 新建 | 本阶段文档 |

## 验收标准（§14）

- **TC-6.1**：`dev_four_process.sh` 单机模式四进程全部健康（:38080-38083），日志无
  `database is locked`，`rt_chat_*` 前缀表落库。
- **TC-6.2**：集群模式注册表→`grape_runtime.chat_*` 限定名；`/internal/chat/*` 挂载
  Worker；cluster profile 齐备；缺 MySQL 配置快速失败。
- **TC-6.3**：compose 四容器 + nginx 结构、分流配置与四进程版一致。
- **TC-6.4**：四 systemd 单元 ExecStart/端口正确。
- **TC-6.5**：pytest 全量通过。
