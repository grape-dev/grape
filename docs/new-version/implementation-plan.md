# Agentic DevOps — 任务实现计划

> 基于 `tech-design.md`、`prototype.html`、`test-plan.md` 的分步实施路线。

---

## Phase 1：基础设施搭建

### Task 1.1 FastAPI 项目结构

| 项目 | 内容 |
|------|------|
| **目标** | 搭建 FastAPI 应用骨架，注册所有路由模块 |
| **要点** | `src/api/main.py`、`src/api/deps.py`、`src/api/routes/` 目录结构 |
| **产出** | `fastapi dev` 可启动，`/health` 返回 `{"status":"ok"}` |
| **验收** | `GET :3000/health` → 200 |

### Task 1.2 数据库迁移与 Seed

| 项目 | 内容 |
|------|------|
| **目标** | 新增 `events`、`agent_sessions`、`event_route_config`、`agent_type_config`、`agent_registry` 表 |
| **要点** | `init_db()` 幂等 DDL + ALTER TABLE，JSON 种子数据 bootstrap 导入 |
| **产出** | 表创建完成，启动时自动从 JSON 导入首次数据 |
| **验收** | 查询各表有初始数据，重复启动不覆盖 |

### Task 1.3 统一认证与 Token 中间件

| 项目 | 内容 |
|------|------|
| **目标** | 实现 `deps.py:get_current_user()`，所有 API 统一 `Authorization: Bearer` |
| **要点** | SHA256 Token，24h 过期，内存+DB 双重验证，401 未登录 / 401 过期 |
| **产出** | 受保护端点返回 401 当无 token，正确 token 可调通所有 API |
| **验收** | A1-A11 认证测试用例全部通过 |

---

## Phase 2：核心 API 实现

### Task 2.1 仓库 API

| 项目 | 内容 |
|------|------|
| **目标** | 实现 `/api/repos/` 的 5 个端点 |
| **要点** | 复用现有 `repo_manager.py` 的 `fetch_repos_with_role`、`setup_webhook`；`join` 与 `host` 区分逻辑 |
| **产出** | 仓库列表返回 GitCode 成员仓库，Join/Host/Remove 正常 |
| **验收** | R1-R9 全部通过 |

### Task 2.2 Dashboard API

| 项目 | 内容 |
|------|------|
| **目标** | 实现 `GET /api/dashboard/{owner}/{repo}` |
| **要点** | 聚合 events 表 + agent_sessions 表数据，返回 `{repo, metrics, agents, activity}` |
| **产出** | 前端 Dashboard 可对接真实数据 |
| **验收** | D1-D2 通过，响应包含完整 5 个 Agent 状态 + 5 条最近事件 |

### Task 2.3 Pipeline API

| 项目 | 内容 |
|------|------|
| **目标** | 实现 `GET /api/pipeline/{owner}/{repo}` |
| **要点** | 从 events 表按 issue_id 聚合，构建 active pipeline + history |
| **产出** | 前端 Pipeline 页面可对接真实数据 |
| **验收** | P1-P2 通过 |

### Task 2.4 Events API

| 项目 | 内容 |
|------|------|
| **目标** | 实现 `GET /api/events/{seq}` 和 `GET /api/events/{owner}/{repo}` |
| **要点** | 单条事件返回上下文+日志+元信息，列表支持分页 |
| **产出** | Event Detail 页面可加载真实事件数据 |
| **验收** | E1-E3 通过 |

### Task 2.5 Agent API

| 项目 | 内容 |
|------|------|
| **目标** | 实现 `GET/PUT /api/agents/{name}/{owner}/{repo}` |
| **要点** | 从 events 表聚合 Agent 指标，从 agent_type_config 读取 Backend |
| **产出** | Agent Detail 页面可展示真实数据，Config 面板可保存 |
| **验收** | G1-G4 通过 |

### Task 2.6 Chat API

| 项目 | 内容 |
|------|------|
| **目标** | 实现 Chat 对话、配置、反馈 4 个端点 |
| **要点** | `POST /send` → 委托 Chat Agent 执行；`POST /feedback` → 构造 A2A 事件给 VOD Agent |
| **产出** | Chat 页面可用，反馈→A2A→VOD 链路闭环 |
| **验收** | C1-C6 通过 |

### Task 2.7 Settings API

| 项目 | 内容 |
|------|------|
| **目标** | 实现 Credentials、Notifications、GitCode Repo、Backend Agents 的 GET/PUT |
| **要点** | 读写 user_accounts 表字段，Backend Agents 调用 `discover_backends()` + `check_health()` |
| **产出** | Settings 页面可管理所有配置 |
| **验收** | S1-S8 通过 |

