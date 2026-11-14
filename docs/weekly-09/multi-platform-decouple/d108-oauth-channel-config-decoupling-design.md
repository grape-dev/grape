> ⚠️ 本文为批次方案稿（D108 实施设计）。索引导航：./multi-platform-decouple-design.md
> 域：登录渠道（OAuth）配置面——与仓库托管配置分层、文件真源、代码零平台卡。
> 全局约束：继承索引页「表设计铁律/凭据续期口径/验收纪律」。

# D108 登录渠道配置解耦（oauth-providers.json 真源 + 与托管配置分层）

## 1. 问题（三条实锤）

1. **仓库侧已解耦，登录侧没解耦**：`configs/platforms/*.json` 早就是仓库平台卡的
   文件真源（`platform_card.py:528` `_builtin_raws_from_dir`，代码 `_DEFAULT_CARDS_RAW`
   只作兜底）；而登录渠道卡全部硬编码在 `src/domains/mgmt/auth/providers.py` 的
   `_DEFAULT_PROVIDERS` 里，加渠道 = 改代码 + 发版。更糟的是该常量在文件里被**定义了
   两遍**（历史重复，后者覆盖前者），第一份只含 gitcode/github——任何只改第一份的
   修改都不会生效。
2. **配置面向使用者不可用**：`configs/oauth-providers.example.json` 只有 gitcode/github
   两段，gitee 与华为没有任何样例；而且文件的迁移语义是"协议参数整块覆盖写
   `oauth_providers_config`"（`user/db.py:3282` `_sync_providers_from_config`），
   谁拿它建文件重启，DB 里的 gitee/huawei 卡会被整块抹掉。
3. **真文件被 gitignore**：`.gitignore` 第 149 行忽略 `configs/oauth-providers.json`。
   如果把它当真源又不移除该规则，新部署不会有渠道卡 —— 登录全线不可用（部署靠手工
   复制 = 手工步骤，与"要自动不要手动"的口径冲突）。

## 2. 目标形态（与仓库侧同构）

| 面 | 文件真源 | 运行时覆盖 | 代码兜底 |
|---|---|---|---|
| 登录渠道 | `configs/oauth-providers.json`（一份聚合，全部渠道） | `meta.oauth_providers_config`（逐键合并） | **无**（fail-closed） |
| 仓库托管 | `configs/platforms/<provider>.json`（每平台一文件） | `meta.vcs_providers_config`（逐键合并） | `_DEFAULT_CARDS_RAW`（仅目录缺失时） |

分层纪律（本次新增守卫锁死）：两类配置字段**互不渗透**。
- 登录侧合法键：`name/type/channel_type/auth_url/token_url/user_url/scope/id_field/
  login_field/name_field/email_field/avatar_field/pkce_enabled/brand_color/
  accept_header/emails_endpoint/claims_map/idp_sso_url/attribute_map`
- 托管侧能力位：`dialect/host/webhook_path/event_header/api_base/api_auth/
  webhook_verify/webhook_dialect/clone_auth/issue_write_form/branch_create_form/
  pr_head_filter/pr_comment_endpoint/pr_labels_via_issues/git_tree_family/
  token_storage_field/webhook_token_field/session_token_field/pat_create_url/
  global_bot_meta_key/global_bot_status_meta_key/download_base/user_images_base/
  bot_username/token_env`

## 3. 实现

1. **配置真源**：新增 `configs/oauth-providers.json`（四渠道 gitcode/github/gitee/huawei，
   不含任何凭据）。`.gitignore` 移除该忽略项，并注明与 `configs/platforms/*.json`
   同口径（运行必需默认数据入库；凭据不进文件）。
2. **加载器**（`src/domains/mgmt/auth/providers.py`）：
   - 删除两份 `_DEFAULT_PROVIDERS` 字面量，代码里平台卡归零；
   - `_builtin_channels_from_file()`：顶层必须 `{provider: card}`；未知键**整卡拒绝**
     （error 级响亮，静默丢键=能力静默丢失）；缺 `auth_url/token_url/user_url`
     整卡拒绝；`client_id/client_secret` 出现则**剥离并告警**（凭据唯一落点=DB meta/管理面）；
   - 文档键约定：下划线前缀键（`_readme`/`_modes`/`_note`）跳过，不当渠道名、不参与校验；
   - `_load_providers()`：文件卡 → `meta.oauth_providers_config` 逐键覆盖（D105-19 同型，
     缺席键落文件卡）→ 缓存（TTL 300s，管理面保存主动失效）。
