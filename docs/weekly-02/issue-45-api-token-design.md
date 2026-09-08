# Issue #45 设计文档：API Token 认证机制（会话级临时 + 用户级长期）

> Issue：https://gitcode.com/grape-dev/grape/issues/45
> 类型：Feature（API 认证）| P1 | 状态：spec-ready
> 分支：`weekly-02`
> 日期：2026-09-07
> 范围：grape 对外业务 API 的 **Token 认证机制**——会话级临时 token（A，首选）+ 用户级长期 API Key（B，补充）。
> 从 `issue-45-openapi-api-design.md` §3.1 抽出独立成文，便于聚焦评审与实现。

## 关联文档

- OpenAPI 暴露与接口归属：`docs/weekly-02/issue-45-openapi-api-design.md`
- CLI 设计：`docs/weekly-02/issue-45-cli-design.md`
- Git 仓库统一接口：`docs/weekly-02/issue-45-git-repos-api-design.md`
- 接口级参考：`docs/openapi/00-auth.md`

## 一、目标

worker/chat agent 及三方软件可通过 **Token** 认证访问对外业务 API，无 JWT 会话也能程序化调用。

- **方式 A（会话级临时 token）**：worker/chat agent 执行期自动签发，零配置、用完即失效。
- **方式 B（用户级长期 API Key）**：三方软件 / CI / 外部 SDK 长期集成。

传输姿势对齐 GitCode 官方三通道：`Authorization: Bearer` / `PRIVATE-TOKEN` / `?access_token=`。

---

## 二、方式 A：会话级临时 Token（首选，P0）

### 2.1 设计决策

> **worker/agent 访问对外业务 API 采用「会话级临时 token」**——按 **仓库 × agent 类型** 为主作用域签发、执行期间有效、用完即失效；经现有环境变量注入通道下发，**零额外配置、防泄露**。

**授权身份 = repo_key + agent_type（主）**：
- **devops 事件 agent（vod/issue/coding/qa/sre）没有 user_id 概念**：由仓库事件/管道触发、以**仓库 bot 身份**（`BOT_GITCODE_TOKEN`）执行，agent 并不知道、也不需要知道用户是谁 → 主作用域就是**仓库**。
- **user_id 仅 chat agent 可选绑定**：chat 以**用户身份**（`USER_GITCODE_TOKEN`）执行、env 注入 `USER_ID`，此时才带用户身份。

**为何不绑 task_id**：
- **chat agent（对话）没有任务概念** → 授权身份不用 task_id。
- `session_key`（task_id / conv_id）仅作**管理键**（失效时精确定位、避免并发同类会话误删），**不是授权身份**。
- 临时 token 关联 repo_key + agent_type（+ chat 可选 user_id）→ 任务（enqueue）与对话（chat agent）都适用，代表"该仓库、该类型 agent 执行期间的访问凭据"。

### 2.2 机制

```
任务 / 对话执行（enqueue / chat agent 启动）
  │ 签发临时 token（tk_<rand>）绑定 (repo_key, agent_type[, user_id for chat])
  ▼
注入 agent 环境变量：GRAPE_API_TOKEN=tk_xxx（复用现有 BOT_GITCODE_TOKEN 注入通道）
  ▼
agent 执行中用该 token 调 {{SERVER_URL}}/api/...（三通道携带）
  ▼
执行结束（任务完成/失败/取消；对话关闭） → token 删除即时失效（DELETE，无需标记）
```

### 2.3 数据表 `session_api_tokens`

**存在即有效，删除即失效**（无 revoked 标记位）：

