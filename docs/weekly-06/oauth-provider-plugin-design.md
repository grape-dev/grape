# OAuth Provider 可插拔插件化设计方案（v5）

> v3 变更：配置文件从 global-bot-token.json 分离到独立的 oauth-providers.json
> v4 变更：架构审查优化——统一 migrate 入口消除三处重复、清理 db_setup.py 废弃迁移、admin 配置分流精确映射、回退 global-bot-token.json 为原始状态
> v5 变更：D26 统一回调地址 /api/auth/oauth/callback（所有平台共用）+ oauth_status 动态化 + ConfigBody 删除 redirect_uri + 设计文档凭据示例改为占位符

## 数据来源链路追溯

```
阶段1: configs/accounts.json（多租户配置，无 OAuth）
  ↓ 迁入 DB
阶段2: user_accounts 表（加 OAuth 列，手工配置）
  ↓ accounts.json 废弃，收敛到
阶段3: configs/global-bot-token.json（OAuth 凭据 + bot 配置混入）
  ↓ migrate 启动时同步
阶段4: meta 表（运行时读取） + providers.py（硬编码协议参数）
  ↓ 配置分离
阶段5: configs/oauth-providers.json（独立 OAuth 配置） + global-bot-token.json（回归纯 Bot 配置）
```

## 问题分析

### 问题 1：Provider 硬编码分布在 5 个文件中

providers.py / grape_auth.py / config.py / user/db.py / db_setup.py 中的 if-else 分叉。新增一个平台需要改全部 5 个文件，违反需求 6"标准 OAuth 2.0 平台通过配置文件接入，无需改代码"。

### 问题 2：账号体系配置与 Bot 运行时配置职责混淆

global-bot-token.json 同时承载三个关注点：Bot 运行时配置（API token、webhook token、bot 用户名）、账号体系 OAuth 凭据（client_id/secret）、账号体系 OAuth 协议参数（oauth_providers）。

导致的问题：
1. 故障域耦合——JSON 格式错误同时影响 Bot 运行时和 OAuth 登录
2. 权限边界模糊——管理 bot token 的运维人员能看见 OAuth client_secret
3. 文件名误导——"Bot Token"文件名不暗示里面有账号体系 OAuth 配置
4. migrate 逻辑重复——三处独立从配置文件读 OAuth 写 meta 表

对应需求 6："用户账号登录配置与自动化 Bot 运行时配置各自独立管理……各自独立存放、独立变更，一方配置的修改或异常不影响另一方的正常运行"。

### 问题 3：migrate 逻辑三处重复

| 位置 | 读取来源 | 写入 meta | 问题 |
|------|---------|----------|------|
| user/db.py migrate_accounts_from_json | global-bot-token.json | client_id/secret 幂等 + oauth_providers_config 覆盖 | 只读旧文件，不读 oauth-providers.json |
| db_setup.py _migrate_tables 3096-3145 | global-bot-token.json | client_id/secret 幂等（含 redirect_uri）+ default 账户迁移 | 还在迁移 D27 已废弃的 redirect_uri |
| v3 设计 migrate_oauth_providers | oauth-providers.json 优先，fallback global-bot-token.json | client_id/secret 幂等 + oauth_providers_config 覆盖 | 设计了但未替代上面两处 |

### 问题 4：global-bot-token.json 已被 v2 方案改动

上一轮实施往 global-bot-token.json 加了 oauth_providers 字段。v4 要求 global-bot-token.json 回归原始纯 Bot 配置状态——删除 oauth_providers 字段，旧 gitcode_oauth_client_id/secret 保留（向前兼容 fallback）。

## 设计方案

### 核心思路：独立配置文件 + 统一 migrate 入口 + meta 表 + 代码内置默认 + 消除 if-else