### Task 2.8 Skills API

| 项目 | 内容 |
|------|------|
| **目标** | 实现技能列表、上传、删除 |
| **要点** | 列表查询 user_skills 表 + skills 文件系统，上传 zip/base64 校验 |
| **产出** | DevOps Skills 页面可展示技能库 |
| **验收** | K1-K6 通过 |

### Task 2.9 Search API

| 项目 | 内容 |
|------|------|
| **目标** | 实现全局搜索 |
| **要点** | 按 `type=event|repo|agent` 分别查询 events/managed_repos/agent_registry |
| **产出** | Topbar 搜索框可用 |
| **验收** | H1-H3 通过 |

### Task 2.10 Token 三层体系实现

| 项目 | 内容 |
|------|------|
| **目标** | 实现 DevOps Bot Token、User Token、Webhook Token 三层隔离 |
| **要点** | `stream_agent` 根据 Agent 类型注入不同 Token：DevOps Agents 使用 Bot Token（系统全局），Chat Agent 使用 User Token（个人 OAuth）；Bot Token 支持回退：repo 级未配置 → 使用 `default` 账户的默认 Token；`user_accounts` 字段复用但语义区分；Bot Token 配置入口在 GitCode Repo 页（含 `default` 默认条目），User Token 在 General Settings → Credentials |
| **产出** | DevOps Agents 自动使用 Bot Token（或回退到默认 Token）；Chat Agent 使用用户个人 Token；Webhook 验签使用独立的 Webhook Token |
| **验收** | 验证 DevOps Agent 提交的 PR/Issue 显示为 Bot 用户身份；未配置 Bot Token 时回退到默认 Token；Chat Agent 提交的操作显示为当前用户身份；Webhook Token 错误时不处理事件 |

### Task 2.11 默认 Bot Token 管理员配置入口

| 项目 | 内容 |
|------|------|
| **目标** | 在 GitCode Repo 页面增加 `default` 条目，允许管理员配置/更新系统默认 Bot Token |
| **要点** | GET/PUT `/api/settings/gitcode-repo/default` 对系统默认 Token 进行读写；SPA 中 GitCode Repo 页面增加 `default` 条目卡片（与普通仓库条目同级，标注"系统默认"），表单字段为 `gitcode_api_token` 和 `gitcode_webhook_token` |
| **产出** | 管理员可在管理员系统中配置默认 Bot Token，新仓库无需单独配置 |
| **验收** | 配置 default Token 后，未配 Token 的仓库自动继承；修改 default Token 后后续事件使用新 Token |

---

## Phase 3：事件处理管道

### Task 3.1 事件归一化重构

| 项目 | 内容 |
|------|------|
| **目标** | 从旧代码提取 VCS 适配器，确保 GitCode 事件归一化正确 |
| **要点** | 保持现有 `normalize_event()` 逻辑，补充未覆盖的事件类型映射 |
| **产出** | 7 种 GitCode 事件类型全部正确归一化 |
| **验收** | Unit test 7 种事件类型全部通过 |

### Task 3.2 事件路由 DB 化

| 项目 | 内容 |
|------|------|
| **目标** | `get_domain_skill()` 从 JSON 文件切换到 DB 查询 |
| **要点** | 三级路由：`repo_skill_mapping` → `event_route_config`(repo覆盖) → `event_route_config`(全局) |
| **产出** | 事件→Skill 路由完全走 DB，前端可管理 |
| **验收** | Bootstrap 迁移验证 + 路由查询测试 |

### Task 3.3 Agent Session 独立化

| 项目 | 内容 |
|------|------|
| **目标** | 从旧 "一个仓库一个 Session" 改为 "(repo_key, agent_type) 一个 Session" |
| **要点** | 修改 `session_manager.py`：`get_or_create_agent_session(repo_key, agent_type, backend)` |
| **产出** | VOD/Coding/SRE 即使同用 opencode 也各自独立 session |
| **验收** | Session 隔离测试 EP7-EP8 通过 |

### Task 3.4 Event History 共享化

| 项目 | 内容 |
|------|------|
| **目标** | events 表独立于 agent_sessions，按 repo 共享 |
| **要点** | `append_event_history(repo_key, event)` → INSERT events；`get_event_history(repo_key)` → SELECT |
| **产出** | 所有 Agent 共享同一份事件历史 |
| **验收** | 不同 Agent 追加事件后，查询返回所有 Agent 的事件 |

