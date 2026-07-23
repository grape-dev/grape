# DevOps Bot A2A（Agent-to-Agent）协议 — 设计文档

## 背景

### 当前问题

DevOps Bot 的事件处理是事件类型驱动路由：

```
GitCode Webhook → event_processor → 按事件类型路由 → 执行对应 skill → 结束
```

一个 Agent 执行完毕后无法主动调用另一个 Agent。典型断点：

```
issue_comment（用户回复"同意开发"）
  → issue skill → Agent 只能打标签、发评论 → 结束
  → ❌ 无法触发 implementation skill 开始开发
```

### 目标

引入 A2A 协议，使 Agent 执行完毕后能主动调用 DevOps Bot API 触发另一个 Agent，实现多 Agent 协作流水线。

## A2A 协议定义

### 请求格式

```
POST /api/a2a
Content-Type: application/json

{
  "agent": "implementation",
  "payload": {
    "repository": "owner/repo",
    "account": "default",
    "context": "前序 Agent 的处理摘要 + 当前 Agent 的分析结论"
  }
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `agent` | 是 | 下一个要调用的 Agent 名称（对应 skill 目录名） |
| `payload` | 是 | 透传上下文 |
| `payload.repository` | 是 | 仓库标识 |
| `payload.account` | 是 | 账户名 |
| `payload.context` | 是 | 原始 prompt 摘要 + 当前 Agent 处理结论 |

### 处理流程

```
Webhook → event_processor → agent.run()
  → Agent 完成任务后如果判断需要其他 Agent：
    → curl POST http://localhost:8080/api/a2a {"agent": "implementation", ...}
  → Webhook 处理器收到 A2A 请求
  → event_processor 校验 agent 合法性，路由到指定 Agent
  → 下一个 Agent 收到 prompt（含前序 context）
```

### 改动范围

| 文件 | 改动 |
|------|------|
| `src/webhook_handler.py` | 新增 `POST /api/a2a` 路由 |
| `src/event_processor.py` | 新增 `process_a2a_event()`，支持 `_override_agent` |
| `src/prompt_builder.py` | 注入 `_a2a_context` 到 prompt 顶部；追加 A2A 调用说明 |
| `src/agent/executor.py` | 追加注入 `REPO_FULL_NAME`、`ACCOUNT`、`CURRENT_EVENT_TYPE` |

## 详细设计

### 1. A2A API 端点

`src/webhook_handler.py` 新增路由：

```python
if self.path == "/api/a2a" and self.command == "POST":
    length = int(self.headers.get("Content-Length", 0))
    body = json.loads(self.rfile.read(length))
    from src.event_processor import process_a2a_event
    process_a2a_event(body)
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.end_headers()
    self.wfile.write(b'{"status":"ok"}')
```

### 2. event_processor A2A 处理

```python
def process_a2a_event(body: dict):
    agent_name = body.get("agent", "")
    agent_dir = os.path.join(SKILLS_DIR, agent_name)
    if not os.path.isdir(agent_dir):
        log.warning(f"A2A agent not found: {agent_name}")
        return
    payload = body.get("payload", {})
    repository = payload.get("repository", "")
    account = payload.get("account", "default")
    repo_key = f"gitcode:{repository}"
    a2a_evt = {
        "type": "a2a_event",
        "repository": repository,
        "source": "gitcode",
        "actor": "bot",
        "payload": payload,
        "_override_agent": agent_name,
        "_a2a_context": payload.get("context", ""),
    }
    _process_event_inner(a2a_evt, repo_key, account)
```

`_process_event_inner` 支持 `_override_agent`：

```python
def _process_event_inner(evt, repo_key, account):
    override_agent = evt.pop("_override_agent", "")
    if override_agent:
        skills = [override_agent]
    else:
        skills = get_domain_skill(evt['type'], repo_key)
    ...
```

### 3. context 注入

`build_prompt` 收到 `_a2a_context` 后注入到 prompt 顶部：

```python
def build_prompt(evt, ...):
    parts = []
    a2a_ctx = evt.get("_a2a_context", "")
    if a2a_ctx:
        parts.append("## 前序 Agent 处理摘要\n")
        parts.append(a2a_ctx)
        parts.append("请基于以上上下文继续处理。\n")
    ...
