# A2A 交办 Issue Agent（VOD Agent 用）

## 触发时机

VOD 完成以下全部动作后，**必须** A2A 交办 Issue Agent：

1. Issue 标签已添加（至少 `vod` + 类型/优先级标签）
2. 分析评论已发布到 Issue
3. 无需进一步澄清（或已在评论中提出澄清问题，等待回复）

⚠️ 不可跳过此步骤直接交给 Coding/QA/SRE。VOD 的职责边界到分析评论为止。

## A2A 示例

```bash
curl -X POST https://example.com/api/a2a \
  -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "issue",
    "payload": {
      "repository": "owner/repo",
      "account": "owner/repo",
      "issue_number": "1",
      "context": "VOD 分析完成。Issue 已标签 vod/type:feature/priority:P1，已发布分析评论。请 Issue Agent 接力进行分类确认、指派、Spec 生成，并触发 Coding Agent。"
    }
  }'
```

## 成功判定

- 响应包含 `"status":"ok"` 且 `"seq"` 为非零数字 → 交办成功
- 响应 `"seq":0` 或 `"skipped"` → 交办被跳过，检查目标 agent 是否配置了 subscribe_events 去重

## 失败降级

A2A 调用失败时（网络错误/超时/非零 seq 未返回）：

1. 重试 3 次，间隔 5 秒
2. 仍失败则在 Issue 评论告知「A2A 交办 Issue Agent 失败，请手动触发」
3. 在最终输出的 `next_step` 字段标注「需人工介入：Issue Agent 未自动接手」

## 注意事项

- `repository` 和 `account` 字段必须传 `owner/repo` 全称（如 `gcw_3FZApeLT/tf-infra`），不能传裸仓库名
- `context` 应包含 VOD 分析结论摘要，供 Issue Agent 继承上下文
- A2A 成功后 Issue Agent 会接管 stage（从 vod 推进到 issue）
