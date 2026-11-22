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

## 能力补齐（PR #1854 二轮对账，2026-11-20）

对上一条 PR #1854 逐能力复核后确认四项真缺口，本批一次补齐（其余差异保留原口径：
前端 UI 归 P5、文档/探针脚本不入主干）：

1. **多级命名空间寻址**：GitLab 仓库全路径是 `<组>/<子组>/<仓>`，此前 `parse_repo_key`
   按首个 `/` 切分（owner=组、repo=子组/仓），且 git-repo 网关只有两段式
   `/{owner}/{repo}` 路由，子组仓在树/文件/分支/PR 文件等页面不可达。现按卡方言
   （`dialect=gitlab`）末段切分，并在既有两段式路由之后追加 `{owner:path}` 变体
   （斜杠直连 + `/-/` 分隔两种形态；星网址点贪婪，故按路径长度降序且 `/-/` 先注册）。
   `include_in_schema=False`：OpenAPI/CLI 命令面零变化。
2. **实例卡模块解析**：自建实例卡（`gitlab-corp` 一类）既无代码模块也无 `resources` 段，
   `module_for()` 返回 None → 实例仓的网关调用全量 502。现按卡方言回落到同族代码模块
   （`FamilyProviderModule` 代理），`API_BASE` 取卡的实例 host。
3. **API 侧私有 CA + 不跟随重定向**：`ca_bundle` 此前只进 git 传输层，API 侧
   （列仓/托管/建钩/Issue/MR）走默认 SSL 上下文 → 私有 CA 自建实例 TLS 全失败；且带
   `PRIVATE-TOKEN` 的 3xx 会被 urllib 把 header 带到异域。新增
   `src/kernel/upstream_http.py` 统一入口（按卡取 CA、GitLab 方言族不跟随重定向），
   provider 模块 / token_resolver / api_client / 托管钩子四处收口；其它平台保持存量语义。
4. **仓库树/文件内容 + 按用户名指派**：GitLab 没有 `git/trees`、`contents` 端点，模块钩子
   改发 `/repository/tree`、`/repository/files/{整路径编码}`（含递归与翻页、文件→目录 404
   回退），条目归一成网关契约；建 Issue 的 `assignee` 由用户名解析为
   `assignee_ids`（纯数字直接采用、解析不到只告警不阻断）。
5. 回归用例：`test_issue_3580_namespace_routes.py`（8）、`test_issue_3580_gitlab_repo_paths.py`（8）、
   `test_issue_3580_upstream_tls.py`（6）、`test_issue_3580_gitlab_assignee.py`（4）。

## 实例接入（自建 GitLab，2026-11-20）

自建实例的接入口径、场景用例与决策见同目录两份文档：`gitlab-instance-requirements.md`（S1–S8 场景）与
`gitlab-instance-design.md`（D1–D10 决策）。要点：实例 = 一张平台卡（`provider` 即 repo_key 前缀），
接入走既有 `PUT /api/admin/vcs-platforms`（Builder 校验 + host/webhook_path 唯一 + 审计），
**不新增平台对象写接口、不新增前端页面**；接入后用
`python scripts/probe_gitlab_instance.py <provider> --token <PAT> --repo <owner>/<repo>` 六项自检
（token / 列仓 / namespace / 树 / 文件 / 建钩 dry-run / CA）。
