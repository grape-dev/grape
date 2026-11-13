# 全链路平台硬编码审计（2026-11-12）

范围：feat/arch-weekly-11-yaw-dev @ 4df695a09 及基线 62568298e。
方法：全仓正则扫平台字面量/二分（python+js，命中 ~350 处 / 76 文件，其中测试文件过半）
→ 排除测试与合法 vendor 分支（如 providers/gitee.py 实现 gitee 差异）→ 逐条读码定性
→ 高价值项实跑复核（HTTP 状态码 / 真实 token / 单测双侧对照）。

**验证标记**：✅=我实跑复核过；📖=读码确认（数据流清楚，未实跑）；📄=子代理报告条目，未逐条复核。

结论一句话：这批问题的共同机制不是"缺平台"，而是**卡已经声明了能力位，传输层/业务层仍在比较平台名字面量**——
"非 github ⇒ 一律当 gitcode v5（gitcode.com/api/v5 + Bearer）"是最高频的错误二元分叉。
同仓已有正确的卡驱动 helper（`create_provider`、`_vcs_get_issue`、`_list_issue_comments_card`、
`issue_web_url`、`parse_repo_key`）——属于**迁移未收口**，不是重新设计。

## 已修

批次 1（4df695a09）：仓库列表/同步/`?provider=` 过滤：平台集合=平台卡目录，拉取走
create_provider 端口。✅（修前 gitee 绑定用户 user_repos 零 gitee 行、Gitee Tab 恒空）

批次 2（本批）：上表 P0-1/2/3 三条数据/能力类，全部卡驱动落地并实弹复核 ✅

1. **探活按 provider 打对应平台**（原 P0-1）：新增 `_platform_api_root`（卡 api_base +
   dialect：gitlab 走 `/projects/{urlencoded}`，其余 `/repos/{o}/{r}`）、`_platform_api_get`
   （卡 api_auth：query_token/private_token/Bearer）、`_probe_repo_token`（provider 无关
   PAT→OAuth）；`_probe_stale_repos(user_id)` 按每个 repo_key 的 provider 取 token 探活；
   平台未在册/无 token/非 404 一律不判定（fail-safe）。hooks-404 语义按平台保留
   （`_HOOKS_404_DELETED_PROVIDERS={"gitcode"}`，其余平台=权限信号跳过——m2 事故口径）。
   实跑 trace（DB 副本）：5 个 gitee 托管仓全部 `OK https://gitee.com/api/v5/repos/...?access_token=...`
   （改造前是 `ERR 404 https://gitcode.com/...`），行零变化；gitcode/github 探活路径不变。
2. **托管判定卡驱动 + 未接入平台显式拒绝**（原 P0-2）：`_provider_from_hint` 用
   `parse_repo_key` + 卡目录（前缀/DB 登记两条路都覆盖），前缀剥离改为在册前缀通用剥离，
   provider 无关 token 解析（`get_user_token_for`）；`_HOSTING_IMPLEMENTED={"gitcode","github"}`，
   其余平台返回 422 `HOSTING_NOT_IMPLEMENTED` 并指向 repair-host（不再兜底成
   github/gitcode 建错 webhook）。实弹：`POST /api/repos/host {gitee:...}` → 422 显式文案。
3. **git 传输卡驱动**（原 P0-3）：`_SUPPORTED_PROVIDERS` 常量退役 → `_transport_providers()`
   = 卡目录；clone/workspace 域名走 `_clone_host`（卡 host，gitee.com/gitlab.com 不再被
   写成 gitcode.com）；`_repo_platform_token` 非 github/gitcode 平台走端口 bot token +
   provider 无关用户 token；502 文案走卡 name。

验证：新增 `tests/mgmt/test_repos_platform_driven.py`（7 例：卡 URL/鉴权派生、gitee 判定、
传输集合/域名、探活打对平台、未知平台不判定）；`tests/insight/test_probe_github_hooks_404.py`
适配新签名与新鉴权（断言口径不变）。15 文件大集对照基线：**本分支 36 红 vs 基线 40 红，
零新增红，4 条基线预存红转绿**（d30 gitee hint、s62 gitee fetch、s62 gitcode parity、
s62 repo_platform_token_gitee）。

## P0（用户可见功能缺失 / 数据风险，均 ✅ 或 📖 复核）

