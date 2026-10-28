# 意见反馈接口对外开放 OpenAPI 契约设计（feedback-openapi-expose-design.md）

> Status：plan-ready（TDD 待实施）
> Branch：`weekly-08-lyz`
> Related：Issue #174 反馈模块 · Issue #45 OpenAPI 对外 · `docs/weekly-03/issue-174-user-feedback-design.md`
> Sensitive info: this doc contains no real IP, domain, password, account or token (all `<xxx>` placeholders)

---

## 〇、Problem / Observed behavior

1. **对外能力缺口**：`/api/feedback/*` 意见反馈整套路由已挂载（`src/api/main.py:976`），但全部被排除在公开 OpenAPI 契约之外：
   - 用户侧端点认证依赖是 `get_current_user`（仅 Web JWT 会话）；而 `custom_openapi` 的 `route_is_public`
     （`src/api/openapi.py:82-87`）只收录依赖链含 `get_current_user_or_api_key` / `get_user_or_none` 的「对外安全路由」，
     JWT 单会话认证视为内部 → **不进契约**。
   - 结果：`help/api/openapi.json` 的 paths 中没有任何 `/api/feedback/*`（仅 `/api/chat/feedback`，另一个接口）；
     `help/api/*.md` 只文档化 `/api/chat/feedback`（`04-chat.md:108`）。外部用户 / CLI 走 API Key 三通道
     无法提交、查看、删除自己的反馈。
2. **安全缺口（同批暴露）**：`GET /api/feedback/list`、`PUT /api/feedback/{fid}/status`、`GET /api/feedback/stats`
   三个管理端点（`feedback.py:212/270/337`）**没有任何鉴权依赖**，而其 docstring 却声称"JWT 或 API Key 认证"。
   若服务公网可达，任何人可读取全部用户反馈（含身份信息）并任意修改状态 / 处理人 —— 与
   `docs/DOCUMENTATION-GUIDE.md` 的安全红线冲突，必须修复。
3. **Agent 自动反馈诉求（本版新增）**：devops agent 在任务执行中发现 grape CLI 命令 agent 不友好、技能规范写得差，
   导致大量 token 消耗与重试，希望在任务结束后**主动调用接口提交意见反馈**，并且平台侧能：
   - 凭 `tk_` 会话 token **权威还原**该反馈的仓库 / 任务 / agent / 技能；
   - **自动创建对应 Issue**：官方技能反馈 → 官方技能仓库（以平台 bot 身份）；仓库/用户技能反馈 → 该仓库（以仓库 bot 身份）；
   - 管理侧 `feedback/list` 能查看 Agent 反馈。
   当前四缺：`/api/feedback` 不认 `tk_`；`_collect_feedback_context`（`feedback.py:28-155`）不解析 skill；
   `user_feedback` 无 source/skill/issue 回写列；无「反馈 → Issue」转换机制。

## 一、Target semantics

| 分面 | 语义 |
|------|------|
| 对外开放 | 用户侧 5 个端点进公开契约；`POST /api/feedback` = **dual**（`tk_` 会话 token 可用，Agent 场景必需），其余 4 个 = **user_only** |
| 保留内部 | 管理侧 3 个端点（list/stats/status）**不进契约**，补 `require_admin` 鉴权堵住无鉴权漏洞；`list` 增加 Agent 反馈可见性（source 筛选 + 列展示） |
| Agent 反馈闭环 | `tk_` 提交 → `source='agent'` 落库；平台从 token 权威解析上下文（仓库/任务/agent/技能，不信任请求体）→ 后台 reconcile worker 自动创建 Issue → 回写 issue 链接 |
| 行为不变 | 用户提交/删改用例、附件校验、JWT 前端零改动均保持 |
| 契约规范 | 三通道 security、`{"detail":{"code","message"}}` 错误结构、逐 op `x-token-scope`，全部自动获得 |

### 1.1 公开端点清单（进契约）

| 方法 | 路径 | operation_id | 说明 | x-token-scope |
|------|------|--------------|------|:---:|
| POST | `/api/feedback` | `feedback_submit` | 提交反馈；`tk_` 走 Agent 分支（服务端从 token 权威解析上下文 + 落 `source='agent'`） | **dual** |
| GET | `/api/feedback/mine` | `feedback_mine` | 我的反馈分页（个人维度，`user_id` 归属） | user_only |
| GET | `/api/feedback/context/{event_seq}` | `feedback_context` | 失败事件上下文（服务端权威搜集 + 仓库权限校验） | user_only |
| DELETE | `/api/feedback/{fid}` | `feedback_delete` | 删除本人反馈（软删除，403 归属校验） | user_only |
| POST | `/api/feedback/upload` | `feedback_upload` | 附件上传（图片/zip ≤5MB，multipart） | user_only |

