# Issue #3580 — 自建 GitLab 多实例接入（Spec 落档）

> 服务端已发布 Spec（issue 评论），本文件为 coding 阶段执行基线，内容与已确认 Spec 对齐。

## Objective

任一 HTTPS GitLab 实例（GitLab.com 或自建）以「一张平台卡」接入后，仓库发现、托管/解除托管、
webhook 事件、git 传输、PAT 凭据五条链路全部可用；新增实例只加卡/写 meta，不改代码、不加环境变量，
且不触碰登录渠道。

## Scope

- P1 卡体系多实例：`ca_bundle` 字段、卡名/host/实例 URL 校验、SCHEMA 文档与测试。
- P2 传输安全：`_run_git` 以 env 注入 Authorization / `sslCAInfo` / `followRedirects`，token 不进 argv。
- P3 事件持久队列：`grape_t_webhook_inbox`（幂等键、claim 租约、finish 退避）、端点先持久化再 200、
  Event 进程 worker 消费、`web_url` 与卡 host 强校验。
- P4 托管全流程 GitLab 化、`/api/repos/<provider>/connection` PAT。
- **前端（实例选择 / 托管 / PAT 连接页）本期未实现**：本 PR 变更集不含任何 frontend 文件，
  归入 P5（前端 Vue + locales + i18n 版本 bump），不随本 PR 交付。

Out of scope：GitLab OAuth 登录/身份绑定/登录白名单；组织级成员同步、分支保护、合并策略；
实例级 bot 身份与协作者邀请。

## Acceptance Criteria（R1–R12）

R1 一实例一卡；R2 卡名校验；R3 实例 URL 校验；R4 CA 卡字段与 git 注入；R5 分页发现 + Maintainer；
R6 托管冲突互斥；R7 解除托管失败保留本地记录；R8 bot token 不新增物理列；R9 argv 无 token；
R10 事件验签/幂等/重放/实例隔离；R11 PAT 不产生登录身份；R12 登录渠道无 gitlab 键。

端到端：配卡 → 列仓 → 托管 → MR 事件 → clone/fetch → 解除托管；实例隔离；同 delivery 三投只消费一次
且可重放；存量四平台回归无新增失败。
## Review fixes（PR #1868 复审，2026-11-20）

1. **Webhook 密钥语义（高危）**：GitLab 钩子的 `token` 只能是共享密钥。`setup_gitlab_webhook`
   删除 `webhook_password or api_token` 回落（会把 PAT/bot token 写进 GitLab 项目 webhook
   配置并对维护者可见，且 DB `webhook_secret` 为空导致所有投递 404）；`gitlab_v4` 家族在
   `get_hosting_provider_config` 中 `bot_token` 恒为空 → 所有调用点走路径 B（随机密钥）。
2. **对外契约**：通用 webhook 响应保持 `status="ok"`，入队信息以 `inbox_id` / `duplicate`
   新增字段表达（既有 `test_webhook_dynamic_routes` 不再红）。
3. **守卫回潮**：`find_existing_gitlab_webhook` 查询失败记 `warning`（原静默 `pass` 会静默
   重复建钩）；`server_tasks` worker 取消/超时记 `debug`。
4. **幂等与稳健**：`remove_gitlab_webhook` 上游 404 视为成功；`_run_git` 累加调用方已有
   `GIT_CONFIG_*`；通用 webhook 增 10MB 载荷上限（413）；收件箱 `payload` 改 `MEDIUMTEXT`
   （MySQL 64KB 上限）并补 claim 索引 `(status, next_attempt_at, lease_expires_at)`。
   收件箱 `last_error` 原为 `TEXT NOT NULL DEFAULT ''`——MySQL 不接受 TEXT 字面默认值，
   兼容层剥掉后严格模式下入队必报 1364（SQLite 测不出来）；改为可空 `MEDIUMTEXT`，
   入队显式写 `''`，存量库由迁移 `202611200004` 收敛并回验 `IS_NULLABLE`。
5. **GitLab hooks 单源**：新增 `src/kernel/vcs_gitlab_hooks.py`（project_path / find /
   setup / update / remove），mgmt 托管三件套与 `GitLabProvider` 三个同名方法都改为薄委托，
   消除"两版实现语义分叉"（此前一版用 `token=webhook_secret`、一版用 `or api_token`）。
6. **P3 覆盖全部入口**：gitcode / github 专用端点与通用路由统一走
   `webhook.persist_webhook_event()` → 先入队再 200，入队失败 500 让上游重投；
   gitee / gitlab 走通用路由本就覆盖。
7. 回归用例：`tests/runtime/test_issue_3580_review_fixes.py`（密钥必填/禁止 token 回落、
   gitlab_v4 强制路径 B、404 幂等、GIT_CONFIG 累加、hooks 单源对拍、
   gitcode/github 端点入队与幂等）。
