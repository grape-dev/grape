> ⚠️ 本文为拆分分片（内容唯一权威源）。索引导航：./multi-platform-decouple-design.md
> 来源：multi-platform-decouple-design.md L201-L420（原文原样搬运，零改写）。
> 域定义：vcs 基座与早期扫雷——webhook/命令层/工厂/基座合一/审计 P1。
> 全局约束：继承索引页「表设计铁律/实现纪律/验收纪律」（见索引页）。

### D12：webhook 验签与注册路由收口（W2/W3 补全 D2/D10）

D2 方言模板补两个此前遗漏的面：

- **验签进方言**：方言模板新增验签策略字段——github 系 HMAC-SHA256
  （X-Hub-Signature-256 / X-Gitee-Signature），gitlab 系 token 直比
  （X-Gitlab-Token），策略与 header 名进平台卡（webhook_verify: hmac / token_compare）。
  routes/webhook.py 的按平台 if/elif 验签分支收敛为策略分派。
- **注册路径进平台卡**：setup_webhook 的回调 URL 从 meta webhook_url +
  平台卡 webhook_path 派生（替换 repo_manager 二分 if/else 与 db.py:1865 字符串
  replace——后者是"把 gitcode 后缀替换成 github 后缀"的历史补丁，改为按目标平台
  卡重建 URL）。
- **bot 身份进平台卡**：bot_identity（username + 状态 meta key），B2 收口；
  collaborator 授权、PR 评论署名等 bot 触点统一从卡取。
- SC1：GRAPE_CLI_REPO 已支持 env 覆盖，随 D1 平台卡补 cli_repo 字段并写验收用例
  （内网镜像源装 worker）。
- F1：实现时核验前端 PAT 填写/平台选择是否按配置卡动态渲染，硬编码处随本批收口。

### D13：VCS API 命令层配置化——命令注册表 + API 方言模板

api_client.py 的统一命令面（评论/关 issue/建 PR/合 PR/get_pr/repo_info）保留，
命令内部按平台 if/elif 分叉消除，改为三层（与 D2 webhook 层同构）：

| 差异层 | 内容 | 载体 |
|---|---|---|
| 传输 | api_base、鉴权风格（query_token / bearer_header）、路径模板 `/repos/{owner}/{repo}/pulls` | 仓库平台卡 api 段（纯配置） |
| 映射 | 响应字段名（title/body/number/state）、分页风格（page/cursor） | 仓库平台卡 field_map（纯配置） |
| 语义 | GitCode v5 必须 `?access_token=`（header 401，注释实测坑）、GitCode PUT merge 行为、GitLab PR=MR(iid) 模型 | 方言模板代码（github-v5 / gitlab-v4 两个） |

- 仓库平台卡 api 段示例（gitee）：`{api_base: "https://gitee.com/api/v5", auth: "query_token", dialect: "github-v5", paths: {...}, fields: {...}}`
- gitee 与 gitcode 同属 Gitee 系 v5 + query_token 鉴权 → 零代码接入（首个验收对象）
- token_resolver.VCSProvider 的命令（add_collaborator/setup_webhook 等）同改：
  现委托 repo_manager 的 gitcode 调优函数 → 改走方言模板，endpoint/鉴权/字段全部来自配置卡
- 非标准语义平台继续走子类插件口（与 D2 的 register_vcs_backend 对称，保留 escape hatch）

### D14：webhook 路由动态注册 + oauth_fill 解绑级联 + 平台卡管理面（W4/E1/A1 收口）

- **动态路由（W4）**：路由注册从硬编码 endpoint 改为按仓库平台卡生成——
  启动时遍历卡，为每个 webhook_path 注册统一 handler（provider 由路径反查，
  即 D2 的 resolve_source_by_path 扩到 FastAPI 层）；存量 gitcode/github
  endpoint 保留为别名，行为不变（回归锁）。
- **解绑级联（E1，S15；D100 对齐）**：OAuth 解绑/吊销 external_identities 时，
  该 (user, provider) 授权即时失效（token 清空，lazy 链断供，get_user_token_for
  返回空）；source=pat 不受影响；删除 PAT 不动登录绑定（两域独立吊销）。
  原"oauth_fill 行降级失效态"表述随落库填充退役——实测链=ei 直清。
- **平台卡管理面（A1）**：复用登录渠道卡 P2-2 机制——admin CRUD + 保存时校验
  （dialect 枚举合法、host 全局唯一、token_env 不与既有环境变量冲突、webhook_path
  唯一）+ invalidate 缓存热失效。管理面 UI 归 F1 前端核验范围一并做。

