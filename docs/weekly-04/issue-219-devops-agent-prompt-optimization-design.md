# Issue #219 设计方案：优化 DevOps Agent 的 prompt，移除直接 API 调用指令

> 状态：设计方案（v1，基于 Issue #219 + Spec v2）✅ 已实施（2026-09-22，prompt 侧全部完成；技能 SKILL.md 为可选跟进）
> 分支：weekly-04-lyz
> 关联：Issue #219（spec-ready / triaged / P1）、#213（grape-cli 能力已落地）

---

## 1. 背景与目标

### 1.1 问题现状

`src/prompt_builder.py` 为所有 DevOps Agent 构建 prompt 时注入了一段硬编码的「直接 API 调用指令」（多为 curl 模板），集中在 5 个区域顶多 8 处实际 prompt 注入点（Spec 摘要计数为 9 处，含 1 处 docstring 注释）：

| 位置 | 代码行 | 指令内容 | 说明 |
|------|--------|---------|------|
| `_build_feishu_context_section` | L309 | 飞书发通知卡片 curl | 飞书 API，非 Grape API |
| `_inject_attachments_by_role`（vod） | L547 | `curl /api/analyze-image?url=<图片URL>` | Grape API 直调 |
| `_inject_attachments_by_role`（vod） | L549 | `curl` 下载文本/代码/文档 → read tool | Grape API 直调 |
| `_inject_attachments_by_role`（coding） | L570 | `curl` 下载用户附件 → read tool | Grape API 直调 |
| `build_devops_agent_prompt` A2A 协议 | L751 | `curl -X POST {{SERVER_URL}}/api/a2a` | Grape API 直调 |
| `build_devops_agent_prompt` VCS API Access | L828 | `curl ... ${_bot_token_env}` | GitCode/GitHub API 直调 |
| `build_general_chat_prompt` VCS API Access | L958 | `curl ... ${_token_env}` | GitCode/GitHub API 直调 |
| `build_task_chat_prompt` VCS API Access | L1057 | `curl ... ***` | GitCode/GitHub API 直调 |

（注：Spec v2 表格将 5 个区域计为「9 处 curl 模板」，其中含 L499 注释行 `调 analyze-image / curl 下载`，非 prompt 输出，实际 prompt 注入点为 8 处。）

### 1.2 核心问题

1. **prompt 臃肿**：硬编码 curl 模板 + 大量上下文全文注入（项目知识基线 2500 字符内联、Issue Pipeline 等），token 浪费、误导 agent。
2. **交互方式不由业务决定**：curl 模板假定「直调 API」，而 grape-cli（#213）已覆盖 42 个 API 端点，但部分技能仍采用 grape API 方式。prompt 层不该统一禁止或统一引导。
3. **agent 被动接收**：上下文靠 prompt 注入，而非技能内按需主动获取（Issue 详情、项目知识、Pipeline 状态等）。

### 1.3 目标（Spec v2 口径，最终版）

> 经 @Jason7602 评论反馈，v2 将目标收敛为：**移除直接 API 调用指令，prompt 保持中性**，不引导 API 也不引导 CLI，交互方式选择权完全下放到各技能 SKILL.md。

- **移除外**：prompt 中的直接 API curl 模板
- **保持中性**：不新增任何 API/CLI 使用引导（v2 明确排除「在 prompt 中添加 grape-cli 引导」）
- **最小化注入**：保留认证 token 环境变量名与仓库标识，内联大段上下文（项目知识基线、A2A 协议）降为摘要 + 路径引用
- **行为不退化**：Issue 评论、A2A 交办、项目知识获取等现有功能不因 prompt 精简而失效

---

## 2. 方案概述

在 `prompt_builder.py` 中**纯移除**所有 Grape 侧 curl 模板，不替换为任何替代引导；仅保留：

- 认证信息（token 环境变量名）
- 仓库标识（仓库名 / Issue 编号）
- 必要安全约束（禁泄 token 等）
- 飞书通知 curl（`_build_feishu_context_section` L309）——飞书 API 不在 grape 契约内，**保留**

