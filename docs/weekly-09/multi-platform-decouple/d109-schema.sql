-- D109 平台对象模型 schema 草案（2026-11-13）
-- 规范（yangaiwu 拍板）：
--   1) 表名前缀 grape_t_（与既有 grape_t_repo_credentials / grape_t_patrol_findings 一致）
--   2) 主键一律 UUID（TEXT PRIMARY KEY，Python 侧 uuid7_str()（src/kernel/id_gen，时间有序） 生成，禁自增）
--   3) 每表带 created_at / updated_at（TEXT，默认 datetime('now')；updated_at 由应用层写入时刷新）
--   4) 每个字段都有注释；不写 DB 级 REFERENCES 级联（与既有表一致，关系由应用层维护）
--
-- 归属分层（§3 第 5 条边界纪律）：
--   配置派生（导入器可覆盖，有 config_hash / drifted / orphaned）：
--     grape_t_oauth_channels / grape_t_repo_platforms / grape_t_channel_repo_link
--   凭据能力表（grape_t_repo_credential_caps）已于 2026-11-14 删除：存活字段并入 grape_t_repo_platforms
--   运行态（导入器绝不触碰）：
--     grape_t_user_platform_link / grape_t_bot_accounts
--     + 既有凭证域 grape_t_repo_credentials（本次加 derived_from / created_at）

-- ========== 一、配置派生三表 ==========

CREATE TABLE IF NOT EXISTS grape_t_oauth_channels (
    id              TEXT PRIMARY KEY,                  -- UUID（uuid7_str()（src/kernel/id_gen，时间有序），Python 侧生成，禁自增）
    provider        TEXT NOT NULL,                     -- 渠道 key；= configs/oauth-providers.json 的 key，也是 /api/auth/login/<provider> 路径段
    name            TEXT NOT NULL DEFAULT '',          -- 显示名（登录按钮文案）
    type            TEXT NOT NULL DEFAULT 'oauth2',    -- 协议模式：oauth2 | oidc | saml（越界拒绝）
    auth_url        TEXT NOT NULL DEFAULT '',          -- 授权页端点（用户跳转入口）
    token_url       TEXT NOT NULL DEFAULT '',          -- 换取 access_token 的端点
    user_url        TEXT NOT NULL DEFAULT '',          -- 拉取用户信息的端点；可含 {access_token} 占位（华为/部分平台需要）
    emails_endpoint TEXT NOT NULL DEFAULT '',          -- 拉取邮箱的端点；空=该渠道不支持邮箱
    scope           TEXT NOT NULL DEFAULT '',          -- 登录申请 scope（最小权限，空格分隔；Gitee 只 user_info）
    pkce_enabled    INTEGER NOT NULL DEFAULT 0,        -- 1=启用 PKCE；Gitee 必须 0（不认 PKCE，会导致每次弹授权）
    id_field        TEXT NOT NULL DEFAULT 'id',        -- 用户信息里唯一 id 的字段名（华为=openID）
    login_field     TEXT NOT NULL DEFAULT 'login',     -- 登录名的字段名（华为无登录名，用 openID）
    name_field      TEXT NOT NULL DEFAULT 'name',      -- 显示名的字段名
    avatar_field    TEXT NOT NULL DEFAULT '',          -- 头像字段名；空=由平台默认规则派生
    redirect_uri    TEXT NOT NULL DEFAULT '',          -- 回调地址；空=由 server_base_url 派生 /api/auth/callback/<provider>
    brand_color     TEXT NOT NULL DEFAULT '',          -- 品牌色（登录按钮/标识展示）
    accept_header   TEXT NOT NULL DEFAULT '',          -- 请求 Accept 头（GitHub=application/vnd.github+json）
    client_id_enc     TEXT NOT NULL DEFAULT '',        -- OAuth 客户端 ID 密文（配置 JSON 不含凭据；管理面写入，加密存储）
    client_secret_enc TEXT NOT NULL DEFAULT '',        -- OAuth 客户端密钥密文（_encrypt_repo_secret，明文永不出库）
    repo_links_json TEXT NOT NULL DEFAULT '[]',        -- 关联声明 JSON 数组：[{"repo_provider":"gitee","relation":"identity_only"}]
    source          TEXT NOT NULL DEFAULT 'config',    -- 行来源：config（文件导入）| manual（管理面新增）
    config_hash     TEXT NOT NULL DEFAULT '',          -- 规范化内容 hash（行级，非文件级；导入/启动扫描比对用）
    enabled         INTEGER NOT NULL DEFAULT 1,        -- 运行时开关（DB 权威，配置文件不覆盖）
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),  -- 首次导入/创建时间（UTC）
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),  -- 最近一次定义字段更新时间（应用层刷新）
    UNIQUE (provider)                                  -- 一个渠道一行；provider 全局唯一
);

