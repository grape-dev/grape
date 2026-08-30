# 工作台用户详情页优化方案

> 分支：`feat/admin-style-optimization`

## 改动点（7 个）

### 1. 凭证改为分组卡片布局
4 个分组卡片，每组带标题 + badge：
- GitCode：Token
- 华为云：AK / SK
- 飞书：App ID / Secret
- CodeArts：AK / SK

### 2. 凭证汇总
凭证区域顶部加汇总行："已配置 X/7"

### 3. 发布仓库加超链接
gitcodePublishRepo / githubPublishRepo 加链接跳转 /repos/manage

### 4. 用户活动信息
详情 API 加返回：
- taskCount：该用户的任务数（agent_tasks WHERE created_by=uid）
- repoCount：托管仓库数（managed_repos WHERE created_by=uid）
- successRate：任务成功率

### 5. 时间格式化
前端 formatTime 函数：2026-08-15T03:22:11 → 2026-08-15 11:22

### 6. 重置密码按钮（强制下线）
后端加 POST /api/admin/console-users/{id}/reset-token
调 increment_token_version 让旧 token 失效

### 7. 页面标题加用户摘要
page-header 显示：用户名 · 在线状态 · 最后活跃时间

## 改动文件

| # | 文件 | 改动 |
|---|------|------|
| 1 | ConsoleUserDetail.vue | 分组卡片+汇总+超链接+活动信息+时间格式化+重置按钮 |
| 2 | ConsoleUserController.java | detail API 加 taskCount/repoCount/successRate + reset-token 接口 |
