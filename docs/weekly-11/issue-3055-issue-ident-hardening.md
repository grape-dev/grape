# Issue #3055 深度分析与实施方案：字符串 ident 单号导致 A2A 交办整族失效

> 关联单：#3055（作者 <platform_account> / <operator>，标签 bug）
> 基线：`arch-weekly-11` @ `6ac893296`（2026-11-17 19:10）
> 分支：`feat/issue-3054-oauth-hardening`（#3054 与本单同批交付）
> 状态：分析结论已按基线真实代码逐条复核（file:line 均为本基线实测，非引用单内行号）

## 0. 结论摘要

1. 单内 A（判定谓词统一）、C（仓库解析/Mismatch 作用域）的根因**全部成立**，且比单列出的更多：
   基线里还有 40+ 处 `int(issue_id)` / `.isdigit()` 会吞掉 ident 单号（详见 §2.3）。
2. 单内 B 的"列类型仍是 INT → 静默截断成 0"**只在 MySQL 后端成立**；SQLite 侧
   `INTEGER` 亲和不会截断 ident（动态类型把 `IKW28R` 原样存 text），而且 SQLite 的
   INTEGER 亲和是基线 #995/#2072 的**有意契约**（数字编号读侧必须恒 int）。因此 B 项
   不能简单"三列改 VARCHAR(64)"，正确落法是**按后端分治**（见 §4）。
3. 单内"B 项要先处理存量 0 号行再建唯一索引"成立：`idx_tasks_repo_issue` 这条唯一索引
   在代码里**已经写了**（`src/api/db_setup.py:1043` 与升级分支 `:1197`），但 MySQL 上
   一旦存在同仓多行 `issue_number=0` 的 PR 卡片，CREATE UNIQUE 就 1062，异常被
   `log.warning` 吞掉 → 索引静默缺失，与单内观察一致。
4. 已有修正的"幂等函数" `_widen_mysql_issue_ident_cols()`（`db_setup.py:2286`）与
   `_normalize_issue_number_text()`（`:2338`）**没有任何调用点**（全仓仅测试引用）——
   D105 的 MySQL 放宽逻辑从未接线，这正是 beta 上 INT 列一直存在的直接原因。属本次
   B 项的核心缺口，修法是注册迁移，不是改旧迁移函数体。

## 1. 单内论断逐条核验（实查基线）

| # | 单内断言 | 基线实况 | 判定 |
|---|---|---|---|
| 1 | `POST /api/a2a` 入口写死 `isdigit`（webhook.py:1907） | `src/domains/runtime/routes/webhook.py:1907` `if not issue_number_early.isdigit():` → 400 | 成立 |
| 2 | GET `/api/a2a/status` 同样 `isdigit`（:1582） | `:1582` `if not issue_number or not str(issue_number).isdigit():` | 成立 |
| 3 | `_is_valid_issue_number()`（:2815）是零调用死代码 | `def _is_valid_issue_number` 在 `:2814`，全仓无调用（`grep -rn "_is_valid_issue_number" src/` 只命中定义） | 成立 |
| 4 | `_is_native_issue_ident()` 全仓只用 1 次 | `event_pipeline.py:5337` 定义；唯一调用点 `event_pipeline.py:1117-1128`（ISSUE_NUMBER 注入） | 成立 |
| 5 | A2A 主体 `int(issue_number)`（:1325/1367/1471） | 实测 `:1327 _issue_num = int(issue_number)`、`:1368 UPDATE issue ... int(issue_number)`、`:1471 _get_issue_title(repo_key, int(issue_number))`；另外 `:518/:713/:1716/:1800/:2092/:2106/:2119/:2214/:2594/:2693` 也各有一处 | 成立且**范围更大** |
| 6 | 状态机 isdigit 门禁清单 | `event_pipeline.py:106`（创建人）、`:1339`（cancelled）、`:1692`（子单标题）、`:1873`（/done /cancel /resume）、`:1901`（awaiting_input 恢复）、`:2190`（关单关任务）、`:2405`（PR merged 恢复）、`:2424`（任务中心建档/关单）全部命中；`engine/service.py:3290`（发布子单回填）、`event_store.py:184`（兜底 issue_id）命中 | 成立 |
| 7 | `_resolve_a2a_repo_key` 只试 github/gitcode 前缀（:930-952） | `:938-948` 候选硬编码 `[f"github:{repository}", f"gitcode:{repository}"]`，未命中回落 `to_repo_key()` 默认 gitcode（`src/kernel/repo_key.py:10`） | 成立 |
| 8 | `_issue_repo_mismatch` 裸编号全局匹配（:511） | `:518 int(...)` + `:521 SELECT repo_key FROM issue WHERE issue_number=? AND repo_key<>? LIMIT 1`，无 provider 作用域 | 成立 |
| 9 | 列 INT（schema_ddl.py:219） | `_ISSUE_DDL` 声明 `issue_number INTEGER NOT NULL`；MySQL 翻译层原样 INT。`chat_conversations.issue_number` 由老轨 `_ensure_column(..., "INTEGER DEFAULT 0")`（`db_setup.py:6007`）补列 | 成立（MySQL）／SQLite 不截断（见 §3） |
| 10 | 无 (repo_key, issue_number) 唯一索引 | 代码里**有** `CREATE UNIQUE INDEX ... idx_tasks_repo_issue`（`:1043`，MySQL 升级分支 `:1197`），但创建失败被吞 → 现网可表现为"没有" | 成立（表现为缺失） |
| 11 | 单内"已修好、勿改"清单 | `issues.py:2294/2379`、`issues.py:826`、`im/handler_async.py:811`（S75 注释在）、`event_pipeline.py:1117`（ISSUE_NUMBER 注入）、`_norm_issue_id`（`:5313`）逐条实查在位；且 `issues_center.py` 已广泛使用 `_native_issue_number()` | 成立，不回改 |

