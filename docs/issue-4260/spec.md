# Issue #4260 实现 Spec：技能详情深链缺 id 时不再请求 by-id/undefined

> 来源：Issue #4260 已发布 Spec（评论 id=203442321）| 目标分支 `arch-weekly-11-ljt` | 里程碑 arch-weekly-11

## 目标

技能视图（`#skill`）在缺技能 id 时，前端不得以字面量 `undefined` 拼入
`/api/skills/by-id/undefined`；无参深链回落技能列表，参数错误与「技能不存在」
在 API 与 UI 语义上分离。

## 范围（本 PR 落地）

1. **路由层守卫**：`app.js` `nav()` 的 `view === 'skill'` 分支在取数前判定
   `空 / undefined / null` 三哨兵，命中即轻提示 + `nav('skills')` + `return`，
   **不发起任何请求**；`popstate` 重放复用同一分支，覆盖刷新/前进后退。
2. **hash 生成侧**：`_pushHistory(view, param)` 对 `skill` 缺参改写为 `#skills`，
   从源头消除半截 hash。
3. **API 层语义**：`skills.py` `get_skill_detail_by_id` 仅对 `''/undefined/null`
   三哨兵返回 `400 invalid_skill_id`，置于名称解析之前；另补 `/by-id/` 空路径
   专用路由返回 400。
4. **前端错误渲染**：`catch` 按 `e.status === 400` 走参数错误文案，其余（含 404）
   仍走「加载失败」；新增中英文 `common.invalid_param`。
5. **回归用例**：前端源码级 6 项断言 + 后端 10 项哨兵/名称深链断言。

## 约束（不得违反）

- 哨兵拦截**只限三个字面量**，严禁扩大为「非数字即 400」；
- Issue #630 名称深链保持：未知名称仍返回 404。

## 验收

- `#skill` / `#skill/undefined` / `#skill/null` 回落技能列表或返回 400 参数提示，
  不再显示「技能未找到」；
- `#skill/<有效id>` 与 `#skill/<技能名>` 行为零回退；未知名称仍 404；
- 刷新/前进后退不复现 undefined 请求；
- 灰度后 `/api/skills/by-id/undefined` 请求计数归零（由 sre 核对）。