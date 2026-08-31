# A2A 交办 SRE Agent（QA Agent 用）

## 触发时机

QA 审查通过后，**自动合并 PR**，合并成功后 **必须** A2A 交办 SRE Agent 执行部署。

### 触发步骤（按顺序执行）

1. QA 审查结论为 ✅ LGTM（无 P0/P1 阻塞项）
2. QA 通过 GitCode API 自动合并 PR：`PUT /repos/:owner/:repo/pulls/:number/merge`
3. PR 合并确认后，通过 A2A 交办 SRE Agent

### 不触发的情况

- 审查结论为 Request Changes（有 P0/P1 未修复）→ A2A 回 Coding Agent 修复
- PR 合并失败（如冲突、权限不足）→ 在 PR 评论告知「PR 合并失败：<原因>，请人工处理」，不提前交办 SRE

⚠️ **不可只在报告中「建议」维护者交办 SRE**——必须自己发起 A2A 调用。

## A2A 示例

```bash
curl -X POST https://example.com/api/a2a \
  -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "sre",
    "payload": {
      "repository": "owner/repo",
      "account": "owner/repo",
      "issue_number": "1",
      "pr_number": "1",
      "context": "PR#1 已合并，QA 审查通过（✅LGTM）。请执行 Terraform 部署：make init && make plan && make apply。注意：需配置 HW_ACCESS_KEY/HW_SECRET_KEY 环境变量，预创建 OBS state 桶。"
    }
  }'
```

## 成功判定

- 响应包含 `"status":"ok"` 且 `"seq"` 为非零数字 → 交办成功，SRE 任务已入队
- 响应 `"seq":0` 或 `"skipped":"sre_subscribed"` → SRE 配置了 subscribe_events 去重，需清空订阅后重发
- 响应 `"status":"error"` 或无响应 → 参考失败降级

## 失败降级

A2A 调用失败时（网络错误/超时/非零 seq 未返回）：

1. 重试 3 次，间隔 5 秒
2. 仍失败则在 Issue 评论告知「A2A 交办 SRE Agent 失败，请手动触发部署」
3. 在最终输出的 `next_step` 字段标注「需人工介入：SRE Agent 未自动接手，部署未执行」

## 注意事项

- `repository` 和 `account` 字段必须传 `owner/repo` 全称（如 `gcw_3FZApeLT/tf-infra`），不能传裸仓库名
- `context` 应包含 QA 审查结论摘要 + 部署所需的注意事项（凭证、state 桶等）
- A2A 成功后 issue_state 会从 qa 推进到 sre，SRE Agent 接管部署流程
