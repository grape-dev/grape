# VOD Agent IM 设计方案

> VOD Agent IM 作为 DevOps Bot 对外收集用户声音、提交 Issue 建议、查询 Issue 处理状态的公共对外发声出入口。

---

## 一、架构定位

### 与 Chat Agent 的对比

| 维度 | Chat Agent | VOD Agent IM |
|------|-----------|-------------|
| **角色** | 个人专属对话助手 | 项目公共发声入口 |
| **凭证级别** | 用户级（`chat_config.user_id`） | 全局级（`vod_bot_config`） |
| **绑定方式** | 用户自行绑定 | `/bind` 命令 + 仓库必须预先配置 |
| **会话隔离** | `chat_conversations` | `vod_conversations` |
| **WS 连接** | 每用户 1 个 | 每种凭证 1 个（全局） |
| **目标用户** | 个人 | 群内所有人 |

### 三层模型

```
vod_bot_config (1) ──── (N) agent_sessions ──── (N) vod_conversations
     凭证                仓库 VOD 配置           群独立对话 + oc_session

凭证：每个 bot_app_id 存一条
配置：agent_sessions.repo_key + "vod" 指向 vod_bot_config
对话：每个群一条记录，绑定到 agent_session_id
```

**一个 VOD Agent 只绑定一个机器人。** 仓库在 Dashboard 中配置 VOD Agent 时填写机器人凭证，系统据此验证 `/bind` 请求的合法性。

---

## 二、数据表设计

### 2.1 新增 `vod_bot_config`

```sql
CREATE TABLE IF NOT EXISTS vod_bot_config (
    bot_app_id TEXT PRIMARY KEY,
    bot_app_secret TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
)
```

统一存储 VOD 机器人的 Feishu 凭证。每种 `bot_app_id` 只存一次，不按仓库重复。

### 2.2 新增 `vod_conversations`

```sql
CREATE TABLE IF NOT EXISTS vod_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_session_id TEXT NOT NULL,          -- → agent_sessions.id
    feishu_chat_id TEXT NOT NULL,            -- 飞书群 chat_id
    feishu_chat_name TEXT DEFAULT '',        -- 群名称（便于辨识）
    oc_session_id TEXT DEFAULT '',           -- opencode 会话 ID（上下文记忆）
    status TEXT DEFAULT 'active',            -- active / inactive
    created_at TEXT DEFAULT (datetime('now')),
    last_active_at TEXT DEFAULT (datetime('now')),
    UNIQUE(agent_session_id, feishu_chat_id)
)
```

每个群的独立对话状态。`oc_session_id` 用于延续 VOD Agent 的对话上下文，不同群之间隔离。

### 2.3 修改 `agent_sessions`

```sql
ALTER TABLE agent_sessions ADD COLUMN vod_bot_app_id TEXT DEFAULT '';
-- 指向 vod_bot_config.bot_app_id，表示该仓库的 VOD 使用哪个机器人凭证
```

仓库 VOD 配置指向全局机器人凭证，而非重复存储 app_secret。

---

## 三、配置流程

### 3.1 管理员配置 VOD 机器人凭证

```
管理员进入 Dashboard → VOD Agent → Configure

┌─ Agent Configuration ──────────────────────────┐
│  Backend  [opencode ▼]                         │
│  Skill    [开发者声音分析(vod) ▼]              │
│                                                 │
│  ── IM 配置 ──                                   │
│  机器人凭证                                        │
│  ┌───────────────────────────────────────────┐  │
│  │ Feishu App ID  [cli_xxxxxxxxxxxx]         │  │
│  │ Feishu Secret  [****************]         │  │
│  │ [保存凭证] → upsert vod_bot_config        │  │
│  └───────────────────────────────────────────┘  │
│                                                 │
│  已绑定的群                                       │
│  ┌───────────────────────────────────────────┐  │
│  │ 研发讨论群 · team/repo-a       🟢 已绑定   │  │
│  │ 技术反馈群 · org/repo-b        🟢 已绑定   │  │
│  │ ...                                       │  │
│  └───────────────────────────────────────────┘  │
│                                                 │
└─────────────────────────────────────────────────┘
```