CREATE TABLE IF NOT EXISTS grape_t_repo_platforms (
    id                  TEXT PRIMARY KEY,              -- UUID（uuid7_str()（src/kernel/id_gen，时间有序），禁自增）
    provider            TEXT NOT NULL,                 -- 仓库平台 key；= configs/platforms/<provider>.json 文件名，也是 repo_key 前缀
    name                TEXT NOT NULL DEFAULT '',      -- 平台显示名（前端按钮/文案）
    dialect             TEXT NOT NULL DEFAULT '',      -- API 方言族：github | gitlab（无 gitee；Gitee 走 github 方言）
    host                TEXT NOT NULL DEFAULT '',      -- 平台主域名（clone URL / 展示 / 探活基准）
    api_base            TEXT NOT NULL DEFAULT '',      -- REST API 根（端点 = api_base + 模块派生路径）
    api_auth            TEXT NOT NULL DEFAULT '',      -- 令牌传递形态：query_token | token_header | private_token | bearer_header
    webhook_path        TEXT NOT NULL DEFAULT '',      -- webhook 接收路径（全局唯一，如 /webhook/gitee）
    event_header        TEXT NOT NULL DEFAULT '',      -- 事件类型请求头名（如 X-Gitee-Event）
    webhook_verify      TEXT NOT NULL DEFAULT '',      -- 验签方式：hmac | token_compare
    webhook_dialect     TEXT NOT NULL DEFAULT '',      -- webhook 载荷族：github_web | gitee_v5（未声明则托管 fail-closed）
    pr_head_filter      TEXT NOT NULL DEFAULT '',      -- PR 列表 head 参数形态：bare_ref | owner_ref（形态错会静默空集）
    clone_auth          TEXT NOT NULL DEFAULT '',      -- clone 凭据形态：x_access_token | oauth2 | basic_password
    issue_write_form    TEXT NOT NULL DEFAULT '',      -- 建/关 Issue 端点形态：repo_path | owner_param（Gitee=owner_param）
    branch_create_form  TEXT NOT NULL DEFAULT '',      -- 建分支形态：form | refs_sha
    create_repo_options INTEGER NOT NULL DEFAULT 0,   -- 1=适配器接受 auto_init/default_branch（建仓可选参数）
    issue_search        INTEGER NOT NULL DEFAULT 0,    -- 1=支持原生 issue 搜索端点（布尔，非枚举）
    git_tree_family     TEXT NOT NULL DEFAULT '',      -- 文件树/内容 API 族：github_api | （空=不支持）
    pr_comment_endpoint TEXT NOT NULL DEFAULT '',      -- PR 评论端点族：issues（借 issue 评论接口）| 空=专用端点
    pr_labels_via_issues INTEGER NOT NULL DEFAULT 0,   -- 1=标签走 issues 接口
    workflows_write     INTEGER NOT NULL DEFAULT 0,    -- 1=允许写 .github/workflows（能力位；第二批迁能力矩阵）
    legacy_token_symbol TEXT NOT NULL DEFAULT '',      -- 历史 token 取用函数名（deps.py PEP 562 动态解析用；兼容垫片，勿删）
    pat_create_url       TEXT NOT NULL DEFAULT '',     -- PAT 创建页直达链接（原 caps 字段，2026-11-14 合表）
    token_storage_field  TEXT NOT NULL DEFAULT '',     -- managed_repos bot token 物理列名（''=无）
    webhook_token_field  TEXT NOT NULL DEFAULT '',     -- managed_repos webhook token 物理列名（''=无）
    bot_username         TEXT NOT NULL DEFAULT '',     -- 配置声明的默认 bot 账号名
    bot_token_enc        TEXT NOT NULL DEFAULT '',      -- 平台 bot PAT 密文（空=该平台无全局 bot，托管回落用户 PAT）
    bot_token_status     TEXT NOT NULL DEFAULT 'unknown', -- ok | expired | unknown（巡检写入，替代 meta global_<p>_bot_token_status）
    bot_checked_at       TEXT NOT NULL DEFAULT '',      -- 最近一次 bot token 探活时间
    owner_fallback       INTEGER NOT NULL DEFAULT 0,   -- 1=允许 owner 身份 token 回落
    download_base       TEXT NOT NULL DEFAULT '',      -- 归档/文件下载基址（展示用）
    user_images_base    TEXT NOT NULL DEFAULT '',      -- 用户图片基址（展示用）
    brand_color         TEXT NOT NULL DEFAULT '',      -- 品牌色（展示用）
    source              TEXT NOT NULL DEFAULT 'config',-- 行来源：config | manual
    config_hash         TEXT NOT NULL DEFAULT '',      -- 规范化内容 hash（行级）
    enabled             INTEGER NOT NULL DEFAULT 1,    -- 运行时开关（DB 权威）
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),  -- 创建时间
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),  -- 最近更新时间
    UNIQUE (provider)
);

