# Grape API Token 作用域分类优化设计方案

> 目标：对 grape 对外 API 的 token 认证做**作用域分类**，收敛各接口允许的 token 类型
> （`session_token`（tk_）/ `user_token`（JWT / Grape API Key）），提升系统安全与职责隔离。
> 参照：CLI 命令分类（Core workflows / Agent Context / Code & repositories / Account & platform）
> 分支：`weekly-02-lyz`；状态：设计草案；日期：2026-09-14

---

## 一、Token 类型与当前认证现状

### 1.1 三类 token

| token | 前缀 | 载体 | 语义 |
|-------|------|------|------|
| **session_token** | `tk_` | `session_api_tokens` 表 | agent/worker 执行期临时凭据（按 repo+agent 签发），代表「该仓库该 agent 的执行身份」 |
| **user_token (API Key)** | `grape_` | `api_tokens` 表 | 用户长期 API Key（CLI worker 用） |
| **JWT（Web 登录）** | Bearer JWT | JWT access token | Web 登录用户身份；**本次变更豁免，维持现状放行** |

> **本方案变更范围**：**只约束 `tk_`（session_token）与 `grape_`（API Key）两类**的允许端点；
> **JWT（Web 登录）不受影响**——保持 `get_current_user_or_api_key` 现状放行，避免 Web 页面因收紧而损坏
> （前端页面全部走 JWT，若 JWT 也收紧会导致「我的需求」/仓库/技能等页面 403）。
>
> `auth_type` 区分：`session_token` / `api_key` / `jwt`；本次规则**仅对前两类生效，`jwt` 直接放行**。

### 1.2 当前契约全变量（44 paths）

**契约层（`/openapi.json`）目前所有公开端点 security 均三通道**——即契约不区分 token 类型。
**真实区分在代码内**：
- `git-repo/*`、`repos git-credential/workspace`：`_repo_token` **强制 session_token**（api_key/jwt → 403）
- `tasks/notify` 等管理端点：`_require_bot_token`（bot 专用）
- 其余大多数：三通道均放行

---

## 二、目标分类（按 CLI 命令分组）

### 2.1 四类作用域

| 分类 | 英文 | session/API Key 规则 | 安全语义 |
|------|------|---------------------|----------|
| **核心工作流** | Core workflows | **仅 session_token（API Key 拒绝）** | agent 驱动的任务动作（创建/推进/完成/交接），只能由执行期 agent 触发 |
| **Agent 上下文** | Agent Context | **session_token + API Key 均可用** | 只读上下文（issue/events/knowledge 详情）；创建走 issues（双兼容） |
| **代码与仓库** | Code & repositories | **仅 session_token（API Key 拒绝）** | 本地 git / 平台 repo 透传，凭据敏感，仅 agent 会话可用 |
| **账户与平台** | Account & platform | **仅 API Key（session_token 拒绝）** | 用户级平台资源与配置（agents/auth/chat/repos/scenes/skills），仅用户/管理身份 |

> **JWT 三个分类通用豁免**：以上规则仅作用于 `tk_` 与 `grape_`；JWT 在任何分类下都保持现状放行。

### 2.2 端点归属建议映射

#### A. Core workflows — 仅 session_token（API Key 拒绝；JWT 豁免放行）

| 端点 | operationId | 当前认证 | 需改 |
|------|--------------|----------|:---:|
| `POST /api/tasks/complete` | tasks_complete | get_current_user_or_api_key | 收紧（API Key→403） |
| `POST /api/tasks/{task_id}/start` | tasks_start | get_current_user_or_api_key | 收紧 |
| `GET/PUT /api/tasks/{task_id}/status` | tasks_status / tasks_status_get | get_current_user_or_api_key | 收紧 |
| `GET /api/tasks/mine` | tasks_mine | get_current_user_or_api_key | 收紧 |
| `GET /api/tasks/repo/{owner}/{repo}` | tasks_repo_all | get_current_user_or_api_key | 收紧 |
| `POST /api/tasks/notify` | tasks_notify(内部) | `_require_bot_token` | 已 session/内部，保留 |
| `POST /api/a2a` | a2a_handoff | get_current_user_or_api_key | 收紧到 session |

> **A 类 JWT 豁免说明**：`GET /api/tasks/mine` 等 Web「我的需求」页面走 JWT——因 JWT 豁免放行，**前端页面不受影响**；
> 仅 `grape_` API Key 在本类被拒（防外部长期 key 冒充 agent 操作任务）。

#### B. Agent Context — session + API Key 双兼容（JWT 豁免放行）

| 端点 | operationId |
|------|--------------|
| `GET /api/issues/{...}`（list/detail/events/pipeline） | issues_list/detail/events/pipeline |
| `POST /api/issues/{owner}/{repo}`（create 入口） | issues_create |
| `GET /api/events/detail/{seq}` | events_detail |
| `GET /api/knowledge/{content,context,list,stats,detail}` | knowledge_* |

#### C. Code & repositories — 仅 session_token（API Key 拒绝；JWT 豁免）

| 端点 | operationId | 当前 | 需改 |
|------|--------------|------|:---:|
| `/api/git-repo/*`（全部透传） | gitrepo_* | 已强制 session（`_repo_token`，api_key/jwt→403） | 保留 |
| `/api/repos/{owner}/{repo}/git-credential` | repos_git_credential | 已强制 session | 保留 |
| `/api/repos/{owner}/{repo}/workspace` | repos_workspace | 已强制 session | 保留 |

> 注：C 类目前 `_repo_token` 连 JWT 也拒（403）——如需 JWT 豁免（Web 页面读平台透传），按 §3.1 调整；默认保持现状（C 仅 agent 会话）。

