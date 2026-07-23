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

引入 **A2A（Agent-to-Agent）协议**，使 Agent 在执行完毕后，能够通过调用 DevOps Bot API 主动调用另一个 Agent，实现多 Agent 协作流水线。

## 架构设计

### Agent 注册表（Agent Registry）

当前 Agent 需要知道有哪些 Agent 可用、各自能干什么，才能决定将任务交给谁。每个 Agent 对应一个技能目录（`skills/{agent_name}/`），在注册表中注册其名称和能力描述。

```python
# src/config.py 或独立文件
AGENT_REGISTRY = {
    "issue": {
        "description": "处理 Issue 分类、打标签、回复用户、审批确认",
    },
    "implementation": {
        "description": "代码实现：创建分支、编码、提交、创建 PR",
    },
    "pr": {
        "description": "Pull Request 审核：检查代码质量、添加评论",
    },
    "cloud-deploy": {
        "description": "云部署：SSH 登录服务器、拉取代码、重启服务",
    },
    "build-debug": {
        "description": "构建调试：分析构建失败日志、修复编译错误",
    },
    "deploy-guard": {
        "description": "部署守护：监控部署状态、回滚异常发布",
    },
    "changelog-gen": {
        "description": "生成变更日志：基于 commit 历史生成 CHANGELOG",
    },
    "skill-pr": {
        "description": "技能仓库 PR 审核：校验自定义技能格式",
    },
}
```

### A2A 协议定义

Agent 在需要调用其他 Agent 时，通过 **HTTP POST** 向 DevOps Bot 提交 A2A 事件：

```json
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
| `agent` | 是 | 下一个要调用的 Agent 名称（对应 AGENT_REGISTRY 中的 key） |
| `payload` | 是 | 透传上下文 |
| `payload.repository` | 是 | 仓库标识 |
| `payload.account` | 是 | 账户名 |
| `payload.context` | 是 | 原始 prompt 摘要 + 当前 Agent 处理结论 |

### 处理流程变化

当前（单次执行）：
```
Webhook → event_processor → agent.run() → 结束
```

新架构（Agent 主动触发 A2A）：
```
Webhook → event_processor → agent.run(prompt + AGENT_REGISTRY)
  → Agent 完成任务后，查阅注册表决定下一步：
    → curl POST http://devops-bot:8080/api/a2a {"agent": "implementation", ...}
  → Webhook 处理器收到 A2A 请求
  → event_processor 路由到指定 Agent
```

### 改动范围

| 文件 | 改动 |
|------|------|
| `src/config.py` | 新增 `AGENT_REGISTRY` 常量 |
| `src/webhook_handler.py` | 新增 `POST /api/a2a` 路由 |
| `src/event_processor.py` | 新增 `process_a2a_event()`，支持 `_override_agent` |
| `src/prompt_builder.py` | 在 prompt 中注入 `AGENT_REGISTRY` + A2A 调用说明 |
| `src/agent/executor.py` | 追加注入 `REPO_FULL_NAME`、`ACCOUNT`、`CURRENT_EVENT_TYPE` |

## 详细设计

### 1. Agent 注册表

在 `src/config.py` 中定义 `AGENT_REGISTRY`：

```python
AGENT_REGISTRY = {
    "issue":          {"description": "Issue 分类、打标签、回复、审批确认"},
    "implementation": {"description": "代码实现：创建分支、编码、提交 PR"},
    "pr":             {"description": "PR 审核：代码质量检查、评论"},
    "cloud-deploy":   {"description": "云部署：SSH 部署、重启服务"},
    "build-debug":    {"description": "构建调试：分析失败日志、修复"},
    "deploy-guard":   {"description": "部署守护：状态监控、异常回滚"},
    "changelog-gen":  {"description": "变更日志：基于 commit 生成 CHANGELOG"},
    "skill-pr":       {"description": "技能仓库 PR 审核"},
}
```

### 2. A2A API 端点

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

### 3. event_processor A2A 处理

```python
from src.config import AGENT_REGISTRY

def process_a2a_event(body: dict):
    agent_name = body.get("agent", "")
    if agent_name not in AGENT_REGISTRY:
        log.warning(f"Invalid A2A agent: {agent_name}")
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

修改 `_process_event_inner` 支持 `_override_agent`：

```python
def _process_event_inner(evt, repo_key, account):
    override_agent = evt.pop("_override_agent", "")
    if override_agent:
        log.info(f"A2A: routing to agent={override_agent}")
        skills = [override_agent]
    else:
        skills = get_domain_skill(evt['type'], repo_key)
    for skill in skills:
        # 构建 prompt（含 AGENT_REGISTRY + A2A 说明）+ 执行 Agent
        ...
```

`build_prompt` 收到 `_a2a_context` 后，注入到 prompt 顶部：