CREATE TABLE IF NOT EXISTS grape_t_channel_repo_link (
    id             TEXT PRIMARY KEY,                   -- UUID（uuid7_str()（src/kernel/id_gen，时间有序），禁自增）
    oauth_provider TEXT NOT NULL,                      -- 登录渠道 key（指向 grape_t_oauth_channels.provider）
    repo_provider  TEXT NOT NULL,                      -- 仓库平台 key（指向 grape_t_repo_platforms.provider）
    relation       TEXT NOT NULL DEFAULT 'identity_only', -- 授权语义：identity_only（只认身份）| identity_and_credentials（允许发起独立仓库授权）
    source         TEXT NOT NULL DEFAULT 'derived',    -- 行来源：config（oauth 卡显式声明）| derived（缺省派生）| manual
    config_hash    TEXT NOT NULL DEFAULT '',           -- 规范化内容 hash（按 oauth_provider 粒度）
    created_at     TEXT NOT NULL DEFAULT (datetime('now')), -- 创建时间
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')), -- 最近更新时间
    UNIQUE (oauth_provider, repo_provider)             -- 同一对渠道-平台只允许一行（repo_provider 唯一性放导入期校验，不放 DB 索引）
);

-- ========== 二、运行态三表 ==========

CREATE TABLE IF NOT EXISTS grape_t_user_platform_link (
    id            TEXT PRIMARY KEY,                    -- UUID（uuid7_str()（src/kernel/id_gen，时间有序），禁自增）
    user_id       TEXT NOT NULL,                       -- 用户 id（grape 用户主键）
    repo_provider TEXT NOT NULL,                       -- 仓库平台 key
    via_bits      INTEGER NOT NULL DEFAULT 0,          -- 获得途径位图：bit0=oauth_login(1) | bit1=pat(2) | bit2=manual(4)；增减用原子位运算 | / &
    first_linked_at TEXT NOT NULL DEFAULT '',          -- 该关系首次建立时间
    last_linked_at  TEXT NOT NULL DEFAULT '',          -- 最近一次追加获得途径的时间
    revoked_at    TEXT NOT NULL DEFAULT '',            -- 撤销时间；非空=已撤销（via_bits 归零后同事务写入）
    revoked_reason TEXT NOT NULL DEFAULT '',           -- 撤销原因：user_unbind | credential_deleted | oauth_unbound | admin
    created_at    TEXT NOT NULL DEFAULT (datetime('now')), -- 行创建时间
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')), -- 最近更新时间（含复活时清 revoked_at）
    UNIQUE (user_id, repo_provider)                    -- 一个用户对一个平台只有一条关系行
);

CREATE TABLE IF NOT EXISTS grape_t_bot_accounts (
    id            TEXT PRIMARY KEY,                    -- UUID（uuid7_str()（src/kernel/id_gen，时间有序），禁自增）
    provider      TEXT NOT NULL,                       -- 仓库平台 key（bot 归属 repo_provider，不归 oauth）
    username      TEXT NOT NULL,                       -- 平台上的 bot 账号登录名（身份的唯一定位）
    token_enc     TEXT NOT NULL DEFAULT '',            -- bot token 密文（_encrypt_repo_secret；明文永不出库）
    is_default    INTEGER NOT NULL DEFAULT 0,          -- 1=该平台默认账号（每平台至多一行，见下方部分唯一索引）
    status        TEXT NOT NULL DEFAULT 'active',      -- active | disabled（第一批无池选号策略，只有二态）
    token_status  TEXT NOT NULL DEFAULT 'unknown',     -- ok | expired | unknown（巡检写入，对应旧 global_<p>_bot_token_status）
    last_used_at  TEXT NOT NULL DEFAULT '',            -- 最近一次被选中使用的时间（选号/巡检参考）
    last_checked_at TEXT NOT NULL DEFAULT '',          -- 最近一次探活时间
    created_at    TEXT NOT NULL DEFAULT (datetime('now')), -- 创建时间
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')), -- 最近更新时间（token 轮换时刷新）
    UNIQUE (provider, username)                        -- 同一平台同一账号名只能一行
);

-- 平台默认账号唯一：每平台至多一行 is_default=1（部分唯一索引，SQLite/MySQL 8 均支持）
CREATE UNIQUE INDEX IF NOT EXISTS uk_grape_t_bot_accounts_default
    ON grape_t_bot_accounts(provider) WHERE is_default = 1;


-- ========== 三、既有凭证域补列（不是新表）==========
-- grape_t_repo_credentials 现有：id(VARCHAR PK) / repo_key / cred_type / value / updated_at，UNIQUE(repo_key, cred_type)
-- 迁移（应用层判列存在后执行，SQLite 不支持 ADD COLUMN IF NOT EXISTS）：
--   ALTER TABLE grape_t_repo_credentials ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
--   ALTER TABLE grape_t_repo_credentials ADD COLUMN derived_from TEXT NOT NULL DEFAULT '';
-- derived_from：该行凭据的来源账号 "provider:username"（托管时从默认账号复制则写入）；空 = 用户自定义，轮换不得覆盖

-- ========== 四、跨实例一致性（meta 键，不建表）==========
-- meta.config_state = {"epoch": <int>, "migrated": <bool>}：每次导入 / 管理面写表 epoch += 1；
-- 各实例取表缓存时比对 epoch，变化即刷新（300s TTL 降为兜底）