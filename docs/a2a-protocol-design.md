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

引入 **A2A（Agent-to-Agent）协议**，使 Agent 在执行完毕后，能够通过结构化指令主动调用另一个 Agent，实现多 Agent 协作流水线。

## 架构设计

### A2A 协议定义

Agent 在最终输出中附带 `[A2A]` 指令块，格式如下：

```
[A2A]
next_skill: implementation
next_event: issue_labeled
params:
  branch: feat/xxx
  issue_number: "33"
[/A2A]
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `next_skill` | 是 | 下一个要执行的技能名称（对应 skill 目录名） |
| `next_event` | 否 | 覆盖的事件类型（默认沿用当前事件类型） |
| `params` | 否 | 透传给下一个 Agent 的额外参数（JSON 格式） |

### 处理流程变化

当前（单次执行）：
```
Webhook → event_processor → agent.run() → 结束
```

新架构（循环执行直到无 A2A）：
```
Webhook → event_processor
  → loop:
    1. 按事件类型路由 skill
    2. 构建 prompt（含 A2A 协议说明）
    3. agent.run(skill_prompt)
    4. 解析 agent 输出中的 [A2A] 指令
    5. 如果有 A2A → 切换 skill/event → 回到 step 1
    6. 无 A2A → break
```

### 改动范围

| 文件 | 改动 |
|------|------|
| `src/event_processor.py` | `_process_event_inner` 改为循环结构，增加 A2A 解析逻辑 |
| `src/prompt_builder.py` | 追加 A2A 协议说明到 prompt 末尾 |
| `skills/*/SKILL.md` | 可选：在各 skill 中提示 agent 使用 A2A |

## 详细设计

### 1. A2A 指令解析器

新增函数 `parse_a2a_instruction(output: str) -> dict | None`：

```python
import re

A2A_PATTERN = re.compile(
    r'\[A2A\]\s*'
    r'next_skill:\s*(\S+)\s*'
    r'(?:next_event:\s*(\S+)\s*)?'
    r'(?:params:\s*(.*?))?'
    r'\[/A2A\]',
    re.DOTALL
)

def parse_a2a_instruction(output: str) -> dict | None:
    """解析 Agent 输出中的 A2A 指令，返回 None 表示无 A2A"""
    m = A2A_PATTERN.search(output)
    if not m:
        return None
    result = {"next_skill": m.group(1)}
    if m.group(2):
        result["next_event"] = m.group(2)
    if m.group(3):
        try:
            result["params"] = json.loads(m.group(3).strip())
        except json.JSONDecodeError:
            result["params"] = {"raw": m.group(3).strip()}
    return result
```

### 2. 事件处理器循环

修改 `_process_event_inner`：

```python
def _process_event_inner(evt, repo_key, account):
    max_a2a_depth = 5  # 防止无限循环
    depth = 0

    while depth < max_a2a_depth:
        depth += 1
        # 路由当前事件
        skills = get_domain_skill(evt['type'], repo_key)
        if not skills:
            log.info(f"No skill for event type {evt['type']}, skipping")
            return

        for skill in skills:
            # 构建 prompt（含 A2A 协议说明）
            prompt = build_prompt(evt, ..., override_skill=skill)
            # 执行 Agent
            output, exit_code = run_agent(prompt, ...)
            # 保存 session / 历史

            # 解析 A2A
            a2a = parse_a2a_instruction(output)
            if a2a:
                log.info(f"A2A: {evt['type']} → {a2a['next_skill']}")
                evt['type'] = a2a.get('next_event', evt['type'])
                # 继续外层循环，下次迭代用新 skill
                break  # break for 循环，继续 while 循环
            # 无 A2A，继续下一个 skill（如果有）
        else:
            # for 循环正常结束（无 break）→ 无 A2A
            break
```

### 3. Prompt 追加 A2A 说明

在 `build_prompt` 末尾追加：

```
## Agent-to-Agent 协议

当你完成当前任务后，如果需要调用其他技能继续处理，请在最终回复中附加以下指令块：

[A2A]
next_skill: implementation    # 下一个技能名称（必填）
next_event: issue_labeled     # 事件类型（选填，默认当前类型）
params: {"key": "value"}      # 透传参数（选填，JSON 格式）
[/A2A]

例如，issue 审核通过后需要开发：
[A2A]
next_skill: implementation
[/A2A]

当前可调用的技能列表：
- implementation：代码实现与提交
- pr：Pull Request 审核
- cloud-deploy：云部署
- issue：Issue 处理
```

> **注意**：`[A2A]` 指令块是内部协议，不会出现在任何外部消息中。详见下节。

### 4. A2A 指令剥离机制

当前 Agent 发布业务消息到 Issue / PR 是**直接调用 GitCode API**（通过 prompt 中的 curl 指令），不经 DevOps Bot 转发。因此剥离流程如下：

#### 实际调用链路

```
Agent Final Response 包含 [A2A]
  ├→ curl 发评论到 GitCode（Agent 自行排除 A2A）
  └→ DevOps Bot 读取 Final Response，解析 [A2A] → 切换 skill
```

#### Agent 层剥离（关键）

Skill 指令需明确告知 Agent 区分两种内容：

```
## Agent-to-Agent 协议

当你完成任务后，请在最终回复中附加以下指令块：

[A2A]
next_skill: implementation
[/A2A]

⚡ 重要规则：
- [A2A] 块必须放在 Final Response 中，DevOps Bot 会读取
- 调用 curl 发评论时，必须从 body 中移除 [A2A] 块
- 即：发到 GitCode 的评论不含 [A2A]，只含业务消息
```

#### DevOps Bot 层解析

DevOps Bot 从 Agent 的 Final Response 中解析 `[A2A]` 指令，不负责剥离或转发消息：

```python
output = agent.get_final_response()
a2a = parse_a2a_instruction(output)
if a2a:
    evt['type'] = a2a.get('next_event', evt['type'])
    continue  # 切换 skill 重新执行
```

#### 补充说明

- **Agent 自行负责剥离**：发 curl 时从 body 中去掉 `[A2A]`，这是 prompt 指令约束的事
- **业务消息和 A2A 相互独立**：`[A2A]` 块仅出现在 Final Response 末尾，不混入业务文本
- **纯 A2A 不发布**：如果 Agent 最终回复只有 `[A2A]` 没有业务文本，则不产生评论

### 5. 安全限制

- **最大循环深度**：`max_a2a_depth = 5`，防止 Agent 无限循环
- **A2A 仅限于 post-execution 解析**：Agent 执行期间产生的中间步骤不解析 A2A
- **参数校验**：`next_skill` 必须在已注册的技能列表中
- **深度日志**：每次 A2A 跳转记录 `event_type → next_skill` 链路

## 场景验证

### 场景一：Feature 审批后自动开发

```
用户创建 Issue（feature 类型）
  → issue_opened → issue skill
    → Agent 打标签、指派、回复等待批准
    → 输出 [A2A]next_skill: implementation[/A2A]
  → event_processor 捕获 A2A
  → 切换 implementation skill
    → Agent 创建分支、编码、提交 PR
```

### 场景二：Bug 自动修复

```
用户创建 Issue（bug 类型）
  → issue_opened → issue skill（auto_impl_enabled=ON）
    → Agent 分类、加 PATCH 标签
    → 输出 [A2A]next_skill: implementation[/A2A]
  → 切换 implementation skill
    → Agent 分析代码、修复、提交 PR
```

### 场景三：PR 审核后自动部署

```
PR merged
  → pull_request_merged → cloud-deploy skill
    → Agent 部署到指定环境
    → 输出 [A2A]next_skill: pr[/A2A]
  → 切换 pr skill
    → Agent 更新 PR 状态、添加评论
```

## 实施阶段

### 阶段 1：核心协议

| 文件 | 改动 |
|------|------|
| `src/event_processor.py` | `_process_event_inner` 改为循环结构 + `parse_a2a_instruction()` |
| `src/prompt_builder.py` | 追加 A2A 协议说明到 prompt |
| `configs/event-routing.json` | 无需改动（事件类型不变） |

### 阶段 2：Skill 适配

| 文件 | 改动 |
|------|------|
| `skills/issue/SKILL.md` | 增加在批准场景下使用 A2A 的指引 |
| `skills/pr/SKILL.md` | 可选：增加 A2A 指引 |

### 阶段 3：监控与安全

| 改动 | 说明 |
|------|------|
| A2A 调用链日志 | 记录 `issue → implementation` 完整链路 |
| 最大深度限制 | `max_a2a_depth = 5` 硬限制 |
| 异常终止 | A2A 解析失败或 skill 不存在时记录错误并终止循环 |

## 不变的部分

| 模块 | 说明 |
|------|------|
| `agent/*.py` | 后端实现（opencode / CodeArts）不变 |
| `session_manager.py` | Session/历史管理不变 |
| `user/db.py` | 用户管理不变 |
| `event-routing.json` | 路由规则不变，A2A 在路由之后生效 |
| `webhook_handler.py` | Webhook 接收不变 |
| `im/*.py` | 飞书处理不变 |
