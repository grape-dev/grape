# 云沙箱 Provider 插拔设计

> 分支：feat/devbox-sandbox-integration (HEAD a4b1c8ad)
> 日期：2026-10-09
> 状态：已确认

## 0. 设计原则

### 0.1 职责单一——每个对象只管一件事

| 对象 | 职责 | 不做什么 |
|------|------|---------|
| runtime_credentials（rta_） | worker 信任边界：register/heartbeat/claim 身份认证 | 不携带 scope 隔离信息；不携带用户身份；不获取用户敏感数据 |
| agent_runtimes | 引擎实例：per-runtime scope/owner_id/owner_name 管任务分配隔离 | 不从 credential 继承 scope；scope 从 Provider 配置解析 |
| sandbox_provider_config | Provider 实例配置：platform/endpoint/api_key/scope/user_id/repo_key | 不持有 credential；不注册 runtime |
| user_gitcode_token | 用户身份：以用户身份调外部 API（GitCode） | 不用于 grape 内部认证；不通过 worker credential 获取 |

**核心**：credential 管信任边界（credential_id 匹配），agent_runtimes.scope 管任务分配隔离（per-runtime 真实 scope）。两者完全独立——credential 不携带 scope 隔离信息，scope 变化不影响 credential_id 匹配。

### 0.2 单一数据源——每个数据只有一个权威来源

| 数据 | 唯一来源 | 废弃的冗余来源 |
|------|---------|--------------|
| runtime scope/owner_id/owner_name | agent_runtimes 表（从 Provider 配置解析写入） | ~~runtime_credentials.scope~~（过滤点不再 JOIN rc）、~~provider_scope 列~~（v2 废弃） |
| Provider scope/user_id/repo_key | sandbox_provider_config 表 | ~~runtime_info 中的 provider_scope/provider_user_id~~（死字段已删） |
| 引擎-模板关联 | engine_template_binding 表 | ~~sandbox_templates.engines JSON~~（废弃，单一数据源） |
| 用户敏感信息 | task.user_id → 服务端按需查询 | ~~worker credential 携带用户身份~~（不携带） |

**核心**：避免双源。register API 从 runtime_info.provider_id 查 Provider 配置取 scope/user_id/repo_key（DB 真值），不从 runtime_info 投影字段取。过滤点直接读 agent_runtimes，不 JOIN runtime_credentials。

### 0.3 生命周期独立——对象之间不互相持有

| 关系 | 独立性 | 说明 |
|------|--------|------|
| credential ↔ Provider | 完全独立 | Provider 不持有 credential_id。credential 是 worker 级（cloud_worker 启动时生成），Provider 是配置级（用户/系统创建）。删除 Provider 不影响 credential，吊销 credential 不影响 Provider 配置 |
| Provider ↔ runtime | 间接关联 | 通过 provider_id 关联。Provider 删除 → cloud_worker 下次 refresh 下线对应 runtime（不直接删 runtime） |
| worker token ↔ user token | 完全独立 | worker credential（rta_）只获取公共数据。用户 token（user_gitcode_token）注入 task 供 worker 调外部 API。两条路径独立 |

**核心**：Provider 创建/删除不生成/删除 credential。Provider 创建/删除不直接创建/删除 runtime（通过 cloud_worker refresh 间接下线）。

### 0.4 互斥注入——敏感凭据按任务类型隔离

| 任务类型 | 注入 token | 不注入 token | 原因 |
|---------|-----------|-------------|------|
| chat | USER_GITCODE_TOKEN | ~~BOT_GITCODE_TOKEN~~ | 防用户对话上下文暴露仓库 bot token |
| devops 事件 | BOT_GITCODE_TOKEN | ~~USER_GITCODE_TOKEN~~ | 事件任务用仓库身份，不注入用户 token |

**核心**：cloud_worker 和 local worker 的 _assemble_env 保持一致——按 event_type 互斥注入 GitCode token，不同时注入。

### 0.5 面向接口——平台差异通过钩子/注入隔离

| 场景 | 接口 | 实现 |
|------|------|------|
| SSL 证书 | `_post_create_hook` 钩子方法 | CubeSandboxInstance override，SDK 私有属性访问局限在一个方法内 |
| 沙箱创建 | registry[platform] 工厂模式 | platform 用于查找工厂，provider_name 用于实例区分 |
| 配置注入 | config dict 依赖注入 | 子类从 config 取实例名，不硬编码 |

**核心**：CloudSandboxBase 定义钩子接口，子类实现具体逻辑。SDK 升级时只改钩子方法一处。

## 1. 需求分析

### 1.1 需求清单

| # | 需求 | 说明 |
|---|------|------|
| R1 | 多 Provider 配置 | 支持 cubesandbox / devbox 两种平台类型，每种可配置多个实例 |
| R2 | 系统 Provider 只读 | 系统预置的 Provider 配置，用户只能查看不能修改 |
| R3 | 用户 Provider CRUD | 用户可新增、编辑、删除、启停自己的 Provider 实例 |
| R4 | 模板-Provider 关联 | 模板归属于具体 Provider 实例，用户 Provider 下可增删改模板 |
| R5 | 引擎模板唯一约束 | 同一 Provider 实例下，每个引擎类型只能配置一个模板 |
| R6 | cloud_worker 自动同步 | cloud_worker 定时拉取所有 enabled 的 Provider + 模板，自动注册 runtime |
| R7 | 基于模板构建 runtime | cloud_worker 根据 Provider + 模板列表展开为不同 runtime 引擎 |
| R8 | 引擎 scope 独立于 worker scope | cloud_worker 基于 system credential 注册不同 scope 级别的 runtime（global/user/repo），引擎 scope 从 Provider 配置解析，不继承 credential scope |
| R9 | 用户敏感数据基于用户 token 获取 | task 携带用户 token（user_gitcode_token），worker 用用户 token 直接调外部接口（如 GitCode API）获取用户级敏感数据（代码仓库、PR、Issue 等）。用户 token 不通过 worker credential 获取——worker credential 不携带用户身份 |
| R10 | worker 凭据只获取公共数据 | worker credential（rta_）只用于调 grape /env API 获取服务端管理的公共凭据（LLM key、AK/SK 等），不用于获取用户级敏感数据 |
| R11 | 池广播隔离 | 系统 Provider 的 runtime 设 agent_tags=["all"] 参与池广播；用户级/仓库级 Provider 的 runtime 设 agent_tags=[] 不参与池广播——用户/仓库级 Provider 的 runtime 只认领 scope 匹配的任务，不抢公共池任务 |
| R12 | /env API 越权防护 | /env API 校验调用方是否真正认领了该 task：task.runtime_id 非零时校验 runtime.credential_id == cred.id，不匹配返回 403。防止 worker A 获取 worker B 认领的 task 的用户敏感信息 |
| R13 | cloud_worker token 互斥注入 | cloud_worker 按 event_type 互斥注入 GitCode token（和 local worker _assemble_env 一致）：chat 任务只注入 USER_GITCODE_TOKEN 不注入 BOT_GITCODE_TOKEN（防 bot token 暴露在用户对话上下文）；devops 事件任务只注入 BOT_GITCODE_TOKEN 不注入 USER_GITCODE_TOKEN |
| R14 | SSL 证书 per-provider 隔离 | 多 cubesandbox Provider 各有不同 ssl_cert_content 时，证书按 provider_id 落盘（不设进程级 REQUESTS_CA_BUNDLE 互相覆盖），通过 _post_create_hook 钩子方法在 SDK session 创建后注入 per-provider 证书 |
| R15 | provider_name 与 platform 分离 | CloudSandboxBase 构造时接收 provider_name（实例可读名，从 config 注入不硬编码）和 platform（平台类型，固定 cubesandbox/devbox）两个参数。platform 用于 registry 查找和引擎策略分派，provider_name 用于日志/DB/审计区分 |
| R16 | Runtime 孤儿清理 | runtime name 格式从 `{engine}:{provider}` 改为 `{engine}:{platform}:{provider_id}`，旧格式 runtime 变孤儿。迁移时清理存量 cloud 孤儿 runtime + _refresh_templates 首次从 DB 初始化 _last_runtimes + register offline 注册时清理同 credential_id 下不在新列表中的 cloud runtime |
| R17 | 过滤点去耦合 | 三个过滤点（_member_scope / claim_by_tag / _check_pool_engines_reachable）从 JOIN runtime_credentials 取 scope 改为直接读 agent_runtimes。agent_runtimes 成为 scope 唯一数据源，runtime_credentials 只管信任边界，过滤点不再依赖 runtime_credentials 的 scope 字段 |

### 1.2 现有架构局限

当前 `sandbox_provider_config` 表 `provider` 字段 UNIQUE，全局只能有一个 cubesandbox 和一个 devbox 实例。模板 `sandbox_templates` 无 Provider 实例外键关联，所有模板全局共享，cloud_worker 拉取全部模板统一展开。

具体局限：

1. `sandbox_provider_config.provider` 是 VARCHAR(64) UNIQUE → 同类平台只能配一个实例
2. 无 user_id / scope 字段 → 无法区分系统配置和用户配置
3. `sandbox_templates.provider` 是字符串（"cubesandbox"/"devbox"），不是外键 → 无法关联到具体 Provider 实例
4. 模板的 engines 是 JSON 数组，无约束 → 同一 provider 下同一引擎可配多个模板
5. cloud_worker `_expand_templates` 按 provider 字符串展开，无实例区分

## 2. 数据模型设计

### 2.1 表结构变更

#### 2.1.1 sandbox_provider_config 表改造

现有表结构（不满足多实例需求）：

```sql
CREATE TABLE sandbox_provider_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider VARCHAR(64) NOT NULL UNIQUE,  -- UNIQUE 约束限制多实例
    endpoint VARCHAR(512) DEFAULT '',
    api_key_enc TEXT,
    extra_config TEXT,
    enabled INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_by VARCHAR(64) DEFAULT ''
)
```

改造后表结构（新增 name/平台类型/scope/user_id/credential_id，去掉 provider UNIQUE）：

```sql
-- DDL (SQLite 语法, MySQL 由 mysql_compat 翻译)
CREATE TABLE IF NOT EXISTS sandbox_provider_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(128) NOT NULL,              -- Provider 实例名称（用户自定义，如 "我的CubeSandbox-1"）
    platform VARCHAR(64) NOT NULL,           -- 平台类型：cubesandbox / devbox
    endpoint VARCHAR(512) NOT NULL DEFAULT '',-- API URL
    api_key_enc TEXT,                        -- 加密存储的 API Key
    extra_config TEXT,                       -- 平台特有配置 JSON（ssl_cert_content / gateway_url 等）
    enabled INTEGER DEFAULT 1,               -- 启用/禁用
    scope VARCHAR(16) NOT NULL DEFAULT 'system', -- system / user（仓库级也存 scope=user + repo_key 非空）
    user_id INTEGER DEFAULT 0,              -- scope=user 时关联用户；scope=system 时为 0
    repo_key VARCHAR(512) DEFAULT '',       -- 仓库级 Provider 时存仓库全名（scope=user + repo_key 非空 = 仓库级）；用户级/系统级为空
    credential_id INTEGER DEFAULT 0,         -- 已废弃（v3 删除，Provider 不持有 credential）
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_by VARCHAR(64) DEFAULT ''
)
```

关键变更：
- `provider` → `platform`：语义从"唯一标识"改为"平台类型"
- 去掉 `provider` 的 UNIQUE 约束
- 新增 `name`：Provider 实例名称，用户可读
- 新增 `scope`：system / user（仓库级也存 scope=user + repo_key 非空，不单独存 scope=repo）
- 新增 `user_id`：scope=user 时关联用户
- 新增 `repo_key`：仓库级 Provider 存仓库全名（scope=user + repo_key 非空 = 仓库级）；用户级/系统级为空
| credential_id | **已删除**（v3 不需要） |

> **v3 变更**：v2 在 sandbox_provider_config 上定义 credential_id 字段，设想每个 Provider 用自己的 credential 注册 runtime。v3 已改为 credential 只管信任边界（所有 runtime credential_id=cloud_worker 的 cred.id），scope 从 Provider 配置解析。Provider 不需要自己的 credential，credential_id 字段删除。Provider 创建/删除不再生成/删除 runtime_credentials。 |
- UNIQUE 约束改为 `UNIQUE(scope, user_id, repo_key, name)`——同一用户同一仓库下实例名不重复（仓库级 Provider 按 repo_key 区分，用户级 repo_key 为空）

#### 2.1.2 sandbox_templates 表改造

现有表结构（provider 是字符串，无实例关联）：

```sql
CREATE TABLE sandbox_templates (
    ...
    provider VARCHAR(64) NOT NULL,  -- "cubesandbox" / "devbox" 字符串
    template_id VARCHAR(255) NOT NULL,
    engines VARCHAR(1024) DEFAULT '[]',
    ...
)
```

改造后表结构（新增 provider_id 外键关联）：

```sql
CREATE TABLE IF NOT EXISTS sandbox_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL,             -- 关联 sandbox_provider_config.id
    name VARCHAR(255) NOT NULL,
    template_id VARCHAR(255) NOT NULL,        -- 平台侧模板 ID
    alias VARCHAR(255) DEFAULT '',
    description VARCHAR(512) DEFAULT '',
    preinstalled VARCHAR(2048) DEFAULT '{}',
    disk_size_mb INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    last_check_at VARCHAR(64) DEFAULT '',
    last_check_status VARCHAR(32) DEFAULT '',
    last_check_error VARCHAR(512) DEFAULT '',
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    created_by VARCHAR(64) DEFAULT '',       -- 存用户名字符串（和现有约定一致）
    updated_by VARCHAR(64) DEFAULT '',        -- 存用户名字符串（和现有约定一致）
    FOREIGN KEY (provider_id) REFERENCES sandbox_provider_config(id) ON DELETE CASCADE
)
```

关键变更：
- `provider VARCHAR(64)` → `provider_id INTEGER`：外键关联到具体 Provider 实例
- 新增 `FOREIGN KEY ... ON DELETE CASCADE`：删除 Provider 时自动删除关联模板
- **废弃 engines 字段**——引擎-模板关联全部走 engine_template_binding 表（见 §2.1.3）
- 保留旧 `provider` 列作为冗余列兼容（JOIN 时减少查询），由应用层写入，后续可删

