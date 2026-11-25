# Task: Issue #3396

- [x] T1 改动③：认领/派发角色守卫口径统一到 engine_capabilities（engine_capability_tags / engine_allows_role 新增；_filter_by_role / _member_role_tags / _role_no_candidates_detail / _candidate_reject_reasons / claim_by_tag 改源；空标签 fail-closed；engine_role_tags 降级注册透传）
- [x] T2 改动③测试：test_issue_658_engine_role_filter.py 口径改造 + 新增 A4/A7/脏 role_tags/A8 用例（32 例全绿，新基线适配探活闸门）
- [x] T3 改动②：chat 进标签值域四处联动（AGENT_TAGS / _RT_TAG_OPTIONS / _RT_TAG_COLORS / _rtTagLabel / onEngineTagChange 5→6 / _sceneAvailableRoles 默认表）+ 对账扫描测试（test_issue_3396_tag_domain.py 11 例，解析 RuntimesView.vue）
- [x] T4 改动⑤：Chat 两条兜底路径 chat 角色过滤（resolve_local_engine agent_type 参数 + _resolve_chat_runtime_id 逐级过滤），无合规候选明确提示（test_issue_3396_chat_fallback_role_filter.py 12 例）
- [x] T5 改动①：删 _sre_no_broadcast 及 pick_engine/get_scene_pool_engine_ids 4 处调用分支 + scenes 写侧校验/预检/engine-status/dashboard 前端配套特判拆除（test_sre_repo_worker_relax.py 重写 + test_scene_precheck_1245/test_issue_404 更新；scene_precheck 落 src/domains/runtime/，scenes 写侧另删 SRE 授权档与空绑定自动停用、前端迁 DashboardView/IssuesView Vue）
- [x] T6 改动④：require_runtime_owner_or_admin 写权限收紧（所有者/org owner+admin/平台管理员可改，仓库管理员禁改，本机直连仅平台管理员）+ OpenAPI 契约登记（test_issue_3396_tags_write_permission.py 11 例）——**2026-11-24 分层修正**：本机直连（credential_id=0）改为任意登录用户可改，凭据引擎保留归属矩阵，见 spec.md 决策表 / A5
- [x] T7 测试与验收：A1-A8 用例覆盖 + 受影响套件与基线逐文件对账（无新增失败，基线既有失败清单见 PR 描述）+ 5 commit 独立可 revert + 无 weekly-11 专属假设残留（改动内无分支/行号假设，符号名定位）
- [x] T8 文档：docs/issue-3396/{spec,plan,task}.md
- [x] T9（M1 整改）A8b 声明兼容：注册/本机发现带 role_tags → 按声明播种角色能力行（无声明仍补 all；**已有角色行含 enabled=0 一律不覆盖**；`tags_user_set=1` 且零角色行的历史遗留行按 11-24 裁定照补；存量行「role_tags 非空 + 仅自动 all」再注册改写）；test_issue_3396_role_tags_seed_compat.py 7 例
- [x] T9 架构守护复审整改（新基线 77d77b202 后重跑）：
  - F1/A9：settings 三个同源写端点（`POST/PUT/DELETE /api/settings/agent-runtimes/{rid}/tags`）统一挂
    `require_settings_runtime_owner_or_admin`，与运行域 `require_runtime_owner_or_admin` 共用 `_check_runtime_owner_or_admin` 判定。
  - F2/C3：四个标签写入口补审计 `engine_tag_change`（runtime 经 `src.domains.mgmt.public.log_audit`；settings 经 `auth.audit`），新增 EVENT_TYPES 条目。
  - F3/A11：`src/api/deps.py` 去裸 SQL，改运行域 `public.read_one_any` 具名查询
    （`runtime_credential_ref_any` / `runtime_credential_owner_org_any`），`foreign_table_refs.py --check` 由 10→11、6→7 恢复为棘轮 PASS。
  - Informational 3：认领侧守卫模块导入失败改为显式 fail-closed 留痕。
- [x] T10 文案与描述校正：恢复 `dashboard.json` en/zh 的 `fgrp_range/fgrp_type/fgrp_scope`（`DashboardView.vue` 只解析
  dashboard./pipeline./common. 三个命名空间）；PR 正文符号名更正为 `onEdTagChange`。
