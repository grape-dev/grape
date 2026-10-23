# 认证页面 i18n 零闪动修复 — 设计文档（最终版）

> **方案**：defer + 服务端注入翻译数据 + 内联 early-translate script（业界方案四：同步初始化 + 预加载资源）
> **对应需求**：F25-F28（P28/P29）

## 一、问题本质

英文用户的 `data-i18n-pending="1"` 在 `<head>` 内联 script 中设置，CSS `[data-i18n-pending="1"] [data-i18n]{visibility:hidden}` 在首次绘制前生效——所有 `[data-i18n]` 元素 `visibility:hidden`。文字要显示必须等 `I18N.init()` → `applyI18nToDOM()` → 移除 pending，但 init 依赖：
1. i18n.js 外部文件下载（网络延迟）
2. DOM 解析完成（body 中元素就绪）
3. DOMContentLoaded 触发（等同步脚本 + 样式表）

三个条件满足远晚于首次绘制——英文用户看到"空白→英文"闪动。

## 二、最终方案

### 2.1 三层架构

| 层 | 组件 | 作用 | 时机 |
|----|------|------|------|
| 1 | `__I18N_BOOST__` 注入 | 翻译数据就绪 | `</head>` 前（HTML 解析早期） |
| 2 | early-translate script | 翻译 DOM + 移除 i18n-loading | `</body>` 前（HTML 解析完，零网络延迟） |
| 3 | i18n.js defer + 自动 init | 全量翻译 + 异步加载全量包 | DOM 解析后、DOMContentLoaded 前 |

层 2 是关键——它不依赖 i18n.js 外部文件下载，用层 1 已就绪的 boost 数据在 HTML 解析阶段完成翻译。

### 2.2 服务端注入翻译数据（`__I18N_BOOST__`）

main.py `_get_i18n_boost()` 启动时读取 locale JSON，提取 91 个 auth 页面 `[data-i18n]` key，缓存为双语 JSON（~5KB）。

提取逻辑覆盖三种 key 结构：
- 嵌套 key：`app.login.xxx` → `app_data['login']['xxx']`
- 顶层字符串 key：`app.back_home` → `app_data['back_home']`
- 跨文件 key：`organizations.xxx` → `organizations.json`

构建失败时 try-except 返回 `'{}'`——early-translate 检测空数据直接 return。

### 2.3 内联 early-translate script

main.py 在 `</body>` 前注入内联 `<script>`（不是外部文件，零网络延迟）。HTML 解析到 `</body>` 时 body 中所有 `[data-i18n]` 元素已解析完。

script 逻辑：
1. 检测 `__I18N_BOOST__` 是否存在 → 不存在直接 return
2. 读 `localStorage.grapeLang` 检测语言
3. 中文用户：直接移除 `i18n-loading` class
4. 英文用户：用 boost 数据翻译 `[data-i18n]` + `[data-i18n-placeholder]` → 移除 `i18n-loading`

### 2.4 i18n.js defer + 自动 init

三页面统一 `<script defer src="i18n.js">` 放 `<head>`。defer 并行下载不阻塞解析，在 DOM 解析完成后、DOMContentLoaded 前执行。

i18n.js 改动：
- `_initialized` 标志防重复 init
- 末尾自动 init（检查 `window.__I18N_CACHE_VER__` 是否设置——仅 defer 模式触发）
- `init()` 读取 `__I18N_BOOST__` + `_deepMerge` 合并到 `_packs`
- `applyI18nToDOM()` 移除 `i18n-loading` class（兼容旧 `data-i18n-pending` attribute）
- 全量包加载后再次 `applyI18nToDOM()` 补全 boost 未覆盖的 key

### 2.5 CSS class 替代 attribute

`data-i18n-pending="1"` → `html.i18n-loading` class。5 秒超时兜底在内联 `<script>` 中设置。

### 2.6 执行时序

```
1. HTML 解析 <head>：i18n-loading class + __I18N_BOOST__ 注入 + defer i18n.js 开始下载
2. HTML 解析 <body>：[data-i18n] 元素 visibility:hidden
3. </body> 前 early-translate 同步执行：翻译 + 移除 i18n-loading
4. 首次绘制：英文文字已就绪（零网络延迟）
5. defer i18n.js 执行：自动 init + applyI18nToDOM（重复翻译但内容一致）
6. DOMContentLoaded：业务 JS 执行
7. 异步：全量 locale JSON 加载 → applyI18nToDOM 补全
```

间隙 = 步骤 2 到步骤 3 = HTML 解析时间（纯本地，零网络延迟）。

## 三、边界条件

