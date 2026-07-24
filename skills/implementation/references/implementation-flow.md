# 开发实现流程

## 问询责任人（仅兜底）

仅当无 A2A context 且通过 issue_labeled + PATCH 标签触发时执行：

```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $GITCODE_API_TOKEN
Content-Type: application/json
{"body": "此需求是否启动开发？回复「启动」或「暂缓」。如需调整方案，请一并说明。"}
```

## CODEOWNERS 生成

收到 A2A context 后，解析模块责任人信息，生成 CODEOWNERS 并推送：

```bash
git checkout -b codeowner
cat > CODEOWNERS << 'EOF'
# 模块：用户管理
# 责任人：@username
# 触发词：登录、注册、权限、角色
# 描述：用户认证与权限管理
* @username
EOF
git add CODEOWNERS
git commit -m "docs: add CODEOWNERS for module ownership"
git push origin codeowner
```

## 创建开发分支

```bash
git checkout -b feat/issue-{number}-{简短描述}
git push origin feat/issue-{number}
```

## 需求文档（仅大型 feature）

读取并执行 `skills/agent/spec-driven-development/SKILL.md`
- 编写 PRD：Objective、Commands、Structure、Style、Testing、Boundaries
- 保存到 `docs/spec.md`，评论 `📋 需求文档已归档到 [docs/spec.md](链接)`

## 方案设计（中等及以上 feature）

读取并执行 `skills/agent/planning-and-task-breakdown/SKILL.md`
- 输出任务到 `tasks/plan.md` 和 `tasks/todo.md`
- 评论 `📋 方案设计已归档到 [tasks/plan.md](链接)`

## 代码实现

1. 测试先行 — 读取 `skills/agent/test-driven-development/SKILL.md`
   - RED：先写测试 → GREEN：最小代码通过 → REFACTOR：清理
2. 增量实现 — 读取 `skills/agent/incremental-implementation/SKILL.md`

```bash
git add -A && git commit -m "feat: #{number} 描述"
git push origin feat/issue-{number}
```

## 自审查（可选）

读取 `skills/agent/code-review-and-quality/SKILL.md`
- 五轴审查：正确性、可读性、架构、安全、性能

## 创建 PR

```bash
# 创建 PR
curl -sL -X POST -H "Authorization: Bearer $GITCODE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "feat: #{number} 标题",
    "head": "feat/issue-{number}",
    "base": "main",
    "inner_issue_nums": "#{owner}/{repo}#{number}",
    "close_related_issue": true
  }' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls

# PATCH 补充 body
curl -sL -X PATCH -H "Authorization: Bearer $GITCODE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "body": "## 概述\n\n自动实现 Issue #{number}",
    "labels": "auto-pr",
    "prune_branch": true
  }' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{pr_number}
```

`inner_issue_nums` 是字符串 `"#owner/repo#number"`，`close_related_issue: true` 表示 PR 合入时自动关闭 Issue。

评论告知：
```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $GITCODE_API_TOKEN
Content-Type: application/json
{"body": "✅ 已完成实现，请审查 PR [#{pr_number}]。"}
```
