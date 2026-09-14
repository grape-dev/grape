# Issue #95: chat 路径 Agent 注入流程

## 改前流程（问题标注）

```
触发：用户在 Issue 对话窗口发消息
执行：build_event_chat_prompt (prompt_builder.py:907)

─────────────────────────────────────────

1. 事件上下文
   - Seq / 事件类型 / 标题 / 状态 / Agent 结论

2. Skill 指令
   "用 read tool 读取 event-dialogue/SKILL.md 并严格执行"
   "只做对话 / 沉淀共享知识 / 流程控制"

3. 对话历史
   最近 20 条消息

4. _build_knowledge_section(repo_key, agent_type, issue_num)
   → 查 ref 表不过滤 source                ← 问题：三源交叉
   → 返回 system + agent + manual 全部
   → 内联到 prompt: "[source 级] {content}"
   → 包含:
     - baseline 全文（project-graph.md 等）  ← 问题：#67 之前没有，#67 引入
     - agent 知识（project/issue/issue_agent）
     - member 用户上传知识全文              ← 问题：不该内联

5. 用户消息

6. 回复提示
   "调 POST /events/{seq}/knowledge 沉淀知识"
```

## 改后流程

```
触发：用户在 Issue 对话窗口发消息
执行：build_event_chat_prompt (prompt_builder.py:907)

─────────────────────────────────────────

1. 事件上下文
   - Seq / 事件类型 / 标题 / 状态 / Agent 结论

2. Skill 指令
   "用 read tool 读取 event-dialogue/SKILL.md 并严格执行"
   "只做对话 / 沉淀共享知识 / 流程控制"

3. 对话历史
   最近 20 条消息

4. _build_agent_knowledge_section(repo_key, agent_type, issue_num, scene_id)
   → 查 ref 表 source='agent'              ← 改后：只查 agent 沉淀知识
   → 三维匹配: scene_id + agent_type + issue_num
   → + credential 过滤
   → read_entry_content 读内容
   → 内联到 prompt: "[agent 级] {content}"
   → 恢复 #67 之前的原始语义

5. 用户消息

6. 回复提示
   "调 POST /events/{seq}/knowledge 沉淀知识"
```

## chat 路径不调 _build_project_info_section

chat 路径从最初设计（e22dbce7）就不调 _build_project_info_section：
- 没有 baseline 摘要内联
- 没有 member 引导文字
- 只有 _build_agent_knowledge_section 内联 agent 知识

这是设计如此——chat 是对话场景，agent 做对话/沉淀/流程控制，不需要 baseline 项目背景。

## chat vs devops 对比

| 项 | chat 路径 | devops 流水线 |
|----|----------|-------------|
| 触发 | Issue 对话窗口发消息 | issue_opened / a2a_event |
| 执行 | build_event_chat_prompt | build_devops_agent_prompt |
| worker 物化 | 不走 | 走 enqueue_task |
| .project-info/ | 无 | 有（baseline + member 物化） |
| .knowledge/ | 无 | 有（agent 知识物化） |
| baseline | 不注入 | _build_project_info_section 内联摘要 |
| agent 知识 | _build_agent_knowledge_section 内联全文 | agent_knowledge_files() 物化到 .knowledge/ |
| member | 不注入 | member_files_pi() 物化到 .project-info/member/ |
| credential 过滤 | 有 | 有 |

## 改前改后对比

| 项 | 改前 | 改后 | 说明 |
|----|------|------|------|
| 函数名 | _build_knowledge_section | _build_agent_knowledge_section | 语义明确 |
| source 过滤 | 无 | source='agent' | 恢复 #67 之前语义 |
| baseline 内联 | 有（#67 引入） | 无 | #67 之前就没有，恢复原状 |
| member 内联 | 有（#67 引入） | 无 | #67 之前就没有，恢复原状 |
| agent 知识内联 | 有 | 有 | 不变 |
| credential 过滤 | 无 | 有 | 新增 |
