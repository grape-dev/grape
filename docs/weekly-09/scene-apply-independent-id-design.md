# Spec: 场景市场应用冲突 —— 每次应用生成独立 scene_id + 应用弹窗支持命名

> 来源：主人 2026-10-29 对话确认 | 目标分支：weekly-08-mys（本地）| 文档归属：weekly-09

## 背景问题

市场场景应用（`apply_market_scene`）当前把**市场模板名当作仓库内 scene_id**（如 `vod`），
且 display_name 查重撞名直接 409。后果：

1. 同一仓库内，用户A 应用了官方模板后，用户B 想应用同一模板（或自建同名场景）→
   409「仓库中已存在名为'xxx'的场景」，互相挡路；
2. 更隐蔽的风险：自建场景若恰好用了模板名作 scene_id，
   `create_custom_scene` 按 `scene_id + repo_key` 查重后会**静默 UPDATE 覆盖**已有行。

根因：市场模板名、用户自定义 scene_id、display_name 共享一个扁平命名空间（仓库内）。

## 方案（主人确认）

1. **scene_id 唯一化**：应用市场场景时不再用 `scene["name"]`，改为
   `custom-{uuid4().hex[:8]}`（沿用现有惯例）——每次应用产生独立行，互不覆盖。
2. **幂等口径保留**：同一仓库重复应用同一市场场景仍按
   `market_scene_id + repo_key` 查重 → 已存在则走「更新」（覆盖更新该行配置），
   语义不变。
3. **名称可编辑**：应用弹窗（applyOptionsModal）顶部新增「场景名称」输入框，
   预填模板 display_name，**每次应用都弹窗**，用户可确认或改名。
4. **撞名校验保留**：后端继续按仓库内 display_name 查重，撞名返回 409；
   前端收到 409 时在**应用弹窗内**提示改名重试（不再引导去改旧场景）。

## 改动范围

### 后端 `src/api/routes/scenes.py` — `apply_market_scene`

- 接受 body/query 参数 `display_name`（空则用模板 display_name）。
- 新建路径：`scene_id = f"custom-{uuid.uuid4().hex[:8]}"`（不再用 `scene["name"]`）；
  `create_custom_scene(name=用户提供的名称)`。
- 更新路径（幂等命中）：同步把 name 更新为用户提供的名称
  （`update_custom_scene` 传 `name=`）。
- 撞名 409 保留，消息不变（前端改用弹窗内提示）。

### 前端 `src/api/static/js/pages/scene-market.js`

- `applyOptionsModal` 标题下方新增输入框：label「场景名称」，
  预填模板 display_name（`check.scene_name` 或市场卡片名）。
- `_doApplySceneWithOptions` 提交时带上 `display_name` 参数。
- 409 处理改为：不关闭场景冲突独立弹窗引导跳转，改为在应用弹窗内
  输入框下方红字提示 + 聚焦输入框（弹窗不销毁，用户改名即可重试）。
  为此 `_doApplySceneWithOptions` 需能回传失败让弹窗处理，或 409 时重建弹窗并预填用户输入。

### i18n `locales/{zh,en}/skills.json`

- `apply_scene_name_label`: 场景名称 / Scene Name
- `apply_scene_name_conflict_inline`: 仓库中已有同名场景，请换个名称 / A scene with this name already exists in this repo. Try another name.

### 兼容性

- 存量已应用行（scene_id 为旧模板名）不动；幂等键 `market_scene_id + repo_key`
  照样识别，重复应用走更新路径，无数据迁移。
- `experience-check` / 体验场景路径（771 行同款 409）本期不动（体验路径自动建仓，
  冲突概率低），仅记录。
- `webhook` 的 scene 回退链（自定义 → 官方 pipelines.json → default）：自定义
  scene_id 本来就不在官方表，行为不变。

## 验收标准

- [ ] 同一仓库两个用户先后应用同一官方模板，各自起不同名 → 两条独立
  `devops_pipeline_scene` 行，scene_id 均为 `custom-*`，互不影响。
- [ ] 应用弹窗每次弹出，名称可编辑，默认为模板名。
- [ ] 撞名时：弹窗不关闭，输入框下红字提示，改名后可重试成功。
- [ ] 同一仓库重复应用同一市场场景（幂等命中）→ 更新已有行，名称更新为本次输入。
- [ ] 旧模板名 scene_id 的存量行，重复应用仍走更新（不新建第二行）。
- [ ] 现有相关测试通过；新增/更新单测覆盖：新 scene_id 生成、幂等更新、409 分支。
