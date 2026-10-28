# Spec: Issue #487 — 智能推荐规则选择后无法保存

## 背景

用户报告「智能推荐规则」弹窗中选择兜底场景与规则后点「保存」，接口返回"已保存"，但配置未真实持久化，重开弹窗不回显（P1，静默丢数据）。

## 根因（代码走查定位）

1. **repo_key 前缀错位（主因）**：`src/api/routes/repos.py` 的 GET/PUT `/scene-recommend-config` 固定 `to_repo_key(f"{owner}/{repo}")`（默认 `gitcode:` 前缀），而场景列表 `/scenes/with-pipeline` 走 `_resolve_repo_key`（查 `managed_repos` 解析真实前缀）。GitHub 托管仓前缀为 `github:` 时读写用错 key：写入不存在的行 → UPDATE 0 行 → 假成功。
2. **落库静默失败**：`src/user/db.py` `set_scene_recommend_config`：
   - `get_scenes(repo_key)` 为空时直接 `return`，不写库也不报错；
   - UPDATE 0 行时无 INSERT 兜底，仍返回成功。
3. **前端加载竞态**：`dashboard.js` `_openSceneRecommendModal` 场景列表异步加载完成前即可点「添加规则/保存」，`sceneOptions` 为空 → 规则行下拉无选项/规则被静默跳过。

## 需求

- 保存后 `GET scene-recommend-config` 能读回刚保存的 fallback_scene 与 rules。
- gitcode: / github: 双前缀仓库均正确读写。
- 空场景 / 写入不满足时返回明确 4xx，不再假成功。
- 前端场景列表未加载完成时禁用「添加规则/保存」，失败有提示。
- 补齐后端 + 前端回归测试，现有测试全过。

## 范围

- In：`src/api/routes/repos.py`（repo_key 解析 + 错误映射）、`src/user/db.py`（落库兜底与显式失败）、`src/api/static/js/pages/dashboard.js`（竞态防护）、`tests/`（新增用例）。
- Out：推荐算法本身、UI 重构。

## 验收标准

1. 保存后 GET 能读回 fallback_scene 与 rules；
2. 双前缀仓库读写 key 一致；
3. 空场景/写入失败返回明确 4xx；
4. 前端未加载完成时按钮禁用、失败有提示；
5. 新增回归测试通过 + 现有测试全过。
