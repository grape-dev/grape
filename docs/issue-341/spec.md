# Spec: Issue #341 — 需求中心深链 `#tasks/<issue号>` 直达详情页

## 目标
前端 hash 路由支持 `https://www.grapedev.cn/app/#tasks/<issue号>` 深链，直达需求中心对应 Issue 详情页。

## 范围
- In：`src/api/static/js` hash 路由解析 `^#tasks/\d+$`；需求中心按编号直达详情（复用 #166/#290 详情打开链路 `_pendingIssueNumber` → `_openBoardDetail`）；无效编号 Toast 提示 + 回退列表；`#tasks` 原行为零回归；刷新可恢复。
- Out：后端改动；pathname 路由；其它页面深链。

## 验收标准
- `#tasks/341` 直达详情；无效编号提示回退；`#tasks` 无回归；刷新仍生效；现有测试通过 + 新增测试覆盖。
