# 自建 GitLab 实例接入（Issue #3580）— 设计

需求见 `gitlab-instance-requirements.md`。本文只记**决策与落点**，每条带实现位置与验证方式。

## D1 实例 = 一张平台卡 + 一行平台对象（不是环境变量）

- 一实例一 `provider` key（`gitlab-corp`），它同时是 `repo_key` 前缀、webhook 路径段
  （`/webhook/gitlab-corp`）、凭据行的 provider、平台目录的 provider。
- 卡的定义字段（`dialect/host/api_base/api_auth/webhook_path/event_header/webhook_verify/
  webhook_dialect/clone_auth/ca_bundle/...`）与 `grape_t_repo_platforms` 列 1:1
  （`src/api/platform_object_ddl.py:43-95`；卡列清单 `platform_card._PLATFORM_CARD_COLUMNS`）。
- **不采用** PR #1854 的 `GRAPE_GITLAB_INSTANCES` + `gitlab@corp`：env 是第二真源、需重启、
  `@` 会渗进 webhook 路径与白名单；一实例一卡可审计、可禁用、可与登录渠道正交。
- 验证：S1（`/settings/vcs/platforms` 出该实例）、S3（`gitlab-corp:grp/sub/proj` 前缀与寻址）。

## D2 配置真源顺序：DB 对象 > 平台卡文件 > 代码内置

- 运行时读口：`platform_card._build_cards()`（meta `vcs_providers_config` > `configs/platforms/*.json`
  > 内置默认）叠加 `_raws_from_table()`（D109 表行，`enabled_only`）。
- 用户面目录：`src/domains/platform/registry.py::platform_descriptors()` **表优先**，表空/未迁移回落卡。
- 新增实例卡的写入口 = `PUT /api/admin/vcs-platforms`（`src/domains/mgmt/admin/routes.py:886`）：
  Builder 全字段校验 → host / webhook_path 全局唯一校验 → 写 meta `vcs_providers_config`
  → `invalidate_platform_card_cache()` → `audit_logs(admin_vcs_platforms_saved)`。
- **写卡即落行（本批新增）**：路由在 meta 写入之后，对每个 provider 调
  `RepoPlatformRepo.upsert_manual()` 落一行 `source='manual'`（字段先按表列过滤——卡里的
  `token_env`/`event_map`/`fields`/`global_bot_meta_key` 等不是列，不过滤会被未知键
  fail-closed 拒掉），再失效一次卡缓存。**写卡是同一个请求内完成"卡 + 行"，用户面目录与
  凭据 tab 立即出现该实例，不需要重启。**
- 为什么不能靠"立即导入"：`importer.load_repo_platforms()` 只 glob
  `configs/platforms/*.json`（**不读 meta `vcs_providers_config`**），
  `platform_descriptors()` 又只在**表为空**时回落平台卡——而表里已有内置平台行，
  所以纯 meta 卡永远进不了目录。立即导入跑一次也是导入文件，补不上 meta 卡。
- 文件派生行不被改写：`upsert_manual` 遇到 `source != 'manual'`（即
  `configs/platforms/<p>.json` 派生）返回 `config_owned` 且不写，响应里 `rows` 可见；
  该 provider 的定义真源仍是配置文件（防双真源，下次启动导入不会被冲掉）。
- 落行失败不阻断卡保存：响应带 `rows_error`（同时 `log.warning`），避免"卡存成功但目录
  没实例"被静默当成已接入。
- 已知技术债（登记，不在本议题修）：`/api/admin/vcs-platforms` 是 D109 之前的老卡视图，
  对象模型批次接管时把"新增实例"迁到 `/api/admin/repo-platforms` 的显式创建动作，
  并守住 §23 契约（无父容器 / 无整卡 PUT）。

## D3 实例卡没有代码模块时按方言族回落

- `providers/__init__.py::module_for()`：`_MODULES` → 卡 `resources` 段（card_module 垫片）
  → **方言族代理** `FamilyProviderModule(card, _MODULES[card.dialect])`；`API_BASE` 取卡。