#### 2.1.3 engine_template_binding 表（新增）

满足 R5"每个引擎只能配一个模板"约束。废弃 sandbox_templates.engines JSON 字段，引擎-模板关联全部走 binding 表——单一数据源，无双写一致性问题。

```sql
CREATE TABLE IF NOT EXISTS engine_template_binding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL,
    engine_type VARCHAR(64) NOT NULL,         -- hermes / opencode / codex / codearts
    template_id INTEGER NOT NULL,             -- 关联 sandbox_templates.id
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    UNIQUE(provider_id, engine_type),
    FOREIGN KEY (provider_id) REFERENCES sandbox_provider_config(id) ON DELETE CASCADE,
    FOREIGN KEY (template_id) REFERENCES sandbox_templates(id) ON DELETE CASCADE
)
```

关键设计：
- `UNIQUE(provider_id, engine_type)`：同一 Provider 实例下，同一引擎只能绑定一个模板
- **废弃 sandbox_templates.engines 字段**——引擎-模板关联全部走 binding 表，单一数据源
- 创建/更新/删除模板时不写 engines JSON，改为通过 bind-engine / unbind-engine API 操作 binding 表

废弃 engines 后各读路径改造：

| 读路径 | 现有（engines JSON） | 改造后（binding 表） |
|--------|---------------------|---------------------|
| cloud_worker `_expand_templates` | `json.loads(t.get("engines"))` 遍历 | sandbox-templates 接口返回每个模板绑定的引擎列表（JOIN binding 表） |
| repository.py `query_sandbox_template` | `engines LIKE '%"hermes"%'` SQL 模糊匹配 | JOIN engine_template_binding ON engine_type=? |
| settings.py 模板 CRUD | body.engines 存入 DB | 不接收 engines 参数，改为 bind-engine API |
| 前端 sandbox.js | 展示 t.engines | 展示从 binding 表 JOIN 出的引擎列表 |

### 2.2 数据模型关系图

```
sandbox_provider_config (1) ──< sandbox_templates (N)
        │                           │
        │                           │
        └──< engine_template_binding (N) >──┘
                    │
                    └── UNIQUE(provider_id, engine_type)
                        → 同一 Provider 下每个引擎只绑一个模板
```

### 2.3 存量数据迁移

迁移逻辑放在 `db_setup.py` 的 `_migrate_tables` 函数中，服务启动时自动执行（幂等）。遵循现有模式：`_ensure_column` 加列 + 独立 try 块建表 + 数据回填。

现有数据状态：
- sandbox_provider_config 表：0 行（空表，provider 配置在配置文件中）
- sandbox_templates 表：3 行（provider="cubesandbox"，created_by="system"）
- cloud_sandboxes 表：0 行

| 步骤 | 操作 | 位置 | 说明 |
|------|------|------|------|
| M1 | _ensure_column sandbox_provider_config name VARCHAR(128) | _migrate_tables migrations 列表 | 存量行用 platform 值填充（下面 M6 回填） |
| M2 | _ensure_column sandbox_provider_config platform VARCHAR(64) | _migrate_tables migrations 列表 | 从 provider 列复制（下面 M6 回填） |
| M3 | _ensure_column sandbox_provider_config scope VARCHAR(16) DEFAULT 'system' | _migrate_tables migrations 列表 | 存量行默认 system |
| M4 | _ensure_column sandbox_provider_config user_id INTEGER DEFAULT 0 | _migrate_tables migrations 列表 | 存量行默认 0 |
| M4b | _ensure_column sandbox_provider_config repo_key VARCHAR(512) DEFAULT '' | _migrate_tables migrations 列表 | 仓库级 Provider 存仓库全名；用户级/系统级为空 |
| M5 | ~~_ensure_column sandbox_provider_config credential_id~~ | ~~_migrate_tables migrations 列表~~ | **v3 删除**——Provider 不持有 credential |
| M6 | 独立 try 块：回填 name/platform | _migrate_tables 独立 try | UPDATE sandbox_provider_config SET name=provider, platform=provider WHERE name='' OR platform='' |
| M7 | 独立 try 块：DROP UNIQUE INDEX provider | _migrate_tables 独立 try | MySQL: ALTER TABLE sandbox_provider_config DROP INDEX provider；SQLite: DROP INDEX IF EXISTS（需检测索引存在） |
| M8 | 独立 try 块：CREATE UNIQUE INDEX(scope, user_id, repo_key, name) | _migrate_tables 独立 try | CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_scope ON sandbox_provider_config(scope, user_id, repo_key, name) |
| M9 | _ensure_column sandbox_templates provider_id INTEGER DEFAULT 0 | _migrate_tables migrations 列表 | 从 provider 字符串回填（下面 M10） |
| M10 | 独立 try 块：回填 provider_id | _migrate_tables 独立 try | UPDATE sandbox_templates t SET provider_id = (SELECT id FROM sandbox_provider_config WHERE platform = t.provider LIMIT 1) WHERE t.provider_id = 0 |
| M11 | 独立 try 块：CREATE TABLE engine_template_binding | _migrate_tables 独立 try | 建表 + 从 sandbox_templates.engines JSON 展开回填 binding + 回填后 engines 列不再读写（保留列兼容，后续可删） |
| M12 | _ensure_column cloud_sandboxes provider_id INTEGER DEFAULT 0 | _migrate_tables migrations 列表 | kill 流程按 provider_id 查沙箱 |
| M13 | 独立 try 块：预置系统 Provider | db_setup.py _migrate_provider_configs_to_db + _seed_sandbox_templates 改造 | _seed_cloud_worker_key 生成 credential（不写入 Provider）→ _migrate_provider_configs_to_db 写 Provider(scope=system, user_id=0) → _seed_sandbox_templates 写模板+binding |
| M14 | 独立 try 块：清理存量 cloud 孤儿 runtime | _migrate_tables 独立 try | DELETE FROM agent_runtimes WHERE runtime_type='cloud' AND name LIKE '%:%' + 清理 engine_capabilities 孤儿行 |

幂等保证：
- _ensure_column 检测列已存在则跳过
- UPDATE WHERE 条件保证只回填空值
- CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
- engine_template_binding 回填前检查已存在的 binding 行

## 3. API 设计

### 3.1 Provider CRUD API

#### 3.1.1 列出 Provider

```
GET /api/settings/sandbox-providers
```

- 权限：登录用户
- 返回：系统 Provider（scope=system）+ 当前用户 Provider（scope=user, user_id=当前用户）
- api_key 脱敏（***）

#### 3.1.2 新增 Provider

```
POST /api/settings/sandbox-providers
```

- 权限：登录用户（scope 自动设为 user, user_id 自动设为当前用户）
- 请求体：

```json
{
  "name": "我的CubeSandbox-1",
  "platform": "cubesandbox",
  "endpoint": "https://cube.example.com",
  "api_key": "sk-xxx",
  "extra_config": {
    "ssl_cert_content": "..."
  },
  "enabled": true,
  "scope_level": "user",
  "repo_key": ""
}
```

- scope_level：user（用户级，默认）/ repo（仓库级）
- repo_key：scope_level=repo 时必填，目标仓库全名
- 校验：platform ∈ {cubesandbox, devbox}；name 同用户下不重复；scope=user 时校验当前用户 Provider 数量未超上限（默认 5，可配置）
- **v3：不生成 runtime credential**——Provider 不持有 credential。cloud_worker 用自己的 system credential 注册所有 Provider 的 runtime，scope 从 Provider 配置解析。Provider 创建/删除不涉及 runtime_credentials。
- 响应：`{"id": 3, "message": "Provider 创建成功"}` 或 `{"code": 409, "message": "已达到 Provider 数量上限(5)"}`

#### 3.1.3 更新 Provider

```
PUT /api/settings/sandbox-providers/{provider_id}
```

- 权限：system scope → 直接 403；user scope → 本人 only
- api_key 传 null 保留旧值（和现有逻辑一致）
- 请求体同 3.1.2（不含 platform，platform 创建后不可改）

#### 3.1.4 删除 Provider（两步确认流程）

**第一步：预览删除影响**

```
GET /api/settings/sandbox-providers/{provider_id}/delete-preview
```

- 权限：system scope → 直接 403；user scope → 本人 only
- 响应：返回关联的沙箱和模板数量，供前端展示确认弹窗

```json
{
  "provider_id": 3,
  "provider_name": "我的CubeSandbox-1",
  "platform": "cubesandbox",
  "running_sandboxes": [
    {"sandbox_id": "sb-xxx", "issue_id": "123", "repo_key": "gitcode:owner/repo", "state": "running"}
  ],
  "running_sandbox_count": 1,
  "template_count": 2,
  "template_names": ["hermes模板", "opencode模板"]
}
```

**第二步：确认删除**

```
DELETE /api/settings/sandbox-providers/{provider_id}?confirm=true
```

- 权限：system scope → 直接 403；user scope → 本人 only
- 不传 confirm=true → 返回 409 + 待删除资源清单（同 delete-preview 响应）
- 传 confirm=true → 执行删除：
  1. 遍历 cloud_sandboxes 表，按 provider_id 查 state=running 的沙箱 → 逐个 kill
  2. 级联删除关联的 sandbox_templates 和 engine_template_binding（FK ON DELETE CASCADE）
  3. 删除 sandbox_provider_config 行
  4. **v3：不删除 runtime_credentials**——Provider 不持有 credential，cloud_worker 的 system credential 不受 Provider 删除影响
- 响应：`{"message": "Provider 已删除", "killed_sandboxes": 1, "deleted_templates": 2}`

#### 3.1.5 启停 Provider

```
PUT /api/settings/sandbox-providers/{provider_id}/toggle
```

- 权限：system scope → 直接 403；user scope → 本人 only
- 请求体：`{"enabled": true}`

#### 3.1.6 健康检查

```
POST /api/settings/sandbox-providers/{provider_id}/health
```

- 权限：登录用户（可查看自己 + 系统）
- 服务端用 Provider 配置创建临时 Provider 实例，调 health check
- 响应：`{"healthy": true, "reason": ""}`

### 3.2 模板 CRUD API

#### 3.2.1 列出模板

```
GET /api/settings/sandbox-templates?provider_id={id}
```

- 权限：登录用户
- 不传 provider_id → 返回系统 + 当前用户可见的全部模板
- 传 provider_id → 返回该 Provider 下的模板
- 响应同现有格式，增加 provider_id / provider_name / platform 字段

#### 3.2.2 新增模板

```
POST /api/settings/sandbox-templates
```

- 权限：system Provider → 直接 403；user Provider → 本人 only
- 请求体：

```json
{
  "provider_id": 3,
  "name": "hermes模板",
  "template_id": "tpl-xxx",
  "alias": "",
  "description": "",
  "preinstalled": "{}",
  "disk_size_mb": 0,
  "enabled": 1
}
```

- 不接收 engines 参数——引擎绑定通过 bind-engine / unbind-engine API 独立操作
- 校验：provider_id 存在且属于当前用户（或 system）

#### 3.2.3 更新模板

```
PUT /api/settings/sandbox-templates/{tpl_id}
```

- 权限同 3.2.2
- 不接收 engines 参数

#### 3.2.4 删除模板

```
DELETE /api/settings/sandbox-templates/{tpl_id}
```

- 权限同 3.2.2
- 级联删除 engine_template_binding 关联行

#### 3.2.5 引擎-模板绑定

```
POST /api/settings/sandbox-templates/{tpl_id}/bind-engine
```

- 请求体：`{"engine_type": "hermes"}`
- 校验：该 Provider 下该引擎未绑定其他模板（R5 约束，UNIQUE(provider_id, engine_type) DB 级保证）
- 幂等：同一模板重复绑定同一引擎不算错误（INSERT OR IGNORE）

```
DELETE /api/settings/sandbox-templates/{tpl_id}/bind-engine/{engine_type}
```

- 解绑引擎

### 3.3 cloud_worker 同步 API 变更

#### 3.3.1 模板列表接口（现有）

```
GET /api/settings/sandbox-templates
```

cloud_worker 拉取时，服务端 JOIN sandbox_provider_config，只返回 enabled=1 的 Provider 下的模板。

返回数据增加 provider_id、platform、provider_name 字段（供 cloud_worker 构建 runtime_info）。**v3 精简**：不返回 provider_scope/provider_user_id/provider_repo_key/provider_credential_id——register API 从 runtime_info.provider_id 查 DB 取真值，不从模板 API 返回值取（避免双源）。

#### 3.3.2 Provider 配置下发接口（现有）

```
GET /api/runtime/provider-configs
```

改造：从按 `provider`（字符串）返回改为按 `provider_id` 返回：

```json
{
  "providers": [
    {
      "id": 1,
      "name": "系统CubeSandbox",
      "platform": "cubesandbox",
      "endpoint": "https://cube.example.com",
      "api_key": "sk-xxx",
      "extra_config": {...}
    },
    {
      "id": 3,
      "name": "我的CubeSandbox-1",
      "platform": "cubesandbox",
      "endpoint": "https://cube2.example.com",
      "api_key": "sk-yyy",
      "extra_config": {...}
    }
  ]
}
```

**注意**：返回结构从 `{provider_name: config_dict}` 变为 `{providers: [...]}`。cloud_worker 端需同步改造。

## 4. cloud_worker 改造

### 4.1 _expand_templates 改造

现有逻辑按 `provider` 字符串展开模板，改造后按 `provider_id` + `platform` 展开：

```
现有:
  template.provider = "cubesandbox"  →  runtime name = f"{engine_type}:cubesandbox"

改造后:
  template.provider_id = 3, platform = "cubesandbox"  →  runtime name = f"{engine_type}:cubesandbox:{provider_id}"
```

runtime_info JSON 含 provider_id（register API 从此查 Provider 配置解析 scope）：

```json
{
  "engine_type": "hermes",
  "platform": "cubesandbox",
  "provider_id": 3,
  "provider_name": "我的CubeSandbox-1",
  "template_id": "tpl-xxx",
  "template_name": "hermes模板",
  "preinstalled": "",
  "description": "",
  "disk_size_mb": 0
}
```

