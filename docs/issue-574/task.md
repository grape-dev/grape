# Task List: Issue #574

## Task 1: events.py get_event_detail 补充 skill_display_name
- **Acceptance**: GET /api/events/detail/{seq} 返回 `skill_display_name`；skill 表有 display_name 时返回之，无/空时返回空串；不存在/异常不影响既有字段
- **Verify**: `python3 -m pytest tests/test_issue_574_skill_display_name.py -q`
- **Files**: `src/api/routes/events.py`, `tests/test_issue_574_skill_display_name.py`
- **Scope**: S

## Task 2: autopilot.py _row_to_task 补充 display_name
- **Acceptance**: GET /api/autopilot/tasks/{id} 与列表返回 `display_name`；skill 表有 display_name 时返回，无/空时返回空串
- **Verify**: `python3 -m pytest tests/test_issue_574_skill_display_name.py -q`
- **Files**: `src/api/routes/autopilot.py`, `tests/test_issue_574_skill_display_name.py`
- **Scope**: S

## Task 3: events.js:358 优先 skill_display_name 回退 skill
- **Acceptance**: Skill 行渲染 `data.skill_display_name || data.skill || agent`
- **Verify**: JS 源码断言测试 + node 语法检查
- **Files**: `src/api/static/js/pages/events.js`, `tests/test_issue_574_skill_display_name.py`
- **Scope**: XS

## Task 4: autopilot.js 两处卡片优先 display_name 回退 skill_name
- **Acceptance**: 详情页与编辑刷新两处 Skill 行渲染优先 `display_name` 回退 `skill_name`
- **Verify**: JS 源码断言测试 + node 语法检查
- **Files**: `src/api/static/js/pages/autopilot.js`, `tests/test_issue_574_skill_display_name.py`
- **Scope**: XS

## Task 5: 回归 + pre-pr 检查 + 建 PR
- **Acceptance**: 相关测试全绿；pre-pr 检查通过；PR 创建并通知 QA
- **Verify**: 测试套件 + pre-pr-release-check
- **Files**: —
- **Scope**: S