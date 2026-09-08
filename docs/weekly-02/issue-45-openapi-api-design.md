# Issue #45 设计文档：grape 对外 OpenAPI 与 API 认证

> Issue：https://gitcode.com/grape-dev/grape/issues/45
> 类型：Feature | P1 | 状态：spec-ready
> 分支：`weekly-02`（设计在 `weekly-02-lyz` 上产出）
> 作者：Issue Agent Spec（2026-09-07）+ 本地代码复核
> 日期：2026-09-07
> 范围：本文档聚焦 **Get API 认证 + OpenAPI 对外暴露**；CLI / Git 仓库接口见姊妹文档。

## 关联文档

- CLI 设计：`docs/weekly-02/issue-45-cli-design.md`（**契约驱动：新增接口自动获得 CLI 命令**）
- Git 仓库统一接口：`docs/weekly-02/issue-45-git-repos-api-design.md`
- 技能实际 API 使用面（参考基线）：`docs/weekly-02/issue-45-skills-api-scan.md`
- 接口级参考（按接口组织）：`docs/openapi/`

> 服务端新增接口 → OpenAPI 契约更新 → CLI 契约生成命令（§3.5 ↔ cli-design §三）。

## 一、目标与定界（已确认）

| 定界问题 | 结论 |
|----------|------|
| 接口范围 | 业务类接口对外（tasks/repos/agents/chat/skills/scenes/knowledge/events），管理类保留内部（auth/settings/feishu_onboard/webhook/dashboard/templates/runtime/devstage/search/task_logs） |
| OpenAPI 含义 | 基于现有 FastAPI Swagger UI + Token 认证 + 版本管理；**无额外 API 网关/速率限制** |
| Token 权限模型 | **用户级 Token**（绑定单个用户权限），非项目级；暂不需要 RBAC 多级权限 |

> CLI / 技能迁移定界见对应姊妹文档。

## 二、现状复核（代码验证）

### 2.1 关键事实（与 Spec 假设的差异）

- **OpenAPI 当前是关闭的**，不是"已自动生成可访问"：
  `src/api/main.py:244-246` `docs_url=None` / `redoc_url=None` / `openapi_url=None`。
  → 需**显式开启** `/docs`、`/redoc`、`/openapi.json`。
- **现有认证为会话式 JWT**：`src/api/deps.py:get_current_user`（`Authorization: Bearer <jwt>`）→ `src/api/jwt_utils.verify_access_token`。无独立 API Key/Token 机制。
- 21 个路由模块挂载在 `src/api/main.py:402-422`。
- 已有自定义中间件先例：`NoCacheStaticMiddleware`（main.py:436）。

### 2.2 接口分级清单

**对外公开（Token 认证）**：

| 路由模块 | 前缀 | 典型端点 | worker agent 用途 |
|----------|------|----------|-------------------|
| `tasks` | /api/tasks | mine/create/start/status | 任务创建/查询/状态 |
| `repos` | /api/repos | managed/list/deploy-url | 仓库列表/托管状态 |
| `agents` | /api/agents | /{type}/{owner}/{repo} | agent 会话配置查看 |
| `chat` | /api/chat | conversations/messages | 对话/结果获取 |
| `skills` | /api/skills | 列表/详情 | 技能元数据 |
| `scenes` | /api/scenes | 列表 | 场景信息 |
| `knowledge` | /api/knowledge | issue/project 知识 | 上下文获取 |
| `events` | /api/events | detail | 事件/结果查询（worker 核心） |

**保留内部（会话认证，不对外）**：auth、settings、dashboard、feishu_onboard、feishu_card、webhook、templates、runtime、devstage、search、task_logs、health。

## 三、API 设计

### 3.1 Token 认证机制（对齐 GitCode 官方认证风格）

> **参考**：GitCode 官方 OpenAPI 文档（docs.gitcode.com/docs/apis）认证方式：
> - `Authorization: Bearer {token}`（Header）
> - `PRIVATE-TOKEN: {token}`（Header，GitLab 风格）
> - `?access_token={token}`（query，Gitee 风格，GitCode 写操作用它）
> - 认证无效 → `401 Unauthorized` + `{"message":"401 Unauthorized"}`。
>
> **差异点**：grape token 是 **grape 自签发的用户级 API Key**（非 GitCode 个人访问令牌），但传输姿势复用 GitCode 官方三种方式。

