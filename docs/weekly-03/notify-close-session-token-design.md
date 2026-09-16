# notify/close 端点改造方案：bot-token → session token 认证

> 分支：`weekly-03-lyz`；状态：**已实施（2026-09-16：notify 改 session token；close 移除对外）**；日期：2026-09-16
> 触发：API 文档评审提出「对外接口应只接收 `tk_` 会话 token / `grape_` API Key / WebUI JWT，
> 不应暴露『bot-token』这类第 4 种认证」；且 `POST /api/tasks/close` **端点已不存在**（文档/技能残留 404）。

---

## 一、现状与问题

### 1.1 两个端点的认证现状

| 端点 | 现状认证 | 守卫 | 契约 |
|------|----------|------|:---:|
| `POST /api/tasks/notify` | `Authorization: Bearer <仓库 bot_gitcode_token>` 或 `X-Bot-Token` | `_require_bot_token`（`tasks.py:24`，字符串比对仓库级平台 token） | ❌ 不进契约 |
| `POST /api/tasks/close` | 同 notify（`_require_bot_token`） | 同上 | ❌ 端点已移除（404） |

> **`close` 现状**：`tasks.py` 已无 `@router.post("/close")` 路由，但业务函数 `close_issue_tasks`
> （`tasks.py:234`）仍被 webhook 兜底 / `event_pipeline` 内部调用；`help/api/01-tasks.md` 与
> `docs/new-version2/skills-api-reference.md` 等仍宣称该端点存在。

### 1.2 问题

1. **第 4 种认证方式对外暴露**：`.栅 bot token` 是「仓库级平台密钥」（webhook 同源），不属于
   `tk_`/`grape_`/JWT 三通道体系；API 文档中出现「bot-token」令读者困惑（用户明确提出）。
2. **close 端点残留**：文档宣称存在、技能/CLI 若调用返回 404，契约也无此路径——不一致。
3. **notify 未进契约**：技能无法从 `/openapi.json` 发现它（`_require_bot_token` 不在 `_PUBLIC_AUTH_DEPS`）。

### 1.3 为什么可改成 session token（可行性）

- **调用方本就是 Agent**：`notify`/`close` 由 issue 系列技能在**执行期**调用（`issue-45-skills-api-scan.md`
  确认 notify 26 处、close 被 issue 系列依赖）；Agent 执行期**已持 `$GRAPE_SESSION_TOKEN`**（`tk_`，
  CLI 参考 `skills/.../grape-cli-reference.md` 明确「只用品系统注入的 `$GRAPE_SESSION_TOKEN`」）。
- **session token 恰好携带所需作用域**（`auth_token.py:59` `verify_session_token`）：
  `repo_key`（仓库）+ `agent_type`（哪个 agent）+ `user_id` + `session_key`（task_id）。
- **已有同款先例**：`verify_session_token_for_repo`（`auth_token.py:91`）就是「按仓库作用域校验 session token」，
  已被 events `_require_event_bot_token` / A2A 内部端点使用——改造只需复用同一机制。

---

## 二、改造方案

### 2.1 认证统一：`_require_bot_token` → session token 作用域校验

```python
# 新增守卫（deps.py 或 tasks.py 内），对齐 events._require_event_bot_token 思路
def _require_session_repo_token(request: Request, repo_key: str) -> dict:
    """notify/close 等 agent 动作端点：仅接受 tk_ 会话 token，且仓库作用域必须匹配。

    与 bot-token 的区别：认证走三通道 session token（tk_），不再接受仓库平台 token；
    作用域以 token 绑定的 repo_key 为准（防跨仓库调用）。
    """
    token = (request.headers.get("Authorization", "") or "").replace("Bearer ", "").strip()
    if not token and request.headers.get("X-Bot-Token"):
        # 兼容期：仍接受 X-Bot-Token 直达（见 §2.3）
        token = request.headers.get("X-Bot-Token", "")
    from src.api.auth_token import verify_session_token_for_repo
    info = token.startswith("tk_") and verify_session_token_for_repo(token, repo_key)
    if not info:
        raise HTTPException(status_code=403, detail={"code": 403, "message": "需仓库级会话 token（tk_）且仓库作用域匹配"})
    return info
```

- `notify_task`：`_require_bot_token(request, body.repo_key)` → `_require_session_repo_token(request, body.repo_key)`
- `close` 端点：**重建**，同样用 session 守卫（若决定恢复，见 §2.4）

### 2.2 语义对齐：`agent_type` 从 token 派生，不再信 body

改造后 `info = _require_session_repo_token(...)` 返回 session 上下文，notify 内部：
- 仓库 = `body.repo_key`（仍显式，但必须与 `info["repo_key"]` 一致——由 `verify_session_token_for_repo` 保证）
- 触发 agent = `info["agent_type"]`（审计/日志用，不再依赖调用方自报）

这样 notify 依旧创建任务卡片（`_ensure_assignee_task_card`），但**身份由会话 token 确立**，与 bot-token 的「仓库密钥即身份」等价且更可控。