```
configs/oauth-providers.json（新增，账号体系 OAuth 配置）
  ├── gitcode: { name, auth_url, ..., client_id, client_secret }
  └── github: { name, auth_url, ..., client_id, client_secret }
      │
      │ migrate_oauth_providers() 统一入口启动时同步
      ▼
  meta 表
  ├── oauth_{provider}_client_id       ← 凭据（幂等同步，仅首次写入）
  ├── oauth_{provider}_client_secret
  └── oauth_providers_config           ← 协议参数（覆盖同步，配置文件是真相源）
      │
      │ providers.py _load_providers 运行时加载
      ▼
  _PROVIDERS_CACHE（内存）

configs/global-bot-token.json（回归纯 Bot 配置）
  ├── gitcode_api_token                ← Bot 运行时（不变）
  ├── gitcode_webhook_token            ← Bot 运行时（不变）
  ├── bot_usernames                    ← Bot 运行时（不变）
  ├── webhook_url                      ← Bot 运行时（不变）
  ├── gitcode_oauth_client_id          ← 旧字段保留（向前兼容 fallback，新代码不主动读）
  └── gitcode_oauth_client_secret      ← 旧字段保留（向前兼容 fallback，新代码不主动读）
  （oauth_providers 字段删除——已移到 oauth-providers.json）
```

### 1. 配置文件：configs/oauth-providers.json（新增）

```json
{
  "gitcode": {
    "name": "GitCode",
    "auth_url": "https://gitcode.com/oauth/authorize",
    "token_url": "https://gitcode.com/oauth/token",
    "user_url": "https://gitcode.com/api/v5/user",
    "scope": "all",
    "id_field": "id",
    "login_field": "login",
    "name_field": "nickname",
    "pkce_enabled": false,
    "brand_color": "#e7330f",
    "whitelist_column": "gitcode_username",
    "accept_header": "application/json",
    "client_id": "<your_gitcode_client_id>",
    "client_secret": "<your_gitcode_client_secret>"
  },
  "github": {
    "name": "GitHub",
    "auth_url": "https://github.com/login/oauth/authorize",
    "token_url": "https://github.com/login/oauth/access_token",
    "user_url": "https://api.github.com/user",
    "scope": "repo user admin:repo_hook offline_access",
    "id_field": "id",
    "login_field": "login",
    "name_field": "name",
    "pkce_enabled": false,
    "brand_color": "#24292f",
    "whitelist_column": "github_username",
    "accept_header": "application/vnd.github+json",
    "client_id": "",
    "client_secret": ""
  }
}
```

设计要点：
- 协议参数和凭据在同一个 provider 条目里——同一平台的 OAuth 配置内聚
- 新增平台：在此 JSON 加一个 provider 条目 → 重启生效，不改任何 Python 代码
- global-bot-token.json 删除 oauth_providers 字段，旧 client_id/secret 保留但不被新代码主动读取

### 2. global-bot-token.json 回退为原始状态

从 global-bot-token.json 删除 oauth_providers 字段（上一轮 v2 方案加的）。保留旧 gitcode_oauth_client_id/secret 字段——migrate_oauth_providers 在 oauth-providers.json 不存在时 fallback 读取这些旧字段。

回退后的 global-bot-token.json：
```json
{
  "gitcode_api_token": "m4kj4JfiLDKLxisQpbfjURwh",
  "gitcode_oauth_client_id": "<your_gitcode_client_id>",
  "gitcode_oauth_client_secret": "<your_gitcode_client_secret>",
  "bot_usernames": ["grape_dev_bot"],
  "webhook_url": "https://grape.example.com/webhook/gitcode",
  "gitcode_webhook_token": "hBz31z3uAwQRzHpycsNT7W6e"
}
```

### 3. 向前兼容：三级 fallback 加载策略

```
oauth-providers.json 存在？
  ├─ 是 → 从 oauth-providers.json 读取协议参数 + 凭据，同步到 meta 表
  └─ 否 → global-bot-token.json 有旧 OAuth 字段？
           ├─ 是 → 从 global-bot-token.json 读取凭据（gitcode_oauth_client_id 等），同步到 meta 表
           │        协议参数用代码内置 _DEFAULT_PROVIDERS 兜底
           └─ 否 → 代码内置 _DEFAULT_PROVIDERS 兜底
```

兼容性矩阵：