| # | 位置 | 现象（实查） | 影响 | 验证 | 修法 |
|---|------|--------------|------|------|------|
| 1 | repos.py:236-470 `_probe_stale_repos` | gitee 托管仓被 gitcode.com 探活：`ERR 404 .../repos/<gitee-login>/demo`，二次复核拿 gitee bot token 再打 gitcode → `ERR 401` → 走"其他错误跳过"才没删 | gitee/gitlab 托管仓；gitcode 若回 404 即 `cascade_cleanup_repo_data` 级联删 issue/场景/流水线。live 库现有 4 个 gitee 托管仓（验收用户）全在弹道 | ✅ 真实 trace | 探活按 repo_key provider 走 `create_provider(p)` + 卡 api_base；`_start_stale_probe_async` 的 gitcode/github 门槛同改 |
| 2 | repos.py:1043-1100 托管入口 | `_provider_from_hint` 只认两前缀 → gitee 返回 ''；`_provider_from_token`（github 优先→gitcode）兜底 → gitee 仓被当 github/gitcode 托管；1046 行前缀剥离也只认两前缀 | gitee/gitlab 无法正确托管（webhook 建错平台、repo_key 前缀错） | 📖 | `_provider_from_hint` 用 `parse_repo_key` + 卡目录；托管分支改为 provider 卡驱动 |
| 3 | repos.py:2539 `_SUPPORTED_PROVIDERS` | git 传输只认 gitcode/github，`_repo_provider` 对 gitee 直接 403 | gitee/gitlab 仓 agent 换不到会话级 git 凭据（workspace 克隆/推送不可用） | 📖 | 集合改为卡目录；凭据下发走 `create_provider` |
| 4 | git_repo_api.py:244-248 `_vcs_url` | 实测 `gitlab → https://gitlab.com/api/v4/repos/o/r/issues/1`（v4 无 `/repos` 资源段，应为 `/projects/:id/...`） | gitlab 全端点 404 | ✅ 实调 | 路径段由卡/方言模块提供（PATH_MAP），不写死 `/repos/{o}/{r}` |
| 5 | git_repo_api.py:609 update_issue | 只对 `p=="gitcode"` 补 `repo` 字段；gitee 卡实查 `issue_write_form=owner_param`（路径 `/repos/{owner}/issues/{n}`，repo 入 body），当前走 `/repos/{owner}/{repo}/issues/{n}` | gitee issue 更新 404 | ✅（卡字段实查 + 用例两侧同红） | 判据换卡能力位（`issue_write_form`/`ISSUE_BODY_REPO_BARE`），路径由卡决定 |
| 6 | event_pipeline.py:3129 `fetch_issue_via_vcs`（+2956/3056/3106/3147 同族） | 非 github 一律 `gitcode.com/api/v5` + `get_repo_token_with_refresh`；实测 gitee 仓返回 `('unknown', {})`，而卡驱动 `_vcs_get_issue` 对同一仓同一 issue 返回 `exists` + 完整数据 | gitee/gitlab 的 issue 状态/指派人/评论读取全瞎；流水线门禁、去重、回帖都受影响 | ✅ 实跑对比 | 同族函数统一改调 `_vcs_get_issue`（:5614）/`_list_issue_comments_card`（:5605）/`_post_issue_comment_ledgered`（:5584） |
| 7 | event_pipeline.py:4652/4680/4826/5030 | 父 issue 回帖、评论→chat 同步、场景预校验失败评论均二分走 gitcode 实现，失败只 warning | gitee/gitlab issue 收不到流水线回帖/评论不同步 chat | 📄（同族机制已 ✅） | 同上统一卡驱动 helper |

P0-1/2/3 已修（见上「已修·批次 2」）；P0-4~7（gitlab `_vcs_url`、gitee `update_issue`
owner_param、event_pipeline 旧 gitcode 读链、父 issue 回帖/评论同步）排下批。

P0-4~7 已修（批次 3，见下「已修·批次 3」）。

## 已修·批次 3（2026-11-12，代理层契约开关 + 运行时读链）