```

### 4. Prompt 追加 A2A 调用说明

在 `build_prompt` 末尾追加，由 skill 自身的 SKILL.md 决定是否以及何时调用 A2A：

```
## Agent-to-Agent 协议

如果你认为当前任务需要其他技能继续处理，请先完成当前任务，
然后调用 A2A API：

  curl -X POST http://localhost:8080/api/a2a \
    -H "Content-Type: application/json" \
    -d '{
      "agent": "implementation",
      "payload": {
        "repository": "'"$REPO_FULL_NAME"'",
        "account": "'"$ACCOUNT"'",
        "context": "当前处理摘要（见下方构造规则）"
      }
    }'

### context 构造规则

context 是传给下一个 Agent 的接力说明，应包含：
1. 原始需求：DevOps Bot 给你 prompt 的核心内容摘要
2. 你的分析结论：关键分析结果
3. 建议下一步：需要下一个 Agent 做什么

示例：
原始需求：处理 issue #33 "新增VOD声音查询"（feature 请求）
分析结论：用户请求新增 VOD 声音查询功能，需要修改 vod-service 模块
建议下一步：创建分支 feat/vod-query，实现查询接口

⚡ context 要简洁完整，让下一个 Agent 无需重复分析。
```

### 5. context 传递链路

```
DevOps Bot 生成原始 prompt（含 A2A 说明）
  → 当前 Agent 执行 → 生成 context
  → curl POST /api/a2a 携带 agent + context
  → event_processor 校验 agent 目录存在
  → 将 context 注入下一个 Agent 的 prompt 顶部
  → 下一个 Agent 收到含前序上下文的 prompt
```

### 6. agent 合法性校验

通过检查 `skills/{agent_name}/` 目录是否存在来确认 agent 是否有效，无需注册表文件。

### 7. 环境变量

当前已注入：
- `GITCODE_API_TOKEN`
- `GITCODE_WEBHOOK_TOKEN`

需要追加注入：
- `REPO_FULL_NAME`
- `ACCOUNT`
- `CURRENT_EVENT_TYPE`

### 8. 安全限制

| 限制 | 说明 |
|------|------|
| 本地访问 | A2A API 仅限 `localhost:8080` |
| Agent 校验 | 校验 `skills/{agent_name}/` 目录是否存在 |
| 最大深度 | `max_a2a_depth = 5` |

## 场景验证

### Feature 审批后开发

```
issue_comment（用户回复"同意开发"）
  → issue skill → Agent 检测到批准意图
  → curl POST /api/a2a {"agent": "implementation", "context": "..."}
  → implementation skill → Agent 创建分支、编码、提交 PR
```

### Bug 自动修复

```
issue_opened（bug 类型，auto_impl_enabled=ON）
  → issue skill → Agent 分类、加 PATCH 标签
  → curl POST /api/a2a {"agent": "implementation", "context": "..."}
  → implementation skill → Agent 修复代码、提交 PR
```

## 实施阶段

| 文件 | 改动 |
|------|------|
| `src/webhook_handler.py` | 新增 `POST /api/a2a` 路由 |
| `src/event_processor.py` | 新增 `process_a2a_event()`，`_process_event_inner` 支持 `_override_agent` |
| `src/prompt_builder.py` | 注入 `_a2a_context` 到 prompt 顶部；追加 A2A 调用说明 |
| `src/agent/executor.py` | 追加注入 `REPO_FULL_NAME`、`ACCOUNT`、`CURRENT_EVENT_TYPE` |
| `skills/issue/SKILL.md` | 增加在批准场景下调用 A2A 的指引 |

## 不变的部分

| 模块 | 说明 |
|------|------|
| `agent/*.py` | 后端实现不变 |
| `session_manager.py` | Session/历史管理不变 |
| `user/db.py` | 用户管理不变 |
| `event-routing.json` | 路由规则不变 |
| `im/*.py` | 飞书处理不变 |
| `configs/` | 无需新增配置文件 |
