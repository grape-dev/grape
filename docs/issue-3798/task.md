# 任务拆解：Issue #3798

- [x] **T1 字典不可用口径**：`skill_sync._load_skill_category_map()`（异常 →
  `read_failed`，空字典 → `empty`；各 1 条 ERROR + `record_startup_degraded`），
  `_sync_skills_to_db` 改调该函数。
- [x] **T2 未命中轮级聚合**：`_record_unresolved` / `_log_unresolved_misses`；
  循环内降 DEBUG 入 sink，轮末 ≤1 条 WARNING（slug + 次数 + ≤3 样例，无旧字面量）。
- [x] **T3 种子补种**：`mgmt/migrations/skill_category_content_generation_seed.py`
  （`content-generation`，幂等插入 + 启用重申 + post_check）+ `db_setup` 注册
  `202611240012`。
- [x] **T4 回归用例**：`tests/runtime/test_issue_3798_skill_sync_category.py`
  （聚合计数/样例上限/无告警/ERROR+打点/空字典/端到端 1 条 WARNING/端到端 ERROR/
  种子幂等/启用映射命中/post_check 闸门/`category_id` 非 0）。
- [x] **T5 既有用例对齐**：`test_issue179_skills.py` 种子行数 15 → 16（含注释说明）。
- [x] **T6 门禁**：silent-except 棘轮、迁移号完整性、迁移双轨守卫、
  `scripts/check-arch.py`、`make lint`、架构与运行时用例。
- [ ] **T7 交付**：提交 → 推送 `feat/issue-3798` → 建 PR（base `arch-weekly-11-ljt`）
  → Issue 评论告知 → A2A 通知 QA。
- [ ] **T8 部署核对（SRE）**：Loki 同名告警计数 0；`content-generation` 行 `is_active=1`；
  内容生成类技能 `category_id<>0`。

## 未随本次合入（需产品决策）

- 老分类体系（15 条，202611210002/202611230001 已停用、202611230002 已清零
  `skill.category_id`）**不重开**：`tests/runtime/test_issue179_skills.py` 中依赖
  「15 条全部启用 + 官方技能 `category_id<>0`」的 5 条断言仍红（基线亦红，本次由 6 条
  降至 5 条）。若产品要求恢复老分类 Tab，应单独立项（涉及分类树/前端与两处迁移
  post_check）。