| 编号 | 条件 | 处理 | 验证 |
|------|------|------|------|
| BC-1 | defer 保证 DOM 解析完才执行 | querySelectorAll 能找到所有元素 | ✅ |
| BC-2 | auth.js `typeof I18N` 防御 | defer 未执行时跳过 init | ✅ |
| BC-3 | api.js I18N.t 全在异步回调 | 执行时 I18N 已加载 | ✅ |
| BC-4 | i18n.js 加载失败 | 5 秒超时兜底移除 i18n-loading | ✅ |
| BC-5 | 重复 init | _initialized 标志防重复 | ✅ |
| BC-6 | 动态内容翻译 | setLang → applyI18nToDOM | ✅ |
| BC-7 | cacheVer 参数 | window.__I18N_CACHE_VER__ | ✅ |
| BC-8 | boost 与全量包合并 | _deepMerge + 全量包覆盖 | ✅ |
| BC-9 | 中文用户不注入 boost | i18n-loading 不设置，元素始终可见 | ✅ |
| BC-10 | admin.html 不受影响 | FileResponse 不走 _serve_auth_page，__I18N_CACHE_VER__ 未设置 → 自动 init 不触发 | ✅ |
| BC-11 | boost 构建失败 | try-except 返回 '{}'，early-translate 跳过 | ✅ |
| BC-12 | OAuth 模式 initOAuthMode() Auth.t 调用 | 延迟到 DOMContentLoaded | ✅ |
| BC-13 | index.html 30 个 body 脚本 defer 化 | 依赖链顺序不变、DOMContentLoaded 前执行完、无 document.write、抢跑窗口文字已翻译 | ✅ |

## 四、影响文件

| 文件 | 改动 |
|------|------|
| `src/api/main.py` | `_get_i18n_boost()` + `_EARLY_TRANSLATE_SCRIPT` + `_inject_email_configured` 注入 head + body |
| `src/api/static/js/i18n.js` | `_initialized` + 自动 init + `__I18N_BOOST__` 读取 + `_deepMerge` + `classList` |
| `src/api/static/register.html` | defer + `i18n-loading` CSS + 超时兜底 + `__I18N_CACHE_VER__` |
| `src/api/static/forgot.html` | 同上 |
| `src/api/static/index.html` | 同上 + body 30 个同步脚本 defer 化（F29） |

不改动：auth.js、api.js、locale JSON、admin.html

## 五、测试

### 5.1 静态断言（T46-T58）

- T46: main.py 含 `__I18N_BOOST__` + `_EARLY_TRANSLATE_SCRIPT` + querySelectorAll + classList.remove
- T47: i18n.js 含 `_initialized` 防重复
- T48: i18n.js 含自动 init readyState 检查
- T49: i18n.js 含 `__I18N_BOOST__` 读取 + `_deepMerge`
- T50: 三页面 i18n.js 标签含 defer
- T51: 三页面 CSS 含 `html.i18n-loading`
- T52: 三页面含 5 秒超时兜底
- T53: 三页面含 `__I18N_CACHE_VER__`
- T54: i18n.js applyI18nToDOM 移除 i18n-loading class
- T55: main.py 含 `_EARLY_TRANSLATE_SCRIPT` + querySelectorAll + classList.remove
- T56: main.py boost 构建含 try-except 兜底返回 '{}'
- T57: main.py boost 提取覆盖三种 key 结构
- T58: early-translate script 处理 data-i18n-placeholder
- T59: index.html body 中所有 `<script src>` 含 defer（无同步阻塞脚本）（F29）

### 5.2 场景用例（U11-U12）

- U11-1~U11-7: defer + 自动 init + 防重复 + 超时兜底 + typeof 防御
- U12-1: early-translate 在 `</body>` 前同步翻译
- U12-2: boost 覆盖全部 91 个 data-i18n key
- U12-3: boost 含跨文件 key（organizations.xxx）
- U12-4: boost 构建失败返回 '{}'
- U12-5: early-translate 处理 data-i18n-placeholder
- U12-6: admin.html 不受影响
- U12-7: 英文用户冷加载登录页（30 个脚本 defer 化，early-translate 首绘前执行）

## 六、为什么这是最优方案

| 方案 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| **defer + 服务端注入 early-translate** | 翻译数据零延迟就绪 + 内联 script 零延迟翻译 + defer 不阻塞解析 | 需服务端注入 ~5KB JSON | ✅ 采用 |
| 纯 defer（无 early-translate） | 简单 | 仍依赖 i18n.js 下载完成才能翻译 → 首次绘制空白 | ❌ 间隙未消除 |
| 移到 body 前 + 立即 init | 简单 | 仍阻塞解析（同步下载） | ❌ 打补丁 |
| SSR/SSG | 最彻底 | 需大规模架构改造 | ❌ 不现实 |
| Loading Gate | 体验好 | 认证页面过重 | ❌ 过度设计 |
| 内联整个 i18n.js | 彻底 | 20KB 代码重复 | ❌ 维护困难 |
