# 方案设计：Issue #3798

## 1. 字典不可用 → 轮级 ERROR + 启动降级打点

`skill_sync` 新增 `_load_skill_category_map()`，把原来内联的 `try/except → logger.debug`
改为两条可判定路径（**都在 `_sync_skills_to_db` 每轮调用一次，因此天然「每轮 1 条」**）：

| 情形 | 日志 | 打点 |
|---|---|---|
| `skill_category_active_map()` 抛异常（缺表 / DSN 错 / 跨域通道坏） | `logger.error("... dictionary read failed ... (reason: <exc>)")` | `record_startup_degraded("skill_category", "read_failed")` |
| 读到空字典（表在但无启用行：种子缺失 / 整表停用） | `logger.error("... dictionary is empty ... check the mgmt-domain skill_category seed / is_active state")` | `record_startup_degraded("skill_category", "empty")` |

二者都只降级、不中断同步（`category_id=0` 语义保持不变 → 不打散既有行为）。
打点复用 `src/kernel/startup_metrics.record_startup_degraded`（未安装指标时 no-op，
不引入 prometheus 依赖）：生产事故排查从「grep 关键词」升级为「计数器 + 日志」双信号。

## 2. 未命中 slug → 每轮 ≤1 条聚合

与 #3796 `scene_sync` 同构，新增两个薄函数：

```
_record_unresolved(sink, slug, skill_name)  # slug → {"count": N, "skills": [≤3 样例]}
_log_unresolved_misses(sink)                # 轮末 ≤1 条 WARNING；空 sink 不发日志
```

- 循环内：未命中 → 进 sink + `logger.debug("... unresolved in skill_category
  (aggregated per round)")`（逐技能明细保留，级别降到 DEBUG）；
- 循环后（`_db.commit()` 之后）：一次性输出
  `skill sync: <K> sub_scene slug(s) unresolved in skill_category this round, category_id
  stays 0 (aggregated per round): 'slug'×N skills (e.g. a, b, c); ...`；
- **文案不含旧字面量** `has no matching skill_category slug`：Loki 按该串计数的告警规则
  归零（验收项），信号由聚合行承载。样例上限复用常量 `_UNRESOLVED_SAMPLE_LIMIT = 3`。

## 3. `content-generation` 补种（mgmt 迁移 `202611240012`）

新文件 `src/domains/mgmt/migrations/skill_category_content_generation_seed.py`：

- `seed_content_generation_category(conn=None)`：
  `INSERT_IGNORE_SKILL_CATEGORY`（mgmt `migrations_ddl` 单一来源）插 `内容生成 /
  content-generation / ✍️ / order_num=16`；随后
  `UPDATE skill_category SET is_active=1 WHERE slug=? AND is_active<>1`
  ——同步侧只认 `is_active=1`，行在而停用等于没种；
- `verify_content_generation_category(conn=None)`：post_check，行在位且启用才盖章，
  否则 `RuntimeError`（不盖章 → 下次启动重试）；
- `MIGRATION = {version 202611240012, kind "backfill", domains ("mgmt",), post_check}`；
  注册在 `src/api/db_setup.py`，**字面量**登记 version/name/kind（供
  `tests/api/test_migration_track_guard.py` 解析）。

**与既有迁移的边界**（同一张表、口径不同，必须区分）：

- 202611210002 / 202611230001：把老分类体系**整体停用**（`grape_t_category_dict` 收口）；
- 202611230002（runtime）：把老列 `skill.category_id` **清零**；
- 本迁移：**不重开**上述 15 条，只新增并启用 `content-generation` 这一条新 slug，
  服务内容生成类技能的老口径 `category_id` 解析。分类的正式口径仍是
  `cat_l1_id`/`cat_l2_id`（`/api/categories/tree`）。

## 4. 影响面与回归

| 文件 | 改动 |
|---|---|
| `src/domains/runtime/storage/skill_sync.py` | 新增 3 个函数；`_sync_skills_to_db` 接入 sink/聚合；去掉静默 DEBUG |
| `src/domains/mgmt/migrations/skill_category_content_generation_seed.py` | 新增（种子 + post_check + MIGRATION） |
| `src/api/db_setup.py` | 注册迁移（字面量） |
| `tests/runtime/test_issue_3798_skill_sync_category.py` | 新增 15 条用例 |
| `tests/runtime/test_issue179_skills.py` | 种子行数断言 15 → 16（种子集合法增长） |

验证命令：`pytest tests/runtime/test_issue_3798_skill_sync_category.py
tests/runtime/test_issue179_skills.py tests/arch tests/api/test_migration_track_guard.py
tests/api/test_migration_version_integrity.py tests/api/test_issue_536_migrations.py`、
`python3 scripts/check-arch.py`、`make lint`。

## 5. 部署后核对（SRE）

1. Loki `has no matching skill_category slug` 计数 → 0（字面量已从代码移除）；
2. 日志出现 `skill sync: N sub_scene slug(s) unresolved in skill_category this round`
   （每轮 ≤1 条），可据此定位仍在用的停用老 slug；
3. `SELECT slug, is_active FROM skill_category WHERE slug='content-generation'` → 1 行、`is_active=1`；
4. 官方技能同步一轮后，`sub_scene='content-generation'` 的 `category_id<>0`。