# Issue #980 Plan — 方案设计

## 方案概述
两处重复的 official-only 映射抽为共用解析层，按 skill_id 优先、(scope, skill_name) 次之、
user/repo 上下文、official 兜底的回退链解析；两路由统一调用。

## 实现步骤
1. `src/api/skill_display.py`：单名解析 + 批量映射（单次查询全部 active 技能，Python 侧按
   user(3) > repo(2) > official(1) 优先级选定）。
2. `scenes.py` with-pipeline：映射改用 `build_skill_display_map(repo_key, user_id)`；
   node 带 `skill_id`（#979 落地后）时走 `resolve_skill_display_name` 精确解析。
3. `dashboard.py`：agent 卡片 skill_display_name 同口径替换。
4. 单测 `tests/test_issue_980_skill_display_name.py`：解析层单元 + 两路由行为级 +
   源码契约（official-only 查询不再残留）。

## 兼容性
- 优先级与派发侧 `resolve_skill_id`（src/user/db.py）一致 → 展示名与实际绑定档位一致。
- DB 异常降级为空 map（调用方回退 skill_name），与旧 try/except 行为一致。
- node 无 skill_id 时按批量映射，无同名冲突时输出与旧逻辑逐字一致。
