# Grape Skills API 接口梳理

> 梳理范围：issue-advance、vod、qa、pr、issue、implementation、build-debug、deploy-guard 八个核心技能  
> 梳理日期：2026-08-19  
> 数据来源：各技能的 SKILL.md 及 references/ 目录

---

## 一、环境约定

### 全局变量

| 变量 | 含义 | 注入方式 |
|------|------|---------|
| `$BOT_GITCODE_TOKEN` | 所有 API 认证 Token | Prompt 注入 |
| `$USER_GITCODE_TOKEN` | 用户级 Token（备用） | Prompt 注入 |
| `$AGENT_NAME` | Agent 显示名 | Prompt 注入 |
| `$REPO_FULL_NAME` | 仓库全名 `owner/repo` | Prompt 注入 |
| `$ACCOUNT` | 账户名 | Prompt 注入 |
| `$ISSUE_NUMBER` | Issue 编号 | Prompt / 事件 payload |
| `$PR_NUMBER` | PR 编号 | Prompt / 事件 payload |
| `$HEAD_SHA` | PR 最新 head commit SHA | Prompt 注入 |

### Base URL

| 类型 | URL |
|------|-----|
| Grape 内部 API | `{{SERVER_URL}}`（运行时替换） |
| GitCode API | `https://api.gitcode.com/api/v5` |

### 认证方式

```
Authorization: Bearer $BOT_GITCODE_TOKEN
或 PRIVATE-TOKEN: {token}
```

### 安全硬约束（所有技能通用）

- 仅使用 Prompt 注入的 Token，禁止从 DB / 配置 / 日志查找其他凭证
- 禁止读取 `data/`、`configs/`、`sessions.db` 等 Grape 内部数据
- API 403 / 无权限 → 立即停止，告知用户，不绕行
- Token 不得写入任何文件 / 日志 / 评论

---

## 二、Grape 内部 API（A2A + 任务管理）

### 2.1 POST /api/a2a — Agent-to-Agent 协议

**描述**：技能间跨 Agent 委托的唯一边界。上游技能完成自身阶段后，通过 A2A 将工作交办给下游技能。

| 调用方 | 目标 Agent | 触发场景 |
|--------|-----------|---------|
| **issue-advance** | `coding`、`vod`、`sre` | 需求评级为 ready → 交办 coding 开发；需澄清 → 交办 vod；构建/部署 → 交办 sre |
| **vod** | `issue` | 需求收敛完成 → 交办 issue-advance 进行 Spec 设计/打标签/排里程碑 |
| **qa** | `coding` | 审查发现 Critical/Required 问题 → 通知 implementation 修改代码 |
| **pr** | `coding` | 审查发现问题 → 通知 implementation 修改 |
| **issue** | `coding`、`implementation`、`pr`、`build-debug`、`cloud-deploy` | 用户要求开发 → 交办对应 Agent |
| **implementation** | `qa` | 新开发完成 → 通知 QA 审查 |

**请求体结构**：
```json
{
  "agent": "<目标Agent类型>",
  "payload": {
    "repository": "<owner/repo>",
    "account": "<账户>",
    "issue_number": "<Issue编号>",
    "pr_number": "<PR编号 或 空>",
    "context": "<上下文描述>"
  }
}
```

**关键约束**：
- `issue_number` 必填（服务端强制校验），严禁将 PR 编号填入此字段
- `pr_number` 有则填，无则不填
- VOD 调用失败时重试最多 3 次，仍失败则在 Issue 评论标注需人工介入

---

### 2.2 POST /api/tasks/notify — 任务通知

**描述**：处理结束后创建/重派/关闭任务通知，将 Issue 与人或机器人关联。

| 调用方 | assignee 类型 | 行为 |
|--------|-------------|------|
| **issue-advance** | 人 | 创建/重派任务给责任人 |
| **issue-advance** | 机器人（grape_bot / gitcode_bot） | 关闭该 Issue 旧任务（bot_takeover） |
| **issue** | 人（仅人，非机器人） | 创建/重派任务 |

