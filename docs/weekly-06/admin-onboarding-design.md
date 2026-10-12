# 首次部署引导 + Admin 配置中心设计方案

> 状态：待评审（v1）
> 生成日期：2026-09-29
> 分支：weekly-06-lyz
> 关联问题：首次部署未配置 `configs/global-bot-token.json` 时系统不可登录、不可用（OAuth 依赖 meta 表配置，而配置又需登录）

---

## 1. 背景与问题

当前系统登录有两条路：
- 密码登录 `/api/auth/login`：依赖 `user_accounts` 表中的账号，**首次部署表为空**
- GitCode / GitHub OAuth：依赖 meta 表 `oauth_gitcode_client_id` 等（由 `global-bot-token.json` 首启加载），**未配置即不可用**

结论：首次部署若未预先写好 `configs/global-bot-token.json`，系统完全不可登录，形成"鸡生蛋"死锁。

---

## 2. 方案目标

1. **系统内置默认 admin 账号**，首次部署开箱即可登录
2. admin 登录后**引导修改默认密码**（非强制，可跳过）
3. 提供 **`/admin` 配置中心**，在线配置 GitCode/GitHub 的 bot token、OAuth client_id/secret、bot_usernames 等
4. admin 认证走 **JWT**（与主系统融合，无独立 session）
5. admin 配置**落盘持久化**到 `configs/global-bot-token.json`（重启仍生效）+ 同步 meta 表

---

## 3. 已确认决策

| # | 决策 | 说明 |
|---|------|------|
| 1 | 新增 `admin_accounts` 表 | 与 `user_accounts` 独立，承载 admin 账号/密码/角色 |
| 2 | 配置落盘 `global-bot-token.json` | 重启持久，与现有首启加载机制一致 |
| 3 | admin 认证走 JWT | 复用 `jwt_utils`，与主系统 token 体系一致，无独立 session |

---

## 4. 设计

### 4.1 数据库：新增 `admin_accounts` 表

```sql
CREATE TABLE IF NOT EXISTS admin_accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    is_super      INTEGER NOT NULL DEFAULT 1,      -- 预留角色：1=超管
    token_version INTEGER NOT NULL DEFAULT 0,       -- 改密后 +1，旧 JWT 失效
    created_at    TEXT DEFAULT (datetime('now')),
    last_login_at TEXT DEFAULT NULL
);
```

- 与 `user_accounts` **完全独立**，互不影响（用户侧 OAuth/密码登录保持现状）
- 默认种子：`admin / admin123`（见 4.2）

### 4.2 首次初始化与默认账号

- `db_setup.py` 启动阶段：
  - 建 `admin_accounts` 表
  - 若表为空 → 插入默认账号 `admin`，密码 `admin123`（sha256）
  - meta 表写入标记：`admin_initialized = 1`、`admin_pwd_initial = 1`（标识仍为默认密码）
- 密码来源优先级：环境变量 `GRAPE_ADMIN_PASSWORD` > 默认 `admin123`
- README / 部署文档明示默认凭据与改密引导

### 4.3 admin 认证（JWT）

复用 `src/api/jwt_utils.py`（access+refresh 双 token），但 payload 增加 `role: "admin"`：

- `POST /api/admin/login` → 校验 `admin_accounts` → 签发 admin JWT（`role=admin`）
- `POST /api/admin/refresh` → 刷新 admin access_token（cookie 沿用 refresh_token，path 用 `/api/admin`）
- `POST /api/admin/logout` → token_version+1
- `POST /api/admin/change-password` → 校验旧密码 → 更新 hash + token_version+1 → 置 meta `admin_pwd_initial=0`
- 鉴权依赖：`require_admin`（校验 JWT 且 `role==admin`）

### 4.4 `/admin` 前端

#### 4.4.0 入口关系（重要）

`/admin` 与主 SPA 是**两个独立页面**，登录逻辑与面向用户不同：

```
https://<host>/
        │
        ├── 用户登录（主 SPA）
        │     ├─ 使用 GitCode 登录（OAuth）
        │     ├─ 使用 GitHub 登录（OAuth）
        │     └─ 密码登录（隐藏入口，togglePwdLogin 展开）
        └── 登录成功 → 用户工作台（仓库/需求/流水线/Chat…）

https://<host>/admin
        │
        └── 管理员登录（admin 账号密码，独立 admin.html）
              └── 登录成功 → 配置中心（GitCode/GitHub token + OAuth + bot_usernames）
```