| 场景 | oauth-providers.json | global-bot-token.json 旧字段 | meta 表 | 协议参数来源 | 结果 |
|------|---------------------|------------------------------|---------|-------------|------|
| 旧环境 + 新代码 | 不存在 | 有值 | 凭据同步（幂等） | 代码内置默认 | 正常 |
| 新环境 + 新代码 | 有值 | 不影响 | 凭据+协议参数同步 | 从 DB 读取 | 正常 |
| 全新部署 | 有值 | 不存在 | 凭据+协议参数同步 | 从 DB 读取 | 正常 |
| 回滚（新→旧代码） | 有值 | 有值 | 旧代码读旧文件 | 硬编码 PROVIDERS | 正常 |

### 4. 代码内置默认配置（_DEFAULT_PROVIDERS）

```python
_DEFAULT_PROVIDERS: dict[str, dict] = {
    "gitcode": {
        "name": "GitCode",
        "auth_url": "https://gitcode.com/oauth/authorize",
        "token_url": "https://gitcode.com/oauth/token",
        "user_url": "https://gitcode.com/api/v5/user",
        "scope": "all",
        "id_field": "id",
        "login_field": "login",
        "name_field": "nickname",
        "pkce_enabled": False,
        "brand_color": "#e7330f",
        "whitelist_column": "gitcode_username",
        "accept_header": "application/json",
    },
    "github": {
        "name": "GitHub",
        "auth_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "user_url": "https://api.github.com/user",
        "scope": "repo user admin:repo_hook offline_access",
        "id_field": "id",
        "login_field": "login",
        "name_field": "name",
        "pkce_enabled": False,
        "brand_color": "#24292f",
        "whitelist_column": "github_username",
        "accept_header": "application/vnd.github+json",
    },
}
```

注意：_DEFAULT_PROVIDERS 不含 client_id/secret——凭据只从配置文件或 meta 表读取，代码里不硬编码凭据。

### 5. 统一 migrate 入口：migrate_oauth_providers

**v4 核心优化：migrate_oauth_providers 作为唯一 OAuth 配置迁移入口，替代三处重复逻辑。**

```python
def migrate_oauth_providers():
    """从配置文件同步 OAuth 配置到 meta 表。

    统一入口——替代 user/db.py migrate_accounts_from_json OAuth 部分
    和 db_setup.py _migrate_tables 3096-3145 OAuth 部分。

    优先级：oauth-providers.json > global-bot-token.json（向前兼容）。

    - 协议参数（oauth_providers_config）：覆盖模式（配置文件是真相源）
    - 凭据（client_id/secret）：幂等模式（仅首次写入，管理员可通过 API 修改不被覆盖）
    """
    config_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "configs")

    # 1. 尝试读取 oauth-providers.json
    new_path = os.path.join(config_dir, "oauth-providers.json")
    if os.path.exists(new_path):
        try:
            with open(new_path, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception as e:
            log.warning(f"Failed to load oauth-providers.json: {e}")
            data = {}
        if data:
            _sync_providers_from_config(data)
            return

    # 2. 向前兼容：读取 global-bot-token.json 中的旧 OAuth 字段
    old_path = os.path.join(config_dir, "global-bot-token.json")
    if not os.path.exists(old_path):
        return
    try:
        with open(old_path, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception as e:
        log.warning(f"Failed to load global-bot-token.json: {e}")
        return
    # 旧文件格式：扁平的 gitcode_oauth_client_id 等
    _oauth_keys = [
        ("gitcode_oauth_client_id", "oauth_gitcode_client_id"),
        ("gitcode_oauth_client_secret", "oauth_gitcode_client_secret"),
        ("github_oauth_client_id", "oauth_github_client_id"),
        ("github_oauth_client_secret", "oauth_github_client_secret"),
    ]
    loaded = []
    with db_lock:
        c = db.cursor()
        for src_key, meta_key in _oauth_keys:
            val = data.get(src_key, "") or ""
            if not val:
                continue
            c.execute("SELECT COUNT(*) FROM meta WHERE key=?", (meta_key,))
            if c.fetchone()[0] == 0:
                c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                          (meta_key, val))
                loaded.append(meta_key)
        db.commit()
    if loaded:
        log.info(f"Loaded OAuth credentials from global-bot-token.json (fallback): {loaded}")
    # 协议参数不写——由 _DEFAULT_PROVIDERS 兜底


def _sync_providers_from_config(data: dict):
    """从 oauth-providers.json 格式的配置同步到 meta 表。

    新文件格式：嵌套的 { provider: { name, auth_url, ..., client_id, client_secret } }
    - 协议参数（不含 client_id/secret）：覆盖写入 oauth_providers_config
    - 凭据（client_id/secret）：幂等写入 oauth_{provider}_client_id/secret
    """
    providers_cfg = {}
    loaded_creds = []
    with db_lock:
        c = db.cursor()
        for provider, cfg in data.items():
            # 剥离 client_id/secret，只存协议参数
            prov_cfg = {k: v for k, v in cfg.items()
                        if k not in ("client_id", "client_secret")}
            providers_cfg[provider] = prov_cfg
            # 幂等同步凭据
            for cred_field, meta_suffix in [
                ("client_id", "client_id"),
                ("client_secret", "client_secret"),
            ]:
                val = cfg.get(cred_field, "") or ""
                if not val:
                    continue
                meta_key = f"oauth_{provider}_{meta_suffix}"
                c.execute("SELECT COUNT(*) FROM meta WHERE key=?", (meta_key,))
                if c.fetchone()[0] == 0:
                    c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                              (meta_key, val))
                    loaded_creds.append(meta_key)
        # 覆盖写入协议参数
        if providers_cfg:
            import json as _json
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                      ("oauth_providers_config", _json.dumps(providers_cfg, ensure_ascii=False)))
        db.commit()
    log.info(f"Synced OAuth providers from oauth-providers.json: "
             f"providers={list(providers_cfg.keys())}, credentials={loaded_creds}")
```

