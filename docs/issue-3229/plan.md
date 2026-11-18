# Issue #3229 方案设计

## 前端防御（SkillsView.vue）
- 新增 `stripLeadingBom(text)`：`String(text || '').replace(/^\uFEFF+/, '')`，只剥离首部连续 BOM，无 BOM 原样返回。
- `renderSkillContent(rawContent)`：入口先 `stripLeadingBom`，再匹配 `/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/`，随后按原逻辑渲染 frontmatter 表格 + 正文。
- `detailVersion`：`stripLeadingBom(d.content || '')` 后再取 `version:`。
- `skillMdDescription`：`stripLeadingBom(content)` 后再取 frontmatter `description`。
- `firstSentenceOfSkillMd`（上传预填摘要）：同样先剥 BOM。
- 函数声明提升，`stripLeadingBom` 可定义在调用点之后，不引入模块级状态。

## 同步入库归一化（skill_sync.py）
- 新增 `_strip_utf8_bom(bytes)`：循环剥离首部 `EF BB BF`。
- 新增 `_is_bom_text_file(name)`：`SKILL.md` 或扩展名 `.md/.markdown/.txt/.yaml/.yml/.json`。
- 新增 `_normalize_skill_dir_bom(dir)`：遍历技能目录，仅改写文本文件首部 BOM，返回改写数；幂等，无 BOM 返回 0；跳过 `__pycache__/__MACOSX/.git`。
- `_compute_skill_hash`：文本文件读取后先 `_strip_utf8_bom` 再入哈希，使同一技能 BOM 有无同 hash。
- 目录同步：拷贝前用 hash 判定（已 BOM 不敏感）；`skip` 分支就地归一化本地目录，拷贝后再次归一化，保证本地目录无 BOM。
- OBS 同步：先归一化本地目录再算 hash/打包；本轮有实际改写（`bom_normalized > 0`）时即使 hash 相同也重传一次，刷新存量 zip；计数按 `updated` 记录。第二轮 `bom_normalized == 0` 正常命中 skip。
- `_pack_skill_zip` 从已归一化的本地目录打包，OBS zip 内无 BOM。

## 测试
- JS：从 `SkillsView.vue` 抽取相关纯函数，以桩替换 `renderMarkdown`，断言无 BOM 正常渲染、带 BOM 输出与无 BOM 完全一致、正文不泄漏裸 frontmatter。
- Python：断言目录归一化仅改文本文件且幂等、hash 对 BOM 不敏感、打包 zip 无 BOM、OBS 两轮同步第二轮 skip 不重复上传。

## 兼容与回滚
- 无 DB 迁移、无接口变更；读取链 `utf-8-sig` 保留为兜底。
- 回滚仅需回退代码；归一化只删首部 BOM，不改变正文语义。