- [x] T11 复审收口（head 168d9f161，已 rebase 至基线 8e4f961ea）：
  - 删除 `src/domains/runtime/engine/engine_select.py` 中无消费者的 `_bound_ids` / `_cand_trace` 预构建块
    （`_pick_decision` 已自带 candidates 取证），避免「场景已绑定引擎」派发时重复 `_engines_by_ids`
    与逐候选 `_candidate_reject_reasons`（含 engine_capabilities 查询与健康判定）。
  - PR 描述按 head 重写：基线更正为 `8e4f961ea`，commit 表刷新为 rebase 后的 22 个 commit，并补记
    `68d4141e9f` / `24285c5bbc` / `16143a2980`；其中 `16143a2980` 把「选择引擎 — SRE」候选口径由
    user+repo 两档改为与其它角色一致的四档（global/user/repo/local），属行为变更已在描述中说明。
  - nit 清理：`RuntimesView.vue` 删除未使用的 `ALL_ROLES` 导入；`DashboardView.vue` 将恒等于
    `engModalAll` 的 `engModalVisible` 合并为单一 computed；`locales/{zh,en}/dashboard.json` 删除已无引用的
    `sre_no_engine` 键并同步页面缓存版本与前端棘轮登记值。
- [x] T12 基线漂移整改（基线 8e4f961ea，head 168d9f161）：
  - rebase 至 `arch-weekly-11@8e4f961ea`（新增 5 个 commit：sandbox 去 create-time clone、worker 模板探活
    改走 60s 刷新环、pipeline 徽标深色调色板、跨域迁移收敛到对象所属库）。冲突仅落在
    `shell/index.html` 与两套前端棘轮表（`dashboard-view.js` / `pipeline.js`），按守卫纪律以实际源重建登记值：
    `pipeline.js` 取基线新值 `?v=55`（`Pipeline.vue` 在基线已改）与指纹 `3b7ad8e15b706e28`；`dashboard-view.js`
    保留本分支 `?v=88`；`all-views.css` 源集合因基线样式改动变化，页面 `?v=` 68→69 并同步 `ALL_VIEWS`
    指纹 `90b2c856f16b0d56`。
  - rebase 后三套前端棘轮（bundle_source / cache_version / i18n_locale）9 例全绿；`docs/issue-3396/{spec,plan}`
    基线行与 T11 中 rebase 前 SHA（`16143a298` / `48496bbaa` / `0c239f2dd`）已更新为现值 `8e4f961ea` / `168d9f161`。

## 验收映射（A1-A8 → 测试）

| 验收 | 覆盖测试 |
|---|---|
| A1 SRE global 池认领 | test_sre_repo_worker_relax（unbound 广播 ""、sre 标签 global 引擎认领）+ tag_domain/scene_precheck 空绑定放行 |
| A2 两侧不可调度 + role_no_candidates 留痕 | 658 池过滤/认领拒绝 + sre 无 cap 用例（_log_role_no_candidates 留痕路径） |
| A3 UI 可勾 chat、6 角色折叠、tags_user_set | test_issue_3396_tag_domain（折叠/互斥）+ 658 A8 用例（注册自动 all、tags_user_set 既有机制未动，test_issue_654 回归绿） |
| A4 UI 改标签即时生效、role_tags 不再影响 | 658 test_ui_tag_change_*（派发+认领）+ test_dirty_role_tags_* |
| A5 权限矩阵 | test_issue_3396_tags_write_permission（11 例全矩阵） |
| A6 Chat 兜底只落 chat/all、明确提示 | test_issue_3396_chat_fallback_role_filter（resolve/末级兜底/全不勾 None+留痕） |
| A7 空标签 fail-closed | 658 cleared_engine（派发+认领）+ chat fallback cleared 用例 |
| A8 存量回归（注册自动补 all） | 658 test_register_auto_all_keeps_legacy_engines_claimable + 默认 all 用例组 |
| A8b 声明即收窄（特殊场景机器） | test_issue_3396_role_tags_seed_compat（声明播种/多角色/非法声明回落/存量改写/user_set 不覆盖/既有角色行不覆盖） |