### 5a. user/db.py 改造——migrate_accounts_from_json 瘦身

migrate_accounts_from_json 的 OAuth 迁移部分移交给 migrate_oauth_providers。旧函数保留但只负责 default 账户迁移（非 OAuth）：

```python
def migrate_accounts_from_json():
    """首次运行加载配置：从 global-bot-token.json 迁移旧数据。

    OAuth 配置迁移已移交给 migrate_oauth_providers()（统一入口）。
    本函数仅保留 default 账户清理（旧版 user_accounts default 账户的 OAuth 配置迁移到 meta 表）。
    """
    # OAuth 配置由 migrate_oauth_providers 统一处理
    migrate_oauth_providers()

    # default 账户迁移（旧版兼容，非 OAuth 配置分离相关）
    # ... 保留 db_setup.py 3096-3145 中的 default 账户迁移逻辑
    # 但删除 redirect_uri 迁移（D27 已废弃）
```

### 5b. db_setup.py 改造——清理废弃迁移

db_setup.py 3096-3145 的 OAuth 迁移逻辑：

| 行 | 内容 | 处理 |
|----|------|------|
| 3100-3104 | 读取 global-bot-token.json | 删除——migrate_oauth_providers 已覆盖 |
| 3105-3112 | _oauth_keys 映射表（含 redirect_uri） | 删除——redirect_uri 已在 D27 废弃 |
| 3117-3123 | client_id/secret 幂等写入 meta | 删除——migrate_oauth_providers 已覆盖 |
| 3124-3138 | default 账户迁移 | 保留——这是旧数据清理，和 OAuth 配置分离无关 |
| 3140-3143 | 日志 | 调整——只记 default 账户迁移日志 |

改造后 db_setup.py 该段：
```python
# default 账户迁移（旧版兼容——OAuth 配置曾存于 user_accounts default 账户）
# OAuth 配置迁移由 migrate_oauth_providers() 统一处理（user/db.py 模块加载时执行）
try:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT * FROM user_accounts WHERE username='default'")
        row = c.fetchone()
        if row:
            cols = [d[0] for d in c.description]
            acc = dict(zip(cols, row))
            # 迁移 default 账户中的 OAuth 配置到 meta 表（幂等）
            _oauth_keys = [
                ("gitcode_oauth_client_id", "oauth_gitcode_client_id"),
                ("gitcode_oauth_client_secret", "oauth_gitcode_client_secret"),
                ("github_oauth_client_id", "oauth_github_client_id"),
                ("github_oauth_client_secret", "oauth_github_client_secret"),
                # 不迁移 redirect_uri——D27 已废弃
            ]
            migrated = False
            for src_key, meta_key in _oauth_keys:
                val = acc.get(src_key, "") or ""
                if val:
                    c.execute("SELECT COUNT(*) FROM meta WHERE key=?", (meta_key,))
                    if c.fetchone()[0] == 0:
                        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                                  (meta_key, val))
                        migrated = True
            c.execute("DELETE FROM user_accounts WHERE username='default'")
            db.commit()
            if migrated:
                log.info("Migrated OAuth config from default account to meta table")
            else:
                log.info("Deleted default account (no config to migrate)")
except Exception as e:
    log.warning(f"Migrate/delete default account failed: {e}")
```