> **v3 精简**：runtime_info 只含 provider_id（register API 从此查 DB 解析 scope/user_id/repo_key）。不放入 provider_scope/provider_user_id——这些字段是 Provider 配置的投影，register API 直接查 DB 取真值，不从 runtime_info 取（避免双源）。

### 4.2 Runtime 注册隔离——credential 信任边界 vs runtime scope 隔离（v3 重设计）

> **v2 方案废弃**：v2 在 agent_runtimes 新增 provider_scope/provider_user_id/provider_repo_key 三个列，
> 与现有 scope/owner_id 形成双 scope 维度，过滤点需要 "优先读 provider_scope，为空 fallback 到 scope" 的逻辑，
> 职责不清。v3 彻底分离两个维度：credential 只管信任边界，agent_runtimes.scope 是 scope 唯一数据源。

**架构原则**：两个维度完全分离——

| 维度 | 载体 | 职责 | 校验点 |
|------|------|------|--------|
| 信任边界 | `runtime_credentials.id` ↔ `agent_runtimes.credential_id` | 这个 runtime 属于哪个 worker？心跳/probe 合法吗？ | heartbeat `WHERE credential_id=?`、claim endpoint `WHERE id=? AND credential_id=?` |
| 任务分配隔离 | `agent_runtimes.scope` + `owner_id` + `owner_name` | 这个 runtime 能认领哪些任务？ | claim_by_tag scope 过滤、_member_scope 档位、_check_pool_engines_reachable scope 匹配 |

credential 不携带 scope 隔离信息（scope 仍保留在 runtime_credentials 上，仅为 local worker 凭据兼容，过滤点不再读它）。scope 不影响信任边界（scope 变化不影响 credential_id 匹配）。

#### v2 方案为什么不清

v2 新增 provider_scope 列后，agent_runtimes 同时有 `scope`（从 credential 继承，cloud_worker 恒为 global）和 `provider_scope`（从 Provider 配置注入）。过滤点需要：

```python
# v2 过滤逻辑（废弃）
_ps = m.get("provider_scope") or ""
if _ps:
    if _ps == "system": return "global"
    if _ps == "user": ...
else:
    # fallback 到 scope（local worker 兼容）
    if m.get("scope") == "repo": ...
```

问题：
1. 两个 scope 字段做同一件事——过滤点需要判断读哪个
2. cloud_worker 的 `scope` 列恒为 global（从 credential 复制），但实际隔离靠 `provider_scope`——`scope` 列成了无用冗余
3. fallback 逻辑隐含假设：provider_scope 为空 = local worker。但 cloud_worker 注册失败的 runtime 也可能 provider_scope 为空——误判为 local

#### v3 方案：agent_runtimes 是 scope 唯一数据源

**核心变更**：agent_runtimes.scope 不再从 credential 继承，而是 per-runtime 真实 scope。所有过滤点直接读 agent_runtimes，不再 JOIN runtime_credentials 取 scope。

#### 数据模型

agent_runtimes 新增一列（db_setup.py _ensure_column 迁移）：

```sql
ALTER TABLE agent_runtimes ADD COLUMN owner_name VARCHAR(255) DEFAULT '';
```

| 字段 | v2（废弃） | v3 |
|------|-----------|-----|
| scope | 从 credential 继承（cloud_worker 恒为 global） | **per-runtime 真实 scope**（global/user/repo） |
| owner_id | 从 credential 继承（cloud_worker 恒为空） | **per-runtime 真实 owner_id**（user_id / repo_key） |
| owner_name | 无（过滤点 JOIN rc.owner_name） | **per-runtime 真实 owner_name**（新增列，消除 JOIN） |
| repo_key | 从 credential 继承（cred_repo_key） | **不写入**（v3：repo scope 的 owner_id 已存 repo_key 值，repo_key 列是死字段，不再写入） |
| provider_scope | 新增列（system/user） | **不需要** |
| provider_user_id | 新增列 | **不需要** |
| provider_repo_key | 新增列 | **不需要** |

runtime_credentials 保留 scope/owner_id/owner_name/repo_key 列（local worker 凭据兼容），但过滤点不再读它们。

#### Provider 配置 → runtime scope 映射

sandbox_provider_config 的 scope/user_id/repo_key 映射到 agent_runtimes 的 scope/owner_id/owner_name：

| Provider 配置 | ar.scope | ar.owner_id | ar.owner_name |
|--------------|----------|-------------|---------------|
| scope=system | global | '' | '' |
| scope=user, repo_key='' | user | str(user_id) | username（查 user_accounts） |
| scope=user, repo_key='owner/repo' | repo | 'owner/repo' | username（查 user_accounts） |

映射在 register API 中完成（服务端解析，不从请求体取 scope——防篡改）。

#### register 接口改造

register 接口从 runtime_info.provider_id 查 Provider 配置，写入 per-runtime scope/owner_id/owner_name。credential_id 恒等于 cred.id（cloud_worker 的 system credential，信任边界不变）：

```python
for rt in body.runtimes:
    name = (rt.get("name") or "").strip().lower()
    # ---- v3: per-runtime scope 从 Provider 配置解析 ----
    _ri = json.loads(rt.get("runtime_info") or "{}")
    _provider_id = _ri.get("provider_id", 0)
    if _provider_id and cred.source == "system":
        # cloud_worker runtime —— 从 Provider 配置解析 scope
        c.execute(
            "SELECT scope, user_id, repo_key FROM sandbox_provider_config WHERE id=?",
            (_provider_id,))
        _prow = c.fetchone()
        if _prow:
            _p_scope = _prow[0] or "system"
            _p_user_id = _prow[1] or 0
            _p_repo_key = _prow[2] or ""
            if _p_scope == "system":
                _rt_scope = "global"
                _rt_owner_id = ""
                _rt_owner_name = ""
            elif _p_scope == "user" and _p_repo_key:
                _rt_scope = "repo"
                _rt_owner_id = _p_repo_key
                _rt_owner_name = _resolve_username(c, _p_user_id)
            elif _p_scope == "user":
                _rt_scope = "user"
                _rt_owner_id = str(_p_user_id)
                _rt_owner_name = _resolve_username(c, _p_user_id)
            else:
                _rt_scope = cred_scope
                _rt_owner_id = owner_id
                _rt_owner_name = cred_owner_name
        else:
            _rt_scope = cred_scope
            _rt_owner_id = owner_id
            _rt_owner_name = cred_owner_name
    else:
        # local worker —— 从 credential 继承（as now）
        _rt_scope = cred_scope
        _rt_owner_id = owner_id
        _rt_owner_name = cred_owner_name

    # SELECT/INSERT/UPDATE 用 _rt_scope, _rt_owner_id, _rt_owner_name
    c.execute(
        "SELECT id FROM agent_runtimes WHERE credential_id=? AND lower(name)=? AND scope=? AND owner_id=? LIMIT 1",
        (cred.id, name, _rt_scope, _rt_owner_id))
    # ... INSERT/UPDATE 写入 _rt_scope, _rt_owner_id, _rt_owner_name
```

**安全模型**：
- `cred.source == "system"`（cloud_worker system credential）：信任 Provider 配置解析的 scope（Provider 配置在服务端 DB，非请求体可篡改）
- `cred.source != "system"`（local worker credential）：scope 从 credential 继承（local worker 不能自己传 scope）
- scope 不从请求体取——从 Provider 配置（DB）解析，防篡改

#### sandbox-templates 接口返回字段增强

cloud_worker 拉取模板时，服务端 JOIN sandbox_provider_config 返回 Provider 元数据（同 v2）：

```json
{
  "templates": [
    {
      "id": 20,
      "name": "hermes-acp-预装",
      "provider_id": 1,
      "platform": "cubesandbox",
      "provider_name": "系统CubeSandbox",
      "template_id": "tpl-xxx",
      "engines": "[\"hermes\"]",
      ...
    }
  ]
}
```

cloud_worker `_expand_templates` 从这些字段构建 runtime_info（含 provider_id），register API 从 runtime_info.provider_id 查 Provider 配置解析 scope——cloud_worker 不传 scope，只传 provider_id。

#### 三道过滤改造——从 JOIN rc 改为直接读 ar

**1. _member_scope 改造**（engine_select.py）：

现有逻辑已读 `m.get("scope")`（来自 agent_runtimes），但 `m` 的构建 JOIN 了 rc 取 owner_name。改造后直接读 ar：

```python
def _member_scope(m: dict, repo: str, user_id: int = 0) -> str:
    """成员档位：repo（仓库级专属）/ user（用户级专属）/ global（远端全局）/ local（本机直连）。"""
    cid = m.get("credential_id") or 0
    if cid == 0:
        return "local"
    _scope = m.get("scope") or "global"
    if _scope == "repo":
        from src.repo_key import from_repo_key
        oid = from_repo_key(m.get("owner_id") or "")
        if oid and oid == repo:
            return "repo"
        return "excluded"
    if _scope == "user":
        _puid = m.get("owner_id") or ""
        if _puid and str(_puid) == str(user_id):
            return "user"
        return "excluded"
    return "global"
```

变更：新增 "user" 档位（现有只有 repo/global/local）+ "excluded" 返回值（不匹配时排除）+ user_id 参数。

pick_engine scopes 顺序加入 "user" 档：

```python
for scope in cfg.get("scopes") or ["repo", "global", "user", "local"]:
    cand = _apply_filters([m for m in members if _member_scope(m, repo, user_id) == scope], cfg)
```

**2. claim_by_tag 改造**（service.py）：

现有 JOIN runtime_credentials 取 scope，改为直接读 agent_runtimes：

```python
# v3: 直接读 ar.scope/owner_id/owner_name，不 JOIN rc
c.execute(
    "SELECT ec.tag, ar.scope, ar.owner_id, ar.owner_name "
    "FROM engine_capabilities ec "
    "JOIN agent_runtimes ar ON ar.id = ec.runtime_id "
    "WHERE ec.runtime_id=? AND ec.enabled=1 AND (ec.tag=? OR ec.tag='all')",
    (runtime_id, tag)
)
row = c.fetchone()
if not row:
    return None
scope = row[1] or "global"
owner_id = row[2] or ""
owner_name = row[3] or ""
```

scope 过滤逻辑不变（global/repo/user 三档），但数据来源从 rc 改为 ar。user scope 不再需要查 user_accounts 解析 owner_name→user_id（owner_id 已是 user_id）：

```python
elif scope == "user":
    # v3: owner_id 已是 user_id（register 时从 Provider 配置写入）
    params.append(int(owner_id) if owner_id else 0)
    sql = (
        "SELECT t.id FROM agent_tasks t "
        f"WHERE {_tag_match} AND t.status='queued' AND t.user_id=? "
        ...
    )
```

**3. _check_pool_engines_reachable 改造**（service.py）：

现有 JOIN rc 取 scope，改为直接读 ar：

```python
# v3: 直接读 ar.scope/owner_id/owner_name
c.execute(
    "SELECT scope, owner_id, owner_name FROM agent_runtimes WHERE id=?",
    (eid,))
sr = c.fetchone()
if sr:
    _scope = sr[0] or "global"
    _owner = sr[1] or ""
    _owner_name = sr[2] or ""
    if _scope == "repo" and _owner:
        _owner_norm = (from_repo_key(_owner) or "").strip()
        if _repo_norm and _owner_norm != _repo_norm:
            continue
    elif _scope == "user" and task_user_id > 0:
        # v3: owner_id 已是 user_id
        _engine_uid = int(_owner) if _owner else 0
        if _engine_uid != task_user_id:
            continue
```

**4. query_engines 改造**（engine_select.py）：

现有 WHERE rc.scope='repo' 改为 WHERE ar.scope='repo'，保留 JOIN rc 仅取 device_name + status='active'（展示用）：

```sql
-- v3
SELECT r.id, r.name, r.status, r.healthy, r.installed, r.credential_id, r.last_seen_at,
       r.scope, r.owner_id, r.owner_name,
       COALESCE(rc.device_name, ''), rc.status
FROM agent_runtimes r
LEFT JOIN runtime_credentials rc ON rc.id = r.credential_id
WHERE r.credential_id > 0 AND r.scope = 'repo' AND (r.owner_id = ? OR r.owner_id = ?)
  AND (rc.status = 'active' OR rc.status IS NULL)
ORDER BY r.id
```

变更：scope/owner_id/owner_name 从 ar 读取（不再从 rc），保留 JOIN rc 仅用于 device_name 展示 + credential 状态过滤。

**5. _engine_scope_info 改造**（engine_select.py）：

现有同时读 r.scope 和 rc.scope（COALESCE fallback），v3 简化为只读 ar：

```python
def _engine_scope_info(runtime_id: int) -> tuple[str, str]:
    """返回引擎行档位 (scope, owner_id)：本机直连（credential_id=0）→ local；失败返回 ('global', '')。"""
    db, lock = _db()
    with lock:
        try:
            c = db.cursor()
            c.execute(
                "SELECT scope, owner_id, credential_id FROM agent_runtimes WHERE id=?",
                (runtime_id_id,))
            row = c.fetchone()
        except Exception:
            return "global", ""
    if not row:
        return "global", ""
    if (row[2] or 0) == 0:
        return "local", ""
    return row[0] or "global", row[1] or ""
```

#### 池广播隔离

用户级/仓库级 Provider 的 runtime 注册时设 `agent_tags = []`（空标签，不参与池广播）。系统 Provider 的 runtime 设 `agent_tags = ["all"]`（参与全部池广播）。

register 接口接收 body 中每个 runtime 的 `agent_tags` 字段，cloud_worker `_expand_templates` 从模板 API 返回的 Provider 配置映射（通过 provider_id 关联，或模板 API 返回 scope 字段）：
- Provider scope=system → agent_tags=["all"]
- Provider scope=user → agent_tags=[]

#### 隔离效果

