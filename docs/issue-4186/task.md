# Issue #4186 任务拆解（task）

- [x] 1. 定位 worker 侧路径注入点与物化点（agent.py `_execute_task` 3494/3540、`_materialize_worker_skill`）
- [x] 2. 新增同源化规整 helper：`_resolve_prompt_skill_root` / `_prompt_referenced_skill_roots` / `_normalize_prompt_skill_path`
- [x] 3. 在 `_execute_task` 物化后接线 `_normalize_prompt_skill_path`
- [x] 4. 单测 `tests/runtime/test_skill_prompt_path_4186.py`：路由名 ≠ agent 类型（反馈 1304 形态）、DEVOPS_SKILLS_DIR 优先、degraded 标注 + 回退路径、同源幂等、异名回退、空入参 noop（8 用例）
- [x] 5. 回归 #564/#3663 既有场景（test_skill_materialize / test_os_adapter / 887 / 1635 / 1070 / fallback_logging / hermes / codex）
- [x] 6. 文档：spec.md / plan.md / task.md
- [x] 7. QA 复审修复（PR #2091 评论 203319775）：异名官方检查器 fallback 按 task.skill_fallback.skill_name 重写为可读路径（含 layout 行）；same directory as 裸 str.replace 改带边界正则（防 /a/.skills 误伤 /a/.skills2）；异常分支补显式 degraded 标注；Notice 通知单一出口（已有 fallback/degraded Notice 不重复追加）；DEVOPS_SKILLS_DIR 读取去重 + _auth 更名 _authoritative_root（新增 3 用例，共 11）
