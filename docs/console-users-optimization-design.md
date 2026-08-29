# 工作台用户页面优化方案

> 分支：`feat/admin-style-optimization`

## 需求

1. 总览页 KPI 卡片：控制台用户→工作台用户，去掉管理台用户显示
2. 点击工作台用户卡片跳转 http://1.92.115.229/sys/console
3. ConsoleUsers.vue 页面标题改为工作台用户
4. 页面参照健康中心风格优化
5. 查不到已注册用户的问题

## 根因分析：为什么查不到用户

数据库 rds-grape-dev.user_accounts 表有 18 条记录，但 API 返回 404。

根因：admin 后端上次重启时（合并 dev 最新代码后）编译了但没重新加载 ConsoleUserController——因为该 Controller 文件在 dev 合并时可能有变化但 mvn spring-boot:run 进程是旧代码。

解决：重启 admin 后端。

## 改动点

### 1. Overview.vue KPI 卡片（前端）

- 标题：控制台用户 → 工作台用户
- 去掉 `kpi-sub` 管理台用户显示
- 点击跳转改为 `window.open('http://1.92.115.229/sys/console')` 而不是 `$router.push`
- 数据口径说明里同步改名
- 用户构成图改名

### 2. ConsoleUsers.vue 页面优化（前端）

参照健康中心风格：
- 页面标题：控制台用户 → 工作台用户
- 加 KPI 统计卡行（总用户数/有 GitCode/有华为云/有飞书）
- 表格用苹果风格（已有全局样式）
- 详情按钮改 SVG 图标
- 搜索框样式对齐
- 加统计分布图（echarts 饼图——按凭证配置分布）

### 3. 路由 meta title 改名

- `/sys/console` meta.title: 控制台用户 → 工作台用户

### 4. 后端重启

- 重启 admin 后端加载最新代码解决 404

## 不改的

- 后端 API 逻辑（SQL/接口路径/字段）
- 数据库结构
- 业务逻辑