3. **管理面保存基线**（`src/domains/mgmt/admin/routes.py` `auth_channels_put`）：
   `merged = builtin_channels()` 取代 `dict(_DEFAULT_PROVIDERS)` —— 保存动作不得把
   "代码里的平台"写回 DB，否则解耦被管理面悄悄撤销。
4. **example 说明**（`configs/oauth-providers.example.json`）：
   - `_readme`（7 条）：用法、真源链、凭据去哪、redirect_uri 留空语义、分层纪律、
     合法键表、文档键约定；
   - `_modes`：支持的协议模式说明（见 §4）；
   - `_fields`：逐字段语义（必填项、取值族、与消费面的关系）；
   - `_huawei_impl`：华为在代码里的既有适配点（`{access_token}` 占位、avatar_field、
     无登录名用 openID、id_token 兜底只在 type=oidc 时生效、不认 PKCE、AGC 回调、图标键等价）；
   - 每渠道 `_note`：各自坑位（Gitee 合法 scope 集与"每次弹授权页"根因、GitHub 记住
     授权与 `/user/emails` 补抓、华为占位/openID/AGC 回调登记）；
   - `test_shipped_example_file_is_loadable` 保证 example 可直接复制成真文件加载。

5. **配置文件的注释约定（两侧统一）**：JSON 无注释语法，两类配置统一用下划线前缀键承载
   文档（`_readme` / `_fields` / `_modes` / `_note` / `_huawei_impl`）。
   - 登录侧：`_builtin_channels_from_file` 剥离文档键后校验；
   - 托管侧：`platform_card._builtin_raws_from_dir` 新增同样剥离（原先直接做键集校验，
     往平台卡里加注释会"未知键整卡拒绝"）；
   - 守卫同步：`test_s83_file_keys_subset_of_dataclass` 的未知键判定排除 `_` 前缀；
     `test_platform_card_files_ignore_doc_keys` 断言四平台卡仍能从文件加载且不含 OAuth 端点。

## 4. 协议模式（type 字段，代码既有能力，本次补文档）

| type | 实现位置 | 状态与要点 |
|---|---|---|
| `oauth2`（默认） | `channel_strategy.py:124` | 授权码流程，`grant_type=authorization_code` 唯一；PKCE 按平台开关；必填三端点 |
| `oidc` | `channel_strategy.py:174` | claims 默认 `sub/preferred_username/name/email`，卡 `claims_map` 可覆盖；userinfo 失败时解码 id_token payload 兜底（不验签，token 由 IdP 签发+TLS 保证）；scope 一般含 openid |
| `saml` | `channel_strategy.py:242` | **骨架**：`idp_sso_url` + `attribute_map` 必填，POST 回调（ACS）；验签走 `verify_assertion` 钩子，未配置即拒绝登录（安全默认）；需真实 IdP 元数据联调 |
| 未注册 type | `register_channel_strategy` 校验 | 构建期直接拒绝，不静默回退 oauth2（协议错配=安全事故） |

不支持：`password` / `client_credentials` / `device_code`（`exchange_token` 只发
authorization_code）；标准 oauth2 渠道不做自动 refresh（仅 OIDC 策略按需调用）。

**华为注意**：当前卡 `type=oauth2`，**不触发** id_token 兜底；若 `/user` 端点返回不理想，
需改 `type=oidc` + `claims_map`（`id_field: sub`、`login_field: preferred_username`）。

## 5. 附带修正（同批，均有反证用例）

- **Gitee 每次弹授权页的根因**：Gitee 官方 OAuth 文档原文——跳过授权页要求"请求 scope 与
  用户上次授权勾选范围一致"。旧卡 `scope="user_info emails"` 里的 `emails` 照抄 GitHub 的
  `user:email`，不在 Gitee 合法集（`user_info/projects/pull_requests/issues/notes/keys/
  hook/groups/gists/enterprises`）内 → 每次重新授权。改为 `user_info projects`。
