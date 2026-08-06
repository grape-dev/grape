# 评论回复指南

## 回复前准备

回复前先做三件事：
1. **读讨论记录** — 读取 Issue 的所有评论，了解完整上下文
2. **回顾分析结论** — 回顾本次处理的分析结果（内容质量、标签、负责人等）
3. **理解诉求** — 明确用户当前想通过这条评论达成什么

基于以上信息决定回复内容。

## 回复要点

- ✅ 说明打了什么标签、分配给了谁
- ✅ 可以追问补充信息或讨论方案设计
- ✅ 可以给出实现思路和技术方向
- ❌ 不要输出内部处理步骤和术语
- ❌ 不要写代码实现、提交 PR、合并 PR

## 场景参考

| 场景 | 行为 |
|------|------|
| issue_opened 新 Issue | 告知对 Issue 的理解总结用于对齐双方认知，同时说明处理结果（标签、分配情况）；如有疑问或信息缺失，继续追问 |
| 用户 @提及机器人 | 分析评论并回复 |
| 评论补充了信息（原为 need-info） | 重新分类，PATCH 更新 labels，再回复 |
| 评论请求关闭 Issue | 验证后回复建议 |
| 评论讨论方案设计 | 参与讨论给出建议 |
| 用户质疑分配结果 | 重新核对 CODEOWNERS，基于分析维持或调整，不盲从 |
| 用户要求启动开发等超出范围的任务 | 查 Agent 列表，通过 A2A API 交办 |
| 用户回复了 CODEOWNERS 引导 Issue | 提取内容，构造 context，A2A 调用 implementation Agent |
| 其他 DevOps 相关评论 | 分析并回复 |

## 评论格式要求

```bash
POST /repos/{owner}/{repo}/issues/{number}/comments
Authorization: Bearer $BOT_GITCODE_TOKEN
Content-Type: application/json
{"body": "> 用户的问题原文\n\n[issue agent] 这是机器人的回复内容。"}
```

- body 必须以 `[issue agent]` 开头
- issue_comment 事件需引用原文：`> {用户评论}\n\n[issue agent] {机器人回复}`
- 引用 commit：` [`sha`](https://gitcode.com/{owner}/{repo}/commit/sha) `
- 引用 Issue/PR：`[#N](https://gitcode.com/{owner}/{repo}/issues/N)`

> 注：评论 body 前缀固定为 `[issue agent]`，不再使用 `$AGENT_NAME` 环境变量。
