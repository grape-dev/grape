# 方案：换人 / bot 接管不再把需求置为已完成

> 状态：技术方案（2026-10-09）｜分支：weekly-06-lyz
> 动机：需求「确认完成」应只由用户侧（`confirm-done` / 关闭 Issue）或 Agent 侧（`review-pass done` + 末节点兜底）判定。当前**换人（reassigned）与 bot 接管（bot_takeover）**两条路径会把 `issue.status` 置为 `completed`——责任转移 ≠ 需求完成，导致需求被误标为已完成。
> 目标：换人/接管只做**责任转移 / 任务延续**，**不触碰需求的完成状态**。
> 关联：`docs/weekly-05/review-pass-last-node-force-done-design.md`（完成条件的闭合路径）。

---

## 1. 现状与问题（代码核对 2026-10-09）

### 1.1 两条错误注入「完成」的路径

| 函数 | 位置 | 现状 |
|------|------|------|
| `_close_other_assignee_tasks`（换人） | issues_center.py:350 | `UPDATE issue SET status='completed', completed_reason='reassigned' WHERE ... AND status IN ('active','processing') AND assignee<>?` —— 换人把需求置完整完成 |
| `_close_issue_tasks_bot_takeover`（bot 接管） | issues_center.py:363 | `stage='done'` 时 `UPDATE issue SET status='completed', completed_reason='bot_takeover'` —— 接管把需求置完成 |

其它「完成」注入（**保留，不属于本方案范围**）：
- 用户显式 `confirm-done`（issues.py:596）、用户关 Issue（`close_issue_tasks`，webhook，issues_center.py:300）、Agent `review-pass done`（issues_center.py:474）、末节点归一（#2266）。

### 1.2 语义错位

- **换人**：`notify_task`（tasks.py:228）在通知新 assignee 前调 `_close_other_assignee_tasks`，意图是"关闭旧责任人的任务"，但落到 `issue.status='completed'` 即把**整条需求**标完成——与「继续由新负责人处理」直接矛盾。
- **bot 接管**：`notify_task` 中 assignee=bot 分支（tasks.py:216-220）调 `_close_issue_tasks_bot_takeover`，意图是"bot 自己接管继续跑"，`stage='done'` 分支却把需求标完成——同上矛盾。

### 1.3 现状单行模型说明（影响判定）

Phase C3 后 `issue.(repo_key, issue_number)` 为 **UNIQUE 单行**（换人动作先 `_close_other_assignee_tasks` 再更新 assignee），因此 `_close_other_assignee_tasks` 的 `assignee<>keep` 在**多数情况不命中任何行**——是历史多行模型的遗留；真正的 bug 在 bot 接管 `stage='done'→completed` 分支（单行下稳定命中）。

---

## 2. 方案设计

### 2.1 换人 `_close_other_assignee_tasks`

**移除** `UPDATE ... status='completed'` 的副作用，改为只做记账/降级为 no-op（不触碰 `issue.status`）：

```python
def _close_other_assignee_tasks(repo_key: str, issue_number: int, keep_assignee: str) -> int:
    """换人：不再把需求标完成（reassigned 语义错位）。
    单行模型下 assignee<>keep 基本不命中——保留为 no-op 记账，责任转移由
    _ensure_assignee_task_card 更新 assignee 承担。"""
    return 0
```

- 调用点（tasks.py:228）保留（幂等、无副作用）；`_ensure_assignee_task_card` 照常把 assignee 刷成新负责人；
- 需求 `status` 保持 `active/processing` 不变 → 流水线继续由新 assignee 处理。

### 2.2 bot 接管 `_close_issue_tasks_bot_takeover`

**移除 `stage == 'done' → completed` 分支**；接管语义 = bot 继续处理，绝不完结需求：
- `stage == 'done'`（本已终态）→ **不改变**（保持现状：已完成的保持 completed；绝不再翻回处理，也不重复标完成）→ 直接 return；
- `stage == 'pending_review'` → 保持 pending_review（不变）；
- 无 running agent 且活跃 → `pending_review`（等待开发者确认，不变）；
- 有 running agent → 不动（不变）。

即：**删除 completed 分支，`stage=='done'` 分支改为早退不改状态**。

### 2.3 完成条件的闭合

调整后「确认完成」只剩三条权威路径：
1. 用户 `confirm-done`（任意阶段）；
2. Agent `review-pass target=done`（pending_review 或末节点放宽）；
3. 用户关闭 Issue 且 `stage='done'`（webhook）。

换人/接管不再产生 `completed`；`reassigned`/`bot_takeover` 的 `completed_reason` 也不再写入。

---

## 3. 各模块改动

| 模块 | 改动 |
|------|------|
| `src/api/issues_center.py` | `_close_other_assignee_tasks` 降级为 no-op；`_close_issue_tasks_bot_takeover` 删除 `stage=='done'→completed` 分支（改为早退不改状态） |
| `tests/` | 见 §5 |
| 文档 | （可选）`task-lifecycle-design.md` / `issue-status-lifecycle.md` 同步「换人/接管不再完成」语义 |

---

## 4. 影响面 / 边界

| 项 | 说明 |
|----|------|
| 需求中心 | 换人/接管后需求仍显示「进行中」，不再误进「完成」列 |
| sweeper workdir 清理 | 依赖 `stage='done'`；本方案不新增 `stage=done`，无副作用 |
| `confirm-done` / `review-pass` / webhook 关闭 | 不受影响（保持权威完成路径） |
| bot 接管已完成需求 | notify_task 不建任务、不改状态（保持已完成），防重复处理 |
| 多任务父任务 | 不受影响（complete_task 另一规则） |
| `_close_other_assignee_tasks` 返回值 | 恒 0（调用方文案 `closed=N` 变为 0，可接受） |

---

## 5. 验证

| 用例 | 断言 |
|------|------|
| UT-01 换人（assignee A→B，处理中需求）→ 需求 `status` 仍 `active/processing`，非 completed | 换人不完成 |
| UT-02 bot 接管 + `stage='done'` + `status='completed'` → 调用后 status 仍 completed（早退不改） | 接管不翻回 |
| UT-03 bot 接管 + `stage='done'` + `status='processing'`（异常态）→ 不置 completed | 接管不完成 |
| UT-04 bot 接管 + 处理中（无 running）→ 仍 pending_review（原语义保留） | 兼容 |
| 回归 | `confirm-done`、`review-pass`、webhook `issue_closed` 的完成路径不受影响 |

---

## 6. 落档修订说明

- 范围严格限定「换人 / bot 接管」两条 notify 侧路径；不触碰用户/Agent 的权威完成判定。
- 依据单行模型（UNIQUE repo+issue）说明 `_close_other_assignee_tasks` 多数为 no-op；bot 接管 `stage='done'` 分支是实际命中点。
- 若后续有任务级（agent_tasks/chat_conversations）的「关闭旧负责人执行任务」诉求，应作用于**任务/会话**而非需求卡片状态，另行设计。