1. **代理层消费 provider 模块契约开关**（原 P0-4/P0-5）：
   - `ISSUE_UPDATE_OWNER_PATH`：issue 写 URL 走 `{API_BASE}/repos/{owner}/issues/{n}`，
     body.repo 由 `ISSUE_BODY_REPO_BARE` 决定纯仓名/全称（gitcode/gitee 生效，github 零变更）；
   - `LABEL_COLOR_PREFIX_HASH`（labels 色值 `#`）、`MILESTONE_DUE_DATE_ONLY` /
     `MILESTONE_DUE_ON_REQUIRED`（due_on 只发日期/缺失可读 400）、
     `PR_COMMENT_ID_HEX`（32 位 hex → 数字 note_id 再 update/delete）；
   - gitlab `_vcs_url` 走模块 `repo_segment`（`/projects/{urlencoded}`），其余 `/repos/{o}/{r}`；
   - `LABELS_ARRAY_FORM`（github 数组形态）新登记为模块能力位，替代 `provider == "github"`。
   验证：`tests/api/test_proxy_contract_adaptations.py` 5 红→全绿；milestone 契约 5 红→绿。
2. **运行时 issue 读/回帖/评论同步统一卡驱动**（原 P0-6/P0-7）：
   `_fetch_issue_assignee` / `_fetch_issue_title_via_api` / `fetch_issue_via_vcs` /
   父 issue 回帖 / 评论→chat 同步 / 场景预校验回帖全部改走 `_vcs_get_issue` /
   `_post_issue_comment_ledgered` / `_list_issue_comments_card`；
   `engine/service.py` 401 回帖去重改 `_list_issue_comments_card`、正文平台名改卡 name；
   旧 `_gitcode_post_issue_comment_ledgered` 删除（回帖唯一写路径）。
   验证：新增 `tests/runtime/test_w11_gitee_issue_read_card_driven.py`（结构锁 + gitee/gitlab
   行为锁 + 父 issue 回帖唯一入口）；进程内实弹同一条 gitee issue（IKTEST1 占位）旧路径
   `('unknown', {})` → 新路径 `exists` + 标题（见锁用例 mock 版与 P0 表记录）。

## 已修·批次 4（2026-11-12，剩余 P1 四条）

- **`/issues/mine` 候选集**：`repo_key_candidates()` 取代 gitcode/github 双前缀
  （gitee/gitlab 仓 issue 不再恒空）。
- **通用 webhook 端点仓库删除级联**：验签通过后 `_is_repo_delete_event` +
  `_handle_repo_deleted(repo, provider)`，与两个专用端点同语义（此前只调度流水线，
  上游删仓后托管记录残留）。
- **IM 卡片 issue 链接**：`im/notify.issue_url` 改调 `kernel.repo_key.issue_web_url`
  （host/路径按卡方言，gitee 不再落 gitcode.com）。
- **字符串 ident 兜底建档**：`ensure_issue_task(rk, num)` 不再 `int(num)`；
  `tasks.py` 详情查询/回显同样去掉 int 强转、issue 链接改 `issue_web_url`。

同批顺带收口的护栏点：`agents.py` 会话 token 列 → `create_provider(p).session_token_field()`；
chat/handler_async 401 文案 → 卡 `display_name_for_repo_key`；`repos.py` 裸名解析 → 卡目录候选；
ratchet 剪除 notify 幽灵基线点。

新增锁：`tests/runtime/test_w11_p1_leftover_locks.py`（6 例：候选集/删除事件识别/通用端点接线/
IM 链接四平台/ident 不强转）；`tests/mgmt/test_agents_session_token_and_tasks_url.py` 的源码断言
从"变量名字面"放宽为"调用形态"。

已知遗留（基线即红，非本批引入）：`test_s76_no_ident_int_crash_sites` 仍报
`session_store.py`×2 / `webhook.py`×3 / `issues.py`×2（后者均在 `isdigit()` 守卫内，属守卫误报）；
`test_s76_display_sites_use_helper` 在基线亦红。

## 已修·批次 3（P1）

- settings 平台可见性：撤掉「非默认平台须配 OAuth 才显示」隐式白名单，唯一权威 =
  `vcs_platforms_hidden`（纯 PAT 部署也能看到卡、绑 PAT）；两条旧口径用例改为新契约；
  本部署 hidden=["gitlab"]，可见面不变。
- templates 建仓选平台 / scenes 场景体验与分支 / agents 会话多前缀候选 / chat 提示词与
  401 文案/图片白名单 / issues_center 身份折叠 / middleware 强加 `gitcode:` 前缀 /
  合同端口 mgmt_local 按 provider 取 bot·webhook 列（rc 单表优先）/
  issue_vcs_index 非二平台 fail-open / qa_gate PR 链接按模块 `PR_WEB_PATH` /
  repo_manager 模板源下载按卡 `download_base`：全部卡/端口驱动，无平台名二分。
