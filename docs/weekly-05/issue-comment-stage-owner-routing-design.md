# 方案：issue_comment 按当前 stage owner 路由（评论直达阶段负责人，模糊交回 vod/issue）

> 状态：技术方案（2026-10）｜目标分支：weekly-05-lyz
> 关联：现 `_STAGE_EVENT_AGENT_MAP["issue_comment"]`（`event_pipeline.py:537`）把 `coding/qa/sre` 阶段评论一律回给 `issue`
> Agent（需求侧审视），导致评论绕路；目标改为「评论对应当前 stage 的 owner」，由 owner 判断自理或 A2A 回 vod/issue。

---

## 1. 现状与问题

```python
# 现状 issue_comment 矩阵
"issue_comment": {"vod":"vod","issue":"issue","coding":"issue","qa":"issue","sre":"issue","pending_review":"issue","done":""}
```

| 问题 | 说明 |
|---|---|
| 实现的阶段评论绕路 | stage=coding/qa/sre 的评论先给 issue Agent，再 A2A 派人 → 多一跳 |
| pending-review 收口绕路 | QA 设「待审核·是否合入」，用户回复“同意”→ 现回给 issue Agent 再 A2A QA，QA 才能 `review-pass` |
| 运行期评论被吞 | 若 owner 对同 issue 已有 running 事件，`dedup skip`（`event_pipeline.py:1542`）直接丢弃且无人接手 |

---

## 2. 目标

- **评论直达 owner**：`issue_comment` 按当前 `stage → 对应 owner agent` 路由（vod/issue/coding/qa/sre ↔ 同名 agent）。
- **模糊交回**：owner 收到评论后自行判断——属本阶段反馈直接处理；属需求/规格澄清 → A2A 交回 `vod/issue` 或转 `awaiting_input`（系统不做关键词猜测）。
- **不吞评论**：owner 正在 running 时，评论改为「排队等 owner 当前事件终态后再入队」，而非 dedup 丢弃。
- 前置闸口（bot 自评回声 / 系统回帖 / `/done /cancel /resume` / L1 过滤 / `_resume_issue_waiting`）保持不变。

---

## 3. 方案设计

### 3.1 新路由矩阵

```python
"issue_comment": {"vod":"vod","issue":"issue","coding":"coding","qa":"qa","sre":"sre",
                  "pending_review": <pending_review 的 owner_agent 或 stage owner>,
                  "done":""}   # cancelled/done 仍不分发
```

- `pending_review` 列：取 `owner_agent`（设审核点的 agent，通常即 stage owner，如 QA）；查询 `issue.owner_agent`，缺省回退同 stage。
- 其余前置逻辑不变：`_resolve_issue_stage_agent`（`event_pipeline.py:548`）表驱动 + `issue_opened 恒 vod` + cancelled/done 短路。

### 3.2 owner「自理 / 交回」规则（技能侧约定）

不改服务端判定，由收到评论的 owner agent 按其技能自决：

| 评论性质 | owner 动作 |
|---|---|
| 本阶段反馈（改代码/再审/重部署…） | 直接处理 |
| 需求/规格澄清（理解偏差、四要素缺失…） | `grape issues awaiting-input`（转待澄清）或 A2A 交回 `vod/issue` |
| 审核确认（pending_review 下） | 正常走 `review-pass/review-reject` |

→ 对应各 agent 技能补充说明（grape-skills 独立仓库，vod/issue/coding/qa/sre 的 SKILL/runbook）。

### 3.3 不吞评论：issue_comment 豁免 webhook 去重（复用 issue 级串行队列）

现 `dedup skip`（`event_pipeline.py:1542`）对「同 (issue, agent) 已有 running 事件」直接跳过 → 运行中插入的评论会丢失。
改为对 **issue_comment 豁免去重**：始终新建 `running` 事件 + `agent_tasks` 入队；同一 issue/agent 的执行由
`issue_agent_session` 串行化，当前事件终态后自然轮到该评论——等价“排队补波”，**无需新建队列表**。