各技能 SKILL.md 由各自维护者决策：已有 grape API 方式的技能保持不变；需使用 grape-cli 的技能在自身 SKILL.md 中补 CLI 引导。本方案**不统一要求/不批量改写**技能文件（列为可选跟进）。

---

## 3. 改动点

### 改动点 1：移除 A2A 协议章节 curl 模板（`build_devops_agent_prompt`，L751）

**现状**：A2A 章节含完整 curl 示例（`{{SERVER_URL}}/api/a2a` + payload JSON），下方是服务端强校验的字段规则。

**改法**：
- 删除 curl 模板块（`.replace(...)` 替换占位符的整段 curl 示例）。
- 保留并精简字段规则说明（`issue_number` 必填、`pr_number` 有则必填、严禁字段混用、`repository`/`account` 必填、`context` 构造规则）——这段是服务端强制校验的契约，不能丢。
- 认证说明段（`$GRAPE_SESSION_TOKEN` 用于 `/api/a2a`、`$__BOT_TOKEN__` 用于 VCS API）**保留**，仅去掉调用示例。前提：侧写在「不要新增 CLI 引导」范围内，认证说明属于最小必要信息。

### 改动点 2：移除 3 处 VCS API Access 章节 curl 模板

`build_devops_agent_prompt`（L828）、`build_general_chat_prompt`（L958）、`build_task_chat_prompt`（L1057）三个函数里的 VCS API Access 段：

**现状**：
```
## {_vcs} API Access
Your token is available as the {_token_env} environment variable.
API base URL: {_api_base}
  curl -s -H "Authorization: Bearer ${_token_env}" \
    {_api_base}/repos/$REPO_FULL_NAME/issues
Never print or reveal the token value.
```

**改法**：删除 curl 行与其续行，保留：
```
## {_vcs} API Access
Your token is available as the {_token_env} environment variable.
API base URL: {_api_base}
Never print or reveal the token value.
```

即只告知「token 环境变量名 + API base URL + 安全约束」，具体 HTTP 调用方式交给技能的 SKILL.md。

### 改动点 3：精简附件注入 curl 指令（`_inject_attachments_by_role`，L547/L549/L570）

**现状**：
- vod：`curl {_server_url}/api/analyze-image?url=<图片URL>`、`curl 下载文本/代码/文档 → read tool`
- coding：`curl 下载用户附件 → read tool`

**改法**：
- 保留附件清单（文件名 + URL）。
- 将「curl 下载 / curl analyze-image」的**具体命令**改为中性指引：只保留「附件 URL 已列出，需读取附件内容请用你所在技能 / 环境提供的方式下载后 read tool 读取」。
- 飞行安全：`analyze-image` 是 Grape 服务端点，若 vod 技能当前依赖它，需在 vod 技能 SKILL.md 中补该端点调用方式（列为可选技能跟进项，本改动不删能力仅去 curl 命令）。

### 改动点 4：最小化上下文全文注入（`_build_project_info_section`）

**现状**（L94-115）：当有项目知识基线时，将 `project-graph.md` + `project-capabilities.md` **全文内联**（各自截断至 `_PROJECT_INFO_MAX_CHARS = 2500` 字符），再附 `{{PROJECT_INFO_DIR}}/` 路径引用。两个文件最多可注入 5000 字符。

**改法**：
- 内联降为**摘要**：仅取每文件开头 `_PROJECT_INFO_MAX_CHARS` 下调至 ≤500 字符（可新增常量 `_PROJECT_INFO_SUMMARY_CHARS = 400`）。
- 保留行 L115 `> 全文副本已落地：{{PROJECT_INFO_DIR}}/project-graph.md 与 {{PROJECT_INFO_DIR}}/project-capabilities.md，可用 read tool 读取全文。`——这是路径引用，是「按需主动获取」的关键，必须保留。
- 保留 commit/updated 元信息行（L106-107）与 degraded 提示（L102-103）。
- 无基线降级分支（L96）保留。

