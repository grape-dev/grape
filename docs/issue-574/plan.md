# Implementation Plan: Issue #574 — Skill 展示改为 display_name

## Overview
事件信息卡片 Skill 字段从裸 Skill ID 改为场景选择时的显示名称（display_name）。后端事件详情与 Autopilot 任务详情补充 display_name 字段，前端优先展示 display_name，为空回退 Skill ID。

## Architecture Decisions
1. **后端补字段，前端回退**：名称来源由后端 skill 表查询（`is_active=1 AND scope='official'`，与 scenes.py 同款条件）保证与场景选择处一致；空值交由前端回退 Skill ID，后端不返回哨兵。
2. **Autopilot 后端也补 display_name**：autopilot.js 详情页为懒加载技能列表，前端无法自算 display_name，故在 `_row_to_task` 统一补充（list/get 同源）。
3. **最小改动**：不新增公共 helper 模块，直接在 events.py 内联查询（单条查询，热路径成本低）；autopilot.py 使用同款内联查询。

## Task List

### Phase 1: 后端字段
- [ ] Task 1: events.py get_event_detail 补充 skill_display_name
- [ ] Task 2: autopilot.py _row_to_task 补充 display_name

### Checkpoint: 后端字段
- [ ] 新增测试通过（事件详情返回 skill_display_name；autopilot 任务返回 display_name）

### Phase 2: 前端渲染
- [ ] Task 3: events.js:358 优先 skill_display_name 回退 skill
- [ ] Task 4: autopilot.js 两处卡片优先 display_name 回退 skill_name

### Checkpoint: 前端渲染
- [ ] JS 语法校验通过、现有测试不回归

### Phase 3: 收尾
- [ ] Task 5: 全量回归测试 + pre-pr 检查 + 建 PR

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| skill 表 display_name 为空 | Low | 前端回退 Skill ID |
| scenes.py 条件与 events 查询不一致 | Low | 复用同款 SQL 条件（is_active=1 AND scope='official'） |

## Open Questions
- 无