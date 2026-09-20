# Grape 自动化任务列表页重构方案（参照 multica）

## 1. 当前问题

- 使用 `<table table-layout:fixed>`，名称列占用太大，操作列 3 个按钮溢出
- 列宽硬编码（width:30px/100px/120px），不灵活
- 操作按钮平铺（▶ ✎ 🗑），占 80px 还不够
- 行高不统一，视觉不齐

## 2. multica 设计参考

### 2.1 布局结构
- **CSS Grid**（非 table）：`grid-cols-[checkbox 28px | name minmax(200px,1fr) | scene 100px | cron 130px | lastRun 120px | nextRun 110px | status 70px | actions 36px]`
- **固定行高 48px**：所有行统一高度
- **hover 显示操作**：默认隐藏，鼠标悬停时显示

### 2.2 操作列
- 单个 kebab 按钮（⋯），点击弹出下拉菜单
- 菜单项：立即触发 / 编辑 / 删除
- 仅占 36px 宽度

### 2.3 列宽分配
| 列 | 宽度 | 说明 |
|----|------|------|
| checkbox | 32px | 批量选择 |
| name | minmax(160px,1fr) | 弹性宽度，truncat |
| scene | 90px | 场景ID |
| cron | 130px | cron表达式 |
| lastRun | 110px | 相对时间 |
| nextRun | 110px | 绝对时间 |
| status | 60px | 启用/禁用 |
| actions | 36px | kebab菜单 |

## 3. 实现方案

### 3.1 表格容器
```html
<div style="display:grid;grid-template-columns:32px minmax(160px,1fr) 90px 130px 110px 110px 60px 36px;gap:0">
  <!-- header -->
  <!-- rows -->
</div>
```

### 3.2 行渲染
- 每行是一个 grid 子项，用 `display:contents` 或直接 grid-row
- 行高固定 44px，border-bottom 分隔
- hover 背景色变化
- 操作列默认 opacity:0，hover 显示

### 3.3 kebab 菜单
- 点击 ⋯ 按钮弹出绝对定位下拉菜单
- 菜单项：立即触发 / 编辑 / 删除（红色）
- 点击外部关闭

## 4. 改动文件

| 文件 | 改动 |
|------|------|
| autopilot.js | 重写 _apListHTML + _apRenderTable，grid布局 + kebab菜单 |
| docs/weekly-03/autopilot-schedule-editor-redesign.md | 更新设计文档 |
