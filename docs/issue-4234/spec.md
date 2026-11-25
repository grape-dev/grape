# Spec: Issue #4234 — 统一需求中心状态词表（status → {列, 展示名}）

## 背景
同一条 `status=active` 需求：看板 in_progress 列头叫「处理中」（该列游标下发
`statuses=active,processing`），卡片/详情状态点却叫「待处理」（`mapStatusL` 按 stage
优先取名，详情更是裸比原始 `status==='processing'`）。列分组口径与状态词表各自定义，
没有单一事实来源。

## 目标
- 建立一张 `status → {列, 展示名}` 映射表（STATUS_VIEW_MAP），列游标、列头文案、
  卡片/详情状态点统一消费。
- 不改后端 status 语义：`statuses=active,processing` 筛选、翻页与列头计数不回归。

## 范围
- In：`src/api/frontend/app/src/views/IssuesView.vue`（映射表抽取与三处消费点接线）。
- Out：后端数据模型/API、看板列数（不拆列）、其他业务实体的状态词表。

## 验收
1. 同一条 active/processing 需求：列名与卡片/详情状态名语义一致。
2. `statuses=active,processing` 筛选行为不变，翻页与列头计数不回归。
3. zh/en 词条无缺失键；现有测试全绿，新增映射一致性测试。