#### D. Account & platform — 仅 API Key（session_token 拒绝；JWT 豁免放行）

| 端点 | operationId | 当前 | 需改 |
|------|--------------|------|:---:|
| `GET /api/agents/{name}/{owner}/{repo}` | agents_detail | or_api_key | 收紧（session→403） |
| `GET /api/auth/profile` | profile | or_api_key | 保留（JWT/API Key） |
| `GET /api/chat/conversations|messages` | chat_conversations/messages | or_api_key | 收紧（session→403） |
| `GET /api/repos/member|managed|deploy-url` | repos_member/managed/deploy_url | or_api_key | 收紧（session→403） |
| `GET /api/scenes` | scenes_list | or_api_key | 收紧（session→403） |
| `GET /api/skills|skills/by-id` | skills_list/detail | or_api_key | 收紧（session→403） |

> **D 类 JWT 豁免说明**：Web 页面「仓库管理/场景/技能」均走 JWT——JWT 照常放行，仅 **API Key 允许 / session_token 拒绝**。

---

## 三、实现方式（分层）

### 3.1 依赖注入口新增 scope 控制

在 `src/api/deps.py` 新增依赖函数，替代 `get_current_user_or_api_key` 的使用点：

```python
def _auth_type(request: Request) -> str:
    return get_current_user_or_api_key(request).get("auth_type", "")

def require_session_token(request: Request) -> dict:
    """session_token 专用；API Key → 403；JWT 豁免放行（Web 页面保持现状）。"""
    user = get_current_user_or_api_key(request)
    if user.get("auth_type") == "api_key":
        raise HTTPException(403, detail={"code": 403, "message": "该接口不支持 API Key（会话 token 专用）"})
    return user  # session_token / jwt 均放行

def require_user_token(request: Request) -> dict:
    """API Key 专用；session_token → 403；JWT 豁免放行（Web 页面不损）。"""
    user = get_current_user_or_api_key(request)
    if user.get("auth_type") == "session_token":
        raise HTTPException(403, detail={"code": 403, "message": "该接口不支持会话 token（用户 API Key 专用）"})
    return user  # api_key / jwt 均放行
```

### 3.2 各端点替换 Depends

- A 类：`get_current_user_or_api_key` → `require_session_token`
- D 类：`get_current_user_or_api_key` → `require_user_token`
- B 类：保持 `get_current_user_or_api_key`（双兼容）
- C 类：已强制 session（`_repo_token` 兜底），保留

### 3.3 契约层标注（可选，增强可发现性）

在 `custom_openapi` 中按 operationId 分组对特定端点注入 `x-token-scope`（session/user/dual）扩展字段，
供 CLI 契约驱动生成时约束命令的 token 类型（非必选，代码级已保证安全性）。

---

## 四、约定与原则

1. **session_token 用途**：agent 执行期（pipeline/worker/A2A）的动作与敏感凭据访问。
2. **API Key（`grape_`）用途**：用户/CLI 长期凭据；按其端点分类（核心工作流/上下文/账户平台）约束。
3. **JWT 豁免**：Web 登录 JWT **不参与本次规则**——任何分类都保持现状放行，防止页面因收紧损坏。
4. **双兼容用途**：部分端点既需要 agent 读取（session）又允许用户只读（API Key/JWT）→ 放行两者。
5. **默认从严**：无法一眼归类的端点按「更低权限」处理（能 API Key 就不用 session 的，归 D；反之归 A）。
6. **不破坏现有流程**：先保证 agent/skill/CLI/Web 实际调用路径不因收紧断掉（见 §六 验证）。

---

## 五、影响面与风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| A 类收紧后 API Key 调用断 | CLI/外部用 `grape_` 调 tasks 动作会 403 | 符合预期（任务动作仅 agent 会话）；CLI 若需用户操作改走 JWT/或用 session |
| D 类收紧后 session 调用断 | agent 用 session 读 skills/scenes 会 403 | 确认 agent 技能是否只走 B/C/A 类端点；若需读取归 B |
| **Web JWT 不受影响** | — | JWT 豁免，页面（mine/repo/scene/skill）稳定 |
| 契约不标 scope | CLI 契约驱动无法感知 | 加 `x-token-scope` 扩展（§3.3） |

### 需重点核对（实施前）
- CLI `grape tasks mine` / `grape issues ...` 实际携带的 token 类型（决定是否收紧/归 B）。
- `events/detail` 是否被 session 与 API Key 都用。
- agent 技能在 D 类端点（skills/scenes）的读取是否改为走 B 或 JWT。

---

## 六、验证

1. 依赖替换后各端点冒烟：session_token / API Key / JWT 三种认证各调代表性端点，断言符合分类（且 JWT 全放行）。
2. `test_openapi` / help 契约更新。
3. 前端页面（Web JWT）回归：需求中心 mine、仓库管理、场景、技能——**应全部正常（JWT 豁免）**。
4. CLI 回归：tasks 操作（session）、issues 查询（session/API Key）、git-repo（session）。
5. 全量 pytest。

---

## 七、待确认项

- [x] ~~`tasks/mine` 前端用 JWT，收紧 session 是否波及「我的需求」页~~ → **已解**：JWT 豁免，mine 收紧仅拒 API Key，页面不受影响
- [ ] `events/detail` 用途（agent 读取 vs 用户查看）——决定其归 B 或 A
- [ ] `a2a_handoff` 是否仅 agent 触发（→ A 仅 session）
- [ ] CLI 各命令实际携带的 token 类型表（决定是否需在 CLI 侧调整调用的 token）