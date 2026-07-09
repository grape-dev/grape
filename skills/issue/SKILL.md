---
name: issue
version: 2.0.0
description: |
  Issue 全生命周期管理：接收 → 分类 → 打标签 → 分配负责人 → 设置里程碑 → 关联 PR → 回复评论。
  当收到 issue_opened 或 issue_comment 事件时使用此技能。
  不适用于：PR 相关事件（用 pr skill）、非 Issue 的仓库评论。
triggers:
  - event_type: issue_opened
  - event_type: issue_comment
tools: []
---

# Issue 运营

## 核心原则

### 守门不修 bug
可以打标签、发评论、分配负责人，但**不要直接写代码修 bug**。除非是 P0 安全事故需要紧急止血。

### 验证外部 claim
PR body / commit message / 评论里的 `fixes #N`、`tests pass`、`small fix` 都是 claim，不是事实。**先打开原对象核对**，核对后才能用作决策依据。

### 读 Session 历史
你的 Prompt 中包含了同一仓库的历史事件。处理前先看最近事件，避免重复分析和矛盾决策。

### 禁止探索 Bot 内部
不要执行 `ls`、`grep`、`cat` 等命令查看 workspace 目录、Bot 代码、日志文件或数据库。
**所有信息通过 GitCode API 获取**，不读取本地文件。

## 强制流程（每一步都必须执行）

> 所有步骤完成后，**必须**用 curl 将结果 POST 到 Issue 评论，body 以 `[DevOps Bot]` 开头。这是最终强制步骤。

### 第一步：分类与打标签

读取 Issue 标题和正文 → 按以下标准判定 → **立即调用 API 设置标签**。

| 标签 | 颜色 | 适用场景 | 判定线索 |
|------|------|---------|---------|
| `feature` | `#a2eeef` | 新功能请求 | 标题含"建议/需求/新增/支持/实现/希望/能不能" |
| `bug` | `#d73a4a` | 缺陷报告 | 标题含"bug/故障/报错/错误/不生效/白屏/异常/500"或正文含堆栈 |
| `need-info` | `#d4c5f9` | 信息不足 | 正文 < 30 字、无复现步骤、无日志/截图 |
| `triaged` | `#bfdadc` | 已分诊完成 | **与主标签一起加**，表示已完成分诊 |
| `invalid` | `#e4e669` | 本仓库项目内容与项目范围完全无关 | 标题/正文与项目范围完全无关 |

**打标签流程（两步）：**

1. **确保标签存在且有颜色：** 如果标签尚未创建，用以下 API 创建（带上颜色参数）：
```bash
POST /repos/{owner}/{repo}/labels
# Body (form-encoded, 不要 Content-Type 头):
name=feature&color=a2eeef
```

2. **设置标签到 Issue：**
```bash
PATCH /repos/{owner}/{repo}/issues/{number}
Content-Type: application/json
{"labels": "feature,triaged"}
```

> 多个标签用逗号分隔。如果标签已存在则跳过创建步骤，直接 PATCH issue。

### 第二步：分配负责人

1. **读取 Issue** 标题和正文，理解该 Issue 所属的核心领域
2. **打开 CODEOWNERS 文件**，逐模块读取：
   - **触发词** → 快速预筛选：Issue 内容命中触发词的模块标记为候选
   - **描述** → 语义确认：对比 Issue 核心领域与模块描述，判断是否真正匹配
3. **选择最匹配的模块**：从候选模块中选择描述最贴近的那个
4. **如命中多个模块** → 选择描述匹配度最高的
5. **如未命中任何模块** → 走兜底负责人

6. **能确定负责人 → 立即调用 API 分配（必须先执行，再评论）：**
   ```bash
   curl -s -X PATCH -H "Authorization: Bearer $GITCODE_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"assignee": "younger_eric"}' \
     https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}
   ```
   `assignee` 的值为用户的 **login 用户名**（如 `younger_eric`），直接传入即可。

