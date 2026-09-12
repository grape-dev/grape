# Issues 对外接口实施执行计划

> 关联设计：`docs/weekly-02/issues-api-design.md`
> 目标：新增 `/api/issues` 对外接口，澄清 Issue/Event/Task 概念，并迁移 `tasks/create`
> 分支：`weekly-02-lyz`
> 状态：**实施计划（执行中：B2/C 部分完成）**
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

### Phase B：`tasks` → `issue` 表迁移（渐进式，拆为两个子阶段）

> **B1（先，只改表名）**：`tasks` → `issue`，**不新增字段**，确保功能正常后再继续。
> **B2（后，加字段）**：在 `issue` 上逐步收编 `issue_state` 的字段。

#### Phase B1：仅表改名（不增字段）

| # | 任务 | 产出 | 验收 | 依赖 |
|---|------|------|------|------|
| B1.1 | `db_setup` 迁移 | `ALTER TABLE tasks RENAME TO issue`（幂等：已存在 `issue` 跳过）；`_TASKS_DDL` → `_ISSUE_DDL`（结构=原 tasks） | 老库 `tasks`→`issue`；列结构原样 | — |
| B1.2 | 消费方表名替换 | 10+ 文件 SQL `FROM/INTO/UPDATE tasks` → `issue`（除 `agent_tasks`） | 全局 grep 归零；任务中心/接口功能回归绿 | B1.1 |

> B1 里程碑：**表名已是 `issue`，字段仍同旧 tasks**（status/title/issue_number/task_description/conversation_id/...），功能零变化。

#### Phase B1 实施记录（2026-09-12，已上线生产）

> **坑 1：迁移顺序导致空 `issue` 表吞数据**
>
> 初始实现把 rename 放在 `_migrate_tables()` 里，而 `init_database()` 顺序是
> `_create_tables()` → `_migrate_tables()`。`_create_tables` 的 `CREATE TABLE IF NOT EXISTS issue`
> **先建出空 `issue` 表**，`_migrate_tables` 检查 `"issue" in tables` → **跳过 rename** →
> 数据滞留旧 `tasks` 表、空 `issue` 供代码读 → 迁移失败（生产实测：issue 0 行、tasks 182 行）。
>
> **修复**：抽出 `_tasks_to_issue_rename_pre_create()`，在 `_create_tables()` **之前**执行；
> 逻辑处理三种情况：
> 1. 无 `tasks` 表 → skip（新库）
> 2. 有 `tasks` 且无 `issue` → `ALTER TABLE tasks RENAME TO issue`
> 3. 有 `tasks` 且 `issue` 存在：若 `issue` 为空且有 tasks 数据 → `DROP TABLE issue`（误建空表）+ rename；
>    否则（issue 非空）→ skip（正常已迁移）
>
> 生产验证：`Phase B1: tasks -> issue (dropped empty issue, renamed), 182 rows` —— 数据完整迁移。

> **坑 2：表名替换需覆盖测试文件**
>
> `FROM/INTO/UPDATE tasks` 的 SQL 在 **src 53 处 + 测试 74 处**。若只改 src，
> 测试里 `INSERT INTO tasks` 会因 rename 后无 `tasks` 表而失败。需 src + tests 同步替换。

> **坑 3：`_tasks_to_issue_rename_pre_create` 依赖 `_create_tables` 前的时机，不能放迁移列表里**
>
> rename 必须在 `CREATE TABLE IF NOT EXISTS issue` 之前 —— 迁移列表（`_migrate_tables`）时机太晚。
> 因此拆成独立函数，在 `init_database` 的 SQLite/MySQL 两个分支的 `_create_tables()` 前显式调用。

**B1 关键产物**：
- `src/api/db_setup.py`：`_ISSUE_DDL`（结构=原 tasks）+ `_tasks_to_issue_rename_pre_create()`（pre-create 迁移）
- src 10 文件 + tests 12 文件：表名 `tasks`→`issue`（agent_tasks 不受影响）
- 提交：`cccd891a`（表名替换+迁移）+ `e9c92d56`（迁移顺序修复）
- 验证：本地 `test_issues_api` 14 passed；生产 `tasks`(182)→`issue`(182) 数据完整、服务 active

#### Phase B2：字段收编（ADD COLUMN）

| # | 任务 | 产出 | 验收 | 依赖 |
|---|------|------|------|------|
| B2.1 | 补列（批 A） | `issue` 加 `stage`（默认 vod）、`owner_agent`（默认 vod） | `_ensure_column`；老 issue 行 stage 默认 vod | B1 |
| B2.2 | 补列（批 B） | `scene_id`/`scene_user_id`/`allow_scene_override` | 补列成功 | B2.1 |
| B2.3 | 补列（批 C） | `a2a_seq`/`parent_issue_id`/`is_subtask_mode`（命名对齐） | 旧 `parent_issue_number` 弃用或改名；is_subtask_mode 去重 | B2.2 |
| B2.4 | 存量回填 | `issue_state` 数据并入 `issue`（stage/scene/owner_agent/...） | 迁移后两表对齐（抽样核对） | B2.3 |
| B2.5 | 读写切换 | pipeline 写 `issue`；读 `issue` 优先、`issue_state` fallback | 双读一致 | B2.4 |

> 批 D（`body`/`status_updated_at`）按需做；`agent_tasks` 不参与改名。

#### Phase B2 实施记录（2026-09-12，部分上线）

