# 方案：Issue「待澄清」状态显式化 —— Agent 自报 `issues awaiting-input` 接口
# 澄清三件套：awaiting-input（Agent 发起）/ clarify（用户回复）/ clarifications（历史）同聚 /api/issues/{issue_id}

> 状态：技术方案（2026-10-06 修订）｜目标分支：weekly-05-lyz
> 关联：
> 1. `issue.status='awaiting_input'` 当前由「输出关键词判定」隐式进入，判定与模型措辞强耦合、存在误判/漏判/路径不一致/静默失败四类不确定性 → 改为 Agent 显式契约调用。
> 2. 经 2026-10-06 评审：awaiting-input **只修改 issue 状态、与任务生命周期正交（C 单信号）**，应从 `/api/tasks/awaiting-input`
>    **迁移到 `/api/issues/{issue_id}/awaiting-input`**，与既有的 `issues clarify`（用户回复）、`issues clarifications`（历史）组成澄清三件套。

---

## 1. 现象与现状问题

### 1.1 现状进入「待澄清」的两条路径

| 路径 | 位置 | 说明 |
|------|------|------|
| **自动（关键词判定，主）** | `event_pipeline.py`（已退役） | vod/issue agent 当轮 `completed` 后 `_detect_awaiting_input(result_text)` 对关键词表子串匹配，命中 → 置 `awaiting_input` |
| 手动（owner 改状态） | `issues.py:445-477` | `PUT /api/issues/{issue_id}/status` 按 `_TASK_STATUS_FLOWS`（`issues_center.py:530-535`）合法迁移（active/processing → awaiting_input） |

### 1.2 自动判定的四类不确定性

1. **漏报**：Agent 用其它表达求输入（「你回 Yes 我就继续」「选 A 还是 B」）不命中关键词 → 卡片仍 `processing`，进度失真。
2. **误报**：输出「提到」澄清词但非要等输入（「本轮无需澄清」）→ 误置待澄清，卡住流水线。
3. **执行路径不一致**：检测只在 direct 路径跑，worker 路径不检测。
4. **静默失败**：`UPDATE … WHERE status IN ('active','processing')` 未命中行时 rowcount=0，无返回提示。

根因：**把「进度语义」寄托在模型口语措辞上**；且判定对象是 `result_text` 文本，本质在猜意图。

---

## 2. 修复目标

- **会话级显式接口**：Agent 主动声明「我需要用户输入」，消除上述四类不确定性。
- **放在 /api/issues/{issue_id}（澄清三件套，同组聚合）**：awaiting-input 与 `issues clarify` / `issues clarifications` 同属一个资源的澄清往还；下沉 issue 归属，消除「放 tasks 却只管 issue 状态」的错位。
- 接口**原子迁移** `active/processing → awaiting_input`，返回 `changed`（消除静默失败）。
- 携带**澄清问题上下文**（写入任务会话，`GET /api/issues/{issue_id}/clarifications` 直接展示）。
- **直接删除关键词自动判定**（不留兜底），唯一契约路径 = 显式接口。
- 与任务终态**正交**（C 单信号）：不终态化、不钉住 running；随 `finalize_by_stream_end` 正常收尾；待澄清由 issue 卡片承载。

---

## 3. 方案设计

### 3.1 端点契约

新增 `POST /api/issues/{issue_id}/awaiting-input`（原 `/api/tasks/awaiting-input` **迁移**过来）

| 项 | 值 |
|----|----|
| 认证 | `session_only`（进 `openapi._SESSION_ONLY_OPERATIONS`）：仅 `tk_` 会话 token |
| 鉴权 | 会话 token 携带 `(repo_key, agent_type)`；取 `{issue_id}` → 校验 `issue.repo_key == token.repo_key` 且 `agent_type ∈ {vod, issue}` |
| 资源定位 | `issue_id` = 需求卡片 id（`/api/issues` 语义），URL 直接给出，无需再解析 task |
| 拒绝 | `BOT_GITCODE_TOKEN` / `grape_` API Key / JWT → 403；非 vod/issue agent → 403；repo 作用域不匹配 → 403 |
| 信任模型 | **repo + agent 作用域**（与 `tasks/notify` 同源：会话 token 背书仓库与 agent 类型，不绑定具体任务）；强度低于“task→issue 强绑定”，但符合“Agent 执行期凭据操作本仓库需求”语义 |

请求体（`TaskAwaitingInputBody`）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `questions` | string | ✅ | 向开发者提出的澄清问题 / 需要补充的信息 |
| `reason` | string | 否 | 进入待澄清的原因（写日志/归档） |