**请求体**：
```json
{
  "assignee": "<GitCode用户名>",
  "repo_key": "<owner/repo>",
  "issue_number": "<Issue编号>",
  "task_description": "Issue #<N> 已分配给你，请处理：<简要描述>"
}
```

**响应**：`{ok: true}` 或 `{ok: false}`（责任人未注册平台时）

---

### 2.3 POST /api/tasks/close — 关闭任务

**描述**：Issue 关闭后将关联的待处理任务标记为已完成。

| 调用方 | 触发场景 |
|--------|---------|
| **issue-advance** | Issue 关闭后刷新 |
| **issue** | Issue 关闭后刷新 |

**请求体**：
```json
{
  "repo_key": "<owner/repo>",
  "issue_number": "<Issue编号>"
}
```

---

### 2.4 Grape 内部 API 总览

| 端点 | 方法 | issue-advance | vod | qa | pr | issue | impl | build-debug | deploy-guard |
|------|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| `/api/a2a` | POST | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | - | - |
| `/api/tasks/notify` | POST | ✅ | - | - | - | ✅ | - | - | - |
| `/api/tasks/close` | POST | ✅ | - | - | - | ✅ | - | - | - |

---

## 三、GitCode API 使用全景

### 3.1 Issue 相关

| 端点 | 方法 | 用途 | issue-advance | vod | qa | pr | issue | impl |
|------|------|------|:---:|:---:|:---:|:---:|:---:|:---:|
| `/repos/{o}/{r}/issues` | GET | 列出 Issue（去重/历史画像） | ✅ | ✅ | - | - | ✅ | - |
| `/repos/{o}/{r}/issues` | POST | 创建 Issue（部分技能已弃用） | ✅ | ✅ | - | - | ✅ | - |
| `/repos/{o}/{r}/issues/{n}` | GET | 获取 Issue 完整内容 | ✅ | ✅ | - | - | ✅ | ✅ |
| `/repos/{o}/{r}/issues/{n}` | PATCH | 更新 Issue（标签/分配/关闭/里程碑/结构化） | ✅ | ✅ | ✅ | ✅ | ✅ | - |
| `/repos/{o}/{r}/issues/{n}/comments` | GET | 列出 Issue 评论 | ✅ | ✅ | - | - | ✅ | - |
| `/repos/{o}/{r}/issues/{n}/comments` | POST | 创建 Issue 评论 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `/repos/{o}/{r}/issues/{n}/labels` | GET | 获取 Issue 标签 | ✅ | - | - | - | - | - |
| `/repos/{o}/{r}/issues/{n}/labels` | DELETE | 清除所有标签 | ✅ | - | - | - | - | - |

### 3.2 PR 相关

| 端点 | 方法 | 用途 | vod | qa | pr | impl |
|------|------|------|:---:|:---:|:---:|:---:|
| `/repos/{o}/{r}/pulls` | GET | 列出 PR | ✅ | - | - | - |
| `/repos/{o}/{r}/pulls` | POST | 创建 PR | - | - | - | ✅ |
| `/repos/{o}/{r}/pulls/{n}` | GET | 获取 PR 详情 | - | ✅ | ✅ | ✅ |
| `/repos/{o}/{r}/pulls/{n}` | PATCH | 更新 PR（body/labels） | - | - | - | ✅ |
| `/repos/{o}/{r}/pulls/{n}/comments` | GET | 获取行内评论 | - | ✅ | ✅ | ✅ |
| `/repos/{o}/{r}/pulls/{n}/comments` | POST | 创建行内评论 / 总结 | - | ✅ | ✅ | ✅ |
| `/repos/{o}/{r}/pulls/{n}/comments/{id}` | PUT | 标记评论已解决 | - | ✅ | ✅ | - |
| `/repos/{o}/{r}/pulls/{n}/files` | GET | 获取变更文件 diff | - | ✅ | ✅ | - |