## 2. 根因分层（确认 + 补充）

### 2.1 谓词层
- 全仓存在**三套判据**：`_is_native_issue_ident()`（正确，用得少）、`_norm_issue_id()`（归一 ID，含 IK 正则）、
  `_is_valid_issue_number()`（alnum ≤32，零调用）、以及零散的 `str(...).isdigit()`。
- 任何 `isdigit()` 门禁遇到 `IKW28R` 都返回假：入口 400、cancelled 检查跳过、评论指令 no-op、
  等态不恢复、关单不关任务、创建人反查恒空。

### 2.2 强转层（单内只列了 3 处，实测更多）
`int(<issue ident>)` 在 ident 下抛 `ValueError`，被各处 `except` 吞成"静默不做"，比 400 更难排查：
- `src/api/session_store.py:59/113/161/207/249/293`（set_issue_scene/stage/status/… 全部先 `int()`）；
`.745` 的注释已自认此坑
- `src/kernel/session_manager.py:434`（会话 issue_number 比对）
- `src/kernel/prompt_builder.py:431`（任务 prompt 的 issue 号）
- `src/domains/runtime/im/notify.py:92/159/304/390/673/686`、`im/outbox.py:92/715`（IM 通知/去重键）
- `src/domains/runtime/engine/takeover.py:210/1261/1548`、`engine/service.py:3290/3296`、
  `routes/pipeline.py:212/274`、`contracts/local/runtime_local.py:178`
- `src/domains/runtime/routes/issues.py:831/1354/1538`、`routes/events.py:2244`
- `src/api/patrol.py:434`、`src/api/patrol_compensate.py:279`、`src/api/issue_vcs_index.py:274/287`、
  `src/api/qa_gate.py:307/714`
- `src/domains/mgmt/public/__init__.py:390/397`（知识库 issue_num 维度写入）
> 结论：**只放开入口校验 = 派发成功但卡片/状态/prompt 全不更新**，单内警告成立；必须整族替换。

### 2.3 列类型/后端层（关键裁定）
- **SQLite**：`INTEGER` 亲和 ≠ 只能存整数。`INSERT issue_number='IKW28R'` 存 text、读回 `'IKW28R'`；
  `WHERE issue_number=?` 传 `'30'` 经亲和无损转 30 命中。所以 SQLite 上不存在"截断成 0"，也不丢 ident。
- **MySQL**：翻译层把 `INTEGER` 翻成 `INT`，非严格 `sql_mode` 下 `'IKW28R'` → 0 且无告警（单内实测）。
- 基线 #995/#2072 契约要求**数字编号读侧恒 int**（`tests/api/test_issue_2072_issue_number_int.py`），
  而 SQLite 改成 TEXT 亲和会让所有数字编号读成 `'30'`，直接推翻契约 → **B 项不能改 SQLite DDL 类型**。
- 现成修法已在代码里：`_widen_mysql_issue_ident_cols()` 把 MySQL 的 `issue_number/
  parent_issue_number` 从 INT 族 `MODIFY VARCHAR(191)`（含视图基表解析），但**没有调用点/未注册迁移**；
  且未覆盖 `chat_conversations.issue_number`。
- 因此 B 的正解 = **注册迁移调用放宽 + 覆盖第三列 + 处理后建唯一索引**，而不是改 DDL（新装库也走
  注册迁移，因为新库先跑 legacy 全量建表，再跑注册迁移）。

### 2.4 仓库解析层
- `repo_key_candidates()`（`src/kernel/repo_key.py`）已提供"平台卡驱动全 provider 候选集"抽象；
  `_resolve_a2a_repo_key` 却硬编码两个前缀，等于绕开既有抽象。任何 `gitee:`/后续平台裸传必错判 gitcode。

