# DevOps Bot 多用户与自定义技能系统 — 设计文档

## 概述

为 DevOps Bot 增加多用户支持，区分管理员和普通用户，支持用户管理自己的云平台凭据，以及为指定仓库配置自定义技能。

## URL 结构

| URL | 用途 | 鉴权 |
|-----|------|------|
| `/` | 普通用户登录页 + 用户面板（凭据管理、技能配置） | Token+Session |
| `/admin/` | 管理员登录页 + 管理员面板 | Token+Session |
| `/login` | 普通用户登录 POST | 无 |
| `/admin/login` | 管理员登录 POST | 无 |
| `/admin/save` | 管理员保存用户账户配置 POST | Token |
| `/user/cred` | 用户更新自己的凭据 POST | Token |
| `/user/vm` | 用户管理云主机 POST | Token |
| `/user/skill` | 用户上传自定义技能 POST | Token |
| `/user/mapping` | 用户配置仓库技能映射 POST | Token |

## 登录认证

### 管理员登录

- 固定账号：`developer` / `YOUR_SSH_PASSWORD`
- POST `/admin/login` → 返回管理员面板
- 管理员可创建、编辑、删除普通用户（即 `bot_accounts` 中的账户），管理凭据、云主机、自定义技能等

### 普通用户登录

- POST `/login` → 返回用户面板
- 用户由管理员在 admin 页面创建（设置密码后自动同步到 `users` 表）
- 用户登录后可自行修改凭据、管理云主机、上传自定义技能

## 用户身份

| 身份 | 认证方式 | 入口 |
|------|---------|------|
| 管理员（admin） | 固定账号 `developer` / `YOUR_SSH_PASSWORD`（硬编码） | `/admin/` |
| 普通用户（user） | 由管理员通过 admin 页面创建，凭据同步存入 `bot_accounts` + `users` 表 | `/` |

## 核心设计原则

**`bot_accounts` 即用户账户表**。Admin 页面管理的 Bot 账户（gitcode_token、feishu_app_id 等）就是用户的凭据配置，同一份数据同时在 admin 面板和用户面板展示。不再通过 `user_credentials` 冗余存储。

## 数据库设计（`sessions.db`）

### bot_accounts 表（核心用户账户表）

```sql
CREATE TABLE bot_accounts (
    name TEXT PRIMARY KEY,
    gitcode_api_token TEXT DEFAULT '',
    gitcode_webhook_token TEXT DEFAULT '',
    gitcode_oauth_client_id TEXT DEFAULT '',
    gitcode_oauth_client_secret TEXT DEFAULT '',
    feishu_app_id TEXT DEFAULT '',
    feishu_app_secret TEXT DEFAULT '',
    huawei_cloud_ak TEXT DEFAULT '',
    huawei_cloud_sk TEXT DEFAULT '',
    CODEARTS_CLI_AK TEXT DEFAULT '',
    CODEARTS_CLI_SK TEXT DEFAULT '',
    password TEXT DEFAULT '',
    bot_usernames TEXT DEFAULT '[]'   -- JSON 数组
);
```

每个 `name` 代表一个用户/ Bot 实例。Admin 页面直接管理这些字段，用户面板也读取同一份数据。

### users 表（仅登录认证）

```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,     -- 对应 bot_accounts.name
    password_hash TEXT NOT NULL,       -- SHA256
    created_by INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
```

只存登录认证所需信息，`username` 与 `bot_accounts.name` 一一对应。

### user_credentials 表（已废弃）

不再使用。凭据直接存储在 `bot_accounts` 的各字段中。

### user_vms 表

每个用户可配置多台云主机，每台云主机可关联多个仓库。用于 Bot 的 cloud-deploy 技能 SSH 登录部署。

