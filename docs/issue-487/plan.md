# Plan: Issue #487 — 修复智能推荐规则无法保存

## 方案

三处小改 + 测试补齐，接口契约（URL/入参/成功响应）不变，仅新增失败 4xx 路径。

### 1. 后端 repo_key 解析统一（src/api/routes/repos.py）

- 新增本地 `_resolve_repo_key(owner_repo)`（与 `scenes.py` 同款：裸名查 `managed_repos` 中 `gitcode:`/`github:` 实际前缀，已带前缀幂等）。
- GET/PUT `/scene-recommend-config` 改用该解析，消除固定 `gitcode:` 前缀错 key；读取路径与写入 key 对齐保证回显。
- PUT 将 DB 层 `ValueError` 映射为 400（空场景等），写入不满足（upsert 兜底后仍 0 行）返回 400 而非假成功。

### 2. 落库兜底与显式失败（src/user/db.py）

- `set_scene_recommend_config`：
  - 场景列表为空 → `raise ValueError("场景列表为空...")`（不再静默 return）；
  - UPDATE 0 行 → INSERT OR IGNORE 兜底（按 repo_key 补行）再 UPDATE；仍 0 行 → 返回 `False`（调用方显式报错）；
  - 成功返回 `True`。
- `get_scene_recommend_config` 保持幂等 `to_repo_key` 归一（与写入同一 key 体系）。

### 3. 前端竞态防护（src/api/static/js/pages/dashboard.js）

- 弹窗初始：「添加规则」「保存」按钮 `disabled`；
- 场景列表加载成功后启用；加载失败保持禁用并 toast 提示；
- `_addRecRuleRow` 在 `sceneOptions` 为空时直接返回并提示（双保险）；
- `_saveSceneRecommendConfig` 读取失败响应中的 detail.message 展示（已有），保持。

### 4. 测试

- 后端 `tests/test_scene_recommend_config.py`（TestClient）：
  - 保存后读回 fallback_scene + rules；
  - `github:` 前缀托管仓（managed_repos 只有 github: 行）读写正确（即 _resolve_repo_key 命中）；
  - 空场景列表 → PUT 返回 400；
  - UPDATE 0 行时 INSERT 兜底（直接调 db 函数，managed_repos 无行场景）。
- 前端 `tests/js/test_issue_487_scene_recommend_save.js`（node）：
  - 按钮初始禁用、加载完成后启用；
  - 加载失败保持禁用；
  - 保存失败 toast 展示错误信息。

## 风险与缓解

- `_resolve_repo_key` 行为变更影响其他调用方：不改既有函数，仅在 scene-recommend 端点新用；跑全量测试。
- INSERT 兜底并发冲突：`INSERT OR IGNORE` + repo_key 主键，幂等。
- 前端禁用态遗漏失败分支：失败时保持禁用 + toast。
