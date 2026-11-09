# Plan: Issue #486

## 方案
前后端一致口径，复用 #277 的可见性闭包 `_make_visible_credential_fn(repo_filter)` 与 `_build_cred_map`：

### 前端 `src/api/static/js/pages/autopilot.js`
1. 新增模块级状态：`_apFormRepoKey`（当前表单仓库 full_name，无前缀）、`_apEnginesRepoKey`（引擎缓存对应仓库，按 repo 缓存防串缓存）、`_apDetachedEngineIds`（编辑态原绑定引擎超出可见范围的集合）。
2. `_apShowDialog`：先解析任务 repo_key（编辑态用 `existing.repo_key`，新建用 `app.currentRepo()`，与工作分支加载同源）存入 `_apFormRepoKey`，再按「缓存为空或 repo 不匹配」决定是否重新加载引擎。
3. `_apLoadEngineList`：请求追加 `?repo=<fullName>`（复用 #473 runtimes.js 传参模式），返回 Promise；加载后计算 detached 集合（选中 ∩ 不可见）。
4. `_apOpenEnginePicker`：缓存为空或 repo 变化时重新拉取后再打开。
5. 选择器渲染：detached 引擎单独分组展示，checkbox 勾选态保留（可见当前绑定）但带「已不在当前可用范围」警示文案；仅允许取消（移除），不允许重新勾选。
6. i18n：`locales/{zh,en}/autopilot.json` 新增 `engine_picker.detached_group` / `engine_picker.detached_hint`。

### 后端 `src/api/routes/autopilot.py`
1. 新增 `_validate_engine_runtime_ids(user, repo_key, engine_ids)`：
   - 空列表（广播模式）不校验，行为不变。
   - 函数体内局部导入 `settings._build_cred_map` / `_make_visible_credential_fn`（避免循环依赖）。
   - 按 `from_repo_key(repo_key)` 作为 repo_filter，逐 runtime 查 `agent_runtimes.credential_id` → `runtime_credentials`，凭据 revoked 或不可见 → 400，message 列出越权 ID。
2. `create_task`：repo_key 规范化后、INSERT 前校验。
3. `update_task`：`_check_task_access` 返回任务 repo_key，与 `body.repo_key`（若有）取生效仓库校验；未提交 `engine_runtime_ids`（None）不校验。

## 关键决策
- 越权显式报错（400）而非静默剔除，与 Spec 一致。
- detached 引擎保留展示 + 禁选提示，不强制清空，由用户显式改选（Spec 风险缓解项）。

## 测试计划
新增 `tests/test_issue_486_autopilot_engine_filter.py`（参考 `test_issue_277_repo_isolation.py`）：
- 单元级：`_validate_engine_runtime_ids` 对 global / 本人 user / 当前仓库 repo 引擎放行；对其他仓库 repo、他人 user、不存在 ID、revoked 凭据拒绝。
- API 级：TestClient + dependency_overrides(get_current_user)（session_token 作用域跳过 repo 访问校验，聚焦引擎归属校验）：POST /api/autopilot/tasks 越权 engine_runtime_ids → 400；合法 → 200；PUT 同样覆盖；空列表（广播）不受影响。
- 回归：`tests/test_issue_277_repo_isolation.py` 等现有测试保持通过。