### Task 3.5 Prompt 构建增强

| 项目 | 内容 |
|------|------|
| **目标** | Prompt 注入按 Issue 聚类的事件历史，而非原始 JSON |
| **要点** | `build_prompt` 增加 `get_event_history()` 调用 → 按 issue_id 分组 → 结构化文本 |
| **产出** | Agent 接收的 Prompt 包含 "Issue #345: VOD→创建, Issue→分类, Coding→PR" |
| **验收** | Prompt 内容包含按 Issue 的聚类摘要 |

---

## Phase 4：前端 SPA 迁移

### Task 4.1 原型 HTML → 独立 SPA 模块

| 项目 | 内容 |
|------|------|
| **目标** | 将 prototype.html 提取为生产可用的 SPA |
| **要点** | 分离 CSS/JS/HTML，`fetch()` 替换假数据，错误处理 + 加载态 |
| **产出** | SPA 可独立部署访问 |
| **验收** | F1-F11 页面路由全部通过 |

### Task 4.2 API 对接

| 项目 | 内容 |
|------|------|
| **目标** | render 函数从 `fetch()` 获取真实数据替代硬编码 |
| **要点** | 每个 renderXxx 函数增加 `fetch(url, {headers:{Authorization:...}})` + `.then(render)` |
| **产出** | Dashboard/Agent/Pipeline/Chat/Settings 全部使用 API 数据 |
| **验收** | 启动 API 服务后，SPA 可正常加载所有页面 |

### Task 4.3 状态管理与路由守卫

| 项目 | 内容 |
|------|------|
| **目标** | localStorage 存储 token + current_repo，未登录跳转 |
| **要点** | `nav()` 前置检查 token，`login()` 后存 token，`logout()` 清除 |
| **产出** | 未登录 → 登录页；登录后 token 持久化；刷新不丢失 |
| **验收** | F1-F3、F5 登录/退出流程通过 |

### Task 4.4 交互链路补全

| 项目 | 内容 |
|------|------|
| **目标** | 确保所有点击交互正确跳转 |
| **要点** | Dashboard Activity → Event；Pipeline 节点 → Event；Agent 事件 → Event |
| **产出** | 全部交互链路可用 |
| **验收** | F12-F17 交互测试全部通过 |

---

## Phase 5：Webhook 与 A2A 集成

### Task 5.1 Webhook 端点保留与增强

| 项目 | 内容 |
|------|------|
| **目标** | 保持端口 8080 webhook_handler.py，增强 Webhook Token 多账户支持 |
| **要点** | 复用现有 Handler，Token 验证 → resolve_account → 异步处理 |
| **产出** | GitCode Webhook 事件正常路由处理 |
| **验收** | W1-W7 Webhook 测试全部通过 |

### Task 5.2 A2A 协议保留

| 项目 | 内容 |
|------|------|
| **目标** | A2A 端点保持，载荷格式兼容新 Agent 类型 |
| **要点** | `/api/a2a` 不变，`process_a2a_event()` 适配新 `agent_type` |
| **产出** | Issue→Coding、QA→Coding、Event→VOD 三条 A2A 链路可用 |
| **验收** | EP3-EP4 + C6 反馈 A2A 测试通过 |

---

## Phase 6：IM 与 Backend 集成

### Task 6.1 飞书 IM 集成

| 项目 | 内容 |
|------|------|
| **目标** | 保持飞书 WS 长连接 + Slash 命令系统 |
| **要点** | 复用现有 `im/feishu.py` + `im/handler.py`，Chat Prompt 接入新 `build_chat_prompt` |
| **产出** | 飞书群中 @Bot 可对话，Slash 命令正常 |
| **验收** | Chat 对话 + 7 个 Slash 命令全部可用 |

### Task 6.2 Backend Agent 发现

| 项目 | 内容 |
|------|------|
| **目标** | 实现引擎发现与健康检测 |
| **要点** | `shutil.which()` 检测 4 种引擎，`--version` + 最小 Prompt 验证，CodeArts AK/SK 验证 |
| **产出** | Backend Agents 页面可实时监控引擎状态 |
| **验收** | S7-S8 Backend Agents 测试通过 |

---

## Phase 7：体验优化

### Task 7.1 Chat SSE 流式回复

| 项目 | 内容 |
|------|------|
| **目标** | `POST /api/chat/send` 返回 SSE 流而非一次性 JSON |
| **要点** | FastAPI `StreamingResponse` + `text/event-stream`，逐 token 推送 |
| **产出** | 用户发送消息后看到逐字输出 |
| **验收** | Chat 页面实时显示 Agent 思考过程 |