> `feedback_submit` 用 `get_current_user_or_api_key`（三通道 + `tk_`）；其余 4 个用 `require_no_session_token`
> （`tk_` → 403）。Agent 无"我的反馈 / 删除 / 附件"场景，`mine/delete/upload/context` 保持 user_only。

### 1.2 内部端点清单（不进契约，补鉴权）

| 方法 | 路径 | 鉴权改造 | 说明 |
|------|------|----------|------|
| GET | `/api/feedback/list` | `Depends(require_admin)` | 管理列表；加 `source` 筛选 + source/skill/task_id/issue 列展示 |
| PUT | `/api/feedback/{fid}/status` | `Depends(require_admin)` | 状态流转 |
| GET | `/api/feedback/stats` | `Depends(require_admin)` | 统计 |

> `require_admin`（`src/api/deps.py:206`）不在 `_PUBLIC_AUTH_DEPS` 内 → 天然不进契约，无需 openapi.py 改动。

### 1.3 Agent 自动反馈 → Issue 闭环（本版核心语义）

```
Agent (tk_, 任务执行期/结束后)
  │  POST /api/feedback  {category|feedback_kind, title, description[, event_seq]}
  ▼
feedback.py 落库 user_feedback（source='agent'，服务端从 token 解析：
  repo_key ← token.repo_key；task_id ← token.session_key；
  event_seq/agent_type/skill ← agent_tasks 反查）
  ▼
reconcile worker（后台，周期）
  │  扫 source='agent' AND issue_url='' 的反馈行
  │  按 skill.scope 路由目标仓库 + bot 身份
  │  幂等键 (task_id, skill_name, title) 查重 → 创建 Issue
  ▼
回写 issue_url / issue_number / converted_at
```

| 路由规则 | skill.scope | 目标仓库 | 提交身份 |
|---|---|---|---|
| 官方技能 | `official` | 官方技能仓库（平台配置常量 `GRAPE_SKILLS_REPO` = `<owner>/<repo>`） | 全局 grape bot（`get_global_gitcode_bot_token` + bot_username） |
| 仓库/用户技能 | `repo` / `user` | 反馈所属 `repo_key` 的 `<owner>/<repo>` | 仓库 bot token（`Repo(repo_key).bot_token` / `get_repo_bot_token`） |

## 二、Plan

```
Step 1   feedback.py 鉴权改造 + 契约声明（submit=dual；mine/delete/upload/context=user_only；
         operation_id / response_model / summary / tags="feedback"）
Step 2   _collect_feedback_context 补 skill 解析 + tk_ 分支从 token 权威解析上下文
Step 3   db_setup.py user_feedback 增列迁移（source/skill_id/skill_name/skill_scope/
         issue_url/issue_number/converted_at/feedback_kind，均 nullable）
Step 4   submit 落库扩展：写 source + skill 维度；tk_ 分支不信任 body 的 repo_key/skill
Step 5   新增 feedback→Issue 转换器 + reconcile worker（官方路由/bot 身份/幂等/失败重试/回写）
Step 6   list 增强：source 筛选 + 列展示（source/skill/task_id/issue）
Step 7   契约落盘重刷（/static/openapi.json + help/api/openapi.json）+ help/api 文档补齐
Step 8   TDD 红→绿：先写用例确认 FAIL，再逐步实施 Step1-7 转绿，回归全绿
```

> 无 openapi.py 逻辑改动：公开端点按认证依赖自动收录/标注（`submit` → dual，其余 → user_only）。

## 三、Module changes

