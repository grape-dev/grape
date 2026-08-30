# 工作台用户详情页优化方案 V2

> 分支：`feat/admin-style-optimization`

## 改动点

### 1. 布局改为上下结构
- 基本信息改为紧凑横排（一行展示用户名/昵称/来源/创建时间/最后活跃）
- 活动统计占主要空间
- 凭证配置移到基本信息下方

### 2. tab 改名
- "任务总览" → "任务统计"
- "Issue 分析" → "仓库与 Issue"

### 3. 加最近活动时间线
- 展示最近 5 条任务（时间/仓库/Issue/状态/Agent）
- 后端 detail API 加 recentTasks

### 4. 强制下线移到 page-header 右侧
- 和返回按钮对称，不再孤立在底部

### 5. page-header 加用户摘要
- 用户名 + 在线状态 + 来源标识（GitCode 登录/其他）

## 改动文件

| # | 文件 | 改动 |
|---|------|------|
| 1 | ConsoleUserDetail.vue | 布局重构+时间线+tab改名+操作栏移位 |
| 2 | ConsoleUserController.java | detail API 加 recentTasks |
