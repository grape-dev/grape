# 总览页用户统计卡片优化方案

## 需求

在总览页"工作台用户 · 注册趋势"卡片内增加 tab 切换，支持查看"注册趋势"和"活跃用户"两个维度。

## 设计

### 卡片结构

```
┌─────────────────────────────────────────────────┐
│ 工作台用户 · [注册趋势 | 活跃用户]    [年|月|日]  │
│                                                 │
│  📈 折线图（平滑曲线+面积填充）                  │
│                                                 │
└─────────────────────────────────────────────────┘
```

- 主 tab：注册趋势 / 活跃用户（切换数据源）
- 维度切换：年/月/日（注册趋势用）/ 近7天/近30天/近90天（活跃用户用）
- 维度切换按钮根据主 tab 变化

### 注册趋势 tab（已有）

- 维度：年/月/日
- 数据源：`GET /api/admin/stats/user-registration?granularity=month`
- X轴：时间（2026-08），Y轴：注册人数
- 折线图：平滑曲线 + 面积填充 + 圆点标记

### 活跃用户 tab（新增）

- 维度：近7天/近30天/近90天
- 数据源：`GET /api/admin/stats/active-users?days=30`
- X轴：日期（2026-08-27），Y轴：活跃用户数
- 折线图：同样风格（平滑曲线+面积填充）
- 活跃定义：当天有 agent_tasks 记录的用户（通过 repo_owner 三表 JOIN）
- SQL：`COUNT(DISTINCT u.id) GROUP BY DATE(t.started_at)`

### KPI 卡片（可选，不在本次范围）

DAU/WAU/MAU 三个数字可以加在总览页 KPI 区域，本次先不做。

## 后端 API

### `GET /api/admin/stats/active-users?days=30`

返回：
```json
{
  "days": 30,
  "data": [
    {"label": "2026-08-27", "count": 1},
    {"label": "2026-08-26", "count": 1},
    ...
  ]
}
```

SQL：
```sql
SELECT DATE(t.started_at) as label, COUNT(DISTINCT u.id) as cnt
FROM agent_tasks t
INNER JOIN managed_repos m ON t.repo_key = m.repo_key
INNER JOIN user_accounts u ON u.username = m.repo_owner
WHERE t.started_at IS NOT NULL
AND t.started_at >= DATE_SUB(NOW(), INTERVAL ? DAY)
GROUP BY label
ORDER BY label
```

## 前端改动

### Overview.vue

1. 主 tab 切换：`statTab = ref('registration')` — registration / active
2. 维度切换根据 statTab 变化：
   - registration: 年/月/日
   - active: 7天/30天/90天
3. 切换主 tab 时重新加载数据 + 重新渲染图表
4. 图表标题动态变化

## 改动文件

| # | 文件 | 改动 |
|---|------|------|
| 1 | StatsController.java | 新增 active-users API |
| 2 | Overview.vue | 加主 tab 切换 + 活跃用户维度 + 数据加载 |