出参：

```json
{"ok": true, "issue_id": 210, "status": "awaiting_input", "changed": true}
```

> `changed=false`：issue 已终态 / 已处于待澄清 → 幂等未迁移。

### 3.2 处理流程（`awaiting-input` handler，归属 issues 路由）

1. **鉴权 + 定位**：仅 `tk_` → 取 `user.repo_key` / `user.agent_type`；按 URL `{issue_id}` 取 issue，校验 `issue.repo_key == user.repo_key`，且 `agent_type ∈ {vod, issue}`；任一不满足 → 403。
2. **幂等边界**：issue 已 `completed/cancelled` → `changed=false`；已 `awaiting_input` → `changed=false`（重复声明幂等）。
3. **原子迁移**：
   ```sql
   UPDATE issue SET status='awaiting_input'
   WHERE id=? AND status IN ('active','processing')
   ```
   以 `rowcount` 决定 `changed`；未命中 → `changed=false` + warning（不再静默）。
4. **写入澄清上下文**：复用 `_ensure_task_conversation` / `add_message`，把 `questions` 作为 agent 消息写入任务会话（`GET /api/issues/{issue_id}/clarifications` 直接展示，`issues.py:789` 通道复用）；可选补 `events.agent_logs` 记录「Agent 显式进入待澄清」。
5. **飞书卡片通知**（awaiting 语义就是等用户输入，必须主动触达）：复用 `_run_notify` 静态通知机制（`issues_center.py:461-494`），以**用户本人 chat bot** 发卡片，正文带 `questions`；无 `feishu_open_id` / 无本人 bot → 跳过（与 notify 一致）。
6. **与任务终态正交（C 单信号）**：不终态化、不钉住 running；任务照常随引擎流结束由 `finalize_by_stream_end`（`stream-end-finalize-single-signal-design.md`）收尾；待澄清状态由 issue 卡片承载。
7. 返回 `{"ok": true, "issue_id": ..., "status": "awaiting_input", "changed": true}`。

> 明确不做：不触发 ChatAgent / 不执行 opencode / 不注入用户个人 token（§十，`issues_center.py:461-467`）；飞书仅静态卡片。

### 3.3 状态机衔接（澄清三件套）

| 方向 | 端点 | scope | 效果 |
|---|---|---|---|
| Agent **发起** | `POST /api/issues/{issue_id}/awaiting-input` | session_only | `active/processing → awaiting_input` + 记 questions |
| 用户**回复** | `POST /api/issues/{issue_id}/clarify`（既有 `issues.py:907-913`） | user_only | 发 GitCode 评论 + 写会话 + `awaiting_input → processing` |
| 查看**历史** | `GET /api/issues/{issue_id}/clarifications`（既有） | dual | 澄清消息 + 会话 |

- `awaiting_input` 与 pipeline `stage` 解耦：不改 stage；`complete_task` 随流结束推进 stage（如最后节点→pending_review）时，`session_store` 的 stage→status 同步守卫（`session_store.py:155-169`）**不覆盖 `awaiting_input`**，卡片保持待澄清。
- 用户回复后还可经 webhook `issue_comment` → `_STAGE_EVENT_AGENT_MAP` 路由回 vod/issue 重新处理。
- 迁移合法性由 `_TASK_STATUS_FLOWS` 约束。

### 3.4 关键词判定退役（直接删除）

- **移除** `_detect_awaiting_input` 定义与调用点，不再按文本猜意图。
- 进入「待澄清」**唯一契约路径** = `issues awaiting-input` 显式调用；不留兜底。
- 存量能力由技能升级承接（§3.5）。

### 3.5 CLI / 契约 / 文档

- `openapi.py`：注册 `issues_awaiting-input` operation + 加入 `_SESSION_ONLY_OPERATIONS`；**移除** `tasks_awaiting-input`。
- grape-cli（独立仓库）：进契约后**自动生成** `grape issues awaiting-input --issue_id {issue_id} --questions "..."`（命令映射 `/api/issues/{issue_id}/awaiting-input`，`grape refresh` 刷新可用）。
- 文档：`help/api/11-issues.md` 增补该节（与 clarify/clarifications 相邻列出）。
- 技能仓库：`vod-cli` / `issue-cli` 的 SKILL.md 改为「调用 `grape issues awaiting-input` 声明待澄清」。

---

## 4. 各模块改动

