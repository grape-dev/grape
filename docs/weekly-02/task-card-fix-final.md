# 任务中心看板卡片压缩问题修复方案

## 问题描述

任务中心选择"仓库全部"时，看板视图中任务卡片（如 #126、#124、#123）被压缩成很小的高度，内容显示不全。在 Edge 浏览器上尤为明显，Chrome 上也有概率复现。

## 根因

看板视图采用嵌套 flex 布局：外层容器 → 看板行容器 → 看板列 → 列头 + 卡片容器 → 卡片。多处 flex 子项缺少 `flex-shrink:0` 和 `min-height:0`，导致 flex 引擎在空间不足时按比例压缩子项高度，卡片被压扁。

具体问题点：

1. **看板卡片**：`flex-shrink` 默认为 1，任务多时被按比例缩小
2. **看板行容器**：缺少 `flex-shrink:0`，被父级 `flex:1;min-height:0` 压缩
3. **看板列**：缺少 `flex-shrink:0` 和 `height:100%`，高度不确定
4. **卡片容器**（列内滚动区）：缺少 `min-height:0`，Edge/Chromium 下 flex 子容器不会触发 `overflow-y:auto` 滚动，而是撑开父级导致布局塌陷

## 修改清单

文件：`src/api/static/js/pages/tasks.js`

### 修改 1：看板卡片加 flex:0 0 auto + min-height

位置：`_renderBoard()` 函数内，约第 497 行

修改前：
```js
style="position:relative;background:var(--c,#fff);border:0.5px solid ..."
```

修改后：
```js
style="position:relative;flex:0 0 auto;min-height:76px;background:var(--c,#fff);border:0.5px solid ..."
```

说明：`flex:0 0 auto` 等价于 flex-grow:0 + flex-shrink:0 + flex-basis:auto，卡片不增长不缩小，按内容撑高度。`min-height:76px` 兜底确保最小可读高度。

### 修改 2：看板行容器加 flex-shrink:0

位置：`_renderBoard()` 函数内，约第 471 行

修改前：
```js
let html = '<div style="display:flex;gap:10px;height:100%;overflow-x:auto;padding-bottom:4px">';
```

修改后：
```js
let html = '<div style="display:flex;gap:10px;height:100%;overflow-x:auto;padding-bottom:4px;flex-shrink:0">';
```

说明：防止看板行容器被父级 `flex:1;overflow:auto;min-height:0` 压缩。

### 修改 3：看板列加 flex-shrink:0 + height:100% + overflow:hidden

位置：`_renderBoard()` 函数内，约第 476 行

修改前：
```js
html += `<div style="min-width:240px;max-width:300px;flex:1 1 240px;display:flex;flex-direction:column;background:rgba(0,0,0,.02);border-radius:14px;border:0.5px solid var(--b,#e8e8ed)${dim}">`;
```

修改后：
```js
html += `<div style="min-width:240px;max-width:300px;flex:1 1 240px;flex-shrink:0;display:flex;flex-direction:column;background:rgba(0,0,0,.02);border-radius:14px;border:0.5px solid var(--b,#e8e8ed);height:100%;overflow:hidden${dim}">`;
```

说明：`flex-shrink:0` 防止列被压缩，`height:100%` 让列占满看板行高度，`overflow:hidden` 配合列内卡片容器的 `overflow-y:auto` 实现列内滚动。

### 修改 4：卡片容器加 min-height:0

位置：`_renderBoard()` 函数内，约第 484 行

修改前：
```js
html += `<div style="flex:1;overflow-y:auto;padding:0 8px 8px;display:flex;flex-direction:column;gap:6px">`;
```

修改后：
```js
html += `<div style="flex:1;overflow-y:auto;min-height:0;padding:0 8px 8px;display:flex;flex-direction:column;gap:6px">`;
```

说明：`min-height:0` 是 Edge/Chromium flex 布局的关键。默认 flex 子项的 `min-height` 为 `auto`（以内容高度为准），导致 `overflow-y:auto` 无法触发滚动，而是撑开父级。设为 0 后，flex 引擎允许该容器收缩到内容高度以下，触发滚动条。

### 修改 5：版本号升级

文件：`src/api/static/index.html`

```
tasks.js?v=79 → tasks.js?v=82
app.css?v=52 → app.css?v=54
```

### 修改 6：CSS 补充（app.css 末尾追加）

```css
/* 任务卡片雷达扫描动画（进行中状态） */
.tsk-card-radar{position:relative;overflow:hidden}
.tsk-radar-sweep{position:absolute;inset:0;border-radius:10px;pointer-events:none;animation:radarRotate 3s linear infinite}
@keyframes radarRotate{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
```

### 修改 7：删除过宽的 CSS 兄弟选择器（app.css）

删除第 307 行：
```css
.tsk-list .tsk-list-sec:first-child ~ .tsk-row{border-top:none}
```

说明：通用兄弟选择器 `~` 移除了第一个分区标题后所有 `.tsk-row` 的上边框，导致列表视图多分区时卡片无分隔线。

## 验证方式

1. 任务中心选择"仓库全部"
2. 查看看板视图"处理中"列
3. 确认任务卡片高度正常，内容完整显示（issue 编号、标题、assignee、日期）
4. 确认卡片多时列内出现滚动条，卡片不被压缩
5. 在 Edge 和 Chrome 两个浏览器上分别验证
6. 切换到列表视图，确认卡片间有正常分隔线

## 注意事项

- 双主（master-001 + master-002）需同步修改，建议修改后用 `md5sum` 校验两台文件一致
- 修改后需强制刷新（Ctrl+F5）清除浏览器缓存
- `min-height:0` 是本次修复的关键点，之前只加了 `flex-shrink:0` 但 Edge 下仍然压缩，就是因为缺少 `min-height:0` 导致 flex 容器无法触发滚动