**新增表 `api_tokens`**（用户级 Token，绑定单用户）：

```sql
CREATE TABLE IF NOT EXISTS api_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,                  -- 绑定单用户
    token_sha256 TEXT UNIQUE NOT NULL,             -- SHA-256（不存明文）
    name        TEXT DEFAULT '',                   -- token 名称（如 "worker-calc-01"）
    scopes      TEXT DEFAULT '["public"]',         -- 预留：未来 RBAC 扩展
    created_at  TEXT DEFAULT (datetime('now')),
    last_used_at TEXT DEFAULT NULL,
    revoked     INTEGER DEFAULT 0,
    UNIQUE (user_id, name)
)
```

**新增 `src/api/auth_token.py`**：

```python
def generate_api_token(user_id: int, name: str = "") -> str: ...
def verify_api_token(token: str) -> dict | None: ...
def revoke_api_token(user_id: int, token_id: int) -> bool: ...
def list_api_tokens(user_id: int) -> list[dict]: ...
```

**认证流程（三通道统一入口，`src/api/deps.py` 新增）**：

```python
def get_user_by_api_token(request: Request) -> dict:
    """Authorization: Bearer / PRIVATE-TOKEN / access_token query 三通道取值。"""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif request.headers.get("PRIVATE-TOKEN"):
        token = request.headers.get("PRIVATE-TOKEN").strip()
    else:
        token = request.query_params.get("access_token", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "401 Unauthorized"})
    info = verify_api_token(token)
    if not info:
        raise HTTPException(status_code=401, detail={"code": 401, "message": "401 Unauthorized"})
    mark_user_active(info["user_id"])
    return {"id": info["user_id"], "username": info["username"], "auth_type": "api_key"}
```

- **与现有 JWT 的关系**：三通道 API Key + 现有 `get_current_user`（JWT）并存。对外接口用 `get_current_user_or_api_key`（二选一）。
- **安全**：明文仅生成时返回一次；落库 sha256；支持吊销；按 worker/集成方命名。

### 3.2 对外接口认证接入

对外路由（tasks/repos/agents/chat/skills/scenes/knowledge/events）的 Depends 替换为 `get_current_user_or_api_key`；管理类保持 `get_current_user`。

### 3.3 OpenAPI 对外暴露与版本管理

**`src/api/main.py` 开启文档**：

```python
app = FastAPI(
    title="Agentic DevOps API",
    version="2.1.0",
    docs_url="/docs", redoc_url="/redoc", openapi_url="/openapi.json",
)
```

**custom_openapi 注入三通道 securitySchemes**（`src/api/openapi.py`，见 3.4）：

```python
from fastapi.openapi.utils import get_openapi
def custom_openapi():
    if app.openapi_schema: return app.openapi_schema
    schema = get_openapi(title="Agentic DevOps API", version="2.1.0", routes=app.routes)
    schema["components"]["securitySchemes"] = {
        "apiKey":      {"type": "http", "scheme": "bearer"},
        "privateKey":  {"type": "apiKey", "in": "header", "name": "PRIVATE-TOKEN"},
        "accessToken": {"type": "apiKey", "in": "query", "name": "access_token"},
    }
    app.openapi_schema = schema
    return schema
app.openapi = custom_openapi
```

- **访问控制**：`/docs`、`/openapi.json` 对外公开（只读元数据）；生产经 nginx 443 暴露。

### 3.4 接口定义归属（决策：分散于 routes/*.py，openapi.py 统一渲染）

| 职责 | 放置 |
|------|------|
| 接口端点 | 各 `src/api/routes/*.py`（现状，不迁移；公开由认证依赖区分） |
| 对外认证依赖 | `src/api/deps.py`（`get_current_user_or_api_key`） |
| OpenAPI 渲染 | `src/api/openapi.py`（新建：custom_openapi + securitySchemes + 版本；不定义新端点） |
| token 校验/DB | `src/api/auth_token.py`（新建） |

> 不采用统一 public router：避免业务逻辑搬移造成重复与维护撕裂；改动最小。

### 3.5 新增 OpenAPI 接口的流程（服务端一次定义，CLI 自动获得命令）

> 闭环：**新增接口只需在服务端定义** → OpenAPI 契约自动更新 → CLI（契约驱动，方案 B）自动获得命令，**无需改 CLI 代码**。见 `issue-45-cli-design.md` §三。

