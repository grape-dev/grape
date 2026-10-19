# Spec: Issue #369 — agent_tasks 判死未回写关联事件（事件侧零诊断信息）

> 来源：Issue #369 Spec（Issue 评论 id=194458783，issue agent 生成）| 目标分支：weekly-07-clean-mys

## 需求
任务判死（agent_tasks 置 failed）时，同步回写关联事件记录，使事件侧具备完整诊断信息
（result / exit_code / completed_at），排障不再只能靠任务表单侧推断。

## 现状（缺陷）
`src/runtime/service.py` 三处判死路径只调 `update_event_status(seq, "failed")`——仅写
status，事件 result/exit_code/completed_at 全空（实测事件 #13424）：
1. `_fail_task_pool_unreachable`（认领判死：绑定引擎全部物理不可达）
2. `_fail_task_pool_unhealthy`（健康分不足 120s 超时判死）
3. dispatched 超时回收 attempts≥50 判死（回收超限）

## 范围
- In：上述三处路径统一完整回写；全仓同类缺口补齐（executor 停机判死、recover-orphans 封顶判死）；幂等（双主 scp 同步两侧重复回写安全）。
- Out：事件表结构变更、前端展示改动、历史空字段事件批量回补。

## 验收
- [x] 三处判死路径置 failed 时事件 result/exit_code/completed_at 均回写
- [x] 幂等：事件已终态（completed/failed）不被覆盖，重复回写不冲突
- [x] 现有测试通过（基线既有失败与本改动无关），新增测试覆盖判死回写路径
