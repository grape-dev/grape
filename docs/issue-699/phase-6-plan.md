# Phase 6 Plan — 部署双模式验证（Issue #699）

## 实施顺序

### 1. 基线与 v2.2 对齐（前置）
- rebase `feat/issue-699` 到 `origin/weekly-09-ljt`（含 616e757ad 设计 v2.2）。
- 表注册表三表迁移 + 契约接口 + internal 端点 + 迁移脚本联动验证（注册表为单一事实源）。

### 2. 部署产物
- `scripts/dev_four_process.sh`：profile 加载 → 模式环境导出 → sqlite WAL/busy_timeout →
  四进程后台启动 → 分进程健康轮询（Mgmt/Insight `/api/health`，Event/Worker `/health`）→
  locked 日志检测 → trap 统一回收。cluster+mysql 缺连接配置即快速失败（不静默回退）。
- `configs/profiles/{single,cluster}.conf`：环境变量 profile（systemd `EnvironmentFile`
  与脚本 `PROFILE=` 双用途）。
- `deploy/docker-compose.yml`：4 应用服务（各自 `command` 指定入口模块 + 端口 38080-38083、
  `GRAPE_PROCESS_ROLE`、healthcheck、共享 `grape-data` 卷）+ nginx 服务（挂
  `nginx-grape-compose.conf`，upstream 指服务名，80 对外）。
- `deploy/nginx-grape-compose.conf`：由四进程版派生（upstream→服务名、listen 80、去 TLS/跳转块），
  路径分流逻辑与 `nginx-grape-four-process.conf` 完全一致（`/api/chat/` → Worker + SSE 不缓冲）。

### 3. 验证（TC-6.1~TC-6.5）
- 单机实机：`dev_four_process.sh` 起四进程（临时 `DEVOPS_DB_PATH`），四健康端点 200，
  日志无 locked，sqlite 落库表前缀正确（`rt_chat_*` 归运行域）。
- 自动化：`tests/test_issue_699_phase6_deployment.py`（注册表/前缀映射/迁移保数据/
  契约 Local/internal 挂载/compose 结构/nginx 分流/systemd 静态/cluster 快速失败）。
- 全量回归：`pytest tests/ -q`（TC-6.5）。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| chat 表归属变更影响存量 mgmt 裸 SQL（cooldown/account 清理等） | SQL 经统一连接代理自动改写（`SchemaSQLiteDB.qualify_sql`），schema mode 关闭时零回归；契约口径只约束 Mgmt 读路径 |
| MySQL/systemd/Docker 本环境不可实装 | 等价静态校验 + 脚本交付 + PR 描述注明真机验收项 |
| 四进程健康端点不统一（/health vs /api/health） | 脚本双探测；compose healthcheck 按进程挂载端点（Event/Worker `/health`，Mgmt/Insight `/api/health`）；测试断言各自端口与端点 |
| uvicorn 首次启动慢（引擎探测 ~40s） | 健康轮询超时默认 60s 可调（`GRAPE_FOUR_HEALTH_TIMEOUT`） |
