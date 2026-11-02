# Plan: Issue #645

## 改动文件
| 文件 | 改动 |
|------|------|
| `src/api/static/js/pages/skills.js` | 6 处 `I18N.t(key)\|\|'兜底'` → `I18N.t(key, null, '兜底')`；`showSkillUpdate` 传参改 `s.display_name \|\| s.name`（含 `'` 转义） |
| `src/api/static/locales/zh/skills.json` | 新增 `action.update_skill/confirm_update`、`update.title/tip`、`toast.update_success/failed` |
| `src/api/static/locales/en/skills.json` | 同上英文文案 |
| `tests/js/test_issue_645_skill_update_fallback.js` | 回归测试（新增） |

## 实施顺序（TDD）
1. RED：先写测试 `tests/js/test_issue_645_skill_update_fallback.js`，断言 6 处第三参 fallback、showSkillUpdate 传参、zh/en 键齐全、键缺失返回 fallback —— 初跑失败。
2. GREEN：修改 `skills.js` + 两个 locale JSON，测试全绿。
3. REFACTOR：`toast.update_failed` 键不带 `{{msg}}`（调用点 `e.message ||` 已先取具体信息，避免占位符裸奔）。

## 风险与边界
- `I18N.t` 第三参签名已确认存在（`i18n.js:447`，查找顺序：当前语言 → 默认语言 → fallback → 键名），无兼容风险。
- `showSkillUpdate` 单引号转义与现有 `deleteSkill('${s.id}','${s.scope}')` 风格一致并加强。
- 不改后端、不改其它页面，范围仅 Issue #645 涉及的更新弹窗链路。
