# Grape 登录系统全面体检报告

> 日期：2026-10-09
> 范围：登录链路 + 用户管理 + 用户数据表 + GitCode/GitHub 登录差异
> 相关文件：`src/api/routes/auth.py`、`src/api/deps.py`、`src/api/jwt_utils.py`、`src/user/db.py`、`src/api/db_setup.py`、`src/session_manager.py`、`src/admin/routes.py`

## 一、登录体系总览（3+2 认证通道）

| 通道 | 端点 | 凭据载体 | 适用者 |
|---|---|---|---|
| GitCode OAuth | `/api/auth/login/gitcode` + callback | access(15min)/refresh(7d滚动,上限30d) | 普通用户 |
| GitHub OAuth | `/api/auth/login/github` + callback | 同上，带 `login_provider=github` | 普通用户 |
| 密码登录 | `POST /api/auth/login` | 同上，`login_provider` 为空 | 普通用户（近乎废弃，无改密/注册入口） |
| Admin 密码 | `POST /api/admin/login` | 同上 + `role=admin` | 管理后台 |
| API Key / 会话 token | 无登录页 | `grape_`（api_tokens 表）/ `tk_`（session_api_tokens 表） | 程序化访问/Agent |

双 token 机制构架合理：短期 access 存 localStorage + 长效 refresh 存 HttpOnly cookie（`samesite=strict`，JS 不可读，防 XSS），`token_version` 支持改密/登出全局撤销，refresh 含 30 天绝对上限 + CSRF header 校验。这套骨架是健康的。

## 二、用户数据表（`data/sessions.db`）

- **`user_accounts`**（`src/user/db.py:11`）：主用户表。username/gitcode_id/github_id 关联平台账号，`gitcode_api_token`/`github_api_token` 明文存 VCS token，`token_version`=JWT 撤销版本，`last_active_at`=在线状态。OAuth 用户名冲突时自动加 `gc_`/`gh_` 前缀。
- **`user_credentials`**（`db.py:315`）：`config_json` 存 OAuth access/refresh_token + fetched_at（长住凭据）。
- **`admin_accounts`**（`db_setup.py:762`）：独立于用户表；默认种子 `admin/admin123`，靠 `admin_pwd_initial` meta 提示改密。
- **`oauth_states`**：单次消费、10 分钟过期的 CSRF state（DB 共享，集群可用）。
- **`login_whitelist`**：`gitcode_username`(UNIQUE) + 后加的 `github_username` 列；DB 为空时降级读 `configs/login-whitelist.json`。
- **`unified_user_mapping`**（`session_manager.py:26`）：VCS 用户名↔飞书 open_id，供 IM 通知解析 assignee。
- **`api_tokens` / `session_api_tokens`**：长期 Key（存 SHA-256）/ 会话级 token。

## 三、用户管理现状

- **三套独立账号体系**：`user_accounts`（普通用户，OAuth/密码）、`admin_accounts`（管理后台，独立密码）、`api_tokens`/`session_api_tokens`（程序化访问）。
- **账号生命周期**：OAuth 登录自动建号（无注册流程）；用户名冲突自动加 `gc_`/`gh_` 前缀隔离跨平台同名账号；`token_version` 递增即全局登出。
- **白名单管控**：管理员在管理后台维护 `login_whitelist`，踢出后仅 **登录时** 拒绝，存量会话不受影响（见高危 #1）。
- **活跃状态**：`deps.mark_user_active` 内存缓冲 → 定时批量写 `last_active_at`（O(1) 非阻塞）。
- **缺口**：普通用户无「修改密码」端点（`update_password` 存在但未挂路由）；密码登录用户不受白名单管控；无 2FA。

## 四、GitCode vs GitHub 登录差异