7. **检查跨仓同步：** 该负责人的模块定义中是否有 `# 仓库：` 字段？
   - **有**（如 `# 仓库：developer-skill/huaweicloud-skills`）→ 在目标仓库创建 Issue，标题和内容与原 Issue 一致，body 末尾注明 `源自 [owner/repo#N](链接)`
   - **无** → 跳过同步

8. **评论回复：** @ 该负责人，说明已分配及匹配依据。如有跨仓同步，追加 `已同步到 [目标仓库#N](链接)`

9. **不能确定 →** 评论中 @ 项目创建者（Issue 作者），说明未找到对应模块负责人

### CODEOWNERS 格式说明

| 字段 | 格式 | 说明 |
|------|------|------|
| 责任人 | `# 责任人：@用户名` | 模块负责人 |
| 模块 | `# 模块：模块名称` | 模块名称和作用域 |
| 触发词 | `# 触发词：keyword1, keyword2` | 快速预筛选关键词 |
| 描述 | `# 描述：功能说明` | 语义匹配的描述信息 |
| 仓库 | `# 仓库：owner/repo` | 跨仓同步目标（可选） |
| 兜底 | `* @用户名` | 未匹配任何模块时使用 |

### 第三步：评论回复

在 Issue 中发表评论，说明已完成的操作：

```
已标记为 [{分类}] 标签。
[如已分配] 已分配给 @username。
[如需补充] 请提供复现步骤/日志/截图等信息。
[如有讨论需要] 你可以在此讨论方案设计。
```

**回复要点：**
- ✅ 说明打了什么标签、分配给了谁
- ✅ 可以追问补充信息或讨论方案设计
- ✅ 可以给出实现思路和技术方向
- ❌ 不要写代码实现
- ❌ 不要提交 PR
- ❌ 不要合并 PR

## 评论回复（issue_comment 事件）

| 场景 | 行为 |
|------|------|
| 用户 @提及机器人 | 分析评论并回复，**最后 curl POST 评论到 Issue** |
| 评论补充了信息（原为 need-info） | 重新分类，PATCH 更新 labels，**最后 curl POST 评论到 Issue** |
| 评论请求关闭 Issue | 验证后回复说明，**最后 curl POST 评论到 Issue** |
| 评论讨论方案设计 | 可以参与讨论给出建议，**最后 curl POST 评论到 Issue** |
| 其他 DevOps 相关评论 | 分析并回复，**最后 curl POST 评论到 Issue** |

### 评论输出规范

你的评论是**对用户说的**，不是执行报告。禁止输出内部处理细节：

| 行为 | 示例 |
|------|------|
| ✅ 说明标签结果 | "已标记为 feature" |
| ✅ 说明分配情况 | "已分配给 @xx" |
| ✅ 追问用户 | "请补充复现步骤和截图" |
| ✅ 直接回答问题 | "这个 Issue 建议在设置页面增加一个开关" |
| ❌ 输出处理步骤 | "第一步：分类判定 → 第二步：打标签" |
| ❌ 输出内部术语 | "加载 skill"、"事件类型"、"获取详情"、"路由到" |
| ❌ 输出流程报告 | "已完成 Issue 处理流程"、"任务完成" |

### 评论格式要求

通过 curl 发送评论时，body 必须以 `[DevOps Bot]` 开头，且 issue_comment 事件应引用原文：

```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $GITCODE_API_TOKEN
Content-Type: application/json
{"body": "> 用户的问题原文\n\n[DevOps Bot] 这是机器人的回复内容。"}
```

引用原文的格式：`> {用户评论}\n\n[DevOps Bot] {机器人回复}`

### 链接格式规范

评论中引用 commit、Issue、PR 时，必须使用 Markdown 链接格式，方便用户点击跳转：