### 6. providers.py 改造

删除硬编码 PROVIDERS 字典，改为 _DEFAULT_PROVIDERS + _PROVIDERS_CACHE + _load_providers。

```python
_PROVIDERS_CACHE: dict[str, dict] = {}
_loaded: bool = False

def _load_providers():
    """从 meta 表加载 provider 配置，无则用代码内置默认。"""
    global _loaded
    raw = user_db.get_meta_value("oauth_providers_config") or ""
    if raw:
        try:
            data = json.loads(raw)
            _PROVIDERS_CACHE.clear()
            _PROVIDERS_CACHE.update(data)
        except Exception as e:
            log.warning(f"Failed to parse oauth_providers_config: {e}, using defaults")
            _PROVIDERS_CACHE.clear()
            _PROVIDERS_CACHE.update(_DEFAULT_PROVIDERS)
    else:
        _PROVIDERS_CACHE.clear()
        _PROVIDERS_CACHE.update(_DEFAULT_PROVIDERS)
    _loaded = True

def get_provider(provider: str) -> dict | None:
    if not _loaded:
        _load_providers()
    return _PROVIDERS_CACHE.get(provider)

def list_providers() -> list[str]:
    if not _loaded:
        _load_providers()
    return list(_PROVIDERS_CACHE.keys())
```

v4 优化：用 `_loaded` 标志位替代 `if not _PROVIDERS_CACHE` 判断——避免空缓存时重复触发 _load_providers（即使 meta 表返回空也会正确标记为已加载）。

get_user_info / _get_client_id / _get_client_secret 消除 if-else（与 v3 相同）。

### 7. grape_auth.py 改造

白名单检查、providers API、_sync_legacy_binding 消除 if-else（与 v3 相同）。D32 修复后 _sync_legacy_binding 已删除。

### 8. config.py 改造

白名单函数合并为通用 is_user_allowed，旧函数保留为 wrapper（与 v3 相同）。

### 9. user/db.py 其他改造

link_user_provider / refresh_oauth_token 通用函数（与 v3 相同）。

link_user_provider 中 col_map 硬编码 {"gitcode": "gitcode_id", "github": "github_id"} 标记为迁移期已知限制——旧表只有这两个平台列，新平台无旧表列可更新。D32 修复后 _sync_legacy_binding 和 link_gitcode_user/link_github_user 已删除，col_map 随之消失。

### 10. db_setup.py 其他改造

login_whitelist DDL 完整最终态 + _ensure_whitelist_columns 动态加列（与 v3 相同）。

### 11. admin/config_store.py 改造

**v4 优化：精确映射逻辑。**

admin 配置中心当前 ConfigBody 字段是扁平结构（gitcode_oauth_client_id 等），oauth-providers.json 是嵌套结构（gitcode: { client_id, ... }）。save_config 需要做扁平→嵌套映射。

```python
# ConfigBody 扁平字段 → oauth-providers.json provider 条目
_OAUTH_FIELD_MAP = {
    "gitcode_oauth_client_id": ("gitcode", "client_id"),
    "gitcode_oauth_client_secret": ("gitcode", "client_secret"),
    "github_oauth_client_id": ("github", "client_id"),
    "github_oauth_client_secret": ("github", "client_secret"),
}

# Bot 字段——继续写 global-bot-token.json
_BOT_FIELDS = [
    "gitcode_api_token",
    "github_bot_token",
    "gitcode_webhook_token",
    "webhook_url",
    "bot_usernames",
]
```