- **PKCE**：Gitee/华为 authorize 参数表无 PKCE，`pkce_enabled` 一律 `false`（GitHub/GitCode 保持 true）。
- **华为渠道卡缺失**：原来只有管理面 `huawei_oauth_client_id/secret` 字段映射，没有卡，
  填了凭据也 `400 不支持的平台: huawei`。补齐卡：`user_url` 带 `{access_token}` 占位
  （GOpen 只认查询参数、忽略 Authorization 头，`get_user_info` 已有占位支持）、
  `id_field=login_field=openID`（华为不返回登录名）、`avatar_field=headPictureURL`。

## 6. 迁移与兼容

- 老行为（meta 有配置 → **整块替换**）改为**逐键合并**：meta 只覆盖它声明的键，缺席键落文件卡。
  存量 meta 是管理面全字段 dump，语义不变；但文件新增能力位不再被 stale dump 抹掉。
- `configs/oauth-providers.json` 缺失时 = 无渠道 + error 日志（fail-closed）。
  **有意不放回代码兜底**：放了等于把平台塞回代码，解耦重被撤销。
- 旧的"首次启动从 `oauth-providers.example.json` 迁移"路径保留（幂等写凭据、覆盖写协议参数），
  但不再是运行期的真源。

## 7. 验收

- 定向：`test_oauth_channel_consistency.py`（11 条）+ `test_channel_strategy.py` +
  `test_d28_channel_login_wiring.py` + `test_s74_family_closure.py` = 43 passed；
  另 `test_grape_auth.py` 4 条（rate-limit/audit/throttle）为基线既有红，stash 对照实证无关。
- 守卫清单（反证有效）：
  1. gitee scope ⊆ Gitee 合法集（写回 `emails` 即红）
  2. gitee/huawei `pkce_enabled=false`（写回 true 即红）
  3. huawei 卡存在、`{access_token}` 占位、`openID`、`headPictureURL`、通过构建校验
  4. 登录卡出现托管字段 → 红
  5. 平台卡出现 OAuth 字段 → 红
  6. 随仓库配置文件出现 `client_id/client_secret` → 红
  7. 加载器剥离文件内凭据
  8. 未知键整卡拒绝
  9. 文档键（`_` 前缀）被忽略、卡内 `_note` 被剥离
  10. example 可直接复制成真文件加载

## 8. 风险与待办

- **fail-closed 的部署约束**：`configs/oauth-providers.json` 必须随发布走；缺失即无登录渠道。
- **华为仍未实弹**：`GOpen.User.getInfo` 返回结构（是否顶层 `openID/nickName/headPictureURL`、
  是否 gzip）需真 token 打一次定稿，`user_url/id_field/login_field` 按官方文档+代码既有注释推定。
- **P4 未做**：管理面「全局配置」页缺 `gitee_bot_token/username` 字段（`config_store._BOT_FIELDS`
  只有 gitcode/github），gitee bot 目前只能走「仓库平台」页（写 `global_bot_gitee_token`）
  或直写 meta `global_bot_gitee_username`；且 `global_bot_gitee_username` 未配时
  self-loop 判定回落 gitcode 用户名池（`user/db.py:5108`），需界面化。
- **scenes/其它模块**：本次未扫其余平台字面量，若后续发现登录域外的残留，按批次扫雷模板另立条目。

## 9. D108-B：登录令牌不回落仓库操作（选项 A，2026-11-13 用户拍板）

外部评审建议"登录与仓库用两个独立 OAuth 应用"。裁定：安全方向采纳，但**不采用双应用**，
改为单应用 + 收窄 scope + 关回落 —— 安全性等价，成本是双应用的零头。

理由（实锤）：
- 我们的仓库令牌本就有更合适的载体：`get_user_token_for` 解析序 ①凭据域 PAT → ②ei OAuth
  （`src/api/deps.py:560`）。PAT 长期有效、可单独吊销、可精确授权。
- 双应用要把刷新链复制一遍：刷新责任全部在登录域（`_try_refresh_user_token` 注释原文
  "刷新责任全部留在登录域（仓库域只消费短期 access token 结果）"），且要新增 ei 的 app 维度、
  401-retry 链、撤销传播、迁移 —— 不是"多填几个值"。
