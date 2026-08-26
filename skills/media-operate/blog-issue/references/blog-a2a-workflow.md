# 博客 A2A 协作链路

## 多 Agent 协作交付链路（博客专属）

```
blog-vod Agent（需求澄清）
  → A2A → blog-issue Agent（本技能：分析→Spec→A2A）
    → A2A → coding（ai-blog-generation 技能：生成正文+配图+视频→提交PR）
      → A2A → sre（blog-publish 技能：合并PR→上传媒体→发布文章）
```

### 各 Agent 职责边界

| Agent | 职责 | 不做 |
|-------|------|------|
| blog-vod | 博客需求澄清、结构化需求清单 | 直接 A2A coding/sre |
| blog-issue | 可开发性分析、博客 Spec 生成、A2A 触发 coding | 直接写博客内容、直接发布 |
| coding | 撰写 HTML 正文、AI 配图/视频生成、提交 PR | 直接调用博客发布 API、直接合并 PR |
| sre | 合并 PR、上传媒体到博客服务器、发布文章 | 修改博客内容（仅发布操作） |

⚠️ **博客链路跳过 QA**：博客产物是 HTML/图片/视频，不涉及代码逻辑变更，无需代码审查。

## A2A 触发时机

| 目标 Agent | 触发条件 | context 内容 |
|-----------|---------|-------------|
| coding | 博客 Spec 生成完成，评级 ready | 博客规格摘要 + 指定 ai-blog-generation 技能 |
| sre | coding 完成 PR 创建后（由 coding 触发，非 issue） | PR 编号 + 博客目录 + 发布目标地址 |

### Issue Agent → Coding（博客场景）

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
      "context": "博客 Spec 已发布（评级 ready）。请使用 ai-blog-generation 技能执行。\n\n## 博客规格\n{规格摘要}\n\n## 发布目标\n博客系统: {BLOG_HOST}\n接口: POST /api/articles + POST /api/upload\n\n完成后请 A2A 通知 sre 使用 blog-publish 技能发布。"
    }
  }'
```

### Coding → SRE（coding 完成后触发，非 Issue Agent 触发）

此步骤由 coding agent 的 ai-blog-generation 技能内部执行，Issue Agent 不参与。详见 `ai-blog-generation/SKILL.md` Step 8。

```bash
curl -X POST {{SERVER_URL}}/api/a2a \
  -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "sre",
    "payload": {
      "repository": "'"$REPO_FULL_NAME"'",
      "account": "'"$ACCOUNT"'",
      "issue_number": "'"$ISSUE_NUMBER"'",
      "context": "博客内容已生成并提交 PR。\n\n- 仓库: '"$REPO_FULL_NAME"'\n- PR 编号: {PR_NUMBER}\n- 博客目录: blog/{topic-slug}/\n\n请使用 blog-publish 技能：\n1. 合并 PR\n2. 从 blog/ 目录读取文章+媒体\n3. 上传媒体到博客服务器\n4. 发布文章\n5. 在 Issue 评论反馈发布结果"
    }
  }'
```

## 重复 A2A 检查（必须做）

A2A 前先看 prompt 中的 Issue Pipeline 信息，如果当前 Issue 的 pipeline 中**已包含目标 agent 的 `a2a_event`**（如已有 `coding:a2a_event` 则不再 A2A 给 coding），**不要重复 A2A**——只发评论回复即可。

## 成功判定

- 响应包含 `"status":"ok"` 且 `"seq"` 为非零数字 → 交办成功
- 响应 `"seq":0` 或 `"skipped"` → 交办被跳过，检查目标 agent 是否已配置 subscribe_events 去重

## 失败降级

A2A 调用失败时：
1. 重试 3 次，间隔 5 秒
2. 仍失败则在 Issue 评论告知「A2A 交办失败，请手动触发」
3. 在最终输出的 `next_step` 字段标注「需人工介入」
