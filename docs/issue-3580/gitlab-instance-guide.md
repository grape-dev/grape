# GitLab 实例接入手册（运维/实施口径）

适用：arch-weekly-11 线（含 `feat/arch-weekly-11-yaw-dev`）。GitLab.com 与自建实例同一套口径，
差异只在平台卡字段（`api_base` / `ca_bundle` / 多实例新增卡）。

> 与 PR #1854 的差异：本系统不提供 `/api/gitlab/*` 独立路径族，统一走
> `/api/repos/{provider}/*`（provider = 平台卡键，默认 `gitlab`）；事件入库不建
> `grape_t_gitlab_webhook_inbox`，统一落通用 `grape_t_webhook_inbox`。

---

## 1. 前置条件

| 项 | 要求 |
|---|---|
| GitLab 版本 | v4 API（GitLab 9.0+；托管依赖 `/projects/{id}/hooks`、`/projects/{path}/members/all/{uid}`） |
| 网络 | grape 的 event 进程必须可被 GitLab 回调**访问到**（见 §4：GitLab 拒绝 localhost/内网 webhook URL） |
| PAT scope | 托管 + 写操作：`api`；仅读代码：`read_repository`。PAT 以 `PRIVATE-TOKEN` 头传递 |
| 账号权限 | 托管建 hook 需 **Maintainer(40) 及以上**；仅克隆需 Reporter(20)+ |

PAT 创建入口：`https://<host>/-/user_settings/personal_access_tokens`
（卡字段 `pat_create_url`，管理面回显给用户）。

---

## 2. 平台卡（默认 gitlab.com，无需配置）

卡真源：`configs/platforms/gitlab.json`（每平台一个文件）。
读卡装配链（`card_sources.PlatformCardAssembler`）：
**表/契约整卡 > meta 逐键（`meta.vcs_providers_config`） > 文件卡 > 代码默认**；
管理面覆盖层（`meta.vcs_platform_overrides`）最后叠，只含相对文件卡变化过的键。

GitLab 关键字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `dialect` | `gitlab` | API 方言族 = v4 |
| `api_base` | `https://gitlab.com/api/v4` | 自建实例改成 `https://<host>/api/v4` |
| `api_auth` | `private_token` | `PRIVATE-TOKEN` 请求头 |
| `webhook_path` / `event_header` | `/webhook/gitlab` / `X-Gitlab-Event` | 接收路径与事件头 |
| `webhook_verify` | `token_compare` | `X-Gitlab-Token` 明文比对 |
| `webhook_dialect` | `gitlab_v4` | 托管走 `gitlab_v4` 家族（随机 secret，路径 B） |
| `clone_auth` | `oauth2` | 克隆 `https://oauth2:<token>@<host>/<path>.git` |
| `ca_bundle` | 空 | 自建实例自签 CA 的 PEM **绝对路径** |
| `bot_username` | 空 | GitLab 无内置 bot → 托管跳过 bot 邀请（路径 B） |
| `token_storage_field` / `webhook_token_field` | 空 | 无 managed_repos 物理列，凭据走 `grape_t_repo_credentials` |

### 自建实例 / 多实例

管理面：`GET|PUT /api/admin/vcs-platforms`（`PlatformCardService`）。
PUT 提交整卡，服务只把**与基线卡的差异键**写入 `meta.vcs_platform_overrides`（A2 覆盖层），
不新增表列。

- **改单实例字段**（切自建实例 / 加 `ca_bundle`）：直接覆盖 `api_base`、`ca_bundle`、`webhook_path` 即可。
- **并存多实例**：覆盖层只能叠加到**已存在**的 provider 卡（装配器 `if _p in base`），
  要让第二套实例作为独立 provider（如 `gitlab-corp`）出现，必须给它一个基座——新增卡文件
  `configs/platforms/gitlab-corp.json`，或在平台表/契约里落一行（D109 运行时真源，支持新增 provider）。
  之后 `provider=gitlab-corp` 走同一套 `/api/repos/{provider}/*`。
- `webhook_path` 在全局必须唯一（如 `/webhook/gitlab`、`/webhook/gitlab-corp`）。

---

## 3. 用户绑定 PAT

```
GET    /api/repos/{provider}/connection    # 只回 configured/login/source，永不回 token
PUT    /api/repos/{provider}/connection    # body: {"token": "glpat-...", "login": "可选"}
DELETE /api/repos/{provider}/connection    # 解绑
```