```python
def build_prompt(evt, ...):
    parts = []
    a2a_ctx = evt.get("_a2a_context", "")
    if a2a_ctx:
        parts.append("## 前序 Agent 处理摘要\n")
        parts.append(a2a_ctx)
        parts.append("请基于以上上下文继续处理。\n")
    # 后续拼接原始 prompt ...
```

### 4. Prompt 注入 Agent 注册表 + A2A 调用说明

在 `build_prompt` 末尾追加。注意：`AGENT_REGISTRY` 在 prompt 中动态渲染为 JSON，Agent 可直接读取。

```
## 可用 Agent 列表

以下是你可以调用的其他 Agent。当你完成任务后，如果判断需要其他 Agent 继续处理，
查表选择最合适的 Agent，然后调用 A2A API。

{{AGENT_REGISTRY_JSON}}

### 调用方式

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

`context` 是传给下一个 Agent 的"接力说明"，应包含：
1. **原始需求**：DevOps Bot 给你 prompt 的核心内容摘要
2. **你的分析结论**：你通过分析得出的关键信息
3. **建议下一步**：需要下一个 Agent 做什么

示例：
```
原始需求：处理 issue #33 "新增VOD声音查询"（feature 请求）
分析结论：用户请求新增 VOD 声音查询功能，需要修改 vod-service 模块
建议下一步：创建分支 feat/vod-query，实现查询接口
```

⚡ context 要简洁完整，让下一个 Agent 无需重复分析。
```

### 5. context 的传递链路

```
DevOps Bot 生成原始 prompt（含 AGENT_REGISTRY + A2A 说明）
  → 当前 Agent 收到 prompt + 执行
    → Agent 查阅注册表，判断应调用哪个 Agent
    → Agent 生成 context（原始需求摘要 + 自己的结论）
    → Agent 发 curl POST /api/a2a 携带 agent + context
  → event_processor 收到 A2A 请求
    → 校验 agent 是否在注册表中
    → 将 context 注入到下一个 Agent 的 prompt 顶部
    → 下一个 Agent 收到含前序上下文的 prompt + 完整注册表
```

### 6. 环境变量

当前已注入的环境变量（`src/agent/executor.py`）：
- `GITCODE_API_TOKEN`
- `GITCODE_WEBHOOK_TOKEN`

需要追加注入：
- `REPO_FULL_NAME`（如 `owner/repo`）
- `ACCOUNT`（账户名）
- `CURRENT_EVENT_TYPE`（当前事件类型，如 `issue_comment`）

### 7. 安全限制

| 限制 | 说明 |
|------|------|
| 本地访问 | A2A API 仅限 `localhost:8080` 访问，不对外暴露 |
| Agent 白名单 | 只允许 `AGENT_REGISTRY` 中已注册的 Agent |
| 最大调用深度 | `max_a2a_depth = 5`，防止无限循环 |
| 日志记录 | 每次 A2A 跳转记录完整链路 |

## 场景验证

### 场景一：Feature 审批后自动开发

```
用户创建 Issue（feature 类型）
  → issue_opened → issue agent
    → Agent 打标签、指派、回复等待批准
    → 用户回复"同意开发"
    → issue_comment → issue agent（再次触发）
    → Agent 检测到批准意图
    → 查注册表 → 选择 implementation agent
    → curl POST /api/a2a {"agent": "implementation", "context": "..."}
  → process_a2a_event → 切换 implementation agent
    → Agent 收到前序 context + "创建分支、编码、提交 PR"
```

### 场景二：Bug 自动修复

```
用户创建 Issue（bug 类型）
  → issue_opened → issue agent（auto_impl_enabled=ON）
    → Agent 分类、加 PATCH 标签
    → 查注册表 → 选择 implementation agent
    → curl POST /api/a2a {"agent": "implementation", "context": "..."}
  → 切换 implementation agent
    → Agent 修复代码、提交 PR
```

## 实施阶段

### 阶段 1：核心协议

| 文件 | 改动 |
|------|------|
| `src/config.py` | 新增 `AGENT_REGISTRY` 常量 |
| `src/webhook_handler.py` | 新增 `POST /api/a2a` 路由 |
| `src/event_processor.py` | 新增 `process_a2a_event()`，`_process_event_inner` 支持 `_override_agent` |
| `src/prompt_builder.py` | 注入 `AGENT_REGISTRY` JSON + A2A 调用说明到 prompt；注入 `_a2a_context` |
| `src/agent/executor.py` | 追加注入 `REPO_FULL_NAME`、`ACCOUNT`、`CURRENT_EVENT_TYPE` |

### 阶段 2：Agent 适配

| 文件 | 改动 |
|------|------|
| `skills/issue/SKILL.md` | 增加在审批/分类场景下调用 A2A 的指引 |

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