保存后：
1. `vod_bot_config` 写入/更新凭证
2. `agent_sessions.vod_bot_app_id = bot_app_id`
3. 如尚未启动 WS → 启动全局 VOD WebSocket 连接

### 3.2 绑定流程（自动/手动）

#### 自动绑定（无 `/bind` 命令时的首次消息）

```
用户 @VOD Bot 在群 A 发送消息
  │
  ▼
查 vod_conversations WHERE feishu_chat_id = "群A"
  ├── 已有记录 → 正常处理消息
  └── 无记录 → 自动绑定流程：
        │
        查 agent_sessions WHERE agent_type="vod" AND vod_bot_app_id=当前bot
          ├── 0 条 → 回复 "❌ 当前机器人未绑定任何仓库，请联系管理员在 Dashboard 中配置"
          ├── 1 条 → 自动创建 vod_conversations + 回复 "✅ 已自动绑定仓库 {repo}"
          └── N 条 → 回复当前机器人已绑定的仓库列表，提示 "请发送 /bind owner/repo 指定要绑定的仓库"
```

#### 手动绑定（群内 `/bind`）

```
用户在群内发送：
  /bind team/repo-a

VOD WS 收到消息 → vod_handler 处理：
  ① 解析 /bind → repo_key = "team/repo-a"
  ② 查 agent_sessions WHERE repo_key=? AND agent_type="vod"
      ├── 找到且 vod_bot_app_id=当前bot → 取 agent_session_id
      ├── 找到但 vod_bot_app_id≠当前bot → 回复 "❌ 仓库 team/repo-a 使用了其他机器人凭证，无法绑定"
      └── 未找到（仓库未加入） → 回复 "❌ 仓库 team/repo-a 未托管，请先加入该仓库"
  ③ INSERT vod_conversations (agent_session_id, feishu_chat_id, feishu_chat_name)
  ④ 回复 "✅ 已绑定仓库 team/repo-a，现在可以向我提交反馈了"
```

**核心验证：** `/bind` 时必须检查目标仓库的 `agent_sessions.vod_bot_app_id` 是否等于当前 WS 使用的 `bot_app_id`。只有配置了该机器人的仓库才能被绑定。

### 3.3 解绑流程

```
用户在群内发送：
  /unbind

VOD WS 收到 → vod_handler 处理：
  ① 查 vod_conversations WHERE feishu_chat_id=?
  ② 删除该行
  ③ 回复 "已解除绑定，不再接收该仓库的消息"
```

### 3.4 通用命令

VOD IM 支持以下命令（与 Chat Agent 保持一致）：

| 命令 | 功能 | 说明 |
|------|------|------|
| `/bind owner/repo` | 绑定仓库 | 将当前群绑定到指定仓库 |
| `/unbind` | 解绑仓库 | 解除当前群的绑定关系 |
| `/status` | 查看绑定 | 显示当前群绑定的仓库 |
| `/help` | 帮助 | 显示可用命令列表 |
| `/clean` / `/reset` | 清除会话 | 清除当前对话的 oc_session_id，重置上下文 |

不同点（Chat Agent 有但 VOD IM 不需要）：

| Chat Agent 命令 | VOD IM | 原因 |
|----------------|--------|------|
| `/autodev on/off` | ❌ 不支持 | 自动实现是 Chat Agent 专属功能 |
| `/notify on/off` | ❌ 不支持 | 通知开关是 Chat Agent 专属功能 |
| `/refresh` | ❌ 不支持 | 用户映射同步是 Chat Agent 专属功能 |

---

## 四、消息处理流程

### 4.1 完整消息链路

