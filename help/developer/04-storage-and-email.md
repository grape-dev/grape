# 5. 对象存储与邮件

## 5.1 对象存储（OBS）

技能包、图标等上传文件默认存本地；启用 OBS（华为云对象存储）后改存云端，**不配置则自动降级为本地存储**，功能不中断。

**在哪配**：环境变量（`grape.env` / systemd `EnvironmentFile` / 部署平台注入），重启服务生效。

**配置示例**（`grape.env`）：

```bash
OBS_ENABLED=true
OBS_ENDPOINT=https://obs.cn-north-4.myhuaweicloud.com
OBS_BUCKET=<your-bucket-name>
OBS_ACCESS_KEY=<your-ak>
OBS_SECRET_KEY=<your-sk>
OBS_PREFIX=grape/prod
# 可选：
OBS_URL_EXPIRES=300
```

| 变量 | 说明 |
|------|------|
| `OBS_ENABLED` | 总开关（`true`/`1`/`yes`），默认 `false`（本地存储） |
| `OBS_ENDPOINT` | OBS 服务端点（默认华北-北京四） |
| `OBS_BUCKET` | 桶名 |
| `OBS_ACCESS_KEY` / `OBS_SECRET_KEY` | 访问密钥（AK/SK） |
| `OBS_PREFIX` | 桶内对象前缀（默认 `grape/prod`），多环境共用一桶时用于隔离 |
| `OBS_URL_EXPIRES` | 预签名下载链接有效期（秒，默认 300） |

**如何验证**：重启后在控制台上传一个文件（如技能包/图标）→ 下载 URL 域名指向 OBS 桶（预签名地址）→ 文件可正常下载，即已切换成功。日志无 OBS 报错、且本地存储目录不再新增该文件。

## 5.2 邮件（SMTP / Resend）

用于注册/找回密码的验证码与通知邮件。

**在哪配**：`configs/email.json`（复制 `email.example.json`）。**改后无需重启**（每次发送时读取）。

**配置示例**（QQ 邮箱 SMTP）：

```json
{
  "env": "production",
  "mode": "smtp",
  "from_email": "noreply@yourdomain.com",
  "smtp": {
    "host": "smtp.qq.com",
    "port": 465,
    "username": "your@qq.com",
    "password": "<your-smtp-authorization-code>",
    "use_tls": true
  }
}
```

关键字段：

| 字段 | 说明 |
|------|------|
| `env` | `production` = 强制真实邮件服务；`development`（或留空）= 开发模式，允许固定验证码、日志降级 |
| `mode` | `smtp` = SMTP 中继；`resend` = Resend API；留空且非生产环境时降级为**日志输出**（不发真实邮件） |
| `smtp.password` | SMTP **授权码**（不是邮箱登录密码）。QQ 邮箱：设置 → 账户 → POP3/SMTP 服务 → 开启 → 获取授权码 |
| `dev_verification_code` | 开发环境固定验证码（生产模式自动忽略） |

常见邮箱端口：QQ `smtp.qq.com:465` · 163 `smtp.163.com:465` · Gmail `smtp.gmail.com:587` · 腾讯企业邮 `smtp.exmail.qq.com:465`。

**如何验证**：在登录/找回密码页触发一次「发送验证码」→ 数秒内收到邮件 → 用邮件中的验证码完成校验，即链路全通。若配置为 `mode=resend`，填 `resend.api_key`（resend.com 创建，免费额度 100 封/天，发件域名需在 Resend 后台验证）。

> ⚠️ **注意**
>
> `env=production` 时固定验证码会被强制忽略——生产环境必须配好真实邮件通道，否则验证码无法送达。
