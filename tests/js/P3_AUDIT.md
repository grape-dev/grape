# P3 测试审计台账（2026-11-11）

状态：js 测试 22 通过 / 38 C类 / 31 D类（共 91）

## 已完成
- 路径债清零：60 个文件 62 处引用改到 Vue 源码或 frontend/legacy（commit 686c1f62f）
- D 类改写（源码级断言，17 条实质检查）：#460 / #446 / #202 / #203

## 真回归（测试正确报红，勿改断言迁就代码）
- #290 openIssueLink：IssuesView.vue:2185 写回 `tk.is_pr ? '/pull/' : '/issues/'`，
  违反 #290 验收「查看 Issue 入口统一 /issues/、无 is_pr 分支」。→ 待主人判定（修代码 or 改验收）

## C 类（38）——vm 执行旧页面 JS，被测对象已删，需逐一判定改写/退役
- [ ] test_anomaly_page_size.js
- [ ] test_chat_conv_title.js
- [ ] test_chat_onboard_409_restart.js
- [ ] test_chat_onboard_refresh.js
- [ ] test_chat_recall_442.js
- [ ] test_chat_task_bubble.js
- [ ] test_event_back_to_task.js
- [ ] test_event_nav_hash.js
- [ ] test_issue_1077_board_col_paging.js
- [ ] test_issue_1102_load_more.js
- [ ] test_issue_12_host_confirm.js
- [ ] test_issue_1310_dropdown_display.js
- [ ] test_issue_1310_runtimes_display.js
- [ ] test_issue_1564_scene_scope_violation.js
- [ ] test_issue_187_workspace_repo_restore.js
- [ ] test_issue_273_lang_switch_toast.js
- [ ] test_issue_341_tasks_deeplink.js
- [ ] test_issue_342_event_poll_partial.js
- [ ] test_issue_348_bot_token_health.js
- [ ] test_issue_402_scene_copy.js
- [ ] test_issue_406_skill_display_name.js
- [ ] test_issue_407_chat_attachment.js
- [ ] test_issue_407_chat_draft.js
- [ ] test_issue_407_dash_draft.js
- [ ] test_issue_436_runtime_search.js
- [ ] test_issue_456_orgs_back_to_workspace.js
- [ ] test_issue_461_scene_deploy_url.js
- [ ] test_issue_465_worker_card_name.js
- [ ] test_issue_539_review_status_render.js
- [ ] test_issue_543_migrate_orgs.js
- [ ] test_observability_events_flow.js
- [ ] test_observability_i18n.js
- [ ] test_observability_page_size_162.js
- [ ] test_observability_pagination.js
- [ ] test_observability_sections.js
- [ ] test_pipeline_scroll_layout.js
- [ ] test_pipeline_search.js
- [ ] test_scene_apply_detail_refresh.js

## D 类（35）——断言针对旧实现写法，需按原意在新实现上重写
- [ ] test_engine_guide_simplified.js
- [ ] test_i18n_repos_local_refresh.js
- [ ] test_issue_1077_smooth_pagination.js
- [ ] test_issue_1167_runtime_empty_filter.js
- [ ] test_issue_1477_apikey_time_format.js
- [ ] test_issue_205_beta_apply_persist.js
- [ ] test_issue_273_en_prefix_refresh.js
- [ ] test_issue_278_refresh_route.js
- [ ] test_issue_286_chat_bubble_layout.js
- [ ] test_issue_290_view_issue.js
- [ ] test_issue_297_multi_image_error_and_persist.js
- [ ] test_issue_299_org_name_display.js
- [ ] test_issue_316_scene_repo_tab_removed.js
- [ ] test_issue_329_cross_pipeline.js
- [ ] test_issue_335_skill_search.js
- [ ] test_issue_372_r13_ui_optimization.js
- [ ] test_issue_393_admin_confirm_entry.js
- [ ] test_issue_473_range_filter.js
- [ ] test_issue_475_runtime_health_hint.js
- [ ] test_issue_487_scene_recommend_save.js
- [ ] test_issue_498_health_consistency.js
- [ ] test_issue_645_skill_update_fallback.js
- [ ] test_issue_648_attachments_inline.js
- [ ] test_issue_697_dedup_template.js
- [ ] test_issue_978_skill_unique_key.js
- [ ] test_password_blocklist.js
- [ ] test_repo_persist_on_refresh.js
- [ ] test_repos_host_provider.js
- [ ] test_sidebar_collapse.js
- [ ] test_token_create_deeplink.js
- [ ] test_unbind_repo_list_invisible.js