### 改动点 5：确认环境变量注入完备（无改动）

Spec v2 确认无需改动，实测佐证：

| 环境变量 | 注入位置 | 用途 |
|---------|---------|------|
| `REPO_FULL_NAME` / `ACCOUNT` | event_pipeline.py:1664-1665 | 仓库标识 |
| `GRAPE_SERVER_URL` | event_pipeline.py:1666、runtime/agent.py:1027 | 服务端可达地址（grape-cli / API 共用） |
| `ISSUE_NUMBER` / `PR_NUMBER` | event_pipeline.py:1706-1709、2214-2217 | Issue/PR 标识 |
| `GRAPE_SESSION_TOKEN` | event_pipeline.py:2236 | Grape API 认证（仅 A2A 事件路径） |
| `BOT_GITCODE_TOKEN` | executor.py `_assemble_env` L256 | VCS API 认证（devops 事件任务） |
| `USER_GITCODE_TOKEN` | executor.py L232 | VCS API 认证（chat） |

全部已存在于注入点，`src/agent/executor.py` **无改动**。

### 改动点 6：技能 SKILL.md（可选，各技能自主决策，不做统一批量改）

按 Spec v2，「API vs CLI 选择权下放技能」。本方案建议对以下技能做**可选跟进**（不阻塞 prompt 改动）：
- `skills/vod/*`：若 prompt 移除 analyze-image curl 后依赖该能力，在 SKILL.md 补服务中心端点调用方式或迁移 grape-cli。
- 其他技能：已有 grape API 方式保持不变；如需 grape-cli 由技能维护者在各自 SKILL.md 中补充，不在 prompt 层添加。

### 改动点 7：更新测试（`tests/test_prompt_builder.py`）

新增断言，覆盖 Spec v2 验收项：

1. `build_devops_agent_prompt` 输出不含 `curl`（飞书通知段除外——im-notify 技能场景仍可含 curl）。
2. `build_general_chat_prompt` 输出不含 `curl`。
3. `build_task_chat_prompt` 输出不含 `curl`。
4. 输出不含 CLI 引导（`grape ` / `grape-cli` / `a2a handoff` 等，负向断言）。
5. 输出仍含 token 环境变量名（`_token_env` / show as e.g. `BOT_GITCODE_TOKEN`）。
6. 输出仍含仓库标识（`REPO_FULL_NAME`）。
7. `_build_project_info_section` 有基线时内联摘要长度 ≤500 且含 `{{PROJECT_INFO_DIR}}/` 路径引用。

---

## 4. 调用链路影响

```
prompt 构建                             执行
event_pipeline.run_pipeline()  ──►  build_devops_agent_prompt(evt, ...)  
                                     ├─ 移除 A2A curl（改动1）
                                     ├─ 移除 VCS curl（改动2）
                                     ├─ 附件下载中性化（改动3）
                                     ├─ 项目知识摘文化（改动4）
                                     └─ env 注入不变（改动5） → executor._assemble_env
```

- 纯 prompt 文本改动，不改执行路径、不改事件调度、不改 API 契约。
- Agent 拿到的 prompt 更短、更中性；具体「如何调」由 SKILL.md 决定。

---

## 5. 验收标准

| # | 验收项 | 对应改动 |
|---|--------|---------|
| 1 | `build_devops_agent_prompt` 输出不含 curl 模板（飞书通知段除外） | 1/2/3 |
| 2 | `build_general_chat_prompt` 输出不含 curl 模板 | 2 |
| 3 | `build_task_chat_prompt` 输出不含 curl 模板 | 2 |
| 4 | prompt 不含任何 API/CLI 使用引导 | 1/2/3 |
| 5 | prompt 仍包含认证 token 环境变量名 | 2 |
| 6 | prompt 仍包含仓库标识环境变量名 | 2（保留不动） |
| 7 | 项目知识基线内联 ≤500 字符摘要 + 路径引用 | 4 |
| 8 | 所有现有测试通过 + 新增 curl 移除断言 | 7 |
| 9 | Agent 行为不退化（Issue 评论、A2A 交办、项目知识获取） | 全量，需回归 | 

