# Issue #3227 任务拆解

1. 概览模板四层重排：元信息表 → 摘要卡片 → 技能概述 → 技能介绍 → 用户评论。
2. 摘要卡片：新增 `detailSummaryTitle` / `detailSummaryBody`，标题化并限制正文 15–50 字。
3. 技能介绍：移除 `ov_summary`，段序改为适用范围 → 关键步骤链 → 产物说明 → 约束简述。
4. 摘写工具：新增 `sentenceClamp` 等句界函数，删除 `cleanLines` 120 字硬截。
5. 文案与样式：`KEY_DEFAULTS` 更名、清理旧 key；更新 `.skd-summary*` / `.skd-ov-text` 样式。
6. 验证：`npm run lint`、`npm run build` 通过；全仓检索旧 key 无残留。
