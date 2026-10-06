# Issue 状态生命周期说明（需求中心视角）

> 状态：技术方案整理（2026-10-06）｜目标分支：weekly-05-lyz
> 关联：`issue.status`（需求中心可读状态，6 值）与 `issue.stage`（pipeline 阶段，vod/issue/coding/qa/sre/done/pending_review）**解耦并存**；
> 本文件只讲 `status` 生命周期：进入/退出每个状态的前提与触发来源。
> 增补：**awaiting_input → processing 新增「平台评论自动恢复」**——非 bot 用户 `issue_comment` 即进入 clarify 处理
> （`issue-explicit-awaiting-input-api-design.md` §3.6，`_resume_issue_from_awaiting`）。

---

## 1. 总览

```
                    ┌────────────┐  用户 msg/流水线推进   ┌────────────┐
                    │   active   │ ─────────────────────▶ │  processing │
                    └────────────┘                       └────────────┘
                        │  ▲                                  │  │
                 owner指定│  │resume                           │  │Agent 显式等待(clarify回复/平台评论/owner)
                        ▼  │                                  ▼  │
                    ┌────────────┐   Agent 显式 awaiting-input   ┌────────────┐
                    │  cancelled │  ◀─────────────────────────▶  │ await_input│
                    └────────────┘   owner 手动(PUT status)     └────────────┘
                        │
                    （终态，仅 resume 回 active）
                    ┌────────────┐   末节点完成(stage→pending_review)   ┌──────────────┐
                    │  completed │  ◀─ confirm-done / stage=done ──    │ pending_review│
                    └────────────┘                                      └──────────────┘
```

状态全集（`_TASK_STATUS_FLOWS`，`issues_center.py:530-535`）：`active / processing / awaiting_input / pending_review / completed / cancelled`。

### 通用规则

| 规则 | 位置 |
|---|---|
| **stage→status 自动同步**：stage∈{vod,issue,coding,qa,sre}→processing、pending_review→pending_review、done→completed；同步**不覆盖 `awaiting_input/completed/cancelled`** | `session_store.py:156-170`，`set_issue_stage` |
| **手动改状态**：仅需求 owner（`user_id` 匹配或 0）可调 `PUT /api/issues/{issue_id}/status`，迁移必须合法（`_TASK_STATUS_FLOWS`） | `issues.py:445-477` |
| **Agent 显式接口**：`POST /api/issues/{issue_id}/awaiting-input`（发起待澄清）、`issues clarify`（回复待澄清） | `issues.py:878` / `990-991` |
| **取消可从任意非终态进入**；**completed 不可取消、cancelled 不可 confirm-done、resume 仅限 cancelled** | `issues.py:608-613, 638-641, 692-693` |
| **需求中心雷达 `is_running`** 只看 `agent_tasks` 非终态行，与 `issue.status` 解耦 | `issues_center.py:1187-1205` |

---

## 2. 各状态：进入前提 / 退出前提

### 2.1 `active` — 待处理（前端列：待处理）

**进入前提**
| 触发 | 说明 | 位置 |
|---|---|---|
| 建档 | Issue webhook/首次事件 → `ensure_issue_task` / `_ensure_assignee_task_card` 新建卡片，默认 `status='active'` | `issues_center.py:94-104,169-175` / `session_store.py:98` |
| 恢复（resume） | 仅 `cancelled` 可恢复回 `active`（清空 completed_reason/completed_at） | `issues_center.py:624-626` / `issues.py:694-696` |

**退出前提**
| 去向 | 触发 | 位置 |
|---|---|---|
| → processing | 用户发消息开任务会话；stage 被推进到某执行阶段（set_issue_stage 同步）；owner 手动 PUT | `session_manager.py:625-629` / `session_store.py:161-170` / `issues.py:445-477` |
| → awaiting_input | Agent 显式 awaiting-input；owner 手动 PUT | `issues.py:878` |
| → cancelled | 用户 cancel（任意非终态）；owner 手动 PUT | `issues.py:672` |

### 2.2 `processing` — 处理中（前端列：进行中）

**进入前提**
| 触发 | 位置 |
|---|---|
| stage 推进到 vod/issue/coding/qa/sre（webhook 路由、A2A 交棒、重派）→ 同步为 processing | `session_store.py:156-170` |
| 用户在该需求会话发消息（active→processing） | `session_manager.py:625-629` |
| 恢复已取消（resume，cancelled→processing） | `issues.py:694-696` |
| 用户回复澄清后（awaiting_input→processing，`issues clarify`） | `issues.py:990-991` / `issues_center._resume_issue_from_awaiting` |
| **webhook `issue_comment`（非 bot 用户评论）自动恢复**（awaiting_input→processing，§3.6） | `event_pipeline.py` run_pipeline(issue_comment) → `_resume_issue_from_awaiting` |
| owner 手动 PUT | `issues.py:445-477` |

**退出前提**
| 去向 | 触发 | 位置 |
|---|---|---|
| → awaiting_input | Agent 显式 `issues awaiting-input`（原子迁移，`changed` 可观测）；owner 手动 PUT | `issues.py:878` |
| → pending_review | 流水线末节点完成 → stage=pending_review → 同步；bot 接管且无 running；owner 手动 PUT | `session_store.py:159,168-170` / `issues_center.py:353-359` / `service.py:1668-1689` |
| → completed | stage=done 同步；换人 reassign（旧负责人置 completed）；owner 手动 PUT | `session_store.py:159` / `issues_center.py:316` |
| → cancelled | 用户 cancel / owner 手动 PUT | `issues.py:672` |
| → active | owner 手动 PUT（回退） | `issues.py:445-477` |

### 2.3 `awaiting_input` — 待澄清（前端列：待澄清）