- 为什么必须代理而不是直接返回族模块：族模块的 `API_BASE` 是内置域名（gitlab.com），
  直接返回会把实例仓的凭据打到公网。
- 验证：S3 网关寻址、`test_issue_3580_namespace_routes.py::test_vcs_url_quotes_full_namespace_path`。

## D4 多级 namespace（子组仓）

- `repo_key` 解析：GitLab 方言族按**最后一段**切分（`grp/sub/proj` → owner=`grp/sub`）；
  其它平台首个 `/`（零回归）。判据 = 卡 `dialect == "gitlab"`（`repo_key._namespace_provider`）。
- 网关路由：既有两段式 `/{owner}/{repo}` 之后批量追加 `{owner:path}` 变体（斜杠直连 + `/-/` 分隔），
  **按路径长度降序、`/-/` 先注册**（星网址点贪婪）；`include_in_schema=False` → OpenAPI/CLI 零变化。
- 上游 URL 无需改：`repo_segment` 已是 `quote(f"{owner}/{repo}")` 全路径编码。
- 验证：S3。

## D5 传输：私有 CA + 不跟随重定向（单一入口）

- `src/kernel/upstream_http.py`：按请求 URL 的 `api_base` 前缀找卡 → `ca_bundle` 建 SSL 上下文；
  GitLab 方言族 `allow_redirects=False`（3xx 直接 `HTTPError`，避免 `PRIVATE-TOKEN` 被带到异域）。
- 收口四处：provider 模块 `request` / `token_resolver._http` / `VcsApiClient._do_request` /
  `kernel.vcs_gitlab_hooks._request`。非 GitLab 平台保持系统 CA + 跟随（零回归）。
- git 传输层沿用 `repogit._transport_config_for`（卡 `ca_bundle` → `GIT_CONFIG sslCAInfo`）。

## D6 方言端点补齐：树 / 文件 / 指派

- 树：`/repository/tree?path=&ref=&recursive=1&per_page=`（模块钩子 `tree_url`/`dir_url`，
  含翻页与 `TREE_RECURSIVE`）；文件：`/repository/files/{整路径 URL 编码}?ref=`，
  404 回落目录列表（网关"文件/目录同端点"语义）；条目归一 `{type,mode,path,name,sha,size}`
  / 文件 `{...,content(base64),encoding}`（`providers/gitlab.py::normalize_tree/normalize_file`）。
- 指派：`assignee_ids`（数值直通；用户名经 `GET /users?username=` 精确匹配；解析不到只告警）。
- 触发条件走模块钩子（`_repo_path_hooks`），路由层无平台名分支。

## D7 凭据与 bot

- 仓库级 PAT：用户面 PAT 走 `PUT /api/settings/credentials {platform_tokens: {"gitlab-corp": …}}`，
  解析序 ①`<p>_pat` ②OAuth（gitlab 无登录渠道 → 实际只有 PAT）。
- 实例 bot（可选）：平台卡 `bot_username` + `grape_t_repo_platforms.bot_token_enc`（D110），
  托管路径 A 复制到仓库级凭据并带 `derived_from`。
- webhook 密钥：GitLab 家族强制路径 B（随机密钥），**禁止回落 api token**
  （`repo_manager.py:909-911`；`vcs_gitlab_hooks.setup_hook` 只收 `secret`）。

## D8 事件接入

- 每实例 webhook 路径 `card.webhook_path` → 卡驱动路由 `/webhook/{provider}`；验签 `token_compare`
  对卡/仓库级 secret；载荷方言 `gitlab_v4`（`object_kind`/`action` → 中性事件）。
- 入队：全平台通用 `grape_t_webhook_inbox`（幂等键 `(repo_key, delivery_id)`、claim 租约、
  指数退避、dead 保留现场）；迁移 `202611200003`（建表）+ `202611200004`（last_error 可空）。

## D9 可见性与开关

- `enabled`（平台行）= 用户面目录可见性；**不门禁**托管/列仓/凭据解析（内网仍可用）。
- 隐藏 ≠ 关闭；`is_platform_visible()` 单一判据（本议题不改动该语义）。

## D10 最小配置：方言默认值（B 方案）

