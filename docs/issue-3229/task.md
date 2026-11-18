# Issue #3229 任务拆解

1. 前端 BOM 防御：`SkillsView.vue` 新增 `stripLeadingBom`，接入 `renderSkillContent` / `detailVersion` / `skillMdDescription` / `firstSentenceOfSkillMd`。
2. 入库 BOM 归一化：`skill_sync.py` 新增 `_strip_utf8_bom` / `_is_bom_text_file` / `_normalize_skill_dir_bom`。
3. hash 幂等：`_compute_skill_hash` 对文本文件 BOM 不敏感；目录同步 skip/拷贝路径归一化本地目录。
4. OBS 归一化：`sync_official_skills_to_obs` 打包前归一化，本轮有改写时重传一次以刷新存量 zip。
5. 单测：`tests/js/test_issue_3229_skill_bom.js`（带/不带 BOM 两例）+ `tests/runtime/test_issue_3229_skill_sync_bom.py`（归一化、hash 幂等、zip 无 BOM、第二轮 skip）。
6. 验证：`node tests/js/test_issue_3229_skill_bom.js`、`pytest tests/runtime/test_issue_3229_skill_sync_bom.py tests/insight/test_skill_sync_obs.py` 通过。
7. PR 说明存量策略：无 DB 迁移，上线后一次定时同步覆盖本地目录与 OBS，content_hash 变化失效缓存；上游 grape-skills 数据清理单独提交。
