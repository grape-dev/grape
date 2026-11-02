# Spec: Issue #655 — pending-review / awaiting-input agent_type 白名单放宽为场景动态校验

> 来源：用户反馈 id=282 | 优先级 P1 | 目标分支 weekly-08-mys | 前序 Issue Agent Spec（方案 1）已确认

## 问题

`src/api/routes/issues.py` 两处硬编码 agent_type 白名单导致自定义流水线节点角色调用官方审核点接口一律 403：

1. **awaiting-input**（L1213）：`session_agent not in ("vod", "issue")` → 自定义 agent 一律 403；
2. **pending-review / review-pass / review-reject**：共用 `_session_devops_issue()`（L1281）校验 `_DEV_OPS_AGENTS = ("vod", "issue", "coding", "qa", "sre")` → 自定义 agent 一律 403。

影响：非标准角色节点（如「场景提取」）只能降级为发 Issue 评论，审核状态不进 `pending_review` / `awaiting_input`，流程可观测性受损。

## 目标

以 Issue 绑定场景的节点集合动态构建允许集，替代硬编码元组；场景不可得时回退原白名单保证向后兼容。

## 验收标准

- [ ] 自定义 agent_type（如「场景提取」）调用 `pending-review` 返回 200 且 status 置为 `pending_review`
- [ ] 自定义 agent_type 调用 `awaiting-input` 返回 200 且 status 置为 `awaiting_input`
- [ ] 原有 vod/issue/coding/qa/sre 角色不受影响（向后兼容，含场景未绑定/解析失败 fallback）
- [ ] 场景解析失败/未绑定（scene_id 空、default、DB 未命中 fallback）时仅原白名单生效
- [ ] 所有现有测试通过；新增测试覆盖动态校验与 fallback 分支
