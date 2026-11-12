# P3 测试审计台账（2026-11-11，滚动更新）

js 测试：**78 通过 / 6 C类 / 7 D类**（共 91；起点 13 通过 / 78 失败）

## 已完成
- 路径债清零：60 文件 62 处引用 → Vue 源码 / frontend/legacy（686c1f62f）
- D 类改写为源码断言：
  - #460 标题同步 4 断言  ·  #446 部署链接XSS 5  ·  #202 查重不阻断 4  ·  #203 复制单 4（fe4927268）
  - #393 管理员确认 4  ·  #648 附件渲染 4  ·  #1077 平滑分页 5
  - 合计 7 个测试 / 30 条实质断言

## 已知偏差（记录下来，不假装满足）
- #123：异常任务页「每页条数选择器」未随 Vue 版移植，现固定 10 条/页 + 页码翻页（分页本身正常）。
- #648：原「卡片自带 attachments 内联渲染、免一次请求」的首屏优化未随 Vue 版移植；loadDetailExtras 一律 GET 端点。性能项，非功能缺陷。

## 真回归 · RuntimesView 功能缺失（2026-11-11 批次3 审计）

| Issue | 现象 | 证据 |
|---|---|---|
| #473 范围筛选 | 前端完全没有范围 chips，也不传 `range` 参数 | 后端仍在支持：`settings.py:926`（docstring 记 #473）、`_rt_in_range` 在 `settings.py:1348`、过滤调用 `:1138`；前端 `RuntimesView.vue` 里 `range` 出现 **0 次** |
| #475 健康提示 | 无帮助按钮、无异常描边，`health_reason` 无人消费 | 后端仍透出 `health_reason`/`health_abnormal`（`settings.py:1078,1091`）；前端 `health_reason` **0 个消费者**；i18n `runtimes.health.hint.*` 6 个键成了孤儿；页面只有 `healthLabel(rt.health)` |
| #498 异常口径一致 | 同上（后端口径未接入前端判定） | 同 #475；`_isAbnormalEngine`/`_abnormalBorderColor` 在 Vue 版不存在 |
| #1167 筛选空态 | 有筛选/无筛选共用同一句 `empty.no_engines`，无「组织无引擎」引导区分 | `RuntimesView.vue:169-171` 单一分支；`clearFilters` 存在但空态未按筛选态分叉 |

结论：这 4 个测试**报红是对的**，不是测试陈旧。是否恢复功能、还是确认不再需要，待主人拍板。

## 功能缺失总表（2026-11-11 全页审计，只读结论）

**已确认缺失（8 项）→ 2026-11-11 按方案 B 已修复 5 项**：#473/#475/#498/#1167（RuntimesView）+ #1564（DashboardView）。剩 #290（待判定）、#1102（事件分页，暂记账）、#648（内联首屏性能，暂记账）

| # | 页面 | 缺什么 | 证据 |
|---|---|---|---|
| #473 | RuntimesView | 范围筛选（全部/仓库/个人）chips + `range` 传参 | 后端 `settings.py:926/1138/1348` 仍在；Vue 里 `range` 0 处 |
| #475 | RuntimesView | 健康帮助提示（按钮 + 提示文案） | 后端 `health_reason` 在 `settings.py:1078/1091`；前端 0 消费者；`runtimes.health.hint.*` 6 键成孤儿 |
| #498 | RuntimesView | 异常口径（`health_abnormal` 优先 + 异常描边） | 同 #475；`_isAbnormalEngine`/`_abnormalBorderColor` 在 Vue 版不存在 |
| #1167 | RuntimesView | 筛选空态 vs 组织无引擎 的分叉 | `RuntimesView.vue:169-171` 单一 `empty.no_engines` |
| #290 | IssuesView | 「查看 Issue」入口统一 `/issues/` | `IssuesView.vue:2185` 写回 `is_pr ? '/pull/' : '/issues/'` |
| #1102 | IssuesView | 事件历史「默认 20 条 + 加载更早」分页 | `IssuesView.vue:1604-1608` 一次性渲染全部 events，无翻页/无按钮 |
| #1564 | DashboardView | 保存失败按 skill scope `violations[]` 逐节点标红 | `saveSceneEdit` 的 catch 只 `showToast(t('save_failed'))`（约 :1930） |
| #648 | IssuesView | 卡片自带 attachments 内联首屏（免一次请求） | `loadDetailExtras` 一律 GET `/issues/{id}/attachments`（性能项，非功能） |

**待确认（需运行期验证，静态看不出）**：#407 Chat 附件保活（`ChatView` 有 file/image/upload 逻辑，但"闲置返回不丢"是行为）；#224 Event hash 同步（只有 `replaceState` 1 处）；#1310 agents 下拉（`<option>` 3 处、`display_name` 2 处）。

**其余约 28 个 C 类**：功能在 Vue 实现里找得到（换名/换结构），可按批次改写为源码断言。

## 真回归（测试报红是对的，勿改断言迁就代码）
- #290 openIssueLink（IssuesView.vue:2185）：`tk.is_pr ? '/pull/' : '/issues/'`，违反 #290 验收「查看 Issue 入口统一 /issues/」。**待主人判定**，当前两边都不动。

