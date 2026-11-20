"""D109 平台对象模型建表 DDL（SQLite）。

唯一来源：docs/weekly-09/multi-platform-decouple/d109-schema.sql（设计与迁移共用同一文本，
避免两处漂移）。由 `src/api/db_setup.py` 的 register_migration("202611150007") 执行；
ORM（models_platform_object.py）只做映射，不 create_all。

MySQL 适配待办（D105d gate 机制）：本批按 SQLite 语法交付（当前部署 backend=sqlite）；
切 MySQL 前需转换 —— `DEFAULT (datetime('now'))` → `DATETIME DEFAULT CURRENT_TIMESTAMP`，
部分唯一索引（`WHERE is_default = 1`）→ 应用层守卫 + 触发器（MySQL 不支持带 WHERE 的部分索引）。
"""
from __future__ import annotations

PLATFORM_OBJECT_DDL = """
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
    repo_links_json TEXT NOT NULL DEFAULT '[]',        -- 历史副本（读源已迁 grape_t_channel_repo_link）
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
    create_repo_options INTEGER NOT NULL DEFAULT 0,   -- 1=建仓适配器接受 auto_init/default_branch
    legacy_token_symbol TEXT NOT NULL DEFAULT '',      -- 历史 token 取用函数名（deps.py PEP 562 动态解析用；兼容垫片，勿删）
    pat_create_url      TEXT NOT NULL DEFAULT '',      -- PAT 创建页直达链接（原 repo-credentials.json）
    token_storage_field TEXT NOT NULL DEFAULT '',      -- managed_repos 仓库 bot token 物理列名（''=无）
    webhook_token_field TEXT NOT NULL DEFAULT '',      -- managed_repos webhook token 物理列名（''=无）
    bot_username        TEXT NOT NULL DEFAULT '',      -- D110：该平台唯一 bot 账号名（事实；管理面写，卡仅空值播种）
    bot_token_enc       TEXT NOT NULL DEFAULT '',      -- D110：平台 bot PAT 密文（空=该平台无全局 bot，托管回落用户 PAT）
    bot_token_status    TEXT NOT NULL DEFAULT 'unknown', -- D110：ok | expired | unknown（巡检写入，替代 meta global_<p>_bot_token_status）
    bot_checked_at      TEXT NOT NULL DEFAULT '',      -- D110：最近一次 bot token 探活时间
    owner_fallback      INTEGER NOT NULL DEFAULT 0,    -- 1=允许 owner 身份 token 回落
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
    source         TEXT NOT NULL DEFAULT 'derived',    -- 行来源：config（oauth 卡显式声明）| derived（缺省派生）| manual
    config_hash    TEXT NOT NULL DEFAULT '',           -- 规范化内容 hash（按 oauth_provider 粒度）
    created_at     TEXT NOT NULL DEFAULT (datetime('now')), -- 创建时间
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')), -- 最近更新时间
    UNIQUE (oauth_provider, repo_provider)             -- 同一对渠道-平台只允许一行（repo_provider 唯一性放导入期校验，不放 DB 索引）
);

CREATE TABLE IF NOT EXISTS grape_t_user_repo_platform_link (
    id             TEXT PRIMARY KEY,                   -- UUID（uuid7_str()，Python 侧生成，禁自增）
    user_id        TEXT NOT NULL,                      -- 用户 id（grape_t_user_accounts.id）
    repo_provider  TEXT NOT NULL,                      -- 仓库平台 key（= grape_t_repo_platforms.provider）
    linked_at      TEXT NOT NULL DEFAULT '',           -- 关系首次建立时间（事实，派生不出来）
    revoked_at     TEXT NOT NULL DEFAULT '',           -- 撤销时间；空=关系有效
    revoked_reason TEXT NOT NULL DEFAULT '',           -- user_unbind | credential_deleted | oauth_unbound | admin | disabled_by_user
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),  -- 行创建时间
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),  -- 最近更新时间
    UNIQUE (user_id, repo_provider)                    -- 一个用户对一个平台只有一行
);

"""


def iter_statements(ddl: str | None = None) -> list[str]:
    """把 DDL 文本切成单条语句，供 `cursor.execute()` 逐条执行。

    **不要用 `executescript`**：它是 sqlite3 专有 API，不经过 `mysql_compat`
    的方言翻译层，MySQL 后端下会直接 1064。项目既有建表迁移一律逐条 execute
    （db_setup.py:4788/5021），这里对齐同一写法。

    P2-1：原实现按 `;` + 空白 + 换行的正则切分，**注释里出现"分号+换行"**就会把
    注释尾巴拼进下一条语句（实测注入后切出 22 条碎片）。改为逐字符扫描：识别
    `--` / `#` / `/* */` 注释与 ' " ` 引号，只有"语句态"下的分号才断句。
    """
    text = PLATFORM_OBJECT_DDL if ddl is None else ddl
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    quote = ""
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if quote:
            buf.append(ch)
            if ch == quote and (i == 0 or text[i - 1] != "\\"):
                quote = ""
            i += 1
            continue
        if ch == "-" and nxt == "-":
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if ch == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if ch == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            buf.append(" ")
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out

