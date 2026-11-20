# Issue #3229 需求文档

## 背景
部分官方技能 `SKILL.md` 以 UTF-8 BOM（U+FEFF）开头，前端 `renderSkillContent()` 的 frontmatter 正则 `^---` 锚定字符串起始位置，BOM 使位置 0 不是 `-` 而失配，整段 YAML frontmatter（`name:` / `description: |`）被当正文渲染。详情页版本号、描述提取同样以 `^---` 锚定，一并失配。BOM 源头为 `grape-dev/grape-skills` 仓库中 17 个 `SKILL.md`，同步入库时原样保留。

目标分支 `arch-weekly-11-ldw-loophub` 的前端为 `src/api/frontend/app/src/views/SkillsView.vue`（旧 `src/api/static/js/pages/skills.js` 已迁移，不在本基线）。

## 目标
- 技能详情「SKILL.md」Tab 对全部官方技能正确剥离 YAML frontmatter，页首不再出现裸 `---` / `name:` / `description: |`。
- `content` 带 U+FEFF 时渲染结果与不带 BOM 一致；无 BOM 时行为不变。
- 同步入库侧对技能目录做 BOM 归一化，且 content_hash 对 BOM 不敏感，避免每轮定时同步重复覆盖。

## 范围
### In Scope
- `src/api/frontend/app/src/views/SkillsView.vue`：`renderSkillContent`、`detailVersion`、`skillMdDescription`、`firstSentenceOfSkillMd` 统一去首部 BOM。
- `src/domains/runtime/storage/skill_sync.py`：下载解包/本地目录/打包 OBS 前做 BOM 归一化；`_compute_skill_hash` 对文本文件 BOM 不敏感。
- 单测：`tests/js/test_issue_3229_skill_bom.js`、`tests/runtime/test_issue_3229_skill_sync_bom.py`。

### Out of Scope
- 不改技能卡片布局（#3202）、不改详情概览 Tab 结构（#3227）。
- 不新增 DB 迁移；DB 不存 SKILL.md 正文。
- 上游 `grape-dev/grape-skills` 的 17 个 `SKILL.md` 去 BOM 属独立仓库数据清理，不阻塞本仓交付。
- 读取链 `utf-8-sig` 保持现状作为兜底。

## 存量数据处理策略
不新增 DB 迁移。代码上线后由一次定时同步（每 30 分钟）覆盖本地技能目录与 OBS zip，`content_hash` 变化自然失效两级缓存；归一化后第二轮同步命中 skip，不重复覆盖。上游仓库数据清理单独提交，本仓同步归一化已可消除其运行时影响。

## 验收标准
- [ ] 带 BOM 的 SKILL.md 样本：frontmatter 正确剥离为元信息表格，页首无裸 frontmatter 文本。
- [ ] 17 个受影响技能的 `version` / `description` 提取正常（由同步归一化覆盖，上线后同步即生效）。
- [ ] 抽样 ≥10 个无 BOM 官方技能渲染结果与修复前一致，无回归。
- [ ] 同步幂等：连续两轮 sync，第二轮 `dirs_skipped` / `obs_skipped` 正常递增，不重复覆盖。
- [ ] `node tests/js/test_issue_3229_skill_bom.js` 通过；`pytest tests/runtime/test_issue_3229_skill_sync_bom.py` 通过。
