# Phase 6 Task — 部署双模式验证（Issue #699）：检查清单

## 编码清单

### v2.2 对齐
- [x] src/kernel/db.py — chat 三表移入 `_RUNTIME_TABLES`（mgmt 45 / runtime 25 核心）
- [x] src/domains/contracts/runtime_service.py — `get_chat_conversations` / `get_chat_messages` 抽象方法
- [x] src/domains/contracts/local/runtime_local.py — Local 实现（t_runtime 前缀/schema 无感）
- [x] src/domains/contracts/remote/runtime_remote.py — Remote 实现（/internal/chat/*）
- [x] src/domains/runtime/routes/internal.py — Worker `/internal/chat/conversations{,/messages}`
- [x] scripts/migrate_schemas.py — 注册表单一事实源自动跟随（无需改动，验证联动）

### 部署产物
- [x] scripts/dev_four_process.sh — 四进程启动/健康/locked 检测/trap 回收
- [x] configs/profiles/single.conf — 单机模式 profile
- [x] configs/profiles/cluster.conf — 集群模式 profile
- [x] deploy/docker-compose.yml — 四容器 + nginx
- [x] deploy/nginx-grape-compose.conf — compose 版分流配置

### 测试
- [x] tests/test_issue_699_phase6_deployment.py — TC-6.1~TC-6.5 自动化（20 用例）

### 文档
- [x] docs/issue-699/phase-6-spec.md
- [x] docs/issue-699/phase-6-plan.md
- [x] docs/issue-699/phase-6-task.md

## 验证记录

- TC-6.1 单机：dev_four_process.sh 实机四进程全部 healthy（/tmp 临时库），
  无 `database is locked`，`rt_chat_agent_session / rt_chat_conversations / rt_chat_messages`
  前缀落库（mgmt_/rt_/ins_ = 50/23/11 张，含兼容表与存量表）。
- TC-6.2 集群：注册表→`grape_runtime.chat_conversations` 限定名；Worker `/internal/chat/*`
  挂载（非 404）；cluster 缺 DEVOPS_DB_HOST 快速失败；MySQL 真机验收留运维环境。
- TC-6.3 Compose：YAML 解析 + 五服务/命令/角色/healthcheck/卷/nginx 挂载/upstream 指服务名/
  `/api/chat/`→Worker、chat 读端点→Mgmt、healthcheck 命中各进程真实挂载端点（Event/Worker
  `/health`，Mgmt/Insight `/api/health`）全部断言通过；docker 真机 up 留运维环境。
- TC-6.4 systemd：四单元 ExecStart/端口/Restart 断言通过；systemd 运行态留运维环境。
- TC-6.5 回归：pytest 全量（见 PR 描述）。
