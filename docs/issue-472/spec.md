# Spec: Issue #472 — Agent 运行时下引擎和 Workers 设备展示数量优化（全局口径）

> 来源：Issue Agent 结构化收敛（Issue #472 评论），本文件为开发落地版。目标分支：weekly-08-ljt

## 目标
Runtimes 页面 Engines / Workers 两个 tab 顶部统计栏（共 N 台 / 在线 / 离线 / 待回收）的数量口径从「当前分页（单页）列表」改为「当前搜索/筛选条件下的整体搜索结果」。

## 验收标准
- 搜索/筛选后，统计栏「共 N 台」= 整体搜索结果总数（非当前页条数）
- 在线/离线/待回收等健康分布计数同样基于整体搜索结果
- 清除搜索/筛选后，统计栏恢复全量总数
- Engines tab 与 Workers tab 统计口径一致（均整体搜索结果口径）
- 分页切换不影响统计栏数字（统计与分页解耦）

## 范围
- In：两 tab 统计栏全局口径；Engines 的 q/type/scene/tag 筛选迁移到后端（否则前端只能过滤当前页，无法得到全局结果）；后端返回 health_stats 全局分布。
- Out：搜索/筛选语义变更（谓词与现前端 _filterEngines 保持一致）；文案 i18n（#432 已覆盖）；Workers 筛选行为（#245 已在后端）。

## 关键决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 全局计数来源 | 接口返回 health_stats（total + 四态分布） | 避免前端全量拉取；与 #432 Risks 的 _rtTotal 思路一致 |
| Engines 筛选位置 | 迁移到后端（q/type/scene/tag 参数） | 与 Workers #245 同构；前端只过滤当前页无法实现全局口径 |
| 筛选谓词 | 与原 _filterEngines 完全一致（haystack/type/scene/tag） | 不改变用户可见的筛选行为 |
