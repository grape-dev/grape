# DevOps Bot 多用户与自定义技能系统 — 设计文档

## 概述

为 DevOps Bot 增加多用户支持，区分管理员和普通用户，支持用户管理自己的云平台凭据，以及为指定仓库配置自定义技能。

## URL 结构

| URL | 用途 | 鉴权 |
|-----|------|------|
| `/` | 普通用户登录页 | 无 |
| `/panel?token=xxx` | 用户面板（凭据管理、技能配置） | Bearer token |
| `/admin/` | 管理员登录页 | 无 |
| `/admin/panel?token=xxx` | 管理员面板（用户管理） | Bearer token |

## 登录认证

### 管理员登录

- 固定账号：`developer` / `YOUR_SSH_PASSWORD`
- POST `/admin/login` → 返回管理员面板
- 管理员可创建/删除/重置普通用户

### 普通用户登录

- POST `/login` → 返回用户面板
- 用户由管理员预先创建（用户名 + 初始密码）
- 用户登录后可自行修改密码

## 用户身份

| 身份 | 认证方式 | 入口 |
|------|---------|------|
| 管理员（admin） | 固定账号 `developer` / `YOUR_SSH_PASSWORD`（硬编码） | `/admin/` |
| 普通用户（user） | 由管理员创建，存入 `users` 表 | `/` |

## 数据库设计（`sessions.db` 新增表）

### users 表

```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_by INTEGER,           -- 创建者的 user_id（admin 在代码中硬编码）
    created_at TEXT DEFAULT (datetime('now'))
);
```

### user_credentials 表

```sql
CREATE TABLE user_credentials (
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,       -- 'gitcode','feishu','huawei','codearts'
    config_json TEXT NOT NULL,    -- 如 {"api_token":"xxx", "app_id":"xxx"}
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, provider)
);
```

| provider | config_json 示例 |
|----------|-----------------|
| `gitcode` | `{"api_token":"xxx","webhook_token":"xxx"}` |
| `feishu` | `{"app_id":"xxx","app_secret":"xxx"}` |
| `huawei` | `{"ak":"xxx","sk":"xxx"}` |
| `codearts` | `{"ak":"xxx","sk":"xxx"}` |
| `vm` | `{"host":"203.0.113.2","port":22,"user":"root","password":"xxx","deploy_dir":"/root/repo","service_name":"devops-bot"}` |

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
浏览器 → / → 用户登录页
  ├─ POST /login → 验证 → /panel?token=xxx → 用户面板
   │    ├─ 凭据管理（GitCode / 飞书 / 华为云 / CodeArts）
   │    ├─ 云主机管理（关联仓库的部署 VM）
   │    ├─ 自定义技能（上传 SKILL.md）
   │    └─ 仓库技能映射（指定仓库用哪个技能）
   └─ 链接到 /admin/


浏览器 → /admin/ → 管理员登录页
  ├─ POST /admin/login → 验证 → /admin/panel?token=xxx → 管理员面板
  │    ├─ 创建用户
  │    ├─ 删除用户
  │    └─ 重置密码
  └─ 链接到 /
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
管理员登录 /admin/panel
  → 点击"创建用户"
  → 填写：用户名、初始密码
  → 存入 users 表（password_hash = hashlib.sha256(password)）
  → 用户收到账号信息
  → 用户登录 / → 修改密码 → 配置凭据和技能
```

## 凭据注入

当 `Agent` 子进程启动时，从 `user_credentials` 表中读取对应用户的凭据注入环境变量：

```python
# src/agent/executor.py
def stream_agent(prompt, cwd, session_id="", event_type="", backend_name="opencode", account="default", user_id=0):
    ...
    if user_id:
        from src.session_manager import get_user_credentials
        creds = get_user_credentials(user_id)
        for c in creds:
            for k, v in json.loads(c["config_json"]).items():
                env[k.upper()] = v
    ...
```

## 实施阶段

### 阶段 1：用户系统
| 文件 | 改动 |
|------|------|
| `src/session_manager.py` | 新增 users 表 + `create_user()` / `get_user()` / `list_users()` / `delete_user()` / `verify_user()` |
| `src/admin/server.py` | 区分 `/` 和 `/admin/` 两套路由、管理员面板添加用户管理页面 |

### 阶段 2：用户凭据管理
| 文件 | 改动 |
|------|------|
| `src/session_manager.py` | 新增 user_credentials / user_vms 表 + CRUD |
| `src/admin/server.py` | 普通用户面板添加凭据编辑表单 + 云主机管理表单 |

### 阶段 3：自定义技能 + 仓库映射
| 文件 | 改动 |
|------|------|
| `src/session_manager.py` | 新增 user_skills / repo_skill_mapping 表 + CRUD |
| `src/admin/server.py` | 技能上传表单、仓库映射配置 |
| `src/config.py` | `get_domain_skill()` 增加 `user_id` 和 `repo_key` 参数 |
| `src/event_processor.py` | 路由时检查 repo_skill_mapping |
| 文件系统 | 创建 `skills/custom/` 目录 |

### 阶段 4：凭据注入 Agent
| 文件 | 改动 |
|------|------|
| `src/agent/base.py` | `get_env()` 支持 user_id 参数 |
| `src/agent/executor.py` | `stream_agent()` 传递 user_id 给 `get_env()` |

## 不改动的模块

| 模块 | 说明 |
|------|------|
| `accounts.json` | bot 的监听账户体系保留，不受用户系统影响 |
| `configs/event-routing.json` | 全局路由保留，作为最低优先级 fallback |
| `feishu WS / Webhook handlers` | 事件监听和处理逻辑不变 |
| `session / history` | 现有 session 管理不变 |
| `agent/codearts.py` / `agent/opencode.py` | 后端实现不变，仅凭据来源改为 user_credentials |
| `prompt_builder.py` | 路由逻辑扩展，但构建方式不变 |

## 安全措施

- 密码使用 SHA256 哈希存储（用户登录时比对 hash）
- 用户 token 24h 过期，管理员 token 独立管理
- 用户只能访问自己的凭据和技能（user_id 隔离）
- 管理员能访问所有用户的管理操作
- 自定义技能 SKILL.md 存储在文件系统，用户间隔离（`custom/{user_id}/`）
- API 端点校验 token 对应的 user_id
