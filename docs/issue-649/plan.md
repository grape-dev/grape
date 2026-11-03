# Plan: Issue #649 实现方案

## 定位
- 入口：`POST /api/issues/{owner}/{repo}`（src/api/routes/issues.py::create_issue）→ `src/api/issues_center.py::create_task_via_issue`。
- 缺陷点：assignee 解析处 `else: assignee = user["username"]`（默认填创建者）+ 403 分支文案「处理人无效或无权限…请检查 assignee 是否为有效 GitCode 用户名」。

## 改动清单
1. `create_task_via_issue` assignee 解析：`(body.assignee or "").strip()`；新增 `_creator_login`（external_login 优先）承接 issue_creator / pipeline payload 创建人（原用 assignee 的两处改用 `_creator_login`）。
2. 新增 `_gitcode_assignee_member_check(api_token, owner, repo, username)`：协作权限接口三态判定；`create` 前置调用（GitCode + assignee 非空），not_member → HTTP 403（message 非成员语义 + trace_id + upstream_detail）。
3. 403 权限/成员类分支：assignee 非空 → 非成员语义 + trace_id + upstream_detail；为空 → 通用上游 403；移除「用户名无效」措辞。
4. 测试：新增 `tests/test_issue_649_assignee_member_check.py`（5 用例）；更新 `tests/test_issue_523_create_403_attribution.py`（语义文案 + 预校验 mock）。

## 风险与对策
- 权限查询接口 403（无权查询）被误判为非成员 → 三态 unknown 放行，交上游 create 最终裁决。
- 内部流程（场景归属 / 事件创建人）依赖原默认 assignee → `_creator_login` 解耦承接，行为不变。
- 限流/瞬时错误 → unknown 放行，不阻断建单。

## 验证
- `pytest tests/test_issue_649_assignee_member_check.py tests/test_issue_523_create_403_attribution.py` 全通过；
- 关联回归（404/415/onboarding/tasks/issues_api）通过；既有 5 个失败为基线预存在（stash 验证一致）。
