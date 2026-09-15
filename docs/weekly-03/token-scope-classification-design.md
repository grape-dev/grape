# Grape API Token 作用域分类优化设计方案

> 目标：为 grape 对外 API 的 **session_token（tk_）** 定义作用域边界——`账户/平台管理`类端点
> 禁用 session_token（403），防止 agent 会话凭据用于管理操作。
> **本次仅约束 session_token**；`grape_`（API Key）与 JWT（Web 登录）**维持现状，不做任何变更**。
> 参照：CLI 命令分类（Core workflows / Agent Context / Code & repositories / Account & platform）
> 分支：`weekly-02-lyz`；状态：**已实施并上线生产（2026-09-14）**；日期：2026-09-14

---

## 一、Token 类型与当前认证现状

### 1.1 三类 token

| token | 前缀 | 载体 | 语义 | 本次是否变更 |
|-------|------|------|------|:---:|
| **session_token** | `tk_` | `session_api_tokens` 表 | agent/worker 执行期临时凭据（按 repo+agent 签发） | ✅ **本次唯一被约束** |
| **user_token (API Key)** | `grape_` | `api_tokens` 表 | 用户长期 API Key（CLI worker 用） | ❌ **维持现状** |
| **JWT（Web 登录）** | Bearer JWT | JWT access token | Web 登录用户身份 | ❌ **维持现状** |

> **本方案变更范围**（最终）：**只约束 `tk_`（session_token）的允许端点**——对不可用 session_token 的端点
> （D 类账户/平台管理）收紧为「session_token → 403」。
>
> **`grape_`（API Key）与 JWT 本次均不变，维持现状放行**——避免 CLI/外部 Web 页面因收紧而损坏。
>
> `auth_type` 区分：`session_token` / `api_key` / `jwt`；本次只对 `session_token` 生效。

### 1.2 当前契约全变量（44 paths）

**契约层（`/openapi.json`）目前所有公开端点 security 均三通道**——即契约不区分 token 类型。
**真实区分在代码内**：
- `git-repo/*`、`repos git-credential/workspace`：`_repo_token` **强制 session_token**（api_key/jwt → 403）
- `tasks/notify` 等管理端点：`_require_bot_token`（bot 专用）
- 其余大多数：三通道均放行

---

## 二、目标分类（按 CLI 命令分组）

### 2.1 四类作用域

| 分类 | 英文 | session_token 规则 | API Key / JWT |
|------|------|-------------------|---------------|
| **核心工作流** | Core workflows | **允许**（agent 驱动任务动作） | 维持现状（均放行） |
| **Agent 上下文** | Agent Context | **允许**（只读上下文 + issues 创建） | 维持现状（均放行） |
| **代码与仓库** | Code & repositories | **允许（现状已是强制 session）** | 维持现状（git-repo 现状 API Key/JWT 404；不扩大） |
| **账户与平台** | Account & platform | **拒绝（新增 session→403）** | 维持现状（均放行） |

> **本次变更 = 仅 D 类对 `session_token` 返回 403**；A/B/C 允许 session_token；
> `grape_` 与 JWT 在所有分类维持现状，不做任何收紧/放宽。

### 2.2 端点归属建议映射

#### A. Core workflows — session_token 允许（本次不变）

| 端点 | operationId | 当前认证 | 本次 |
|------|--------------|----------|:---:|
| `POST /api/tasks/complete` | tasks_complete | get_current_user_or_api_key | 维持现状 |
| `POST /api/tasks/{task_id}/start` | tasks_start | get_current_user_or_api_key | 维持现状 |
| `GET/PUT /api/tasks/{task_id}/status` | tasks_status / tasks_status_get | get_current_user_or_api_key | 维持现状 |
| `GET /api/tasks/mine` | tasks_mine | get_current_user_or_api_key | 维持现状 |
| `GET /api/tasks/repo/{owner}/{repo}` | tasks_repo_all | get_current_user_or_api_key | 维持现状 |
| `POST /api/tasks/notify` | tasks_notify(内部) | `_require_bot_token` | 已 session/内部，保留 |
| `POST /api/a2a` | a2a_handoff | get_current_user_or_api_key | 维持现状 |