### 2.3 兼容期（可选）

若担心旧技能仍发 `BOT_GITCODE_TOKEN`：`_require_session_repo_token` 中保留 `X-Bot-Token` 的
「仓库 bot token 精确比对」作为**临fallback**，返回 403 前先比对 `get_repo_bot_token_no_global`。
建议**短暂兼容后移除**（对齐「对外仅三通道」的目标）。

### 2.4 close 端点去留

现状 close 路由已删，但 `close_issue_tasks` 函数 / webhook 兜底仍用。二选一：

| 方案 | 做法 | 适用 |
|------|------|------|
| **A（推荐）彻底移除对外** | 不重建 `/api/tasks/close`；`close_issue_tasks` 仅 webhook/内部调用；同步删 help/api 与技能的 `/api/tasks/close` 引用 | 该动作本就是「平台侧关闭 Issue 后自动收尾」，无用户主动调用场景 |
| **B（按需求恢复）** | 重建 `POST /api/tasks/close`，改用 session 守卫（同 notify）；契约补录 | 若技能确实仍需要主动触发 close |

> 本文按 **A** 为主推（避免无谓对外面），同时给出 **B** 的 session 化改动点。

---

## 三、契约与文档影响

### 3.1 notify 进契约（若改造后）

- `_require_session_repo_token` 内部走 `verify_session_token_for_repo`（非 `_PUBLIC_AUTH_DEPS`），
  notify 默认**仍不进 `/openapi.json`**（除非将守卫定义为 public session 依赖）。
- **建议**：notify/close 属「Agent 执行期内网动作」——**不对外暴露合理**，对齐 `_require_event_bot_token`
  （events 内部端点不进契约）。文档标注为「Agent 会话（tk_）可调，不入契约」即可。

### 3.2 文档刷新（help/api/01-tasks.md）

| 端点 | 认证 | 说明 |
|------|------|------|
| `notify` / `close`（若保留） | `tk_` 会话 token（`Authorization: Bearer tk_...`） | Agent 执行期内调用；仓库作用域由 token 校验；不进 `/openapi.json` |
| `complete` | dual（`tk_`/`grape_`/JWT 任一） | 不变 |
| `pre-upload/*` | Web 会话（JWT） | 不变 |

- **删除「bot-token」字样**，不再引入第 4 种认证概念。
- `help/api/01-tasks.md` 中 close 条目：若按方案 A，改为「已并入平台 webhook 收尾，无对外端点」。

---

## 四、影响与风险

| 项 | 影响 |
|----|------|
| 兼容 | 旧技能仍发 bot-token（BOT_GITCODE_TOKEN）时 403——需同步更新 issue 系列技能引用改为 `$GRAPE_SESSION_TOKEN`（其实技能本就用 session token，改动小） |
| 安全 | 从「仓库平台密钥」变为「执行期会话（tk_）」，作用域更窄（token 短期、绑定 repo+agent），**安全性提升** |
| 语义 | notify 触发方身份 = token 的 agent_type，不再依赖自报——审计更可靠 |
| close | 按方案 A 直接抹掉对外端点，消除 404 残留；`close_issue_tasks` 内部保留 |

## 五、验收

1. `POST /api/tasks/notify` 带有效 `tk_`（repo 匹配）→ 建任务成功；repo 不匹配 / 非 tk_ → 403。
2. 旧 `BOT_GITCODE_TOKEN` 调用（无兼容期时）→ 403。
3. `/api/tasks/close` 按方案 A → 404（且无文档宣称）；`close_issue_tasks` 内部调用回归正常。
4. `help/api/01-tasks.md` 无「bot-token」，改为 `tk_` 描述。
5. 技能侧（vod/repo-onboarding、issue 系列）用 `$GRAPE_SESSION_TOKEN` 调 notify 通过；webhook 关闭 Issue → pending_review 不回归。

> TODO（实施时）：新增守卫 → 改 notify → 确认/重建 close → 同步技能引用与 help/api → 冒烟 + 回归。

### 实施记录（2026-09-16）

- **决策**：仅保留 `notify` 对外（session token）；`close` 不对外（平台 webhook 内部收尾）。
- `tasks.py`：`_require_bot_token` → **`_require_session_repo_token`**（仅 `tk_` + `verify_session_token_for_repo` 仓库作用域）；`agent_type` 从 token 派生；file docstring 更新。
- `help/api/01-tasks.md`：notify 认证改「`tk_` 会话 token（仓库作用域）」；移除 close 条目；一览表同步。
- 冒烟：无 token 403 / bot-token 样式 403 / 仓库不匹配 403 / 匹配 session 走业务分支（bot-takeover 200、未注册 400）。
- `test_openapi` 8 passed（notify/close 本就不进契约，无影响）。
- **待办（grape-skills 独立仓库）**：issue 系列技能 + vod/repo-onboarding 中调 `notify` 的 `$BOT_GITCODE_TOKEN` 引用改为 `$GRAPE_SESSION_TOKEN`（本地 `skills/` 为铺入副本，改动需同步技能仓库）。