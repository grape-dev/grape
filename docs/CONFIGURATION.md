# Platform configuration reference

Four groups of configuration: **execution engine and model** (a runtime prerequisite), **public base URL**, **OAuth app credentials** and **bot credentials** (the last three are platform-facing, shared by humans and agents).

## Execution engine and model

The engine CLI executes tasks on the **machine running Grape**; if your installing agent is itself a supported engine on that same machine, this is usually already satisfied.

Models are configured **on the engine side** (e.g. `opencode.json`, `codearts_cli.json`, `config.yaml`, or the engine's own login); Grape does not manage them.

```bash
npm install -g opencode       # Node.js based (reference engine)
# or
pip install codearts-cli      # Python based
```

## Platform-facing configuration (shared by humans and agents)

Skippable for a local trial. Hosting a repository from the WebUI, signing in with a third-party account, and letting agents host repositories or connect Feishu all rely on the same three files; all are gitignored.

**1. Public base URL** — `configs/server-config.json` (skippable for local use)

```bash
cp configs/server-config.example.json configs/server-config.json
```

```jsonc
{
  "server_base_url": "https://<your-domain>",
  "webhook_url": "https://<your-domain>/webhook/gitcode"
}
```

This one is mandatory in production; hosting repositories and third-party sign-in need it earlier. Skip it for a single-machine trial: the service falls back to `http://localhost:3000`, and since you browse from that same machine there is no callback problem at all. As soon as others need to reach it, or OAuth login is enabled, it becomes mandatory — otherwise OAuth callbacks point at the local machine, GitCode/GitHub cannot reach back, and login and hosting fail. Startup logs and `GET /api/health` surface an explicit `server_base_url_configured=false` warning.

**2. OAuth app credentials (optional)** — `configs/oauth-providers.json`

```bash
cp configs/oauth-providers.example.json configs/oauth-providers.json
```

```jsonc
{
  "gitcode": {
    "client_id": "<your GitCode OAuth App Client ID>",
    "client_secret": "<your GitCode OAuth App Client Secret>",
    "redirect_uri": "https://<your-domain>/api/auth/callback/gitcode",
    "auth_url": "https://gitcode.com/oauth/authorize",
    "token_url": "https://gitcode.com/oauth/token",
    "user_url": "https://gitcode.com/api/v5/user",
    "scope": "all"
  },
  "github": {
    "client_id": "<your GitHub OAuth App Client ID>",
    "client_secret": "<your GitHub OAuth App Client Secret>",
    "redirect_uri": "https://<your-domain>/api/auth/callback/github",
    "auth_url": "https://github.com/login/oauth/authorize",
    "token_url": "https://github.com/login/oauth/access_token",
    "user_url": "https://api.github.com/user",
    "scope": "repo user admin:repo_hook offline_access"
  }
}
```

This file is only a **seed**: it is imported into the database idempotently on startup, and everything is read from the DB at runtime — **the DB is authoritative** — so change these later in the admin console (Config Center → Platform Credentials); editing the file will not overwrite existing values. The `redirect_uri` must exactly match the callback URL registered in the OAuth App. Credentials in the wrong file, or with empty fields, make `/api/auth/login/{provider}` return 400 `not_configured`. (Legacy OAuth fields in `configs/global-bot-token.json` are still read for backward compatibility; use `oauth-providers.json` for new setups.)

**What if you skip it?** You only lose "sign in with a GitCode / GitHub account" and repo hosting; local accounts, scenes, skills and the pipeline all work normally. And when you do want OAuth, you can fill client_id / client_secret in the admin console (`http://<host>/admin`) after startup instead of coming back to this file.

**3. Bot credentials (only for GitCode / Feishu integration)** — `configs/global-bot-token.json`

```bash
cp configs/global-bot-token.example.json configs/global-bot-token.json
# then fill in the GitCode / Feishu bot credentials
```