---

## 6. 风险与缓解

| 风险 | 等级 | 缓解 |
|------|------|------|
| agent 不知道如何调 API/CLI | Med | 技能 SKILL.md 提供引导（可选跟进） |
| 项目知识基线精简导致冷启动信息不足 | Med | 保留摘要（≤500）+ 路径引用，read tool 可读全文 |
| vod/coding 附件 analyze-image 能力依赖 prompt curl | Med | 技能侧补端点调用引导；能力不删，仅去命令 |
| 部分技能未及时更新 SKILL.md 行为退化 | Low | 技能更新可渐进；prompt 保留 token/base URL 兜底信息 |

---

## 7. 回退方案

- 本改动为纯 prompt 文本变更，回退 = revert 对应 commit，无数据迁移、无 DB 变更。
- 若回归发现某技能行为退化，优先在该技能 SKILL.md 补引导（渐进），无需回退 prompt。

---

## 8. 待办/未决

- [ ] 确认 `_PROJECT_INFO_MAX_CHARS` 拆分为摘要与全文两个常量的命名（建议 `_PROJECT_INFO_SUMMARY_CHARS = 400`）。
- [ ] vod / coding 技能维护者确认附件能力引导（改动点 6 可选项）。
- [ ] 与 #213 grape-cli 的实际命令清单对齐（如技能选用 CLI，需在 SKILL.md 中引用正确子命令）。

---

## 9. 后续优化：收尾二选一（方案 C，附：已实施 2026-09-22）

### 9.1 背景

- 采用 grape-cli 的技能以 `grape tasks complete --json` 结构化自报完成（`actions`/`conclusion`/`artifacts`/`next_step`/`issue_summary`
  直传服务端，`POST /api/tasks/complete`，`src/api/routes/tasks.py:293`），内容已随 complete 上报，最终答复**不再需要 JSON 块**。
- 但 `_build_output_contract` 原为无条件「输出 JSON 块」，与技能收尾流程冲突 → agent 两路都做，内容重复、污染 event 档案。
- 服务端已兼容「无 JSON 块但已 tasks complete」：`event_pipeline.py:2365` 任务 self-reported completed → 跳过 no-output 判失败（Issue #45）。

### 9.2 方案 C：prompt 条件化二选一（已实施）

`_build_output_contract`「处理记录输出要求」改为按技能收尾流程分流：

- **情形一**：技能提供 `grape tasks complete` 结构化收尾 → 调完即结束任务，**不再输出 JSON 块**。
- **情形二**：技能未提供 → 按原契约输出 JSON 块（保留五字段，作为非 CLI 技能 `evaluate_agent_output` 兜底判定材料）。
- 硬约束：两路**二选一**，不得同时输出 JSON 块并调 tasks complete。
- 「任务结束」段同步对齐二选一；思考/评论边界/发布前自检保持不动。

### 9.3 改动

| 文件 | 改动 |
|------|------|
| `src/prompt_builder.py` | `_build_output_contract` 条件化（情形一/情形二 + 二选一硬约束）；任务结束段与最终答复产出说明对齐 |
| `tests/test_prompt_builder.py` | 新增 `TestConditionalClosingFlow` 4 项断言（两分支存在、二选一硬约束、任务结束段对齐）；`test_devops_prompt_no_cli_guidance` 改为 `test_devops_prompt_no_hardcoded_api_usage`（不再断言不含 `grape`，改为断言不含 curl/a2a handoff 命令体） |

### 9.4 验收

- `_build_output_contract` 输出含「以你当前技能（SKILL.md）的收尾流程为准」「不要再重复输出下方 JSON 块」「二选一」
- 非 CLI 技能 JSON 块契约仍完整（五字段 + 纯 JSON 结构约束）
- 服务端无需改动（self-reported completed 跳过误判路径已存在）