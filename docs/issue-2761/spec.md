# Issue #2761 需求文档：场景与技能的统一分类体系 + 标签展示

## 目标

为 grape 平台官方场景与技能建立统一的一/二级业务分类字典（三级 = agent 级别），
支持带来源的受控标签（≤3）与市场卡片展示，并保持旧分类字段/旧数据平滑兼容。

## 范围（M1–M3）

1. `category_dict` 统一字典：`parent_id / level / slug / icon / order_num / applies_to / is_active`；
   种子为 Issue 正文 10 个一级域及其二级。
2. `scene_market` 增加 `cat_l1_id / cat_l2_id`；`skill` 增加 `cat_l1_id / cat_l2_id / tags`；
   旧 `category_id` 保留不动。
3. `scene_market.tags` 由逗号串升级为 JSON `[{name, source}]`，读路径兼容旧逗号串；
   `skill.tags` 新增 JSON 列。
4. `GET /api/categories/tree?scope=scene|skill` 两级树接口；
   scene/skill 列表支持 `cat_l1_id / cat_l2_id` 过滤；列表/详情返回 `category{l1,l2}` 与 `tags`。
5. 标签规则：≤3、大小写不敏感去重、manual 覆盖 auto；超限返回 400 并点名超限标签。
6. 规则型标签推荐：`GET /api/scenes/tag-suggest`、`GET /api/skills/tag-suggest`。
7. `scene_sync` / `skill_sync`：分类 slug 命中即写，未命中告警且不覆盖旧值。
8. 前端：场景/技能卡片展示 ≤3 标签与分类路径；技能上传表单支持一级→二级级联与标签输入；
   列表新增两级分类筛选，旧分类 tab 保留。

## 不在范围

- `grape-dev/grape-skills` 存量资产逐条迁移（M4，依赖跟进项）。
- 三级 agent 级别不落字典表（由节点 tag / agent_type 承载）。
- 分类字典管理后台 CRUD。

## 验收标准

- 树接口返回 10 个一级域及二级，`level / parent_id` 正确，仅 `is_active=1`。
- 场景/技能保存后回读分类与标签；旧 `category_id` 仍可用。
- 保存 4 个标签返回 400 并点名超限标签；≤3 成功；重复标签去重且 manual 优先。
- 旧逗号串标签（如 `iac,terraform`）读取不报错并映射为 manual 标签。
- market / skills 列表与详情返回分类路径与 ≤3 标签。
- 同步层遇未知 slug 告警且不覆盖已有分类。
- 现有测试通过，新增 `tests/test_issue2761_*.py` 覆盖以上用例。