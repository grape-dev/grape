# 方案：Issue「待澄清」状态显式化 —— Agent 自报 `tasks awaiting-input` 接口

> 状态：技术方案（2026-10-01）｜目标分支：weekly-05-lyz
> 关联：`issue.status='awaiting_input'` 当前由「输出关键词判定」隐式进入，判定与模型措辞强耦合、
> 存在误判/漏判/路径不一致/静默失败四类不确定性；本方案改为 Agent 显式契约调用（对标 `tasks complete` 自报完成）。

---

## 1. 现象与现状问题

### 1.1 现状进入「待澄清」的两条路径

| 路径 | 位置 | 说明 |
|------|------|------|
| **自动（关键词判定，主）** | `event_pipeline.py:2980-2995` | vod/issue agent 当轮 `completed` 后，`_detect_awaiting_input(result_text)`（`event_pipeline.py:535-539`）对关键词表做子串匹配，命中 → `UPDATE issue SET status='awaiting_input'` |
| 手动（owner 改状态） | `issues.py:445-477` | `PUT /api/issues/{issue_id}/status` 按 `_TASK_STATUS_FLOWS`（`issues_center.py:530-535`）合法迁移（active/processing → awaiting_input） |

关键词表（`event_pipeline.py:533`）：`澄清 / 需要确认 / 请问 / 请提供 / 需要补充 / 请回复 / 请说明 / 需要以下信息`。

### 1.2 自动判定的四类不确定性

1. **漏报**：Agent 用其它表达求输入（如「你回 Yes 我就继续」「选 A 还是 B」「等你确认后我再动」）不命中关键词 → 实际已停下等用户，卡片仍 `processing`，进度状态失真。
2. **误报**：输出中「提到」澄清相关词但并非要等输入（如总结「本轮无需澄清」、复述技能要求）→ 误置待澄清，卡住流水线或误导用户去澄清。
3. **执行路径不一致**：检测只在服务端直连执行（`_execute_agent_inner`）收尾跑；worker 远端回传 `complete` 的路径**不检测** → 同行为两种执行模式结果不同。
4. **静默失败**：`UPDATE … WHERE status IN ('active','processing')` 未命中行时 rowcount=0，无返回/日志提示 → Agent 以为已设置，实际未设置。

根因：**把「进度语义」寄托在模型口语措辞上，非契约化**；且判定对象是 `result_text` 文本（常为结构化 JSON），本质是在猜意图。

---

## 2. 修复目标

- **新增会话级显式接口**，Agent 主动声明「我需要用户输入」，语义与 `tasks complete` 的自报完成对称。
- 接口**原子迁移** `active/processing → awaiting_input`，**返回是否迁移成功**（消除静默失败）。
- 允许携带**澄清问题上下文**（写入任务会话，供 `GET /api/issues/{id}/clarifications` 复用展示）。
- **直接删除关键词自动判定**（退役，不留兜底），只保留 Agent 显式接口，杜绝双路径抢改状态产生抖动。
- 非终态语义：**不置 task 终态、不推进 pipeline stage**，等开发者通过现有 `POST /{issue_id}/clarify`（`issues.py:834-916`）回复 → `awaiting_input → processing` 闭环。

---

## 3. 方案设计

### 3.1 端点契约

新增 `POST /api/tasks/awaiting-input`

| 项 | 值 |
|----|----|
| 认证 | `session_only`（进 `openapi._SESSION_ONLY_OPERATIONS`）：仅 `tk_` 会话 token |
| 鉴权 | `_require_session_repo_token`（`issues_center.py:20`）校验仓库作用域；会话 token 携带 `agent_type`，仅放行 `{vod, issue}` |
| token 定位 | `task_id = user.session_key`（与 `tasks complete` 一致，`tasks.py:305-307`）；体上无需传 id |
| 拒绝 | `BOT_GITCODE_TOKEN` / `grape_` API Key / JWT → 403；非 vod/issue Agent → 403 |

请求体（`TaskAwaitingInputBody`）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `questions` | string | ✅ | 向开发者提出的澄清问题 / 需要补充的信息 |
| `reason` | string | 否 | 进入待澄清的原因（可选，写日志/归档） |

出参：

```json
{"ok": true, "task_id": "t_xxx", "issue_id": 210, "status": "awaiting_input", "changed": true}
```

> `changed=false` 表示已完成/已处于等待/终态等无需迁移（幂等），与 `tasks complete` 的 `already` 语义对齐。

### 3.2 处理流程（`awaiting-input` handler）