### 3.3 仓库/内容/元信息

| 端点 | 方法 | 用途 | issue-advance | vod | qa | issue | impl | build-debug | deploy-guard |
|------|------|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| `/repos/{o}/{r}` | GET | 仓库信息 | ✅ | ✅ | - | ✅ | ✅ | ✅ | ✅ |
| `/repos/{o}/{r}/readme` | GET | README | ✅ | ✅ | - | ✅ | - | - | - |
| `/repos/{o}/{r}/contents/{p}` | GET | 文件内容（CODEOWNERS/AGENTS.md/CI配置等） | ✅ | - | ✅ | ✅ | ✅ | ✅ | ✅ |
| `/repos/{o}/{r}/contents/{p}` | POST/PUT | 创建/更新文件（降级方案） | - | - | - | - | ✅ | - | - |
| `/repos/{o}/{r}/branches` | GET | 分支列表 | ✅ | ✅ | - | - | ✅ | - | - |
| `/repos/{o}/{r}/commits` | GET | 提交历史 | ✅ | ✅ | ✅ | ✅ | - | ✅ | ✅ |
| `/repos/{o}/{r}/labels` | GET | 标签列表 | ✅ | - | - | ✅ | - | - | - |
| `/repos/{o}/{r}/labels` | POST | 创建标签 | ✅ | - | - | ✅ | - | - | - |
| `/repos/{o}/{r}/milestones` | GET | 里程碑列表 | ✅ | - | - | ✅ | - | - | - |

---

## 四、技能详情

### 4.1 issue-advance — Issue 高级运营 v3.1

**触发事件**：`issue_opened`（VOD 交办后）、`issue_updated`、`issue_comment`、`issue_labeled`

**工作流**：项目知识基线 → 深度可开发性分析 → 分类/标签/分配 → Spec 生成 → A2A 触发开发

**API 调用量**：Grape 3 个 + GitCode ~17 个（项目中 API 调用最密集的技能）

**关键场景**：
1. 需求评级 `ready` → `POST /api/a2a agent=coding` 启动开发
2. 需求不清晰 → `POST /api/a2a agent=vod` 需求澄清
3. 责任人确定 → `POST /api/tasks/notify` 分配任务
4. Issue 关闭 → `POST /api/tasks/close` 清理任务

---

### 4.2 vod — 开发者声音分析 VOD v2.0

**触发事件**：`vod_signal`、飞书消息、`issue_opened`

**双职责模型**：
- **场景 A**：从飞书/Issue 收集开发者声音 → 多轮引导 → 收敛为结构化需求 → 建 Issue → A2A
- **场景 B**：接管 `issue_opened` → 审视非 VOD 创建的 Issue → 澄清 → 结构化总结 → A2A 交办 issue-advance

**API 调用量**：Grape 1 个 + GitCode ~12 个

**关键场景**：需求收敛完成 → `POST /api/a2a agent=issue` 交办 issue-advance

---

### 4.3 qa — PR 全生命周期审查 + 工作流质量审查

**触发事件**：`pull_request_opened`、`pull_request_updated`、`push`、`note_hook_pr`、`a2a_event`

**双维度审查**：
1. **代码审查**：diff 分析、行内评论、修复验证、PR 配置检查
2. **工作流审查**（Agent Work Loop 五维模型）：对仓库 AI 编码工作流的证据化审查

**API 调用量**：Grape 1 个 + GitCode ~10 个 + 本地文件读取

**关键场景**：审查发现 Critical/Required → `POST /api/a2a agent=coding` 通知 implementation 修改

---

### 4.4 pr — PR 生命周期管理（简化版）

**触发事件**：同 `qa`（pull_request_opened/updated、push、note_hook_pr）

**与 qa 的差异**：无工作流审查维度，仅做代码审查

**API 调用量**：Grape 1 个 + GitCode ~9 个

**关键场景**：审查发现问题 → `POST /api/a2a agent=coding` 通知修改

---

