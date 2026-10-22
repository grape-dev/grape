# Spec: Issue #275 — 首页切换中英文后，新弹出的 Grape 工作台不跟随切换语言

## Objective

修复官网首页（`/`，独立 index.html，localStorage key = `grape-lang`）切换中英文后，点击「进入 Grape / Enter Grape」新打开的工作台（`/app/#dash`，SPA，localStorage key = `grapeLang`）不继承首页语言的问题。

根因：两个独立页面使用**不同的 localStorage key**（`grape-lang` vs `grapeLang`），首页写入的语言偏好对工作台不可见；`detectLang()` 无 URL 前缀时 fallback 到 `grapeLang`（未设置）→ 回到默认中文。

修复策略（双管齐下，语言状态传递机制统一为：**URL `/en/` 前缀为第一优先级 + `grapeLang` localStorage 为跨页面通道**）：

1. **工作台侧（本仓库，核心修复）**：`i18n.js` 的 `detectLang()` 在 `grapeLang` 未设置时，兼容读取官网首页写入的 `grape-lang`（同源 localStorage，仅初始化时读一次，不回写）。这样首页切换语言后点「进入 Grape」，工作台（URL 无 /en/ 前缀时）自动继承首页语言。
2. **首页侧（外部官网 index.html，已上线）**：首页 JS 已把「进入 Grape」链接指向 `/app/`；语言同步依赖本次工作台侧兼容读取实现。官网源码不在本仓库（生产 www.grapedev.cn 独立部署），本仓库不做首页改动。

## 用户补充现象（验收标准 5 的来源）

> 网址为 https://www.grapedev.cn/app/#dash（缺少/en/路由前缀）。在工作台内点击 Issues Center、Project Scenes 等页签跳转正常，未因缺少/en/而回退为中文。但如果F5刷新，则页面内容会根据网址而变成中文

SPA 内部页签跳转不回退是因为 `_currentLang` 内存状态已初始化为英文；F5 刷新后内存状态丢失，`detectLang()` 按「URL 前缀 → localStorage」顺序重新检测——URL 无 `/en/`、`grapeLang`（旧 key 场景）也无值 → 回退中文。**修复后**：`grapeLang` 未设置时兼容读取 `grape-lang`，即使 URL 无前缀 + F5 刷新，语言也保持英文。

## 关于 `/en/` 路由前缀必要性的结论（代码注释 + 本文档双重说明）

- `/en/` 前缀是 `detectLang()` 的**第一优先级**（`i18n.js:320-330`），保证刷新/分享链接时语言与 URL 一致；
- 但它**不是语言切换的必要条件**：SPA 会话内 `_currentLang` 由 localStorage fallback 维护（本次修复后含 `grape-lang` 兼容读取），URL 无前缀也能正常显示英文；
- 后端 `main.py` 已有 `/en` 与 `/en/{path:path}` 路由（`main.py:1048-1068`），`/en/app/` 返回同一 index.html，无需修改；
- 语言状态在页面间的传递机制：**URL 前缀（权威，刷新/分享安全）+ localStorage `grapeLang`/`grape-lang` 兼容（跨页面继承，URL 无前缀时生效）+ cookie `grapeLang` 双写**。

## Scope

### In
- `src/api/static/js/i18n.js` `detectLang()`：增加 `grape-lang` 兼容读取（仅当 `grapeLang` 无有效值时）+ 机制注释
- `tests/js/test_issue_275_lang_inherit.js`：新增回归测试（node，mock localStorage/location，验证 6 场景）
- `docs/issue-275/spec.md`：本需求文档（语言同步机制说明，验收标准 4）

### Out
- 官网首页 index.html 源码（独立部署，不在本仓库）
- 后端路由（`/en/` 路由已存在，验证无需修改）
- 工作台内语言切换逻辑（`setLang` 已正确双写 `grapeLang` + cookie + URL，不回归）
- 新增语言支持（仅维护现有 zh/en）

## Acceptance Criteria

- [x] 首页切换为英文后（`grape-lang=en`），点击「Enter Grape」，工作台以英文呈现（detectLang 兼容读取生效）
- [x] 首页切换为中文后（`grape-lang=zh`），点击「进入 Grape」，工作台以中文呈现
- [x] 语言状态在首页与工作台之间一致传递（同源 localStorage，key 兼容桥接）
- [x] `/en/` 路由前缀必要性已在代码注释与本文档说明
- [x] URL 无 `/en/` 前缀 + F5 刷新场景：`grapeLang` 未设置时读 `grape-lang`，语言不回退中文（用户补充现象修复）
- [x] `grapeLang` 已设置时行为不变（优先级：URL > grapeLang > grape-lang > 默认 zh）
- [x] 现有测试通过（`tests/js/test_issue_273_*.js`、`test_i18n_repos_local_refresh.js` 等基线不回归）
- [x] 新增测试覆盖语言状态跨页面传递场景