1. **鉴权 + 定位 task**：会话 token → `task_id`；查 `agent_tasks`，校验 repo 作用域 + `agent_type ∈ {vod, issue}`。
2. **幂等边界**：task 已 `completed/failed/cancelled` → 返回现状 `changed=false`（同 `tasks.py:330-335`）。
3. **原子迁移**：
   ```sql
   UPDATE issue SET status='awaiting_input'
   WHERE repo_key=? AND issue_number=? AND status IN ('active','processing')
   ```
   以 `rowcount` 决定 `changed`；未命中 → `changed=false` 并记 warning（不再静默）。
4. **写入澄清上下文**：复用 `_ensure_task_conversation` / `add_message`，把 `questions` 作为 agent 消息写入任务会话（供 `GET /api/issues/{issue_id}/clarifications` 直接展示，`issues.py:789` 读取通道复用）；可选补 `events.agent_logs` 记录「Agent 显式进入待澄清」。
5. **飞书卡片通知**（awaiting 的语义就是等用户输入，必须主动触达用户）：复用 `_run_notify` 静态通知机制（`issues_center.py:461-494`），以**用户本人 chat bot** 发卡片，正文带 `questions`（例：`📮 需要你补充 · Issue #N\n仓库: …\n\n<questions>`）；`feishu_open_id` 为空 / 未配本人 bot → 跳过（与 notify 一致，`issues_center.py:247-257`）。
6. **非终态**：不调用 `update_task_status` 终态化；task 保持 `running`（服务端持有执行流）——与 sweeper 回收规则需对齐（见 §4.2）。
7. 返回 `{"ok": true, ..., "changed": true}`。

> 明确不做：不触发 ChatAgent / 不执行 opencode / 不注入用户个人 token 做 GitCode 写操作（沿用 notify/`_run_notify` 的 §十 原则，`issues_center.py:461-467`）；飞书仅作静态卡片通知，不产生 LLM 回复。

### 3.3 状态机衔接

- 进入后 pipeline `stage` **不推进**（保持当前 vod/issue 阶段），等待开发者输入。
- 开发者回复路径不变：
  - `POST /{issue_id}/clarify`（`issues.py:907-913`）→ `awaiting_input → processing`，并同步发 GitCode 评论 + 写会话；
  - 或 webhook `issue_comment` → `_STAGE_EVENT_AGENT_MAP`（`event_pipeline.py:545-555`）在 stage∈{vod,issue,…} 时路由回 vod/issue → 新一轮处理 → 再次由 Agent 显式决定是否继续等待。
- 状态迁移合法性仍由 `_TASK_STATUS_FLOWS`（`issues_center.py:530-535`）约束：`active→awaiting_input`、`processing→awaiting_input` 合法；`awaiting_input→processing/cancelled` 合法。

### 3.4 关键词判定退役（直接删除）

- **移除** `_detect_awaiting_input`（`event_pipeline.py:535-539`）及其调用点（`event_pipeline.py:2980-2995`），不再在 agent 完成后按文本猜意图。
- 进入「待澄清」**唯一契约路径** = Agent 显式调用本接口；不留任何兜底，避免双路径抢改状态产生抖动/误判。
- 存量能力由技能升级承接：vod-cli / issue-cli 的 SKILL 增加对应步骤（§3.5）。

### 3.5 CLI / 契约 / 文档

- `openapi.py`：注册 `tasks_awaiting-input` operation + 加入 `_SESSION_ONLY_OPERATIONS`。
- grape-cli（独立仓库 `grape-dev/grape-cli`）：`tasks_awaiting-input` 进契约后**自动生成** `grape tasks awaiting-input --questions "..."`（命令映射到 `/api/tasks/awaiting-input`，`grape refresh` 刷新可用）。
- `help/api/01-tasks.md`：补充接口文档。
- 技能仓库 `grape-dev/grape-skills`：`vod-cli` / `issue-cli` 的 SKILL.md 增加“若因缺少用户输入无法继续，调用 `grape tasks awaiting-input` 声明待澄清”。

---

## 4. 各模块改动

| 模块 | 改动 | 说明 |
|------|------|------|
| `src/api/issues_center.py` | 新增 `TaskAwaitingInputBody` + `awaiting_input` 迁移逻辑（复用 `_ensure_task_conversation`/`add_message`）+ 复用 `_run_notify` 静态飞书卡片 | 领域逻辑 |
| `src/api/routes/tasks.py` | 新增 `POST /awaiting-input` handler | 端点 |
| `src/api/openapi.py` | 加 operation + `_SESSION_ONLY_OPERATIONS` | 契约 |
| `src/api/event_pipeline.py` | **移除** `_detect_awaiting_input` 定义与调用点（`event_pipeline.py:535-539` / `2980-2995`），退役不留兜底 | 退役 |
| `src/api/routes/issues.py` | 无（clarify 复用现有逻辑） | — |
| `src/runtime/cleanup.py` | sweeper 回收规则与「awaiting 中保持 running」对齐（见 §4.2） | 防误回收 |
| `tests/test_tasks_awaiting.py`（新增） | 见 §5 | 测试 |
| `help/api/01-tasks.md` / 技能仓库 | 文档 + SKILL 更新 | 同步 |