| 模块 | 改动 | 说明 |
|------|------|------|
| `src/api/routes/issues.py` | **新增** `POST /{issue_id}/awaiting-input` handler（session_only，`get_current_user_or_api_key` + 体内强制 session） | 端点迁移落点 |
| `src/api/routes/tasks.py` | **移除** `POST /awaiting-input` handler | 迁移清理 |
| `src/api/issues_center.py` | `TaskAwaitingInputBody` + `awaiting_input` 迁移逻辑（复用 `_ensure_task_conversation`/`add_message`）+ 复用 `_run_notify` 静态飞书 | 领域逻辑 |
| `src/api/openapi.py` | 注册 `issues_awaiting-input` + `_SESSION_ONLY_OPERATIONS`；删 `tasks_awaiting-input` | 契约 |
| `src/api/event_pipeline.py` | 移除 `_detect_awaiting_input`（已退役，随 C 落地） | 退役 |
| `src/runtime/cleanup.py` | 无新规则（C 单信号下 awaiting 不依赖 task running；残留由年龄规则兜底） | — |
| `tests/test_issues_awaiting.py`（由 test_tasks_awaiting.py 迁移） | 见 §5 | 测试 |
| `help/api/11-issues.md` / 01-tasks.md / 技能仓库 | 文档迁移 + SKILL 更新 | 同步 |

> 说明：issues 组将出现「写操作 user_only（clarify）与 session_only（awaiting-input）并存」，由 `x-token-scope` 逐个标注，读路径按 per-op scope 判断。

---

## 5. 测试

| 用例 | 断言 |
|------|------|
| UT-01 vod/issue 会话 token（repo 匹配）调 awaiting → issue `awaiting_input`，`changed=true` | 状态迁移 + 出参 |
| UT-02 会话 token 仓库 ≠ issue 仓库 → 403 | 仓库作用域 |
| UT-03 会话 `agent_type` 为 coding/qa → 403 | agent 范围 |
| UT-04 `BOT_GITCODE_TOKEN` / `grape_` / JWT 调用 → 403 | 仅会话通道 |
| UT-05 issue 已 completed/cancelled/awaiting_input → `changed=false` | 幂等 |
| UT-06 questions 写入会话 → `GET /{issue_id}/clarifications` 可读到 | 澄清上下文 |
| UT-07 进入 awaiting 向本人飞书 bot 发卡片（含 questions）；无 open_id/本人 bot → 跳过 | 飞书触达 |
| UT-08 关键词判定退役：含「澄清」的输出不再自动改状态 | 退役验证 |
| 回归 | `issues clarify` awaiting→processing 不变、`issues clarifications` 历史不变、`tasks notify/complete` 不变 |

---

## 6. 兼容性 / 上线要求

1. **接口迁移（破坏性改名）**：本特性已短期上线为 `POST /api/tasks/awaiting-input` / `grape tasks awaiting-input`；本次迁移到
   `POST /api/issues/{issue_id}/awaiting-input` / `grape issues awaiting-input`。**同一版本内直接改路径并删旧端点**，
   需同步发布：grape-cli（`grape refresh` 后命令变组）、技能仓库 SKILL（`grape issues awaiting-input`）。旧调用方在刷新契约后自动失效（404 原路径）。
2. **技能仓库必须随本特性同步升级**：关键词判定已直接退役，存量 vod/issue flow 中「需要用户输入才能继续」的场景**只有显式调用接口才会进入待澄清**；该风险为直接退役的既定代价（确定性优先于宽松）。
3. **执行路径一致性**：显式接口对 direct 与 worker 模式天然一致（都走服务端 HTTP），消除 1.2-3 的不一致。
4. **前端**：`awaiting_input` 看板列 + `clarifications` 接口已存在，UI 无需新增。

---

## 7. 验收对照

| 项 | 结果 |
|----|------|
| Agent 可显式声明进入待澄清（含问题上下文） | ✅ 3.2 |
| 归属 /api/issues/{issue_id}，与 clarify/clarifications 成三件套 | ✅ 3.1/3.3 |
| 迁移原子、可观测（changed，无静默失败） | ✅ 3.1/3.2 |
| 仅会话通道（tk_），repo+agent 作用域校验 | ✅ 3.1/3.2 |
| 澄清后经 clarify 回到 processing 闭环不变 | ✅ 3.3 |
| 关键词判定**直接退役**，唯一契约路径 | ✅ 3.4 |
| 进入待澄清即飞书卡片触达用户（本人 bot） | ✅ 3.2-5 / UT-07 |
| 与任务终态正交（C 单信号，不钉 running） | ✅ 3.2-6 |
| 旧 tasks 端点迁移清理、CLI/SKILL 同步 | ✅ 6-1 |