# Issue #3227 方案设计

## 模板
- 概览面板顺序：元信息表 → 摘要卡片 → 技能概述卡片 → 技能介绍卡片 → 用户评论。
- 摘要卡片：`detailSummaryTitle` 非空才渲染；`h2.skd-summary-title` 为摘要文本，`p.skd-summary-body` 可选。
- 技能介绍卡片沿用 `v-if="hasOverview"`，slot 渲染由列表改为单句 `p.skd-ov-text`。

## 摘要取值
- `summaryTitle`：`detail.summary` → `description` 首句；去换行、按 `→|->` 取首段，≤50 字。
- `summaryBody`：`description` 首句按句界取 15–50 字；不足依次回退 `description` 全文 → SKILL.md frontmatter `description` → 「定位」首句；仍不足或与标题相同则不渲染正文。
- `summary` 与 `description` 皆空 → 不渲染整张摘要卡片。

## 技能介绍摘写
- 移除「总体说明」（`ov_summary`）。
- 段序：适用范围 → 关键步骤链 → 产物说明 → 约束简述。
- 适用范围：agent 类型 + 场景名合成一句，≤50 字。
- 关键步骤链：从「处理流程」有序列表或 `### Step N` 标题提取最多 5 步，以 `→` 连接成一句。
- 产物说明：取「任务完成流程 / 输出规范 / 产物」段，句界取 30–50 字。
- 约束简述：取「操作边界 / 约束」段，句界取 30–50 字。
- 缺段返回空、不渲染、不占位。

## 句界工具
- 新增 `normalizeInline` / `splitSentences` / `cutAtPunct` / `sentenceClamp`。
- `sentenceClamp(text, min, max)`：按 `。！？；!?;` 聚句至下限；超上限优先回退最近分句标点，无标点才硬截 `max`；不足下限返回空。
- 删除 `cleanLines` 的 120 字盲截实现。

## 文案与样式
- `KEY_DEFAULTS`：`description_original` → `skill_overview`（技能概述），`structured_overview` → `skill_intro`（技能介绍），删除 `ov_summary`。
- 样式：`.skd-summary` 改为 `.skd-summary-title`（17px/600）与 `.skd-summary-body`（13px、次要色、行高 1.6）；新增 `.skd-ov-text`。