- 前端：issues/dashboard PR·Issue 链接走 `_vcsUrl` + `_prWebPath`（后端 `/settings/vcs/platforms`
  新增 `pr_web_path`，来源 provider 模块 `PR_WEB_PATH`）；oauth-popup 中性文案；
  app.js 引导卡不再双平台硬兜底、`_NAME_MAP` 补 huawei/hwcloud。
- 护栏修复：`_HOSTING_IMPLEMENTED` 白名单 → provider 模块 `HOSTING_FULL_FLOW` 能力位；
  `test_platform_whitelist_ratchet` 剪除已消失基线点、登记 hosting 分支基线；
  `test_issue_290_view_issue` 断言从 `/pull/` 字面量改为 `_prWebPath/_vcsUrl` 派生。

验证（26 文件批次，与基线 62568298e 同集对照）：本分支 **40 红 / 316 通过** vs 基线
**59 红 / 263 通过**——零新增红，19 条基线预存红转绿；JS 门禁零新增红（18→18+0）。

| 8 | settings.py:2441 `_DEFAULT_PLATFORMS={"gitcode","github"}` | 非默认平台需 `oauth_<p>_client_id` 才出现在 `/settings/vcs/platforms`；**纯 PAT 部署**（不配 OAuth）下 gitee/gitlab 卡片被剔除，PAT 无从绑定 | gitee/gitlab（条件触发） | ✅ 代码 + live 实查：本部署 oauth_gitee_client_id 有值故 gitee 仍显示，gitlab 被隐藏 |
| 9 | middleware.py:70 / server_common.py:255 | `?repo=github:o/r` → 强加前缀成 `gitcode:github:o/r`（只判 startswith("gitcode:")） | 污染 repo_var、日志归属、scenes 三级回落 | ✅ 读码；建议 `":" in qp` 或 `to_repo_key` |
| 10 | issues.py:1790 `/issues/mine` | 只生成 gitcode/github 两候选（`to_repo_key` + 另一前缀互镜像） | gitee 仓 issue 列表返回空 | 📖 |
| 11 | routes/webhook.py:455/529 通用端点 | repo 删除级联只在 gitcode/github 两个专用端点有；通用端点无 | gitee/gitlab 上游删仓后 grape 残留托管记录 | 📖 |
| 12 | im/notify.py:82-86 | IM 卡片"查看 Issue" gitee 仓落 gitcode.com | gitee/gitlab 链接错 | 📄 |
| 13 | routes/chat.py:86 | analyze-image 白名单仅 gitcode CDN | github/gitee/gitlab 图片被拒 | 📄 需实跑确认 gitee 图床域名 |
| 14 | routes/scenes.py:665/753-763 | 场景体验/前置写死 `with_oauth_token(user,"gitcode")` + gitcode.com/user | gitee/gitlab/华为用户体验前置失败 | ✅ 读码 |
| 15 | agents.py:191-194 | 裸键→gitcode:，只镜像 github；gitee session 永不命中 | gitee/gitlab agent 会话查不到 | ✅ 读码 |
| 16 | issues_center.py:161 | `COALESCE(gitcode_user, github_login)` 折叠 | gitee/gitlab/华为 得空串 | ✅ 读码 |
| 17 | templates.py:154 + templates.js:443 | 自动选平台走 `_get_user_provider_and_token`（无 gitee）；单绑定用户无选择器 → provider='' → 400 | gitee/gitlab 用户无法从模板建仓 | ✅ 读码 |
| 18 | qa_gate.py:558-560 | 非 github 一律 `gitcode.com/merge_requests` | gitee host+路径双错、gitlab host 错 | 📄 |
| 19 | issue_vcs_index.py:145-158 | else → `result=None`（fail-open，静默） | gitee/gitlab 索引检测永久失效（删卡不感知） | ✅ 读码 |
| 20 | mgmt_local.py:34,157（合同端口） | `get_repo_webhook_config` 取 `gitcode_webhook_token`；`get_bot_config` 取 `bot_gitcode_token`，不按 provider 选列 | gitee/github 仓经合同给 SRE 的 bot/webhook token 恒空 | ✅ 读码 |
| 21 | repo_manager.py:556-558 | 模板源无下载器回落 gitcode 下载器 | gitee/gitlab 源模板下载错误 | 📄 |
| 22 | engine/service.py:3287/3659 | 401 回帖去重查评论走 gitcode；正文写死 `GitCode` | gitee/gitlab 刷屏/文案错 | 📄 |
| 23 | frontend issues.js:856 / dashboard.js:689 / oauth-popup-callback.html:131 | PR/issue 链接 host 二分（gitee 出 gitcode.com）、PR 路径应为 `/pulls/`；弹窗页平台名二分 | gitee/gitlab 链接/文案错 | 📄（repos.js 同类已改） |
| 24 | issues.py:2388 | 兜底补建 `int(num)`，gitee `IK*` ident 崩溃 | gitee issue 兜底建号失败 | 📄 需实跑 |

