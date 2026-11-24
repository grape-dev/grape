# Plan: Issue #3621 — 建单端点 `trigger_pipeline` 开关（只建单不触发）

## 方案概述
在 `TaskCreateBody` / 建单入口透传 `trigger_pipeline`（默认 `true`），把 `create_task_via_issue` 中
「写 `stage=vod` + 同步构造原生 `issue_opened` payload 调 `run_pipeline`」这一步改为受该开关控制；
payload 构造与同步触发抽取为共用 helper `_issue_opened_pipeline_trigger`，供建单直发与挂单后续首次
触发（`trigger_pending_issue_pipeline`）复用同一事件路径。

## 改动点
| 文件 | 改动 |
|------|------|
| `src/domains/runtime/issues_center.py` | `TaskCreateBody` 增 `trigger_pipeline: bool = True`；`create_task_via_issue` 建单成功后按开关分支（true → `stage=vod` + 同步触发；false → `stage=created`，跳过触发）；抽取 `_issue_opened_pipeline_trigger`（原生 payload + `_grape_internal_trigger` + `asyncio.to_thread(run_pipeline)` 语义保持）；新增 `trigger_pending_issue_pipeline`（幂等首次触发：stage 非 `created` → 409，已有事件 → 409，触发后 stage 切 `vod`）；返回体增 `trigger_pipeline` 回显与挂单文案 |
| `src/domains/runtime/routes/issues.py` | `issues_create`（POST /api/issues/{owner}/{repo}）显式解析 `trigger_pipeline` 布尔（缺省 true，字符串 "false" 不当 true）；新增 `POST /api/issues/{issue_id}/pipeline/trigger`（user_only）触发挂单首次入链；端点 description/docstring 登记 |
| `docs/openapi/11-issues.md` | 建单 body/出参登记 `trigger_pipeline`；stage 取值补充 `created` 挂单态 |
| `tests/runtime/test_issue_3621_trigger_pipeline_switch.py` | 新增：默认/显式 true 回归、false 挂单路径、后续触发入链 + 幂等 409、非 created 拒绝 409 |
| `tests/runtime/test_event_active_guard.py` | 方案 D 原生 payload 源码断言随逻辑迁移指向 `_issue_opened_pipeline_trigger`（断言不变） |

## 关键设计决策
| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 参数默认值 | true / false | `true` | 兼容所有现有调用方，零回归 |
| 不触发时的状态标记 | 空 stage / `stage=created` / 新 status | `stage=created` | 可辨识且不影响现有 `status=active` 查询口径（`_STAGE_TO_TASK_STATUS` 无 `created` 键 → 任务状态不被同步改写） |
| 后续触发事件 | 复用 `issue_opened` / 新事件类型 | 复用 `issue_opened` | 与直接建单路径同一条代码路径（共用 helper），事件形态一致，减少分支 |
| 幂等保护 | 触发前校验 | stage≠created → 409；已有任一事件 → 409；stage 校验/事件计数/created→vod 认领同锁+同一事务（条件 UPDATE CAS） | 不重复入链，并发仅一个成功 |

## 风险与缓解
- `stage=created` 被巡检误判：巡检按 pipeline_order 推进目标，`created` 不在序 → 不推进（预期跳过）。
- 后续触发重复产生事件：`trigger_pending_issue_pipeline` 双重幂等校验（stage + 事件存在性）+ 同锁事务 + CAS 认领。
- 触发先落 `stage=vod` 后 run_pipeline 失败导致卡死：失败/未落事件时回滚 `stage=created`（无事件），可重试。
