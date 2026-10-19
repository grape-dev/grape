# 2. GitCode 集成

GitCode 集成 = **登录（OAuth）** + **Bot 写操作（建仓/发评论）** + **事件接入（webhook）** 三件事，配齐后 Grape 才能监听 GitCode 仓库并回写操作。

## 2.1 OAuth 应用（用户登录）

**在哪配**：`configs/oauth-providers.json`（复制 `configs/oauth-providers.example.json`）。`gitcode.client_id` / `client_secret` / `redirect_uri` 在首次启动时自动迁入 DB meta，之后以 DB 为权威（Admin 控制台可改）。

**配置示例**：

```json
{
  "gitcode": {
    "client_id": "<your-gitcode-client-id>",
    "client_secret": "<your-gitcode-client-secret>",
    "redirect_uri": "https://<your-grape-domain>/api/auth/callback/gitcode",
    "scope": "all"
  }
}
```

> 💡 **提示**
>
> `redirect_uri` 固定格式为 `https://<你的 Grape 域名>/api/auth/callback/gitcode`，必须与 GitCode OAuth 应用中登记的回调地址**完全一致**（含协议与路径）。

**如何验证**：访问 Grape 首页 → 点击「使用 GitCode 登录」→ 跳转 GitCode 授权页并能回跳登录成功，即 OAuth 三字段正确。

## 2.2 Bot Token（平台写操作）

Grape 以 Bot 身份在 GitCode 上执行写操作（创建/关闭/评论 Issue、建仓等）。

**在哪配**：`configs/global-bot-token.json` 的 `gitcode_api_token`（首次启动迁入 DB meta）；之后在 **Admin 控制台 → 全局配置**（`PUT /api/admin/config`）修改，DB 为运行时权威。

**配置示例**：

```json
{
  "gitcode_api_token": "<your-gitcode-bot-pat>",
  "bot_usernames": ["<your-bot-username>"]
}
```

- `gitcode_api_token`：GitCode Bot 的个人访问令牌（PAT），需要仓库/Issue 写权限。
- `bot_usernames`：Bot 用户名列表（用于识别「自己发的评论」，避免自回复循环）；留空可由 `/api/v5/user` 自动获取。

**如何验证**：Admin 控制台全局配置页保存后，让任意 Agent 在托管仓库创建一条 Issue 评论——评论出现在 GitCode 且作者是 Bot 账号，即 Token 有效。

## 2.3 Webhook（事件接入）

**在哪配**：`configs/global-bot-token.json` / Admin 控制台的 `webhook_url` + `gitcode_webhook_token`（同样以 DB meta 为运行时权威）。

**配置示例**：

```json
{
  "webhook_url": "https://<your-grape-domain>/webhook/gitcode",
  "gitcode_webhook_token": "<your-webhook-secret>"
}
```

- `webhook_url`：Grape 服务端接收 GitCode 事件的路径，固定为 `https://<你的域名>/webhook/gitcode`（`POST /webhook/gitcode`）。
- `gitcode_webhook_token`：webhook 签名密钥（也接受字段名 `webhook_secret`），用于校验请求确实来自 GitCode。

**如何验证**（托管仓库 → 测试 webhook → 看事件进来）：

1. 在 GitCode 仓库 **设置 → Webhooks** 新增 webhook，URL 填上面的 `webhook_url`，Secret 填 `gitcode_webhook_token`。
2. 在 GitCode webhook 详情页点「测试」→ GitCode 显示 200 OK。
3. 在该仓库创建/评论一条 Issue → Grape 控制台「事件」页出现对应事件、流水线启动，即链路全通。

> ⚠️ **注意**
>
> 若测试 webhook 返回 403/校验失败，优先核对 GitCode 端 Secret 与 Admin 控制台中的 `gitcode_webhook_token` 是否一致（两端必须完全相同）。
