# 平台配置参考

本页四组配置：**执行引擎与模型**（运行前提）、**对外基址**、**OAuth 应用凭据**、**机器人凭据**（后三组为平台对外配置，Human / Agent 共用）。

## 执行引擎与模型

引擎 CLI 在 **Grape 服务所在机器**上执行任务；若你的安装 Agent 本身是受支持的引擎且与服务同机，通常已满足。

模型在**引擎侧**配置（如 `opencode.json`、`codearts_cli.json`、`config.yaml`，或引擎自带的登录鉴权），Grape 不代为管理。

```bash
npm install -g opencode       # 基于 Node.js（参考引擎）
# 或
pip install codearts-cli      # 基于 Python
```

## 对外配置（Human / Agent 共用）

本地体验可跳过。人用 WebUI 托管仓库、以第三方账号登录，与 Agent 托管仓库、接入飞书，需要的是同一组配置；三项均在 gitignore 中。

**1. 对外基址** —— `configs/server-config.json`（本地体验可跳过）

```bash
cp configs/server-config.example.json configs/server-config.json
```

```jsonc
{
  "server_base_url": "https://<your-domain>",
  "webhook_url": "https://<your-domain>/webhook/gitcode"
}
```

上线到生产时这一项为必配；托管仓库与第三方登录会提前用到它。本地单机体验不用配：服务会回落到 `http://localhost:3000`，而你就是从这台机器访问，没有任何回调问题。一旦要给他人访问或启用 OAuth 登录，就必须配 —— 否则 OAuth 回调指向本机，GitCode/GitHub 无法回访，登录与托管都会失败。启动日志与 `GET /api/health` 会显式给出 `server_base_url_configured=false` 告警。

**2. OAuth 应用凭据（可选）** —— `configs/oauth-providers.json`

```bash
cp configs/oauth-providers.example.json configs/oauth-providers.json
```

```jsonc
{
  "gitcode": {
    "client_id": "<你的 GitCode OAuth App Client ID>",
    "client_secret": "<你的 GitCode OAuth App Client Secret>",
    "redirect_uri": "https://<your-domain>/api/auth/callback/gitcode",
    "auth_url": "https://gitcode.com/oauth/authorize",
    "token_url": "https://gitcode.com/oauth/token",
    "user_url": "https://gitcode.com/api/v5/user",
    "scope": "all"
  },
  "github": {
    "client_id": "<你的 GitHub OAuth App Client ID>",
    "client_secret": "<你的 GitHub OAuth App Client Secret>",
    "redirect_uri": "https://<your-domain>/api/auth/callback/github",
    "auth_url": "https://github.com/login/oauth/authorize",
    "token_url": "https://github.com/login/oauth/access_token",
    "user_url": "https://api.github.com/user",
    "scope": "repo user admin:repo_hook offline_access"
  }
}
```

该文件只是首启时的**种子**：启动时幂等导入数据库，运行时一律从 DB 读取，**DB 才是权威源** —— 之后在管理台「配置中心 → 平台凭据」修改，改文件不会覆盖已有值。`redirect_uri` 必须与 OAuth App 登记的回调地址完全一致；凭据写错或字段留空会让 `/api/auth/login/{provider}` 返回 400 `not_configured`。（`configs/global-bot-token.json` 中的旧 OAuth 字段仍兼容读取，新部署请用 `oauth-providers.json`。）

**不配这一项会怎样？** 只失去"用 GitCode / GitHub 账号登录"和托管仓库的能力，本地账号登录、场景、技能、流水线全都照常可用。真要用 OAuth 时，也可以在启动后进管理台 `http://<主机>/admin` 填 client_id / client_secret，不必回头改这份文件。

**3. 机器人凭据（接入 GitCode / 飞书 时才需要）** —— `configs/global-bot-token.json`

```bash
cp configs/global-bot-token.example.json configs/global-bot-token.json
# 再填入 GitCode / 飞书机器人凭据
```

