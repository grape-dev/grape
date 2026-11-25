# Issue #3564 — 任务拆解

- [x] T1 `pipeline_config.get_scene_engine_binding` 解析/归一化 `engine_priorities`
- [x] T2 `engine_select.get_scene_engine_priorities` 输出任务级 JSON
- [x] T3 `agent_tasks.engine_priorities`：schema DDL + `register_migration("202611240016")`
- [x] T4 `enqueue_task` 落列（未显式传时按场景解析）
- [x] T5 `claim_by_tag` 优先级闸 + `claim guard [priority]` 决策日志 + 超时旁路
- [x] T6 场景保存校验 + 复制清洗孤儿优先级键
- [x] T7 前端引擎绑定弹窗优先级控件（高/中/低，zh/en）
- [x] T8 sqlite 内存库真实 `claim_by_tag` 验收用例（累计 35 条）+ 迁移/列清单校验

## QA 复审修复（2026-11-20）
- [x] Q1 `_create_direct_task` / `_create_failed_task` 补 `engine_priorities=""` 列值
      （修复 `_task_insert_row` ValueError 被 except 吞掉、direct/failed 行不落库）
- [x] Q2 `_claim_priority_gate` 判定限定任务 `pool_engine_ids` 与映射交集（空池=旧语义）
- [x] Q3 `_engine_claimable_now` 增加健康分闸（`health_scorer.is_healthy`，
      与 claim_by_tag 同尺子；判定异常保守视为不可接取）
- [x] Q4 测试补齐：enqueue 写入静态守卫、direct/failed 落库回归、健康分/池交集/
      空池旧语义用例、归一化 helper strict 与非 strict 用例（合计 24 条）

## QA rebase 复审修复（2026-11-23）
- [x] R1 迁移已改号 `202611240016`（基线占用旧号），同步更新 spec/plan/task 文档。
- [x] R2 实例 uid 绑定模式（D11 全 uid 提交、ids 清空）确认时同清 `engine_priorities`，
      避免后端 strict 校验 400；`applyDraftNodes` 逗号连接语句拆分为独立赋值。
- [x] R3 `dbv-engprio-hint` 与优先级控件同 `v-if`（仅实例模式显示）。
- [x] R4 测试补齐：前端 uid 分支清优先级静态守卫 + uid+priorities 保存侧 400 兜底
      契约（合计 35 条）。
- [x] J1 归一化收敛为 `normalize_engine_priorities(raw, allowed_ids, strict)` 单一 helper
      （pipeline_config 读侧 / routes 校验 / copy_scene 清洗 / service 任务侧）
- [x] J2 提取常量 `ENGINE_PRIORITY_MIN/MAX/DEFAULT`（1..9 / 默认 9，唯一真源）
- [x] J3 删除 `get_scene_engine_priorities` 未用参数 `issue_creator` 与函数内冗余 `import json`