| 过滤点 | 系统 Provider runtime | 用户级 Provider runtime | 仓库级 Provider runtime |
|--------|----------------------|------------------------|------------------------|
| credential_id | cred.id（心跳/probe 正常） | cred.id（心跳/probe 正常） | cred.id（心跳/probe 正常） |
| ar.scope | global | user | repo |
| ar.owner_id | '' | str(user_id) | repo_key |
| ar.owner_name | '' | username | username |
| pick_engine | global 档 | user 档（仅该用户） | repo 档（仅该仓库） |
| claim_by_tag | 不按 scope 过滤 | WHERE t.user_id=? | WHERE t.repo_key=? |
| _check_pool_engines_reachable | 不按 scope 过滤 | 匹配 user_id | 匹配 repo_key |
| pool 广播 | agent_tags=["all"] | agent_tags=[] | agent_tags=[] |

#### 完整隔离链路

```
用户 A 创建用户级 Provider
  → sandbox_provider_config(scope=user, user_id=A, repo_key='')
  → cloud_worker 拉取模板 → _expand_templates → runtime_info 含 provider_id
  → register API 从 provider_id 查 Provider 配置 → ar.scope=user, ar.owner_id=str(A), ar.owner_name='A的 username'
  → 心跳/probe 正常（credential_id=cred.id 匹配）
  → pick_engine：_member_scope 返回 "user" 档（仅 user_id==A）
  → claim_by_tag：WHERE t.user_id=A 的任务
  → pool 广播：agent_tags=[] → 不参与
  → 用户 B 的任务永远不会被分配到该 runtime

用户 A 创建仓库级 Provider（指定 repo_key=owner/repo）
  → sandbox_provider_config(scope=user, user_id=A, repo_key='owner/repo')
  → cloud_worker 注册 → ar.scope=repo, ar.owner_id='owner/repo', ar.owner_name='A的 username'
  → pick_engine：_member_scope 返回 "repo" 档（仅 repo 匹配）
  → claim_by_tag：WHERE t.repo_key='owner/repo'
  → pool 广播：agent_tags=[] → 不参与
  → 其他仓库的任务永远不会被分配到该 runtime
```

**设计原则**：credential 管信任边界（credential_id 不变），agent_runtimes.scope 管任务分配隔离（per-runtime 真实 scope）。heartbeat/probe/claim 的 credential_id 校验不变。三道过滤直接读 agent_runtimes，不再 JOIN runtime_credentials 取 scope。单一 scope 字段，无双维度，无 fallback。

### 4.3 Provider 配置缓存改造

现有 `_provider_configs` 是 `{provider_name: config_dict}`，改造为 `{provider_id: config_dict}`：

```python
# 现有
_provider_configs = {
    "cubesandbox": {"api_url": "...", "api_key": "...", ...},
    "devbox": {"api_url": "...", "api_key": "...", ...},
}

# 改造后
_provider_configs = {
    1: {"platform": "cubesandbox", "name": "系统CubeSandbox", "api_url": "...", "api_key": "...", ...},
    3: {"platform": "cubesandbox", "name": "我的CubeSandbox-1", "api_url": "...", "api_key": "...", ...},
}
```

### 4.4 SandboxManager 改造

`_select_provider_from_context` 改造：从 `ctx.sandbox_provider`（字符串 provider 名）改为 `ctx.provider_id`（整数 ID）：

```
现有:
  ctx.sandbox_provider = "cubesandbox"
  → registry["cubesandbox"](config=_provider_configs["cubesandbox"])

改造后:
  ctx.provider_id = 3
  → platform = _provider_configs[3]["platform"]  # "cubesandbox"
  → registry[platform](config=_provider_configs[3])
```

registry key 保持 platform 类型名（cubesandbox/devbox），config 从 `provider_id` 索引的 dict 中取。

### 4.5 SandboxContext 改造

```
现有:
  sandbox_provider: str  # "cubesandbox" / "devbox"
  template_id: str       # 平台侧模板 ID

改造后:
  provider_id: int        # Provider 实例 ID
  platform: str           # 平台类型（从 provider_configs 查）
  template_id: str        # 平台侧模板 ID
```

build_context 从 runtime_info 中提取 `provider_id`（替代现有 `sandbox_provider`）。

### 4.6 定时同步流程

cloud_worker 心跳周期（60s）已有 `_refresh_templates` + `_refresh_provider_configs`。改造后流程：

```
1. GET /api/runtime/provider-configs → 更新 _provider_configs（按 provider_id 索引）
2. GET /api/settings/sandbox-templates → 更新模板列表（含 provider_id）
3. _expand_templates → 按 provider_id 展开为 runtime 条目
4. 对比 runtime_info hash → 增量注册/下线
5. Provider 配置变更 → set_provider_configs + invalidate_provider_cache
```

### 4.7 Runtime name 格式变更与存量清理

**现有格式**：`f"{engine_type}:{provider}"`（如 `hermes:cubesandbox`）
**新格式**：`f"{engine_type}:{platform}:{provider_id}"`（如 `hermes:cubesandbox:3`）

register 用 `credential_id + lower(name) + scope + owner_id` 做唯一匹配。name 格式变更后，旧格式 runtime 不被新格式匹配 → 旧 runtime 变成孤儿（status=offline 永远残留，reap_stale_runtimes 只标 offline 不 DELETE）。

**当前数据状态**：DB 中没有 cloud 类型 runtime（cloud_worker 未运行），不需要清理存量。但需要在迁移时做防御性清理 + 改进 _refresh_templates 的孤儿清理逻辑。

**清理方案**：

1. 迁移脚本（M14）：删除存量 cloud 类型孤儿 runtime

```sql
DELETE FROM agent_runtimes WHERE runtime_type='cloud' AND name LIKE '%:%';
DELETE FROM engine_capabilities WHERE runtime_id NOT IN (SELECT id FROM agent_runtimes);
```

2. _refresh_templates 首次从 DB 初始化 _last_runtimes——进程重启后 `_last_runtimes` 为空，无法对比删除。改为首次调用时从 DB 查同一 credential_id 的现有 cloud runtime 初始化 `_last_runtimes`，使首次 refresh 能检测到旧格式 name 的 runtime 并走 offline 注册。新增 `client.get_cloud_runtimes()` 接口。

3. register 接口 offline 注册时清理孤儿——注册 healthy=False 的 runtime 时，先 DELETE 同一 credential_id 下 name 不在新列表中的 cloud runtime，再注册新的：

```python
if not _healthy and _rt == "cloud":
    _new_names = [rt.get("name", "").lower() for rt in body.runtimes if rt.get("name")]
    if _new_names:
        placeholders = ",".join(["?"] * len(_new_names))
        c.execute(
            f"DELETE FROM agent_runtimes WHERE credential_id=? AND runtime_type='cloud' "
            f"AND lower(name) NOT IN ({placeholders})",
            (cred.id, *_new_names)
        )
        c.execute(
            "DELETE FROM engine_capabilities WHERE runtime_id NOT IN "
            "(SELECT id FROM agent_runtimes)"
        )
```

### 4.8 provider_name 多实例区分

**问题**：`CloudSandboxBase.provider_name` 硬编码为 "devbox" / "cubesandbox"，多实例后无法区分。cloud_sandboxes 表的 provider 列、日志前缀、SandboxContext.name 全部是平台类型名，无法区分是哪个 Provider 实例。

**分析**：provider_name 实际承担了两个职责：
1. 平台类型标识（cubesandbox / devbox）——用于 registry 查找、引擎策略分派
2. 实例可读标识——用于日志、DB、审计区分

这两个职责应该拆分：

| 字段 | 职责 | 来源 | 示例 |
|------|------|------|------|
| `platform` | 平台类型 | Provider 配置中的 platform 字段 | "cubesandbox" |
| `provider_name` | 实例可读名 | Provider 配置中的 name 字段 | "我的CubeSandbox-1" |

**改造方案**：

1. `CloudSandboxBase` 构造时接收 `provider_name`（实例名）和 `platform`（平台类型）两个参数：

```python
# cloud_base.py CloudSandboxBase.__init__
def __init__(self, *, issue_id, repo_key, provider_name: str, platform: str = "", ...):
    self.provider_name = provider_name  # 实例名（如 "我的CubeSandbox-1"）
    self.platform = platform or provider_name  # 平台类型（如 "cubesandbox"）
```

2. 子类构造时从 config dict 中取实例名：

```python
# cubesandbox.py CubeSandboxInstance.__init__
super().__init__(
    issue_id=issue_id, repo_key=repo_key,
    provider_name=config.get("name", "cubesandbox"),  # 实例名
    platform="cubesandbox",  # 平台类型固定
    ...
)

# devbox.py DevBoxSandbox.__init__
super().__init__(
    issue_id=issue_id, repo_key=repo_key,
    provider_name=config.get("name", "devbox"),  # 实例名
    platform="devbox",  # 平台类型固定
    ...
)
```

3. 日志前缀用 `provider_name`（实例名），SandboxContext.name 也用 `provider_name`：

```python
# cloud_base.py _make_context
return SandboxContext(
    name=self.provider_name,  # 实例名
    ...
)
```

4. cloud_sandboxes 表的 provider 列存 `provider_name`（实例名），新增 `provider_id` 列（已在 §7.5 规划）用于精确关联。kill 流程按 provider_id 查沙箱，不按 provider 字符串。

5. `agent_runtimes.provider` 列存 `provider_name`（实例名），claim 时 JOIN 返回为 `task["sandbox_provider"]`。cloud_worker 的 `_ctx.sandbox_provider` 就是实例名，report_sandbox_status 时传实例名。

6. `CloudSandboxBase._report_state` 中 `provider=self.provider_name`——传实例名。

**设计原则**：平台类型（platform）和实例名（provider_name）分离。platform 用于 registry 查找和引擎策略分派（不变），provider_name 用于日志/DB/审计区分（从 config 注入，不硬编码）。子类构造时从 config dict 取实例名，不硬编码——依赖注入。

### 4.9 SSL 证书处理

**问题**：CubeSandbox SDK 的 `Config` 类没有 SSL 证书字段，SDK 用 `requests.Session()` 发 HTTP 请求不设 verify，依赖进程级 `os.environ["REQUESTS_CA_BUNDLE"]`。多 cubesandbox Provider 各有不同 ssl_cert_content 时，进程级环境变量互相覆盖。

**约束**：不能改 SDK（第三方库）。

**方案**：per-provider 证书落盘 + 封装 `_post_create_hook` 钩子方法。

1. cloud_worker `_materialize_ssl_cert` 改为按 provider_id 落盘：

```
/var/lib/grape/cloud-worker/cube-rootCA-{provider_id}.pem
```

2. `CubeSandboxProvider.__init__` 不再设 `os.environ["REQUESTS_CA_BUNDLE"]`（删除进程级环境变量设置）。

3. `CloudSandboxBase` 新增 `_post_create_hook` 钩子方法（默认空实现），在 `__enter__` 中 `_do_create()` 之后、`_init_sandbox()` 之前调用。子类 override 此方法做平台特定的初始化（如 SSL 证书）：

```python
# cloud_base.py CloudSandboxBase
def _post_create_hook(self) -> None:
    """SDK 对象创建后的平台特定初始化钩子。子类 override。"""
    pass

def __enter__(self) -> "CloudSandboxBase":
    self._sandbox_id = self._do_create()
    self._report_state(state="creating")
    self._post_create_hook()  # ← 新增钩子
    try:
        self._init_sandbox()
    ...
```

4. `CubeSandboxInstance` override `_post_create_hook`，封装 SSL 证书设置到一个方法中（不散落在 `_do_create` / `_do_connect`）：

```python
# cubesandbox.py CubeSandboxInstance
def _post_create_hook(self) -> None:
    """SDK Sandbox 对象创建后，设置 per-provider SSL 证书。"""
    _ca_bundle = self._config.get("_ssl_cert_path", "")
    if _ca_bundle and self._sandbox:
        self._sandbox._session.verify = _ca_bundle
```

`_do_connect` 中也调 `_post_create_hook`（connect 后 session 同样需要设置证书）。

5. `CubeSandboxProvider._do_health_check` 改为直接用 `requests.get` 带 per-provider verify（health 是 classmethod，无法 patch session）：

```python
def _do_health_check(self) -> bool:
    import requests
    _ca_bundle = self._config.get("_ssl_cert_path", "")
    try:
        resp = requests.get(
            f"{self._config.get('api_url', '')}/health",
            headers={"X-API-Key": self._config.get("api_key", "")},
            verify=_ca_bundle or True,
            timeout=3,
        )
        return resp.status_code == 200
    except Exception:
        return False
```

6. provider_configs 下发时增加 `_ssl_cert_path` 字段，值为落盘的证书路径。

**设计原则**：SSL 证书设置封装在 `_post_create_hook` 钩子方法中，不散落在 `_do_create` / `_do_connect`。`CloudSandboxBase` 定义钩子接口，`CubeSandboxInstance` 实现具体逻辑——面向接口，高内聚。`sb._session.verify` 的 SDK 私有属性访问局限在 `_post_create_hook` 一个方法内，SDK 升级时只改这一处。

## 5. 引擎模板路由

### 5.1 路由流程

任务 claim 时，`agent_runtimes.runtime_info` JSON 已含 `provider_id` + `template_id`。build_context 提取后传入 SandboxManager.prepare()：

```
claim → task.runtime_info = {"engine_type": "hermes", "provider_id": 3, "template_id": "tpl-xxx"}
  → build_context → ctx.provider_id = 3, ctx.template_id = "tpl-xxx"
    → _select_provider_from_context
      → platform = _provider_configs[3]["platform"]  # "cubesandbox"
      → factory = registry["cubesandbox"]
      → provider = factory(config=_provider_configs[3])
      → provider.is_configured() + provider.health()
      → return (provider, ctx.template_id)
```

### 5.2 多 Provider 优先级

cloud_worker 注册多个同类 Provider 的 runtime 时，按以下排序注册：

1. cubesandbox 优先于 devbox（同引擎）
2. system scope 优先于 user scope
3. provider_id 升序

`_expand_templates` 排序逻辑：

```python
sorted(templates, key=lambda t: (
    0 if t.get("platform") == "cubesandbox" else 1,  # 平台优先级
    0 if t.get("scope") == "system" else 1,           # scope 优先级（系统优先）
    t.get("provider_id", 0),                          # ID 升序
    t.get("template_id", ""),                         # 模板 ID 兜底
))
```

