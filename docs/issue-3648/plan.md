# Issue #3648 方案设计

## 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 撞号收敛 | renumber 优先（202611210001/2/3） | 旧号在 dev 三库已被旧名占用，renumber 后可正常跑 |
| 执行器兜底 | `post_check` + reconcile | 覆盖历史库和未来跨分支撞号，禁止「目标缺失仍跳过」 |
| 盖章时机 | fn → post_check → 盖章 | 根治「执行即盖章」的假绿 |
| 无 post_check 撞号 | ERROR 跳过（fail-safe） | 不盲目重跑未知历史迁移；日志已非静默 |
| 读路径 | `log.exception` + 503 | schema 故障必须可见，不能伪装空列表 |
| 前端缓存 | 进入视图即拉取 | 管理员更新分类后立即可见，避免会话级旧树 |

## 改动清单

### `src/api/db_setup.py`

- `Migration` 新增 `post_check: Callable | None`；`register_migration` 透传。
- `_run_migrations_with_conn`：
  - 同号不同名且有 `post_check`：先校验；缺失则加入 `_reconcile`，重跑后校验再盖章；
  - pending 循环 fn 成功后调用 `post_check`，失败 `continue`（不盖章）。
- `_fn_skill_classification_columns` 改号 `202611210001`，`post_check` 委托
  `skill_detail_tables.verify_skill_classification_columns`。
- 新增 `verify_migration_targets()`：启动收口后核对 mgmt 活跃一级集合与 runtime 列。
- `run_pending_migrations` / `run_domain_migrations` 调用总校验。

### `src/domains/mgmt/migrations/category_collapse_dict.py`

- 改号 `202611210002`；新增 `verify_category_collapse()` 并挂 `post_check`。
- 校验：`level=1 AND is_active=1` 的 id 必须恰为 `[1001, 1002, 1010]`。

### `src/domains/runtime/migrations/skill_category_collapse.py`

- 改号 `202611210003`；新增 `verify_skill_category_collapse()` 并挂 `post_check`。
- 校验：`cat_l1_id/cat_l2_id` 在；无指向非保留一级的 skill 行。

### `src/domains/runtime/migrations/skill_detail_tables.py`

- 新增 `verify_skill_classification_columns()`：缺列 raise（不盖章）。

### `src/domains/mgmt/routes/skills.py`

- `list_skills` 的 `skill_search` 异常：`log.exception` + `HTTPException(503)`。

### `src/api/frontend/app/src/views/SkillsView.vue`

- `loadCategories()` 删除 `if (categories.value.length) return`，进入视图/切 tab 刷新。
- `src/api/frontend/shell/index.html` `skills-view.js?v=60→61`，同步缓存版本守卫生成值。

## 测试策略

新增 `tests/api/test_issue_3648_migration_reconcile.py`：

1. 旧号不再注册、新号注册且带 post_check、runtime 补列先于收敛；
2. 旧语义（无 post_check）目标缺失跳过 —— 红；
3. 新语义（带 post_check）目标缺失重跑 + 改盖新名 —— 绿；
4. 目标已到位不重跑；
5. post_check 失败不盖章；
6. mgmt / runtime 后置校验行为；
7. `list_skills` schema 异常 503 + ERROR；
8. SkillsView 不再会话级缓存；
9. 启动期 `verify_migration_targets` 拦截缺列 / 活跃一级≠3。

回归：`test_issue_536_migrations`、`test_category_columns_repair`、
`test_migration_track_guard`、域归属守卫、`test_issue2761_classification_tags`。

## 风险

| 风险 | 缓解 |
|------|------|
| 执行器改动影响全部迁移 | 新增字段可选、默认行为不变；相关执行器测试全绿 |
| 新号仍可能与其它未合并分支撞号 | 取当前源码最大号之后的 20261121 段；`post_check` 兜底 |
| 三库进程只看到本域对象 | 总校验按 domain 只查本域；post_check 在属主域执行 |
| 前端缓存版本守卫 | 同步 `FROZEN` 中 `skills-view.js` 版本 |