# Agentic DevOps — 测试方案

> 基于 `tech-design.md` 和 `prototype.html` 的全面测试设计。

---

## 一、测试分层

```
┌─ E2E ──────────────────────────────────────────────┐
│  Webhook → Event Pipeline → Agent → Frontend 全链路 │
├─ Integration ──────────────────────────────────────┤
│  API + DB + Agent + IM + Webhook 跨模块集成         │
├─ Unit ─────────────────────────────────────────────│
│  每个模块独立单元测试                                │
└────────────────────────────────────────────────────┘
```

| 层次 | 框架 | 覆盖范围 |
|------|------|---------|
| **Unit** | pytest + unittest.mock | 每个 .py 模块独立测试 |
| **Integration** | pytest + FastAPI TestClient | API + DB + Agent 集成 |
| **API** | FastAPI TestClient + httpx | 所有 REST 端点 |
| **E2E** | pytest + requests | 端到端流程验证 |
| **前端** | 手动 + Playwright (可选) | SPA 交互 + 视觉效果 |
| **性能** | locust / ab | 并发 + 响应时间 |
| **安全** | 手动 / OWASP ZAP | Token / CSRF / 注入 |

---

## 二、Unit Tests

### 2.1 Agent 执行层 (`src/agent/`)

| 测试项 | 测试文件 | 验证内容 |
|--------|---------|---------|
| OpenCode Backend 参数构建 | `test_opencode.py` | `build_args` 返回正确的 CLI 参数列表, session_id 注入 |
| CodeArts Backend 参数构建 | `test_codearts.py` | prompt 通过 stdin, `build_args` 不含 prompt |
| NDJSON 解析 | `test_base.py` | `parse_output` 正确处理 step_start/text/reasoning/tool_use/error/done |
| Backend 发现 | `test_discovery.py` | `discover_backends()` 正确检测 PATH 中已安装引擎 |
| Backend 健康检测 | `test_discovery.py` | `check_health(binary)` 返回 {running, version, error} |
| CodeArts AK/SK 验证 | `test_discovery.py` | `validate_codearts_auth(ak, sk)` 验证 AK/SK 有效性 |
| 子进程超时 | `test_executor.py` | 120s 超时自动 kill, yield done with exit_code=-1 |
| 子进程正常退出 | `test_executor.py` | 正常流程 yield done with exit_code=0 + text |

### 2.2 事件处理管道 (`event_processor.py`)

| 测试项 | 验证内容 |
|--------|---------|
| 反循环过滤 | `actor ∈ BOT_USERNAMES` 且 `event_type ∈ {issue_comment, note_hook_pr}` → skip |
| 非 Bot 事件处理 | actor 不在 BOT_USERNAMES → 正常处理 |
| IM 通知分支 | 非 im_notify 事件 → clone 为 im_notify → 异步提交 |
| IM 通知去重 | im_notify 事件不再 clone 自己 |
| 未知事件类型 | 无 repository 或 unknown type → skip |
| Per-repo lock | 同一仓库事件串行执行 |
| A2A 深度限制 | 超过 5 层 → 拒绝 |
| A2A agent 验证 | agent 不在 agent-description.json → 拒绝 |

### 2.3 VCS 适配器 (`src/vcs/`)

| 测试项 | 验证内容 |
|--------|---------|
| GitCode Issue Hook | `normalize` 正确映射 `issue_opened` / `issue_updated` |
| GitCode Note Hook (Issue) | 映射为 `issue_comment` |
| GitCode Note Hook (MR) | 映射为 `note_hook_pr` |
| GitCode MR Hook open | 映射为 `pull_request_opened` |
| GitCode MR Hook merge | 映射为 `pull_request_merged` |
| GitCode Push Hook | 映射为 `push` |
| 解码异常 | GBK fallback 解码正常处理 |
| 空 body | 返回 None, 不崩溃 |

### 2.4 Prompt 构建 (`prompt_builder.py`)

