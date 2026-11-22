# Issue #3580 — 方案设计（Plan）

## 关键设计

| 决策 | 选择 | 理由 |
|------|------|------|
| 多实例命名 | 卡名即 provider（`gitlab-corp`） | 不渗进 webhook 路径/前端/白名单 |
| 事件队列落点 | runtime 域 `grape_t_webhook_inbox` | 入队与消费同在 Event 进程 |
| 实例 CA | 卡字段 `ca_bundle` | 改卡即生效，无环境变量 |
| 凭据语义 | `user_credentials(kind='pat')` 单层加密 | 对齐归位模型 |

## 实现路径

1. **卡层**（`src/domains/runtime/vcs/platform_card.py`）：新增 `ca_bundle`；provider 名
   `[A-Za-z0-9_-]{1,64}` 且拒绝 `@`；host 裸主机名可带端口；`api_base`/`download_base`
   https、无内嵌凭据/query/fragment/`..`；CA 必须绝对路径且文件存在。
2. **传输层**（`repogit._run_git`）：改用 `GIT_CONFIG_COUNT` + `GIT_CONFIG_KEY_i/VALUE_i`
   注入 `http.extraHeader` / `http.sslCAInfo` / `http.followRedirects=false`，token 不再进 argv；
   新增 `repo_key`/`ca_bundle` 入参，调用点传入 repo_key。
3. **事件层**：runtime DDL 新增 `grape_t_webhook_inbox`；`webhook_inbox` 领域模块提供
   `enqueue/claim/renew/finish`；通用 webhook 验签后校验 `web_url` host 与卡 host 一致，
   再持久化；Event 进程 `start_event_tasks` 启动消费 worker，调用既有 `run_pipeline`。
4. **Provider 层**：`GitLabProvider` 接受任意卡实例（`gitlab-corp`）；`create_provider`
   对 `dialect=gitlab` 的卡统一走 GitLabProvider；`fetch_repos_with_role` 全量分页，
   `max(project_access, group_access) >= 40` 判 Maintainer。
5. **托管层**：新增 `gitlab_v4` webhook 家族（`/projects/{id}/hooks`），gitlab 卡声明
   `webhook_dialect=gitlab_v4` 并开启托管全流程。
6. **凭据层**：`/api/repos/{provider}/connection` GET/PUT/DELETE，仅写
   `user_credentials(kind='pat')`，不写 `external_identities`。
7. **前端**：Repos 页平台实例选择复用平台卡目录；托管/连接操作按 provider 传参。

## 影响模块

- `configs/platforms/gitlab.json`、`SCHEMA.md`
- `src/domains/runtime/vcs/platform_card.py`、`providers/gitlab.py`
- `src/kernel/token_resolver.py`、`src/kernel/repo_key.py`
- `src/domains/runtime/engine/repogit.py`
- `src/domains/runtime/schema_ddl.py`、`src/api/db_setup.py`、`src/domains/runtime/webhook_inbox.py`
- `src/domains/runtime/routes/webhook.py`、`src/api/server_tasks.py`
- `src/domains/mgmt/routes/repos.py`、`src/domains/mgmt/user/repo_manager.py`
- `src/api/frontend/app/src/views/ReposView.vue` + i18n

## 测试策略

- 卡校验参数化（非法名/host/URL/CA）。
- `_run_git` argv 断言无 token、env 含 Authorization/sslCAInfo。
- `GitLabProvider` 分页与 access_level 判定。
- inbox 幂等入队、claim 租约、finish 退避。
- 通用 webhook `web_url` host 不匹配拒绝。
- connection 端点读写 PAT 且不写身份行。
- 存量四平台回归：`pytest tests/runtime/test_platform_card.py tests/runtime/test_s72_gitlab_provider_chain.py`。