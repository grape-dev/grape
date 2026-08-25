# A2A 部署协作流程（Issue Agent 用）

## 多 Agent 协作交付链路

根据 Issue 内容选择交付路径：

### 完整链路（需要新建或大幅修改 TF 代码）

```
Issue Agent（需求分类、标签、指派、触发 A2A）
  → Coding Agent（创建 PR，实现 IaC 模板）
  → QA Agent（代码审查、安全检查、自动化测试）
    → 合并 PR
  → SRE Agent（部署执行、运维处置、故障排查）
```

### 快速部署（应用项目仓库，无 TF 模板，SRE 临时编写 TF 代码执行部署）

```
Issue Agent（需求分类、标签：quick-deploy、触发 A2A）
  → SRE Agent（在临时工作目录编写 TF 代码，执行部署，不提交到仓库）
```

快速部署适用场景：
- 仓库是具体应用项目（如博客项目），不是 IaC 仓库
- Issue 要求部署基础设施，但仓库中没有也不需要 TF 模板
- SRE 在临时工作目录中编写 TF 代码并执行部署，不提交到应用项目仓库，避免污染代码库

### 各 Agent 职责边界

| Agent | 职责 | 不做 |
|-------|------|------|
| Issue | 需求分类、标签、指派、Spec 生成、A2A 触发、交付路径判定 | 直接合并 PR |
| Coding | TF 配置生成、自审查、PR 创建 | 直接合并 PR |
| QA | 代码质量/功能/安全审查、报告输出 | 修改代码 |
| SRE | 部署执行、故障排查、运维处置 | 修改源代码 |

## A2A 触发

### 完整链路

Issue Agent 完成分类和 Spec 生成后，通过 A2A 依次派发 coding → qa 完成交付。
PR 合并后，通过 A2A 通知 SRE Agent 执行部署。

### 快速部署

Issue Agent 判定为快速部署后，直接通过 A2A 派发 SRE Agent，跳过 Coding 和 QA。

### A2A 触发示例（快速部署）

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
      "context": "快速部署，仓库无 TF 模板，SRE 在临时目录编写 TF 代码执行部署，不提交到仓库。凭证已就绪"
    }
  }'
```

### 触发时机

| 目标 Agent | 触发条件 | context 内容 |
|-----------|---------|-------------|
| coding | Issue 分类完成，spec 已生成（完整链路） | 需求描述、方案建议、关联资源 |
| qa | coding 创建 PR 后（完整链路） | PR 编号、变更说明、资源清单 |
| sre | PR 已合并，凭证已就绪（完整链路） | 仓库、Issue 编号、部署上下文 |
| sre | Issue 判定为快速部署，仓库无 TF 模板（快速部署） | 仓库、Issue 编号、快速部署标记、部署需求 |
