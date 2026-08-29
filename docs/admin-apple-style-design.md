# 运营管理台苹果风格样式优化方案（最终版）

> 分支：`feat/admin-style-optimization`（基于 dev `8f500c0f`）
> 范围：只改 CSS + 布局间距，不改业务逻辑、不改后端、不改 HTML 结构

## 设计目标

将 admin 运营管理台的元素布局和视觉风格对齐 Apple Design——克制、留白、层次感、毛玻璃、大圆角、柔和阴影。

## 实施状态

| # | 改动项 | 状态 | 文件 |
|---|--------|:---:|------|
| 1 | :root 变量——苹果色板 | ✅ | style.css |
| 2 | body font-family——SF Pro | ✅ | style.css |
| 3 | 滚动条——半透明 | ✅ | style.css |
| 4 | 顶栏——毛玻璃 blur(20px) | ✅ | style.css |
| 5 | 导航链接——hover rgba + 选中胶囊 | ✅ | style.css |
| 6 | 用户区域——同 hover | ✅ | style.css |
| 7 | 抽屉——圆角 + hover | ✅ | style.css |
| 8 | 页面容器——padding 32px 40px | ✅ | style.css |
| 9 | 页头——padding + h1 20px | ✅ | style.css |
| 10 | 区块标题——sh-title 16px | ✅ | style.css |
| 11 | 卡片——大圆角 16px + hover 上浮 | ✅ | style.css |
| 12 | 设备卡片——同 card + hover | ✅ | style.css |
| 13 | 卡片网格——gap 20px | ✅ | style.css |
| 14 | 表头——透明 + 大写 + 字间距 | ✅ | style.css |
| 15 | 表格行——padding 14px 16px | ✅ | style.css |
| 16 | 表格 hover——极浅蓝灰 | ✅ | style.css |
| 17 | 按钮——圆角 8px + hover 上浮 | ✅ | style.css |
| 18 | SVG 图标按钮——hover 蓝底 | ✅ | style.css + 各页面 |
| 19 | 分页按钮——圆角 8px | ✅ | style.css |
| 20 | 输入框——圆角 10px + 聚焦阴影 | ✅ | style.css |
| 21 | 列筛选下拉——圆角 + 阴影 | ✅ | DeviceDetail/EngineDetail |
| 22 | 筛选选项——hover 浅灰 | ✅ | DeviceDetail/EngineDetail |
| 23 | 筛选标签——胶囊 | ✅ | style.css |
| 24 | 统计卡——圆角 14px + 柔和阴影 | ✅ | style.css |
| 25 | KPI 卡片——圆角 14px + hover 上浮 | ✅ | Overview.vue |
| 26 | 状态标签——半透明背景 | ✅ | style.css |
| 27 | 徽章——胶囊 + 半透明 | ✅ | style.css |
| 28 | 分页栏——柔和灰 | ✅ | style.css |
| 29 | 弹窗遮罩——blur(4px) | ✅ | style.css |
| 30 | 弹窗——圆角 16px | ✅ | style.css |
| 31 | Toast——毛玻璃 + 圆角 10px | ✅ | style.css |
| 32 | 聊天气泡——不对称圆角 16px | ✅ | style.css |
| 33 | 加载动画——苹果灰 | ✅ | style.css |
| 34 | Tooltip——毛玻璃 | ✅ | style.css |
| 35 | 登录页——渐变 + 毛玻璃卡片 | ✅ | Login.vue |
| 36 | 14 页面 scoped style 旧值清理 | ✅ | 全部 Vue 文件 |
| 37 | echarts 颜色变量——苹果色板 | ✅ | Overview/AgentDetail/EngineDetail |
| 38 | JS 状态颜色变量——苹果色 | ✅ | AgentManage/ReposManage |

## 验证

| # | 验证项 | 结果 | 证据 |
|---|--------|:---:|------|
| 1 | 旧颜色值残留 | ✅ | 35 种旧值全量 grep = 0 |
| 2 | 旧圆角残留 | ✅ | border-radius: 4px = 0（排除滚动条） |
| 3 | 前端构建 | ✅ | built in 16.05s |
| 4 | admin 服务 | ✅ | health=ok |
| 5 | 登录页渲染 | ✅ | Playwright |
| 6 | 总览页渲染 | ✅ | Playwright |
| 7 | 引擎管理渲染 | ✅ | Playwright |
| 8 | 设备管理渲染 | ✅ | Playwright |
| 9 | 凭据管理渲染 | ✅ | Playwright |
| 10 | 健康中心渲染 | ✅ | Playwright |
| 11 | 代码仓管理渲染 | ✅ | Playwright |
| 12 | 引擎详情渲染 | ✅ | Playwright |
| 13 | 设备详情渲染 | ✅ | Playwright |

## commit 历史

- `142b2f6b` 全局 CSS + 登录页 + 总览页
- `8bc76384` 14 页面 scoped style 苹果风格优化
- `4cbca49f` 修复 8 处残留旧颜色值 + 紫色青色替换

## 改动文件清单（20 个）

| 文件 | 改动内容 |
|------|---------|
| style.css | 全局变量 + 所有全局 class |
| Login.vue | 渐变背景 + 毛玻璃卡片 |
| Overview.vue | KPI 卡片 + 图表颜色 |
| AgentDetail.vue | scoped style + echarts 颜色 |
| AgentManage.vue | scoped style + STATUS_COLOR |
| AiAssistant.vue | scoped style |
| DeviceDetail.vue | scoped style + 筛选下拉 |
| EngineDetail.vue | scoped style + echarts 颜色 |
| EngineManage.vue | scoped style |
| DeviceManage.vue | scoped style |
| CredentialManage.vue | scoped style |
| HealthCenter.vue | scoped style + 告警颜色 |
| RolePermissionConfig.vue | scoped style |
| SkillManage.vue | scoped style + SCOPE_COLORS |
| ConsoleUsers.vue | scoped style |
| ReposManage.vue | scoped style + colorOf |
| TemplatesManage.vue | scoped style |
| UserManage.vue | scoped style |
| TaskListSection.vue | scoped style |
| docs/admin-apple-style-design.md | 设计文档 |