- `platform_card.build_platform_card` 新增 `_DIALECT_DEFAULTS`：`dialect=gitlab` 时，
  键缺席的字段按方言默认派生 —— `api_base=https://{host}/api/v4`、`api_auth=private_token`、
  `clone_auth=oauth2`、`webhook_dialect=gitlab_v4`、`event_header=X-Gitlab-Event`、
  `download_base=https://{host}`；`webhook_path`/`name`/`webhook_verify`/`pat_create_url`
  原本就有默认或按 host 推导。
- 所以接入一个自建实例的最小请求是 **`{"<provider>": {"dialect": "gitlab", "host": "<host>"}}`**。
- 为什么只有 gitlab 给默认：该方言当前只有 `gitlab_v4` 一族 webhook（1:1），按方言推导不会
  换族；github 方言下 gitcode/gitee 实为 `gitee_v5`，D105-19 明确禁止推导。
- 显式给值一律优先（少数自建实例 API 前缀不同可覆盖）；校验不放松（非 http、相对 CA 路径、
  host/webhook_path 撞车仍 400）。

## D11 端到端校验脚本

- `scripts/probe_gitlab_instance.py <provider>`：读卡 → ①token 校验（`/user`）②列仓（`/projects?membership=true` 首页）
  ③namespace 寻址（`/projects/{owner%2Frepo}/repository/tree?ref=HEAD`）④文件读（README 或指定路径）
  ⑤建钩 dry-run（只构造请求不发送，打印 URL/字段）⑥CA 状态（卡 `ca_bundle` 是否可加载）。
  输出六行 `PASS/FAIL/SKIP` + 结论；`--json` 供机器读；`--self-test` 用内置假上游跑通（单测用）。
- 目的：把"实例对不对"的判定从人肉试变成一条命令，替代 PR #1854 的 `scripts/verify_gitlab.py`。

## 验证清单（收口）
1. `tests/runtime/test_issue_3580_instance_card_via_admin_config.py`（S1/S2）
2. `tests/runtime/test_issue_3580_probe_script.py`（S7）
3. 既有 26 条（S3–S6）逐文件隔离复跑
4. `tests/arch/test_migration_version_integrity.py` + `tests/api/test_migration_*`（迁移未动，确认无回归）
5. 文档样例字段与 `platform_object_ddl.py` 列一致（grep 双向）

## D12 建仓到组/子组（一键创建项目）

- 后端：`GitLabProvider.create_repo(owner=...)` 先 `GET /namespaces?search=<组路径>` 精确匹配
  `full_path` 得 `namespace_id` 再 `POST /projects`（GitLab 建仓只认数字 id）；`initialize_with_readme`
  默认 true；解析不到 → 明确错误，绝不退化成"建到个人空间"。
- 空间清单：新端口方法 `list_create_spaces()`（基类默认空 = 只有个人空间）；GitLab 实现
  `GET /groups?min_access_level=30`（Developer 及以上可建仓），返回 `full_path`。子组不在默认返回内，
  由表单允许直接填完整路径覆盖（输入即校验）。
- 路由：`GET /api/repos/create-targets?provider=`（个人登录名 + 空间清单）、
  `POST /api/repos/create` 的 owner 改为段级校验（允许 `group/sub`，禁点段/空段/首尾斜杠）、
  `GET /api/repos/check-name?provider=&owner=` 按空间查重（否则子组同名仓误判占用）。
- 建仓响应回填：`full_name` 用 `rpartition` 取 owner（`grp/sub/proj` → `grp/sub`），
  split 首个 `/` 会把 owner 截成 grp → webhook 路径拼错、托管 404。
- 前端：创建弹窗新增"所属空间"下拉（默认个人空间），换平台即重拉清单；提交带 `owner`；
  查重带 `provider/owner`。样式复用输入框规则；locales 新增 3 键并 bump
  i18n 指纹/index cacheVer，`all-views.css?v=` 与前端版本棘轮同批抬版。
- 用例：`tests/runtime/test_issue_3580_create_space.py`（6 条：namespace 解析/无 owner 零查询/
  空间不存在不建/空间清单/路径校验/full_name 回填）。