save_config 分流逻辑：
```python
def save_config(payload: dict) -> dict:
    # 1. Bot 字段 → global-bot-token.json
    bot_payload = {k: v for k, v in payload.items() if k in _BOT_FIELDS}
    if bot_payload:
        data = load_config()  # global-bot-token.json
        for field in _BOT_FIELDS:
            if field in bot_payload:
                data[field] = bot_payload[field]
        # 原子写
        tmp = _GBT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _GBT_PATH)

    # 2. OAuth 字段 → oauth-providers.json
    oauth_payload = {k: v for k, v in payload.items()
                     if k in _OAUTH_FIELD_MAP}
    if oauth_payload:
        _save_oauth_providers(oauth_payload)

    # 3. 同步 meta 表（OAuth 凭据）
    for flat_field, (provider, cred_field) in _OAUTH_FIELD_MAP.items():
        if flat_field in oauth_payload:
            val = oauth_payload[flat_field]
            if val:
                set_meta_value(f"oauth_{provider}_{cred_field}", val)

    return read_public_config()


def _save_oauth_providers(oauth_payload: dict):
    """把 ConfigBody 扁平 OAuth 字段写入 oauth-providers.json（嵌套结构）。"""
    path = os.path.join(CONFIG_DIR, "oauth-providers.json")
    # 读取现有配置
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            pass
    # 扁平 → 嵌套
    for flat_field, (provider, cred_field) in _OAUTH_FIELD_MAP.items():
        if flat_field in oauth_payload:
            if provider not in existing:
                existing[provider] = {}
            existing[provider][cred_field] = oauth_payload[flat_field]
    # 原子写
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
```

read_public_config 合并两个文件：
```python
def read_public_config() -> dict:
    # Bot 配置从 global-bot-token.json
    bot_data = load_config()
    # OAuth 配置优先从 meta 表
    out = {}
    for field in _BOT_FIELDS:
        val = bot_data.get(field, "")
        if isinstance(val, list):
            out[field] = val
        elif val:
            tail = val[-4:] if len(val) >= 4 else val
            out[f"{field}_present"] = True
            out[f"{field}_tail"] = tail
    # OAuth 字段从 meta 表读取（运行时权威）
    for flat_field, (provider, cred_field) in _OAUTH_FIELD_MAP.items():
        meta_key = f"oauth_{provider}_{cred_field}"
        val = get_meta_value(meta_key) or ""
        if val:
            out[f"{flat_field}_present"] = True
            out[f"{flat_field}_tail"] = val[-4:] if len(val) >= 4 else val
            # client_id 非敏感，回显明文
            if cred_field == "client_id":
                out[flat_field] = val
    # oauth_status（动态从 provider 列表生成，不硬编码平台名）
    from src.auth.providers import list_providers
    out["oauth_status"] = {
        p: bool(get_meta_value(f"oauth_{p}_client_id"))
        for p in list_providers()
    }
    return out
```

ALL_FIELDS 和 _META_SYNC_MAP 调整：
```python
ALL_FIELDS = _BOT_FIELDS + list(_OAUTH_FIELD_MAP.keys())

# _META_SYNC_MAP 不再需要——save_config 直接同步 meta 表
# 旧 _META_SYNC_MAP 删除
```

### 12. 启动流程

```
服务启动
  → user/db.py 模块加载
    → init_db()                        ← 建表
    → migrate_oauth_providers()        ← 统一入口：同步配置文件到 meta 表
                                         （优先 oauth-providers.json，fallback global-bot-token.json）
  → init_database()
    → _migrate_tables()                ← default 账户清理（不含 OAuth 迁移，已移交）
    → _ensure_whitelist_columns()      ← 动态加白名单列（调用 list_providers → _load_providers）
  → 运行时 providers._load_providers() ← lazy loading，首次调用 get_provider 时触发
```

### 13. 新增平台流程

新增 Gitee 平台：
1. 在 configs/oauth-providers.json 加 `"gitee": { name, auth_url, ..., client_id, client_secret }`
2. 重启服务

不改任何 Python 代码。