### 5.3 用户 Provider 引擎选择

`pick_engine` 选引擎时，按现有 scope 机制天然过滤：

| Provider 类型 | runtime scope | runtime owner | 对当前用户可见？ | 档位 |
|--------------|---------------|---------------|----------------|------|
| 系统 Provider | global | 空 | 是 | global |
| 用户级 Provider | user | 用户名 | 仅该用户 | user |
| 仓库级 Provider | repo | repo_key | 仅该仓库 | repo |

用户发起任务时 `pick_engine` 按现有 scopes 顺序选引擎：
1. 场景配置的 engine_runtime_ids（精确指定）
2. 仓库级引擎（scope=repo）——仓库级 Provider 的 runtime
3. 全局引擎（scope=global）——系统 Provider 的 runtime
4. 用户级引擎（scope=user）——用户级 Provider 的 runtime
5. 本地引擎（scope=local）

系统 Provider 优先于用户 Provider，用户 Provider 是补充选项。其他用户的 Provider runtime 因 scope=user + owner_name 不匹配，永不被选中。

## 6. 权限模型

系统没有 admin 角色（user_accounts.is_admin 全部为 0）。系统 Provider 是系统预置到数据库中的（初始化写入，scope=system），普通用户只能查看不能修改。

| 操作 | system Provider | user Provider |
|------|----------------|---------------|
| 查看 | 登录用户 | 本人 |
| 新增 | 不允许（系统预置） | 登录用户（上限 5） |
| 更新 | 不允许 | 本人 |
| 删除 | 不允许 | 本人（确认后先 kill 沙箱再删） |
| 启停 | 不允许 | 本人 |
| 健康检查 | 登录用户 | 本人 |
| 管理模板 | 不允许 | 本人 |

校验逻辑：
- system scope + 写操作 → 直接 403（不依赖 admin 角色）
- user scope + user_id != 当前用户 → 403

### 6.1 系统 Provider 预置

系统 Provider 在 db_setup.py 初始化流程中预置。改造现有 `_migrate_provider_configs_to_db()` + `_seed_sandbox_templates()` 两个函数。

**现有预置链路**（db_setup.py init_db → SQLite/MySQL 分支）：

```
_migrate_provider_configs_to_db()  → 从 configs/*.json 读配置 → INSERT sandbox_provider_config
_seed_sandbox_templates()          → 硬编码 3 个模板 → INSERT sandbox_templates
_seed_cloud_worker_key()            → 生成 cloud_worker system credential → runtime_credentials + key 文件
```

**改造后预置链路**：

```
1. _seed_cloud_worker_key()
   → 生成 cloud_worker system credential（scope=global）→ 拿到 credential_id
   → **v3：credential_id 不写入 Provider**——credential 只管信任边界，Provider 不持有 credential

2. _migrate_provider_configs_to_db() 改造：
   → 从 configs/cubesandbox-config.json 读配置
   → INSERT sandbox_provider_config (name="cubesandbox", platform="cubesandbox", scope="system", user_id=0)
   → 从 configs/devbox-config.json 读配置（如存在）
   → INSERT sandbox_provider_config (name="devbox", platform="devbox", scope="system", user_id=0)
   → 返回 {platform: provider_id} 映射
   → **v3：不写 credential_id**——Provider 不持有 credential

3. _seed_sandbox_templates() 改造：
   → 硬编码 3 个模板
   → INSERT sandbox_templates (provider_id=系统cubesandbox Provider的id, 不写 engines)
   → INSERT engine_template_binding (provider_id, engine_type, template_id)

执行顺序（在 _migrate_tables 之后，独立 try 块）：
_seed_cloud_worker_key() → _migrate_provider_configs_to_db() → _seed_sandbox_templates()
```

**幂等保证**：
- sandbox_provider_config：按 platform + scope=system 查询，已存在则 UPDATE 不 INSERT
- sandbox_templates：按 name + provider_id 查询，已存在则跳过
- engine_template_binding：INSERT OR IGNORE（UNIQUE 约束保证幂等）

**预置数据来源**：

| 数据 | 来源 | 说明 |
|------|------|------|
| cubesandbox Provider 配置 | configs/cubesandbox-config.json | endpoint/api_key/extra_config |
| devbox Provider 配置 | configs/devbox-config.json | endpoint/api_key/extra_config |
| 系统 credential | _seed_cloud_worker_key 生成 | cloud_worker 的 system credential |
| 3 个系统模板 | _seed_sandbox_templates 硬编码 | hermes-acp-预装 / devkit-预装 / codearts-预装 |
| 引擎绑定 | _seed_sandbox_templates 中硬编码 | hermes→tpl1, opencode→tpl2, codearts→tpl3 |

## 7. 改动点清单

### 7.1 DB 层

| 文件 | 改动 |
|------|------|
| `src/api/db_setup.py` | sandbox_provider_config 表结构改造（**v3：不加 credential_id 列**——Provider 不持有 credential）+ sandbox_templates 加 provider_id + 新增 engine_template_binding 表 + cloud_sandboxes 加 provider_id 列 + **agent_runtimes 加 owner_name 列**（v3：per-runtime scope 唯一数据源，过滤点不再 JOIN rc）+ 新增配置项 sandbox_provider_max_per_user（默认 5）+ 存量迁移 + _migrate_provider_configs_to_db 改造（写 scope=system/user_id=0，不写 credential_id）+ _seed_sandbox_templates 改造（写 provider_id+binding 不写 engines）+ M14 清理 cloud 孤儿 runtime |
| `src/agent/sandbox/schema.py` | 同步 DDL |
| `src/agent/sandbox/repository.py` | query_sandbox_template 改为按 provider_id 查 |

### 7.2 API 层

| 文件 | 改动 |
|------|------|
| `src/api/routes/settings.py` | Provider CRUD 重写（**v3：不生成 runtime credential**）+ 模板 CRUD 加 provider_id 关联 + 引擎绑定 API + 权限校验（system scope 写操作直接 403）|
| `src/api/routes/runtime.py` | register 接口从 runtime_info.provider_id 查 Provider 配置解析 per-runtime scope/owner_id/owner_name（cred.source=system 时）+ 新增 owner_name 列写入 + agent_tags 从 body 取（系统 Provider=["all"]，用户 Provider=[]）+ offline 注册时清理同 credential_id 下不在新列表中的 cloud 孤儿 runtime + provider-configs 接口改为按 provider_id 返回 + **§12: runtime_task_env 端点增加 credential_id 匹配校验（越权防护）+ 增加 user_gitcode_token 返回（按 task.user_id 查 get_user_gitcode_token）** |
| `src/api/routes/sandbox_admin.py` | 无改动（沙箱审计/白名单/路径不受影响）|

### 7.3 cloud_worker 层

| 文件 | 改动 |
|------|------|
| `src/runtime/cloud_worker.py` | _expand_templates 按 provider_id 展开 + runtime_info 含 provider_id（register API 从此查 Provider 配置解析 scope）+ runtime 注册条目携带 agent_tags（系统 Provider=["all"]，用户 Provider=[]）+ _provider_configs 按 provider_id 索引 + _materialize_ssl_cert 按 provider_id 落盘 + _refresh_templates 首次从 DB 初始化 _last_runtimes 检测孤儿 + **§12: _cloud_execute_task 新增 chat/devops 互斥注入 + chat 任务从 /env API 获取 USER_GITCODE_TOKEN + chat 不注入 BOT_GITCODE_TOKEN** |
| `src/runtime/client.py` | get_provider_configs 返回结构适配 + 新增 get_cloud_runtimes 查现有 cloud runtime |
| `src/runtime/engine_select.py` | **v3 改动**：_member_scope 新增 user 档 + excluded 返回值 + user_id 参数 + pick_engine scopes 顺序加 "user" 档 + _engines_by_ids / query_engines / _engine_scope_info 从 JOIN rc 改为直接读 ar（scope/owner_id/owner_name 从 ar 读取，JOIN rc 仅取 device_name + status='active' 展示用） |
| `src/runtime/service.py` | **v3 改动**：claim_by_tag 从 JOIN rc 取 scope 改为直接读 ar（scope/owner_id/owner_name 从 ar 读取，不 JOIN rc）+ user scope 的 owner_id 已是 user_id（不再查 user_accounts 解析 owner_name→user_id）+ _check_pool_engines_reachable 同理从 JOIN rc 改为直接读 ar |

### 7.4 沙箱管理层

| 文件 | 改动 |
|------|------|
| `src/agent/sandbox/sandbox_context.py` | SandboxContext 加 provider_id 字段 + build_context 提取 provider_id |
| `src/agent/sandbox/manager.py` | _select_provider_from_context 改用 provider_id 索引 provider_configs + registry key 从 provider 名改为 platform 类型 |
| `src/agent/sandbox/backends/__init__.py` | registry key 保持 platform 类型名（cubesandbox/devbox），不按实例区分 |
| `src/agent/sandbox/backends/devbox.py` | DevBoxSandbox.__init__ provider_name 从 config.get("name") 取 + platform="devbox" 固定 |
| `src/agent/sandbox/backends/cubesandbox.py` | CubeSandboxProvider.__init__ 删除进程级 REQUESTS_CA_BUNDLE + _do_health_check 改为手动 requests.get 带 per-provider verify + CubeSandboxInstance override _post_create_hook 设置 SSL 证书 + _do_connect 后调 _post_create_hook + __init__ provider_name 从 config.get("name") 取 + platform="cubesandbox" 固定 |
| `src/agent/sandbox/backends/cloud_base.py` | CloudSandboxBase 新增 `platform` 属性 + `provider_name` 从 config 注入不硬编码 + 新增 `_post_create_hook` 钩子方法 + `__enter__` 中 `_do_create` 后调用 |

### 7.5 不受影响的模块

- `engine_sandbox_toggle` 表及查询接口——引擎维度开关不变
- `bwrap` 沙箱——本地沙箱不涉及 Provider
- `cloud_sandboxes` 表——新增 provider_id INT 列（关联 sandbox_provider_config.id），保留旧 provider VARCHAR 列兼容（kill 流程按 provider_id 查正在运行的沙箱；前端筛选从 provider 字符串改为 provider_id）
- `engine_sandbox_config` / `engine_sandbox_whitelist` / `engine_sandbox_paths`——不受影响
- LLM 凭据 / OBS / webhook 链路——不受影响
- `src/runtime/agent.py`（本地 worker）——不涉及云沙箱，不需要 provider_configs 注入。本地 worker 只注册 local 类型 runtime（provider=空），claim 返回 sandbox_provider=空，_select_provider_from_context 直接走 bwrap 路径，不进入云沙箱分支。_provider_configs 为空 dict 不影响本地 worker。v3 scope 变更不影响 local worker——cred.source != "system" 时 scope 仍从 credential 继承（现有逻辑不变）
- `sandbox_templates.created_by`/`updated_by`——存用户名字符串（和现有约定一致），user_id 列存整数 user_id（scope=user 时关联用户）

## 8. 前端影响

### 8.1 页面结构调整

现有 UI：双 tab（沙箱列表 + 系统模板），tab 之间无关联，provider 筛选硬编码 `['cubesandbox', 'devbox']`。

改造为双 Tab + Provider 详情页：

```
云沙箱页面
├── Tab 1: Provider 列表（表格 + 分页 + 搜索）
│   ├── 表格列：Name / Platform / Endpoint / Scope / Max Sandboxes / Enabled / 操作
│   ├── 系统 Provider：操作列只有"详情"按钮
│   ├── 用户 Provider：操作列有 详情/编辑/启停/健康检查/删除 按钮
│   ├── 新增 Provider 按钮（弹窗表单，按 platform 动态展示专属字段）
│   └── 搜索：按 name/endpoint 模糊匹配
│
├── Tab 2: 沙箱列表（表格 + 分页 + 搜索 + provider_id 筛选下拉）
│   ├── 表格列：Sandbox ID / Issue / Repo / Provider / State / Agent / Created By / Created At / Last Refresh
│   ├── provider 筛选下拉：从 GET /sandbox-providers 动态获取（不再硬编码）
│   └── 按 provider_id 筛选（从 Provider 详情跳转时自动带入）
│
└── Provider 详情页（从 Provider 列表点击"详情"进入，独立视图）
    ├── 返回按钮（返回 Provider 列表）
    ├── Provider 信息卡片（完整字段展示）
    │   ├── 通用：name / platform / endpoint / scope / user_id / max_sandboxes / enabled
    │   ├── CubeSandbox 专属：ssl_cert_content（脱敏显示"已配置"/"未配置"）
    │   └── DevBox 专属：gateway_url / gateway_ip / gateway_skip_tls_verify
    ├── 模板列表表格（完整列）：
    │   ├── 表格列：Name / Template ID / Alias / Description / Engines / Preinstalled / Disk Size / Status / Last Check / 操作
    │   ├── 用户 Provider：操作列有 编辑/删除/绑定引擎/解绑引擎 按钮，可新增模板
    │   └── 系统 Provider：只读
    └── 查看沙箱实例按钮 → 跳转沙箱列表 Tab，provider_id 自动带入
```

### 8.2 新增/编辑 Provider 弹窗（按 platform 动态切换专属字段）

```
通用字段（所有 platform）：
  - name（名称，创建后不可改）
  - platform（下拉：cubesandbox / devbox，创建后不可改）
  - endpoint（API URL）
  - api_key（密钥，编辑时留空保留旧值）
  - max_sandboxes（1-20，默认 10）
  - enabled（开关）
  - scope_level（下拉：用户级 / 仓库级）
    - 仓库级时：repo_key 自动从 currentRepo() 取，显示为只读标签
    - 用户级时：不显示 repo_key

CubeSandbox 专属字段（platform=cubesandbox 时显示）：
  - ssl_cert_content（textarea，粘贴 PEM 证书内容，可选）
  - 提示文字："将 CubeSandbox 平台的 CA 证书内容粘贴到此处"

DevBox 专属字段（platform=devbox 时显示）：
  - gateway_url（网关 URL 模板，如 https://{tunnel_id}-{port}.devbox-s2.hwtunnel.com）
  - gateway_ip（网关 IP，可选）
  - gateway_skip_tls_verify（开关，是否跳过 TLS 验证）
```