## 已修·批次 5（P2 扫尾，2026-11-12）

- `git_repo_api`：PR 标签通道（`pr_labels_via_issues`）与 PR 评论端点族
  （`pr_comment_endpoint`：github=issues、其余=pulls）全部按平台卡能力位派生，
  替换 5 处 `p == "github"` 比较（行为对四平台逐一等价：卡值实查
  github=issues/True，gitcode/gitee/gitlab=pulls/False）。
- `platform_card.display_name_for_repo_key` / `host_for_repo_key`：无卡回退不再冒充
  具体平台（回退 provider key / `<provider>.com`）。
- `repo_key.repo_key_candidates`：卡目录不可用时退化到内核前缀目录
  （`_catalog_providers`），不再硬编码 `["gitcode","github"]`。
- `prompt_builder`：`_is_github_repo` 删除，api_base / token env / bot token env /
  平台名全部走卡或 provider 模块（`module_for(p).API_BASE`）；提示词文案
  "Repository (GitCode/GitHub) API operations" 改为中性 "(platform)"。
- `issues.py` 会话消息 issue 链接 host → `host_for_repo_key`。

刻意保留（附理由，非遗漏）
- `repos.js` bot 协作者文案写 GitHub：后端 bot 状态只由 github 托管分支产出
  （gitcode 分支不返回 bot_status），当前文案与作用域一致。
- `scene_sync` 工具链的 `gitcode_api_token`：运维脚本单一来源（同步源已公共仓库化，
  私有场景才需 token），不属业务链。用户 2026-11-12 裁定维持。
- `s76` 守卫红：`int()` 误报点在 `isdigit()` 守卫内 / 基线即红，见上节。

## D105-19 平台化到位收口（2026-11-12 用户拍板，解除 D105-17 冻结）

用户裁定：平台化/仓库化应到位，"为测试保留生产代码垫片"不成立（test-induced
design damage）。本批收口三项，原"刻意保留"前两条作废：

1. `repo_manager.get_hosting_provider_config`：`provider == "github"` webhook
   函数指针特例分支删除。分派键改为平台卡新字段 `webhook_dialect`
   （github_web=GitHub web-hooks 家族 / gitee_v5=v5 hooks 家族，须卡显式声明，
   不按 dialect 派生——gitcode/gitee API 方言同 github 而 webhook 形态实为 v5，
   派生会静默换家族）。分派走 `_WEBHOOK_FUNC_FAMILIES` 注册表：新平台复用既有
   家族（卡声明 + 自己的 api_base，零代码），新家族注册表加一行。六件套函数
   增加 `api_base` 关键字参数（默认值=原硬编码 base，行为等价），partial 绑定
   卡 api_base——`api.github.com`/`gitcode.com/api/v5` 硬编码消除。
   gitcode.json/github.json/gitee.json（真源=configs/platforms，代码兜底同步）
   已声明 webhook_dialect；live 库 stale meta 整卡 dump 由此暴露 loader 缺陷，
   meta 层改为【逐键覆盖】（meta 只覆盖声明的键，缺席键落到文件卡，防止
   stale dump 抹掉新增卡字段）。未声明 webhook_dialect 的卡托管 fail-closed。
2. `repo_manager` 模板 push 的 workflows 三态：回退位从 `provider == "github"`
   改读卡新字段 `workflows_write`（github=False，其余 True，语义逐字节等价）。
3. `api_client` 三个传输垫片删除，调用侧按 dialect 收敛，存量测试缝
   （#993/#1739/linked_repos monkeypatch 点）迁移到 `_request`——见对应提交。
   行为锁：test_repo_manager_governance 锁 2b（新平台 gitea 复用 github_web
   家族、api_base 打到自定义 host）/ 锁 2c（webhook_dialect 未声明 fail-closed）。

### head 过滤形态进卡（同批实弹纠错）

