# Plan: Issue #607 — 技能物化失败 FALLBACK.md 兜底实现方案

## 方案概述

在两端技能物化失败路径补统一兜底函数 `write_skill_fallback()`：物化结果为空/失败/
异常时向 `skills_root/<skill>/FALLBACK.md` 写降级指引并 `log.warning` 结构化留痕，
保证注入目录非空且可读。不改物化失败根因。

## 兜底函数（单一实现，两端复用）

落点：`src/runtime/skill_materialize.py`（worker 与 direct 已共同 import 的零 DB
模块，避免新模块循环导入风险——与 Spec「建议放公共位置供两端复用」一致）。

`write_skill_fallback(skills_root, skill_name, reason, stage) -> bool`：

- 目录已有 SKILL.md → 跳过（成功路径不受影响）
- 写 FALLBACK.md（技能名 / stage / 原因 / 本地目录 / `grape skills detail --skill <name>`
  权威取回指引，首行声明 NOT a SKILL.md 防误读）
- 成功 → `log.warning("[skill] fallback file written: skill=... stage=... dir=... reason=...")`
- 自身失败仅 `log.error` 静默降级（不影响主任务）

## 接入点

### worker 端（src/runtime/agent.py）

- `_execute_task` 技能物化 try 块提取为模块级 `_materialize_worker_skill(task, env,
  prompt, skill_dir)`（逐字对齐原逻辑，仅为可单测），调用处一次替换
- 三处失败分支接入兜底：
  1. zip 物化失败且官方包回退 degraded → `stage="zip_b64"`
  2. skill_files 物化失败/为空（含 skill_obs_url 直连+代拉均失败且 skill_files 为空
     的静默空注入场景，改为降级处理不再注入空 DEVOPS_SKILLS_DIR）→ `stage="skill_files"`
  3. 外层 except（`skill materialize failed`）→ `stage="exception"`

### direct 端（src/api/event_pipeline.py）

- `_materialize_direct_workdir`：`materialize_skill_with_fallback` 返回 degraded →
  `stage="direct"`；外层 except 同样接入

## 测试（tests/test_skill_materialize.py 增补）

- `write_skill_fallback` 单测：写入内容/跳过已有 SKILL.md/空名/结构化日志/坏路径静默
- worker 端：obs 失败+files 空 → FALLBACK 且不再空注入；空 zip → FALLBACK；
  成功路径不受影响；异常分支 → FALLBACK
- direct 端：degraded → ws/.skills/ 出现 FALLBACK.md；ok → 无 FALLBACK.md

## 风险

- 兜底写失败 → 自身 try/except 静默 + log.error
- 与 SKILL.md 同目录误读 → 首行声明非 SKILL.md；下发头口径一致
