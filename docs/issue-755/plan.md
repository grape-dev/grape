# Plan: Issue #755

## 单号解析优先级（`_resolve_cross_repo_issue_number`）

1. 目标仓 `issue` 表已有该编号行 → 原样返回（`kept`，正常同仓 A2A 零开销）
2. `carried_context.target_issue_number`（转交建单后显式回填）且目标仓 DB 存在 → 采用（`target_field`）
3. 目标仓 `issue` 表反查 `carried_context.source_issue_number == 传入编号` → 唯一/最新命中（`db_mapping`，多命中取最大编号=最新落地单）
4. 均未命中 → 原样返回（`unresolved`，行为不变）

## 实施步骤（TDD）

1. 新增 helper：`_issue_exists_in_db` / `_resolve_cross_repo_issue_number` / `_a2a_issue_vcs_check`（token 口径与 `_a2a_repo_fallback_check` 对齐：TokenResolver 策略链；VCS 404 → not_found，网络/无 token → unknown 不阻断）
2. `a2a_endpoint`：repo_key 解析后调用解析 helper 回填 payload.issue_number（方案 A 入口）；鉴权与仓库归属校验之后、幂等查询之前做 VCS 存在性校验，not_found → 400（方案 B，未认证不外呼与 #671 口径一致）
3. `_cross_pipeline_handoff`：场景校验后、去重检查前做同口径回填（覆盖直接调用路径，如 issues.py 列表视图移交代理）；carried_context 持久化时补 `target_issue_number` 字段
4. `resolve_issue_id`：payload.issue_number == carried_context.source_issue_number 且 target_issue_number 为数字时返回真实单号（防御性兜底）
5. 测试：`tests/test_issue_755_a2a_issue_number.py`（helper 单测 + _cross_pipeline_handoff 回填 + a2a_endpoint 400/回填 + resolve_issue_id）

## 风险

- VCS 外呼误杀：仅 DB 未命中 + 有 token + VCS 明确 404 才 400；unknown 一律放行
- 多命中歧义：同源建多单（#753/#754）→ 取最大编号（最新落地单），与 `_max_issue_from_issues` 口径一致
- 兼容性：target_issue_number 为增量字段，向后兼容；正常同仓 A2A（DB 命中）零行为变化