| 测试项 | 验证内容 |
|--------|---------|
| 事件 Prompt 结构 | 8 段组成完整（A2A 上下文→事件→历史→Skill→飞书→角色→A2A 协议）|
| A2A 上下文注入 | `_a2a_context` 存在时注入前一 Agent 摘要 |
| Chat Prompt 结构 | 7 段组成完整（身份→消息→能力→历史→Skill→角色→A2A） |
| Skill 注入 | `skills/{skill}/SKILL.md` 全文正确读取注入 |
| 飞书上下文注入 | 协作者列表、群成员、Card API 指令正确格式 |
| Agent 注册表注入 | 所有注册 Agent 的 curl 模板正确生成 |

### 2.5 会话管理 (`session_manager.py`)

| 测试项 | 验证内容 |
|--------|---------|
| 新建 Agent Session | `get_or_create_agent_session(repo_key, agent_type, backend)` → 返回 agent_session_id=None |
| 复用 Agent Session | 同一 (repo_key, agent_type) 再次调用 → 返回已有 session_id |
| Agent Session 隔离 | 不同 agent_type 的 session_id 互不干扰 |
| Session 更新 | `update_agent_session` 正确写入 backend_session_id |
| 事件追加 | `append_event_history` 追加到 events 表 |
| 事件历史查询 | `get_event_history(repo_key)` 返回所有 Agent 的事件 |
| 容量裁剪 | 超过 100 条自动裁剪最旧条目 |
| 失败 Session 重置 | status=error → 下次 `get_or_create` 自动重置为 idle |

### 2.6 用户数据库 (`src/user/db.py`)

| 测试项 | 验证内容 |
|--------|---------|
| 用户创建 | `create_user(username, password)` → 密码 hash 正确 |
| 用户验证 | `verify_user(username, password)` → 正确/错误密码 |
| Token 创建/验证/过期 | SHA256 + 24h 过期 |
| OAuth state 保存/验证 | 内存 dict pop 一次性使用 |
| GitCode 用户绑定 | `link_gitcode_user` 正确 set gitcode_id + 存 credentials |
| 用户凭据 CRUD | `get/set/delete_credential` |
| 账户保存 | `save_user_account` UPDATE 保留 id, INSERT 新建 |

---

## 三、API 测试 (FastAPI :3000)

### 3.1 认证 `/api/auth/`

| # | 方法 | 路径 | 测试场景 | 预期 |
|---|------|------|---------|------|
| A1 | POST | `/login` | 正确的用户名密码 | 200 `{token, user}` |
| A2 | POST | `/login` | 错误的密码 | 401 "用户名或密码错误" |
| A3 | POST | `/login` | 不存在的用户 | 401 |
| A4 | GET | `/login/gitcode` | 已配置 OAuth | 200 `{redirect_url}` |
| A5 | GET | `/login/gitcode` | 未配置 client_id | 400 |
| A6 | GET | `/callback/gitcode` | 无效 state | 400 "state 无效" |
| A7 | GET | `/callback/gitcode` | 有效 state + code | 200 `{token, user}` |
| A8 | GET | `/profile` | 带有效 token | 200 `{username, managed_repos}` |
| A9 | GET | `/profile` | 无 token | 401 |

### 3.2 仓库 `/api/repos/`

| # | 方法 | 路径 | 测试场景 | 预期 |
|---|------|------|---------|------|
| R1 | GET | `/member` | token 有效 + API Token 已配 | 200 `{repos:[...]}` |
| R2 | GET | `/member` | API Token 未配置 | 400 |
| R3 | GET | `/managed` | 有托管仓库 | 200 `{repos:[...]}` |
| R4 | POST | `/join` | 有效 full_name | 200 "已加入仓库" |
| R5 | POST | `/join` | 无效 full_name | 400 |
| R6 | POST | `/host` | Admin 角色 + 有效 token | 200 "仓库托管成功" + webhook_id |
| R7 | POST | `/host` | API Token 未配置 | 400 |
| R8 | POST | `/remove` | 有效的托管仓库 | 200 "已移除仓库" |
| R9 | POST | `/remove` | 不存在的仓库 | 404 |

### 3.3 Dashboard `/api/dashboard/`

| # | 方法 | 路径 | 测试场景 | 预期 |
|---|------|------|---------|------|
| D1 | GET | `/{owner}/{repo}` | 有效仓库 | 200 `{repo, metrics, agents, activity}` |
| D2 | GET | `/{owner}/{repo}` | 非托管仓库 | 404 |

