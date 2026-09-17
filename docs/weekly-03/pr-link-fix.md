# Issue 评论中 PR 链接 404 问题分析与修复方案

## 一、问题现象

Coding Agent 完成 Issue #185 开发后，在 GitCode Issue 评论中发布了一条通知：

> [Coding Agent] ✅ 已完成实现，请审查 PR #225。

用户点击 PR 链接后出现 404，实际跳转到了 `https://gitcode.com/grape-dev/grape/pulls/225`（pulls 复数），而 GitCode 正确的 PR 页面地址是 `https://gitcode.com/grape-dev/grape/pull/225`（pull 单数）。

## 二、根因分析

### 2.1 SKILL.md 模板是正确的

`skills/coding/ai-coding-review/implementation/references/new-dev-flow.md` 第 143 行的评论模板：

```json
{"body": "[Coding Agent] ✅ 已完成实现，请审查 PR [#{pr_number}](https://gitcode.com/{owner}/{repo}/pull/{pr_number})。"}
```

模板用的是 `/pull/`（单数），是正确的。同时模板用了 markdown 链接格式 `[#{pr_number}](url)`，在 GitCode 渲染后是一个可点击的超链接。

### 2.2 Agent 实际执行偏离了模板

Agent 在实际执行时没有严格按照 SKILL.md 的评论模板发布评论。实际发布的评论是纯文本 `PR #225`，没有使用 markdown 链接格式。

GitCode 的 markdown 渲染器会将 `#225` 自动识别为 Issue 引用并生成链接 `https://gitcode.com/grape-dev/grape/issues/225`（指向 Issue 而非 PR）。但用户实际看到的链接是 `pulls/225`（复数），说明 Agent 可能自己拼了一个 `pulls/225` 的链接但用错了路径。

### 2.3 API 路径与页面路径混淆

GitCode 的 API 路径使用 `/pulls/`（复数）：
- 创建 PR：`POST /api/v5/repos/{owner}/{repo}/pulls`
- 获取 PR：`GET /api/v5/repos/{owner}/{repo}/pulls/{number}`

但 GitCode 的 Web 页面路径使用 `/pull/`（单数）：
- PR 页面：`https://gitcode.com/{owner}/{repo}/pull/{number}`

Agent 在 SKILL.md 中看到了大量 `/pulls/` 的 API 路径，自己拼 Web 链接时用了 `/pulls/`（复数），导致 404。

### 2.4 问题影响范围

- implementation SKILL.md：模板正确，但 Agent 可能不按模板执行
- coding-cli SKILL.md：模板也正确（同样用 `/pull/` 单数）
- 其他引用 `/pulls/` 的文件：均为 API 路径，不是 Web 链接，但可能误导 Agent

## 三、修复方案

### 3.1 SKILL.md 中增加明确的 PR 链接规则提示

在 `new-dev-flow.md` 的"发 Issue 评论告知"部分，增加醒目提示，明确区分 API 路径和 Web 页面路径：

文件：`skills/coding/ai-coding-review/implementation/references/new-dev-flow.md`

修改"能力八：创建 PR"之后的评论模板部分，增加路径规则提示：

```markdown
### ⚠️ 必须执行：发 Issue 评论告知

> **PR 链接路径规则（重要）：**
> - GitCode **Web 页面** PR 链接使用 `/pull/`（**单数**）：`https://gitcode.com/{owner}/{repo}/pull/{pr_number}`
> - GitCode **API** 路径使用 `/pulls/`（复数）：`https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls`
> - **Issue 评论中必须使用 Web 页面链接（`/pull/` 单数），不得使用 API 路径（`/pulls/` 复数）**
> - **必须使用 markdown 链接格式** `[PR #{pr_number}](https://gitcode.com/{owner}/{repo}/pull/{pr_number})`，不得使用纯文本 `PR #{pr_number}`

```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $BOT_G...OKEN
Content-Type: application/json
{"body": "[Coding Agent] ✅ 已完成实现，请审查 PR [#{pr_number}](https://gitcode.com/{owner}/{repo}/pull/{pr_number})。"}
```
```

### 3.2 coding-cli SKILL.md 同步修改

文件：`skills/coding/ai-coding-review/coding-cli/references/new-dev-flow.md`

同步增加相同的 PR 链接路径规则提示。

### 3.3 JSON 输出模板中增加 PR 链接字段

在 SKILL.md 的最终 JSON 输出模板中，增加 `pr_url` 字段，明确要求 Agent 输出正确的 PR Web 链接：

```json
{
  "actions": ["git push 分支 feat/issue-N", "创建 PR #N", "在 Issue #N 发评论告知", "A2A 通知 QA 审查 PR #N"],
  "artifacts": ["PR #N", "分支 feat/issue-N", "commit xxxxxx"],
  "pr_url": "https://gitcode.com/{owner}/{repo}/pull/{pr_number}",
  ...
}
```

### 3.4 QA SKILL.md 中同步检查

在 QA 审查流程的 SKILL.md 中，增加对评论中 PR 链接正确性的检查项：

- 确认评论中的 PR 链接使用 `/pull/`（单数）
- 确认链接使用 markdown 格式而非纯文本

---

## 四、修改清单

| 步骤 | 文件 | 修改内容 |
|------|------|---------|
| 1 | skills/coding/ai-coding-review/implementation/references/new-dev-flow.md | 评论模板前增加 PR 链接路径规则提示 |
| 2 | skills/coding/ai-coding-review/coding-cli/references/new-dev-flow.md | 同步增加提示 |
| 3 | 上述两个文件 | JSON 输出模板增加 pr_url 字段 |
| 4 | skills/qa/（QA 相关 SKILL.md） | 增加 PR 链接正确性检查项 |

---

## 五、验证

1. 触发一个新的 coding 任务
2. 确认 Agent 发布的 Issue 评论中 PR 链接使用 `/pull/`（单数）
3. 确认链接使用 markdown 格式（可点击的超链接）
4. 点击链接确认跳转到正确的 PR 页面
5. 确认不再出现 `/pulls/`（复数）的 404 链接