#### 流程（服务端侧）

```
① 在 routes/<module>.py 新增接口
   - 使用 Depends(get_current_user_or_api_key)  → 对外（否则 internal，不进契约）
   - 声明 response_model / tags / summary / operationId（建议显式，便于契约生成命令）
     例: @router.get("/mine", tags=["tasks"], operation_id="tasks_mine", response_model=TaskList)
   ↓
② openapi.py custom_openapi 遍历 app.routes
   - 自动收录该接口（含 security=apiKey、参数 schema、response_model）
   - 版本号 bump（2.1.0 → 2.2.0 ...）
   - 过滤内部接口（Depends=get_current_user 的 → 不进对外契约）
   ↓
③ 发布后 /openapi.json 立即可访问新接口契约
   ↓
④ CLI（grape）检测到契约版本变化 → 自动拉新 → 新 operation 注册为命令
   → 直接可用 `grape tasks mine`：OperationId（tasks_mine）拆为 `grape <tag> <verb>`（零 CLI 代码改动）
```

#### 接口声明规范（契约友好）

| 项 | 要求 | 作用 |
|----|------|------|
| `tags` | 模块名（tasks/repos/...） | CLI 分组（可覆盖；默认 tag_命令） |
| `operation_id` | `tasks_mine` 等（唯一，snake_case） | CLI 命令名（拆为 `grape <tag> <verb>`） |
| `response_model` | 建议声明 | OpenAPI 出参 schema（CLI `--json` 展示） |
| `summary`/`description` | 建议 | 文档与 CLI `--help` |
| `security`/`Depends` | `get_current_user_or_api_key` | 标对外；`get_current_user` 标内部（不对外） |

#### 关键点

- **新增接口 = 一次服务端定义**，CLI 自动跟进（方案 B 契约驱动）。
- 内部接口（管理面）**不进契约** → CLI 不生成命令，天然不外泄。
- 版本变化触发 CLI 契约更新：`2.X.Y` bump 即可。

## 四、改动清单（API 部分）

| # | 模块 | 改动 | 优先级 |
|---|------|------|--------|
| 1 | `src/api/db_setup.py` | 新增 `api_tokens` 表 | P0 |
| 2 | 新增 `src/api/auth_token.py` | generate/verify/revoke/list | P0 |
| 3 | `src/api/deps.py` | `get_current_user_or_api_key`（二选一）+ 三通道 | P0 |
| 4 | 对外路由（tasks/repos/agents/chat/skills/scenes/knowledge/events） | 认证依赖切换为二选一 | P1 |
| 5 | `src/api/main.py` | 开启 docs_url/redoc_url/openapi_url | P0 |
| 6 | 新增 `src/api/openapi.py` | custom_openapi + securitySchemes + 版本 2.1.0 + **operationId 收集 / 内部接口过滤 / 契约含 CLI 生成所需信息**（§3.5 闭环 CLI） | P0 |
| 7 | Token 管理（内部） | Settings 页「API Tokens」面板（会话认证，不对外） | P1 |
| 8 | `tests/` | token 认证 + 二选一 | P1 |

## 五、验收标准

- [ ] 第三方可通过 Token/API Key（三通道）认证调用业务 API
- [ ] 可访问 OpenAPI 文档（Swagger UI/ReDoc/JSON）并据此生成客户端 SDK
- [ ] 对外接口有明确文档与版本管理（2.1.0 + securitySchemes）
- [ ] Token 管理仅在 Web UI（会话），API Key 不能自举
- [ ] 现有测试全绿 + 新增用例通过

## 六、风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 开启 /docs 暴露内部端点 | 中 | 管理类路由仍会话认证；openapi.py 可只呈现带 security 的路由 |
| API Key 明文泄露 | 高 | 明文仅创建返回一次；sha256 落库；可吊销 |
| 二选一认证误放管理接口 | 中 | 仅改对外路由依赖；评审把关 |

## 附：关键代码位置

- OpenAPI 当前关闭：`src/api/main.py` L240-247
- 现有会话认证：`src/api/deps.py` `get_current_user` L23-49
- 路由挂载：`src/api/main.py` L402-422
- 认证分离参考：`src/api/deps.py` `TOKEN_TYPE_DEVOPS/USER/WEBHOOK` L5-7