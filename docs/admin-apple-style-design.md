# 运营管理台苹果风格样式优化方案（最终版）

> 分支：`feat/admin-style-optimization`（基于 dev `8f500c0f`）
> 范围：只改 CSS + 布局间距，不改业务逻辑、不改后端、不改 HTML 结构

## 设计目标

将 admin 运营管理台的元素布局和视觉风格对齐 Apple Design——克制、留白、层次感、毛玻璃、大圆角、柔和阴影。布局上学习苹果官网/Settings App 的宽松间距和卡片化布局。

## 苹果风格布局原则

1. **间距宽松**：页面 padding 32px、卡片间距 20px、卡片内 padding 20-24px（当前偏紧凑 16-18px）
2. **层次分明**：背景灰 #f5f5f7 → 白卡 #fff → 内嵌浅灰区 #fafafa
3. **大圆角**：卡片 16px、按钮 10px、输入框 10px、KPI 卡片 14px
4. **毛玻璃**：顶栏 + 弹窗遮罩用 backdrop-filter
5. **柔和阴影**：极轻 `0 2px 12px rgba(0,0,0,0.04)`，hover 加深 `0 8px 24px rgba(0,0,0,0.08)`
6. **极淡分隔线**：rgba(0,0,0,0.06)（当前 #e8e8e8 偏深）
7. **字体层级**：大标题 24px 半粗体、中标题 17px 半粗体、正文 14px 常规、辅助 12px 浅灰
8. **交互反馈**：hover translateY(-1px) + 阴影加深 + 0.2s 缓动
9. **选中态**：胶囊背景 rgba(0,107,255,0.08) + 文字蓝色（不用 border-bottom）
10. **留白呼吸**：每个区块之间至少 20px gap，表格行高至少 14px padding

## 改动点（35 个）

### 一、全局基础（3 个）

1. **:root 变量**——苹果色板
```css
--text-primary: #1d1d1f;        /* 苹果深灰 */
--text-secondary: #6e6e73;      /* 苹果中灰 */
--text-tertiary: #aeaeb2;       /* 苹果浅灰 */
--bg-body: #f5f5f7;             /* 苹果背景灰 */
--border-color: rgba(0,0,0,0.08);
--border-soft: rgba(0,0,0,0.04);
--card-shadow: 0 2px 12px rgba(0,0,0,0.04);
--card-shadow-hover: 0 8px 24px rgba(0,0,0,0.08);
--success: #34c759;  --warning: #ff9500;  --danger: #ff3b30;
--radius: 12px;  --radius-lg: 16px;  --radius-sm: 8px;
--transition: 0.2s cubic-bezier(0.25, 0.1, 0.25, 1);
```

2. **body font-family**——加 SF Pro 优先级
3. **::-webkit-scrollbar**——半透明 thumb + hover 加深

### 二、导航（4 个）

4. **.topnav**——毛玻璃 `backdrop-filter: saturate(180%) blur(20px)` + rgba(255,255,255,0.72) + 高度 56px
5. **.topnav-link**——hover rgba(0,0,0,0.04)，选中胶囊背景 rgba(0,107,255,0.08) + 圆角 8px + 文字蓝（去掉 border-bottom）
6. **.topnav-user**——同 topnav-link hover 风格 + 圆角 8px
7. **.drawer / .drawer-item**——圆角 8px item + hover 浅灰 + 选中胶囊

### 三、页面结构（6 个）

8. **.page-container**——padding 加大 32px 40px
9. **.page-header**——padding 加大 20px 40px + h1 字号 20px + 底部分隔线极淡
10. **.section-header / .sh-title**——sh-title 16px 半粗体 + sh-count 柔和灰 #aeaeb2
11. **.card**——圆角 16px + 柔和阴影 + hover 上浮
12. **.device-card**——同 card + hover 上浮 + padding 20px
13. **.device-grid / .agent-grid**——gap 加大 16px → 20px

### 四、表格（3 个）

14. **.vod-table th**——透明背景 + 12px 大写 + 字间距 0.5px + 颜色 #6e6e73 + padding 12px 16px
15. **.vod-table td**——padding 14px 16px + 极淡底边线 rgba(0,0,0,0.04)
16. **.vod-table tr:hover**——rgba(0,107,255,0.02)

