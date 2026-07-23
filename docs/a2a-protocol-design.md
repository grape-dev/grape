# DevOps Bot A2A（Agent-to-Agent）协议 — 设计文档

## 背景

### 当前问题

DevOps Bot 的事件处理是 **事件类型驱动路由**：

```
GitCode Webhook 事件
  → event_processor
    → 按事件类型（issue_opened / issue_comment / issue_labeled ...）路由
      → 匹配 configs/event-routing.json 或 repo_skill_mapping
        → 执行对应 Skill（issue / implementation / pr ...）
```

这种架构下，一个 Agent 执行完毕后无法主动调用另一个 Agent。典型断点场景：

```
issue_comment（用户回复"同意开发"）
  → 路由到 issue skill
  → Agent 只能打标签、发评论
  → Agent 结束
  → ❌ 无法触发 implementation skill 开始开发
```

用户需要在 issue 评论后再手动打 label，触发 `issue_labeled` 事件，才能进入 `implementation` skill。流程断裂，体验割裂。

### 目标

引入 **A2A（Agent-to-Agent）协议**，使 Agent 在执行完毕后，能够通过调用 DevOps Bot API 主动触发另一个 Agent，实现多 Agent 协作流水线。

## 架构设计

### A2A 协议定义

Agent 在需要调用其他技能时，通过 **HTTP POST** 向 DevOps Bot 提交 A2A 事件：

```
POST /api/a2a
Content-Type: application/json

{
  "event_type": "issue_comment",     // 事件类型（可选，默认沿用当前事件类型）
  "skill": "implementation",          // 下一个要执行的技能（必填）
  "payload": {                        // 必传上下文（可选）
    "repository": "owner/repo",
    "account": "default"
  }
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `skill` | 是 | 下一个要执行的技能名称（对应 skill 目录名） |
| `event_type` | 否 | 覆盖的事件类型，不传则沿用当前事件类型 |
| `payload` | 否 | 透传最小上下文，`repository` 和 `account` 必含 |

### 处理流程变化

当前（单次执行）：
```
Webhook → event_processor → agent.run() → 结束
```

新架构（Agent 主动触发 A2A）：
```
Webhook → event_processor → agent.run(skill_prompt)
  → Agent 完成任务后，如果判断需要其他技能：
    → curl POST http://devops-bot:8080/api/a2a
  → Webhook 处理器收到 A2A 请求
  → event_processor 切换 skill 重新执行
