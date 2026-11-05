# Spec: Issue #1070 — 修复技能物化校验跨技能引用误判及 skill 表 content_hash_package 数据污染

## 需求
- 修复 `skill_materialize.py` 对跨技能前缀引用（`<skill-name>/references/...`）的物化校验误判：此类引用不在本技能 zip 包内（设计如此，运行时经 `grape skills detail` 按需获取），不应纳入本技能完整性校验。
- 缺自有文件（SKILL.md / 自有 references）仍判失败、走降级链路（回归保障）。
- 一次性数据修复：清洗 skill 表 `content_hash_package` 被 DDL 文本污染的行（582 行量级、content_hash 为空），重建 OBS zip 缓存 index.json 中 hash 为空的条目，恢复 `grape skills detail` / skills API 兜底通道。

## 范围
- In：`parse_skill_md_references` / `validate_skill_dir` 正则与跳过逻辑 + INFO 日志；`scripts/fix_skill_content_hash_package.py`（幂等修复）；`scripts/scan_skill_cross_skill_refs.py`（全量巡检清单）。
- Out：运行时跨技能引用获取降级策略；告警体系；任务级重试语义。

## 验收
- 带跨技能引用的技能物化成功，不误报 missing；缺自有文件仍判败（测试覆盖）。
- 跳过跨技能引用时记一次性 INFO 日志（`skipped N cross-skill refs for skill X`）。
- 修复脚本幂等：仅清洗匹配 DDL 特征且 content_hash 为空的行；终检污染行 = 0。
- 现有测试通过（基线预存失败除外）。
