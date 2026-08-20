# A2A 部署协作流程（Issue Agent 用）

## 多 Agent 协作交付链路

IaC 变更从 Issue 到部署的完整协作流程：

```
Issue Agent（需求分类、标签、指派、触发 A2A）
  → Coding Agent（创建 PR，实现 IaC 模板）
  → QA Agent（代码审查、安全检查、自动化测试）
    → 合并 PR
  → SRE Agent（部署执行、运维处置、故障排查）
  → Issue Agent（部署总结、经验沉淀）
```

### 各 Agent 职责边界

| Agent | 职责 | 不做 |
|-------|------|------|
| Issue | 需求分类、标签、指派、Spec 生成、A2A 触发 | 直接合并 PR |
| Coding | TF 配置生成、自审查、PR 创建 | 直接合并 PR |
| QA | 代码质量/功能/安全审查、报告输出 | 修改代码 |
| SRE | 部署执行、故障排查、运维处置 | 修改源代码（仅输出建议） |

## A2A 触发

Issue Agent 完成分类和 Spec 生成后，通过 A2A 依次派发 coding → qa 完成交付。
PR 合并后，通过 A2A 通知 SRE Agent 执行部署。

### A2A 触发示例

```bash
curl -X POST https://example.com/api/a2a \
  -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "sre",
    "payload": {
      "repository": "owner/repo",
      "account": "owner/repo",
      "issue_number": "8",
      "context": "PR已合并，凭证已就绪，执行部署"
    }
  }'
```

### 触发时机

| 目标 Agent | 触发条件 | context 内容 |
|-----------|---------|-------------|
| coding | Issue 分类完成，spec 已生成 | 需求描述、方案建议、关联资源 |
| qa | coding 创建 PR 后 | PR 编号、变更说明、资源清单 |
| sre | PR 已合并，凭证已就绪 | 仓库、Issue 编号、部署上下文 |
| issue（回调） | sre 部署完成 | 部署结果、资源清单、经验总结 |
