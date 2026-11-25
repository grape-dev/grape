# Plan: Issue #3976 — 技能详情页优化

## 现状核实
- `POST /skills/convert` 为 copy-not-move（`src/domains/mgmt/routes/skills.py:1908`），复制新行并保留原行，不满足原地改 scope。
- `POST /by-id/{id}/update` 以 ZIP 替换并写 `grape_t_skill_version`（版本递增），不适合「保存 SKILL.md 不升版本」。
- 平台 LLM 既有调用范式：`src/domains/mgmt/routes/autopilot.py:970` 从 `~/.hermes/config.yaml` 读取 base_url/api_key/model，走 `/chat/completions`。
- skill 表 `id` 是泛化引用键：`grape_t_skill_version.skill_id`、`grape_t_skill_file.skill_id`、`scene_skill_ref.skill_id`、`skill_likes.skill_id` 均按 id 关联；原地改 scope 天然保留全部引用。

## 后端设计（`src/domains/mgmt/routes/skills.py`）

### 1. `POST /by-id/{skill_id}/convert`
- 入参 `{target_scope: user|repo, repo?}`。
- 权限：official 403；user 需本人；repo 需托管人；目标 repo 需托管人；目标 user 归属调用者。
- 去重：目标 (skill_name, scope, owner_id) 已存在 → 409；显示名在目标 scope 冲突 → 409。
- 存储：`storage.copy_object(src_key, dst_key)` → DB `skill_scope_move_update`（scope/owner_id/file_path）→ 成功后删除旧对象；DB 失败回滚新对象。
- id / 版本行 / 文件清单 / 场景引用 / 点赞保持不变。

### 2. `PUT /by-id/{skill_id}/content`
- 入参 `{content}`。
- 权限：仅 scope=user 且本人；official/repo 403。
- 重写 OBS zip 中 `{skill_name}/SKILL.md`（保留其它条目与权限位），保存新 zip；仅更新 `content_hash`/`updated_at` 与文件清单；**不写版本表**。
- DB 失败回滚旧 zip 字节；两级缓存失效。

### 3. `POST /by-id/{skill_id}/ai-fill`
- 权限：非 official；user 需本人，repo 需托管人。
- 读取 SKILL.md → 组装提示词 → `asyncio.to_thread(_platform_llm_chat)` → 解析 JSON → 规整 summary/description/intro/tags（标签 ≤5、≤12 字、过滤系统标签）。
- 只返回建议，不落库；失败返回 502 由前端提示。

### 4. 具名查询（`src/domains/runtime/public/__init__.py`）
新增可移植写查询：
- `skill_scope_move_update`：`UPDATE skill SET scope=?, owner_id=?, file_path=?, updated_by=?, updated_at=datetime('now') WHERE id=?`
- `skill_content_hash_update`：`UPDATE skill SET content_hash=?, updated_at=datetime('now') WHERE id=?`

## 前端设计（`SkillsView.vue`）
- 头部操作区：repo →「转为个人技能」，user →「转为仓库技能」，official 不渲染；确认后原地 `openDetail(id)` 刷新。
- SKILL.md Tab：scope=user 显示「编辑 SKILL.md」，进入 textarea 编辑态；保存调用 content 端点，成功回详情。
- 编辑信息抽屉：顶部「AI 填写」按钮，异步调用 ai-fill；仅填充当前为空字段，标签追加去重且不超 5；失败 toast 且不改动已填内容。
- i18n：补充 action/ai 文案 zh/en 与 KEY2_DEFAULTS 兜底。

## 测试（`tests/mgmt/test_issue3976_skill_detail_actions.py`）
- 互转：id/版本行/场景引用不变，scope/owner 原地更新；official 403。
- SKILL.md 保存：版本行数量不变、版本号不变；official/repo 403；zip 内 SKILL.md 被替换。
- AI 填写：normalize 逻辑；official 403；失败抛 502。
- 前端冒烟：Vue SFC 可编译、lint 通过。
