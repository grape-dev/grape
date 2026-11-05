# 需求文档（Spec 对齐）：Issue #1071

> 来源：Issue #1071 + Issue Agent 已发布 Spec（Issue 评论）。本文件为编码前的需求锚定，与 Spec 一致，不引入新范围。

## 目标
修复 pending_self_report 超时兜底（Issue #537 引入）误杀「执行中会重启主控」的部署类长任务：

- 部署类任务（grape 打包+生产部署全流程约 40-50 分钟）按设计在执行中重启主控；
- 重启瞬间 `terminate_active_agents` 把 REMOTE 任务置 `pending_self_report`，`pending_since` 记在重启时刻；
- 兜底扫描 `_sweep_pending_self_report_timeouts` 按 `pending_since > PENDING_SELF_REPORT_TIMEOUT_SECONDS`（默认 1800s）判 `timeout_failed`，窗口实际退化为「主控重启后 30 分钟」，剩余工序经常超 30 分钟，必然误杀；
- worker 真实完成上报因任务已终态被幂等拒收，无法自愈，状态与实际结果相反。

## 范围（In Scope，Spec 三条实现路径）
1. `_sweep_pending_self_report_timeouts` 判死前增加「活跃流豁免」：查询 task_messages 最新一条写入时间，`now - latest_write < 600s` → 跳过本次判死，下轮再评估；无记录/查询异常回退原判死逻辑。
2. `PENDING_SELF_REPORT_TIMEOUT_SECONDS` 默认值 1800 → 5400（保持 env 可配置）。
3. worker 完成上报路径：上报结果为成功但任务已 `timeout_failed` 终态且执行流证据齐全（task_messages 有真实执行产出）时，记录告警日志（含 task_id、时间线），不翻转终态。

## Out of Scope（Spec 明确不改）
- 终态幂等拒收机制本身（终态不可翻转；结构化产出覆盖例外路径保留）。
- task_messages 之外的执行流存储重构。
- 其他超时兜底机制（非 pending_self_report 类）。

## 关键设计决策（Spec 已定）
| 决策 | 选择 | 理由 |
|------|------|------|
| 活跃流判据 | A. task_messages 最新写入 | 复用现有数据，改动最小（report_messages 显式写 UTC ISO created_at，与 parse_dt 同源） |
| 豁免窗口 | 600s | 与调度扫描周期（60s）匹配，避免无限豁免 |
| 阈值放宽 | 5400s | 覆盖重启后剩余工序时长；600s 活跃豁免 + 5400s 上限双保险 |

## 验收标准（Spec Acceptance Criteria）
- [ ] 部署类长任务（>30 分钟且中途重启主控）跑完后状态为 completed，不再 timeout_failed 误判
- [ ] 模拟 worker 失联（执行流停止写入）场景，pending 任务仍按超时回收为 timeout_failed
- [ ] worker 成功上报遇终态拒收时产生告警日志（含 task_id）
- [ ] 普通任务正常完成/失败路径回归通过
- [ ] 所有现有测试通过 + 新增测试覆盖新逻辑

## 风险与缓解（Spec）
| 风险 | 缓解 |
|------|------|
| 活跃流查询增加扫描开销 | 仅对超时判死候选任务查询，量小 |
| 豁免过宽导致真失联回收延迟 | 600s 活跃窗口 + 5400s 硬上限双保险 |
| task_messages 数据缺失 | 无记录时回退原判死逻辑 |

## 测试 seam（编码前声明）
- seam = 公开状态机行为：`_sweep_pending_self_report_timeouts()` 返回值 + `agent_tasks.status`/`events`/`autopilot_executions` 落库终态，与 tests/test_issue_537_pending_self_report.py 既有 seam 一致。
- 活跃流输入 seam = `task_messages` 表种子行（真实 DB 行为，不 mock 内部）。
- 告警日志 seam = `caplog` 捕获 `devops-bot` logger warning（真实日志路径）。