### 3.4 Pipeline `/api/pipeline/`

| # | 方法 | 路径 | 测试场景 | 预期 |
|---|------|------|---------|------|
| P1 | GET | `/{owner}/{repo}` | 有效仓库 | 200 `{active, history}` |
| P2 | GET | `/{owner}/{repo}` | 无事件历史 | 200 `{active:null, history:[]}` |

### 3.5 Events `/api/events/`

| # | 方法 | 路径 | 测试场景 | 预期 |
|---|------|------|---------|------|
| E1 | GET | `/{event_seq}` | 有效 seq | 200 `{context, execution_log, metadata}` |
| E2 | GET | `/{event_seq}` | 无效 seq | 404 |
| E3 | GET | `/{owner}/{repo}` | 有效仓库 | 200 `{events:[...]}` |

### 3.6 Agent `/api/agents/`

| # | 方法 | 路径 | 测试场景 | 预期 |
|---|------|------|---------|------|
| G1 | GET | `/{name}/{owner}/{repo}` | 有效 agent | 200 `{name, metrics, events, skill}` |
| G2 | GET | `/{name}/{owner}/{repo}` | 无效 agent name | 404 |
| G3 | PUT | `/{name}/{owner}/{repo}` | 更新 Backend | 200 |
| G4 | PUT | `/{name}/{owner}/{repo}` | 更新 Skill | 200 |

### 3.7 Chat `/api/chat/`

| # | 方法 | 路径 | 测试场景 | 预期 |
|---|------|------|---------|------|
| C1 | POST | `/send` | 发送消息 | 200 `{reply, conversation_id}` |
| C2 | POST | `/send` | 无 token | 401 |
| C3 | GET | `/config` | 获取配置 | 200 `{backend:..., im:...}` |
| C4 | PUT | `/config` | 更新 Backend | 200 |
| C5 | PUT | `/config` | 更新 IM | 200 |
| C6 | POST | `/feedback` | 提交反馈 `{event_seq, title, desc}` | 200 + A2A 触发 |

### 3.8 Settings `/api/settings/`

| # | 方法 | 路径 | 测试场景 | 预期 |
|---|------|------|---------|------|
| S1 | GET | `/credentials` | 获取凭据 | 200 `{gitcode, huawei_cloud}` |
| S2 | PUT | `/credentials` | 更新华为云 AK/SK | 200 |
| S3 | GET | `/notifications` | 获取偏好 | 200 `{issues, pr, build,...}` |
| S4 | PUT | `/notifications` | 更新偏好 | 200 |
| S5 | GET | `/gitcode-repo/{o}/{r}` | 获取 Repo 配置 | 200 |
| S6 | PUT | `/gitcode-repo/{o}/{r}` | 更新 Bot Token | 200 |
| S7 | GET | `/backend-agents` | 获取引擎状态 | 200 `[{name, installed, running}]` |
| S8 | POST | `/backend-agents/refresh` | 刷新检测 | 200 |

### 3.9 Skills `/api/skills/`

| # | 方法 | 路径 | 测试场景 | 预期 |
|---|------|------|---------|------|
| K1 | GET | `/` | 获取所有技能 | 200 `[{agent, skills}]` |
| K2 | GET | `/?agent=vod` | 按 Agent 过滤 | 200 |
| K3 | GET | `/?search=声音` | 搜索技能 | 200 |
| K4 | POST | `/upload` | 上传有效 Skill | 200 |
| K5 | POST | `/upload` | 上传无效 Skill（缺少 SKILL.md） | 400 |
| K6 | DELETE | `/{name}` | 删除自定义 Skill | 200 |

### 3.10 Search `/api/search/`

| # | 方法 | 路径 | 测试场景 | 预期 |
|---|------|------|---------|------|
| H1 | GET | `/?q=issue&type=event` | 搜索事件 | 200 `{results:[...]}` |
| H2 | GET | `/?q=backend&type=repo` | 搜索仓库 | 200 |
| H3 | GET | `/?q=vod&type=agent` | 搜索 Agent | 200 |