## 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| configs/oauth-providers.json | 新增 | OAuth 平台配置（协议参数 + 凭据） |
| configs/global-bot-token.json | 修改 | 删除 oauth_providers 字段，旧 client_id/secret 保留 |
| src/auth/providers.py | 修改 | _DEFAULT_PROVIDERS + _PROVIDERS_CACHE + _loaded 标志位 + _load_providers + 消除 if-else |
| src/api/routes/grape_auth.py | 修改 | 白名单/API/旧表同步消除 if-else（已实施） |
| src/config.py | 修改 | 白名单函数合并为通用 is_user_allowed（已实施） |
| src/user/db.py | 修改 | 新增 migrate_oauth_providers 统一入口 + link_user_provider + refresh_oauth_token + migrate_accounts_from_json 瘦身 |
| src/api/db_setup.py | 修改 | 删除 OAuth 迁移（移交 migrate_oauth_providers）+ 删除 redirect_uri 迁移 + 保留 default 账户清理 + login_whitelist DDL + _ensure_whitelist_columns |
| src/admin/config_store.py | 修改 | save_config 分流 + 扁平→嵌套映射 + read_public_config 合并两文件 |

## 迁移策略

1. 创建 configs/oauth-providers.json（含 gitcode/github 协议参数 + 凭据）
2. 从 global-bot-token.json 删除 oauth_providers 字段
3. providers.py 加 _DEFAULT_PROVIDERS（不含凭据）+ _loaded 标志位
4. user/db.py 新增 migrate_oauth_providers 统一入口，migrate_accounts_from_json 瘦身
5. db_setup.py 删除 OAuth 迁移 + redirect_uri 迁移，保留 default 账户清理
6. _load_providers：meta 有 oauth_providers_config → 从 DB 读取；无 → 用代码内置默认
7. admin/config_store.py save_config 分流 + 扁平→嵌套映射

**部署顺序（关键）：**
- 先合并代码（含 _DEFAULT_PROVIDERS fallback + migrate_oauth_providers 向前兼容）→ 旧环境无 oauth-providers.json 也能正常运行
- 再创建 oauth-providers.json → 重启后 meta 表同步
- global-bot-token.json 删除 oauth_providers 字段 → 不影响功能（协议参数已从 oauth-providers.json 读取）

## v3 → v4 优化对照

| v3 问题 | v4 优化 |
|---------|---------|
| migrate 三处重复 | migrate_oauth_providers 统一入口，user/db.py 瘦身，db_setup.py 删除 OAuth 迁移 |
| db_setup.py 还迁移 redirect_uri | 删除 redirect_uri 迁移行（D27 已废弃） |
| global-bot-token.json 被加了 oauth_providers | 删除 oauth_providers 字段，回归原始状态 |
| admin save_config 缺映射逻辑 | _OAUTH_FIELD_MAP 扁平→嵌套映射 + read_public_config 合并两文件 |
| _PROVIDERS_CACHE lazy loading 竞态 | _loaded 标志位替代 `if not _PROVIDERS_CACHE` |
| link_user_provider col_map 硬编码 | 标记迁移期已知限制，迁移结束后删除 |

## 与需求对齐

需求 4：
- "标准 OAuth 2.0 平台通过配置文件接入，不改代码" → 加 JSON 条目即可
- "平台协议配置和平台凭据分离存储" → 协议参数覆盖同步到 meta，凭据幂等同步到 meta
- "旧配置文件和旧数据无需调整" → fallback 读 global-bot-token.json 旧字段
- "前端登录页根据已配置的平台列表自动展示" → providers API 从 DB 读取
- "新增平台只需在配置文件加平台条目 + 可选配置凭据" → oauth-providers.json 加条目

需求 6：
- "账号登录配置与 Bot 运行时配置各自独立管理" → 独立文件物理分离
- "各自独立存放、独立变更" → oauth-providers.json 和 global-bot-token.json 各自独立
- "一方配置的修改或异常不影响另一方的正常运行" → 文件分离，JSON 解析互不依赖
- "已有混合配置无需手动拆分" → migrate_oauth_providers fallback 读 global-bot-token.json
- "所有平台相关逻辑通过配置驱动，不硬编码平台名" → 消除 5 个文件 if-else
- "代码保留内置默认配置确保向前兼容" → _DEFAULT_PROVIDERS 兜底