## 3. 存量 0 号行的处置口径（B 项前置）

- `issue` 表 `issue_number=0` 行的语义是"占位"（PR 卡片/无单号任务），不是真实单号。
- 唯一键升级前必须给每行一个**互不冲突且非数字**的占位 ident：`pr:<row id>`（示例口径，落地用
  `pr:` + 主键 id，保证幂等与唯一；非数字 → 所有 ident 谓词天然不当成真单号）。
- 只处理 `issue` 表；`chat_conversations.issue_number=0` 是"通用会话/任务会话"合法哨兵，**不动**。
- 迁移必须幂等：二次启动零改动；DDL 失败要 raise（不盖章、下次重试），不能静默吞。

## 4. 实施方案

### A. 判定/强转统一（止血，必须）
1. 新增内核唯一入口 `src/kernel/issue_ident.py`：
   - `is_native_issue_ident(v)`（语义沿用 event_pipeline 现版：纯数字或 `[A-Z]{2,}[A-Z0-9]*`）
   - `normalize_issue_ident(v)`（去 `#`/空白，非法返回 `""`）
   - `native_issue_number(v)`（数字→int，ident→str；与 `vcs/api_client.py:244` 同语义，统一到内核后原处改为转发）
   - `is_actionable_issue_number(v)`（ident 合法且非 `0`，替代 `isinstance/is > 0` 判据）
2. `event_pipeline`/`webhook`/`service`/`event_store`/`session_store`/`prompt_builder`/`im`/`takeover`/
   `pipeline routes`/`issues`/`patrol*`/`issue_vcs_index`/`qa_gate`/`mgmt.public` 里所有"issue 号"的
   `isdigit` 门禁与 `int()` 强转，改为上述 helper（PR 号/seq/普通计数的 `int()` 不动）。
3. A2A 入口与 status 端点统一到 `is_native_issue_ident`；`_is_valid_issue_number` 删除（不留第二判据）。
4. 加守卫测试：枚举本次收口的强转点（AST/正则），新增 `int(issue_id)` 类回归即红。

### B. 后端列类型 + 唯一键（治本）
1. 新注册迁移（`register_migration`，版本号 = 源码最大号 +1，`kind="safe"`）：
   - MySQL：`_widen_mysql_issue_ident_cols()` 覆盖 `issue.issue_number`、`issue.parent_issue_number`、
     `chat_conversations.issue_number`（视图基表解析沿用）；SQLite 跳过并写日志说明（亲和契约）。
   - 前置：`issue` 表 `issue_number=0` 行改 `pr:<id>`；再 `CREATE UNIQUE INDEX IF NOT EXISTS
     idx_tasks_repo_issue`（失败 raise，不盖章）。
   - 后验：MySQL `information_schema.columns` 复查三列非 INT 族；SQLite 复查无重复键、索引存在。
2. 不改 `schema_ddl` 的 `INTEGER` 声明（理由见 §2.3），在 DDL 旁加口径注释。

### C. 仓库解析 / 越仓反查（同批）
1. `_resolve_a2a_repo_key`：`repo_key_candidates(repository)` 全 provider 反查 `managed_repos`；
   仍未命中才回落到平台卡派生（仍无 provider 线索时保留 gitcode 兜底并 `log.warning` 明示）。
2. `_issue_repo_mismatch`：查询加 provider 作用域（`repo_key LIKE 'gitcode:%'` 同族只返回同 provider
   的他仓），或改由 `repo_key_candidates` 生成候选集后按 provider 归组比较。

## 5. 验收对照（单内 6 条 → 本实现证据）

| 验收 | 证据形态 |
|---|---|
| 同仓 ≥2 个 Gitee issue 各自真实 ident、状态互不串 | 迁移/写入用例：SQLite 造两行 `IKW28R`/`IKW29A` + 唯一键断言 + 状态更新断言 |
| vod→issue 交办返回 seq 且 `agent_tasks.runtime_id` 非空 | A2A 入口用例：ident payload 不再 400，进入派发；断言事件 issue_id=`#IKW28R` |
| awaiting_input 回复后 `issue.status` 回 processing | event_pipeline 用例：ident 评论触发 `_resume_issue_waiting` |
| `/done` `/cancel` `/resume` 生效；Gitee 关单同步关任务 | 评论指令/关单用例 |
| 裸 `owner/repo` 解析为 `gitee:` | `_resolve_a2a_repo_key` 用例（managed_repos 造 gitee 行） |
| 纯数字平台行为不变 | 既有 `tests/api/test_issue_2072_issue_number_int.py`、A2A/S75 用例全绿 |

## 6. 风险

