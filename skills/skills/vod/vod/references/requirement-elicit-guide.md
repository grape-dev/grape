# 需求引导与 Issue 审视指南（VOD）

> 配套 `skills/vod/SKILL.md`（v2.0）。覆盖：清晰度四要素、引导漏斗、Issue 审视流程、结构化模板、A2A 交办。

## 一、清晰度四要素

判断一条开发者声音（消息 / Issue）是否构成**清晰开发者需求**：

| 要素 | 判断问题 | 不清晰示例 |
|------|---------|-----------|
| 目标 | 一句话能说清要达成什么？ | "搞一下这个" |
| 现状 | 当前行为/缺失是否明确？ | 未说明当前表现 |
| 期望行为 | 期望的具体行为/场景步骤是否清晰？ | "更好用点" |
| 验收标准 | 是否可验证？ | 无任何可检验描述 |

四要素齐全 → 清晰；任一缺失/含糊 → 需引导。

## 二、需求引导漏斗（多轮对话）

### 规则
| 规则 | 说明 |
|------|------|
| 第一轮 | 提取意图，判断清晰度；模糊则提 1-3 个针对性澄清问题（结合知识基线，问得具体） |
| 判断标准 | 目标/现状/期望/验收 四要素齐全 → 清晰 |
| 多轮 | 每次基于上一轮回答追问，不重复已确认项 |
| 上限 | 最多 3 轮；超限 → 建「待澄清」Issue（场景 A）或在 Issue 评论列缺失项（场景 B），不无限追问 |
| 语气 | 中文、简洁、引导式（不用"请填写模板"式，用具体问题） |

### 提问原则
- **结合知识基线**：参考 `data/workspaces/{owner}/{repo}/project-info/project-graph.md` + `project-capabilities.md`，让问题贴合项目实际
- 示例：
  ```
  用户："这个项目能不能支持批量操作？"
  VOD（结合 project-graph 已知是 FastAPI + 任务队列）：
    "您指的是哪个对象的批量操作？（当前项目有任务、仓库、用户三类管理对象）
     另外期望的批量操作是仅后端接口，还是需要前端入口？"
  ```
- **Issue 评论追问格式**（body 以 `[VOD Agent]` 开头）：
  ```bash
  POST /repos/{owner}/{repo}/issues/{number}/comments
  Authorization: Bearer $BOT_GITCODE_TOKEN
  Content-Type: application/json
  {"body": "> 用户的问题原文\n\n[VOD Agent] 您指的是哪个对象的批量操作？……"}
  ```

## 三、Issue 审视流程（场景 B，`issue_opened`）

1. **自建排除**：Issue 含 `auto-submitted by VOD Agent` 标记 → 跳过审视，直接 A2A 交办 Issue Agent
2. **清晰度审视**：读取 Issue 完整内容（title/body/标签/评论），对照四要素；结合知识基线判断可执行性/重复/歧义
3. **沟通澄清**（不清晰时）：在 Issue 评论 @开发者 追问（body 以 `[VOD Agent]` 开头），或飞书群沟通；≤3 轮
4. **结构化总结**：`PATCH /issues/{number}` 将 body 收敛为模板结构
5. **A2A 交办**：`POST /api/a2a {"agent":"issue", ...}` → Issue Agent 做 spec/标签/里程碑/分配

## 四、结构化 Issue 模板

```markdown
## 目标
{用户想达成的结果，一句话}

## 现状
{当前行为/缺失，结合知识基线描述}

## 期望行为
{清晰描述期望，可含使用场景步骤}

## 验收标准
- [ ] {可验证的验收项 1}
- [ ] {验收项 2}

## 优先级
P0（紧急）/ P1（重要）/ P2（一般）

---
来源：{飞书用户反馈 / Issue 评论 / PR 评论}
auto-submitted by VOD Agent（Feishu）
```

## 五、A2A 交办格式

```bash
curl -X POST {{SERVER_URL}}/api/a2a \
  -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "issue",
    "payload": {
      "repository": "'"$REPO_FULL_NAME"'",
      "account": "'"$ACCOUNT"'",
      "issue_number": "'"$ISSUE_NUMBER"'",
      "pr_number": "",
      "context": "VOD 已完成需求收敛与结构化，请继续 spec 设计/打标签/排里程碑/分配责任人。结构化需求：<摘要>"
    }
  }'
```

> ⚠️ `issue_number` 必填；无法确定则不调 A2A，在评论中说明。交办后 Issue 生命周期进入 `issue` 阶段（`issue_state` 归属更新）。
>
> ⚠️ **失败降级**：A2A 调用返回非 2xx 或连接超时 → 重试最多 3 次（每次间隔数秒）；仍失败 → 在 Issue 评论说明"A2A 服务不可达，待恢复后重试交办"，并在 `next_step` 标注需人工介入，不得静默跳过。