### 4.5 issue — Issue 生命周期管理（基础版）

**触发事件**：`issue_opened`、`issue_comment`

**与 issue-advance 的差异**：
- issue-advance 有深度可开发性分析 + Spec 生成 + A2A 链路编排
- issue 为基础三件套：分类 → 打标签 → 分配负责人

**API 调用量**：Grape 3 个 + GitCode ~13 个

**关键场景**：用户要求开发 → `POST /api/a2a agent=coding` 交办

---

### 4.6 implementation — 代码实现与 PR 创建

**触发事件**：仅 A2A 协议调用（`a2a_event`），无独立触发

**双模式**：
1. **新开发**（被 issue 调用）：分析需求 → 创建分支 → 本地编码 → 创建 PR → A2A 通知 QA
2. **代码优化**（被 qa/pr 调用）：读取审查意见 → 修改代码 → push → 评论告知

**API 调用量**：Grape 1 个 + GitCode ~10 个 + 本地 git 操作

**关键场景**：开发完成 → `POST /api/a2a agent=qa` 触发审查

---

### 4.7 build-debug — 构建失败诊断

**触发事件**：A2A 调用（由 issue/其他技能触发）

**API 调用量**：Grape 0 个 + GitCode ~5 个（按需）

**定位方式**：CI 日志分析 + `git diff` + 依赖检查

---

### 4.8 deploy-guard — 部署前安全与合规检查

**触发事件**：部署前阶段

**检查维度**：Docker 安全检查、K8s 资源规范检查、CI 测试验证、合规审批

**API 调用量**：Grape 0 个 + GitCode ~4 个（按需）

---

## 五、A2A 调用链全景

```
vod_signal / 飞书消息
  │
  ▼
┌─────┐  A2A(issue)   ┌───────────────┐  A2A(coding)   ┌────────────────┐  A2A(qa)   ┌─────┐
│ VOD │ ──────────────►│ issue-advance │ ───────────────►│ implementation │ ──────────►│ QA  │   叫停 → A2A(coding)
└─────┘                └───────┬───────┘                └────────────────┘            └─────┘
                               │
                               │ A2A(vod) ── 需求澄清
                               │ A2A(sre) ── 构建/部署/验证
                               │
┌───────┐  A2A(coding)  ┌───────┴───────┐
│ issue │ ──────────────►│ (同上链路)     │
└───────┘                └───────────────┘

pull_request_opened
  │
  ▼
┌─────┐  A2A(coding)   ┌────────────────┐
│ QA  │ ───────────────►│ implementation │  (审查发现 Critical/Required 问题)
└─────┘                 └────────────────┘
```

---

## 六、评论前缀规范

| 技能 | 评论前缀 | 示例 |
|------|---------|------|
| issue-advance | `[issue agent]` | `[issue agent] Spec 已发布，请查看` |
| vod | `[VOD Agent]` | `[VOD Agent] 需求已收敛，结构化如下` |
| qa | `[$AGENT_NAME]` | `[QA Agent] 🔴 Critical: SQL 注入风险` |
| pr | `[QA Agent]` | `[QA Agent] 代码审查完成` |
| issue | `[Issue Agent]` | `[Issue Agent] 已分配负责人` |
| implementation | `[Coding Agent]` | `[Coding Agent] ✅ PR 已创建` |
| build-debug | 无固定前缀 | — |
| deploy-guard | 无固定前缀 | — |

---

## 七、统计

| 指标 | 数值 |
|------|------|
| Grape 内部 API 端点 | **3 个**（a2a、tasks/notify、tasks/close） |
| GitCode API 端点 | **~28 个**（去重后） |
| API 调用最密集的技能 | issue-advance（3 + ~17） |
| 不调用 Grape API 的技能 | build-debug、deploy-guard |
| A2A 链最长路径 | VOD → issue-advance → implementation → QA（3 跳） |
| 安全硬约束 | 5 条（仅用注入 Token、禁读内部数据、403 即停、Token 不落盘） |