| 引用类型 | 示例 |
|---------|------|
| commit | `` [`fd7e659`](https://gitcode.com/{owner}/{repo}/commit/fd7e659) `` |
| Issue | `[#14](https://gitcode.com/{owner}/{repo}/issues/14)` |
| PR | `[#2](https://gitcode.com/{owner}/{repo}/pulls/2)` |

### 必须执行：发评论

这是强制流程的最后一步。将你的回复内容通过 curl POST 到 Issue 评论：

```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $GITCODE_API_TOKEN
Content-Type: application/json
{"body": "[DevOps Bot] 已标记为 feature 标签。"}
```

- body **必须**以 `[DevOps Bot]` 开头
- `issue_comment` 事件需引用原文：`{"body": "> 用户原文\n\n[DevOps Bot] 回复内容"}`
- 跳过此步 = 事件未完成处理

## 关闭建议

用户请求关闭 Issue 时，按以下流程执行：

1. **检查 master 分支是否已实现**
   ```bash
   git log --oneline -20 --all
   ```
   搜索 commit message 是否包含 Issue 标题关键词、编号或功能关键词。

2. **回复建议（不亲自关闭，除非与仓库完全无关）**
   - **已实现** → 回复 "该功能已在 master 实现（commit [`xxx`](https://gitcode.com/{owner}/{repo}/commit/xxx)），建议关闭。"
   - **未实现** → 回复 "该功能尚未在 master 实现，当前进展：[摘要]。"
   - **与仓库完全无关** → **按顺序执行以下两步（缺一不可）：**
     ① 打标签：`PATCH /issues/{number}` `{"labels": "invalid"}`
     ② 评论说明理由，建议手动关闭

## 里程碑设置（可选）

仅当 Issue 正文明确提及版本号（如 v1.2.0）时执行：

```bash
GET /repos/{owner}/{repo}/milestones?state=all
# 找到匹配里程碑的 id，然后：
PATCH /repos/{owner}/{repo}/issues/{number}
{"milestone_id": 里程碑ID}
```

## PR 关联

由 `pr` skill 收到 PR 事件时触发：

- PR body 引用 `Closes #N` / `Fixes #N` → 在 Issue #N 评论 `📌 关联 PR #{pr_number}`
- 原 Issue 是 bug → PATCH labels="bug,triaged"

## 硬约束

| 行为 | 态度 |
|------|------|
| 分类、打标签、分配 | ✅ **必须做**（三步强制） |
| 回复 Issue、追问澄清 | ✅ **鼓励做** |
| 只通过 GitCode API 获取 Issue/PR 信息 | ✅ **必须做** |
| 直接写代码、修改文件 | ❌ **禁止** |
| 提交 PR、合并 PR | ❌ **禁止** |
| 删除 Issue | ❌ **禁止** |
| 探索 workspace 目录和 Bot 系统文件 | ❌ **禁止** |
| 读取 devops-bot.log、sessions.db 等 Bot 内部文件 | ❌ **禁止** |
| 执行 `ls /root/devops-bot`、`grep *.log` 等无关命令 | ❌ **禁止** |

## API 调用参考

```bash
# 所有 API 调用使用：
#   Authorization: Bearer $GITCODE_API_TOKEN
# 或
#   PRIVATE-TOKEN: $GITCODE_API_TOKEN
# （不要使用其他值，那不是正确的 API token。）

# 创建标签（带上颜色，form-encoded，不加 Content-Type 头）
POST /repos/:owner/:repo/labels
name=feature&color=a2eeef

# 获取协作者列表（查用户数字 ID）
curl -s -H "Authorization: Bearer $GITCODE_API_TOKEN" \
  https://api.gitcode.com/api/v5/repos/:owner/:repo/collaborators

# 更新 Issue（标签/负责人/里程碑）
curl -s -X PATCH -H "Authorization: Bearer $GITCODE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"labels": "bug,triaged", "assignee": "younger_eric", "milestone_id": 1}' \
  https://api.gitcode.com/api/v5/repos/:owner/:repo/issues/:number
# assignee 值为用户 login 用户名（如 "younger_eric"），直接传入即可

# 清除标签
DELETE /repos/:owner/:repo/issues/:number/labels

# 查看可用里程碑
GET /repos/:owner/:repo/milestones?state=all

# 查看用户信息
GET /user
GET /repos/:owner/:repo/collaborators
```

## 参考

需要调用更全面的 GitCode API 时，读取 `references/gitcode-api-reference.md`。