| 文件 | 改动 |
|------|------|
| `src/api/routes/feedback.py` | submit → `get_current_user_or_api_key`；mine/delete/upload/context → `require_no_session_token`；list/stats/status → `require_admin`；`_collect_feedback_context` 取 skill 列 + skill 表 scope；tk_ 分支 token 解析；落库写 source/skill 维度；显式 `operation_id`/`response_model`/`summary` |
| `src/api/db_setup.py` | `user_feedback` ADD COLUMN：`source`(VARCHAR 默认 'user')、`skill_id`(INT NULL)、`skill_name`(VARCHAR)、`skill_scope`(VARCHAR)、`issue_url`(TEXT)、`issue_number`(VARCHAR)、`converted_at`(VARCHAR)、`feedback_kind`(VARCHAR) |
| `src/api/feedback_issue_converter.py`（新增） | 反馈 → Issue 转换器 + reconcile 循环：skill 路由 / bot 身份 / 幂等（task_id+skill_name+title）/ 失败重试 / issue 回写 |
| `src/api/routes/feedback.py` list | `source` 筛选参数 + 列展示 |
| `src/api/openapi.py` | **无逻辑改动**（自动收录/标注） |
| `tests/test_feedback_openapi.py`（新增） | 三通道 + scope 守卫（submit=dual、mine 等 user_only）+ 管理端点不进契约与鉴权矩阵 |
| `tests/test_feedback_issue_converter.py`（新增） | 幂等 / 官方技能路由+全局 bot / repo·user 技能路由+仓库 bot / 失败不回写留重试 / source 标记 |
| `tests/test_feedback_status.py` | 改用 admin JWT；新增普通用户 → 403 断言 |
| `tests/test_openapi.py` | `EXPECTED_PUBLIC` +5；`NOT_IN_CONTRACT` +3；scope 抽样补 `/api/feedback`(post, **dual**) 与 `/api/feedback/mine`(get, user_only) |
| `help/api/17-feedback.md`（新增） | 反馈接口文档（含 Agent 场景说明） |
| `help/api/README.md` | 目录 + 「各文档 scope 一览」加 Feedback 行 |
| `help/api/openapi.json`、`src/api/static/openapi.json` | 启动/重刷后自动带新字段 |

## 四、Key implementation notes

1. **submit 认证选择（dual）**：`Depends(get_current_user_or_api_key)` 直接可被 `route_is_public` 收录并标 `dual`；
   同时保留 Web JWT 全部现有行为。`tk_` 分支专用逻辑见注 3/4。其余 4 个用户端点 `require_no_session_token`（user_only）
   ——依赖链含对外认证、`tk_` 403、前端零改动（与既有 156 处 D 类端点同一机制）。
2. **operation_id 命名**：统一 `feedback_*`，与既有 `chat_feedback`（chat.py，另一个接口）区分。
3. **tk_ 分支上下文解析（服务端权威，不信任请求体）**：在 `submit_feedback` 内当 `user.auth_type == "session_token"` 时：
   - `repo_key` ← `user["repo_key"]`（绑定，不可伪造）；
   - `task_id`/`session_key` ← `user["session_key"]` → 反查 `agent_tasks` 取 `event_seq`/`agent_type`/`skill`/`runtime_id`；
   - `skill_name/skill_scope` ← `agent_tasks.skill` → `skill` 表（`id/skill_name/scope/owner_id`）；
   - 请求体传入的 `repo_key`/`event_seq` 在 tk_ 分支**忽略**。
   - JWT/API Key 分支维持现逻辑（带 `event_seq` 服务端现查；不带则信任 body 的 `repo_key`）。
4. **skill 解析补全**：`_collect_feedback_context`（`feedback.py:80-92`）task 查询的 SELECT 增加 `skill` 列；
   另查 `skill` 表得 `scope`。官方判定 = `scope='official'`。
5. **feedback→Issue 转换器**：
   - 目标仓库与身份按「一.1.3」路由表；调用 git provider 创建 Issue 接口（gitcode/github 双适配，参考
     `issues_center.py:1198` 的 `_github_req` 与 `issues.py:529` `_try_close_issue_for_repo` 的 bot token 用法）；
   不走 session_only 的透传端点 `gitrepo_issues_create`。
   - 官方仓库 bot 配置：平台配置/环境变量（owner/repo + bot username + bot token），**不硬编码真实值**。
   - Issue 标题/正文组装：反馈 id、repo_key、task_id、agent_type、skill、event_seq、fail_summary、原始 description；
     正文做长度与空行消毒（进入 git 平台文本）。
   - **幂等**：INSERT 前按 `(task_id, skill_name, title)` 查 `user_feedback` 是否已有 `issue_url`；同一 task 重复反馈去重
     （或给每次转换生成 `issue_request_id` 存库防重放）。
   - **失败处理**：转换异常仅记日志，**不回写 issue_url**，下轮 reconcile 重试；连续失败 N 轮后可标记 `converted_at=error` 供人工介入。
6. **reconcile worker 调度**：复用既有后台 periodic worker 模式（与 skill/scene serial sync 同款），周期扫描
   `source='agent' AND issue_url='' AND is_deleted=0`，加工后回写。转换为**强一致落库**，worker 崩溃不丢状态。