**B2 已完成**：
- B2.1/B2.2/B2.3 补列：`issue` 加 `stage`/`owner_agent`/`scene_id`/`scene_user_id`/`allow_scene_override`/`a2a_seq`/`parent_issue_id`/`updated_at`（`_ensure_column`，幂等）
- B2.4 存量回填：`UPDATE issue SET stage=..., owner_agent=..., scene_id=..., scene_user_id=..., allow_scene_override=..., a2a_seq=..., parent_issue_id=..., is_subtask_mode=..., updated_at=... FROM issue_state`（按 `issue_id=='#'||issue_number` 关联，权威对齐两表）
- B2.5 读写切换（双写 + 双读）：
  - **写**：`session_store.set_issue_stage / set_issue_scene / set_scene_override_flag / set_parent_issue / set_subtask_mode` 统一经 `_sync_issue_fields()` 双写 `issue` 单表（issue 有卡片行则同步）
  - **读**：`get_issue_stage` **`issue_state` 优先 + `issue` 兜底**（见坑 4）

> **坑 4：读切换方向反了——`issue_state` 才是权威阶段机，`issue` 只能兜底**
>
> 初版按「读 `issue` 优先、`issue_state` fallback」实现。实测 `test_issues_api::test_aggregates_stage_and_status`
> 直接把 stage 写在 `issue_state` 表（绕过双写），若 read 优先 `issue`（stage 默认 vod）→ 返回 'vod' 而非 'qa'。
> 且 pipeline 阶段写在 `issue_state`（权威，Issue #85 联动 `task.status` 亦由 set_issue_stage 触发）。
> **修复**：过渡期 `issue_state` 为权威读，`issue` 卡片行兜底；等 C3 停写/迁移验收后再切单表。

> **坑 5：B2 须补 `parent_issue_id` 列——旧 `parent_issue_number` 保留兼容输出**
>
> `issue` 表原仅 `parent_issue_number INTEGER`（multi-task 遗留）。B2.3 命名对齐新增 `parent_issue_id TEXT`（如 "#20"），
> `tasks._create_task`/`ensure_issue_task` INSERT 同时写两列，`_query_tasks` SELECT 双字段输出，
> 前端 `parent_issue_number` 兼容展示不受影响。

**B2 关键产物**：
- `src/api/db_setup.py`：B2 补列 + 回填块（幂等）
- `src/api/session_store.py`：`_sync_issue_fields()` + 5 个 set_* 双写 + `get_issue_stage` 双读
- `src/api/routes/tasks.py`：`_get_issue_parent_id()` + INSERT/SELECT 双列
- 验证：本地 `test_issues_api` 14 passed + 端到端双写脚本（issue 行 9 字段与 get_* 一致）

### Phase C：接口与表统一 + 收尾

| # | 任务 | 产出 | 验收 | 依赖 |
|---|------|------|------|------|
| C1 | `issues.py` 改单表读 | `GET /api/issues/*` 从 `issue` 单表出 stage/status 全量（不再 join issue_state） | 字段与 join 一致 | B6 |
| C2 | `POST /api/tasks/create` 废弃 | 移除/标注迁移提示（文档 `01-tasks.md`）；调用方改 `/api/issues` | 调用方（CLI/Agent）文案更新 | A5 |
| C3 | `issue_state` 停写 | pipeline 写路径改 `issue`；`issue_state` 只读 | 无新写入 issue_state | B6/C1 |
| C4 | 文档 | `help/api/11-issues.md` + `README.md` 目录 + `EXPECTED_API_FILES` + 01-tasks 迁移说明 | 契约/文档一致 | C1 |
| C5 | 迁移验收 | `issue_state` 数据核对后 `DROP`（保留备份）；全量回归 | 任务中心建/改/催/完成/取消 + pipeline 各阶段正常 | C3/C4 |

#### Phase C 实施记录（2026-09-12，部分完成）

**C 已完成**：
- **C1**：`issues.py._issue_stage_map` 双表聚合——`issue_state`（权威阶段）优先 + `issue` 卡片兜底（无 issue_state 记录但已有卡片行的 issue 也能列出）
- **C2（部分）**：文档 `01-tasks.md`（docs/openapi + help/api 两处）标注 `POST /api/tasks/create` 已废弃并指向 `POST /api/issues/{owner}/{repo}`；
  前端 `tasks.js createTaskSubmit` 已从 `/tasks/create` 迁移至 `/api/issues/{owner}/{repo}`（body 保留 repo_key/title/description/scene_id）。
  **后端端点暂保留**（未移除）：避免既有调用方（含 CLI/Agent）断裂，待 C5 统一清理。
- **C4（部分）**：`docs/openapi/01-tasks.md` 与 `help/api/01-tasks.md` 同步废弃说明（`test_help_directory` 一致性恢复）

**C 未完成（留待后续）**：
- **C3（issue_state 停写）暂不执行**：pipeline 3.1 阶段初始化可能早于 `ensure_issue_task` 建档
  （无 `issue` 卡片行时 `issue_state` 是唯一数据落点）。直接停写会丢数据。故过渡期保留双写
  （issue 权威 + issue_state 同步），C5 数据核对后再切。
- **C5（DROP issue_state）**：依赖 C3 全部落定，上线生产验证后再做。

---

## 三、依赖与顺序

```
A1 → A2/A3/A4 → A5 → A6 ──┐
                           ├─ B1.1 → B1.2 ──┐
                           │                 ├─ B2.1 → B2.2 → B2.3 → B2.4 → B2.5 ──┐
                           └────────────────────────────────────────────────────┴─ C1 → C2 → C3 → C4 → C5
```

**关键**：
- A 阶段先交付 `/api/issues`（join 读），业务角度立即可用、风险低。
- B 阶段拆 B1（只改名）/ B2（加字段）：B1 独立验证功能正常后再 B2，风险隔离。
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