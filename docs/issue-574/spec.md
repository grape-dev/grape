# Spec: Issue #574 — 事件信息中 Skill 展示改为场景选择时的显示名称

> 需求文档（基于 Issue Agent 已发布 Spec 落地，2026-10-30 | 目标分支: weekly-09）

## Objective
- **要构建什么**：将事件信息卡片中 Skill 字段从裸 Skill ID（如 `vod-cli-crossrepo`）改为场景选择时的显示名称（display_name），覆盖 vod/issue/coding/qa/sre 五类事件
- **为什么要构建**：当前展示 Skill ID 对外部用户可读性差，无法直观对应技能用途
- **用户是谁**：通过飞书/Web 查看事件信息的用户
- **成功标准**：五类事件卡片 Skill 展示为易感知显示名称，名称来源与场景选择处一致

## Scope

### In Scope
- 后端 `src/api/routes/events.py` `get_event_detail` 查询 skill 表补充 `skill_display_name` 字段返回（复用 `scenes.py` 中 `SELECT skill_name, display_name FROM skill WHERE is_active=1 AND scope='official'` 的同款映射逻辑）
- 前端 `src/api/static/js/pages/events.js:358` 优先使用 `data.skill_display_name`，回退 `data.skill`（再回退 agent）
- Autopilot 后端 `src/api/routes/autopilot.py` `_row_to_task` 补充 `display_name` 字段（skill 表同款查询）
- Autopilot 前端 `src/api/static/js/pages/autopilot.js` 两处任务信息卡片（1462/1577）优先使用 `display_name` 回退 `skill_name`
- 五类事件（vod/issue/coding/qa/sre）统一覆盖（事件详情页五类共用同一渲染函数，改一处即覆盖）

### Out of Scope
- 场景选择页面本身的显示名称逻辑（已存在，复用即可）
- skill 表 `display_name` 字段的创建（已存在）

## Technical Approach

### 方案概述
后端查询 skill 表 `display_name` 补充返回，前端优先使用 `display_name` 回退 Skill ID。

### 实现路径
1. **后端 events.py**：`get_event_detail` 结果 dict 补充 `skill_display_name`；查 skill 表 `SELECT display_name FROM skill WHERE skill_name=? AND is_active=1 AND scope='official'`，取不到/为空时返回空串，由前端回退
2. **后端 autopilot.py**：`_row_to_task` 返回 dict 补充 `display_name`（skill 表同款查询），list/get 均复用
3. **前端 events.js:358**：`data.skill_display_name || data.skill || agent`
4. **前端 autopilot.js:1462/1577**：`(task.display_name || task.skill_name || '—')`（详情页与编辑刷新共用同一表达式）

### 关键设计决策
| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 映射逻辑复用 | 新写映射 / 复用 scenes.py 同款查询 | 复用同款查询 | 同名条件（is_active=1 AND scope='official'）保证名称来源与场景选择处一致 |
| 回退策略 | 报错 / 回退 Skill ID | 回退 Skill ID | display_name 为空时回退，满足验收标准 3 |
| Autopilot display_name 来源 | 前端查技能表 / 后端返回 | 后端返回 | 详情页加载技能列表是懒加载，前端无可靠来源 |

## Affected Modules
| 模块 | 改动类型 | 说明 |
|------|---------|------|
| `src/api/routes/events.py` | 修改 | get_event_detail 补充 skill_display_name 字段 |
| `src/api/routes/autopilot.py` | 修改 | _row_to_task 补充 display_name 字段 |
| `src/api/static/js/pages/events.js` | 修改 | 优先使用 skill_display_name 渲染 |
| `src/api/static/js/pages/autopilot.js` | 修改 | 优先使用 display_name 回退 skill_name |

## Acceptance Criteria
- [ ] 五类事件（vod/issue/coding/qa/sre）卡片中 Skill 展示为 display_name 而非裸 Skill ID
- [ ] 名称来源与场景选择处的显示名一致
- [ ] display_name 为空时回退展示 Skill ID
- [ ] 所有现有测试通过
- [ ] 新增测试覆盖 skill_display_name 返回与回退逻辑

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| skill 表部分记录 display_name 为空 | Low | 回退策略：display_name 为空时回退 Skill ID |
| 五类事件渲染差异 | Low | events.js 事件详情页五类共用同一页面，修改一处即覆盖 |

## Open Questions
- 无