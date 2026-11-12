# 退役测试登记（P3，2026-11-11）—— **已批准并执行删除**（2026-11-11）

本文件登记**被测对象已不存在、且无法用源码断言等价替代**的回归测试。
规则：每条必须写清「原测什么 / 为什么无对象 / 现在由谁覆盖」，严禁无记录删除。

## 1. token_create_deeplink（`tests/js/test_token_create_deeplink.js`）

- 原测：legacy `js/pages/account.js` 里 GitHub token 深链创建流程的守卫（`guard: github deeplink`）。
- 无对象：`account.js` 随 Vue 迁移删除；`legacy/js/app.js` 与 `AccountView.vue` 里
  `deeplink` 关键字出现次数均为 **0**，即深链创建 token 的入口已不存在。
- 现覆盖：token 创建由 AccountView 的 token 列表 + 新建流程覆盖（`test_issue_1310_*`、
  `test_password_blocklist` 等间接覆盖同页；无等价深链用例）。
- 结论：退役。若产品要恢复「深链一键建 token」，按新入口另写用例。

## 2. engine_guide_simplified（`tests/js/test_engine_guide_simplified.js`）

- 原测：Q1–Q12 —— 聚光引导已删后保留的**脉冲动画**（仅 box-shadow）、引擎横幅、
  侧栏进入引擎页横幅退场、密码条排队 chip 等。
- 无对象：`legacy/css/app.css` 与 `RuntimesView.vue` 中均无 `pulse` / `rt-guide-pulse`
  规则（grep 0）；横幅逻辑本体仍在 `app.js`（`_checkEngineBanner` / `_dismissEngineBanner`
  / `_gotoEnginePage`），但测试断言的 12 项形态（脉冲、自动开面板、排队 chip）已无实现。
- 现覆盖：横幅部分由 app.js 承担，但**没有对应测试**；确认要保的话，应针对
  `_checkEngineBanner` 新写行为用例，而不是沿用旧的 Q 系列。
- 结论：退役（旧的 12 项形态），横幅留待新用例。

## 3. issue_335_skill_search（`tests/js/test_issue_335_skill_search.js`）

- 原测：技能刷新成功后更新 `_engineSkillCache`，避免重复请求。
- 无对象：`SkillsView.vue` / `RuntimesView.vue` 中均无 `_engineSkillCache` / `skillCache`
  （grep 0），Vue 版改为按需拉取，缓存契约不存在。
- 现覆盖：技能列表由 SkillsView 自身状态管理（`test_issue_645_*` / `test_issue_978_*` 覆盖同页）。
- 结论：退役。

## 执行记录

| 测试 | 状态 |
|---|---|
| token_create_deeplink | 已删除 |
| engine_guide_simplified | 已删除 |
| issue_335_skill_search | 已删除 |

## 3. 未退役、但挂「待确认」的不在本文件

`chat_task_bubble`（去重可能移到后端 SSE）、`issue_372_r13_ui_optimization`（7/14 断言需人工判定）、
`issue_335_skill_search`（引擎技能缓存已不存在）、`issue_1477_apikey_time_format`（时间格式依赖后端返回形态）、
`scene_apply_detail_refresh`（需运行期验证）、`issue_1102_load_more`（已知缺口）、
`issue_406_skill_display_name`（真回归）、`issue_290_view_issue`（待主人判定）。
这些**保持红灯**，不删除、不改断言迁就现状。