# Issue #2816 方案设计

## 变更点
| # | 文件 | 变更 |
|---|------|------|
| 1 | `src/api/a2a_terminal_guard.py` | 新增终态判定纯函数、VCS 解析（Issue 标签/载体 PR 标记行）、人工态落库、派发短路 |
| 2 | `src/domains/runtime/routes/webhook.py` | `/api/a2a` 在既有门禁前调用终态短路（force 可越过） |
| 3 | `src/api/terminal_disposition.py` | intersect 人工处置：指定新载体 / 改派 / 关闭 |
| 4 | `src/domains/runtime/routes/issues.py` | `POST /api/issues/{id}/terminal-disposition` 暴露人工处置入口 |
| 5 | `tests/runtime/test_issue_2816_terminal_guard.py` | 纯判定 + 短路 + 人工处置测试 |

## 判定设计
- 终态信号（任一命中）：Issue labels 含 `needs-human`；载体 PR 评论存在标记行
  `needs-human-terminal`（大小写不敏感，可携带 `CONFLICT_SCOPE=intersect`）。
- 载体 PR 号来源：payload 显式 `carrier_pr`/`pr_number` > 人工处置写入的 meta
  `terminal-disposition:carrier:{issue}` > Issue 标题/正文/评论中的 `载体 PR #N` /
  `carrier PR #N`。生效载体（显式或 meta）由 `_fetch_vcs_state` 回传给
  `evaluate_terminal_state`，保证「读取层 → 判定层」同一载体，避免只读不判的假阴性。
- 解析失败（无 token / 网络异常 / 结构不符）按未命中处理（保守放行），不改变既有行为。
- 标签读取与评论读取解耦：评论读取异常不丢弃已命中的权威标签。

## 人工越过（持久恢复）
- `new_carrier` / `reassign` 落 meta `terminal-disposition:override:{issue}`
  （action / carrier_pr / by / at），后续非 force 派发按载体维度豁免旧
  `needs-human` 标签；载体标记行仍按生效载体判定，新载体再次终态照常短路。
- `close` 清除该越过记录与载体 meta（`terminal-disposition:carrier:*`），
  避免单据重开后旧载体仍压过文本载体判定。

## 置人工态
- `issue.status -> awaiting_input`（既有 PAUSED 口径），`issue.stage -> awaiting_input`
  （`pipeline_constants.MANUAL_ISSUE_STAGE`），`completed_reason` 记
  `needs_human_terminal` / `needs_human_terminal_intersect`。
- `patrol.issue_terminal` 把 `awaiting_input` 视为终态，因此不会再判断链并补偿派发，
  实现「退出 coding 自动循环」。

## 排队 drain 复查
- 终态短路不只挂在 `POST /api/a2a` 门禁：排队事件随 drain 直接进入
  `run_a2a_dispatch`，故在该函数入口复用同一 guard（`force`/`reissue` 放行）。
- 命中后把已抢占为 running 的事件置 `cancelled` 并写 `event_skipped` 审计，
  避免巡检把永不执行的 running 事件当成停滞。

## VCS 读取预算
- 单次缓存 miss 最多读取 Issue 详情 + Issue 评论（≤2 页）+ 至多 2 个载体 PR 评论
  （各 ≤2 页），受 `A2A_TERMINAL_GUARD_BUDGET`（默认 20s）总预算约束，预算耗尽即
  使用已取到的部分数据判定，避免长尾同步阻塞 `POST /api/a2a`。

## 人工处置（intersect）
- 状态下限：`new_carrier` / `reassign` 仅对 `RESUMABLE_ISSUE_STATUSES`
  （active/processing/awaiting_input/pending_review）开放，已收敛单据
  （completed/cancelled 等）返回 **409**，不得改写 stage 或复活派发；结束已收敛
  单据请用 `close`。
- 恢复未命中（并发改写/状态已收敛）时 `resume_issue_from_manual` 返回 `False`
  且**不写 stage**（`set_issue_stage` 会同步任务状态为 processing），路由据此返回
  409 并放弃 force 派发。
- 恢复迁移与 `_resume_issue_waiting` 共用 `_resume_issue_rows` 单源，仅 stage
  策略不同（人工处置显式指定 target stage；审核态恢复回落 `owner_agent`）。
- `new_carrier`：写 meta 新载体号 + 清终态信号缓存 + 恢复 `processing`/`stage=coding`，
  再以 `force=true` 派发 coding。
- `reassign`：恢复 `processing` 后以 `force=true` 派发到指定 agent。
- `close`：复用 `confirm_issue_done` 收尾（含 awaiting_input/pending_review，
  无真实变更时不推进 stage）。

## 风险
| 风险 | 缓解 |
|------|------|
| 终态误判阻断正常单 | 只认权威标签/标记行；解析失败放行；`force=true` 人工可越过 |
| 派发路径同步读 VCS 变慢 | 60s 结果缓存；无 token 直接放行 |
| 与 B1/B11 出口并存 | 人工处置后显式落 `awaiting_input`/终态，巡检按终态短路 |