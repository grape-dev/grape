# 2026-07-29 开发日志

## Chat Agent 全面重构

### 数据模型

- **新增 4 张表**：`chat_config`、`chat_conversations`、`chat_messages`、`user_im_bindings`
- **`chat_config`**（每用户一行）：存储用户 Chat Agent 配置 — `backend`、`skill`、`feishu_app_id`、`feishu_app_secret`
- **`chat_conversations`**：对话会话，按 `user_id` + `repo_key` 隔离。字段：`session_id`（opencode 真实 session）、`user_gitcode_token`（用户 GitCode Token）
- **`chat_messages`**：对话中的消息，`role`（user/agent）、`content`
- **`user_im_bindings`**：用户个人 IM 通知通道，用于 Agent 主动推送

### 页面 UI

- **三栏布局**：左侧对话列表 | 中间对话区 | 右侧配置面板
- **对话列表**：按当前仓库过滤，hover 显示 ✕ 删除按钮
- **自动选择**：打开 Chat Agent 自动选中第一个对话，输入框默认可用
- **固定高度**：Main Chat 窗口固定高度 `calc(100vh - 96px)`，消息溢出时 body 内部滚动
- **配置面板**：Backend 选择（从 `backend_agents` 缓存加载）、Skill 选择（从 `agent_type_config` 加载 chat 技能）、IM 配置（Feishu/WeCom/DingTalk + App ID/Secret）

### 对话标题

- 首次消息自动生成标题（取消息前 30 字符）
- `_auto_title()` 在 `send_message` / `send_message_stream` 中调用

### 后端调用

- **真实调用 backend**：`stream_agent()` 替代 mock 回复
- **使用当前用户 Token**：`account=user["username"]` + `env_extra={"GITCODE_API_TOKEN": token}`
- **Token 持久化**：`chat_conversations.user_gitcode_token` 字段，创建对话时自动存储
- **Skill 注入**：从 `chat_config.skill` 读取，加载 `skills/{name}/SKILL.md` 到 prompt

### Session 隔离

- `create_conversation` 不再生成假 UUID 作为 session_id
- 首次调用 opencode 不传 `--session`，自动创建新 session
- opencode 返回的 `oc_session_id`（`ses_*`）存入 `chat_conversations.session_id`
- 后续同一 conversation 的调用复用该 session ID
- session_id 校验：只有以 `ses_` 开头的才认为是真实 session

### Prompt 构建

```python
_build_chat_prompt(message, repo_key, history, skill_name=""):
    CHAT_SYSTEM_PROMPT
    + Current repository（从 conversation.repo_key 取）
    + Skill: {name}（如果配置了 skill，加载 SKILL.md）
    + Conversation history（最近 10 条）
    + User: {message}
```

### 工作目录修复

- `stream_agent` 的 `cwd` 从 `os.getcwd()`（服务端 git 仓库）改为 `tempfile.gettempdir()`
- 修复 opencode 检测到服务端 `.git` 而忽略 prompt 中的 repo 信息问题

### SSE 流

- `send_message_stream` 使用 `asyncio.Queue` + `run_in_executor` 实现同步→异步桥接
- 支持 `resp.body` 为 null 时的 fallback（`resp.text()`）
- XSS 安全：用户消息 `<` 转义

---

## Backend Agents

- **`backend_agents` 新表**：缓存 backend 元数据（name、installed、version、healthy）
- **手动刷新**：`POST /api/settings/backend-agents/refresh`，仅存储已安装的 backend
- **AK/SK**：Backend Agents 页面支持 CodeArts AK/SK 配置，通过 `PUT /settings/credentials` 保存
- **`settings.py`**：`GET/PUT /credentials` 增加 codearts 字段

---

## 飞书 IM

- **WS 启动**：`main.py` 添加 lifespan 事件，后台线程启动 `feishu.start_ws(process_chat)`
- **事件循环修复**：`start_ws()` 创建独立 asyncio 事件循环，覆盖 lark_oapi 模块级 loop 变量
- 目前飞书 WebSocket 已成功连接

---

## Skills 页面

- **数据源从 DB 读取**：`GET /api/skills/` 不再使用硬编码 `AGENT_CATEGORIES`
- 从 `agent_type_config WHERE parent_agent_type!=''` 查询
- 前端 `renderSkills()` 调 API 渲染，按 agent_type 分组
- `user_skills` 表增加 `agent_type`、`description`、`parent_agent_type` 字段

---

## 修复记录

| 问题 | 修复 |
|------|------|
| 角色显示 Member | `loadProfile()` 保存到 localStorage，托管/取消后重新调用 |
| OAuth 回调地址错误 | `.env` 补上 `OAUTH_REDIRECT_URI` |
| 侧边栏多个按钮同时高亮 | `nav()` 先清除所有 `.sb-nav a` 的 `act` |
| SSE `AttributeError` | `isinstance(data, dict)` 判断，处理 raw 事件字符串类型 |
| Main Chat 无限延伸 | `chat-wrap` 使用固定高度 |
| Backend 返回空文本 | done 事件兜底 + 会话隔离 + prompt 修复 |
