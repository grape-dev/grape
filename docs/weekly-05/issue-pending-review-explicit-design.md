# 方案：pending_review 语义刷新 —— Agent 显式设置阶段审核点（对标 awaiting-input）
# 「等用户」双态：awaiting_input（索要输入/澄清） × pending_review（交付审核点/待拍板）

> 状态：技术方案（2026-10-06）｜目标分支：weekly-05-lyz
> 关联：
> 1. `issue.status='pending_review'` 现仅由**流水线末节点**完成（`complete_task` last-node → `set_issue_stage(pending_review)`，
>    `service.py:1668-1689`）或 bot 接管 / owner 手动进入；退出仅用户 `confirm-done` / cancel / owner 手动。
> 2. 目标：与 `awaiting-input`（`issue-explicit-awaiting-input-api-design.md`）对称——**任意 devops agent**（vod/issue/coding/qa/sre）
>    在**任意 stage** 主动将 issue 设为 `pending_review`（阶段性审核点），并**由 Agent 判定用户是否通过**后显式退出。

---

## 1. 现状与问题

| 维度 | `awaiting_input`（已显式化） | `pending_review`（现状·末节点专用） |
|---|---|---|
| 进入 | Agent 显式 `issues awaiting-input`，任意 vod/issue 阶段 | 仅**末节点**完成自动进入；bot 接管；owner 手动 |
| 语义 | “我要你的输入/澄清，没干完” | “整条需求请你拍板收尾” |
| 退出 | 用户 `clarify` / 平台评论自动恢复 / owner | 仅用户 `confirm-done`→completed / cancel / owner |
| 与 stage 耦合 | 解耦（不改 stage） | 强耦合（stage=pending_review） |

**问题**：
1. 中途的**审核/确认点**无处表达——VOD 想让用户确认需求一致性、ISSUE 想让用户确认设计方案、QA 想让用户确认是否合入代码……现都只能闷头跑完到末节点，或绕道 `awaiting_input`。
2. 退出只认“用户点 confirm-done”，**不是 Agent 判**；Agent 无法根据用户评论内容决定“通过了继续 / 打回改”。
3. 语义上 `pending_review` 本应是“交付审核点等用户”，实际被缩窄成“整条需求收尾”。

---

## 2. 目标

- **进入显式化**：任 devops agent（session_only）可显式 `POST /api/issues/{issue_id}/pending-review`，携带「审核点 + 引用」，原子迁移
  `active/processing/awaiting_input → pending_review`（返回 `changed`，不静默失败）；**不改 stage**（阶段留当前）。
- **退出 Agent 判**：`review-pass`（通过→继续 `processing` 或合入 `done`）/ `review-reject`（打回 `processing`/`awaiting_input`），均显式幂等、记录用户回复证据。
- **与等待恢复对齐**：平台非 bot `issue_comment` 自动把 `awaiting_input` 与 `pending_review` 都恢复为 `processing`（Agent 重读反馈后自己判定），泛化 `_resume_issue_from_awaiting` → `_resume_issue_waiting`。
- **状态机解耦**：`pending_review` 加入 stage→status 同步豁免名单（同 `awaiting_input`），不被 stray 阶段事件冲掉。
- 保留用户级最高权威：`confirm-done` / `cancel` / owner 手动 PUT 语义不变。

---

## 3. 方案设计

### 3.1 语义定位（“等用户”双态）

```
awaiting_input  : "我需要你提供输入/澄清"（agent 不产出，等输入）
pending_review  : "我交付这个决策点，请你审核/拍板"（agent 产出到可审，等确认）
两者都是「等用户」的停留态；进入均显式（Agent）、退出均支持平台评论自动恢复 + Agent 判定。
```

### 3.2 进入端点 `POST /api/issues/{issue_id}/pending-review`

| 项 | 值 |
|----|----|
| 认证 | `session_only`（仅 `tk_`；`grape_`/JWT/`BOT_GITCODE_TOKEN` → 403），入 `_SESSION_ONLY_OPERATIONS` |
| 鉴权 | 会话 `(repo_key, agent_type)`：`issue.repo_key == token.repo_key` 且 `agent_type ∈ {vod, issue, coding, qa, sre}`（**任意 devops agent**） |
| 请求体 | `{ "review_points": "...", "references": "...(可选，如 PR#/设计文档)" }` |
| 迁移 | `UPDATE issue SET status='pending_review' WHERE id=? AND status IN ('active','processing','awaiting_input')` → `changed=rowcount` |
| 出参 | `{"ok":true, "issue_id":…, "status":"pending_review", "changed":true}` |

- **不改 stage**（阶段保持当前 vod/issue/coding/qa/sre）；状态由 issue 卡片承载。
- 审核点写入任务会话（agent 消息，`GET clarifications` 分享据点）+ **飞书静态卡片触达用户**（同 awaiting，`_run_pending_review_notify`；缺 open_id/本人 bot 跳过，不触发 ChatAgent/LLM）。
- 与任务终态正交（同 awaiting，C 单信号）：不终态化、不钉 running。

### 3.3 退出端点（Agent 判定）

| 端点 | 动作 | 目标 |
|---|---|---|
| `POST /api/issues/{issue_id}/review-pass` | `{"target":"processing"\|"done", "evidence":"引用用户评论id/文本"}` | processing →（继续流水线）或 done → `stage=done,status=completed` |
| `POST /api/issues/{issue_id}/review-reject` | `{"target":"processing"\|"awaiting_input", "reason":"打回原因"}` | 打回重做 / 转澄清 |

- 幂等：`UPDATE … WHERE status='pending_review'`;`changed` 可观测。
- `review-pass→done` 仅建议在“最终合入决策”（如 QA 确认 merge）时使用，且要求 Agent 显式断言。
- 均记录 `evidence/reason`（用户评论引用）至日志/会话，防误读无据。

