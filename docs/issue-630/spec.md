# Spec: Issue #630 — 场景技能 vod-cli-crossrepo 物化失败且 skills API 无法按名称获取

> 来源：Issue #630（user feedback #236）| 优先级 P1 | 类型 bug | 关联 #602 #607 #610 #617（注明关联不重复）

## Objective

修复场景绑定用户级技能（vod-cli-crossrepo，sid=3276）在任务派发时物化失败的问题：
派发后工作区 `.skills/<技能名>/SKILL.md` + `references/` 完整存在；物化失败显式阻断
不静默降级；`grape skills detail --skill_id <技能名>` 可用；sid=3276 物化源缺失根因
定位并修复。

## 根因分析（代码级）

1. **物化源缺失（反馈①）**：技能表存在 sid=3276 行（`resolve_skill_id` 命中 user
   scope），但 `file_path`（OBS 对象键）为空、本地 `skills/custom/<owner>/<skill>/`
   目录也为空。`enqueue_task`（src/runtime/service.py）打包时 OBS 键与本地目录均
   不可用 → `skill_files={}`、`skill_obs_key=""` → worker 端 `.skills` 为空，静默降级。
2. **发布链路落盘断点（方案④根因）**：legacy 发布路径 `create_skill()`
   （src/user/db.py）与 `user_skills → skill` 迁移（src/api/db_setup.py）只写 DB 行
   + `os.makedirs` 空目录，从不落任何内容（无 OBS 对象、无 SKILL.md）；唯一写内容
   的现代发布路径是 `POST /api/skills/upload`（写 OBS + file_path）与
   `save_skill_md`（chat 编辑器）。经 legacy 路径注册且从未保存内容的行即成
   "空壳技能"——sid=3276 即此类。
3. **API 按名称 422（反馈②）**：`GET /api/skills/by-id/{skill_id}` 路径参数声明为
   `int`，传技能名时 FastAPI 路径解析直接 422；`resolve_skill_id` 的 user scope
   解析逻辑已存在但未被该端点复用。

## 技术方案（对齐 Spec 四项）

1. **enqueue 前物化完整性校验门禁（方案①）**
   - 新增 `verify_skill_materializable(skill, skill_id, repo_key, user_id)`
     （src/runtime/service.py）：技能未配置恒通过（不误伤，见 Spec 风险表）；配置了
     技能则按「skill_id/名称解析 → OBS 对象可读 → 本地目录完整
     （validate_skill_dir：SKILL.md + references）→ 官方内置包」链校验内容源。
   - `_enqueue_pipeline_task`（src/api/event_pipeline.py）在 `enqueue_task` 前调用
     门禁：失败 → `log.warning` 显式告警 + `_fail_event`（事件流可见）+ 阻断派发，
     不静默降级。
   - 定时任务 autopilot 路径维持 #287 契约（warning 降级不阻断），门禁只挂
     Pipeline 派发 enqueue 前，不改 `enqueue_task` 全局行为。
2. **按 sid 从技能存储整包拉取（方案②）**：既有链路已按
   `skill_id → file_path(OBS 键) → worker 整包拉 zip 物化`（agent.py /
   _materialize_direct_workdir），本次补齐缺口：门禁保证派发前 sid 源可达；源缺失
   （如 sid=3276 空壳行）显式阻断并提示重新上传，不再依赖注入路径是否存在。
3. **/api/skills/by-id 支持 name→sid（方案③）**：路径参数改为 str，非数字时复用
   `resolve_skill_id`（user scope 优先 + repo + official）解析为 sid 后走既有
   详情逻辑；解析不到 → 404。数字路径行为不变。
4. **发布链路断点修复（方案④）**：根因 = legacy 注册路径产生"无内容源空壳行"。
   修复：`create_skill()` 注册后即校验内容源（SKILL.md / file_path），缺失时显式
   warning 留审计痕迹；派发门禁阻断空壳行使用（①落地）；历史空壳数据（sid=3276）
   由所有者经 `/api/skills/upload` 重传补齐（风险表约定：历史数据修复单独处理）。

## 验收标准

- [ ] 场景绑定用户级技能派发后，工作区 `.skills/<技能名>/SKILL.md` + `references/` 完整存在
- [ ] 物化失败时任务派发显式告警并阻断，不静默降级
- [ ] `grape skills detail --skill_id <技能名>` 对用户级场景技能可用（不再 422）
- [ ] sid=3276 物化源缺失根因定位并修复
- [ ] 所有现有测试通过；新增测试覆盖门禁、name→sid 解析、完整性校验
