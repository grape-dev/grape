> ⚠️ 本文为拆分分片（内容唯一权威源）。索引导航：./multi-platform-decouple-design.md
> 来源：multi-platform-decouple-design.md L1-L88, L3037-L3046（原文原样搬运，零改写）。
> 域定义：目标形态与实施主线——总体架构、分批策略、实施顺序。
> 全局约束：继承索引页「表设计铁律/实现纪律/验收纪律」（见索引页）。

# 多平台支持 + OAuth 凭据解耦 — 设计文档

> 需求文档：multi-platform-decouple-requirements.md（场景与用例清单）
> 承接：grape-auth-audit v3.27 §5.6（R1 解耦设计）与 §G1/K1-K4 问题定位。

## 设计决策

### D1：平台配置卡——一条配置驱动四层

复用 auth/providers.py 已验证的机制（配置文件 → meta 表 → 内存缓存 + TTL/主动失效），
把 `oauth_providers_config` 升级为统一的 **vcs_providers_config**，每平台一条配置：

> D10 修订：实施时拆为**两张配置卡**（登录渠道卡 oauth_providers_config + 仓库平台卡
> vcs_providers_config），本节示例为两卡同名关联时的合并视图；PAT-only 平台只配仓库卡。
> 卡内字段归属以 D10 为准。

```json
"gitee": {
  "name": "Gitee",
  "dialect": "github",
  "host": "gitee.com",
  "auth_url": "https://gitee.com/oauth/authorize",
  "token_url": "https://gitee.com/oauth/token",
  "user_url": "https://gitee.com/api/v5/user",
  "webhook_path": "/webhook/gitee",
  "event_header": "X-Gitee-Event",
  "api_base": "https://gitee.com/api/v5",
  "clone_auth": {"scheme": "basic_password", "user": "", "token_env": "BOT_GITEE_TOKEN"},
  "scope": "user_info projects",
  "id_field": "id", "login_field": "login", "name_field": "name",
  "whitelist_column": "gitee_username"
}
```

- 四层同源取配置：OAuth 登录（auth/providers.py）、webhook 归一（vcs/adapter.py）、
  git 传输（repogit.py host/auth scheme/token env）、API 客户端（token_resolver.py）。
- gitcode/github/gitlab 三平台内置默认配置（代码内 _DEFAULT，向前兼容，行为不变）；
  gitee 起所有新平台纯配置接入。
- 刻意不做：全量 JSON-path webhook 映射 DSL。实测四大平台只有两个方言
  （github 系平铺 payload / gitlab 系 object_kind+object_attributes），
  DSL 属过度设计。

### D2：webhook 层——方言模板 + 配置覆盖

现状：vcs/gitcode.py 是混合体（同时处理 GitHub 风格 "pull_request" 与 GitLab 风格
"Issue Hook"/"Note Hook"/"Merge Request Hook"），证明纯选方言不够，需允许覆盖。

- 新增两个方言模板类：GitHubDialectBackend / GitLabDialectBackend，
  内置字段提取（repository/actor/branch/title）、action 细分、changes 归一。
- 每平台配置 = {dialect, event_header, webhook_path, event_map 覆盖项}。
  gitee = {dialect: "github", event_header: "X-Gitee-Event"}，零新代码。
- gitcode 的混合行为通过其内置配置的 event_map 覆盖项表达（GitLab 风格 header
  名映射进 github 方言的 event 表），行为不变、有回归测试锁住。
- 非标准平台继续走 register_vcs_backend 子类插件口（现状保留）。

### D3：OAuth 与仓库凭据解耦（K1-K4 消除）

- K1：deps.py 两个包装器合并为
  `get_user_token_with_refresh(user_id, provider)`，provider 由 repo_key 前缀推导
  （parse_repo_key），不再接收 username 做身份匹配。
- K2：oauth_token.py 的 if provider 分叉改为统一调用 K1 合并后的包装器。
- K3：token_resolver.create_provider 改为从配置卡注册表构建
  （GitLabProvider 从不可达变可达）；VCSProvider 子类仅承载 API endpoint 差异。
- K4：删除 get_user_by_gitcode_id 登录身份兜底。删除前跑双 id 错位数据检测
  （只检测+报告，不自动修数据；历史错位走一次性人工修复脚本，不入交付分支）。
- PAT 语义：OAuth token 自动刷新；手配 PAT 不刷新不轮换，失效时返回
  "PAT 已失效，请在平台重新生成"（S4 文案）。

### D4：git 传输层去硬编码（G1-G4）

- G1：_HOSTS 从配置卡 host 字段取值；未知 provider 不再静默回退 gitcode.com，
  显式返回 None + 报错（fail-fast，消灭静默错误 clone 地址）。
- G2：_basic_auth 按配置卡 clone_auth.scheme 生成：
  - gitcode/github：Basic x-access-token:<token>（现状，回归锁）
  - gitlab：Basic oauth2:<token>
  - gitee：Basic <user或空>:<token>
- G3：askpass 环境变量链由配置卡 token_env 派生（BOT_GITEE_TOKEN 等），
  fallback 链通用化为 本平台 BOT/USER → BOT_TOKEN。
- G4：repo_manager.py URL 内联 token 改为复用 repogit._run_git 的
  http.extraheader 注入路径，token 不再出现在进程参数。

### D5：分批与零 DDL 纪律

- 本批零 DDL、零数据迁移。managed_repos 分列泛型化（收 bot_gitcode_token 等
  死列家族）为批次 1，单独设计单独 MR。
- 最坏伤害半径 = 回滚一个 MR；gitee 为纯新增路径，不动存量三平台行为
  （S8/S9 回归用例锁住）。

## 实施顺序

1. X 用例先行：S5/S22 结构守卫与只读检测 + S2/S8 回归锁（先锁现状，再动刀）
2. D1 配置卡 + D2 方言模板 + D12 验签/注册收口（vcs 层重构，S16/S17/S20 随步验收，行为不变）
3. D14 平台卡管理面 + 动态路由（S18/S19）
4. D4 git 层（含 G1 fail-fast 修复，S8 随步）
5. D3 K1-K4 解耦（S1/S3/S4/S10/S11/S15）
6. gitee 双卡接入：S12（PAT-only 全链路）全绿；S21（OAuth 全链路端到端）
   待目标环境验收（D100 后产物断言=lazy 链全绿，非凭据行存在）
7. S13/S14 收尾 + S9 全量回归