### 8.3 前端交互流程

```
用户点击侧边栏"云沙箱"
  → 默认展示 Tab 1: Provider 列表（表格 + 分页 + 搜索）
  → 表格行：Name / Platform / Endpoint / Scope / Max Sandboxes / Enabled / 操作
  → 用户 Provider 行有操作按钮：详情/编辑/启停/健康检查/删除
  → 系统 Provider 行只有"详情"按钮
  → 顶部有"新增 Provider"按钮

用户点击"详情"或 Name
  → 切换到 Provider 详情页（独立视图）
  → 展示 Provider 完整信息卡片（含平台专属字段）
  → 展示该 Provider 下的模板列表表格（完整列）
  → 用户 Provider：模板行可编辑/删除，可新增模板，可绑定/解绑引擎
  → 系统 Provider：模板只读
  → 点击"查看沙箱实例" → 跳转沙箱列表 Tab，provider_id 自动带入

用户点击 Tab 2: 沙箱列表
  → 表格 + 分页 + 搜索 + provider_id 筛选下拉
  → provider 筛选下拉从 GET /sandbox-providers 动态获取
```

### 8.4 前端数据加载

```
GET /api/settings/sandbox-providers?page=1&page_size=20&q=xxx → Provider 列表（分页）
GET /api/settings/sandbox-templates?provider_id={id} → Provider 详情中的模板列表
GET /api/runtime/cloud-sandboxes?provider_id={id}&page=1&page_size=20 → 沙箱列表（分页）
```

### 8.5 API 路径变更

| 现有路径 | 新路径 | 说明 |
|---------|--------|------|
| GET /api/settings/sandbox-provider-configs | GET /api/settings/sandbox-providers | Provider 列表 |
| PUT /api/settings/sandbox-provider-configs/{provider} | POST/PUT /api/settings/sandbox-providers/{id} | Provider CRUD |
| - | DELETE /api/settings/sandbox-providers/{id} | 新增删除 |
| - | PUT /api/settings/sandbox-providers/{id}/toggle | 新增启停 |
| - | POST /api/settings/sandbox-providers/{id}/health | 新增健康检查 |
| - | GET /api/settings/sandbox-providers/{id}/delete-preview | 删除预览 |
| GET /api/runtime/cloud-sandboxes?provider=xxx | GET /api/runtime/cloud-sandboxes?provider_id={id} | 筛选字段从 provider 字符串改为 provider_id |
| - | POST/DELETE /api/settings/sandbox-templates/{id}/bind-engine/{engine} | 引擎绑定 |

## 9. 风险与确认点

### 9.1 向后兼容

1. **runtime_info 变更**：现有 agent_runtimes.runtime_info JSON 含 `provider` 字段（字符串），改为 `provider_id`（整数）。存量 runtime 需迁移或兼容处理。
   - 方案：build_context 同时支持新旧字段——有 `provider_id` 用新逻辑，有 `provider` 走旧逻辑（fallback 到按 provider 名查 config）。
   - 确认：是否需要严格迁移，还是兼容过渡？

2. **provider-configs 接口结构变更**：从 `{provider_name: config}` 变为 `{providers: [...]}`。cloud_worker 需同步改造。
   - 确认：是否保留旧接口做灰度过渡，还是直接替换？

### 9.2 需要确认的设计决策

| # | 问题 | 默认方案 | 状态 |
|---|------|---------|------|
| Q1 | system Provider 如何管理？ | 系统预置到数据库中（初始化写入 scope=system），普通用户只读不可改 | 已确认 |
| Q2 | 用户 Provider 数量上限？ | 每用户最多 5 个，上限可配置（DB 配置项 sandbox_provider_max_per_user，默认 5） | 已确认 |
| Q3 | 同一引擎在多个 Provider 下都有模板时，cloud_worker 注册多个 runtime？ | 是（runtime name 含 provider_id 区分） | 已确认 |
| Q4 | sandbox_templates.engines JSON 字段保留还是废弃？ | 废弃——引擎-模板关联全部走 engine_template_binding 表，单一数据源，无双写一致性问题 | 已确认 |
| Q5 | 删除 Provider 时正在运行的沙箱如何处理？ | 先提示确认 → 确认后先 kill 正在运行的沙箱（遍历 cloud_sandboxes 表按 provider_id 查 state=running 的沙箱，逐个 kill）→ 再删除 Provider + 级联删除模板和绑定 | 已确认 |
| Q6 | 用户 Provider 配置中的 api_key 是否需要用户级加密？ | 复用现有 crypto.encrypt（全局密钥），不按用户分密钥 | 已确认 |
| Q7 | 删除确认提示如何实现？ | API 分两步：第一步 GET /sandbox-providers/{id}/delete-preview 返回关联的沙箱数量和模板数量供前端展示；第二步 DELETE /sandbox-providers/{id}?confirm=true 执行实际删除（不传 confirm 返回 409 + 待删除资源清单） | 已确认 |
| Q8 | credential 和 Provider 隔离如何分离？ | v3：agent_runtimes.scope 是 scope 唯一数据源（per-runtime 真实 scope），不再从 credential 继承。credential 只管信任边界（credential_id 匹配），不携带 scope 隔离信息。过滤点直接读 agent_runtimes，不 JOIN runtime_credentials。新增 agent_runtimes.owner_name 列消除 JOIN。 | 已确认 |
| Q9 | cloud_worker 如何获取 USER_GITCODE_TOKEN？ | cloud_worker 无法直接读 DB（远程机器），通过 /env API 获取——/env 端点按 task.user_id 查 get_user_gitcode_token 返回。cloud_worker 注入 env["USER_GITCODE_TOKEN"]，沙箱内 agent 用它直接调 GitCode API。local worker 不改（直接读 DB）。 | 已确认 |
| Q10 | /env API 如何防越权？ | task 已被认领（runtime_id 非零）时，校验 runtime.credential_id == cred.id。不匹配返回 403。task 未被认领（runtime_id=0）时不拦截（claim 和 /env 几乎同时调用）。 | 已确认 |

## 10. 实施计划

| 阶段 | 内容 | 依赖 |
|------|------|------|
| P1 | DB 表结构改造 + 存量迁移脚本 | 无 |
| P2 | Provider CRUD API + 权限校验 | P1 |
| P3 | 模板 CRUD API + 引擎绑定 API | P1 |
| P4 | cloud_worker _expand_templates + _provider_configs 改造 | P1, P2 |
| P5 | SandboxManager + SandboxContext 改造 | P1, P4 |
| P6 | provider-configs 接口改造 | P1 |
| P7 | 存量数据迁移 + 兼容性验证 | P1-P6 |
| P8 | E2E 验证（三引擎 × 多Provider × cloud_worker） | P1-P7 |
| P9 | /env API 安全加固 + user_gitcode_token 返回（R10） | P1 |
| P10 | cloud_worker USER_GITCODE_TOKEN 注入（R9） | P9 |
| P11 | 安全验证（/env 越权防护 / 日志脱敏） | P9, P10 |

## 11. 验证矩阵

| 场景 | 验证点 |
|------|--------|
| 系统 Provider 查看 | 登录用户可查看，写操作返回 403 |
| 用户 Provider CRUD | 新增/编辑/删除/启停全流程 |
| 多 Provider 同平台 | 两个 cubesandbox Provider 同时 enabled |
| 模板-引擎绑定 | 同一 Provider 下 hermes 引擎绑定两个模板 → 拒绝 |
| cloud_worker 同步 | Provider 新增/删除后 60s 内 runtime 自动注册/下线 |
| 沙箱创建 | 按 provider_id 选 Provider 实例创建沙箱 |
| 级联删除 | 删除 Provider → 模板 + binding 自动删除 |
| 存量兼容 | 现有 cubesandbox/devbox 配置迁移后正常工作 |
| Runtime 隔离-系统 | 系统 Provider runtime scope=global，所有用户任务可分配 |
| Runtime 隔离-用户级 | 用户级 Provider runtime scope=user，仅该用户任务可分配 |
| Runtime 隔离-仓库级 | 仓库级 Provider runtime scope=repo，仅该仓库任务可分配 |
| Pool 广播隔离 | 用户/仓库级 Provider runtime 不参与池广播（agent_tags=[]） |
| 引擎优先级 | 系统 Provider 优先于用户 Provider 被 pick_engine 选中 |
| /env 越权防护 | worker A 调 /tasks/{B的task_id}/env → 403（credential_id 不匹配） |
| /env user_token 返回 | cloud_worker claim task 后 /env 返回 USER_GITCODE_TOKEN |
| cloud_worker token 注入 | cloud_worker 执行 task 时 env 含 USER_GITCODE_TOKEN |
| 日志脱敏 | USER_GITCODE_TOKEN 在 executor 输出中被 RedactEngine 脱敏 |

## 12. 用户敏感信息获取与 /env API 改造

### 12.1 架构原则

两种凭据严格分离，各管各的：

| 凭据类型 | 载体 | 职责 | 获取方式 | 数据归属 |
|---------|------|------|---------|---------|
| worker credential（rta_） | runtime_credentials.id ↔ agent_runtimes.credential_id | 信任边界：这个 runtime 属于哪个 worker？心跳/probe/claim 合法吗？ | rta_ token 认证 | 只能获取公共数据（LLM key、AK/SK 等服务端管理的凭据） |
| 用户 token（user_gitcode_token） | task payload → env["USER_GITCODE_TOKEN"] | 用户身份：以哪个用户身份调外部接口？ | enqueue 时注入 task → claim 返回 → worker 读 task 或 /env API 获取 | 直接调外部接口（GitCode API）获取用户级敏感数据（仓库、PR、Issue 等） |

worker credential 不携带用户身份——它只认 worker。用户 token 不依赖 worker credential——它认的是 task 的发起用户。两条路径独立。

### 12.2 现有机制

#### 已有的用户 token 注入流程（local worker）

```
chat.py: 用户发起 chat
  → 从 user_accounts.gitcode_api_token 取用户 token
  → enqueue_task(user_gitcode_token=...) → 写入 agent_tasks.user_gitcode_token
  → claim 返回 task dict（含 user_gitcode_token）
  → agent.py _execute_task 读 task.user_gitcode_token（仅 chat 任务）
  → executor._assemble_env 注入 env["USER_GITCODE_TOKEN"]
```

devops 事件任务（event_pipeline）不注入 user_gitcode_token——enqueue_task 调用时不传该参数（恒为空）。

#### 已有的用户敏感信息获取流程（cloud_worker + local worker）

```
cloud_worker: claim 拿到 task dict
  → client.get_task_env_full(task_id) → GET /api/runtime/tasks/{task_id}/env
  → 服务端按 task.user_id 查 user_llm_credentials + user_credentials
  → 返回 _LLM_* + HUAWEICLOUD_SDK_AK/SK
```

runtime_task_env 端点（GET /tasks/{task_id}/env）：
- 认证：rta_ credential（cred.id）
- 返回：LLM 凭据（按 task.user_id 查 select_llm_cred）+ AK/SK（按 task.user_id 查 _collect_server_env）
- 不返回：user_gitcode_token

#### 缺失的流程

1. cloud_worker 不注入 USER_GITCODE_TOKEN——_cloud_execute_task 只读 repo_gitcode_token 设 env["BOT_GITCODE_TOKEN"]，不读 user_gitcode_token（R9 缺失）
2. /env API 不返回 user_gitcode_token——只返回 LLM 凭据 + AK/SK（R10 中 /env API 应返回 user_gitcode_token 供 cloud_worker 获取）
3. /env API 不校验调用方是否真正认领了该 task——任何合法 worker 都能调 /tasks/{any_task_id}/env 获取任意 task 的用户敏感信息

#### 两条路径的现状

| 路径 | R9 用户 token（调外部 API） | R10 公共凭据（调 grape /env API） |
|------|---------------------------|--------------------------------|
| local worker | 已实现：task payload → env["USER_GITCODE_TOKEN"] → executor._assemble_env | 已实现：直接读 DB 查 LLM key/AK-SK |
| cloud_worker | **缺失**：_cloud_execute_task 不读 user_gitcode_token | 部分实现：/env API 返回 LLM key/AK-SK，但不返回 user_gitcode_token |

### 12.3 改造方案

#### 12.3.1 /env API 安全加固 + user_gitcode_token 返回

runtime_task_env 端点改造（src/api/routes/runtime.py）：

**安全加固**：增加 task → runtime → credential_id 匹配校验，只有真正认领了该 task 的 worker 才能获取其 env：

```python
@router.get("/tasks/{task_id}/env")
def runtime_task_env(task_id: str, cred: CredentialContext = Depends(_require_credential)):
    # ---- v3 安全加固：校验调用方是否真正认领了该 task ----
    with db_lock:
        c = db.cursor()
        c.execute("SELECT runtime_id, user_id, account FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
    if not row:
        return {"env": {}}
    _runtime_id, user_id, account = row[0] or 0, row[1] or 0, row[2] or ""
    if _runtime_id:
        c.execute("SELECT credential_id FROM agent_runtimes WHERE id=?", (_runtime_id,))
        _rt_row = c.fetchone()
        _rt_cred_id = _rt_row[0] if _rt_row else 0
        if _rt_cred_id != cred.id:
            # 越权：task 已被认领（runtime_id 非零），但认领的 worker credential 不匹配
            raise HTTPException(status_code=403, detail={"code": 403, "message": "forbidden: task not owned by this credential"})
    # runtime_id=0（queued，未被认领）时不拦截——claim 和 /env 可能同时调用

    env = _collect_server_env(user_id, account)

    # ---- v3 新增：返回 user_gitcode_token（按 task.user_id 查）----
    if user_id and user_id > 0:
        try:
            from src.user.db import get_user_gitcode_token
            _user_token = get_user_gitcode_token(user_id)
            if _user_token:
                env["USER_GITCODE_TOKEN"] = _user_token
        except Exception:
            pass

    # LLM 凭据（现有逻辑不变）
    ...
    return {"env": env}
```