```sql
CREATE TABLE IF NOT EXISTS session_api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key  TEXT NOT NULL,             -- 会话唯一键（task_id / conv_id），失效时精确定位
    repo_key     TEXT NOT NULL,             -- 仓库（主作用域）
    agent_type   TEXT NOT NULL,             -- vod/issue/coding/qa/sre/chat
    user_id      INTEGER DEFAULT 0,         -- 仅 chat agent（用户身份）绑定；devops 事件 agent 无此概念，0=仓库身份
    token_sha256 TEXT UNIQUE NOT NULL,      -- SHA-256
    expires_at   TEXT NOT NULL,             -- 签发时刻 + TTL（校验超时用）
    created_at   TEXT DEFAULT (datetime('now'))
)
```

### 2.4 签发 / 注入 / 失效

- **签发**：执行会话建立时（任务入队 / chat agent 启动）生成 `tk_<rand32>`，sha256 落 `session_api_tokens`，`session_key` 记录会话（task_id / conv_id）；明文仅注入 env，不落盘。
- **注入**：`env_extra["GRAPE_API_TOKEN"]=tk_xxx`（与 BOT_GITCODE_TOKEN 同通道）。
- **校验**：sha256 查表 → **行存在且未过期即有效**；返回作用域（repo_key + agent_type，chat 另含 user_id）。
- **失效 = 删除该行**：执行结束时 `DELETE FROM session_api_tokens WHERE session_key=?`（精确定位该会话，**不影响同仓库/同类型的并发会话**）；TTL 兜底清理过期行。

### 2.5 身份语义与作用域

token 的**授权身份** = **repo_key + agent_type** → 天然限制为"该仓库、该类型 agent 的执行期访问"。

- **devops 事件 agent**（vod/issue/coding/qa/sre）：仅仓库身份，**无 user_id**（agent 内不暴露用户环境变量）；侧查业务 API 时身份=仓库。
- **chat agent**：额外绑定 **user_id**（用户身份，env 已注入 `USER_ID`）；侧查会话类接口（如 /api/chat/conversations）时才带用户上下文。
- `session_key` 只用于失效定位，**不参与**授权身份判断。

**示例（agent 内）**：
```bash
curl -s -H "Authorization: Bearer $GRAPE_API_TOKEN" \
  {{SERVER_URL}}/api/tasks/create ...
```

### 2.6 优势

- token 生命周期 = 执行会话生命周期；泄露影响面=当前会话；
- 无 Web UI 管理面；worker 零配置；
- 可直接复用 BOT_GITCODE_TOKEN 注入通道；
- 任务与 chat 对话通用。

---

## 三、方式 B：用户级长期 API Key（补充，P1）

### 3.1 适用

> **三方软件 / CI / 外部 SDK 长期调用**（外部无 grape 执行会话，无法用会话级临时 token）。**worker agent 不使用**此方式（用 A）。

### 3.2 数据表 `api_tokens`

```sql
CREATE TABLE IF NOT EXISTS api_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,                  -- 绑定单用户
    token_sha256 TEXT UNIQUE NOT NULL,             -- SHA-256（不存明文）
    name        TEXT DEFAULT '',                   -- token 名称（如 "ci-token-01"）
    scopes      TEXT DEFAULT '["public"]',         -- 预留：未来 RBAC 扩展
    created_at  TEXT DEFAULT (datetime('now')),
    last_used_at TEXT DEFAULT NULL,
    revoked     INTEGER DEFAULT 0,
    UNIQUE (user_id, name)
)
```

### 3.3 `src/api/auth_token.py`

```python
def generate_api_token(user_id: int, name: str = "") -> str: ...
def verify_api_token(token: str) -> dict | None: ...
def revoke_api_token(user_id: int, token_id: int) -> bool: ...
def list_api_tokens(user_id: int) -> list[dict]: ...
```

---

## 四、认证流程（统一入口，`src/api/deps.py`）

三通道取值后，按 token 前缀分流：

```python
def get_user_or_device_by_token(request: Request) -> dict:
    token = _extract_three_channel(request)    # Bearer / PRIVATE-TOKEN / access_token
    if token.startswith("tk_"):                # 会话级临时 token（A）
        info = verify_session_token(token)     # session_api_tokens 校验（repo+agent，chat 另含 user）
        return {**info, "auth_type": "session_token"}
    info = verify_api_token(token)             # 用户级 API Key（B）
    if not info: raise 401
    return {**info, "auth_type": "api_key"}
```