**进入前提**
| 触发 | 位置 |
|---|---|
| **Agent 显式接口 `issues awaiting-input`**：仅 `active/processing` → awaiting_input（原子、`changed=false` 幂等守护）；会话 token 作用域 + agent∈{vod,issue} | `issues.py:878` |
| owner 手动 PUT（`active/processing → awaiting_input` 合法） | `issues.py:445-477` / `issues_center.py:530-535` |

> ⚠️ 关键词自动判定已**退役**（`_detect_awaiting_input` 删除）；进入待澄清的唯一契约路径是 Agent 显式接口。

**退出前提**
| 去向 | 触发 | 位置 |
|---|---|---|
| → processing | 用户 `issues clarify` 回复（发 GitCode 评论 + 写会话 + awaiting→processing） | `issues.py:990-991` / `_resume_issue_from_awaiting` |
| → processing | **webhook `issue_comment`（非 bot 用户评论）自动恢复**：平台原生评论即进入 clarify 处理；bot 自评被上游 self-loop 跳过不触发 | `event_pipeline.py` run_pipeline(issue_comment) → `_resume_issue_from_awaiting`（幂等） |
| → cancelled | 用户 cancel / owner 手动 PUT | `issues.py:672` |
| 不可被 | stage 同步覆盖（awaiting_input 在保护名单内） | `session_store.py:169` |

### 2.4 `pending_review` — 待审核（前端列：待审核）

**进入前提**
| 触发 | 位置 |
|---|---|
| 流水线**末节点**完成 → `set_issue_stage(pending_review)` → status 同步 | `service.py:1668-1689`（complete_task last-node）+ `session_store.py:159` |
| bot 接管：stage=pending_review 或无 running agent → 置 pending_review | `issues_center.py:293-294,353-359` |
| owner 手动 PUT（processing→pending_review） | `issues.py:445-477` |

**退出前提**
| 去向 | 触发 | 位置 |
|---|---|---|
| → completed | 用户 `confirm-done`（status=completed + stage=done，终态幂等）；owner 手动 PUT | `issues.py:595-623` |
| → cancelled | 用户 cancel / owner 手动 PUT | `issues.py:672` |

### 2.5 `completed` — 已完成（前端列：已完成，终态）

**进入前提**
| 触发 | 位置 |
|---|---|
| 用户 `confirm-done`（任意阶段可确认，cancelled 除外）→ completed + stage=done | `issues.py:595-623` |
| stage=done 自动同步 | `session_store.py:159` |
| 换人 reassign（同 issue 旧负责人任务）→ completed（reason=reassigned） | `issues_center.py:316` |
| bot 接管且 stage=done → completed | `issues_center.py:347` |
| webhook issue 关闭（issue_closed）→ completed | `issues_center.py:285` |
| owner 手动 PUT（processing/pending_review→completed） | `issues.py:445-477` |

**退出前提**：**终态**——无合法流转（`_TASK_STATUS_FLOWS['completed']=()`，`issues.py:467`）；且已完成不可取消。

### 2.6 `cancelled` — 已取消（前端列：已取消）

**进入前提**
| 触发 | 位置 |
|---|---|
| 用户 `cancel`：**任意阶段**可取消，终止相关 agent 任务 + 事件 → cancelled | `issues.py:626-676` |
| owner 手动 PUT（任意→cancelled） | `issues.py:445-477` |
| 换人 reassign 之外不产生 cancelled（取消以用户/owner 为主） | — |

**退出前提**
| 去向 | 触发 | 位置 |
|---|---|---|
| → active | 用户 `resume`（仅 cancelled 可恢复，回取消前 stage 继续） | `issues.py:694-696` |
| （→ processing 的路径：resume 实际置 processing 后由 stage 继续推进；文档以 active↔processing 双向为准） | — |
| 不可被 | confirm-done（cancelled 报 400） | `issues.py:612-613` |

---

## 3. 触发来源归类（便于排查）

| 来源 | 动作 | 影响的 status |
|---|---|---|
| **Pipeline 自动** | 事件路由/A2A/重派推进 stage → `set_issue_stage` 同步 | processing / pending_review / completed（不覆盖 awaiting_input/completed/cancelled） |
| **Agent 显式** | `issues awaiting-input`（发起待澄清） | active/processing → awaiting_input |
| **用户** | `confirm-done` / `cancel` / `resume` / `PUT status` / `clarify` 回复 / **平台 `issue_comment`（非 bot）自动恢复** / 会话发消息开任务 | 见 §2 各表 |
| **系统** | bot 接管、换人 reassign、issue 关闭 webhook | pending_review / completed |
| **Sweeper** | 只兜 `agent_tasks`/events 悬空（置 failed），**不写 issue.status** | —（不影响 status） |

## 4. 保护与边界

1. **stage→status 不覆盖** `awaiting_input/completed/cancelled`（`session_store.py:169`）——待澄清/终态不会因阶段推进被冲掉。
2. **`awaiting_input` 只能由「Agent 显式接口」或 owner 手动进入**（关键词判定已退役）。
3. **cancel 是“任意阶段可进”的高优先动作**（completed 除外）；**resume 仅限 cancelled**；**confirm-done 拒绝 cancelled**。
4. **`awaiting_input` 的退出**：除 `clarify` 端点 / owner 手动外，**平台 `issue_comment`（非 bot 用户评论）会自动恢复 processing**（`_resume_issue_from_awaiting`，幂等；bot 自评被 run_pipeline 上游 self-loop 跳过，绝不触发）。
5. 需求中心雷达（进行中扫描动画）与 `issue.status` 解耦——反映的是 `agent_tasks` 是否还有非终态执行，清卡以回收 agent_tasks 为准。