| 维度 | GitCode（`auth.py:107`） | GitHub（`auth.py:257`） |
|---|---|---|
| scope | `all`（过宽） | `repo user admin:repo_hook offline_access` |
| 网络 | `urllib` 直连，**无代理支持** | `requests`，**支持 HTTPS_PROXY 代理**（国内服务器关键） |
| 失败提示 | 直接 raise JSON 400/502（前端 toast 无感知跳转） | 重定向 `/login?oauth_error=中文消息`（可解释超时/白名单） |
| 用户映射 | `update_unified_user_info(login, open_id="", gitcode_nickname)` | `update_unified_user_info(login)`（不传昵称） |
| 持久化 | 常规写 | 额外 `wal_checkpoint(TRUNCATE)` |
| provider 抉择 | `login_provider=gitcode` 优先取 GitCode token | `login_provider=github` 优先取 GitHub token |

`_get_user_provider_and_token`（`repos.py:32`）：JWT 携带 `login_provider` → 优先返回对应平台 token；密码登录（空）→ github 优先。refresh 时 `login_provider` 从旧 token 继承（`jwt_utils.py:92-100`），滚动续期不丢失。

## 五、问题清单（按严重度）

### 🔴 高危

1. **被移出白名单 ≠ 立刻踢下线**：白名单只在 OAuth **登录时**校验（`auth.py:192/352`），而 `/auth/refresh`（`auth.py:65`）**不查白名单**。被踢用户凭有效 refresh_token 可持续刷新，最长 **30 天**继续用系统。
2. **密码哈希 = 无盐 SHA256**：`_hash_password`（`db.py:43`）user 与 admin 共用，可彩虹表/爆破。数据库泄漏 = 全量密码可解。
3. **`user_accounts.password` 明文列冗余**：`create_user`/`update_password`（`db.py:53,108`）把明文密码同时写进 `password` 列，`get_all_user_accounts`/`get_user_account` 整表读取即暴露明文。
4. **登录无速率限制**：`/api/auth/login` 与 `/api/admin/login`（默认密码 `admin123`）均可无限爆破，无失败计数/锁定/验证码。

### 🟠 中危

5. **OAuth 凭据明文落库**：`user_credentials.config_json` + `gitcode_api_token` 明文存储 VCS access/refresh_token；DB 备份即凭据泄露。
6. **dashboard 安全悖论**：`managed_repos` 无 user 维度 → `check_repo_access`（`deps.py:255`）靠「仓库在 managed_repos 且用户在某列表」判断，跨平台 `gitcode:`↔`github:` 前缀自动互认，同仓库不同托管来源可能越权，属登录外的一环。
7. **GitCode 回调无友好错误页**：与 GitHub 的 `_oauth_error_redirect` 体验不一致，超时/网络故障时用户看到生硬 JSON。

### 🟡 低危 / 一致性

8. **redirect_uri 推断硬编码 `:80`**（`auth.py:118,269`）：非 80 端口部署且未配 meta/env 时回调地址错误。
9. **`user_accounts.github_login` 不随 GitHub OAuth 更新**：仅 `settings.py:129` 手配 token 时写入，profile 展示可能为空或过期。
10. **密码登录用户不受白名单管控**：白名单按平台 login 判断，本地密码用户（含历史遗留）绕过白名单策略，且无关闭密码登录的开关。
11. **GitHub callback `gitcode_login` 映射语义不对称**、admin 与用户共用 `refresh_token` cookie 名（path 隔离尚可）等小项。

## 六、优化建议（按优先级）

1. **`/auth/refresh` 增加白名单校验**（复用 `is_gitcode_user_allowed`/`is_github_user_allowed`），踢人即刻生效——改动最小、收益最大。
2. **密码哈希升级** pbkdf2_hmac（标准库，无新依赖），存量哈希保持兼容逐次升级；**停止读写明文 `password` 列**。
3. **登录限流**：内存/DB 计数（IP+username 5 次/分钟锁定），Admin 登录必加。
4. 可选：`user_credentials` 凭据加对称加密；GitCode 引入代理支持 + `_oauth_error_redirect`；补齐 `github_login` 回写；登录页按 OAuth 配置状态动态显隐按钮。