校验逻辑：
- task.runtime_id 非零（已被认领）→ 查 runtime.credential_id → 必须匹配 cred.id，否则 403
- task.runtime_id=0（queued，未被认领）→ 不拦截（claim 和 /env 可能同时调用，claim 先成功再调 /env）

#### 12.3.2 cloud_worker 注入 USER_GITCODE_TOKEN

_cloud_execute_task 改造（src/runtime/cloud_worker.py）：

cloud_worker 无法直接读 DB（远程机器），通过 /env API 获取 USER_GITCODE_TOKEN（R10 路径）。获取后注入 env，供沙箱内 agent 用用户 token 直接调 GitCode API 获取用户级数据（R9 路径）。

同时修正互斥注入逻辑——现有 cloud_worker 恒设 env["BOT_GITCODE_TOKEN"]，但 chat 任务应只注入 USER_GITCODE_TOKEN 不注入 BOT_GITCODE_TOKEN（和 _assemble_env 互斥注入一致）：

```python
# 现有：恒设 BOT_GITCODE_TOKEN（不论任务类型）
# env["BOT_GITCODE_TOKEN"] = task.get("repo_gitcode_token") or ""

# v3 改造：按 event_type 互斥注入（和 _assemble_env 一致）
_is_chat = (task.get("event_type") or "") == "chat" or (task.get("agent_type") or "") == "chat"

# LLM 凭据从 /env API 取（现有逻辑）
task_env = await client.get_task_env_full(tid)

if _is_chat:
    # chat 任务：只注入 USER_GITCODE_TOKEN（用户身份）
    # v3 新增：从 /env API 取 user_gitcode_token
    if task_env.get("USER_GITCODE_TOKEN"):
        env["USER_GITCODE_TOKEN"] = task_env["USER_GITCODE_TOKEN"]
    # 不注入 BOT_GITCODE_TOKEN（互斥——防用户对话上下文暴露仓库 bot token）
else:
    # devops 事件任务：只注入 BOT_GITCODE_TOKEN（仓库身份）
    env["BOT_GITCODE_TOKEN"] = task.get("repo_gitcode_token") or ""
    # 不注入 USER_GITCODE_TOKEN
```

变更：
1. 新增 chat/devops 互斥判断（和 _assemble_env 一致）
2. chat 任务从 /env API 获取 USER_GITCODE_TOKEN 并注入（R10 路径获取，R9 路径使用）
3. chat 任务不注入 BOT_GITCODE_TOKEN（安全：防 bot token 暴露在用户对话上下文中）
4. devops 事件任务保持原有 BOT_GITCODE_TOKEN 注入，不注入 USER_GITCODE_TOKEN

#### 12.3.3 executor _assemble_env 改造

_dispatch_execution → _run_cloud_sandbox 路径中，proc_env 已包含 USER_GITCODE_TOKEN（cloud_worker 注入）。_run_cloud_sandbox 的脱敏列表已包含 USER_GITCODE_TOKEN：

```python
# executor.py _run_cloud_sandbox（现有代码，无需改）
_tokens = [t for t in (proc_env.get("BOT_GITCODE_TOKEN", ""), proc_env.get("USER_GITCODE_TOKEN", "")) if t]
_redact_engine = RedactEngine(secret_values=_tokens) if _tokens else None
```

脱敏已覆盖。cloud_worker 路径不需要改 executor。

#### 12.3.4 agent.py local worker 路径——不动

local worker（agent.py _execute_task）保持现有逻辑不变：
- chat 任务读 task.user_gitcode_token → 传给 executor._assemble_env → 注入 env["USER_GITCODE_TOKEN"]
- 不需要改——local worker 能直接读 DB（get_user_gitcode_token），无需通过 /env API

### 12.4 安全分析

#### S1：/env API 越权防护

改造前：任何合法 worker 都能调 /tasks/{any_task_id}/env 获取任意 task 的用户敏感信息。

改造后：task 已被认领（runtime_id 非零）时，校验 runtime.credential_id == cred.id。只有真正认领了该 task 的 worker 才能获取其 env。task 未被认领（runtime_id=0）时不拦截——但这不是问题，因为 claim 和 /env 几乎同时调用，claim 成功后 runtime_id 立即变为非零。

#### S2：user_gitcode_token 获取路径

改造前：cloud_worker 不注入 USER_GITCODE_TOKEN（只有 local worker 在 chat 任务时注入）。

改造后：cloud_worker 通过 /env API 获取 USER_GITCODE_TOKEN（按 task.user_id 查 get_user_gitcode_token），注入 env["USER_GITCODE_TOKEN"]。沙箱内 agent 用它直接调 GitCode API 获取用户级数据。

两条路径独立、互不干扰：
- local worker：直接读 DB 查 get_user_gitcode_token → task payload → env["USER_GITCODE_TOKEN"]（现有，不改）
- cloud_worker：/env API 返回 → env["USER_GITCODE_TOKEN"]（新增）

不改变现有 agent_tasks.user_gitcode_token 列的写入逻辑（chat 任务仍写入，local worker 仍读 task payload）。cloud_worker 路径不依赖 task payload 中的 user_gitcode_token——它通过 /env API 实时获取。

#### S3：日志脱敏

cloud_worker 和 executor 的日志脱敏已覆盖 USER_GITCODE_TOKEN：
- cloud_worker _cloud_execute_task 的 _log 函数不打印 env 内容
- executor _run_cloud_sandbox 的 RedactEngine 脱敏列表包含 USER_GITCODE_TOKEN
- runtime_task_env 端点不打印返回的 env 内容

#### S4：Provider api_key 下发

cloud_worker 拉取 /api/runtime/provider-configs 返回 api_key 明文（用于创建沙箱）。cloud_worker 持有 system credential，可获取所有 Provider 的 api_key。

风险：cloud_worker 进程被入侵时，所有 Provider 的 api_key 泄露。

缓解：provider-configs 接口日志中不打印 api_key（已实现，api_key 字段在日志中脱敏）。Provider 配置的 api_key_enc 在 DB 中加密存储（crypto.encrypt），只有下发给 cloud_worker 时解密。

### 12.5 改动点清单

| 文件 | 改动 |
|------|------|
| `src/api/routes/runtime.py` | runtime_task_env 端点增加 credential_id 匹配校验（S1）+ 增加 user_gitcode_token 返回（按 task.user_id 查 get_user_gitcode_token） |
| `src/runtime/cloud_worker.py` | _cloud_execute_task 新增 chat/devops 互斥注入逻辑（和 _assemble_env 一致）+ chat 任务从 /env API 获取 USER_GITCODE_TOKEN 并注入 + chat 任务不注入 BOT_GITCODE_TOKEN |

## 13. 职责矩阵与耦合审查

### 13.1 对象职责定义

| 对象 | 职责 | 不做什么 | 生命周期 |
|------|------|---------|---------|
| **runtime_credentials** (rta_) | worker 信任边界：register/heartbeat/claim 身份认证 | 不携带 scope 隔离信息；不携带用户身份；不获取用户敏感数据 | _seed_cloud_worker_key 生成 system credential；Provider 创建/删除不生成/删除 credential |
| **agent_runtimes** | 引擎实例：per-runtime scope/owner_id/owner_name 管任务分配隔离；credential_id 管信任边界 | 不从 credential 继承 scope（v3 改为从 Provider 配置解析） | register 时创建（cred.id + Provider 配置解析 scope）；reap_stale 清理 |
| **sandbox_provider_config** | Provider 实例配置：platform/endpoint/api_key/scope/user_id/repo_key | **不持有 credential**（v3 删除 credential_id）；不注册 runtime | 用户创建 / 系统预置；删除时级联删模板+binding |
| **user_gitcode_token** (USER_GITCODE_TOKEN) | 用户身份：以用户身份调外部 API（GitCode） | 不用于 grape 内部认证；不通过 worker credential 获取 | enqueue 时注入 task → claim 返回 → worker 读 task 或 /env API |
| **worker credential** (rta_) | 获取公共数据：调 grape /env API 获取 LLM key/AK-SK | 不获取用户级敏感数据（用户 token、用户仓库数据） | cloud_worker 启动时读 key 文件认证 |

### 13.2 职责分离验证

**credential vs Provider**：
- v2 问题：Provider 持有 credential_id，register 从 Provider 取 credential → Provider 和 credential 耦合
- v3 修正：Provider 不持有 credential。credential 是 worker 级的（cloud_worker 启动时生成），Provider 是配置级的（用户/系统创建）。两条生命周期独立——删除 Provider 不影响 credential，吊销 credential 不影响 Provider 配置

**credential vs scope**：
- v2 问题：过滤点 JOIN runtime_credentials 取 scope → credential 同时管信任边界和隔离
- v3 修正：agent_runtimes.scope 是 scope 唯一数据源，从 Provider 配置解析（不从 credential 继承）。credential 只管信任边界（credential_id 匹配）。过滤点直接读 agent_runtimes，不 JOIN runtime_credentials

**worker token vs user token**：
- R9/R10 修正：worker credential（rta_）只获取公共数据（LLM key/AK-SK via /env API）。用户 token（user_gitcode_token）注入 task，worker 用它直接调外部 API。两条路径独立——worker credential 不携带用户身份，user token 不用于 grape 内部认证

**Provider vs runtime**：
- Provider 提供 scope/user_id/repo_key（隔离维度来源）
- runtime 携带 credential_id（信任边界）+ scope/owner_id/owner_name（隔离维度，从 Provider 解析）
- Provider 删除 → cloud_worker 下次 refresh 下线对应 runtime（runtime_info.provider_id 匹配不到 Provider）。Provider 和 runtime 通过 provider_id 关联，但生命周期间接——Provider 删除不直接删 runtime，而是通过 cloud_worker refresh 间接下线

### 13.3 残留耦合检查

| 检查项 | 状态 | 说明 |
|--------|------|------|
| sandbox_provider_config.credential_id | 已删除 | v2 残留，v3 清理——Provider 不持有 credential |
| Provider 创建时生成 credential | 已删除 | v2 残留，v3 清理 |
| Provider 删除时删除 credential | 已删除 | v2 残留，v3 清理 |
| 过滤点 JOIN runtime_credentials 取 scope | 已删除 | v3 改为直接读 agent_runtimes |
| agent_runtimes.scope 从 credential 继承 | 已删除 | v3 改为从 Provider 配置解析 |
| cloud_worker 不注入 USER_GITCODE_TOKEN | 已修复 | §12.3.2 新增 2 行 |
| sandbox_provider_config 缺少 repo_key 列 | 已修复 | v3 新增 M4b 迁移步骤 + DDL 加列 |
| scope_level vs scope 语义不一致 | 已修复 | v3 明确：scope=user + repo_key 非空 = 仓库级，不单独存 scope=repo |
| runtime_info 含 provider_scope/provider_user_id 死字段 | 已删除 | v3 精简：runtime_info 只含 provider_id，register API 查 DB 取真值 |
| sandbox-templates API 返回 provider_scope/provider_user_id/provider_repo_key 死字段 | 已删除 | v3 精简：只返回 provider_id/platform/provider_name |
| agent_runtimes.repo_key 列死字段 | 已修复 | v3：repo scope 的 owner_id 已存 repo_key 值，register INSERT 不再写入 repo_key 列 |
| UNIQUE 约束缺少 repo_key | 已修复 | v3 改为 UNIQUE(scope, user_id, repo_key, name) |
| cloud_worker 恒设 BOT_GITCODE_TOKEN（不分 chat/devops） | 已修复 | v3 改为按 event_type 互斥注入（和 _assemble_env 一致）：chat 只注入 USER_GITCODE_TOKEN，devops 只注入 BOT_GITCODE_TOKEN |

### 13.4 审查修复记录（2026-10-09）

| 检查项 | 状态 | 说明 |
|--------|------|------|
| agent_runtimes DDL 缺 owner_name 等 13 列 | 已修复 | DDL 补全 owner_name/busy_slots/max_concurrent/probe_*/scene_tags/alias/tags_user_set/runtime_type/runtime_info，新库 CREATE TABLE 即完整 |
| runtime_credentials DDL 缺 source/node_id 等 5 列 | 已修复 | DDL 补全 source/node_id/tmp_percent/bandwidth_percent/bandwidth_mbps |
| engine_sandbox_toggle 用 MySQL 语法内联创建 | 已修复 | 移入 _TABLES 列表，改为 SQLite 兼容 DDL（INTEGER PRIMARY KEY + INTEGER DEFAULT） |
| _seed_sandbox_templates 仍写 engines JSON | 已修复 | INSERT engines 列改为写 "[]"（废弃列写空值），引擎关联走 engine_template_binding |
| register offline 未清理 cloud 孤儿 runtime | 已修复 | register 循环结束后 DELETE 同 credential_id 下不在新列表中的 cloud runtime + 清理 engine_capabilities 孤儿 |
| agent_runtimes.repo_key 死字段仍被写入 | 已修复 | INSERT agent_runtimes 移除 repo_key 列（不再写入 cred_repo_key） |
| events.py/scenes.py/chat.py JOIN rc 取 scope | 不修改 | 基线代码（非本分支引入），不在本分支改动范围 |

### 13.5 E2E 验证修复记录（2026-10-09）

| 检查项 | 状态 | 说明 |
|--------|------|------|
| cloud_worker 流控校验 build_context NameError | 已修复 | `_cloud_execute_task` 第 381 行调用 `build_context(task)` 但该函数直到第 407 行才导入。运行时 `NameError` 被 except 吞掉，F1-F4 流控形同虚设。修复：将 `from src.agent.sandbox.sandbox_context import build_context as _build_ctx` 提前到流控校验之前，删除下方重复导入和重复调用 |
| register offline 清理 `_healthy` 守卫设计确认 | 确认正确 | 子代理审查提出 `if not _healthy` 守卫可能遗漏 worker 正常下线场景。经分析：cloud_worker `_refresh_templates` 增量注册只传 changed runtimes 子集，若去掉 `_healthy` 守卫会误删未变更的 runtime。`_healthy` 守卫设计正确——仅在 worker 报告 unhealthy（全离线）时触发孤儿清理。已补注释说明设计意图 |

### 13.6 E2E 验证矩阵（2026-10-09）

