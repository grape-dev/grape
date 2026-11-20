# Issue #2761 任务拆解

## M1 分类字典 + 标签存储

- [x] `category_dict` DDL + 10 个一级域/二级种子
- [x] `scene_market` / `skill` 扩展列（cat_l1_id / cat_l2_id / tags）
- [x] 注册式迁移 `202611170042`（建表 + 补列 + 旧标签回填）
- [x] `src/kernel/tags.py` 解析/去重/manual 优先/≤3
- [x] `GET /api/categories/tree?scope=scene|skill`

## M2 列表/详情回读 + 同步层

- [x] `list_category_dict` / `list_category_tree` / `category_path`
- [x] scene market 列表/详情返回 `category` 与 `tags`，支持两级过滤
- [x] skills 列表/详情返回 `category` 与 `tags`，支持两级过滤
- [x] 标签推荐端点（scenes / skills）
- [x] `scene_sync` / `skill_sync` slug 命中写入，未命中保留旧值
- [x] 写侧 ≤3 校验与 400 点名超限标签

## M3 前端

- [x] `SceneCard.vue` 分类路径 + ≤3 标签 chip
- [x] `SceneMarket.vue` 一级/二级筛选（保留旧分类 tab）
- [x] `SkillsView.vue` 卡片标签/分类路径、两级筛选、上传表单级联 + 标签输入

## 测试

- [x] `tests/test_issue2761_classification_tags.py`
  - 字典种子与树接口 scope 过滤
  - 标签上限/去重/manual 覆盖 auto/400 点名超限
  - scene/skill 分类与标签回读
  - 旧逗号串兼容与回填幂等
  - sync 映射命中/未命中

## 后续（不在本次范围）

- [ ] M4：`grape-dev/grape-skills` 存量场景/技能分类迁移与未分类校验报告