PUT 的实现链：`verify_token(token)`（GitLab → `GET /user`）→ 无 `id` 则 400 `TOKEN_INVALID`
→ `set_credential(user_id, provider, {token, login}, kind="pat")` →
`invalidate_user_repos()` 刷缓存。**不写 `external_identities`**：PAT 是仓库执行凭据，不是登录身份
（R11）。`login` 取 GitLab `/user` 的 `username`（GitLab 没有 `login` 字段）。

### 单 PAT 驱动（推荐形态，与基线一致）

一个 PAT 就能完成「查身份 → 托管 → 克隆/推送 → API 调用」，不需要 OAuth 登录绑定，
也不需要额外的 bot 身份：

| 步骤 | 代码 | GitLab 调用 |
|---|---|---|
| 查身份 | `create_provider("gitlab").verify_token(token)` | `GET {api_base}/user` → `{id, username, name, state, …}` |
| 当前登录名 | `.get_user_login(token)` | 取 `username`（**GitLab `/user` 没有 `login` 字段**） |
| PAT 元数据 | `.token_expiry(token)` | `GET /personal_access_tokens/self`（GitLab 15.4+）→ `{expires_at, scopes, revoked}`；读不到返回 `{}`，不阻断绑定 |
| 托管权限 | `.get_collaborator_permission(token, owner, repo, username)` | `GET /projects/{path}/members/all/{uid}` |
| 克隆/推送 | `clone_auth=oauth2` | `https://oauth2:<token>@<host>/<path>.git`，token 经 `GIT_CONFIG_*` 环境变量注入、不进 argv |

实测（2026-11-22，gitlab.com PAT）：

```
id       = 44646095
username = yangaiwu
name     = aiwu yang          ← 显示名，就在 /user 的 name 字段
expires  = 2027-01-30  revoked = False
scopes   = [... read_repository, write_repository, read_api, api ...]
```

凭据落点：`user_credentials(user_id, provider, kind='pat')`，`config_json={token, login}`；
**不写 `external_identities`**（PAT 是仓库执行凭据，不是登录身份，R11）。也就是说：
用户带上 PAT 就能直接驱动托管与执行，身份信息按 PAT 实时查询，不依赖先走一次 OAuth 登录。

---

## 4. Webhook 可达性（最常见的卡点）

- 托管前必须配好 `webhook_url`（`user_db.get_webhook_url()`）；未配置 → `400`
  `webhook_url_missing`，`step=configure_webhook_url`。
- **GitLab SaaS 会拒绝 localhost / 内网地址**：`POST /projects/{id}/hooks` 返回
  `422 {"message": {"url": ["Hook url is blocked ..."]}}`。托管必须用**对外可达的公网域名/IP**。
- URL 路径按卡派生：`http(s)://<你的域名>/webhook/gitlab`（`webhook_path` 可被卡覆盖）。

---

## 5. 托管仓库

```
POST /api/repos/host?provider=gitlab      body: {"full_name": "owner/repo"}
```

执行链（按代码顺序）：

1. `_host_owner_conflict()` —— 归属前置校验（全平台）：该 `repo_key` 已被**他人账号**托管 →
   `403 REPO_HOSTED_BY_OTHER`；被**其他组织**托管 → `403 REPO_HOSTED_BY_OTHER_ORG`；
   同用户/同组织重复托管 = 幂等重入。
2. `webhook_url` 检查（§4）。
3. 卡/方言判定 → `gitlab_v4`。
4. `_gitlab_host_precheck()` —— 身份与权限：
   `verify_token` 取 `username`；`login == owner` 直接通过，否则
   `get_collaborator_permission()`（`/projects/{path}/members/all/{uid}`）要求
   `access_level >= 40`。非成员 → `403 PERMISSION_DENIED`；身份取不到 → `502 GITLAB_IDENTITY_FAILED`。
5. **占位**：`add_managed_repo(webhook_id=0, host_user_id=<uid>, org_id=...)` +
   `host_status='hosting'` / `host_step='reserve'` —— 并发第二笔由 `repo_key` 唯一键 +
   步骤 1 的归属校验挡住，不会重复建 hook。