list_prs 收敛后 gitcode 也会带 head 过滤，实弹实测暴露形态差异：gitcode
`/pulls?head=` 要求裸 ref（`owner:ref` 形态返回空列表、裸 ref 命中 PR#1306）；
gitee v5 同家族口径；GitHub 要求 `owner:ref`。统一发 `owner:ref` 会把 gitcode
PR 列表过滤成空 → branch_guard fail-open 守卫失效（真回归）。修复：卡新字段
`pr_head_filter`（owner_ref=GitHub 缺省 / bare_ref=gitcode·gitee 声明），
api_client 按卡构造 head 值；锁用例
test_api_client_config_driven::test_list_prs_head_filter_form_card_driven。
gitee 侧同问题为基线预存（else 分支旧就发 owner:ref），本批一并修复。


## P2（等价/死代码/文案，择机收口）
- git_repo_api.py:184 `p not in set(_MODULES)` 403（纯配卡新平台）📄
- git_repo_api.py:769/826/1012/1079、158-172、329/347/361：`p=="github"` 选 PR labels/评论/树家族；现值巧合正确但卡能力位（`pr_labels_via_issues`/`pr_comment_endpoint`/`git_tree_family`/`labels_form`）在 `src/` 业务侧零消费 📄
- api_client.py:326/330/585/590 provider 三分流（垫片等价）📄
- platform_card.py:549/563 无卡回退 "GitHub"/"GitCode"；repo_key.py:220/290 回退 `["gitcode","github"]`、legacy host 先于卡 📄
- prompt_builder.py:38/45-66/1025/860 startswith("github:")、`.replace("gitcode:","")`、文案写死 GitCode 📄
- chat.py:770 401 文案二分；chat.py:409/412、handler_async.py:1614/1616 prompt 注入固定 `GitCode login:`（华为纯登录渠道无此 login）📄
- account.js:893 gitlab PAT 帮助链到 GitCode；app.js:93/1272/2334 `_NAME_MAP` 缺 huawei；repos.js:629-658 bot 文案仅 GitHub（后端也只对 github 触发）📄
- scene_sync.py:250 只读 `gitcode_api_token`（工具链）📄

## 护栏缺口（为什么这些能长期存活）
- `test_s62_structure_guard_repos_no_platform_compare` 只有 repos.py 一处的 AST 守卫，且基线就红（44 处）；📄
- `test_uc48_pipeline_zero_platform_literal` 只断言一句注释、读旧路径，**完全没扫 `src/domains/runtime/pipeline/event_pipeline.py`** 📄
- `tests/api/test_proxy_contract_adaptations.py` 已把 gitee/gitlab 契约写成用例，但两侧都红（本批改前改后同 5 红、无新增）：说明契约先于实现落地。
- 建议新增一条组织级守卫：业务侧（非 providers/、非卡字段实现）禁止出现平台名字面量的 `If/Compare`，白名单按目录登记——否则同类问题还会回潮。

## 验收修复批（2026-11-12 深夜，10 项验收矩阵发现的产品缺陷全收口）

1. 代理 create issue 平台写契约（gitee v5 owner-path）：create 复用 update 同款
   `_issue_write_url`/能力位机制，owner-path 时 body 补 repo；IK* 字符串 ident
   端点参数 int→int|str 直通。实弹：gitee 建单（IK* 字符串 ident）→评论→关闭全链路 200。
2. /api/obs/events 500：agent_tasks.errors 的 dict 条目直接切片 → KeyError 兜成
   500；_err_text() 按 localized_message/message/error_code 转文本。limit=1/5/50
   及按仓视图全 200。
3. credentials/{cid}/tasks 聚合口径：新增 local_engine_device_name()/
   engine_mounts_to_credential() 单一来源，聚合含挂载本机引擎（79=6 引擎之和，
   交叉验证一致）。
4. has_bot_token 平台通用：repo_has_bot_credential（卡列→rc 单表→存量列回落，
   get_bot_token_by_repo 同一实现消灭双口径）。live-w10 实锤 gitee 旧列数据
   经回落路径正确判 True（终态口径不吞存量数据——白名单行式双读同教训）。
5. autopilot issue_id 回链：_autopilot_issue_ref 合成键（纯字母）创建即落，
   失败路径不丢统计；存量 9/9 回填。vod 空回链（设计口径）不动。
6. 场景市场 apply 幂等化（OpenAPI 契约对齐，reused=true 复用）；
   experience 身份缺失 502→409 MISSING_IDENTITY（根因含 scenes.py 缺
   with_oauth_token import，NameError 被吞成 502）。