要点：
- **用户登录不经过 admin**；admin 只负责配置平台凭据，不承载用户登录
- 首次部署 OAuth 未配置时，**主 SPA 用户登录不可用**（gitcode/github 按钮报"OAuth未配置，请在管理界面配置"）；此时管理员通过 `/admin` 先配置凭据，用户才能正常登录
- **入口发现**：主 SPA 登录页底部增加"管理后台"链接（`/admin`），便于管理员首登
  - **呈现效果**：在登录卡片底部、OAuth 按钮下方新增一行低调的次级链接，与现有"申请内测"同级（灰色小字、hover 变 accent 色，中间用 `·` 分隔），点击新开/跳转 `/admin`
  - 示意：
    ```
    ┌──────────────────────────────────┐
    │        🍇 Grape                  │
    │     Multi-Agent DevOps           │
    │   [ 使用 GitCode 登录 ]          │
    │   [ 使用 GitHub 登录 ]          │
    │                                  │
    │   申请内测 · 管理后台  ←新增      │   ← 灰色小字链接，hover 高亮
    └──────────────────────────────────┘
    ```
  - 注意：该链接**总是显示**（不区分是否已初始化），未初始化时点击进入 `/admin` 即管理登录页；管理员首次部署走此入口
- admin 页面为独立 admin.html，**不混入**用户工作台 SPA；`/admin` 需 require_admin 鉴权

- **URL**：`https://localhost:3000/admin`（本地直连）/ `https://<域名>/admin`（生产经 nginx 反代到 3000）
  - nginx 无需新 location（`/` 通配已覆盖 `/admin`，见 4.6）
- 独立单页：`src/api/static/admin.html` + `src/api/static/js/admin.js`（不做进主 SPA，避免混入用户工作台）
- 页面区块：
  1. **登录**：admin 用户名/密码 → JWT
  2. **修改密码**：默认密码提示 + 改密/跳过
  3. **GitCode 配置**：`gitcode_api_token` / `gitcode_oauth_client_id` / `gitcode_oauth_client_secret` / `gitcode_oauth_redirect_uri`
  4. **GitHub 配置**：`github_bot_token` / `github_oauth_client_id` / `github_oauth_client_secret` / `github_oauth_redirect_uri`
  5. **Bot 账户**：`bot_usernames`（可手动填，也可"用 token 查询"自动获取）
  6. **保存**：写 `configs/global-bot-token.json` + 同步 meta 表，并提示服务已生效/需重启项

#### 4.4.1 页面线框图（v1，ASCII 原型）

**① login 视图**（未登录时展示）

```
┌───────────────────────────────────────────────────────────────┐
│                    ┌─────────┐                                │
│                    │  🍇 红柚 │   ← grape 品牌图标             │
│                    └─────────┘                                │
│                     Grape Admin                                │
│              首次登录请使用默认账号并修改密码                     │
│                                                               │
│      ┌─────────────────────────────────────┐                  │
│      │  管理员账号   [ admin            ﮸ ] │                  │
│      └─────────────────────────────────────┘                  │
│      ┌─────────────────────────────────────┐                  │
│      │  登录密码     [ •••••••           ﮸ ] │                  │
│      └─────────────────────────────────────┘                  │
│                                                               │
│      [        登  录        ]                                  │
│                                                               │
│      ⚠ 首次部署默认账号 admin / admin123，登录后将引导修改        │
└───────────────────────────────────────────────────────────────┘
```

**② 改密引导**（`admin_pwd_initial=1` 时，登录后弹出）

```
┌─────────────────────────────────────────────────────────┐
│  首次登录 · 设置管理员密码                         ✕    │
│  ───────────────────────────────────────────────────   │
│  您正在使用默认密码，为保障安全建议立即修改。               │
│  （跳过后将每次登录提示，直到修改完成）                     │
│                                                         │
│  新密码       [ ••••••••••                    ]         │
│  确认新密码   [ ••••••••••                    ]         │
│                                                         │
│  ┌──────────────────┐   ┌────────────────┐             │
│  │  🚫 跳过(不安全)  │   │  ✅ 确认修改    │             │
│  └──────────────────┘   └────────────────┘             │
└─────────────────────────────────────────────────────────┘
```

**③ 配置中心**（登录后主视图，左侧导航 + 右侧表单）

```
┌──────────┬────────────────────────────────────────────────────────────┐
│ ADMIN    │  ⚙ 平台凭据配置                                              │
│──────────│                                                            │
│ ▶ 凭据配置│  ┌─ GitCode ───────────────────────────────────────────┐  │
│   概览    │  │ gitcode_api_token   [ •••••••••••••• ]  ✓已配置  验   │  │
│   GitCode│  │ oauth_client_id     [ glc_xxxxxx      ]           │  │
│   GitHub │  │ oauth_client_secret [ •••••••••••••• ]  ﮸         │  │
│   Bot账户 │  │ oauth_redirect_uri  [ (留空自动推断)  ]           │  │
│ ▶ 账号    │  └───────────────────────────────────────────────────┘  │
│   改密    │  ┌─ GitHub ───────────────────────────────────────────┐  │
│   登出    │  │ github_bot_token    [ •••••••••••••• ]  ✗未配置  验   │  │
│          │  │ oauth_client_id     [ Iv1.xxxxxxxx   ]           │  │
│          │  │ oauth_client_secret [ •••••••••••••• ]  ﮸         │  │
│          │  │ oauth_redirect_uri  [ (留空自动推断)  ]           │  │
│          │  └───────────────────────────────────────────────────┘  │
│          │  ┌─ Bot 账户 ──────────────────────────────────────────┐  │
│          │  │ bot_usernames       [ grape_bot, ...  ]  用token查询 │  │
│          │  └───────────────────────────────────────────────────┘  │
│          │                                        [ 保存配置 ]       │
│          │  ⓘ 保存后写入 configs/global-bot-token.json 并同步 meta   │
└──────────┴────────────────────────────────────────────────────────────┘
```

