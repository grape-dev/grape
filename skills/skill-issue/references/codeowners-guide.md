# CODEOWNERS 创建引导

当仓库未配置 CODEOWNERS 文件时，引导用户创建。

## 引导 Issue 模板

以下为完整的引导 Issue 内容，发送时 `title` 固定为 `[ACTION] 请配置仓库模块负责人`，`body` 使用以下模板：

```markdown
## 本仓库尚未配置模块负责人

自动化分配 Issue 负责人需要 CODEOWNERS 文件。请按以下步骤配置：

### 第一步：回复此 Issue，填写各模块的负责人信息

**格式说明**：每个模块用 `---` 分隔，包含以下字段：

| 字段 | 必填 | 说明 |
|------|------|------|
| `模块` | 是 | 模块名称 |
| `责任人` | 是 | GitCode 用户名，带 @ 前缀 |
| `触发词` | 是 | 该模块涉及的领域关键词，多个用逗号分隔 |
| `描述` | 是 | 一句话说明模块职责 |

**示例：**

```yaml
模块：用户管理
责任人：@younger_eric
触发词：登录、注册、权限、角色、认证
描述：用户注册、登录、权限管理

模块：订单系统
责任人：@zhirong
触发词：订单、支付、退款、结算
描述：订单创建、支付对接、退款处理、财务结算

模块：部署运维
责任人：@wanglei_0526
触发词：部署、发布、CI/CD、流水线、构建
描述：CI/CD 流水线、环境部署、发布管理
```

### 第二步：机器人自动处理

你提交信息后，机器人会自动：
1. 生成 `CODEOWNERS` 文件
2. 推送到 `codeowner` 分支（独立分支，仅包含 CODEOWNERS 文件）
3. 在此 Issue 中回复告知结果

后续机器人将优先从 `codeowner` 分支读取负责人信息来分配 Issue。
```

## 创建引导 Issue 的 API 调用

```bash
curl -s -X POST -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "[ACTION] 请配置仓库模块负责人",
    "body": "上述引导模板的完整内容"
  }' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues
```

## 用户回复后的处理

当用户在引导 Issue 中回复了模块责任人信息后，将用户填写的内容提取出来，作为 A2A context 发送给 Coding Agent。

### context 构造示例

```
用户填写了以下模块责任人信息：
模块：用户管理
责任人：@younger_eric
触发词：登录、注册、权限、角色
描述：用户注册、登录、权限管理

模块：订单系统
责任人：@zhirong
触发词：订单、支付、退款
描述：订单创建、支付对接、退款处理

请根据以上信息生成 CODEOWNERS 文件，推送到 codeowner 分支。
```

### A2A 调用

```bash
curl -X POST {{SERVER_URL}}/api/a2a \
  -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "coding",
    "payload": {
      "repository": "'"$REPO_FULL_NAME"'",
      "account": "'"$ACCOUNT"'",
      "issue_number": "'"$ISSUE_NUMBER"'",
      "pr_number": "'"$PR_NUMBER"'",
      "context": "用户填写的模块责任人信息（见上方）"
    }
  }'
```

⚠️ **字段规则（A2A 服务端强制校验）：** `issue_number` 必填（当前 Issue 的真实编号）；**无法确定 issue_number 就不要调用 A2A**。`pr_number` 有则填（PR 编号）、无则不填，严禁将 PR 编号填入 issue_number。

### 通知责任人（必做）

责任人确定后调用 `POST /api/tasks/notify` 创建任务（详见 SKILL.md「任务通知硬规则」）。
