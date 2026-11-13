# PR #1318 QA 整改记录（第 4 轮复审 → 整改）

日期：2026-11-13 · 分支：feat/arch-weekly-11-yaw-dev · 基线：arch-weekly-11 @ 909f07cdb

## QA 发现处置

| # | 发现 | 级别 | 处置 | 状态 |
|---|------|------|------|------|
| F1 | admin.html webhook_url 占位文案硬编码中文，未走语言包 | BLOCKED | 占位改走 i18n 键，zh/en 语言包补 `admin.webhookUrlPlaceholder` | ✅ 已整改 |
| F2 | 新增迁移使用历史区间版本号（202611120006/7） | Critical | 迁移改号 >= 202611150001，守卫绿 | ✅ 已整改（e02e1820） |
| F3 | 静默 except ratchet 回潮（3 文件 7 处） | Critical | EXEMPT 显式登记 / 收敛日志 | ✅ 已整改（e02e1820） |
| F4 | templates.js / issues.js 改动未 bump 版本号 | Medium | 版本号 bump，缓存 ratchet 测试绿 | ✅ 已整改 |
| F5 | 新增测试用无 WHERE 的整表 DELETE 构造空表白名单 | Medium | 3 处补 `WHERE id > 0`（与 test_login_whitelist.py 既有 idiom 同款） | ✅ 已整改 |
| F6 | 平台级 bot/webhook token 明文写入 meta | Medium（需人工判断） | 见下方裁定 | ✅ 已裁定登记 |

## F6 裁定：平台级凭据加密口径（已决策，负责人确认）

**决策：本 PR 不改 token 存储与读链，平台级凭据加密化与 OAuth client_secret 加密化
另立任务统一处理。**

依据：

1. 同暴露面先例——OAuth `client_secret` 同为 meta 明文存储（`oauth_{provider}_client_secret`，
   providers.py `_get_client_secret`），管理面值均不回显（只回 configured 布尔）。平台级
   bot/webhook token 与其同表、同暴露面、同管理面语义，单独加密前者不消除系统性风险，
   只造成同表两种口径并存。
2. 读链分散——`global_bot_<p>_token` 消费链在 `get_global_bot_token_for` 来源链、
   bot_token_health 巡检、db_setup/repo_manager seed 兜底、event_pipeline 等 ≥5 处；
   在功能 PR 内改造加密读链，漏改任一读点即生产 bot 身份静默失效（最坏伤害半径=
   全线 webhook/bot 不可用），不符合"检测+登记先行、破坏性改造另立任务"的收敛原则。
3. repo 级凭据已加密（`grape_t_repo_credentials` 走 `_encrypt_repo_secret` AES-GCM），
   风险敞口限于平台级全局凭据，与 client_secret 同批治理。

后续任务口径：平台级凭据加密化覆盖 `global_bot_<p>_token`、`<p>_webhook_token`、
`oauth_<p>_client_secret` 三个键族，读写点清单以本文件为起点，迁移需带存量明文兼容读。

## P1-full 实弹新发现的产品缺陷（同批修复）

| # | 缺陷 | 修复 |
|---|------|------|
| B1 | GitHub issue PATCH milestone 挂载失效：github 通道翻译规则 `milestone_id→milestone_id` 同名直传，上游静默忽略（200 假成功） | providers/github.py 规则改映射 `milestone`；写后回读校验守卫扩展到 github（比对基准=请求值） |
| B2 | 代理 PATCH PR 无 close 语义：PRUpdateBody 未声明 state，pydantic 静默丢弃（github 400 空 body；gitee/gitcode 200 纯 no-op） | PRUpdateBody 增加 state（open/closed 前置校验），透传上游 PATCH body。gitcode 实弹裁决：`{"state":"closed"}` 真实生效，`state_event` 反而 400 |
| B3 | gitee artifacts/clean 422：路径参数 int 与 IK* 字符串 ident 冲突，端点对 gitee 完全不可用 | 路径参数改 `int \| str`，与 issues 族其余端点口径对齐 |
| B4 | milestone 契约文案缺陷：due_on 强制未标注；gitcode PATCH 部分更新必带全量 title+due_on 未标注 | milestones create/update 契约 description 补实测口径 |

风险点登记（不修改）：`PUT /pulls/{n}/merge` 空体即真实合并（gitcode 实弹复现
`{merged:true}`），无 dry-run 语义——merge 端点契约 description 已加 WARNING 标注。

## 附带修复：#1005/#1076 milestone 实现回补

基线 b439764fd 重写 git_repo_api.py 时丢失 #1005/#1076 实现（milestone_id id 优先解析、
400/403/422 失败语义分流、回滚+回读确认），但契约测试留在树上导致基线 17 红。本批
自 weekly-10 线（f5-acceptance-residual）移植回补，语义与既有契约测试对齐：17 红转绿。

## 回归验证口径

整改树 vs 基线（18a7dd791 同树未整改版）同批次对比：整改树零新增失败（api+contracts
批次 44→36，8 个为本批修复转绿）。基线既有失败与整改无关，QA 复审以增量口径核对。
