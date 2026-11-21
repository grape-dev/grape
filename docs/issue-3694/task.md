# Issue #3694 任务拆解

> 目标合入分支：`arch-weekly-11-ljt` | 2026-11-21

| # | 切片 | 交付物 | 验证 |
|---|------|--------|------|
| 1 | 红：回归用例先行 | `tests/js/test_issue_3694_skill_delete_confirm.js` | `node tests/js/test_issue_3694_skill_delete_confirm.js` 先红（缺 computed / 缺词条 / 重复句仍在） |
| 2 | 绿：插值 + 去重 + 长名样式 | `SkillsView.vue`（computed 2 个、模板、`:deep` 样式、`KEY2_DEFAULTS`） | 用例转绿；`npm run lint` |
| 3 | 绿：i18n 词条 + 缓存版本 | `shell/locales/{zh,en}/skills.json`、四页 cacheVer、ratchet 指纹 | 用例转绿；`pytest tests/api/test_i18n_locale_cache_ratchet.py` |
| 4 | 全量回归 | — | `make test-js`（135+ 用例）、`npm run build`、`pytest tests/api/test_i18n_locale_cache_ratchet.py` |
| 5 | 自审查 | 5 轴 + pre-pr 增量检查 | `origin/arch-weekly-11-ljt...HEAD` 变更清单与敏感扫描 |
| 6 | 复审修改：静态版本联动 | `shell/index.html`、`views/entries/*.js`、两道棘轮冻结表 | `pytest tests/api/test_frontend_{cache_version,bundle_source}_ratchet.py` 转绿 |
| 7 | 复审修改：`$` 字面量替换 | `SkillsView.vue` 函数式 replace、`T9` 用例 | `node tests/js/test_issue_3694_skill_delete_confirm.js` |
| 6 | 建 PR + 评论 + A2A QA | PR / Issue 评论 | 收尾 `grape tasks complete` |

## 原子提交

1. `test(#3694): 删除确认弹窗占位符直出 先写失败用例`（红）
2. `fix(#3694): 删除确认弹窗标题插值真实技能名并去重`（绿：Vue + i18n + 缓存版本）
3. `fix(#3694): 静态入口/CSS 缓存版本联动抬档`（复审 Required：入口/CSS `?v=` + 棘轮冻结表）
4. `fix(#3694): 技能名含 $ 时按字面量替换`（复审 Optional：函数式 replace + `T9`）