> **A 类本次不变**：session_token 允许；`grape_` 与 JWT 维持现状（均放行）——不新增任何收紧。

#### B. Agent Context — session + API Key + JWT（本次均维持现状）

| 端点 | operationId |
|------|--------------|
| `GET /api/issues/{...}`（list/detail/events/pipeline） | issues_list/detail/events/pipeline |
| `POST /api/issues/{owner}/{repo}`（create 入口） | issues_create |
| `GET /api/events/detail/{seq}` | events_detail |
| `GET /api/knowledge/{content,context,list,stats,detail}` | knowledge_* |

#### C. Code & repositories — 现状仅 session（本次不变）

| 端点 | operationId | 当前 | 本次 |
|------|--------------|------|:---:|
| `/api/git-repo/*`（全部透传） | gitrepo_* | 已强制 session（`_repo_token`） | 保留现状 |
| `/api/repos/{owner}/{repo}/git-credential` | repos_git_credential | 已强制 session | 保留现状 |
| `/api/repos/{owner}/{repo}/workspace` | repos_workspace | 已强制 session | 保留现状 |

> 注：C 类目前 `_repo_token` 连 API Key/JWT 也 403，属现状；**本次不扩大/不放开**（保持仅 agent 会话）。

#### D. Account & platform — **本次唯一变更：session_token 拒绝（session→403）**；API Key / JWT 维持现状

| 端点 | operationId | 当前 | 本次 |
|------|--------------|------|:---:|
| `GET /api/agents/{name}/{owner}/{repo}` | agents_detail | or_api_key | **session→403** |
| `GET /api/auth/profile` | profile | or_api_key | **session→403** |
| `GET /api/chat/conversations|messages` | chat_conversations/messages | or_api_key | **session→403** |
| `GET /api/repos/member|managed|deploy-url` | repos_member/managed/deploy_url | or_api_key | **session→403** |
| `GET /api/skills|skills/by-id` | skills_list/detail | or_api_key | **session→403** |

> ❗ **`/api/scenes`（list）不在 D 类**：VOD 技能（`skills/vod/vod/references/grape-cli-reference.md`）
> 用 `$GRAPE_SESSION_TOKEN` 调 `grape api GET /api/scenes`（通用兜底），若将 scenes 归 D 类（session→403）
> 会**破坏 VOD 技能**。故 scenes 维持现状（session/API Key/JWT 均放行）。
>
> **D 类说明**：账户/平台管理属用户身份范畴，**agent 会话 token（tk_）不得用于此类管理端点** → 本次对
> session_token 返回 403；`grape_`（API Key）与 JWT（Web 页面：仓库管理/技能）**维持现状放行**。

---

## 三、实现方式（分层）

### 3.1 依赖注入口新增 scope 控制

在 `src/api/deps.py` 新增一个依赖函数，仅用于 **D 类（账户/平台）** 端点：

```python
# src/api/deps.py（已实施）
def require_no_session_token(user: dict = Depends(get_current_user_or_api_key)) -> dict:
    """账户/平台管理端点：session_token（tk_）→ 403；API Key / JWT 维持现状放行。
    （经 Depends 复用 get_current_user_or_api_key，override 可注入、测试友好。）"""
    if user.get("auth_type") == "session_token":
        raise HTTPException(status_code=403, detail={"code": 403,
                                                     "message": "该接口不支持会话 token（agent 凭据不能用于账户/平台管理）"})
    return user  # api_key / jwt 均放行（维持现状）
```

### 3.2 各端点替换 Depends

- **D 类**：`get_current_user_or_api_key` → **`require_no_session_token`**（唯一本次变更点）
- A / B 类：**保持 `get_current_user_or_api_key` 现状**（本次不变）
- C 类：已强制 session（`_repo_token` 兜底），保留现状

### 3.3 契约层标注（可选，增强可发现性）

在 `custom_openapi` 中按 operationId 分组对特定端点注入 `x-token-scope`（session/user/dual）扩展字段，
供 CLI 契约驱动生成时约束命令的 token 类型（非必选，代码级已保证安全性）。

---

## 四、约定与原则