**④ 交互说明**
- 「验」按钮 → `POST /api/admin/config/verify-token`，调 `/api/v5/user`（GitHub `/user`）校验并回填 `bot_usernames`
- 脱敏：`GET /api/admin/config` 对 token/secret 只回显"●已配置/○未配置"+ 尾 4 位；编辑提交时才发全量
- 「改密」入口复用 ② 的交互；改密成功 → `token_version+1`，旧 admin JWT 失效，需重新登录

### 4.5 后端接口

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| POST | `/api/admin/login` | admin 登录（JWT） | 无 |
| POST | `/api/admin/refresh` | 刷新 admin token | refresh cookie |
| POST | `/api/admin/logout` | 登出（token_version+1） | admin |
| POST | `/api/admin/change-password` | 改密（+token_version） | admin |
| GET | `/api/admin/config` | 读取当前配置（脱敏：token 只显示是否存在/尾4位） | admin |
| PUT | `/api/admin/config` | 保存配置到 global-bot-token.json + meta | admin |
| POST | `/api/admin/config/verify-token` | 用填入 token 调 `/api/v5/user`（或 GitHub `/user`）校验并回填用户名 | admin |

新增模块：`src/admin/`（已有空 `__init__.py`）
- `src/admin/db.py`：admin_accounts CRUD + 默认种子
- `src/admin/routes.py`：APIRouter，挂载 `/api/admin`
- `src/admin/config_store.py`：读写 `configs/global-bot-token.json`（复用/重构成独立工具，供 db_setup 与 routes 共用）

### 4.6 nginx 反代（生产）

**无需额外 location**：`nginx-grape.conf` 的 `location /` 已通配反代到 3000，`/admin`（FastAPI 返回 admin.html）与 `/api/admin/*` 均自动落入该反代。

> 已实施：`nginx-grape.conf` 的 `location /` 通配已覆盖 `/admin`；保留原外网 80/443 → 内网 3000 不变即可。

### 4.7 安全要点

- `require_admin` 必须同时校验 JWT 有效 + `role == admin` + token_version 一致
- `/api/admin/*` 端点一律要求 admin JWT，**不得**被普通用户 JWT 访问
- 配置读取接口需脱敏（token/secret 只回显存在性与尾 4 位，回填全量仅在编辑提交时）
- 默认密码 `admin123` 必须首登引导修改；`admin_pwd_initial=1` 时前端醒目提示
- `change-password` 后 `token_version+1`，旧 admin JWT 全部失效

---

## 5. 前端登录引导流

```
首次 GET /login.html（或主 index 的 loginView）
   └─> 响应中带 admin_pwd_initial / admin_initialized 状态
        ├─ 普通用户 OAuth 登录（主 SPA）——不变
        └─ 未初始化 or admin 已登录（/admin）
              └─ admin.html：
                 登录 → 改密引导（可跳过）→ 配置中心
```

- 主 SPA 登录页增加"管理后台"入口链接（`/admin`），供管理员进入
- 不改动现有用户登录逻辑，零回归

---

## 6. 影响面与风险

| 项 | 影响 | 风险/说明 |
|----|------|---------|
| 新增表 | 低 | 幂等建表，存量库自动加 |
| 默认 admin | 低 | 仅首次种子；密码引导改密 |
| /api/admin 端点 | 低 | 独立路由 + require_admin，不影响用户接口 |
| global-bot-token.json 写入 | 中 | 需保证独占写 + 权限（原文件 gitignore，生产可写） |
| JWT 复用 | 低 | 加 role 字段，向下兼容 |
| nginx | 低 | `/` 通配已覆盖 `/admin`，无需新增 location |

**注意事项**
- `configs/global-bot-token.json` 写入要与 `db_setup` 首启加载保持**同一数据源**（先写文件 → 再 sync meta），避免两边不一致
- admin 配置保存后部分项（如 OAuth、bot token）需提示"是否重启服务生效"（取决于运行时常量缓存位置；meta 表动态读则即时生效）

---

## 7. 后续迭代（不在本方案内）

- 多管理员 / 角色权限细分（is_super 预留）
- admin 操作审计日志
- 首次初始化向导（admin.html 未登录时内联引导，而非单独 setup 页，本方案已覆盖核心闭环）