```sql
CREATE TABLE user_vms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,              -- 主机别名，如 '生产环境'
    host TEXT NOT NULL,              -- IP 或域名
    port INTEGER DEFAULT 22,
    user TEXT NOT NULL,              -- SSH 用户名
    password TEXT NOT NULL,          -- SSH 密码
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, name)
);

CREATE TABLE user_vm_repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vm_id INTEGER NOT NULL,          -- 关联 user_vms.id
    repo_key TEXT NOT NULL,          -- 仓库，如 'owner/repo'
    deploy_dir TEXT,                 -- 部署目录，如 /opt/app/{repo}
    service_name TEXT,               -- systemd 服务名，如 devops-bot
    branch TEXT DEFAULT 'master',    -- 部署分支
    UNIQUE (vm_id, repo_key)
);
```

### user_skills 表

```sql
CREATE TABLE user_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    skill_name TEXT NOT NULL,     -- 如 'my-pr-skill'
    skill_dir TEXT NOT NULL,      -- skills/custom/{user_id}/{skill_name}/
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, skill_name)
);
```

技能文件存储在文件系统目录 `skills/custom/{user_id}/{skill_name}/SKILL.md`，DB 只存索引。

### repo_skill_mapping 表

```sql
CREATE TABLE repo_skill_mapping (
    user_id INTEGER NOT NULL,
    repo_key TEXT NOT NULL,       -- 'owner/repo'
    event_type TEXT NOT NULL,     -- 'pull_request_opened','issue_opened' 等
    skill_name TEXT NOT NULL,     -- 对应 user_skills.skill_name
    PRIMARY KEY (user_id, repo_key, event_type)
);
```

等价于当前 `configs/event-routing.json` 的 `repo_routing`，但由用户在界面上自行配置。

## 页面流

```
浏览器 → / → 用户登录页 / 用户面板
  ├─ POST /login → 验证密码 → 返回用户面板
   │    ├─ 凭据管理（GitCode Token / 飞书 / 华为云 / CodeArts，同 admin 样式带当前值）
   │    ├─ 云主机管理（关联仓库的部署 VM）
   │    ├─ 自定义技能（上传 SKILL.md）
   │    └─ 仓库技能映射（指定仓库用哪个技能）
   └─ 链接到 /admin/


浏览器 → /admin/ → 管理员登录页 / 管理员面板
  ├─ POST /admin/login → 验证固定账号 → 返回管理员面板
   │    ├─ Bot 账户列表（即用户列表，带状态指示灯）
   │    ├─ 编辑账户配置（凭据、Bot 用户名、密码）
   │    ├─ 点击保存后同步密码到 users 表
   └─ 链接到 /
```

凭据数据流：

```
Admin 保存 bot_accounts  →  bot_accounts 表（gitcode_api_token 等字段）
                                ↓
                           ACCOUNTS 缓存（config.py）
                                ↙          ↘
                    用户面板读取显示      Executor 注入环境变量
```

## 事件路由优先级

```
GitCode Webhook 事件到达
  → event_processor.py
  → 检查 repo_skill_mapping（用户配置）
    → 有 → 使用用户自定义技能
    → 无 → 检查 event-routing.json repo_routing（全局配置）
      → 有 → 使用全局指定技能
      → 无 → 检查 event-routing.json routing（全局默认）
```

## 自定义技能校验规则

用户上传自定义技能时，服务端需校验以下内容，不合规的返回错误提示。

### 1. 目录结构校验

```
my-skill-name/
├── SKILL.md              # 必填，唯一入口文件
└── references/           # 可选，参考文档（会被 Agent 自动发现）
    ├── api-reference.md  # 可选
    └── best-practice.md  # 可选
```

通过条件：
- 必须包含 `SKILL.md` 文件
- 不允许包含 Java/Python/Shell 等可执行脚本或编译产物
- 文件总大小不超过 1MB
- `references/` 目录下只允许 `.md` / `.txt` / `.json` / `.yaml` / `.yml` 文件

### 2. YAML Front Matter 校验

```yaml
---
name: my-skill-name           # 必填，必须与目录名一致
version: 1.0.0               # 必填，语义化版本
description: 一句话描述      # 必填，不超过 200 字
triggers:                    # 可选
  - event_type: pull_request_opened
tools:                       # 可选
  - git
---
```

