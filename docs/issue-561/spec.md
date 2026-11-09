# Spec: Issue #561 — 个人技能卡片支持直接上传压缩包更新技能

> Coding Agent | 基于 Issue Agent 已发布 Spec 执行 | 目标分支: weekly-08-mys | 开发分支: feat/issue-561

## Objective
个人技能卡片上直接上传 ZIP 覆盖更新既有技能：后端新增按 skill_id 的覆盖更新端点 + 前端卡片「上传更新」入口。更新后技能 id/引用关系（scene_skill_ref）/点赞（skill_like）保留，content_hash 刷新，两级缓存即时失效。

## Scope

### In Scope
- `src/api/routes/skills.py` 新增 `POST /skills/by-id/{skill_id}/update`（multipart ZIP 上传），复用既有校验链（validate_custom_skill / zip 炸弹防护 / 10MB 限制）。
- 写序对齐 #444：save 前留存旧对象字节，upsert 失败回滚恢复；成功后 `zip_cache_put` + `mem_cache_invalidate` 两级缓存失效。
- 权限对齐 delete：official 不可更新（403）；user 需本人；repo 需当前用户托管该仓库。
- 前端 `src/api/static/js/pages/skills.js` 卡片操作区新增「上传更新」入口（确认弹窗 → 调更新端点 → 刷新卡片）。
- 测试 `tests/test_issue561_skill_update.py`：成功保留 id/权限/校验/回滚/缓存失效。

### Out of Scope
- 版本历史/回滚管理 UI。
- zip 目录名与既有技能名不一致时的改名更新（本期拒绝，返回明确 400 文案）。
- #336 version 校验放宽（独立 Issue）。

## Acceptance Criteria
- [x] 对已有个人技能（scope=user）按 id 上传新 ZIP，成功且技能 id 不变
- [x] 更新后 content_hash 刷新、updated_at 变化；scene_skill_ref / skill_like 不丢失
- [x] 更新后读路径缓存失效（zip_cache_put + mem_cache_invalidate）
- [x] 无权限用户更新他人技能 403；official 技能不可更新
- [x] 畸形 ZIP / 名称不匹配 / 超限返回明确错误
- [x] 前端卡片出现「上传更新」入口，仅非 official 技能可见