```

### 改动范围

| 文件 | 改动 |
|------|------|
| `src/webhook_handler.py` | 新增 `POST /api/a2a` 路由 |
| `src/event_processor.py` | 新增 `process_a2a_event()`，支持 `_override_skill` |
| `src/prompt_builder.py` | 在 prompt 末尾追加 A2A API 调用说明 |
| `skills/*/SKILL.md` | 可选：增加 A2A 使用指引 |

## 详细设计

### 1. A2A API 端点

在 `webhook_handler.py` 中新增路由（Webhook 服务器端口 8080）：

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

新增 `process_a2a_event()`：

```python
VALID_SKILLS = {"issue", "implementation", "pr", "cloud-deploy",
                "build-debug", "deploy-guard", "changelog-gen",
                "im-notify", "skill-pr", "agent"}

def process_a2a_event(body: dict):
    """处理 Agent 提交的 A2A 事件，路由到指定 skill"""
    skill = body.get("skill", "")
    if skill not in VALID_SKILLS:
        log.warning(f"Invalid A2A skill: {skill}")
        return
    payload = body.get("payload", {})
    repository = payload.get("repository", "")
    event_type = body.get("event_type", "")
    account = payload.get("account", "default")
    repo_key = f"gitcode:{repository}"
    a2a_evt = {
        "type": event_type,
        "repository": repository,
        "source": "gitcode",
        "actor": payload.get("actor", "bot"),
        "payload": payload,
        "_override_skill": skill,
    }
    _process_event_inner(a2a_evt, repo_key, account)
```

修改 `_process_event_inner` 支持 `_override_skill`：

```python
def _process_event_inner(evt, repo_key, account):
    override_skill = evt.pop("_override_skill", "")
    if override_skill:
        log.info(f"A2A override: routing to skill={override_skill}")
        skills = [override_skill]
    else:
        skills = get_domain_skill(evt['type'], repo_key)
    for skill in skills:
        # 构建 prompt + 执行 Agent
        ...
```

### 3. Prompt 追加 A2A 调用说明

在 `build_prompt` 末尾追加，告知 Agent 如何发起 A2A：

```
## Agent-to-Agent 协议

如果你认为当前任务需要其他技能继续处理（例如 issue 审批通过后需要开发），
请先完成当前任务（发布必要的评论/通知），然后调用 A2A API：

  curl -X POST http://localhost:8080/api/a2a \
    -H "Content-Type: application/json" \
    -d '{
      "event_type": "'"$CURRENT_EVENT_TYPE"'",
      "skill": "implementation",
      "payload": {
        "repository": "'"$REPO_FULL_NAME"'",
        "account": "'"$ACCOUNT"'"
      }
    }'

可用技能：
- implementation：代码实现与提交
- pr：Pull Request 审核
- cloud-deploy：云部署
- issue：Issue 处理
- build-debug：构建调试

⚡ 注意：A2A API 调用是独立操作，不影响你已发布的评论/通知。
```

### 4. Agent 调用 curl 的变量来源

`$GITCODE_WEBHOOK_TOKEN`、`$ISSUE_NUMBER`、`$REPO_FULL_NAME` 等变量通过 `executor.py` 注入到 Agent 的环境变量中，Agent 可直接在 curl 命令中引用。

当前已注入的环境变量（`src/agent/executor.py`）：
- `GITCODE_API_TOKEN`
- `GITCODE_WEBHOOK_TOKEN`

需要追加注入：
- `REPO_FULL_NAME`（如 `owner/repo`）
- `ACCOUNT`（账户名）
- `CURRENT_EVENT_TYPE`（当前事件类型，如 `issue_comment`）

### 5. 安全限制

| 限制 | 说明 |
|------|------|
| 本地访问 | A2A API 仅限 `localhost:8080` 访问，不对外暴露 |
| Skill 白名单 | 只允许 `VALID_SKILLS` 中已注册的技能 |
| 最大调用深度 | `max_a2a_depth = 5`，防止无限循环 |
| 日志记录 | 每次 A2A 跳转记录完整链路 |

## 场景验证

### 场景一：Feature 审批后自动开发

```
用户创建 Issue（feature 类型）
  → issue_opened → issue skill
    → Agent 打标签、指派、回复等待批准
    → 用户回复"同意开发"
    → issue_comment → issue skill
    → Agent 检测到批准意图
    → curl POST /api/a2a {"skill": "implementation"}
  → event_processor 切换 implementation skill
    → Agent 创建分支、编码、提交 PR
```

### 场景二：Bug 自动修复

```
用户创建 Issue（bug 类型）
  → issue_opened → issue skill（auto_impl_enabled=ON）
    → Agent 分类、加 PATCH 标签
    → curl POST /api/a2a {"skill": "implementation"}
  → 切换 implementation skill
    → Agent 分析代码、修复、提交 PR
```

## 实施阶段

### 阶段 1：核心协议

| 文件 | 改动 |
|------|------|
| `src/webhook_handler.py` | 新增 `POST /api/a2a` 路由 + `process_a2a_event` 调用 |
| `src/event_processor.py` | 新增 `process_a2a_event()`，`_process_event_inner` 支持 `_override_skill` |
| `src/agent/executor.py` | 追加注入 `GITCODE_WEBHOOK_TOKEN`、`ISSUE_NUMBER`、`REPO_FULL_NAME` 等变量 |
| `src/prompt_builder.py` | 追加 A2A API 调用说明到 prompt |

### 阶段 2：Skill 适配

| 文件 | 改动 |
|------|------|
| `skills/issue/SKILL.md` | 增加在批准场景下调用 A2A 的指引 |
| `skills/bug/SKILL.md` | 可选：增加 A2A 指引 |

### 阶段 3：监控与安全

| 改动 | 说明 |
|------|------|
| A2A 调用链日志 | 记录 `issue → implementation` 完整链路 |
| 最大深度限制 | `max_a2a_depth = 5` 硬限制 |

## 不变的部分

| 模块 | 说明 |
|------|------|
| `agent/*.py` | 后端实现（opencode / CodeArts）不变 |
| `session_manager.py` | Session/历史管理不变 |
| `user/db.py` | 用户管理不变 |
| `event-routing.json` | 路由规则不变，A2A 在路由之后生效 |
| `im/*.py` | 飞书处理不变 |
