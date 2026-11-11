# D90-N5：OAuth/PAT 配置化平台化收口（评审与落地记录，2026-11-07）

**来源**：用户拍板——挂账 #1/#3/#4 必须实现，否则"OAuth 配置化平台化和
仓库配置支持配置化平台化"需求不算达成。参照 Gitee 官方登录页多渠道形态。

**状态**：已落地——867b38f68。测试 test_n5_platform_config（UC37）。

---

## 一、参照基准（Gitee 登录页实抓分析）

抓取 `https://gitee.com/login` HTML 分析其多渠道实现：
- **统一路由**：`/auth/<provider>` 一个回调处理器吃所有平台（aliyun/gitlab/
  huawei/github + 折叠的 weibo/qq/windowslive/wechat/dingding/openatom）
- **动态渲染**：主流平台平铺 + 长尾折叠进"更多"
- **品牌图标**：每平台 iconfont + title 提示

**对照结论**：我们的骨架已经是配置化形态——`/auth/providers` 动态渲染
（R7.5 全平台始终显示）+ `oauth_callback(provider)` 参数化回调 +
`_oauthIcon` 未知平台首字母降级。**差距不在架构，在渠道卡定义与三处
配置化收尾**。

## 二、落地四件

### N5-1 gitee OAuth 渠道卡（providers 内置默认）
- endpoints 按 Gitee 官方 OAuth 文档（authorize/token/api/v5/user）
- `whitelist_column=gitee_username`（联动白名单动态建列）
- PKCE + 品牌色 #c71d23 + scope user_info emails
- 登录页 gitee SVG 图标（_OAUTH_ICONS）
- **启用方式**：管理员在管理面填 `oauth_gitee_client_id/secret`——零代码

### N5-2 前端 PAT 入口（挂账 #1）
- 实码发现：绑定 tab 平台清单来自 `/auth/providers`——渠道卡在册后
  gitee 自动出现（后端零改动）
- manual 绑定提交体：存量双平台走专用字段（S52 防双路径写同一域），
  **新平台走 `platform_tokens` 泛型**（settings.py 泛型入口 D32/S52
  本就就绪：verify + 双写 user_credentials `<p>_pat` + external_identities）

### N5-3 login_whitelist 列驱动（挂账 #4）
- **实码发现推翻原登记**：`_ensure_whitelist_columns` 已是配置驱动动态
  建列（provider 卡 whitelist_column 派生）——gitee 渠道卡在册即自动建
  `gitee_username` 列（探针实锤），原登记"物理仅两列"过时
- 本批补 admin 面列驱动化：`_whitelist_column_for` 配置派生（原
  `_LOGIN_WHITELIST_COLS` 写死双渠道映射删除）+
  `list_login_whitelist` PRAGMA 列清单组装

### N5-4 display strip 统一（挂账 #3）
- `stripRepoPrefix` 正则清单含 gitee（app.js helper）
- observability.js **8 处**内联 `replace('gitcode:','')` 全部统一为
  stripRepoPrefix（D78 挂账收口，零内联回潮）

## 三、语义演进登记（非回归）

1. **s60 PAT-only 断言更新**：gitee 渠道卡在册后从"PAT-only 平台"变为
   "渠道∩平台双卡链接平台"——正是 N5 目标；PAT-only 断言改用未注册渠道
2. **s63 swap 断言修为参数级**：N3 参数化后 SQL 文本无列名，列名断言
   改参数断言（平台隔离语义不变）

## 四、验证

- test_n5 5 用例（UC37①-⑤）+ 宽域 122 passed 零回归
- JS 语法 node --check（app.js/observability.js/account.js）

## 五、平台接入终态（配置化平台化兑现）

新平台全链接接入 = ① 管理面配平台卡（VCS 域）+ ② 管理面填 OAuth
client_id/secret（登录域）——**纯配置，零代码**。登录/PAT 绑定/白名单/
TokenResolver 消费全链路自动生效。
