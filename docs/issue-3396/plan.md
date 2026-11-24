# Plan: Issue #3396

实施顺序（按依赖，每项独立 commit，可单独 revert）：

1. **改动③ 认领侧口径统一（守卫先行）** `src/domains/runtime/engine/engine_select.py` + `src/domains/runtime/engine/service.py` + `tests/runtime/test_issue_658_engine_role_filter.py`（口径改造）
   - 新增 `engine_capability_tags(runtime_id)`（单一事实源读 engine_capabilities，enabled=1；异常 → 空列表 fail-closed）与 `engine_allows_role(runtime_id, agent_type)`（`agent_type ∈ tags 或 'all' ∈ tags`）。
   - `_filter_by_role` / `_member_role_tags` / `_role_no_candidates_detail` / `_candidate_reject_reasons` 全部改读 engine_capabilities；`engine_role_tags` 保留为注册透传解析（docstring 更新：守卫不再消费）。
   - `service.claim_by_tag`：守卫从 `agent_runtimes.role_tags` 改为 engine_capabilities；bound claim 拒绝 + FIFO skip 语义保留；`'all' ∈ tags` 放行任意角色；空标签 fail-closed。
   - 既有 `test_issue_658_engine_role_filter.py` 用例改为 engine_capabilities 口径（`_mk_engine` 支持 `cap_tags`，不再默认插 'all'），新增 A4/A7/E6 对应用例（脏 role_tags 不影响判定、UI 改标签即时生效、空标签 fail-closed、bound 拒绝）。
2. **改动② chat 进值域四处联动 + 对账扫描** `src/domains/runtime/routes/runtime.py` + `src/api/frontend/app/src/views/RuntimesView.vue` + `tests/test_issue_3396_tag_domain.py`
   - `_apply_tag_rules`：`AGENT_TAGS` 提升为模块级常量并加 `chat`（6 角色全选折叠为全能）。
   - 前端 `_RT_TAG_COLORS`（chat 颜色）、`_rtTagLabel`（chat 文案）、`_RT_TAG_OPTIONS`（+chat）、`onEngineTagChange`（agentCbs +chat、5→6 判定）、`_sceneAvailableRoles` 默认表（+chat）。
   - 对账扫描测试：解析 RuntimesView.vue 源码提取 `RT_TAG_OPTIONS`/`onEdTagChange` 勾选表/`sceneAvailableRoles` 默认表/`RT_TAG_COLORS`/`rtTagLabel`，与后端 `AGENT_TAGS` 断言一致（含 chat）。
3. **改动⑤ Chat 兜底角色过滤** `src/domains/runtime/engine/engine_select.py` + `src/kernel/session_manager.py` + `src/domains/runtime/routes/chat.py` + `src/domains/runtime/im/handler_async.py` + 测试
   - `resolve_local_engine(user_id, agent_type="")`：agent_type 非空时候选（本机直连 + global 回退）必须过 `engine_allows_role`；无合规候选 → decision log `chat_role_no_candidates` 留痕并返回 None（调用方已有「无可用引擎」明确提示）；缓存仅对 user_id=0 且 agent_type 空的调用生效。
   - chat 调用面（session_manager.get_chat_config、chat.py 四处、handler_async IM 路径）传 `agent_type="chat"`。
   - `_resolve_chat_runtime_id`：实例名/本机直连/末级按名兜底逐级过 chat 角色过滤（`engine_allows_role`），不合规落下一级；全不合规 → 0（明确不静默）。
4. **改动① 删 SRE 禁广播** `src/domains/runtime/engine/engine_select.py` + `src/domains/mgmt/routes/scenes.py` + `src/domains/runtime/scene_precheck.py` + `DashboardView.vue`/`IssuesView.vue` + 测试
   - 删 `_sre_no_broadcast` def 及 4 处调用分支（pick_engine SRE 分支整体删除并入通用链；get_scene_pool_engine_ids 3 处特判删除）；SRE 与其他角色同走 role→tag→scope→health 通用过滤链。
   - 新增/改造测试：SRE global 池广播（A1）、未勾 sre/all 引擎两侧不可调度（A2，`role_no_candidates` 留痕）。
