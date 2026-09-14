# 「任务中心」→「需求中心（Issues Center）」改名设计方案

> 目标：将前端「任务中心」页面更名为「需求中心」（英文 Issues Center），与当前以 Issue 为中心的语义对齐。
> 背景：`/api/issues` 对外接口已落地、`tasks` 表已收编为 `issue` 单表（见 `docs/weekly-03/issues-api-design.md`），
> 但前端页面仍叫「任务中心（Task Center）」，与实际语义（管理 GitCode Issue/需求）脱节。
> 分支：`weekly-02-lyz`；状态：设计草案；日期：2026-09-14

---

## 一、现状：页面名称来源

「任务中心」在前后端的位置：

| 位置 | 文件 | 当前值 | 说明 |
|------|------|--------|------|
| 侧边栏菜单 | `src/api/static/index.html:52` | `data-i18n="app.nav.tasks"` | 菜单项，文案从 app.json 读 |
| 菜单文案（zh） | `src/api/static/locales/zh/app.json` `nav.tasks` | `任务中心` | 侧边栏显示名 |
| 菜单文案（en） | `src/api/static/locales/en/app.json` `nav.tasks` | `Task Center` | 侧边栏显示名 |
| 页面标题（zh） | `src/api/static/locales/zh/tasks.json` `page_title` | `任务中心` | 页面头部大标题 |
| 页面标题（en） | `src/api/static/locales/en/tasks.json` `page_title` | `Task Center` | 页面头部大标题 |
| 页面 JS 注释 | `src/api/static/js/pages/tasks.js:1` | `// ====== 任务中心（Account ▸ 任务中心）======` | 注释（可选更名） |

**i18n 机制**：侧边栏用 `data-i18n="app.nav.tasks"` → 读 `locales/{zh,en}/app.json` 的 `nav.tasks`；
页面内标题用 `I18N.t('tasks.page_title')` → 读 `locales/{zh,en}/tasks.json` 的 `page_title`。

---

## 二、目标文案

| 语言 | 旧（任务中心） | 新（需求中心） |
|------|--------------|----------------|
| 中文 | 任务中心 | **需求中心** |
| 英文 | Task Center | **Issues Center** |

> 说明：本次仅更新**用户可见文案**（菜单项 + 页面标题）。

---

## 三、改动清单

| # | 文件 | 改动 | 风险 |
|---|------|------|:---:|
| 1 | `src/api/static/locales/zh/app.json` | `nav.tasks`: `任务中心` → `需求中心` | 低（纯文案） |
| 2 | `src/api/static/locales/en/app.json` | `nav.tasks`: `Task Center` → `Issues Center` | 低 |
| 3 | `src/api/static/locales/zh/tasks.json` | `page_title`: `任务中心` → `需求中心` | 低 |
| 4 | `src/api/static/locales/en/tasks.json` | `page_title`: `Task Center` → `Issues Center` | 低 |
| 5 | `src/api/static/js/pages/tasks.js:1` | 顶部注释同步为「需求中心（Issues Center）」（可选，只影响代码可读性） | 低 |

> 另外检查（见 §五）：聊天跳转文案、README/文档中的「任务中心」字样（不改动接口/路由，仅文案位置确认）。

---

## 四、不改动项（重要）

- **路由/view key 保持 `tasks`**：`app.nav('tasks')`、`view==='tasks'`、`window._currentView`、`#tasks` hash 均**不变**。
  只改显示文案，不动导航标识符，避免波及 app.js nav 逻辑 / 收藏 hash / 其它页面跳转。
- **后端 `/api/tasks/*` 接口不变**：`mine/repo/{owner}/{repo}/complete/...` 等端点与契约保持不变
  （接口「任务中心列表」语义仍由 tasks 路由承载；前端只是改了叫法）。
  ⚠️ 本方案**不**把 view key / API 前缀改名——那属于更大的重构，且 `issues` view/API 已另有用途
  （`/api/issues` 是只读聚合，tasks view 是任务中心操作台），两者不合并。
- **`app.nav.tasks` key 名不变**：避免重命名 JSON key 波及 i18n.js 的 key 列表与所有 `data-i18n` 引用。

---

## 五、需人工核对的其他「任务中心」字样（不在本次自动改）

以下位置含「任务中心/Task Center」字样，**语义是描述性引导文案**，需人工判断是否随需求中心改名：

| 位置 | 内容 | 建议 |
|------|------|------|
| `src/api/static/js/pages/chat.js:186,190,226` | 注释/日志「任务中心跳转」 | 可改注释（低优先） |
| `src/api/routes/tasks.py` docstring | 「任务中心」多处 | 代码注释，可后续统一 |
| `docs/openapi/01-tasks.md` / `help/api/*` | 接口文档标题「Tasks 任务接口」 | 接口文档描述 `tasks` 路由，保持「Tasks」命名（API 语义），本轮不动 |
| `tasks/todo.md`、README、help/usage | 产品文档「任务中心」 | 是否改名由产品/文档owner决定（本方案聚焦前端 UI 文案） |

---

## 六、验证

1. 改文案后提升 `src/api/static/index.html` 中的 JS/CSS 缓存版本号（`?v=`），确保浏览器加载新版文案。
2. 手工验证：登录 → 侧边栏显示「需求中心」；进入该页头部显示「需求中心」。
3. 切英文 → 显示「Issues Center」。
4. 回归：`app.nav('tasks')` 跳转、看板/列表渲染、筛选/搜索正常（无 JS/键位改动）。

---

## 七、备选方案（不在本次范围）

- **彻底更名 view key `tasks` → `issues`**：会波及 `app.js` VIEWS、hash、chat/repos 等十几处跳转 +
  `_currentView` 判断 + tasksApi，破坏面大；且 `/api/issues`（只读）+ tasks view（操作台）语义本就分离，
  强行同名易混淆。**不推荐**，除非产品明确要求「端点与页面完全统一为 issues」。
- **页面文件重命名 `tasks.js` → `issues.js`**：纯代码组织调整，无用户可见收益，风险大于收益，不推荐。