## 实现纪律（模式约定，已拍板）

采用 DDD 领域驱动实现，模式服务于 D1-D14 已定决策，不为模式而模式：

- **限界上下文**：账号域（登录/身份/会话）× 资源域（仓库凭据/平台操作）= D6 分族边界。
- **聚合根**：GrapeAccount（身份聚合）、RepoCredential（凭据聚合，source/status
  生命周期规则内聚在聚合里，不外泄到调用方）。
- **领域事件**：OAuth 解绑 → 授权即时失效（lazy 链断供，D100 语义；原
  "oauth_fill 凭据级联降级"表述随落库填充退役——S15 实测 ei 链）。
- **值对象**：repo_key（不可变解析）、PlatformCard（配置快照，不可变）。
- **仓储**：grape_account_db 为雏形，grape_t_repo_credentials 沿同风格。
- **策略模式**：方言模板四族算法（webhook normalize / 验签 / clone 鉴权 scheme /
  API 命令），平台卡选策略——同族新平台换配置不换代码。
- **工厂+注册表**：create_provider 按配置卡构建（消 K3）；register_vcs_backend
  保留为注册扩展点。
- **Builder**：PlatformCard 由 JSON dict 构建，D14 校验（dialect 枚举/host 唯一/
  token_env 不冲突）为构建 invariant，非法即构建失败拒绝。
- **Facade**：RepoCredential 统一取凭据 facade（D6 批次 0，调用方无感）。
- **适配器**：VcsApiClient 命令面，平台差异被方言模板吸收。

约束：领域对象（RepoCredential/PlatformCard）不放 IO 逻辑；平台 API 调用在
方言策略/适配器层；跨域协作（解绑级联吊销）走显式 facade 调用（oauth_fill
引用已随落库填充退役——D100，授权关系由 ei 承载），不允许跨域直接挖字段。

### D15：K3 工厂 + 新平台 PAT 批次 0 存储（已拍板）

- **GenericVcsProvider**（策略+工厂落位）：能力全部来自仓库平台卡——
  verify_token 走卡 api_base + /user + api_auth；collaborator/webhook 走卡路径
  模板；global_bot_meta_key 按 `<provider>` 派生；create_provider 三平台返回
  原类（行为不变），config-only 平台返回 GenericVcsProvider（S23）。
- **PAT 批次 0 存储**：新平台（gitee 等）用户 PAT 存 user_credentials 泛型表
  （provider=`<p>_pat`，config_json.token），零 DDL、零新列；RepoCredential
  facade 读取顺序：OAuth 身份（external_identities，D3 统一入口）→
  user_credentials `<p>_pat`。批次 1 grape_t_repo_credentials 表落地时一次性迁移收口。
- **边界**：managed_repos 按平台分列的 bot token 写入（token_storage_field）
  对 config-only 平台返回空串——仓库级 bot token 归批次 1（M1/M2 收口），
  批次 0 新平台只有用户级凭据（PAT/OAuth），不含托管 bot 模式。

### D16：平台前缀候选集与 web 链接抽象（扫雷第二轮，A/B/C 类收口）

- **repo_key_candidates(base)**（S25）：裸 repo_key 的跨平台前缀解析统一函数，
  候选集 = 全部平台卡 provider 前缀（含裸 key 本身）。替换 check_repo_access /
  chat / knowledge 三处 `[gitcode:{base}, github:{base}]` 双前缀字面量——
  该字面量对新平台是功能 bug（权限门 403）。守卫用例：禁止新增双前缀候选集字面量。
- **issue_web_url / pr_web_url(repo_key, number)**（S26）：平台 web 链接从卡
  host 派生（`https://{host}/{owner}/{repo}/issues/{n}`），替换 event_pipeline /
  issues_center 高频触点的硬拼 URL；scenes/scene_sync 等低频触点归批次 1。
- **前端仓库跳转**（S27）：app.js 跳转链接 host 来自 PlatformCatalog，
  仓库列表分组渲染归批次 1（改动面大）。
- 平行函数族（repo_manager webhook 双族、repos.py 绑定流程 36 分支）归批次 1
  统一到 GenericVcsProvider/VcsApiClient。

### D17：图片附件与 ZIP 下载基址按卡派生（扫雷第三轮）

- 平台卡新增 user_images_base / download_base 字段（均缺省空）：
  gitcode 默认 raw.gitcode.com（存量行为）；github codeload；gitlab gitlab.com。