---

## 四、Webhook 测试 (端口 8080)

| # | 测试场景 | 预期 |
|---|---------|------|
| W1 | 有效 X-GitCode-Token → Issue Hook | 200 + 异步处理 |
| W2 | 无效 X-GitCode-Token | 403 "invalid webhook token" |
| W3 | 有效 A2A payload `{agent:"issue", payload:{...}}` | 200 `{"status":"ok"}` |
| W4 | A2A agent 不在注册表 | 日志警告, 200 `{"status":"ok"}` (不阻断) |
| W5 | A2A 深度 > 5 | 拒绝, 返回 200 `{"status":"ok"}` (不阻断流程) |
| W6 | GET `/health` | 200 `{"status":"ok", "agents":[...], "skills":[...]}` |
| W7 | GET `/api/sessions` | 200 `[{...session...}]` |

---

## 五、Event Processing 集成测试

| # | 测试流程 | 验证点 |
|---|---------|-------|
| EP1 | GitCode Issue 开事件 → Issue Agent | 路由正确, Skill 加载, Agent 执行, 事件记录 |
| EP2 | GitCode PR 开事件 → QA Agent | Code Review, 结果记录到 events 表 |
| EP3 | A2A Issue→Coding 链 | Issue Agent → 触发 Coding Agent → ref 上下文注入正确 |
| EP4 | A2A QA→Coding 回退 | QA Agent 发现问题 → 回退 Coding Agent 修复 |
| EP5 | IM 通知分支 | 非 im_notify 事件 → clone 为 im_notify → 异步提交成功 |
| EP6 | Bot 自身事件过滤 | actor ∈ BOT_USERNAMES 的 comment → skip |
| EP7 | Session 隔离 | 同一仓库不同 Agent 使用不同 backend session_id |
| EP8 | Agent Session 复用 | 第二次事件使用同一 session_id, Agent 加载历史上下文 |

---

## 六、前端 SPA 测试

### 6.1 页面路由

| # | 测试场景 | 预期 |
|---|---------|------|
| F1 | 未登录访问任意页面 | 显示登录页 overlay |
| F2 | 登录后首次（无仓库） | 显示 Repo Selection 全屏页 |
| F3 | 登录后已有仓库 | 直接进入 Dashboard |
| F4 | 侧边栏点击 Dashboard | 加载 Dashboard |
| F5 | 侧边栏点击 Pipeline | 加载 Pipeline |
| F6 | 侧边栏点击任意 Agent | 加载 Agent Detail |
| F7 | 侧边栏点击 Chat Agent | 加载 Chat 页面 |
| F8 | 侧边栏点击 Skills Library | 加载 DevOps Skills |
| F9 | 侧边栏点击 General Settings | 加载 Settings |
| F10 | 侧边栏点击 GitCode Repo | 加载 GitCode Repo 页 |
| F11 | 侧边栏点击 Backend Agents | 加载 Backend Agents 页 |

### 6.2 交互链路

| # | 测试场景 | 预期 |
|---|---------|------|
| F12 | Dashboard Activity 行点击 | 跳转 Event Detail 页面 |
| F13 | Pipeline 节点点击 | 跳转 Event Detail 页面 |
| F14 | Pipeline 历史行点击 | 切换 Active Pipeline 状态 |
| F15 | Agent Detail 事件行点击 | 跳转 Event Detail 页面 |
| F16 | Repo 卡片点击 | 切换仓库 → Dashboard 刷新 |
| F17 | 侧边栏 Repo 下拉切换 | Dashboard 数据跟随切换 |

### 6.3 表单与配置

| # | 测试场景 | 预期 |
|---|---------|------|
| F18 | General Settings → 修改凭据并保存 | API 调用成功，UI 反馈 |
| F19 | General Settings → 修改通知偏好 | Checkbox 正确保存 |
| F20 | Agent Detail → Configure → 切换 Backend | 下拉选项正确，保存生效 |
| F21 | Chat Agent → IM 配置 → 修改 Feishu 凭据 | App ID/Secret 正确保存 |
| F22 | Chat Agent → Backend 配置 → 切换类型 | Default/Custom 正常切换 |
| F23 | GitCode Repo → 修改 Bot Token | 保存成功 |
| F24 | Event Detail → 提交反馈 | 表单验证 + Submit 成功 |

