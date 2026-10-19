# Plan: Issue #367

## 实现步骤（TDD：先测后改）
1. **RED**：新增 `tests/test_issue_367_skill_scope_priority.py`
   - user+repo+official 三同名并存 → resolve_skill_id 命中 user；
   - repo+official 同名 → 命中 repo；
   - 仅 official → 命中 official（零回归）；
   - 用户 A 的 user_id 不命中用户 B 的个人 skill → 降级 repo/official；
   - user_id 缺失 → repo→official 降级；
   - prompt 注入：user scope skill_id（与 official 同名）→ 真实绝对路径而非
     `{{SKILL_DIR}}` 占位；official scope skill_id → 保持 `{{SKILL_DIR}}` 占位。
2. **GREEN-1** `src/user/db.py`：`resolve_skill_id()` 查询顺序改为
   user（owner_id=user_id）→ repo（owner_id=repo_key）→ official；命中时 log.info scope。
3. **GREEN-2** `src/prompt_builder.py`：`build_general_chat_prompt()` 的 skill 注入分支
   改为按 `get_skill_by_id(skill_id)` 的 scope 判定：scope∈{user,repo} → `get_skill_path`
   真实路径；scope 记录缺失时保留旧回退（`not resolve_official_skill_path(_sn)`）。
4. 调用方核对（只读验证，不改逻辑）：event_pipeline 1618/3891、webhook 857、
   scheduler 420、runtime 451/594、scenes 1700、session_manager 953 均带 user_id；
   PR-sub（event_pipeline 1593）与 session_store 540 无用户上下文 → 降级语义（不改）。
5. 回归：全量跑 resolve/prompt 相关既有测试。

## 风险
- 顺序反转影响隐式 official-first 依赖 → 仅同名冲突场景行为变化，无冲突不变；
- skill 记录查不到（scope 空）→ 保留旧回退分支，行为不劣化。