5. **改动④ 写权限收紧** `src/api/deps.py` + `src/domains/runtime/routes/runtime.py` + 测试
   - 新增 `require_runtime_owner_or_admin(runtime_id)`：平台管理员（admin JWT + admin_accounts token_version 校验）全放行；普通用户路径先过 session_token 禁用（沿用 require_no_session_token 语义），再校验引擎归属——credential_id=0（本机直连）仅平台管理员可改；user 本人（owner_name 匹配）或 org owner/admin（get_member_role ∈ owner/admin）可改；其余 403。
     > 2026-11-24 修正（分层裁定，实现以 `src/api/deps.py`.`_check_runtime_owner_or_admin` 为准）：本机直连（credential_id=0）放开为**任意登录用户**可改；凭据引擎（credential_id≠0）仍仅**平台管理员 / 凭据 owner 本人 / 凭据所属 org 的 owner+admin** 可改，其他登录用户与仓库管理员 403；session_token 始终 403。上句「本机直连仅平台管理员」为 11-22 初版口径，已被本条取代。
   - `PUT /runtimes/{id}/tags` 换用该依赖；`GET role-tags` 保持全员可读。
   - A5 权限矩阵测试（所有者 200 / 平台管理员 200 / 非所有者 403 / 本机直连任意登录用户 200；11-24 分层口径）。

回归验证：全量相关套件（test_issue_658_engine_role_filter、test_engine_select*、test_chat_*、新测试）+ A1-A8 复验 + 无 weekly-11 专属假设残留检查。

> 移植说明：本 Issue 初版基于 arch-weekly-11@9c4c770b8；基线已推进到 8e4f961ea（`src/` → `src/domains/` + `src/kernel/` 分层、前端 JS 页面迁 Vue），本分支已整体移植到新布局，验收口径不变。

回滚约束：5 个 commit 各自可独立 revert；回滚改动④（写权限收紧）必须连带回滚改动③（守卫口径统一）。

## 复审整改（架构守护 F1/F2/F3 + 合规文案项，基线 77d77b202）

1. **F1（A9）· settings 三写入口同一权限矩阵**：`/api/settings/agent-runtimes/{rid}/tags`
   的 POST/PUT/DELETE 改挂 `require_settings_runtime_owner_or_admin`（`rid` 路径名不变，OpenAPI 不动），
   与 `PUT /runtimes/{runtime_id}/tags` 共用 `_check_runtime_owner_or_admin`；矩阵用例补齐三个端点。
2. **F2（C3）· 标签写入审计**：新增 `engine_tag_change` 审计事件，四个写入口分别在成功后留痕
   （runtime 经 `src.domains.mgmt.public.log_audit`，settings 经 `auth.audit.log_audit`），
   detail 只记 runtime_id/tag(s)/action/enabled/priority/operator，不含凭据。
3. **F3（A11）· 跨域棘轮**：api 层归属读取改运行域 public 具名查询
   （`runtime_credential_ref_any`、`runtime_credential_owner_org_any`），
   `scripts/foreign_table_refs.py --check` 恢复 PASS（agent_runtimes←api 11→10、runtime_credentials←api 7→6）。
4. **Informational 3**：`claim_by_tag` 的守卫模块导入包进 try，导入失败显式 fail-closed 并留痕。
5. **合规文案项**：恢复 `dashboard.json` en/zh 的 `fgrp_range/fgrp_type/fgrp_scope`
   （`DashboardView.vue` 的 `t()` 只解析 dashboard./pipeline./common.，不读 runtimes. 命名空间）；
   保留 `sre_engine_*` 键删除。
6. **Informational 1 未采纳**：`_resolve_user_or_platform_admin` 需先尝试 admin JWT 快速通道、
   再回落通用用户认证；改为 `Depends` 组合会让通用认证在 admin JWT 场景先执行并 401，
   故保留体内解析，测试侧整体 override 该依赖。
