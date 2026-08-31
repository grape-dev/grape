# 工作台用户页面优化方案 V3（最终版）

> 分支：`feat/admin-style-optimization`
> 参照 EngineManage.vue 风格

## 改动点（8 个）

### 1. 菜单名称统一（数据库）

`admin_permission_menus` 表 UPDATE：

| id | code | 当前名称 | 改为 |
|----|------|---------|------|
| 18 | sys.console | 控制台用户 | 工作台用户 |
| 5 | ai | AI分析（顶级）+ 子菜单 AI 助手 | 顶级保留"AI 分析"直接跳转，删除子菜单层级 |
| 13 | repos | 代码仓管理 | 代码仓库 |
| 15 | managed-repos | 托管代码仓 | 仓库管理 |
| 16 | template-repos | 模板代码仓 | 模板仓库 |

### 2. 页面布局参照 EngineManage

EngineManage 结构：
- page-header（标题 + badge）
- stats 统计卡行（引擎总数 + 各类型计数）
- em-stats 健康分布徽章行（点击联动筛选）
- toolbar（筛选下拉 + 搜索框 + 查询/重置 + 列表/卡片切换）
- 列表模式：card 包裹 vod-table
- 卡片模式：device-grid
- PaginationBar 组件

ConsoleUsers 对齐为：
- page-header（工作台用户 + badge）
- stats 统计卡行（总用户 + 有GitCode + 有华为云 + 有飞书 + 未配置）—— 后端全量统计
- em-stats 凭证分布徽章行（点击联动筛选）
- toolbar（凭证筛选下拉 + 搜索框 + 查询/重置）
- 列表模式：card 包裹 vod-table + 行可点击
- PaginationBar 组件（10/20/50）

### 3. 分页统一 10/20/50 + PaginationBar 组件

### 4. KPI 统计卡改为后端全量统计

后端列表 API 返回 `credentialStats: { total, hasGitcode, hasHuawei, hasFeishu, noCredential }`

### 5. 凭证配置筛选

工具栏加下拉：全部 / 有 GitCode / 有华为云 / 有飞书 / 未配置
后端 where 条件支持 `credentialFilter` 参数

### 6. 表格行点击打开详情

`<tr class="tl-row-clickable" @click="showDetail(u.id)">`

### 7. 空状态优化

带图标提示

### 8. created_at 显示优化

空值用 — 占位

## 改动文件

| # | 文件 | 改动 |
|---|------|------|
| 1 | 数据库 admin_permission_menus | 菜单名称更新 |
| 2 | ConsoleUsers.vue | 参照 EngineManage 重写布局 |
| 3 | ConsoleUserController.java | 加 credentialFilter + 返回 credentialStats |
