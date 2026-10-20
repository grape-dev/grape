# 3. GitHub 集成

与 [GitCode 集成](01-gitcode-integration.md) 完全对称，只是凭据换成 GitHub OAuth App + Bot PAT，webhook 走独立路径。

## 3.1 OAuth App（用户登录）

**在哪配**：`configs/oauth-providers.json` 的 `github` 段（复制 `configs/oauth-providers.example.json`）。首次启动迁入 DB meta，之后以 DB 为权威。

**配置示例**：

```json
{
  "github": {
    "client_id": "<your-github-client-id>",
    "client_secret": "<your-github-client-secret>",
    "redirect_uri": "https://<your-grape-domain>/api/auth/callback/github",
    "scope": "repo user admin:repo_hook offline_access"
  }
}
```

- 在 GitHub **Settings → Developer settings → OAuth Apps** 创建应用，`Authorization callback URL` 填 `https://<你的域名>/api/auth/callback/github`（与 `redirect_uri` 完全一致）。
- `scope` 按上例即可覆盖仓库读取、用户信息与 webhook 管理。

**如何验证**：Grape 登录页出现 GitHub 入口 → 点击 → GitHub 授权后回跳并登录成功。

## 3.2 Bot Token 与用户名（写操作）

**在哪配**：`configs/global-bot-token.json`（首次启动迁入 DB meta）；之后 **Admin 控制台 → 全局配置**（`PUT /api/admin/config`）。

**配置示例**：

```json
{
  "github_bot_token": "<your-github-bot-pat>",
  "github_bot_username": "<your-bot-username>"
}
```

- `github_bot_token`：GitHub Bot 账号的 PAT（classic，勾选 `repo` 权限），用于创建/关闭/评论 Issue 等写操作。
- `github_bot_username`：Bot 账号用户名（协作者管理等场景使用）。

**如何验证**：托管一个 GitHub 仓库并触发一次 Issue 处理——Agent 的评论出现在 GitHub 且作者为 Bot 账号，即 Token 有效。

## 3.3 Webhook（事件接入）

**在哪配**：GitHub 仓库 **Settings → Webhooks → Add webhook**；Grape 侧接收路径固定为 `POST /webhook/github`。

**配置示例**：

- **Payload URL**：`https://<your-grape-domain>/webhook/github`
- **Content type**：`application/json`
- **Secret**：与 Grape 侧 webhook 校验密钥一致（GitCode/GitHub 事件共用服务端校验逻辑，GitHub 误发到 `/webhook/gitcode` 也会被直接处理）

**如何验证**：GitHub webhook 详情页显示绿色 ✓（Recent deliveries 有 200 记录）→ 在该仓库创建一条 Issue → Grape「事件」页出现对应事件，即接入成功。

> ⚠️ **注意**
>
> GitHub PAT 与 webhook Secret 均为敏感值：文档与配置示例中一律使用 `<placeholder>` 占位，真实值只存放在服务器配置 / Admin 控制台。
