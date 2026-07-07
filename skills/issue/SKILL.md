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

你收到 Issue 事件后只做 **4 件事**，按顺序依次执行，**不得跳过**：

```
分类判定 → 打标签（API） → 分配负责人（API） → 评论回复
```

你可以与作者讨论方案设计、帮助梳理需求、给出实现思路，但 **禁止亲自实现**。

## 强制流程（每一步都必须执行）

### 第一步：分类与打标签

读取 Issue 标题和正文 → 按以下标准判定 → **立即调用 API 设置标签**。

| 标签 | 适用场景 | 判定线索 |
|------|---------|---------|
| `feature` | 新功能请求 | 标题含"建议/需求/新增/支持/实现/希望/能不能" |
| `bug` | 缺陷报告 | 标题含"bug/故障/报错/错误/不生效/白屏/异常/500"或正文含堆栈 |
| `need-info` | 信息不足 | 正文 < 30 字、无复现步骤、无日志/截图 |
| `triaged` | 已分诊完成 | **与主标签一起加**，表示已完成分诊 |

**判定后立即执行：**
```bash
# 可判定分类：
PATCH /repos/{owner}/{repo}/issues/{number}
{"labels": "feature,triaged"}
# 或
{"labels": "bug,triaged"}

# 信息不足：
PATCH /repos/{owner}/{repo}/issues/{number}
{"labels": "need-info"}
```

> 标签不存在时 API 会自动创建。同时符合多个分类则逗号分隔。

### 第二步：分配负责人

1. 检查 Issue 正文中的 `@用户名`（优先作为分配目标）
2. 检查仓库根目录是否有 `CODEOWNERS` 文件
3. 确定所属领域（前端/后端/DevOps/文档）
4. 获取用户数字 ID：`GET /repos/{owner}/{repo}/collaborators`
5. 调用 API 分配：

```bash
PATCH /repos/{owner}/{repo}/issues/{number}
{"assignee_ids": [用户数字ID]}
```

> 不确定分配谁 → 不强制设 assignee_ids，在评论中建议即可。

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
| 用户 @提及机器人 | 分析评论并回复 |
| 评论补充了信息（原为 need-info） | 重新分类，PATCH 更新 labels |
| 评论请求关闭 Issue | 验证后关闭或回复说明 |
| 评论讨论方案设计 | 可以参与讨论，给出建议 |
| 其他 DevOps 相关评论 | 分析并回复 |

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
| 讨论方案设计、给思路方向 | ✅ **可以做** |
| 直接写代码、修改文件 | ❌ **禁止** |
| 提交 PR、合并 PR | ❌ **禁止** |
| 删除 Issue | ❌ **禁止** |

## API 调用参考

```bash
# 更新 Issue（标签/负责人/里程碑）
PATCH /repos/:owner/:repo/issues/:number
Content-Type: application/json
Body: {"labels": "bug,triaged", "assignee_ids": [12345], "milestone_id": 1}

# 清除标签
DELETE /repos/:owner/:repo/issues/:number/labels

# 查看可用里程碑
GET /repos/:owner/:repo/milestones?state=all

# 查看用户信息
GET /user
GET /repos/:owner/:repo/collaborators
```

## 参考

需要调用更多 GitCode API 时，读取 `devops-event-ops/references/gitcode-api-reference.md`。