- **与现有 JWT 的关系**：方式 A/B + JWT（`get_current_user`，Web UI 会话）并存。对外接口用 `get_current_user_or_api_key`（二选一：三种任一通过）。
- **安全**：明文仅生成时返回一次；落库 sha256；支持吊销；**优先会话级临时 token（A）**，长期 API Key（B）仅外部集成。

---

## 五、改动清单

| # | 模块 | 改动 | 优先级 |
|---|------|------|--------|
| 1 | `src/api/db_setup.py` | 新增 `session_api_tokens`（会话级，方式 A）+ `api_tokens`（用户级，方式 B） | P0 |
| 2 | 新增 `src/api/auth_token.py` | session token：issue/verify/revoke（按 `session_key` 定位；作用域=repo+agent，chat 另带 user）；api key：generate/verify/revoke/list | P0 |
| 3 | `src/api/event_pipeline.py` / `runtime` | 会话启动时签发临时 token + 注入 `GRAPE_API_TOKEN` env；会话结束时删除 | P0 |
| 4 | `src/api/deps.py` | `get_current_user_or_api_key`（JWT / session token / API Key 三选一，含三通道） | P0 |
| 5 | `tests/` | session token 签发/失效 + API Key + JWT 三选一 | P1 |

## 六、验收标准

- [ ] **会话级临时 token（A）**：会话签发 `GRAPE_API_TOKEN` → agent 调业务 API；会话结束（任务完成/失败/取消、对话关闭）后 token 失效
- [ ] **用户级 API Key（B）**：三通道认证可调业务 API（外部集成）
- [ ] `tk_`（session）与 `grape_`（api key）与 JWT 三种认证并存、互不影响
- [ ] Token 管理仅在 Web UI（会话），API Key 不能自举
- [ ] 测试全绿

## 七、测试设计

| # | 用例 | 断言 |
|---|------|------|
| T1 | 会话签发临时 token | 会话建立后 `session_api_tokens` 有行（session_key+repo+agent；chat 另带 user_id，devops 无），sha256 落库，明文只经 env |
| T2 | agent 用 `GRAPE_API_TOKEN` 调业务 API | 通过，auth_type=session_token + 携带 repo/agent 上下文（chat 另含 user） |
| T3 | 会话结束（任务完成/对话关闭）token 失效 | 再调 → 401 |
| T4 | 超 TTL token 失效 | 过期后 → 401 |
| T5 | 撤销/失败结束会话 token | 同 T3 |
| T6 | 用户级 API Key（B）三通道 | Bearer / PRIVATE-TOKEN / access_token 均有效 |
| T7 | JWT 会话仍可用 | 通过 auth_type=jwt |
| T8 | session token 作用域 | 仅限对应 repo+agent 组合（chat 另限 user）；**chat agent（无任务）也可用**，跨会话互不通用 |
| T9 | token 泄露出 env? | 明文不落盘；日志/输出脱敏 |

## 八、风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 会话级临时 token 泄露 | 低→中 | 生命周期=执行会话；TTL 兜底；会话结束即删除失效；明文不落盘（T9） |
| 用户 API Key 明文泄露 | 高 | 明文仅创建返回一次；sha256 落库；可吊销；仅方式 B（外部集成）用 |
| 三选一认证误放管理接口 | 中 | 仅改对外路由依赖；评审把关 |

## 九、关键代码位置

- 会话注入通道复用：`src/api/event_pipeline.py` `_execute_agent` env_extra（BOT_GITCODE_TOKEN 同处）
- 现有会话认证：`src/api/deps.py` `get_current_user` L23-49
- 认证分离参考：`src/api/deps.py` `TOKEN_TYPE_DEVOPS/USER/WEBHOOK` L5-7