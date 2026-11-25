# Issue #3908 Plan — 用命名空间短路 + 回链键判据解耦两类 autopilot 任务

> 目标合入分支：`arch-weekly-11-ljt`

## 问题链路

1. `agent_tasks.event_seq = 900_000_000 + exec_id`，autopilot 不写 events 表。
2. `_pr_subscription_state()` 对任意非 0 seq 查 events，必然查空 → WARNING + `None`。
3. `_is_pr_subscription_task()` 把 `None` 视为 PR 订阅 → `reconcile_task_scene_env()` 保守跳过。
4. #3744 已在 reconcile 入口按来源短路，但 `_pr_subscription_state()` 本身仍可对合成 seq
   查表；且 seq 命名空间 + 真实 Issue 编号会被一并跳过，#1745 校正仍失效。

## 改动

| 文件 | 改动 |
|---|---|
| `src/kernel/issue_ident.py` | 新增常量与两个判据；复用 `SYNTHETIC_CARD_PREFIX` 构造 `autopilot-[a-z]+` 正则 |
| `src/domains/runtime/engine/scheduler.py` | `_AUTOPILOT_ISSUE_ID_PREFIX` / `_AUTOPILOT_EVENT_SEQ_BASE` 改为 kernel 常量别名 |
| `src/domains/runtime/pipeline/event_pipeline.py` | `_pr_subscription_state()` 命名空间短路；reconcile 按回链键/无绑定 seq 短路；`_is_autopilot_task()` 不再把 seq+真实 Issue 判 autopilot |
| `tests/runtime/test_issue_3908_autopilot_scene_env.py` | kernel 判据 + 4 情形回归 |

## 关键决策

- 判据下沉 kernel：`event_pipeline` 已被 `scheduler` import，反向 import 会成环。
- 两条判据解耦：seq 命名空间负责「不查表、不告警」；issue 形态负责「是否校正」。
- 跳过日志用 debug：设计内常态，不占运维 warning 通道；真实缺失 WARNING 文案不变。

## 风险

- 现网 seq 量级 9e4，远低于 9e8；若日后逼近再抬高常量即可。
- #3744 既有用例保持绿：`autopilot-a` / 空 issue + 合成 seq 仍短路且保留 debug 依据。