### 五、按钮（3 个）

17. **.btn / .btn-primary / .btn-danger / .btn-sm**——圆角 8px + hover 上浮 + primary 蓝色阴影
18. **.act-icon-btn**——hover 蓝底 rgba(0,107,255,0.08) + 圆角 6px + padding 6px
19. **.page-btn**——圆角 8px + hover 蓝色背景

### 六、输入/筛选（4 个）

20. **.toolbar input / .toolbar select / .form-row input / .page-size-select**——圆角 10px + 聚焦蓝色边框 + box-shadow rgba(0,107,255,0.1) + padding 8px 12px
21. **.col-filter-drop**——圆角 10px + 柔和阴影 0 4px 16px rgba(0,0,0,0.08)
22. **.col-filter-opt**——hover rgba(0,0,0,0.04) + active 蓝底 + 圆角 6px
23. **.filter-chip**——胶囊 border-radius 20px + 半透明背景 + 选中蓝底

### 七、数据展示（4 个）

24. **.stat-card**——圆角 14px + 柔和阴影 + padding 18px 20px + 数值 24px 700
25. **.kpi-card**——圆角 14px + 柔和阴影 + padding 20px + hover 上浮 + 左边框改 4px
26. **.status-tag**——半透明背景（success rgba(52,199,89,0.1) / danger rgba(255,59,48,0.1) / warning rgba(255,149,0,0.1) / info rgba(0,107,255,0.08)）+ 圆角 6px
27. **.badge**——圆角胶囊 border-radius 12px + 半透明背景

### 八、分页（1 个）

28. **.pagination-bar / .page-info**——柔和灰 #aeaeb2 + strong 用 #1d1d1f

### 九、弹窗/提示（3 个）

29. **.modal-overlay**——rgba(0,0,0,0.4) + backdrop-filter blur(4px)
30. **.modal-dialog / .modal-box**——圆角 16px + 阴影 0 20px 60px rgba(0,0,0,0.15)
31. **.toast-message**——圆角 10px + backdrop-filter blur(10px) + 柔和阴影

### 十、聊天/加载/Tooltip（3 个）

32. **.chat-bubble**——圆角 16px（不对称：发送方左上角方 border-radius 16px 16px 4px 16px，接收方右上角方 16px 16px 16px 4px）
33. **.loading-spinner**——颜色 rgba(0,0,0,0.2) + 字号 14px
34. **#ellipsis-tip**——圆角 6px + backdrop-filter blur(10px) + rgba 背景

### 十一、登录页（1 个）

35. **Login.vue**——渐变背景 `linear-gradient(135deg, #f5f5f7, #e8e8ed)` + 卡片圆角 20px + 阴影 0 20px 60px rgba(0,0,0,0.1) + 输入框圆角 10px + 聚焦蓝色边框 + 按钮 full width 圆角 10px

## 布局间距优化

| 元素 | 当前 | 苹果风格 |
|------|------|---------|
| page-container padding | 24px 32px | 32px 40px |
| card padding | 16px 18px | 20px 24px |
| 卡片间距 gap | 12-14px | 20px |
| 表格行 padding | 10px 12px | 14px 16px |
| topnav 高度 | 52px | 56px |
| topnav-link padding | 6px 10px | 8px 14px |
| KPI 卡片 padding | 16px 18px | 20px |
| KPI 卡片圆角 | 10px | 14px |
| section-header margin | 12px | 16px |
| dashboard gap | 20px | 24px |

## 改动文件清单

| # | 文件 | 改动 |
|---|------|------|
| 1 | style.css | 全局变量 + 所有全局 class（1-34） |
| 2 | Login.vue scoped style | 登录页（35） |
| 3 | Overview.vue scoped style | KPI 卡片 + dashboard 间距 |
| 4 | HealthCenter.vue scoped style | 按需微调 |
| 5 | AiAssistant.vue scoped style | 按需微调 |

### 不改的

- 业务逻辑
- 后端
- HTML 结构（只改 CSS + 间距）
- 路由
- 数据库

## 验证

- `npx vite build` 构建通过
- 浏览器渲染各页面检查视觉效果