- MySQL 放宽为 `VARCHAR(191)` 涉及索引列，需 `MODIFY` 权限；失败必须 raise 不盖章（下次启动重试）。
- `pr:<id>` 改写会影响"按 issue_number=0 查 PR 卡片"的历史代码：全仓 grep 无该用法；但若外部报表按 0
  统计会变化，迁移日志打印改写行数供对账。
- 契约风险：SQLite 保持 INTEGER 亲和是**刻意**的；若后续要彻底 TEXT 化，须先迁 #2072 契约，不在本单。

## 7. 三库四进程架构约束（实现前必读）

三库 = `grape_mgmt_db` / `grape_runtime_db` / `grape_insight_db`；四进程 = grape_server(mgmt 路由) /
event_server(runtime 事件) / worker_server(runtime 执行) / insight_server(insight)。
归属真源：`configs/table_ownership.json`（P0 冻结 102 表）+ `configs/table_ownership.json`（DDL 守卫）。

### 7.1 本次触及表的归属

| 表 | owner | 本次用法 |
|---|---|---|
| `issue` / `chat_conversations` / `events` / `agent_tasks` / `meta` / `issue_agent_session` | runtime | A2A/事件链读写、B 项迁移（只在 runtime 库执行 DDL） |
| `external_identities` / `grape_accounts` / `oauth_states` | mgmt | #3054 P1/P2/P3 身份与状态机 |
| `managed_repos` / `devops_pipeline_scene` / `unified_user_mapping` / `agent_shared_knowledge` | mgmt | C 项仓库反查只走 mgmt public（`repo_key_in_candidates` / `repo_key_candidates`） |

### 7.2 硬规则

1. 任何 SQL 只能打 owner 域的表；跨域读走目标域 public（lib 通道）或 GSB `/internal/*`，跨域写必须走目标域写函数/outbox，禁止在调用方直接写外域表。
2. 迁移必须带 owner 判据：`_table_present` + `_is_base_table`，不存在/是视图 → `raise _NonOwnerObject`
   （执行器跳过且**不盖章**），参照 `_fn_chat_conversations_status_index`（`src/api/db_setup.py:7208`）。
   不能用"log.warning 后正常返回"，那会在非 owner 库盖章，真 owner 库永不执行。
3. `tests/arch/test_foreign_table_refs.py` 是棘轮：禁止新增 (表, 消费域) 跨域引用，也不得为过关改
   `docs/weekly-11/foreign-refs-allowlist.csv`。
4. 进程内导入纪律：runtime 代码只能用 mgmt/insight 的 public 面（`src.domains.<dom>.public`）或
   contracts 适配，不得 import 对方内部模块做 SQL。

### 7.3 实现形态（抽象优先，不堆巨文件）

- `src/kernel/issue_ident.py`：`IssueIdent` 值对象（parse / normalize / sql_param / json_number /
  is_actionable / is_zero）+ 单一形态规则；巨型文件（webhook.py 2986 行、event_pipeline.py 6611 行、
  session_store.py）只做一行转发，不再各自持有判据。
- `src/domains/runtime/repo_resolver.py`：`A2ARepoResolver` 类，候选 provider 由平台卡目录注入、
  managed_repos 查询经注入的 `RepoRegistry` 协议（生产实现 = mgmt public 适配器），去掉硬编码前缀。
- `src/domains/runtime/migrations/issue_ident_columns.py`：B 项迁移对象（步骤方法：处置 0 号行 →
  MySQL 放宽三列 → 建唯一索引 → 事后复查），`db_setup.py` 仅 `register_migration(...)` 接线。
- #3054 P1：`mgmt/auth/credential_source.py` 的 `CredentialSource`（成对取 client_id+secret、解密失败
  显式错误、configured 同源判定）；P2/P3：`mgmt/auth/identity_lifecycle.py` 的
  `IdentityLifecycleService` + `OAuthStateStore` / `LoginThrottlePolicy` / `UnbindGuard` 组件，路由只做
  参数整形与调用。

### 7.4 验证门禁（每次合并都跑）

```
python3 -m pytest tests/arch/test_foreign_table_refs.py tests/api/test_table_ownership.py \
    tests/api/test_migration_track_guard.py tests/kernel/test_migration_no_baseline_version_collision.py -q
```

基线实况（2026-11-17 实测 arch-weekly-11 @6ac893296）：上述前两项**基线本身已有 4 个红**
（`test_platform_whitelist_ratchet` ×2、`test_no_binary_platform_dispatch_in_frontend`、
`test_foreign_table_refs::test_baseline_kept_tight`），来源是主线未同步收紧基线与存量平台字面量，
与本单无关。裁决口径 = **红集合 diff**：合并后不得出现新增红，也不得靠改 allowlist / ratchet 基线过关。