- extract_user_image_urls(text, provider)：编辑器图片附件正则按卡派生，
  卡存在但字段空=该平台无此机制（返回 []），无卡回退 gitcode（存量兼容）；
  issues_center 按 repo_key 前缀穿 provider，prompt 组装自动覆盖新平台。
- scene_sync 下载基址：host 匹配平台卡 → 卡 download_base；未知 host 保留
  旧 gitcode 行为但显式告警（G1 同型"静默回退"收口为可观测）。
- 前端图标/品牌色（repos.js PLATFORM_BRAND_COLOR/platformIcon 双分支）：
  gitee 等新平台回退通用图标+默认色可接受，归批次 1 与 F1 分组渲染一并做。

### D18：管道内 Issue 读取下沉到统一命令层（S28）

event_pipeline 的 _fetch_issue_title_via_api / fetch_issue_via_vcs 是最后两处
"按 provider 二路分流 + 手拼 URL"的 API 调用（且 gitcode 分支用 Bearer header，
与 D13 实测坑"v5 必须 query_token"错配）。重构为共享 _vcs_get_issue(repo_key, num)：
token 解析语义保持（github=get_github_token，其余=get_repo_token_with_refresh），
请求统一走 VcsApiClient（卡驱动 api_base/auth），404→missing 映射保持。
新平台（gitee）自动获得正确认证形态与路径。

### D19：分支命令与 bot 巡检接线（扫雷第五轮，S29）

- **VcsApiClient.list_branches**（命令面扩展）：卡驱动分页 + 方言路径
  （gitlab 走 project/repository/branches）；scenes 分支查询对 config-only
  平台走统一命令层（并修复 provider 解析把 config-only 前缀覆盖回 gitcode 的 bug）。
- **bot_token_health 接统一来源链**：_verify_bot_token 的 token 获取改
  get_global_bot_token_for（消 if github/else gitcode）；巡检平台清单 =
  内置两平台 + 配了 global_bot_<p>_token 的 config-only 平台（自动纳入，
  返回体平台键增量向后兼容）。
- 挂账批次 1：admin/config_store 写侧平台键映射表（global_bot_gitcode_token 等
  四键，与新平台 bot 配置入口一并做）；repo_roles 的 _get_gitcode/_github_login
  二函数（用户平台身份解析统一化）。

### D20：白名单列卡化 + 建 Issue 统一命令（扫雷第六轮，S30/S31）

- **白名单列取登录渠道卡**（S30）：grape_account_db 登录名变更联动的 wl_col
  从二元 if 改为 auth/providers 渠道卡 whitelist_column（既有通用机制接线）；
  未配列平台跳过联动（与现状一致）。
- **VcsApiClient.create_issue**（S31）：命令面补建 Issue（卡驱动路径+鉴权，
  方言差异：gitlab 走 projects/issues）；issues_center 子需求/幂等建档两处
  调用委托统一命令（config-only 平台自动正确）。
- 挂账批次 1：labels 归一语义（github 数组 vs gitee 系逗号串，方言字段）、
  _post_github/_gitcode_issue 存量函数族整体退役、git_repo_api token 分支。

### D21：建 Issue 调用方接线 + labels 方言语义（扫雷第七轮，S32/S33）

- **issues_center 两处建单二分支下沉**（S32）：发布子单创建与 onboarding 幂等
  建档的 `if _provider == "github"` 替换为 VcsApiClient.create_issue 统一命令
  （存量两平台在命令内部委托 _post_*_issue，行为不变；config-only 平台自动
  走卡路径含 Gitee form 语义）。
- **labels 归一按方言**（S33）：git_repo_api 的 labels 归一二元 if 改为方言判断
  （github 方言=数组语义，gitee 系=逗号串语义）——方言是既有抽象，不新增枚举。
- 挂账批次 1：_find_open_issue_by_title 的 Search API 分支、_post_*_issue
  函数族整体退役。

### D22：实现基座合一（不留给批次 1 的尾巴）

用户裁定："接口一套、实现基座一套，存量类只保留不可约协议差异"。落地：

- **鉴权四态进卡**：api_auth = query_token（Gitee 系 v5）/ token_header
  （存量 github "token TK" 头逐字节保留）/ private_token（存量 gitlab）/
  bearer_header。内置默认卡对齐存量形态。
