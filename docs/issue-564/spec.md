# Spec: Issue #564 — DEVOPS_SKILLS_DIR 注入路径技能包未物化

> 基线分支：weekly-08-mys（本场景工作基线，权威注入值）。来源：Issue #564 issue agent Spec Document 评论（已复核确认）。

## Objective

- 任务派发/场景流水线在注入 `DEVOPS_SKILLS_DIR` 路径变量的同时，将技能包文件（SKILL.md + references/ 等）真实物化到该目录；注入时校验完整性，缺失时回退官方技能包并在事件中标注降级，不静默空注入。
- 事件 prompt 中给出可解析的技能内容位置（DEVOPS_SKILLS_DIR 绝对路径 + 技能文件相对布局）。
- 物化写入具备原子性（tmp 目录 + 原子 rename），中途失败不留半写 `.skills` 目录。

## 现状问题

1. direct 端 `_materialize_direct_workdir`（src/api/event_pipeline.py）：技能物化失败仅告警跳过，`{{SKILL_DIR}}` 仍被替换为空目录路径 → Agent 拿到空 `.skills` 目录（Issue #564 用户反馈）。
2. worker 端（src/runtime/agent.py）：OBS zip 直接 `extractall` 到 `.skills/`，非原子；解包中断会留半写目录；zip 缺 SKILL.md/references 不校验。
3. 旧格式 `skill_files` 文本逐文件写盘，同样非原子、无完整性校验。
4. prompt 只给 SKILL.md 单文件路径，references 布局与 DEVOPS_SKILLS_DIR 环境变量未说明。

## 方案

详见 plan.md。核心：新增 `src/runtime/skill_materialize.py` 公共模块（原子物化 + 完整性校验 + 官方包回退 + 降级状态返回），event_pipeline / scheduler（direct 物化）与 worker agent.py（zip / skill_files 物化）统一接入；prompt_builder 技能段落补充技能内容位置说明。

## 验收标准

见 task.md 检查清单；测试覆盖物化成功 / 回退 / 降级 / 原子性四类路径。