1. **本次唯一变更**：D 类（账户/平台管理）端点对 `session_token`（tk_）返回 403。
2. **session_token 语义**：agent 执行期（pipeline/worker/A2A）临时凭据——不用于账户/平台管理。
3. **API Key（`grape_`）与 JWT 维持现状**：不发生任何收紧/放宽，防 CLI/Web 页面损坏。
4. **双兼容维持**：A/B 类保持三通道现状（session + API Key + JWT 均放行），不新增约束。
5. **不破坏现有流程**：确认无 agent 用 session 调 D 类端点后才收紧（见 §五/§六）。

---

## 五、影响面与风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| agent/skill 现用 session 调 D 类端点 | D 类收紧 session→403 后 agent 断 | 实施前核对 agent 是否只走 B/C/A（§六验证） |
| CLI 用 session 调 skills/scenes | 收紧后 403 | CLI 若用 session 读平台管理 → 改用 API Key 或 JWT；核对 CLI token 表 |
| **Web JWT / API Key 受影响** | — | **不受影响**（本次不收紧二者） |

### 需重点核对（实施前）
- agent 技能是否用 session 调用 D 类（skills/scenes/repos-managed/profile）——若有，需先改为 API Key/JWT。
- `a2a_handoff` / `events/detail` 现状不被改动（维持）。
- CLI 对 D 类端点（skill/scene/repo 查询）实际携带的 token 类型。

---

## 六、验证（已完成 ✅）

1. ✅ D 类端点冒烟：`session_token` → 403；`API Key` → 200；`JWT` → 200（本地 + 生产实测均通过）。
2. ✅ A/B/C 类端点不回归（session/API Key/JWT 均如现状）。
3. ✅ 前端页面（Web JWT）回归：需求中心 mine、仓库管理、场景、技能——**全部正常**（JWT 不收紧）。
4. ✅ agent/skill 对 B/C/A 端点正常；`/api/scenes`（VOD 技能用 session 调）维持 200。
5. ✅ 全量 pytest（基线 6 failed 零新增；`test_token_scope_guard` 25 passed）。

### 生产实测（2026-09-14，`grape.example.com`）

| 端点 | 分类 | 真实 tk_ 结果 |
|------|------|:---:|
| `/api/skills` | D | **403** ✅ |
| `/api/repos/managed` | D | **403** ✅ |
| `/api/auth/profile` | D | **403** ✅ |
| `/api/skills/by-id/1` | D | **403** ✅ |
| `/api/issues/o/r` | 非 D | 200 ✅ |
| `/api/scenes` | 非 D（VOD 技能） | 200 ✅ |
| `/api/tasks/mine` | 非 D | 200 ✅ |

---

## 六·A 测试用例设计

> 测试对象：`deps.require_no_session_token`（D 类端点）对三类认证的拦截行为。
> 基础设施沿用 `tests/test_auth_routes.py` / `test_auth_token.py` 既有模式：
> `TestClient(app)` + `dependency_overrides` 注入 user（auth_type 区分）+ `_jwt_access_token` / session token 签发。
> ✅ **已落地 `tests/test_token_scope_guard.py`（25 用例，全绿）**。

### 基线「三类认证用户」构造（已实现）

| 用例 | auth_type | 构造方式 | 期望 |
|------|-----------|----------|------|
| session_token | `session_token` | `issue_session_token(...)` 签发，`Authorization: Bearer tk_xxx` | 通过真实 token → user dict auth_type=session_token |
| api_key | `api_key` | `generate_api_token(...)` 签发，`Authorization: Bearer grape_xxx` | auth_type=api_key |
| jwt | `jwt` | `_jwt_access_token(...)`（Web 登录） | auth_type=jwt |

> 也可用 `dependency_overrides[get_current_user_or_api_key]` 直接注入不同 `auth_type` 的 user dict，
> 聚焦守卫逻辑（更简单、免平台 mock）。

### D 类端点守卫（核心）

