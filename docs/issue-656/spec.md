# Spec — Issue #656 末节点完成后 issue 不自动结单，长期停在 processing

> 来源：Issue Agent 已发布 Spec（评级 ready）｜目标分支：weekly-08-mys（后续同步 weekly-09）

## 目标

流水线末节点完成（next_step=done 或无下游节点）且无阻断时，服务端自动完成 issue 级收尾迁移：
- `issue.status → completed`（task_status）
- `issue.stage → done`
- `completed_at` 落库（#368 关联）
- 联动关闭 GitCode（及 GitHub 镜像）侧 Issue

保留 `grape issues confirm done`（confirm-done / admin-confirm-done / review-pass(done)）用于
pending_review 等显式需人工确认的场景。

## 根因

末节点完成回调只更新事件/任务状态：
- worker 路径 `src/runtime/service.py complete_task()`：末节点（next=='done'）完成后仅置
  `stage=pending_review`（Issue #51 语义），无 issue 级 completed 迁移；
- 本地执行路径 `src/api/event_pipeline.py`：仅对 sre 硬编码置 pending_review，其它末节点场景
  完全无收尾迁移 → issue 长期停在 processing，需人工 confirm done（#69/#86/#88/#89/#90 复现）。

关联 #422（阶段自动衔接缺失）、#368（事件收尾无 completed_at——事件侧 completed_at 已由
update_event_complete/update_event_status 防御性落库，本 Issue 补 issue 行侧 completed_at）。

## 范围

In：
- 末节点完成回调（worker `complete_task` + 本地 exec 两条路径）统一接入共享自动结单函数；
- 自动结单条件：当前 stage==完成节点 ∧ 该节点为末节点 ∧ 全部节点最新事件 completed（无 running）∧ 无 failed 阻断 ∧ issue.status 非 pending_review（显式确认场景不自动结单）;
- VCS Issue 自动关闭（bot token，降级不阻断，幂等：已 closed 跳过）;
- 有 failed 阻断时回退 pending_review（人工确认路径）;
- CLI/文档明确标注人工确认触发条件（help/api/11-issues.md + 路由 description）。

Out：
- pending_review 人工确认流程重构；看板/统计前端改造；存量卡 processing issue 批量修复。

## 验收标准

- 末节点完成后无需人工 confirm done 即 stage=done、task_status=completed、completed_at 落库、GitCode Issue 自动关闭；
- 存在 failed/running 阻断、或 issue 处 pending_review 时不自动结单（保留人工确认语义）;
- `grape issues confirm done` 文档/help 明确「仅显式需确认场景使用」；
- 新增测试覆盖自动结单迁移逻辑（含阻断回退与幂等）。
