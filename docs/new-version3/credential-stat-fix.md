# 工作台用户详情页凭证统计优化

## 问题

当前"已配置 X/7"把 AK/SK 拆开算——华为云 AK 和 SK 算 2 个，飞书 App ID 和 Secret 算 2 个。不直观。

## 修正

改为按平台统计"已配置 X/4"：

| 平台 | 判断条件 |
|------|---------|
| GitCode | gitcode_api_token 非空 |
| 华为云 | huawei_cloud_ak 非空（AK 配了就代表配了，SK 跟随） |
| 飞书 | feishu_app_id 非空 |
| CodeArts | CODEARTS_CLI_AK 非空 |

凭证分组卡片展示不变（每组内仍展示 AK/SK 各自状态），但顶部汇总改为 X/4。

## 改动文件

| # | 文件 | 改动 |
|---|------|------|
| 1 | ConsoleUserDetail.vue | credentialConfigured computed 改为按平台统计 4 个 |