- **VcsApiClient 全卡驱动**：删 _gitcode_request/_github_request/
  _gitlab_request 平行族与全部 `self.provider ==` 分支（现 0 处）；
  命令路径由方言（github/gitlab）+ 鉴权风格派生；建 Issue 的 Gitee form
  语义、gitlab squash/description 等方言差异保留在统一路径内。
- **存储字段进卡**：token/webhook/session 列名、全局 bot meta key、
  pat_create_url 全部为卡字段（内置默认=存量 Provider 类现值，逐字节）。
- **Provider 类折叠**：GitCodeProvider/GitHubProvider = GenericVcsProvider
  子类，仅保留协议验证过的存量委托（collaborator 邀请流、webhook 事件集、
  github 登录名获取）——这些是 v5 form/事件集语义，批次 1 验证后退役。
  gitlab 由"错误回退 GitCodeProvider"修正为 GenericVcsProvider（行为修正）。
- **顺带修**：_cached_get_meta 空值不再缓存 60s（admin 写库后的陈旧空窗）。

### D23：审计 P1-3 首批分文件清扫（tasks/agents；S34/S35）

按"分文件批 + 守卫"纪律（不攒大 PR）：
- **tasks.py:458**：issue 链接 host 二分 → issue_web_url（卡派生，S26 复用）。
- **agents.py:174-176**：跨平台候选 if/elif 对 → repo_key_candidates。
- **agents.py:202**：会话 token 列二分 → create_provider(p).session_token_field()
  （D22 存储字段进卡的直接收益——列名即卡数据）。
- **grape_auth.py:1801/1824 保留**：GitHub 落库读回校验是 Issue #455 的数据一致性
  加固（注释明示 GitCode 语义保持不变），属刻意设计锚点非平台耦合债。
- config_store 写侧与 repos.py 绑定流程仍归批次 1（后续文件批次）。
- **D22 回归修复（S35 测试抓出）**：Provider 折叠时存量存储字段方法被删而
  基座读卡替换未落地，gitcode/github 的 session/token 列名一度返回空——
  已修为全卡派生；config-only 平台返回派生列名（bot_gitee_token 等），
  列的 DDL 存在性归批次 1。教训已入技能：大段 python replace 无 assert
  会静默失败，必须逐块 assert。

### D24：三族审计合并前修复（探活误删防护 + 影子函数 + 重复类块）

外部审计三族扫描（行级复核全部属实）后的合并前修复：
- **探活误删防护（破坏性路径，最高优先）**：repos.py 仓库探活原 else 分支把
  非 github 平台一律打 gitcode.com API——gitee 托管仓取到 token 即 404→
  误判 deleted→触发托管清理（唯一会删数据的路径）。修为三分支：github/
  gitcode 原样，其余平台跳过探测（零危害：不误判不删数据）；卡驱动探活
  随 S12 验收批次接（需先接该平台 token 解析链）。
- **tasks.py 影子函数（活缺陷，与 gitee 无关）**：本地 ensure_issue_task
  覆盖 re-export，把 issues_center 的 #244 org 归属修复（校准为仓库 org、
  防串货）和幂等去重（全状态复用）挡在门外——删 132 行影子副本，恢复
  权威实现生效。
- **token_resolver 重复类块（D22 类序调整引入的回归）**：GitCode/GitHub
  Provider 各重复定义两次——删第二对副本（内容逐行一致已验证）。
  过程教训（已入技能）：大段删除/替换必须 assert 边界，本次误删工厂函数
  由 import 测试当场暴露并恢复。
- 审计确认的其余项归类：prompt_builder agent 注入二分 / event_pipeline
  bot 身份 → S12 验收批次（#2/#3）；存储动态建列、前端数据驱动、第五平台
  验签头名入卡 → 后续批次（配置化从 gitee 特例走向通用的门槛）。

### D25：agent 注入与流水线 bot 身份前置修复（审计 #2/#3，原 S12 批次提前）

- **prompt_builder 运行时注入四函数**（S36）：_vcs_api_base/_vcs_token_env/
  _vcs_bot_token_env/_vcs_name 由二分改为卡派生（PlatformCard.api_base/
  token_env_name/user_token_env_name/name）。存量两平台值与卡默认一致
  （回归锁）；gitee agent 获得正确的 API base 与 BOT_GITEE_TOKEN/
  USER_GITEE_TOKEN 变量名（与 repogit askpass 链同源）。
- **event_pipeline 提交身份**（S37）：bot username 二分支改 resolve_bot_username
  来源链（D15 已建，本处接线），host_user 降级路径保持。