| # | 用例 | 准备 | 断言 |
|---|------|------|------|
| T1 | `test_d_skills_session_403` | auth_type=session_token 调 `GET /api/skills` | 403，msg 含「会话 token」 |
| T2 | `test_d_skills_api_key_200` | auth_type=api_key 调 `GET /api/skills` | 200 |
| T3 | `test_d_skills_jwt_200` | auth_type=jwt 调 `GET /api/skills` | 200（JWT 不受影响） |
| T4 | `test_d_repos_managed_session_403` | auth_type=session_token 调 `GET /api/repos/managed` | 403 |
| T5 | `test_d_repos_managed_api_key_200` | auth_type=api_key 调 `GET /api/repos/managed` | 200 |
| T6 | `test_d_profile_session_403` | auth_type=session_token 调 `GET /api/auth/profile` | 403 |
| T7 | `test_d_profile_jwt_200` | auth_type=jwt 调 `GET /api/auth/profile` | 200 |
| T8 | `test_d_agents_session_403` | auth_type=session_token 调 `GET /api/agents/{name}/o/r` | 403 |
| T9 | `test_d_chat_conversations_session_403` | auth_type=session_token 调 `GET /api/chat/conversations` | 403 |
| T10 | `test_d_chat_conversations_api_key_200` | auth_type=api_key 调 `GET /api/chat/conversations` | 200 |
| T10a | `test_scenes_session_200_kept` | auth_type=session_token 调 `GET /api/scenes` | **200（scenes 不归 D 类，VOD 技能用 session 调）** |

### 非 D 类端点不回归（维持三通道）

| # | 用例 | 准备 | 断言 |
|---|------|------|------|
| T11 | `test_a_tasks_mine_session_200` | auth_type=session_token 调 `GET /api/tasks/mine` | 200（A 类维持现状） |
| T12 | `test_a_tasks_mine_api_key_200` | auth_type=api_key 调 `GET /api/tasks/mine` | 200（**API Key 维持现状**） |
| T13 | `test_a_tasks_mine_jwt_200` | auth_type=jwt 调 `GET /api/tasks/mine` | 200（Web「我的需求」不损） |
| T14 | `test_b_issues_list_session_200` | auth_type=session_token 调 `GET /api/issues/o/r` | 200（B 类双兼容） |
| T15 | `test_b_issues_list_jwt_200` | auth_type=jwt 调 `GET /api/issues/o/r` | 200 |
| T16 | `test_b_knowledge_list_api_key_200` | auth_type=api_key 调 `GET /api/knowledge` | 200 |
| T17 | `test_c_gitrepo_session_only` | auth_type=session_token 调 `GET /api/git-repo/o/r/branches` | 200（C 类现状仅 session） |
| T18 | `test_c_gitrepo_api_key_403` | auth_type=api_key 调 `GET /api/git-repo/o/r/branches` | 403（现状 `_repo_token` 拦截，不扩大） |

### 守卫单元测试（`require_no_session_token` 直接测）

| # | 用例 | 断言 |
|---|------|------|
| T19 | `test_guard_rejects_session` | user(auth_type=session_token) → 抛 403 |
| T20 | `test_guard_passes_api_key` | user(auth_type=api_key) → 返回 user |
| T21 | `test_guard_passes_jwt` | user(auth_type=jwt) → 返回 user |

### 必跑命令

```bash
python -m pytest tests/test_token_scope_guard.py -v          # 新增守卫测试
python -m pytest tests/test_auth_routes.py tests/test_auth_token.py -q  # 认证回归
python -m pytest tests/test_openapi.py -q                   # 契约不回归
```

---

## 七、实施结果与遗留

**✅ 已实施（commit `203d0316`，已上线生产并实测）**：
- `src/api/deps.py`：`require_no_session_token`
- D 类端点替换：`agents_detail` / `auth/profile` / `chat conversations|messages` / `repos member|managed|deploy-url` / `skills list|detail`
- `/api/scenes` 不归 D 类（VOD 技能用 session 调，维持现状）
- 测试 `tests/test_token_scope_guard.py`（25 用例）

**遗留（可选，未在本次范围）**：
- [ ] `x-token-scope` 契约扩展标注（§3.3，非必选，代码级已保证）
- [ ] CLI 各命令对 D 类端点的 token 类型表（CLI 侧文档化，服务端已生效）