# Grape Logo 更换方案

> 设计方案 | 2026-09-17 | 涉及文件域：`src/api/static/` | 状态：✅ 已执行并二次替换（2026-09-17，最终源图 `D:\grape项目\logo\new_grape_logo\grape_log1.png`）

## 1. 目标

将 Grape 品牌 logo（改为新图）替换为新的 brand 图，只动 Grape 自己的品牌 logo，保留第三方平台/引擎图标不动。

## 2. Logo 资产清单（需要更换的）

Grape 自己的品牌 logo 共 **3 个文件**，全部位于 `src/api/static/`，内容相同只是分辨率/大小不同：

| # | 文件 | 旧尺寸/大小 | 用途 | 引用位置 |
|---|------|------------|------|---------|
| 1 | `src/api/static/logo.png` | 2048×2048 / 2.3MB → **已换 1024×1024 / 108.6KB** | Lobby 页头品牌图（仅 30×30 展示位） | `src/api/static/js/pages/repos.js:16` |
| 2 | `src/api/static/logo-128.png` | 128×128 / 16.9KB → **已换 128×128 / 10.7KB** | 登录页 / 内测申请页 logo | `index.html:21`、`beta-apply.html:43` |
| 3 | `src/api/static/logo-64.png` | 64×64 / 4.9KB → **已换 64×64 / 4.5KB** | favicon + 侧边栏 logo 小标 | `index.html:7,40`、`beta-apply.html:7`、`help_page_gen.py:369`（生成 `help.html:7`）|

> 新源图 1889×2269（内容 1315×1895，非正方形），三个目标均为正方形画布 contain-fit 居中生成，葡萄整体不变形不被裁切。

### CSS 样式（无需改图，仅说明展示尺寸）

| 文件 | 行 | 类 | 展示规格 |
|------|-----|-----|---------|
| `src/api/static/css/app.css` | 1 | `.lbox .logo` | 96×96、圆角 24px（登录框） |
| `src/api/static/css/app.css` | 9 | `.lobby-logo` | 30×30、圆角 9px（Lobby 页头） |
| `src/api/static/css/app-theme.css` (apple-theme.css) | 84 | `[data-theme="apple"] .lbox .logo` | Apple 主题覆盖 |

## 3. 不在本次更换范围

以下均**不是** Grape 品牌 logo，换 Grape logo 时不动：

- 平台图标：`src/api/static/img/{feishu,wecom,dingtalk}.svg`（IM 接入）、`github.svg`/`gitcode.svg`（未引用的存量文件）
- 引擎水印：`src/api/static/img/engines/{opencode,Hermes,codearts,codex}.{png,jpg,svg}`（runtimes.js 引擎卡片水印）
- `index.html:25,29`、`js/app.js:392-394` 内联的 GitHub/GitCode SVG 品牌标
- 设计稿原型（docs/new-version*/ 下的 prototype.html）属历史文档，不回改

## 4. 更换方案

### 4.1 文件替换方式（推荐：同名覆盖，不动引用）

沿用现有 3 文件命名，避免批量改 6 处引用点：

| 步骤 | 内容 |
|------|------|
| 1 | 拿到新 logo 源文件（建议 SVG/PSD/AI，1:1 方形） |
| 2 | 导出 `logo.png` 为标准清晰度原图（若原图 >200KB，输出压缩版；不推荐 2.3MB 大图） |
| 3 | 导出/缩放 `logo-128.png`（128×128，目标 ≤20KB） |
| 4 | 导出/缩放 `logo-64.png`（64×64，目标 ≤6KB） |
| 5 | 检查 favicon 是否需另出 `.ico`——当前用的就是 `logo-64.png`（`rel="icon"`），无需额外 ico 文件 |

覆盖后无需改任何 HTML/JS/CSS 引用。

### 4.2 顺带修复：Lobby 页头 2.3MB 大图

`repos.js:16` 的 `<img class="lobby-logo" src="/static/logo.png">` 在 30×30 展示位上加载 2.3MB 原图。新方案里 `logo.png` 直接换成压缩版（或该处改引 `logo-64.png`），一举消除首屏大图开销。

### 4.3 可选：帮助页 favicon 依赖生成器

`src/help_page_gen.py:369` 写死 `/static/logo-64.png`，`help.html` 由它启动时重新生成（help.html 本身 gitignore）。**同名覆盖方案下无需改动**；若换了文件名才需要同步改这里。

## 5. 改动清单

同名覆盖方案的实际改动：

| 文件 | 改动 |
|------|------|
| `src/api/static/logo.png` | ✅ 已替换为新图（1024² / 108.6KB，解决 Lobby 2.3MB 大图问题） |
| `src/api/static/logo-128.png` | ✅ 已替换为新图（128² / 10.7KB） |
| `src/api/static/logo-64.png` | ✅ 已替换为新图（64² / 4.5KB，favicon + 侧边栏小标） |
| `src/api/static/js/pages/repos.js:16` | ✅ Lobby 页头已改引 `logo-64.png`（30×30 展示位不再请求大图） |
| `docs/weekly-03/logo-replacement-design.md` | ✅ 本文档 |

代码引用（`index.html`、`beta-apply.html`、`help_page_gen.py`、`app.css`、`apple-theme.css`）在非改名方案下**零改动**。

## 6. 更换后检查清单

- [x] 登录页 logo（96×96 圆角）显示新图
- [x] 侧边栏小标 + 浏览器标签页 favicon（logo-64）显示新图
- [x] Lobby 页头 logo 显示新图，且不再请求 2.3MB 原图（改引 logo-64.png）
- [x] 内测申请页 `beta-apply.html` logo 显示新图
- [ ] 帮助中心 `help.html` favicon 显示新图（重启服务重新生成后检查）
- [x] 三张图尺寸：128→10.7KB、64→4.5KB、logo.png→108.6KB（均 ≤ 原清晰度要求）
- [ ] 图片为正方形、透明底，浅色/深色主题下均可见（apple 主题覆盖 `.lbox .logo` 也要过一遍）
- [ ] `tests/ui_test_playwright.py`、`tests/ui_scenario_test.py` 既有用例通过

## 7. 不做的事

- 不改品牌文案（`<title>`、`brand_sub` i18n、FastAPI `title="Agentic DevOps API"` 等遗留命名本期不动）
- 不强求产 `.ico` favicon——现有 `logo-64.png` 已可用
- 不动第三方平台/引擎图标与内联 GitHub/GitCode SVG
- 不回改 docs/new-version*/ 历史原型图