### 4.1 pipeline stage 推进约束

进入 awaiting 时**不改 stage**。已有约束：none。需在 handler 内为「非 last-node」显式确认不推进（当前 `complete_task` 的 last-node→pending_review 逻辑仅在 task completed 时触发，awaiting 不是 completed，天然不触发——无需额外改动，但需在文档中固化语义）。

### 4.2 与 sweeper 回收规则对齐

进入 awaiting 时 task 保持 `running`（服务端持有流）。需复核 `sweep_stuck_agent_tasks_once`（`cleanup.py:383-467`）：
- 「非终态 + `stream_ended=1 ∧ self_reported=1` → 回收」规则：awaiting 场景若双信号已齐（Agent 显式 complete 后才进入等待，或 flow 中断），会被判死——**结论：等待中若双信号齐，回收为 failed 属合理**（执行已结束）；若双信号未齐（流仍持有），现有年龄类规则（queued 3h / running 6h / 死 runtime 30min）保护，不会误伤新鲜任务。
- 无需改动，但需在 awaiting handler 文档注明该边界，避免后续误改。

---

## 5. 测试

| 用例 | 断言 |
|------|------|
| UT-01 vod/issue 会话 token 调用 awaiting → issue `awaiting_input`，返回 `changed=true` | 状态迁移 + 出参 |
| UT-02 非 vod/issue 会话 token（coding/qa）→ 403 | 作用域拒绝 |
| UT-03 `BOT_GITCODE_TOKEN` / `grape_` / JWT 调用 → 403 | 仅会话通道 |
| UT-04 task 已 completed/failed/cancelled → `changed=false` 幂等，不改状态 | 终态幂等 |
| UT-05 已 awaiting_input（或 completed）再调 → `changed=false` | 幂等 |
| UT-06 questions 写入会话 → `GET /{issue_id}/clarifications` 可读到 | 澄清上下文展示 |
| UT-07 进入 awaiting 时向本人飞书 bot 发卡片（含 questions）；无 open_id / 无本人 bot → 跳过不报错 | 飞书触达 |
| UT-08 关键词判定退役：含「澄清」字样的完成输出**不再**自动改状态 | 退役验证 |
| 回归 | `tasks complete` 双信号收敛、`issues clarify` awaiting→processing 不变、`tasks notify` 分配流程不变 |

---

## 6. 兼容性 / 上线要求

1. **技能仓库必须随本特性同步升级**：关键词判定已直接退役，存量 vod/issue flow 中「需要用户输入才能继续」的场景**只有显式调用接口才会进入待澄清**。故上线该接口时须**同时**发布 `grape-dev/grape-skills` 技能更新（vod-cli / issue-cli 的 SKILL 加「调用 `grape tasks awaiting-input`」步骤），否则此期间相关任务会停在 processing 而非待澄清——该风险为直接退役的既定代价（确定性优先于宽松）。
2. **执行路径一致性**：显式接口对 direct 与 worker 模式天然一致（都走服务端 HTTP），一并消除 1.2-3 的不一致问题。
3. **前端**：`awaiting_input` 看板列 + `clarifications` 接口已存在，UI 无需新增。

---

## 7. 验收对照

| 项 | 结果 |
|----|------|
| Agent 可显式声明进入待澄清（含问题上下文） | ✅ 3.2 |
| 迁移原子、可观测（changed 字段，无静默失败） | ✅ 3.1/3.2 |
| 与 `tasks complete` 同鉴权/幂等骨架，仅会话通道 | ✅ 3.1 |
| 澄清后经 clarify 回到 processing 的既有闭环不变 | ✅ 3.3 |
| 关键词判定**直接退役**（不留兜底），进入待澄清唯一契约路径 | ✅ 3.4 |
| 进入待澄清即飞书卡片触达用户（含 questions，本人 bot） | ✅ 3.2-5 / UT-07 |
| worker/direct 执行模式行为一致 | ✅ 6-2 |
| 技能仓库随上线同步升级（vod-cli/issue-cli 显式调用） | ✅ 3.5 / 6-1 |