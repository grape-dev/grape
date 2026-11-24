# Issue #4260 方案设计

## 方案概述

四层叠加，任一层生效即可阻断 `by-id/undefined`：

| 层 | 位置 | 行为 |
|----|------|------|
| 路由守卫（首选） | `app.js` `nav()` skill 分支 | 缺参 → 提示 + `nav('skills')` + return，不发请求 |
| hash 生成 | `app.js` `_pushHistory` | skill 缺参改写为 `#skills` |
| API 哨兵 | `skills.py` `get_skill_detail_by_id` / `/by-id/` | 三哨兵 → 400 `invalid_skill_id` |
| 错误文案 | `app.js` skill catch | 400 参数错误文案，404 保持「技能不存在」 |

## 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 缺参处理位置 | 路由层守卫 + API 兜底 | 守卫不发请求直接归零指标；API 兜底旧缓存/直连 |
| API 拦截范围 | 仅 `''/'undefined'/'null'` | 保住 #630「非数字按名称解析、未知名称 404」契约 |
| 半截 hash | 改写 `#skills` | 刷新/复制链接不再复现无参 skill 状态 |
| 400 文案 | 新增 `common.invalid_param` | 语义错位正是本单用户可见缺陷 |

## 实现细节

- `_isMissingSkillParam(param)`：显式比较三哨兵 + 空串/undefined/null，
  合法 id、名称深链（含含点、连字符、大写）原样透传。
- 守卫置于 `nav()` 记录来源视图与 `_pushHistory` 之前；`popstate` 内
  `_replaying=true` 时不重复入栈，URL 旧值也不会再触发取数。
- `/by-id/` 空路径路由 `include_in_schema=False`，直接 400，避免落到
  路由未命中 404。

## 回归

- 后端：`tests/mgmt/test_issue4260_skill_id_sentinel.py`（哨兵 8 变体 + 名称 404 + 空路径 400）。
- 前端：`tests/js/test_issue_4260_skill_missing_param.js`（守卫顺序 / hash 改写 /
  400 分流 / i18n / 缓存版本）。
- 既有：`tests/runtime/test_issue_630_skill_gate.py` 名称深链语义、`tests/api/test_openapi.py`。