```
用户 @VOD Bot 在群 A 发送消息
  │
  ▼
VOD WebSocket 收到 im.message.receive_v1
  │
  ├── 检查是否为命令（/bind, /unbind, /list）
  │     ├── 是 → 执行命令逻辑
  │     └── 否 → 继续
  │
  ├── 查 vod_conversations WHERE feishu_chat_id="群A"
  │     ├── 找到 → 取 agent_session_id + oc_session_id，继续处理
  │     └── 没找到 → 自动绑定流程：
  │           ├── 查 agent_sessions WHERE agent_type="vod" AND vod_bot_app_id=当前bot
  │           │     ├── 0 条 → 回复 "未绑定任何仓库，请联系管理员配置"
  │           │     ├── 1 条 → 自动创建 vod_conversations，取 agent_session_id
  │           │     └── N 条 → 列出可绑仓库，提示 /bind
  │           └── return（暂不处理本次消息，用户需先绑定）
  │
  ├── 查 agent_sessions WHERE id=agent_session_id
  │     ├── 取 repo_key / backend / skill / vod_bot_app_id
  │     └── 查 vod_bot_config WHERE bot_app_id=vod_bot_app_id
  │
  ├── 构建事件（event_type="vod_signal", repo_key, actor, payload）
  │
  ├── 调 _execute_agent(session_id=oc_session_id)
  │     ├── 使用 skills/vod/SKILL.md
  │     └── 共享 VOD Agent session
  │
  ├── Agent 执行完毕
  │     ├── 用 get_tenant_token_from(bot_app_id, bot_app_secret) 获取 token
  │     ├── reply_card() 或 reply_text() 回复到群 A
  │     └── 更新 vod_conversations.oc_session_id
  │
  └── 如有 Issue 创建等操作，更新 events 表
```

### 4.2 WebSocket 启动

```python
# main.py lifespan 中
async def lifespan(app):
    # 已有：Chat Agent WS
    threading.Thread(target=start_ws, args=(process_chat,), daemon=True).start()

    # 新增：VOD Bot WS（每个 vod_bot_config 一条）
    for each vod_bot_config:
        threading.Thread(target=start_ws, args=(vod_handler, account=bot_id), daemon=True).start()
```

---

## 五、VOD IM Prompt 结构

### 5.1 VOD IM 角色定义（Role Definition）

VOD IM 使用独立的角色定义，与 Chat Agent 的通用 DevOps 角色区分：

```
你是项目的**公共反馈入口**，部署在飞书群中供所有成员提交建议和反馈。

你的核心能力：
1. **收集反馈** — 收集开发者对项目的问题、建议、痛点，记录到 Issue
2. **创建 Issue** — 将确认的反馈自动创建为 GitCode Issue，标注来源 "飞书用户反馈"
3. **查询进展** — 查询 Issue/PR 当前处理状态、CI 状态、部署状态
4. **解答疑问** — 回答关于项目工作流、代码规范、团队流程等问题

你的限制：
- ❌ 不修改代码、不创建 PR、不合并代码
- ❌ 不处理个人闲聊、天气、新闻等非 DevOps 话题
- ❌ 不回复敏感信息（Token、密码、AK/SK）

回复规则：
1. 用户在群内 @你 或有明确 DevOps 问题时，必须回复
2. 始终用中文回复，简洁清晰
3. 创建的 Issue 必须在正文中标注 `auto-submitted by VOD Agent (Feishu)`
4. 不确定的问题，回复 "已记录你的反馈，我会转给相关团队处理"
5. 不要输出内部推理过程、分析步骤等英文内容
```

### 5.2 完整 Prompt 结构

```
## 用户消息
来自 {user_name} 在仓库 {repo_key}
消息内容: {用户发送的消息文本}

## 仓库 Issue/PR 上下文
[get_issue_summary() — 当前活跃的 Issue 列表]
（可选，如有活跃 Issue 才展示）

## Skill: vod
[skills/vod/SKILL.md 完整内容]
反馈收集 → 聚类分析 → 创建 Issue → 输出摘要

## 角色定义
[VOD IM 专属 role definition — 见 5.1]

## Available Agents
[A2A 协议指令 + agent-description.json 列表]
```

### 5.3 Prompt 构建代码示意