| 验证项 | 方式 | 结果 |
|--------|------|------|
| DB 层 DDL + 迁移 | MySQL DESCRIBE + 数据查询 | 通过：2 provider, 3 template, 3 binding, updated_at ON UPDATE CURRENT_TIMESTAMP |
| Provider CRUD | curl GET/POST/PUT/DELETE | 通过：创建/更新/删除全通过 |
| 模板列表 | curl GET provider_id 筛选 | 通过：返回 platform/provider_name/scope |
| 引擎绑定 | curl POST/DELETE bind-engine | 通过：system Provider 模板 403（正确） |
| cloud-sandboxes 列表 | curl GET /runtime/cloud-sandboxes | 通过：空列表 + 分页元数据 |
| /env API 越权防护 | 代码审查 runtime.py L858-867 | 通过：runtime_id 非零时校验 credential_id 匹配 |
| /env API 返回 user_gitcode_token | 代码审查 runtime.py L872-880 | 通过 |
| register Provider 配置解析 | 代码审查 runtime.py L346-389 | 通过：system→global, user+repo_key→repo, user→user |
| register offline 清理 | 代码审查 runtime.py L445-457 | 通过：`_healthy` 守卫 + DELETE cloud 孤儿 |
| _expand_templates 三段式 | 代码审查 cloud_worker.py L131 | 通过：`f"{_engine_type}:{_platform}:{_provider_id}"` |
| 互斥注入 | 代码审查 cloud_worker.py L343-354 | 通过：chat→USER_GITCODE_TOKEN, devops→BOT_GITCODE_TOKEN |
| 流控校验 | 代码审查 cloud_worker.py L380-394 | 通过（修复后）：active_count >= max → requeue |
| _materialize_ssl_cert | 代码审查 cloud_worker.py L166-191 | 通过：`cube-rootCA-{pid}.pem` per-provider |
| _refresh_templates DB 初始化 | 代码审查 cloud_worker.py L774-782 | 通过：首次 get_cloud_runtimes 初始化 _last_runtimes |
| 过滤点 scope 取自 ar | 代码审查 engine_select.py + service.py | 通过：claim_by_tag/_check_pool_engines_reachable/_engine_scope_info 从 ar 取 scope |
| client.py 新增方法 | 代码审查 client.py L379-433 | 通过：get_provider_configs/get_cloud_runtimes/get_provider_active_sandboxes |
| SandboxContext provider_id+platform | 代码审查 sandbox_context.py L38-39 | 通过 |
| manager provider_id 索引 | 代码审查 manager.py L243-253 | 通过：_provider_configs.get(_provider_id) |
| CubeSandbox per-provider SSL | 代码审查 cubesandbox.py L545-551 | 通过：删除进程级 REQUESTS_CA_BUNDLE |
| 前端双 Tab + 表格 + 分页 | 代码审查 sandbox.js | 通过 |
| 前端弹窗动态字段 | 代码审查 sandbox.js L498-510 | 通过：cubesandbox→SSL, devbox→gateway |
| 前端 scope_level repo_key 自动填充 | 代码审查 sandbox.js L148-151 | 通过：currentRepo() |
| 单元测试 | pytest 209 tests | 209 passed, 1 pre-existing deselected, 0 failed |

### 13.7 深度审查修复记录（2026-10-09）

外部审查发现的问题逐项核实并修复：

| 检查项 | 级别 | 状态 | 修复说明 |
|--------|------|------|----------|
| BUG-1 bwrap.wrap() 静默裸执行 + 审计误导 | 安全 | 已修复 | build_bwrap_argv 返回 None 时 wrap() 跳过 bwrap 包装但审计记 "allow"。修复：检查 _opts_cache is None → 审计记 "deny" + raise SandboxUnavailableError（fail-closed） |
| BUG-2 run_long_cmd 丢弃 envs 参数 | 功能 | 已修复 | run_long_cmd 调用 _run_long_command 时未传 envs。修复：传 envs=envs |
| BUG-3 schema.py 与 db_setup.py 不同步 | Schema | 已修复 | schema.py cloud_sandboxes 缺 provider_id，user_llm_credentials 缺 engine_type + UNIQUE 不一致。修复：schema.py DDL 同步加列 + 改 UNIQUE(user_id, engine_type) |
| BUG-4 _clone_repo 用 cmds.run 而非 self.run_cmd | 可靠性 | 已修复 | 短命令（chmod/git config/test -d）从 cmds.run 改为 self.run_cmd，统一走 gRPC 流中断重试。dir_check 返回值从 .stdout 改为 tuple 解包 |
| BUG-5 codex 不在 ENGINE_REGISTRY | 功能 | 已修复 | 加注释说明 codex 是本机引擎不走云沙箱路径，不在此注册 |
| BUG-7 _FORBIDDEN_PATHS /root/ 阻止审计路径 | 配置 | 已修复 | /root/ 改为 /root/.ssh/ + /root/.gnupg/，root 用户的 ~/.grape/logs/ 审计路径不再被阻止 |
| 遗漏-2 _provider_cache 无锁保护 | 并发 | 已修复 | _select_provider_from_context 中 _provider_cache 读写加 threading.Lock |
| 遗漏-5 _run_long_command 临时文件未清理 | 资源 | 已修复 | 正常完成 + 超时两条路径都清理 /tmp/_long_cmd_*.{sh,done,exit,log} |

审查误报排除（不需要修复）：

| 检查项 | 状态 | 说明 |
|--------|------|------|
| BUG-6 seccomp fd 从不关闭 | 误报 | seccomp fd 是全局共享的（"fd 跨多次 wrap，作为 SandboxManager 全局依赖注入"）。per-task cleanup 不应 close 全局 fd。close() 在 SandboxManager 销毁时调用 |
| 遗漏-4 _init_sandbox step 7 是死代码 | 误报 | step 7 从 self._ctx（SandboxContext）读 repo_url/bot_token。ctx.repo_url 在 build_context() 中从 env_extra 提取构建，非空。不是死代码 |

### 13.8 前端 API 调用审计（2026-10-10）

用户报告 `API.delete is not a function` 错误。根因：api.js 导出方法名为 `del`（L371），不是 `delete`（`delete` 是 JS 保留字不能作属性名）。settings.js L761 和 sandbox.js L179/208/219 共 4 处误用 `API.delete`。

修复后进行全面发散审计：

| 审计项 | 方式 | 结果 |
|--------|------|------|
| API 方法名 vs 导出名对比 | grep 全部 JS 文件提取 `API.\w+` 调用，与 api.js `return {...}` 导出做差集 | 全部匹配。调用集 = {get, post, put, del, upload, deleteFeedback, scheduleProactiveRefresh}。无未导出方法被调用 |
| API.delete 残留 | grep `API\.delete\b` 全部 JS | 0 处（已全部修复为 API.del） |
| API 方法拼写错误 | grep `API\.\(dele\|remov\|updat\|creat\|delet\|inser\|sear\|fetc\)` | 0 处。`API.deleteFeedback` 是合法导出方法（4 处调用正确） |
| await 在 async 函数外 | 检查所有 `await API.*` 调用上下文 | 全部在 async 函数内 |
| currentRepo() 守卫 | 检查 `app.currentRepo()` 调用是否有 typeof 守卫 | settings.js 5 处直接调用无守卫（基线代码，非本分支引入）。sandbox.js 1 处有 `typeof currentRepo === 'function'` 守卫。observability.js 全部有 `typeof app !== 'undefined' && app.currentRepo` 守卫 |
| fetch 绕过 API 封装 | grep `fetch(` 排除 api.js | observability.js 6 处 + chat.js 3 处直接用 fetch。observability.js 端点无认证（"无认证：运维只读数据"），chat.js 带 Authorization header。均正确 |
| Vue data/methods 引用 | sandbox.js 中 `this.*` 引用与 data()/methods 定义对比 | 全部定义。_handleError、healthResults、showDeleteConfirm、showBindDialog 等均存在 |
| sandbox.js API 端点 vs 后端路由 | 逐一验证 14 个 API 调用对应后端 @router 装饰器 | 全部存在。Provider CRUD(6) + 模板 CRUD(3) + 引擎绑定(2) + cloud-sandboxes(1) + delete-preview/toggle/health(3) |
| JS 语法检查 | `node -c` settings.js + sandbox.js | 语法正确 |

### 13.9 初始化插件框架 + 配置文件模板化（2026-10-10）

问题：init_database 中 24 个初始化步骤硬编码在 MySQL/SQLite 两个分支中重复编写，新增步骤需改主流程代码。Provider 配置和模板数据硬编码在 _seed_sandbox_templates 函数中，配置文件（cubesandbox-config.json）只含 Provider 配置不含模板。

改造为插件化框架：

1. 初始化插件框架（src/api/init_framework.py）：
   - InitTask Protocol 接口（name, order, run(c, db_lock)）
   - @register_init_task(order=N) 装饰器自动注册
   - run_all_init_tasks() 按 order 排序执行，每个任务独立 try/except
   - order 约定：0-99 建表，100-199 迁移，200-299 种子，300-399 清理，400-499 平台特定

2. init_tasks 包（src/api/init_tasks/）：
   - __init__.py：包说明，新增任务在此 import 触发注册
   - sandbox_provider_init.py：@register_init_task(order=400)，从配置文件导入 Provider + 模板

3. 配置文件模板化：
   - cubesandbox-config.example.json：加 templates 数组 + _comment/_fields 注释
   - devbox-config.example.json：加 templates 数组 + _comment/_fields 注释
   - 一个配置文件 = 一个 Provider + N 个模板（templates 数组中每个条目含 name/template_id/engine_type/alias/description/preinstalled/disk_size_mb）
   - 引擎绑定关系在 templates[].engine_type 中声明，导入时写入 engine_template_binding

4. init_database 改造：
   - 删除 MySQL/SQLite 两个分支中的 _migrate_provider_configs_to_db() 和 _seed_sandbox_templates() 同步调用
   - 末尾新增后台 daemon 线程异步执行 run_all_init_tasks()——不阻塞主线程
   - 保留 _ensure_sandbox_provider_config_mysql_native()（MySQL DDL 重建必须在 init_tasks 之前同步执行）

5. 扩展方式：新增初始化任务只需在 src/api/init_tasks/ 下新建文件，定义类 + @register_init_task(order=xxx)，在 __init__.py 中 import。init_database 自动发现执行，不改主流程。

| 检查项 | 状态 | 说明 |
|--------|------|------|
| init_framework.py 插件框架 | 新增 | InitTask Protocol + @register_init_task 装饰器 + run_all_init_tasks |
| init_tasks/ 包 | 新增 | sandbox_provider_init.py 从配置文件导入 Provider + 模板 |
| 配置文件含 templates 数组 | 已改 | example.json + 真实配置文件均含 templates 数组 + _comment 注释 |
| init_database 异步执行 | 已改 | 末尾 daemon 线程异步执行 init_tasks，不阻塞主线程 |
| _seed_sandbox_templates 硬编码 | 已删 | 改为从配置文件 templates 数组读取 |
| _migrate_provider_configs_to_db 调用 | 已删 | 逻辑合并到 sandbox_provider_init.py |
| 编译检查 | 通过 | 4 个文件 py_compile 全通过 |
| 单元测试 | 通过 | 209 passed, 1 deselected, 0 failed |

### 13.10 平台可插拔改造——配置文件驱动的自动发现（2026-10-10）

问题：新增平台需要改 6 个地方的硬编码（init_tasks/_PROVIDER_CONFIGS、settings.py/_validate_platform、settings.py/_validate_extra_config、backends/__init__.py/default_registry、sandbox.js platform 下拉、配置文件）。

改造为配置文件驱动的自动发现，新增平台只需 2 步：
1. 新建 configs/{platform}-config.json（含 templates 数组）
2. 在 backends/__init__.py 加 try/import

| 改造点 | 旧实现（硬编码） | 新实现（自动发现） |
|--------|----------------|-------------------|
| init_tasks 配置文件扫描 | _PROVIDER_CONFIGS 硬编码列表 | platform_registry.discover_platform_configs() 扫描 configs/*-config.json |
| settings.py 平台校验 | if platform not in ("cubesandbox","devbox") | get_supported_platforms() 从 backend registry 动态获取 |
| settings.py extra_config 校验 | if provider == "cubesandbox": ... elif "devbox": ... | _PLATFORM_EXTRA_MODELS 注册表 dict 查找 |
| sandbox.js platform 下拉 | <option value="cubesandbox"> <option value="devbox"> | v-for="p in platforms" 从 GET /settings/sandbox-platforms 动态获取 |
| platform_registry.py | 不存在（新增） | discover_platform_configs() + extract_extra_config() + get_supported_platforms() + get_platform_extra_fields() |
| API /sandbox-platforms | 不存在（新增） | 返回 [{platform, extra_fields}] 供前端动态渲染 |

新增平台完整步骤（以 "e2b" 为例）：
1. 创建 configs/e2b-config.json（含 api_url/api_key/templates 数组）
2. 在 backends/__init__.py 加 try: from .e2b import E2BProvider; registry["e2b"] = ...
3. （可选）在 settings.py 加 E2BExtraConfig(BaseModel) + _PLATFORM_EXTRA_MODELS["e2b"] = E2BExtraConfig
4. （可选）在 platform_registry.py _PLATFORM_SPECIAL 加证书特殊处理

不需要改：init_tasks、init_database、sandbox.js 前端代码。

| 检查项 | 状态 | 说明 |
|--------|------|------|
| platform_registry.py | 新增 | 配置文件自动发现 + extra_config 自动提取 + 平台列表动态获取 |
| /sandbox-platforms API | 新增 | 返回平台列表 + extra_fields 供前端动态渲染 |
| sandbox.js platform 下拉 | 已改 | 从 API 动态获取，v-for 渲染 |
| _validate_platform | 已改 | 从 backend registry 动态获取，不硬编码 |
| _validate_extra_config | 已改 | _PLATFORM_EXTRA_MODELS 注册表 dict 查找 |
| sandbox_provider_init.py | 已改 | discover_platform_configs() 替代 _PROVIDER_CONFIGS 硬编码 |
| 编译检查 | 通过 | Python + JS 全通过 |
| 单元测试 | 通过 | 209 passed, 0 failed |