### 6.4 登录页

| # | 测试场景 | 预期 |
|---|---------|------|
| F25 | 点击 GitCode OAuth 按钮 | 跳转到 GitCode 授权页 |
| F26 | 点击用户名密码登录链接 | 展开表单，OAuth 按钮隐藏 |
| F27 | 密码表单中点击返回 | 恢复 OAuth 界面 |
| F28 | 密码登录提交 | 调用 API，成功后进入系统 |

### 6.5 响应式

| # | 测试场景 | 预期 |
|---|---------|------|
| F29 | 窗口宽度 < 768px | 侧边栏隐藏，内容全宽 |
| F30 | 窗口宽度 < 768px | Agent Flow 纵向排列 |
| F31 | 窗口宽度 < 768px | Chat 页面单列布局 |

---

## 七、端到端测试

| # | 测试流程 | 验证点 |
|---|---------|-------|
| E2E1 | 登录 → 选仓库 → Dashboard → Agent → Event → 反馈 | 完整用户路径无断点 |
| E2E2 | GitCode Issue 事件 → Webhook → Event Pipeline → Agent 执行 → Dashboard 显示 | 全链路数据流 |
| E2E3 | 用户提交反馈 → API → A2A → VOD Agent → 创建 Issue | 反馈闭环 |
| E2E4 | 仓库托管 → Webhook 创建 → GitCode 验证 | Webhook 管理流程 |
| E2E5 | 修改 Agent Backend → 下次事件用新 Backend | 配置即时生效 |

---

## 八、性能测试

| # | 测试场景 | 指标 | 阈值 |
|---|---------|------|------|
| P1 | Dashboard API 响应时间 | P50/P95/P99 | < 200ms / < 500ms / < 1s |
| P2 | Repo 成员列表 API | 10 repos | < 500ms |
| P3 | 并发 Webhook (20 req/s) | 错误率 | 0% (队列满则丢弃) |
| P4 | 并发 Chat 消息 (5 req/s) | 错误率 | 0% |
| P5 | Agent 执行超时 | 120s | 超时 kill + 正常记录 |
| P6 | SQLite 千人用户 | CRUD 响应 | < 100ms per op |

---

## 九、安全测试

| # | 测试场景 | 预期 |
|---|---------|------|
| X1 | API 调用无 token | 401 "未登录" |
| X2 | API 调用伪造 token | 401 "会话过期" |
| X3 | Webhook 无 X-GitCode-Token | 403 |
| X4 | Webhook 伪造 X-GitCode-Token | 403 |
| X5 | A2A 从外网 IP 调用 | 拒绝 (仅 localhost) |
| X6 | OAuth state 重放 | 400 "state 无效" |
| X7 | Skill 上传含 .py/.sh | 400 校验拦截 |
| X8 | Skill 上传 > 1MB | 400 大小限制 |
| X9 | 用户 A 访问用户 B 的仓库数据 | 403 (通过 user_id 隔离) |
| X10 | SQL 注入 (搜索参数) | 参数化查询防护 |

---

## 十、测试环境

| 环境 | 用途 | 配置 |
|------|------|------|
| **本地** | 单元 + API 测试 | SQLite, 无需 GitCode/飞书 |
| **Staging** | 集成 + E2E 测试 | 测试 GitCode 账号 + 测试飞书 App |
| **Production** | 部署验证 | 真实 GitCode Webhook + 真实飞书 IM |

---

## 十一、执行计划

| Phase | 内容 | 用例数 | 预计工时 |
|-------|------|--------|---------|
| 1 | Unit Tests (agent/vcs/config/db) | ~40 | 3d |
| 2 | API Tests (auth/repos/dashboard/...) | ~60 | 3d |
| 3 | Integration Tests (event pipeline + webhook) | ~10 | 2d |
| 4 | Frontend Tests (manual) | ~30 | 2d |
| 5 | E2E + Performance + Security | ~15 | 2d |
| **合计** | | **~155** | **12d** |