必填字段检查：
| 字段 | 校验规则 |
|------|---------|
| `name` | 非空，仅允许小写字母、数字、连字符（`[a-z0-9-]+`），与目录名一致 |
| `version` | 匹配 `\d+\.\d+\.\d+` |
| `description` | 非空，不超过 200 字 |

### 3. Markdown Body 校验

```markdown
# Skill 标题

## 场景
描述适用场景...

## 知识
领域知识和最佳实践...

## 步骤
处理流程...
```

必填章节检查：
| 章节 | 校验规则 |
|------|---------|
| `## 场景` | 必须存在且非空 |
| `## 知识` | 必须存在且非空 |
| `## 步骤` | 必须存在且非空，至少包含一个有序或无序列表项（步骤） |

### 4. 命名要求

- 目录名（也是 `name` 字段）必须为 kebab-case：`[a-z0-9-]+`
- 不能与现有项目级技能同名（`pr`、`issue`、`implementation`、`build-debug`、`deploy-guard`、`incident-triage`、`changelog-gen`、`chat`、`cloud-deploy`、`im-notify`、`agent`）
- 不能与同一用户的其他自定义技能同名

### 5. 校验失败处理

返回 400 错误，JSON 体包含具体失败原因：

```json
{
  "error": "技能校验未通过",
  "details": [
    "缺少 SKILL.md 文件",
    "name 字段与目录名不一致",
    "「知识」章节不能为空"
  ]
}
```

### 6. 服务端校验实现

```python
import re, os, yaml

def validate_custom_skill(upload_dir: str, skill_name: str) -> list[str]:
    errors = []

    # 1. 检查 SKILL.md 是否存在
    skill_path = os.path.join(upload_dir, "SKILL.md")
    if not os.path.exists(skill_path):
        return [f"缺少 SKILL.md 文件"]

    with open(skill_path, encoding="utf-8") as f:
        content = f.read()

    # 2. 解析 Front Matter
    parts = content.split("---", 2)
    if len(parts) < 3:
        errors.append("缺少 YAML Front Matter（--- 分隔）")
        return errors
    try:
        fm = yaml.safe_load(parts[1])
    except Exception as e:
        errors.append(f"Front Matter 解析失败: {e}")
        return errors

    if not isinstance(fm, dict):
        errors.append("Front Matter 必须是一个对象")
        return errors

    # name 校验
    name = fm.get("name", "")
    if not re.match(r"^[a-z0-9-]+$", name):
        errors.append("name 字段仅允许小写字母、数字、连字符")
    if name != skill_name:
        errors.append(f"name（{name}）与目录名（{skill_name}）不一致")

    # version 校验
    version = str(fm.get("version", ""))
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        errors.append("version 字段必须符合语义化版本号格式（x.y.z）")

    # description 校验
    desc = fm.get("description", "")
    if not desc:
        errors.append("description 字段不能为空")
    elif len(desc) > 200:
        errors.append("description 字段不超过 200 字")

    # 3. 解析 Body
    body = parts[2] if len(parts) > 2 else ""
    required_sections = ["## 场景", "## 知识", "## 步骤"]
    for sec in required_sections:
        if sec not in body:
            errors.append(f"缺少「{sec.replace('## ', '')}」章节")

    # 4. 步骤章节至少包含一个列表项
    if "## 步骤" in body:
        steps_section = body.split("## 步骤", 1)[1].split("##", 1)[0]
        if not re.search(r"^\s*[-*\d.]", steps_section, re.MULTILINE):
            errors.append("「步骤」章节必须包含至少一个列表项")

    # 5. 检查禁止内容
    forbidden_extensions = [".py", ".sh", ".bat", ".exe", ".jar", ".class"]
    for root, dirs, files in os.walk(upload_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in forbidden_extensions:
                errors.append(f"不允许上传可执行文件: {f}")
                break

    return errors
```

## 自定义技能存储

