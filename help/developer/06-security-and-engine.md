# 7. 安全与引擎调度

## 7.1 JWT_SECRET（强制修改）

**在哪配**：环境变量 `JWT_SECRET`（`grape.env` / systemd `EnvironmentFile`）；或 `configs/security.json` 的 `jwt.secret_keys`（kid 轮换格式，见下）。重启生效。

**配置示例**（`grape.env`）：

```bash
JWT_SECRET=<your-strong-random-secret>
```

- **生产门禁**：`env=production` 时启动会做安全检查，`JWT_SECRET` 为空或命中不安全默认值（如 `change-me-in-production`）将**拒绝以生产模式启动**。
- kid 轮换（`security.json`）：`jwt.secret_keys` 格式 `kid1:secret1,kid2:secret2`，`kid_current` 指定当前签发密钥；留空则回退 `JWT_SECRET` 环境变量（无轮换）。

**如何验证**：重启后登录一次 → 会话正常；将 `JWT_SECRET` 改为默认弱值再以生产模式启动 → 启动日志出现安全门禁报错，即门禁生效。

## 7.2 第三方凭据加密（credential_keys）

**在哪配**：`configs/security.json`（复制 `security.example.json`）。重启生效。

**配置示例**：

```json
{
  "env": "production",
  "jwt": {
    "secret_keys": "",
    "kid_current": ""
  },
  "credential_keys": "prod:<base64-32byte-key>"
}
```

- `credential_keys`：第三方凭据的 AES-256-GCM 加密密钥，格式 `kid:base64key`（32 字节）；留空则从 `JWT_SECRET` 派生。
- 生成方式：`python3 -c "import base64,secrets; print('prod:'+base64.b64encode(secrets.token_bytes(32)).decode())"`

**如何验证**：配置后新增一个引擎凭据（控制台「引擎管理」）→ DB 中该凭据密文存储；解密使用正常即生效。

## 7.3 COOKIE_SECURE（HTTPS）

**在哪配**：环境变量 `COOKIE_SECURE`。重启生效。

- `COOKIE_SECURE=true`（默认）：Cookie 仅经 HTTPS 传输——生产 HTTPS 部署的正确姿势。
- `COOKIE_SECURE=false`：仅用于 HTTP 开发环境。

**如何验证**：HTTPS 域名下登录 → 浏览器开发者工具中会话 Cookie 带 `Secure` 标记。

## 7.4 登录白名单（login-whitelist.json）

**在哪配**：`configs/login-whitelist.json`（复制 `login-whitelist.example.json`）。重启生效。

**配置示例**：

```json
{
  "whitelist": [
    "<allowed-gitcode-username>",
    "<allowed-github-username>"
  ]
}
```

- 填 GitCode / GitHub 的**用户名**（OAuth 登录后按对应用户名列校验）。
- 与**注册白名单**（邮箱/域名）相互独立：本文件只管 OAuth 登录放行；注册开放开关与注册白名单另由注册策略控制。

**如何验证**：白名单内用户名可正常 OAuth 登录；名单外用户名登录被拒，即生效。

## 7.5 引擎调度（engine-selection.json）

**在哪配**：`configs/engine-selection.json`（复制 `engine-selection.example.json`）。重启生效。

**配置示例**：

```json
{
  "enabled": true,
  "scopes": ["repo", "global", "local"],
  "rule": "order",
  "filters": {
    "only_online": true,
    "only_healthy": true,
    "only_installed": true
  },
  "local_priority": 90
}
```

| 字段 | 说明 |
|------|------|
| `enabled` | 引擎调度开关 |
| `scopes` | 参与调度的引擎来源范围（仓库级 / 全局 / 本机） |
| `rule` | 选择规则（`order` = 按序回退） |
| `filters.only_online` / `only_healthy` / `only_installed` | 只选在线 / 健康 / 已安装的引擎（未安装引擎不会被误选） |
| `local_priority` | 本机引擎优先权重（默认 90，越高越优先） |

**如何验证**：控制台「系统监控 → 引擎状态」中离线/不健康引擎不再被分配任务；会话主引擎按 `scopes` + `local_priority` 顺序解析（启动会话的日志会打印引擎选择结果）。

> ⚠️ **注意**
>
> 无可用引擎时行为是**明确不可用**，不会隐式回退到默认引擎——看到「无引擎」报错请先检查 `scopes`/`filters` 是否把可用引擎全部过滤掉了。
