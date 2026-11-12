# Issue #1932 需求文档（Spec）

> 来源：Issue agent Spec（issue discussion 中的需求说明），本文件为开发侧落档。

## 目标
workers 设备页卡片「承载引擎」一栏仅保留 `引擎名 v版本 健康标记`，去掉引擎名后的括号内容（凭据名）。

## 现状
- `src/api/static/js/pages/runtimes.js` → `renderWorkers()` L725：承载引擎项取 `rt.alias || engineDisplayName(rt)`；`engineDisplayName()`（worker-name.js，#1310）输出「引擎名（凭据名）」。
- 设备卡片头部已展示设备名，括号内容属重复信息且导致行变长易折行。

## 期望行为
- 承载引擎行：`引擎名 v版本 ✓/✗`，多项仍以 ` ·` 分隔，不出现全角/半角括号内容。
- 点击跳转引擎详情（openEngineDetail）、版本号、健康标记、排序/筛选逻辑保持不变。
- alias 仍优先展示（alias 本身不含括号）。
- 仅改 workers 卡片该处渲染；不改公共 `engineDisplayName()`，Engines 子页卡片与引擎详情弹窗命名口径不变。

## 验收标准
- [x] workers 设备卡片「承载引擎」行无任何「（…）」「(…)」括号内容。
- [x] 引擎名、版本号、✓/✗ 标记、` · ` 分隔与点击跳转行为不变。
- [x] Engines 子页卡片、引擎详情弹窗命名展示无变化。

## 优先级
P2（纯展示优化）

## 测试 seam（公开行为）
- `renderWorkers({}, [devices])` 输出 HTML：承载引擎 chip 文本（用户可见行）。
- 回归守卫：`renderEngines` / `_renderEngineDetail` 输出仍为「引擎名（凭据名）」口径。