```
skills/
├── pr/                       # 项目级技能（不变）
├── skill-pr/
├── issue/
├── ...                       # 其他项目技能
└── custom/                   # 用户自定义技能
    ├── 1/                    # user_id = 1
    │   ├── my-pr-skill/
    │   │   ├── SKILL.md      # 用户上传的 SKILL.md
    │   │   └── references/   # 可选参考文档
    │   └── my-issue-skill/
    │       └── SKILL.md
    └── 2/                    # user_id = 2
        └── ...
```

## 管理员创建用户流程

```
管理员登录 /admin/
  → 在 admin 面板左侧账户列表底部，填写新名称 + 密码
  → 点"保存" → 写入 bot_accounts 表
  → 密码同步到 users 表（hash）
  → 用户收到账号信息
  → 用户登录 / → 查看/编辑凭据和管理技能
```

## 用户凭据自管理

用户登录 `/` 面板后，凭据管理页面展示 `bot_accounts` 中该用户的所有凭据字段（GitCode Token、飞书 App ID/Secret、华为云 AK/SK、CodeArts AK/SK），带当前值和 👁 密码切换，修改后点「保存」直接写入 `bot_accounts` 表。无需 admin 干预。

## 凭据注入

当 `Agent` 子进程启动时，从 `bot_accounts` 表（通过 `ACCOUNTS` 缓存）读取对应用户的凭据注入环境变量：

```python
# src/user/db.py
def get_credentials(user_id: int) -> list[dict]:
    """获取用户凭据（从 bot_accounts 表读取）"""
    user = get_user(user_id)
    ...
    # 返回格式：[{"provider":"gitcode", "config":{"token":"xxx"}}, ...]

# src/agent/executor.py
for c in get_credentials(user_id):
    for k, v in c.get("config", {}).items():
        env[k.upper()] = str(v)
```

注入的环境变量：

| 凭据 | 字段 | 环境变量 |
|------|------|---------|
| GitCode | token | `TOKEN` |
| 飞书 | app_id, app_secret | `APP_ID`, `APP_SECRET` |
| 华为云 | ak, sk | `AK`, `SK` |
| CodeArts | ak, sk | `AK`, `SK` |

## 当前实现状态（已完工）

| 组件 | 核心文件 | 说明 |
|------|---------|------|
| 用户系统 | `src/user/db.py` | `users` + `bot_accounts` + `user_vms` + `user_skills` + `repo_skill_mapping` 表 CRUD |
| Admin 面板 | `src/admin/server.py` | 纯服务端 HTML 渲染，登录页 / 账户管理 / 用户管理 |
| 用户面板 | `src/admin/server.py: _render_user_panel()` | 凭据管理（读/写 bot_accounts）、云主机、自定义技能、仓库映射 |
| 凭据注入 | `src/user/db.py: get_credentials()` + `src/agent/executor.py` | Agent 运行时从 bot_accounts 读凭据注入环境变量 |
| 路由扩展 | `src/event_processor.py` + `src/config.py` | 事件路由支持 repo_skill_mapping 优先 |
| 自定义技能校验 | `src/user/validator.py` | YAML Front Matter、目录结构、安全限制 |

## 不改动的模块

| 模块 | 说明 |
|------|------|
| `configs/event-routing.json` | 全局路由保留，作为最低优先级 fallback |
| `feishu WS / Webhook handlers` | 事件监听和处理逻辑不变 |
| `session / history` | 现有 session 管理不变 |
| `agent/codearts.py` / `agent/opencode.py` | 后端实现不变，仅凭据来源改为 bot_accounts |
| `prompt_builder.py` | 路由逻辑扩展，但构建方式不变 |

## 安全措施

- 密码使用 SHA256 哈希存储（用户登录时比对 hash）
- 用户 token 24h 过期，管理员 token 独立管理
- 用户只能访问自己的凭据和技能（user_id 隔离）
- 管理员能访问所有用户的管理操作
- 自定义技能 SKILL.md 存储在文件系统，用户间隔离（`custom/{user_id}/`）
- API 端点校验 token 对应的 user_id