### Task 7.2 Dashboard 自动刷新

| 项目 | 内容 |
|------|------|
| **目标** | Dashboard 数据 30s 自动轮询更新 |
| **要点** | `setInterval(fetchDashboard, 30000)`，数据变化时平滑更新 |
| **产出** | Dashboard 无需手动刷新即可看到最新状态 |
| **验收** | 有新事件后 30s 内 Dashboard 自动更新 |

### Task 7.3 移动端响应式

| 项目 | 内容 |
|------|------|
| **目标** | SPA 在移动端可正常使用 |
| **要点** | @media 断点 768px，侧边栏折叠，Agent Flow 纵向排列 |
| **产出** | 手机浏览器可正常操作所有功能 |
| **验收** | F29-F31 响应式测试通过 |

### Task 7.4 性能优化

| 项目 | 内容 |
|------|------|
| **目标** | API 响应 P95 < 500ms，并发 Webhook 不丢事件 |
| **要点** | events 表索引 (repo_key, seq)，Dashboard 数据一次查询返回 |
| **产出** | 性能测试指标达标 |
| **验收** | P1-P6 性能测试全部通过 |

---

## Phase 8：安全加固

### Task 8.1 全面安全审计

| 项目 | 内容 |
|------|------|
| **目标** | Token 伪造、CSRF、A2A 隔离、Skill 上传校验 |
| **要点** | 逐项验证安全措施是否生效 |
| **产出** | 安全测试报告 |
| **验收** | X1-X10 安全测试全部通过 |

---

## Phase 9：开放兼容

基于 `tech-design.md §20` 的插件模型设计，为后续扩展预留接口。

### Task 9.1 Backend Agent 插件接口

| 项目 | 内容 |
|------|------|
| **目标** | 确保 `AgentBackend` 抽象类可支持第三方 Agent 引擎接入 |
| **要点** | `_BACKENDS` 字典注册机制验证，`discover_backends()` 动态发现逻辑 |
| **产出** | OpenClaw/Hermes/Cursor 任一引擎可用 20 行代码接入 |
| **验收** | 新建 HermesBackend 子类 → 注册 → Backend Agents 页面可检测并测试 |

### Task 9.2 IM 通道扩展接口

| 项目 | 内容 |
|------|------|
| **目标** | 预留 WeCom/DingTalk 通道接入点 |
| **要点** | `IMChannel` 抽象类，`user_accounts` 预留 IM 字段 |
| **产出** | Chat Agent Config 页面的 IM 配置可扩展新通道 |
| **验收** | 通过界面配置新通道后，可正常接收/回复消息 |

### Task 9.3 VCS 平台扩展验证

| 项目 | 内容 |
|------|------|
| **目标** | 验证 `VcsBackend` 适配器可支持 GitHub/GitLab |
| **要点** | 用 GitHub Webhook 模拟数据进行 `normalize()` 测试 |
| **产出** | GitHub 事件可成功归一化为统一格式 |
| **验收** | GitHub PR/Issue/Push 事件归一化单元测试通过 |

## 总览

| Phase | 任务数 | 说明 |
|-------|--------|------|
| P1 基础设施 | 3 | FastAPI + DB + Auth |
| P2 核心 API | 9 | 10 组 REST 端点 |
| P3 事件管道 | 5 | VCS + 路由 + Session + Events + Prompt |
| P4 前端 SPA | 4 | HTML→SPA + API对接 + 状态 + 交互 |
| P5 Webhook/A2A | 2 | Webhook 保留 + A2A 适配 |
| P6 IM/Backend | 2 | 飞书 + 引擎发现 |
| P7 体验优化 | 4 | SSE + Auto-refresh + Mobile + Perf |
| P8 安全 | 1 | 全面审计 |
| P9 开放兼容 | 3 | Agent 插件 + IM 扩展 + VCS 验证 |
| **合计** | **33** | |

### 依赖关系

```
P1 基础设施 ──→ P2 核心 API ──→ P3 事件管道
                    │                │
                    └──→ P4 前端 ←──┘
                              │
                    P5 Webhook/A2A
                    P6 IM/Backend
                              │
                    P7 体验优化
                    P8 安全加固
                    P9 开放兼容

P1→P2→P3 串行，P4 可与 P2/P3 并行开发（前期用 mock 数据），P5/P6 独立可并行，P7/P8 最后进行。