7. **`feedback_context` 权限（风险最高，必须显式防御）**：本端点 scope=user_only，`api_key` 可调；
   而 `_collect_feedback_context` 内的 `check_repo_access`（`feedback.py:50-54` → `deps.py:632-655`）对
   `session_token`/`api_key` 一律提前返回（"作用域已限定"，`deps.py:640-642`）——即 **`api_key` 持者可凭任意
   `event_seq` 读取他人仓库事件上下文**（含引擎 IP/设备/健康分）。防御：对 `api_key` 分支按 `user["id"]`
   查 `managed_repos` 做真实仓库归属校验；仅 `session_token` 保留跳过（tk_ 已绑定仓库，作用域天然受限）。
   同理 `submit` 落库时也走本函数，但对 `api_key` 分支同样收紧（避免把他人事件上下文带进自己的反馈行）。
8. **管理端点 handler 来源**：`PUT /status` 的 `handler_name` 从 admin JWT（`require_admin` 返回的 `username`）
   作为权威值落库；`handler_remark` 保留 body 传入。
9. **防刷与消毒**（Agent 提交）：tk_ 生命周期短（任务执行期 + `SESSION_TOKEN_REVOKE_GRACE` 180s 宽限，TTL 6h 兜底），
   已天然抑制跨任务刷量；再加**硬约束：每个 tk_（按 `session_key`/task_id）反馈条数 ≤ 2 条**，超出返回 429；
   title/description 长度上限（如 title ≤ `<n>`、description ≤ `<n>`）；描述进入 Issue 前做长度/空行/URL 消毒。
⑩. **响应模型（对外 5 端点）**：
    - `feedback_submit` → `{id, status, message}`
    - `feedback_mine` → `{items[], total, page, total_pages}`
    - `feedback_context` → `{event_seq, repo_key, issue_id, issue_number, agent_type, event_status, task, engine, skill, fail_summary}`
    - `feedback_delete` → `{success, message}`
    - `feedback_upload` → `{url, file_key, is_zip, original_name}`
11. **多语言/前端**：Agent 场景纯后端；Web UI 无需 i18n 改动。

## 五、Boundaries / Risks

| 项 | 评估 |
|----|------|
| **api_key 跨仓库 context 泄露** | 仅作用于 `GET /api/feedback/context/{event_seq}`（user_only）：`check_repo_access` 对 api_key 跳过校验 → 可凭任意 seq 读他人事件上下文（含引擎信息）；按注 7 收紧 api_key 仓库归属校验 |
| Agent 刷反馈 | tk_ 生命周期=任务执行期+180s 宽限，TTL 6h → 跨任务抑制；再加每 tk_ 任务反馈 ≤ 2 条（超额 429）+ 字段长度上限 + 每 task 去重 |
| 官方技能仓库不可达 | grape-skills 已仓库公开，任意有效 bot 均可提交 issue，**不存在 bot 无权限**；但平台若部署在内网/隔离网络，outbound 连不通 gitcode/github，公开仓库同样**不可达** → 转换失败告警不回写、下轮重试，标记 `skipped` admin 可见；「平台转换配置缺失」同此处理 |
| 管理端点行为破坏 | 现无前端调用（仅测试引用）→ 影响面可控；admin 管理台走独立 Java 后端 grapeJdbc，不受影响 |
| 契约断言强制 | `test_openapi` 的 `EXPECTED_PUBLIC` 集合断言会让改动先红后绿——正是 TDD 闭环 |
| 软删除/状态机 | 不变；`is_deleted=1` 记录 list/mine/stats/reconcile 均排除 |
| 上传体积/类型 | 5MB + 类型白名单保留，不做变更 |

## 六、Test case design（TDD red → green）

> 实施顺序硬约束：**先写用例并确认 FAIL（红）→ 再改 code（绿）**，遵循
> `docs/DOCUMENTATION-GUIDE.md` §5 / `docs/weekly-08/DOCUMENTATION-GUIDE.md` §5。

### A. 新增 `tests/test_feedback_openapi.py`（红阶段全失败）

| # | 用例 | 预期 |
|---|------|------|
| A1 | `feedback_submit`/`mine`/`context`/`delete`/`upload` 出现在 `/openapi.json` paths | 契约含 5 op |
| A2 | `feedback_submit` `x-token-scope=dual`；`mine`/`context`/`delete`/`upload` = user_only | scope 正确 |
| A3 | `/api/feedback/list`、`/api/feedback/stats`、`/api/feedback/{fid}/status` **不在** paths | 不进契约 |
| A4 | 无 token 调 `POST /api/feedback`、`GET /api/feedback/mine` | 401 |
| A5 | `grape_` API Key 三通道（Bearer / PRIVATE-TOKEN / `?access_token=<api_key>`）调 `GET /api/feedback/mine` | 200 |
| A6 | `tk_` session token 调 `POST /api/feedback`（合法 Agent 场景） | 200；落库 `source='agent'` |
| A7 | `tk_` session token 调 `GET /api/feedback/mine`、`DELETE /feedback/{id}`、`POST /feedback/upload` | 403（user_only 守卫） |
| A8 | 普通用户 JWT 调 `PUT /api/feedback/{fid}/status` | 403（admin-only） |
| A9 | admin JWT（role=admin）调 `PUT /api/feedback/{fid}/status` | 200；`handler_name`=admin token 的 username |
| A10 | tk_ 提交时 body 伪造 `repo_key`/`event_seq` 与 token 不一致 | 落库以 token 绑定为准（忽略 body） |
| A11 | 同一 tk_（session_key）连续提交 >2 条反馈 | 前 2 条 200，第 3 条起 429 |

