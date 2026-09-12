# Issues 对外接口实施执行计划

> 关联设计：`docs/weekly-02/issues-api-design.md`
> 目标：新增 `/api/issues` 对外接口，澄清 Issue/Event/Task 概念，并迁移 `tasks/create`
> 分支：`weekly-02-lyz`
> 状态：**实施计划（待执行）**
> 日期：2026-09-11

---

## 一、实施原则与治理

1. **分阶段提交**：每阶段独立 commit，可单独验证/回滚；阶段间不留半成品。
2. **不破坏现有行为**：除 `POST /api/tasks/create`（按设计废弃）外，`events/tasks` 既有端点不回归。
3. **表迁移采用渐进式**（设计 1.1.4）：`tasks` → `issue` 改名 + 分批收编，零数据迁移。
4. **先建 `issue` 表 + 接口（Phase A）→ 再迁移表（Phase B）**：接口先 join 读，表迁移后改单表读，降低耦合。
5. 每次 push 前跑 `pytest tests/test_issues_api.py tests/test_api_routes.py tests/test_help_directory.py -q`。

---

## 二、阶段划分与任务清单

### Phase A：新增 `/api/issues` 接口（读聚合为主）

| # | 任务 | 产出 | 验收 | 依赖 |
|---|------|------|------|------|
| A1 | 新建 `src/api/routes/issues.py` | `GET /api/issues/{owner}/{repo}` 列表 | 返回 stage/task_status/owner_agent/events_count/latest_event；空仓库 `{issues:[], total:0}`；401 | — |
| A2 | 同上文件 | `GET /api/issues/{owner}/{repo}/{number}` 详情 | stage + task_status + events[] + pipeline{}；404 | A1 |
| A3 | 同上文件 | `GET .../{number}/events` | 仅该 issue 事件，seq 升序；404 | A1 |
| A4 | 同上文件 | `GET .../{number}/pipeline` | event.json 链（summary/status/segments[]）；无档案降级 events 重构 | A1 |
| A5 | 同上文件 | `POST /api/issues/{owner}/{repo}`（创建） | 复用 `ensure_issue_task` + GitCode create；400/409/502 | A1 |
| A6 | `src/api/main.py` | `include_router(issues_router)` | `/api/issues/*` 可达 | A1-A5 |

> A1-A5 数据聚合：`issues.py` 内复用 `get_issue_stage`（issue_state）+ `get_events_grouped_by_issue`（events）+ `load_event_json`（pipeline）；`stage/status` 双字段（期初 join 双表，见 1.1.4 过渡）。

### Phase B：`tasks` → `issue` 表迁移（渐进式）

| # | 任务 | 产出 | 验收 | 依赖 |
|---|------|------|------|------|
| B1 | `db_setup._migrate_tables` | `ALTER TABLE tasks RENAME TO issue`（幂等）+ `_ISSUE_DDL` | 已存在 `issue` 则跳过；老库 `tasks`→`issue` | — |
| B2 | `db_setup` 补列（收编批 A） | `issue` 加 `stage`/`owner_agent`（默认 vod） | `_ensure_column`；老 tasks 行 stage 默认 vod | B1 |
| B3 | `db_setup` 补列（收编批 B） | `scene_id`/`scene_user_id`/`allow_scene_override` | 补列成功 | B2 |
| B4 | `db_setup` 补列（收编批 C） | `a2a_seq`/`parent_issue_id`/`is_subtask_mode`（命名对齐） | 旧 `parent_issue_number` 弃用或改名 | B3 |
| B5 | 存量回填 | issue_state 数据按 repo_key+issue_id 并入 issue（`UPDATE issue SET stage=..., scene_id=... WHERE repo_key=? AND issue_number=?`） | 迁移后 issue_state 与 issue 对齐（抽样核对） | B4 |
| B6 | 消费方表名替换（`tasks`→`issue`） | `session_store.py`/`routes/tasks.py`/`routes/pipeline.py`/`im/handler_async.py`/`event_pipeline.py`/`runtime/service.py`/`prompt_builder.py`/`repogit.py`/`onboarding.py`/`cleanup.py` | 全局 grep `FROM/INTO/UPDATE tasks` 归零（除 agent_tasks） | B5 |

> 收编批 D（`body`/`status_updated_at`）按需做；`agent_tasks` 不参与改名。

### Phase C：接口与表统一 + 收尾

| # | 任务 | 产出 | 验收 | 依赖 |
|---|------|------|------|------|
| C1 | `issues.py` 改单表读 | `GET /api/issues/*` 从 `issue` 单表出 stage/status 全量（不再 join issue_state） | 字段与 join 一致 | B6 |
| C2 | `POST /api/tasks/create` 废弃 | 移除/标注迁移提示（文档 `01-tasks.md`）；调用方改 `/api/issues` | 调用方（CLI/Agent）文案更新 | A5 |
| C3 | `issue_state` 停写 | pipeline 写路径改 `issue`；`issue_state` 只读 | 无新写入 issue_state | B6/C1 |
| C4 | 文档 | `help/api/11-issues.md` + `README.md` 目录 + `EXPECTED_API_FILES` + 01-tasks 迁移说明 | 契约/文档一致 | C1 |
| C5 | 迁移验收 | `issue_state` 数据核对后 `DROP`（保留备份）；全量回归 | 任务中心建/改/催/完成/取消 + pipeline 各阶段正常 | C3/C4 |

---

## 三、依赖与顺序

```
A1 → A2/A3/A4 → A5 → A6 ──┐
                           ├─ B1 → B2 → B3/B4 → B5 → B6 ──┐
                           └───────────────────────────────┼─ C1 → C2 → C3 → C4 → C5
```

**关键**：
- A 阶段先交付 `/api/issues`（join 读），业务角度立即可用、风险低。
- B 阶段表迁移独立，风险集中在 B6（全局表名替换）。
- C 阶段统一单表 + 废弃 tasks/create + 收尾。

---

## 四、测试执行策略

| 阶段 | 必须跑的测试 | 说明 |
|------|-------------|------|
| A 完成 | `test_issues_api.py` A 相关 + `test_api_routes.py test_auth_routes.py` | 接口可用、无回归 |
| B 完成 | `test_issues_api.py` + `test_tasks.py` + `test_api_routes.py` + help | 表迁移后任务中心不回归 |
| C 完成 | 全部 + `test_e2e`（如有）+ 手工回归 | 终态 |

---

## 五、风险与应急

| 风险 | 影响 | 缓解 |
|------|------|------|
| B1 rename 与并发写冲突 | 极低（启动期迁移） | 启动前确认无进行中任务；`CREATE TABLE IF NOT EXISTS issue` 幂等 |
| B6 全局表名替换遗漏 | 运行期 SQL 报错 | 合并前后 grep 校验 + 任务中心回归 |
| `parent_issue_number`/`is_subtask_mode` 双命名 | 语义歧义 | 统一 issue_state 命名，旧列弃用 |
| tasks/create 废弃波及 CLI | CLI 调用失败 | 提前更新 CLI 契约/文档，交互提示迁移 |

---

## 六、里程碑

| 里程碑 | 判定 |
|--------|------|
| M1（A 完成） | `/api/issues` 5 端点可用，文档 11-issues.md 产出，含概念澄清表 |
| M2（B 完成） | `issue` 表 + 字段收编 + 消费方改读，任务中心回归绿 |
| M3（C 完成） | tasks/create 废弃、issue_state 停写/drop、单表接口统一，全量回归绿 |