- 实现：`run_pipeline` 去重条件排除 `issue_comment`（events 正常入库 + 分派）；重复安全由 bot 自评回声 / 系统回帖过滤保证（豁免不会导致循环触发）。
- 边界：同一评论若 webhook 重投会生成重复任务（罕见，容忍）；多次不同评论按序各建任务执行（不合并）。
- 死锁防护：owner 终态必然发生（C 单信号流结束即终态 / sweeper 兜底），评论任务不会永久挂起。

### 3.4 前置闸口不变

- bot 自评回声（`run_pipeline` 2.0）；`<!-- grape-auto-comment -->` 系统回帖；`/done /cancel /resume` 指令短路；
- **等用户双态自动恢复**（`_resume_issue_waiting`）：awaiting_input/pending_review → processing 后再按新矩阵路由给 stage owner；
- `_STAGE_EVENT_AGENT_MAP` 其余事件（issue_opened/updated、PR、note_hook_pr）不动。

### 3.5 与 pending-review / awaiting-input 协同

- QA 设 pending_review（stage=qa）→ 用户评论恢复 processing → 新矩阵路由给 **qa** → QA 读用户意见后 `review-pass/review-reject` 收口（无需 issue 中转）。
- vod/issue 设 awaiting_input → 用户评论恢复 → 仍由 vod/issue 接（矩阵原样）。

---

## 4. 各模块改动

| 模块 | 改动 |
|---|---|
| `src/api/event_pipeline.py` | `_STAGE_EVENT_AGENT_MAP["issue_comment"]` coding/qa/sre→自身、pending_review→owner；`dedup skip` 对 `issue_comment` **豁免去重**（走正常入库+入队，由 issue 串行执行） |
| `src/api/event_pipeline.py` | `_resolve_issue_stage_agent` 的 pending_review 分支取 `owner_agent`（缺省回落表值） |
| 测试 | §5 |
| 文档 | `11-issues.md`（issue 说明）、`issue-status-lifecycle.md`、设计同步 |
| `grape-skills`（独立仓库） | vod/issue/coding/qa/sre SKILL：`issue_comment` 到场后“自理或 A2A 回 vod/issue”说明 |

---

## 5. 测试

| 用例 | 断言 |
|---|---|
| UT-01 issue_comment stage=coding/qa/sre → 路由到同名 agent | 矩阵 |
| UT-02 stage=pending_review → 路由到 owner_agent（如 qa） | owner 收口 |
| UT-03 stage=done/cancelled → 仍不分发 | 回归 |
| UT-04 bot 自评、系统回帖、/done 指令 → 不走新路由 | 闸口回归 |
| UT-05 owner 正在 running 时的 comment → 不丢弃，排队；owner 终态后补波入队执行 | 护栏 |
| UT-06 排队期间多条评论 → 合并为最新一条（默认） | 防风暴 |
| 回归 | awaiting-input/pending-review 自动恢复、vod/issue 评论原样、note_hook_pr 不变 |

---

## 6. 兼容性 / 上线要求

1. 行为变化：coding/qa/sre 阶段评论由「issue Agent 审视」变为「owner 直接接手」——对存量关注点需在技能/文档说明（owner 会按 §3.2 交回规格类）。
2. `pending_review` 的 `owner_agent` 兜底：老数据可能缺 owner → 回退同 stage（qa）。
3. 排队任务靠 owner 终态触发，C 单信号下终态必然收敛；若 owner 卡死由 sweeper 兜底，排队记录不悬空。
4. CLI/契约无变化（不改 operation_id/协议）。

---

## 7. 验收对照

| 项 | 结果 |
|---|---|
| issue_comment 按 stage owner 直达（coding/qa/sre 不再绕 issue） | ✅ 3.1 |
| pending-review 回复由设点 agent 收口（如 QA review-pass/reject） | ✅ 3.1/3.5 |
| 需求/规格类评论由 owner A2A 回 vod/issue 或转 awaiting_input | ✅ 3.2（技能约定） |
| running 期间评论不吞（排队补波） | ✅ 3.3 |
| 前置闸口与双态恢复不变 | ✅ 3.4 |
| coding/qa/sre 阶段行为变化有文档/技能同步 | ✅ 6-1 |