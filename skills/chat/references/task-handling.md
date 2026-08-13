# 任务场景：澄清被分配 Issue（task-handling）

当用户从**任务中心**进入对话（上下文含「📋 任务分配 · Issue #N」）或明确要处理某个 Issue 时，执行本流程，帮助用户**明确该 Issue 的需求**并回帖到 Issue。

## 1. 判断是否进入 Issue 澄清

- 对话上下文里存在 `📋 任务分配 · Issue #N`（来自任务中心「去对话」）→ 进入澄清流程
- 用户直接说「帮我看看 Issue #N」/「这个 Issue 要怎么改」→ 进入澄清流程
- 仅闲聊 / 一般查询 → 不进入

## 2. 读取 Issue 背景

用 GitCode API 获取 Issue 详情和全部评论，完整理解背景：

```bash
# 获取 Issue 详情
curl -s -H "Authorization: Bearer $USER_GITCODE_TOKEN" \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}

# 获取该 Issue 的全部评论（理解讨论过程）
curl -s -H "Authorization: Bearer $USER_GITCODE_TOKEN" \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/comments
```

> `{owner}/{repo}` 和 `{number}` 来自任务上下文消息（仓库 + Issue 号）。

## 3. 分析不清晰的关键点

基于 Issue 类型，检查信息完整性，找出**最影响理解的缺失项**：

**Bug/问题类**：
| 检查项 | 缺失时引导 |
|--------|-----------|
| 复现步骤 | 「能否提供一步步复现的步骤？」 |
| 错误信息/日志/截图 | 「有没有报错日志或截图？」 |
| 环境信息 | 「在什么环境/版本下出现？」 |
| 预期 vs 实际 | 「期望的表现 vs 实际发生了什么？」 |

**功能/增强类**：
| 检查项 | 缺失时引导 |
|--------|-----------|
| 需求描述 | 「你希望实现什么功能？核心目的是什么？」 |
| 使用场景 | 「在什么场景下会用到？用户故事是怎样的？」 |
| 验收标准 | 「怎么算完成？期望的效果是什么？」 |
| 边界/范围 | 「有没有『优化/更好』这类模糊表述需要明确？」 |

## 4. 精准引导用户补充

- **一次只问最关键的 1-3 个问题**，不要全部抛出（避免用户负担）。
- 用自然语言引导，结合已理解的 Issue 背景给出针对性提问。
- 用户回答后，继续追问下一个不清晰点，直到关键信息齐全。

## 5. 回帖到 Issue（明确后）

用户澄清完整后，将「Issue 原始内容 + 用户的澄清补充」汇总，**以用户身份回帖到该 Issue**：

```bash
curl -s -X POST -H "Authorization: Bearer $USER_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "**补充需求澄清**（由任务处理对话整理）：\n\n## 背景\n<原 Issue 核心内容>\n\n## 澄清后的需求\n- **使用场景**：<用户补充>\n- **预期效果**：<用户补充>\n- **验收标准**：<用户补充>\n\n---\n补充人：<用户昵称>（通过 Chat 对话整理）"}' \
  https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/comments
```

> **重要**：回帖使用用户自己的 `$USER_GITCODE_TOKEN`（不是 Grape token）。回帖后会触发该 Issue 的 webhook 事件，由于是用户本人（非 bot）发出的评论，**不会被 self-comment 过滤**，issue agent 会重新分诊并刷新责任人。

## 6. 回复用户

回帖成功后，用自然语言告知用户已把澄清内容回写到 Issue，并提示后续 issue agent 会重新分析处理。