## C 类（vm 执行旧页面 JS；被测对象已删，需逐一判定改写/退役）
- [x] test_anomaly_page_size.js（已改写）
- [x] test_chat_conv_title.js（已改写）
- [x] test_chat_onboard_409_restart.js（已改写）
- [x] test_chat_onboard_refresh.js（已改写）
- [x] test_chat_recall_442.js（已改写）
- [ ] test_chat_task_bubble.js（**待确认**：ChatView 与后端均无去重逻辑，可能随统一流设计移到后端 SSE，需主人确认）
- [x] test_event_back_to_task.js（已改写）
- [x] test_event_nav_hash.js（已改写）
- [x] test_issue_1077_board_col_paging.js（已改写）
- [ ] test_issue_1102_load_more.js
- [x] test_issue_12_host_confirm.js（已改写）
- [x] test_issue_1310_dropdown_display.js（已改写）
- [x] test_issue_1310_runtimes_display.js（已改写）
- [x] test_issue_1564_scene_scope_violation.js（已修复并改写）
- [ ] test_issue_187_workspace_repo_restore.js
- [ ] test_issue_273_lang_switch_toast.js
- [x] test_issue_341_tasks_deeplink.js（已改写）
- [x] test_issue_342_event_poll_partial.js（已改写）
- [x] test_issue_348_bot_token_health.js（已改写）
- [x] test_issue_402_scene_copy.js（已改写）
- [ ] test_issue_406_skill_display_name.js（**真回归**：DashboardView 无「选 VOD 技能后禁用 SRE」联动，checked disabled 仅分页按钮）
- [x] test_issue_407_chat_attachment.js（已改写）
- [x] test_issue_407_chat_draft.js（已改写）
- [x] test_issue_407_dash_draft.js（已改写）
- [x] test_issue_436_runtime_search.js（已改写）
- [x] test_issue_456_orgs_back_to_workspace.js（已改写）
- [x] test_issue_461_scene_deploy_url.js（已改写）
- [x] test_issue_465_worker_card_name.js（已改写）
- [x] test_issue_539_review_status_render.js（已改写）
- [x] test_issue_543_migrate_orgs.js（已改写）
- [x] test_observability_events_flow.js（已改写）
- [x] test_observability_i18n.js（已改写）
- [x] test_observability_page_size_162.js（已改写）
- [x] test_observability_pagination.js（已改写）
- [x] test_observability_sections.js（已改写）
- [x] test_pipeline_scroll_layout.js（已改写）
- [x] test_pipeline_search.js（已改写）
- [ ] test_scene_apply_detail_refresh.js（**待确认**：SceneMarket 有 loadDetailData/popstate 重进，应用后刷新语义需运行期验）

## D 类（断言针对旧实现写法，需按原意在新实现上重写）
- [ ] test_engine_guide_simplified.js
- [x] test_i18n_repos_local_refresh.js（修 harness 后恢复，仍为行为测试）
- [x] test_issue_1167_runtime_empty_filter.js（已修复并改写）
- [ ] test_issue_1477_apikey_time_format.js（**待确认**：fmtTimestamp8 只在 legacy/js/api.js，AccountView 直接渲染 created_at 原文，需看后端是否返回 epoch）
- [x] test_issue_205_beta_apply_persist.js（修 harness 后恢复，仍为行为测试）
- [x] test_issue_273_en_prefix_refresh.js（修 harness 后恢复，仍为行为测试）
- [x] test_issue_278_refresh_route.js（修 harness 后恢复，仍为行为测试）
- [x] test_issue_286_chat_bubble_layout.js（已改写）
- [ ] test_issue_290_view_issue.js
- [x] test_issue_297_multi_image_error_and_persist.js（已改写）
- [x] test_issue_299_org_name_display.js（修 harness 后恢复，仍为行为测试）
- [x] test_issue_316_scene_repo_tab_removed.js（已改写）
- [x] test_issue_329_cross_pipeline.js（已改写）
- [ ] test_issue_335_skill_search.js（**待确认**：SkillsView/RuntimesView 均无 _engineSkillCache，刷新后缓存语义需确认是否仍有必要）
- [ ] test_issue_372_r13_ui_optimization.js（**待确认**：14 项断言 7 项失效，需人工逐条判定）
- [x] test_issue_473_range_filter.js（已修复并改写）
- [x] test_issue_475_runtime_health_hint.js（已修复并改写）
- [x] test_issue_487_scene_recommend_save.js（已改写）
- [x] test_issue_498_health_consistency.js（已修复并改写）
- [x] test_issue_645_skill_update_fallback.js（已改写）
- [x] test_issue_697_dedup_template.js（已改写）
- [x] test_issue_978_skill_unique_key.js（已改写）
- [ ] test_password_blocklist.js（**真回归 P10d**：AdminView 三处 `password.length < 8` 硬编码，未走统一谓词 isPwdTooShort）
- [x] test_repo_persist_on_refresh.js（修 harness 后恢复，仍为行为测试）
- [x] test_repos_host_provider.js（已改写）
- [x] test_sidebar_collapse.js（修 harness 后恢复，仍为行为测试）
- [ ] test_token_create_deeplink.js
- [x] test_unbind_repo_list_invisible.js（已改写）