```python
# im/vod_handler.py
def _build_vod_prompt(evt: dict, oc_session_id: str, skill_name: str) -> str:
    from src.config import SKILLS_DIR
    from src.prompt_builder import build_role_definition
    from src.api.event_store import get_issue_summary
    import os, json

    parts = []
    user_msg = evt.get("payload", {}).get("user_message", "")
    user_name = evt.get("actor", "未知用户")
    repo = evt.get("repository", "")

    parts.append(f"## 用户消息\n来自 {user_name} 在仓库 {repo}")
    parts.append(f"消息内容: {user_msg[:2000]}")
    parts.append("")

    # Issue 上下文
    repo_key = f"gitcode:{repo}"
    summary = get_issue_summary(repo_key)
    if summary:
        parts.append(summary)
        parts.append("")

    # Skill 注入
    skill_path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    if os.path.exists(skill_path):
        with open(skill_path, encoding="utf-8") as f:
            parts.append(f"## Skill: {skill_name}\n{f.read()}\n")
    else:
        parts.append(f"Read the file at {skill_path} using the read tool. Execute its instructions step by step.")

    # VOD IM 专用角色定义（见 5.1）
    parts.append("## 角色定义")
    parts.append(_VOD_ROLE_DEFINITION)

    # A2A 协议指令
    try:
        agents_file = os.path.join(os.path.dirname(__file__), "..", "configs", "agent-description.json")
        if os.path.isfile(agents_file):
            with open(agents_file) as f:
                registry = json.load(f)
            parts.append("\n## Available Agents (for A2A calls)")
            for agent_type, info in registry.items():
                parts.append(f"- {info.get('name', agent_type)} ({agent_type})")
    except Exception:
        pass

    return "\n".join(parts)
```

---

## 六、涉及文件
|------|------|------|
| `src/api/db_setup.py` | 新增 | `vod_bot_config` + `vod_conversations` DDL + migration |
| `src/im/vod_handler.py` | **新建** | VOD WS 消息处理：绑定/解绑/消息路由/_execute_agent |
| `src/im/feishu.py` | 修改 | WS 管理支持多实例；`get_tenant_token_from()` 已有 |
| `src/api/routes/agents.py` | 修改 | PUT 保存凭证到 `vod_bot_config` + 返回绑定列表 |
| `src/api/static/js/pages/agents.js` | 修改 | 配置面板增加凭证输入 + 已绑定群列表 + 绑定入口 |
| `src/api/main.py` | 修改 | lifespan 启动 VOD WS |
| `configs/agent-description.json` | 无变更 | VOD subscribe_events 无需改动 |

---

## 七、安全

| 措施 | 说明 |
|------|------|
| **绑定验证** | `/bind` 时必须验证仓库的 `vod_bot_app_id` 等于当前 WS 所用凭证，否则拒绝 |
| **自动绑定** | 无 `vod_conversations` 且只有 1 个仓库时自动创建绑定 |
| **Secret 掩码** | 前端输入 `********` 时不修改，与 Chat Agent 逻辑一致 |
| **WS 凭证隔离** | VOD Bot 与 Chat Agent 使用不同的 WS 连接和 token |
| **会话隔离** | 不同群的 `oc_session_id` 独立存储，互不干扰 |

---

## 八、与 Chat Agent `/bind` 对比

| | Chat Agent `/bind` | VOD Bot `/bind` |
|---|---|---|
| 存储表 | `feishu_bindings` | `vod_conversations` |
| 凭证来源 | `chat_config`（用户级） | `vod_bot_config`（全局级） |
| 绑定验证 | 无额外验证 | 需验证仓库已配置当前机器人 |
| 群绑仓库 | `chat_id → repo_key` | `chat_id → agent_session_id → repo_key` |
| 绑定前提 | 无 | 仓库必须在 Dashboard 配置了机器人凭证 |
| 自动绑定 | 不支持 | 仅 1 个仓库时自动创建 vod_conversations |
| 群支持 | 1 个群绑定 1 个仓库 | 1 个群绑定 1 个仓库 |
| 仓库支持 | 1 个仓库可被多个群绑定 | 1 个仓库可被多个群绑定 |
