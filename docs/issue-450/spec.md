# Spec: Issue #450 — 跨流水线移交支持目标流水线选择自己

## Objective
移除跨流水线移交中「目标不能是当前流水线」限制，目标列表包含当前流水线自身。

## Scope (In)
- src/api/issues_center.py get_handoff_targets()：移除 _sid == _cur 排除逻辑
- src/api/routes/issues.py POST /issues/{id}/handoff：移除 target==current 的 409 拦截
- src/api/static/js/pages/issues.js：前端不再过滤当前流水线
- locales zh/en issues.json：更新 cross_pipeline.no_target / duplicate_current 文案

## Acceptance Criteria
- 移交弹窗目标列表出现当前流水线且可选择
- 选择当前流水线提交不再返回 409，移交成功
- 重复移交仍被 duplicate_handoff 拦截
- zh/en 文案同步更新