### B. 新增 `tests/test_feedback_issue_converter.py`（红阶段全失败）

| # | 用例 | 预期 |
|---|------|------|
| B1 | 幂等：同一 `(task_id, skill_name, title)` 两轮 reconcile | 只创建 1 个 Issue，第二次跳过 |
| B2 | `skill.scope='official'` | 目标 = 平台配置的官方技能仓库 `<owner>/<repo>`；用全局 bot token 身份请求 |
| B3 | `skill.scope='repo'/'user'` | 目标 = 反馈所属 `repo_key`；用仓库 bot token 身份请求 |
| B4 | 转换成功 | 回写 `issue_url`/`issue_number`/`converted_at` |
| B5 | 转换异常（bot 无权限/仓库不可达） | 不写 issue_url，日志告警，下轮可重试 |
| B6 | `source='agent' AND issue_url=''` 才入选 reconcile；用户反馈（source='user'）不转换 | 过滤正确 |
| B7 | `is_deleted=1` 的 agent 反馈不入选 | 过滤正确 |
| B8 | 官方技能仓库目标不可达 / 平台转换配置缺失 | 不写 issue_url，告警保留待重试，路由标记 `skipped` 可在 admin 可见 |

### C. 更新既有用例

| 文件 | 改动 | 依据 |
|------|------|------|
| `tests/test_feedback_status.py` | `_jwt` → `create_access_token(<uid>, <username>, band, "", "admin")`；增设「普通用户 → 403」用例 | 管理端点现在 admin-only（语义变更，需注释说明） |
| `tests/test_openapi.py` | `EXPECTED_PUBLIC` +5；`NOT_IN_CONTRACT` +3；scope 抽样补 `/api/feedback`(post, dual) 与 `/api/feedback/mine`(get, user_only) | 契约集合双向一致（L1/L3） |

## 七、验收清单

1. `/openapi.json` 含 feedback 5 op：`submit`=dual、其余=user_only；管理 3 端点不在契约。
2. Agent 全链路：`tk_` 调 `POST /api/feedback` → 落库 `source='agent'` 且 skill/仓库/任务上下文来自 token；
   `mine/delete/upload` 对 `tk_` 一律 403。
3. reconcile worker：官方技能反馈经消息队列/periodic 转至官方技能仓库（bot 身份），repo/user 技能反馈转至所属仓库；
   幂等不重复建 issue；失败留待重试并 admin 可见。
4. `feedback/list`（admin）：`source` 筛选生效，Agent 反馈可见 source/skill/task_id/issue 列。
5. 管理 3 端点：无 token 401 / 普通用户 403 / admin 200。
6. api_key 越权读他人 `context` 被 403/404（注 7 防御生效）。
7. 前端工作台「问题反馈」弹窗 +「我的反馈」页回归通过（JWT 行为零差异）。
8. `help/api/17-feedback.md` + README scope 表 + 两份 `openapi.json` 已更新。
9. 关联测试全绿（`test_openapi` / `test_feedback_*` 全量）。

## References

- `docs/DOCUMENTATION-GUIDE.md`；`docs/weekly-08/DOCUMENTATION-GUIDE.md`
- `docs/weekly-02/issue-45-openapi-api-design.md`（对外安全路由过滤原则 §3.3/§六）
- `docs/weekly-03/openapi-x-token-scope-design.md`（scope 推导 + L1/L2/L3 测试强制）
- `docs/weekly-03/issue-174-user-feedback-design.md`（反馈模块设计）
- `docs/weekly-08/feedback-task-context-design.md`（服务端现查上下文约定）
- 源码锚点：`src/api/openapi.py:82-110` · `src/api/deps.py:206/233/632` · `src/api/auth_token.py:37-89`
  · `src/api/routes/feedback.py` · `src/api/db_setup.py`（skill/events/agent_tasks DDL）
  · `tests/test_openapi.py:27-271`