6. `find_existing_gitlab_webhook()` → 没有则 `setup_gitlab_webhook(..., webhook_password=随机secret)`
   （`POST /projects/{id}/hooks`，`token` 字段 = 随机 secret，事件位全开：
   `push / tag_push / issues / note / merge_requests / pipeline / wiki_page / releases`；
   secret 必填且禁止复用 API token）。
7. bot 邀请：`bot_username` 为空 → 跳过（GitLab 无 bot 身份）。
8. `add_managed_repo(webhook_id=<真实id>, webhook_secret=<secret>, host_user_id=...)` +
   `mark_user_repo_join()` + `ensure_default_agent_sessions()`。

诊断列：`managed_repos.host_status / host_step / host_error / host_updated_at`。
`host_status` 取值：`hosting`（占位中）/ `partial`（保留了可用部分，如 hook 建成但 bot 缺失）/
`webhook_missing` / `bot_missing`，空=完整；`host_step` 记失败步（如 `reserve` / `create_webhook` /
`host_setup` / `precheck_permission`）。`/api/repos/managed` 可回读。

---

## 6. 事件流

```
GitLab → POST https://<域名>/webhook/gitlab   （X-Gitlab-Event / X-Gitlab-Token）
  └─ webhook.py: @router.post("/webhook/{provider}")
       ├─ 未知 provider → 404；载荷超限 → 413；未托管仓库 → 404（防幽灵 webhook）
       ├─ 期望 token 走 GSB mgmt.get_repo_webhook_config()（分域库/四进程安全，不直查本地库）
       ├─ 验签按卡分派（`token_compare`）：缺 `X-Gitlab-Token` → 401；密钥不匹配 → 403
       ├─ delivery 幂等 → grape_t_webhook_inbox
       │     UNIQUE(repo_key, delivery_id)；status/lease_expires_at/attempts/max_attempts
       └─ claim_next(owner, lease) 消费；失败重试至 max_attempts，purge_finished 清理
```

自建实例自签证书的 API 调用：`kernel/upstream_http.open_with_policy()` 按 `api_base`
前缀匹配卡，把 `ca_bundle` 注入 SSL 上下文；git 传输侧由 repogit 注入
`http.sslCAInfo`，并强制 `http.followRedirects=false`，token 经 `GIT_CONFIG_*` 环境变量下发，
**不进命令行 argv**。

---

## 7. 错误码对照

| HTTP/错误码 | 触发 | 处置 |
|---|---|---|
| `400 TOKEN_INVALID` | PAT 无效 / `/user` 无 id | 重新建 PAT（scope `api`），检查是否过期 |
| `400 webhook_url_missing` | 未配 webhook_url | 设置 `webhook_url` 或用公网域名访问 Web UI 触发自动探测 |
| `403 PERMISSION_DENIED` | 非成员 / 权限 < Maintainer | 加为 Maintainer，或换 owner 的 PAT |
| `403 REPO_HOSTED_BY_OTHER` | 已由其他账号托管 | 先由原账号解除托管（`/api/repos/remove`） |
| `403 REPO_HOSTED_BY_OTHER_ORG` | 跨组织托管 | 在原组织解除后再托管 |
| `502 GITLAB_IDENTITY_FAILED` | `/user` 调用失败 / 无 username | 查 PAT、查 `api_base` 是否指向正确实例 |
| `502 GITLAB_PERMISSION_CHECK_FAILED` | `/members/all` 读取失败 | 查网络/CA/权限 |
| `422 Hook url is blocked` | webhook URL 是 localhost/内网 | 换公网可达地址（§4） |
| `404 repository not managed`（回调侧） | 事件来自未托管仓库 | 先托管；或该仓库已解除托管 |

---

## 8. 验证清单（上线前逐条过）

1. `GET /api/repos/gitlab/connection` → `configured: true`，`login` 正确。
2. `GET /api/repos/create-targets?provider=gitlab` → 返回可建仓空间（命名空间）。
3. `POST /api/repos/host` → 返回 `webhook_id` > 0；GitLab 侧 `GET /projects/{id}/hooks` 能看到
   `http(s)://<域名>/webhook/gitlab`。
4. 在 GitLab 上推一次代码 / 开一个 MR → `grape_t_webhook_inbox` 出现新行且被消费
   （`status='consumed'`）。
5. 用非成员账号复测 → 应得 `403 PERMISSION_DENIED`（校验生效）。
6. 自建实例：确认 `ca_bundle` 生效（否则表现为 `502` 与 SSL 校验错误）。