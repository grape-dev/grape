# Agent Configuration Skill 下拉增加 scope 过滤

## 一、背景

Agent Configuration 页面的 Skill 下拉只显示官方 skill，仓库和个人 skill 看不到。原因是前端依赖 currentRepo 是否选择决定查询参数，且 user skill 的 owner_id 按当前登录用户过滤。

## 二、方案

在 Skill 下拉前增加三个 scope 按钮（全局/项目/个人），点击切换按 scope 查询。

## 三、改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/skills.py | list_skills 增加 scope 参数 |
| src/api/static/js/pages/agents.js | Skill 行增加 scope 按钮 + 切换逻辑 |

## 四、后端改动

list_skills 增加 scope 参数：
- scope=official：只返回官方 skill
- scope=repo：只返回当前仓库 skill（需带 repo）
- scope=user：只返回当前用户个人 skill
- 不传 scope：保持原有行为

## 五、前端改动

- Skill label 右侧增加 [全局] [项目] [个人] 三个按钮
- 默认选中全局，点击切换重新查询
- 去掉 dataset.loaded 缓存，每次切换重新加载
- 当前已配置的 skill 不在当前 scope 时保留下拉

## 六、验证

1. 打开 Agent Configuration → 默认显示全局 skill
2. 点项目 → 下拉刷新为仓库 skill
3. 点个人 → 下拉刷新为个人 skill
4. 保存配置后切换 scope → 已配置 skill 保留
