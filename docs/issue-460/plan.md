# Plan: Issue #460

## 技术方案
1. **i18n**：`tasks.json`（zh/en）的 `create` 对象新增 `sync_title_to_desc` 键
   - zh: `同步标题到描述`
   - en: `Sync title to description`
2. **DOM 渲染**：`openTaskCreateModal` 描述标签 div 改为 flex，在 `*` 后插入 icon button（↧ 符号），title/aria-label 走 `I18N.t('tasks.create.sync_title_to_desc')`
3. **点击逻辑**：新增全局函数 `syncTitleToDesc()`
   - 读 `#tcCreateTitle` trim → 空 return
   - 读 `#tcCreateDesc` → `includes` 幂等去重 → 追加（空行分隔）

## 影响范围
| 文件 | 改动 |
|------|------|
| `src/api/static/locales/zh/tasks.json` | create 对象 +1 键 |
| `src/api/static/locales/en/tasks.json` | create 对象 +1 键 |
| `src/api/static/js/pages/issues.js` | openTaskCreateModal desc label +1 icon；+1 函数 syncTitleToDesc |
| `tests/js/test_issue_460_sync_title_to_desc.js` | 新增测试 |

## 风险
- 无后端/接口变更，纯前端 UI + 逻辑
- 幂等用 includes 简单判断，边界可接受（spec 已确认）