### 3.4 状态机衔接（status × stage 解耦）

- `pending_review` 加入 `session_store.set_issue_stage` 的**豁免名单**（`session_store.py:169`，现为
  `NOT IN ('completed','cancelled','awaiting_input')` → 增加 `'pending_review'`）：阶段事件不再冲掉待审核状态。
- 状态迁移合法性仍由 `_TASK_STATUS_FLOWS` 扩展：`active/processing/awaiting_input → pending_review` 合法；
  `pending_review → processing/completed/cancelled` 合法。
- 既有 `complete_task` 末节点 → `set_issue_stage(pending_review)` 路径**保留**（与新显式接口并存，语义统一为“审核点”）；
  末节点审核通过：Agent `review-pass target=done` → completed（等价用户 confirm-done，但由 Agent 判定）。

### 3.5 平台评论自动恢复（泛化）

把 `_resume_issue_from_awaiting` 泛化为 `_resume_issue_waiting(repo_key, issue_number)`：
```
UPDATE issue SET status='processing' WHERE repo_key=? AND issue_number=? AND status IN ('awaiting_input','pending_review')
```
webhook `issue_comment`（非 bot，bot 已由 run_pipeline self-loop 跳过）命中即恢复 → Agent 重新跑一轮 → 自己调
`review-pass` / `review-reject` / `issues clarify` 续接。移除旧的 awaiting-only 恢复，统一走 `_resume_issue_waiting`。

### 3.6 权限边界与用户权威

- Agent 仅能设/退 `pending_review` 及其可迁移状态；**不能越过 `cancelled`（用户）、不能直接置 `completed` 除非走 review-pass target=done**；
- 用户 `confirm-done`（user_only）与 `cancel` 仍是最高权威，Agent 判定无法覆盖用户已确认/已取消。

---

## 4. 各模块改动

| 模块 | 改动 |
|---|---|
| `src/api/issues_center.py` | `TaskPendingReviewBody` + `set_issue_pending_review()`（原子迁移+写会话）；`_resume_issue_from_awaiting` 泛化 `_resume_issue_waiting`（awaiting+pending_review）；`review_pass`/`review_reject` 领域逻辑（幂等+stage/status 处理） |
| `src/api/routes/issues.py` | 新增 `POST /{issue_id}/pending-review`、`/{issue_id}/review-pass`、`/{issue_id}/review-reject`（session_only + 体内强制） |
| `src/api/session_store.py` | stage→status 豁免名单增加 `pending_review` |
| `src/api/event_pipeline.py` | issue_comment 恢复改为调 `_resume_issue_waiting` |
| `src/api/openapi.py` / `tests/test_openapi.py` | 注册 `issues_pending-review`、`issues_review-pass`、`issues_review-reject` 进 `_SESSION_ONLY_OPERATIONS` |
| 测试 | 见 §5 |
| 文档 | `issue-status-lifecycle.md`、`11-issues.md`、设计同步 |

---

## 5. 测试

| 用例 | 断言 |
|---|---|
| UT-01 各 devops agent（vod/issue/coding/qa/sre）设 pending_review → `changed=true`，状态迁移 | 任意阶段可设 |
| UT-02 非 devops agent / repo 不匹配 / 非 session → 403 | 鉴权 |
| UT-03 已 completed/cancelled/pending_review 重复设 → `changed=false` | 幂等 |
| UT-04 review_points 写入会话可读（clarifications 复用） | 审核点上下文 |
| UT-05 `_resume_issue_waiting`：awaiting_input 与 pending_review → processing；’其他不改 | 恢复泛化 |
| UT-06 review-pass target=processing → processing；target=done → completed+stage=done | Agent 判定通过 |
| UT-07 review-reject → processing / awaiting_input | 打回 |
| UT-08 webhook issue_comment（非 bot）同时恢复 awaiting 与 pending_review | §3.5 |
| 回归 | awaiting-input、clarify、confirm-done/cancel、末节点 pending_review 不变 |

---

## 6. 兼容性 / 上线要求

1. 新增端点与现有 `confirm-done`/`cancel`/owner 手动并存，**向后兼容**；末节点路径不动。
2. `pending_review` 豁免 stage 同步后，靠「平台评论自动恢复 + Agent 显式退出」闭环；若 Agent 忘记退出，卡片停留待审核 → 由恢复/人工兜底（sweeper 不写 status，如需可加“长期待审核提醒”）。
3. CLI（独立仓库）：`grape issues pending-review --issue_id N --json '{"review_points":"…"}'`、`grape issues review-pass/review-reject` 由 operation_id 自动生成，`grape refresh` 可用。
4. 前端：`pending_review` 看板列已存在，详情展示审核点（复用会话/评论区），无破坏性；可选“回复审核”按钮调 `clarify`。

---

## 7. 验收对照

| 项 | 结果 |
|---|---|
| 任意 devops agent 可在任意阶段显式设 pending_review（含审核点） | ✅ 3.2 |
| **飞书静态卡片触达“待审核”用户**（同 awaiting，本人 bot） | ✅ 3.2 / UT-009 |
| 退出由 Agent 判定（pass/reject，显式幂等 + 证据） | ✅ 3.3 |
| 平台评论自动恢复统一覆盖 awaiting_input 与 pending_review | ✅ 3.5 |
| status 与 stage 解耦，pending_review 不被阶段事件冲掉 | ✅ 3.4 |
| 用户级 confirm-done/cancel/owner 权威保留 | ✅ 3.6 |
| 既有末节点 pending_review / awaiting-input / clarify 不回归 | ✅ 6-1 / §5 |