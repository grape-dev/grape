# Spec: Issue #3202 技能卡片展示优化

## 目标

技能市场卡片布局/标签分层/视觉规范对标 LobeHub（C1–C8），并补齐免传包元信息更新链路。

## 范围

- 前端 `src/api/frontend/app/src/views/SkillsView.vue`：
  - C1 上传/更新弹窗标签 chips：≤5 个、每个 ≤12 字、去重、禁与系统标签（Agent 类型名/分类名）重名；
  - C2 标题独占一行 + 省略号 + `title` 悬停全文；分类移出标题行；agent chip 置于标题行右端；
  - C3 卡片视觉统一（12px 圆角、字号层级、三类 chip、底部 meta 左右分栏、普通卡与精选卡同构）；
  - C4 agent 类型右上、分类右下、用户标签左下（>3 折叠 +N），空值不出 chip；
  - C5 摘要固定 3 行（有 `summary` 用 summary、否则截断 description），卡片不再展示「所属场景」行；
  - C6 `scope=official` 名称右侧「官网」徽章，去除 🌟 精选徽章；
  - C7 摘要必填 ≤50 字 + 实时字数 + SKILL.md description 首句预填；
  - C8 仓库/个人技能「编辑信息」弹窗，调用 PATCH 免传包更新。
- 后端：
  - `src/kernel/tags.py`：`MAX_TAG_LEN=12` 与超长检测；
  - `src/domains/mgmt/routes/skills.py`：技能侧 `SKILL_MAX_TAGS=5`、标签 12 字/系统标签重名校验、摘要必填、列表透出 summary、新增 `PATCH /api/skills/{id}`；
  - `src/domains/runtime/public/__init__.py`：列表/精选查询补 summary、单条元信息 PATCH 写语句；
  - `scripts/resync_official_skill_summaries.py`：官方存量摘要批量重梳 ≤50 字，description 零改动。

## 验收

1. 标签 ≤5、每个 ≤12 字、去重、系统标签重名均被 400 拦截；重传覆盖生效。
2. 长标题单行省略 + 悬停全文，标签不挤压标题。
3. 卡片无 <10px 文字、无 emoji 挤行，普通卡与精选卡同构。
4. agent chip 右上、分类右下、用户标签左下（3+N），空值不显示空 chip。
5. 摘要恒 ≤3 行，详情页全文完整。
6. 官方技能显示「官网」徽章，非官方不显示，卡片无 🌟。
7. 新建摘要必填 ≤50 字；官方存量摘要 ≤50 且 description 不变。
8. PATCH 免传包改 tags/summary 即时生效；official/非属主/非成员 403；`updated_at` 刷新、版本号不变。
9. 现有测试通过，新增覆盖 PATCH 鉴权、标签规则、摘要必填。