- 评审里"Gitee 接入规范明确建议凭据分库存储、不得混用"这句**无出处**：Gitee 官方 OAuth 文档
  （gitee.com/api/v5/oauth_doc 全文）只讲 authorize 参数、scope 枚举、token 一天有效期与 refresh，
  没有该表述，不能作为依据。

实现：
1. `src/api/deps.py` 新增 `_oauth_repo_token_fallback_enabled()`：`GRAPE_OAUTH_TOKEN_FALLBACK`
   默认 `0`（关）。**安全默认**：登录应用的令牌在设计上只有身份 scope，不得用于仓库读写。
2. `get_user_token_for`：PAT 未命中且开关关闭 → 直接返回空串并 debug 日志（调用方据此提示
   "请配置 PAT"），不再静默借用登录令牌。
3. `_legacy_token_resolver`（平台卡 `legacy_token_symbol` 遗留符号）同样受开关约束，
   防止老调用方绕过。
4. Gitee 渠道卡 `scope` 由 `user_info projects` 收窄为 `user_info`（登录应用不再申请仓库读权限；
   配合"Gitee 跳过授权页按 scope 一致性判定"，scope 越稳越好）。

影响（已知代价，用户已确认）：
- 只配 OAuth、未配 PAT 的用户，仓库列表/读操作由"能用"变"需配 PAT"。
  生产库实查：ei 有 token 的仅 gitcode 1 条、gitee 1 条，影响面极小。
- 待办（P4 一并）：前端在仓库列表为空且无 PAT 时提示"配置 PAT 后才能列仓库"。

守卫（`tests/mgmt/test_oauth_token_fallback_gate.py`，5 条）：
默认关→空串 / 显式开→恢复旧行为 / 有 PAT 恒优先 / legacy 符号受开关约束 / gitee scope 仅 user_info。


> ⚠️ **后续（D109，2026-11-13）**：本文的 D108-B 环境变量开关 `GRAPE_OAUTH_TOKEN_FALLBACK`
> 由 D109 第一批**删除**，改为结构边界（UserToken 内部来源约束 + 能力视图），详见
> `d109-platform-object-model-design.md`（该分片另将仓库凭据能力拆到新的
> `configs/repo-credentials.json`，方言卡只留协议字段）。本文 §10 的"平台托管判定"在 D109 中收敛为
> 派生能力视图 `configured` / `hosting` 两个布尔。

## 10. 平台是否支持托管是怎么判定的（huawei 为什么天然不行）

不是"写死 huawei 不支持"，而是两套平台集合各自派生、默认不含 → fail-closed：

1. **托管平台集合 = 平台卡目录**：`platform_card.list_vcs_platforms()` → `_load_cards()` 扫
   `configs/platforms/*.json`。没有 `huawei.json` → huawei 不在托管集合里。
2. **行为实现能力位**：`src/domains/runtime/vcs/providers/*.py` 各自声明 `HOSTING_FULL_FLOW=True`
   （只有 gitcode / github / gitee 三个模块有）；`repos.py:141 _hosting_implemented_providers()`
   从 `_MODULES` 派生（模块能力位驱动，零平台字面量）；`repos.py:1194` 托管入口对不在集合内的
   provider 直接拒绝。
3. **bot 身份解析 fail-closed**：`repo_manager.py:1131 _KNOWN_VCS_PROVIDERS` = {gitcode, github, gitlab}
   ∪ 卡列表 → huawei 不在，`resolve_bot_username("huawei")` 抛显式 ValueError（"未知 VCS 平台……请配卡"）。
4. **前端两个列表各自取源**：仓库平台列表来自 `list_vcs_platforms()`（repos.py:152/333/516）→
   huawei 不出现；登录渠道列表来自 oauth 卡 `providers_catalog()` → huawei 出现。
5. **因此**：huawei 只支持登录、不支持托管，是"没给托管卡 + 没实现托管能力位"的结构性结果，
   不是特判。某天要支持华为托管，动作是：加 `configs/platforms/huawei.json` + 在 providers 目录
   补一个声明 `HOSTING_FULL_FLOW=True` 的模块（harness 端点契约另行实弹）——零特判改动。

风险与待办汇总（§8 见前文）：fail-closed 部署约束、华为 userinfo 待实弹、P4 管理面 bot 字段、
同一平台在两份配置里的 provider key 同名（是命名约定，不是耦合）。
