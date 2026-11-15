# 表归属裁决记录（table_ownership.json 的出处与变更依据）

唯一权威：`docs/weekly-11/table-owner.tsv`（P0 冻结的 101 张唯一基表）。
`configs/table_ownership.json` 由它派生，供 `src/kernel/table_ownership.py` 的 DDL 守卫使用：
**进程只允许创建/修改自己域的表**，从而在拆库后阻止「每个进程各建一份全量表」。

## 为什么需要这份记录

manifest 曾经是从某个开发库的 `information_schema` 物理快照生成的（`generated_from`
记着 `<dev-db-host>`）——它把「历史上谁建过表」当成了归属，与 P0 判决相反，直接造成：

1. `scripts/foreign_table_refs.py --check` 误报大量「跨域引用」（棘轮红）；
2. 分库演练环境出现同一张表的多份基表（守卫允许 runtime 建 mgmt 域的表）。

结论：**归属只能来自 `table-owner.tsv` 的裁决**，物理快照只能用来核对「表是否存在」，
不能用来推断「表归谁」。

## 判定口径（三条同时成立才改归属）

- **代码消费域**：`scripts/foreign_table_refs.py` 实测的 `consumer_domains` 与候选 owner 一致（同域=无跨域引用）；
- **SQL 落点**：读写语句实际所在的域模块（谁执行 SQL 谁就是 owner）；
- **DDL 落点**：建表语句所在域模块（缺失另行标注）。

## 14 张表的裁决（2026-11-15，逐条有证据）

| 表 | 旧(快照) | 新(P0) | 代码消费域实测 | SQL 落点 | DDL 落点 |
|---|---|---|---|---|---|
| event_action_filter | runtime | **mgmt** | mgmt:19 | mgmt（`src/domains/mgmt/routes/settings.py` + `mgmt/public`） | mgmt `schema_ddl.py` |
| event_route_config | runtime | **mgmt** | mgmt:2 | mgmt | mgmt `schema_ddl.py` |
| pipeline_instances | runtime | **mgmt** | （无引用） | mgmt | mgmt `schema_ddl.py` |
| grape_t_channel_installation | runtime | **mgmt** | mgmt:43 | mgmt `public`（`upsert_channel_installation`） | mgmt `schema_ddl.py`（`CHANNEL_TABLE_DDL`） |
| grape_t_channel_lease | runtime | **mgmt** | mgmt:19 | mgmt `public`（acquire/renew/release），runtime 仅转发 | mgmt `schema_ddl.py` |
| grape_t_channel_chat_session_binding | runtime | **mgmt** | mgmt:7 | mgmt public | mgmt `schema_ddl.py` |
| grape_t_channel_inbound_dedup | runtime | **mgmt** | mgmt:7 | mgmt public（`claim_inbound_dedup`） | mgmt `schema_ddl.py` |
| grape_t_channel_inbound_message | runtime | **mgmt** | mgmt:2 | mgmt public | mgmt `schema_ddl.py` |
| grape_t_channel_outbound_card | runtime | **mgmt** | mgmt:3 | mgmt public | mgmt `schema_ddl.py` |
| grape_t_channel_sender_identity | runtime | **mgmt** | mgmt:6 | mgmt public | mgmt `schema_ddl.py` |
| grape_t_merge_gate_log | mgmt | **insight** | insight:2 | insight | insight `schema_ddl.py` |
| grape_t_repo_credentials | runtime | **insight** | insight:16 | insight | insight `schema_ddl.py`（`_REPO_CREDENTIALS_DDL`） |
| meta | mgmt | **runtime** | runtime:44 | runtime | runtime `schema_ddl.py` |
| scene_category | mgmt | **runtime** | runtime:11 | runtime | runtime `schema_ddl.py` |

对齐后实测：这 14 张表的「代码消费域」与 owner 全部同域 → 外域引用 **181 → 0**，
`foreign_table_refs.py --check` 转绿（余下基线仅 kernel 侧 2 项）。

## 同轮的两处配套修正

1. **`tasks` 从 manifest 移除**：`tasks` 是历史表，早已 `ALTER TABLE tasks RENAME TO issue`；
   P0 的 101 张清单里没有它，演练/生产库也没有这张基表（只剩开发库残留）。
   留在 manifest 里会把 `src/api/db_setup.py` 里 legacy rename 的语句算成 `tasks←api` 跨域引用。
2. **`scripts/foreign_table_refs.py` 剔除 DDL**：`CREATE/ALTER/DROP/RENAME TABLE …` 是 schema
   定义，不是跨域数据访问（DDL 由 DDL 守卫单独治理）。此前 `ALTER TABLE tasks RENAME TO issue`
   被 `_REF_RE` 里的 `TABLE` 关键字命中，产生 4 处假引用；现已先 `_DDL_RE.sub()` 再统计。

修正后实测：`foreign_table_refs.py --check` → `OK: 外域引用未上升（棘轮通过）`，
跨域引用 192 处 / 16 对 → **6 处 / 2 对**（仅剩 kernel 侧 2 项基线：`unified_user_mapping`、
`grape_t_credential_usage_log`）；`scripts/rds_gate.sh --quick` → GATE 全绿；
`tests/api/test_table_ownership.py + tests/arch/test_foreign_table_refs.py + tests/scripts` → 67 passed。

## DDL 归位（渠道族 + repo_credentials）

| 表 | 原 DDL 位置 | 归位后 |
|---|---|---|
| 6 张 `grape_t_channel_*` + `grape_t_channel_inbound_message` | `scripts/migrations/001_channel_tables.py` 内联 / `runtime/im/engine_wiring.py` 内联 | `src/domains/mgmt/schema_ddl.py::CHANNEL_TABLE_DDL`（7 条），进入 `db_setup._NEW_TABLES` |
| `grape_t_repo_credentials` | 迁移函数内联于 `src/api/db_setup.py` | `src/domains/insight/schema_ddl.py::_REPO_CREDENTIALS_DDL`，迁移函数只引用常量 |

配套：建表收敛闸门 `table_ownership.db_name_domain()`（只按库名判 owner）——
库名为共享/单库（`None`）与 `mgmt` 时才建渠道族表，`runtime`/`insight` 跳过并记
`IM channel tables owned by mgmt, skipped in domain=…`。`001` 迁移改为
`executescript(";\n".join(CHANNEL_TABLE_DDL))`，不再自带副本。

验证：新建 SQLite 库跑 `init_database()` → 7 张渠道表 + `repo_credentials` 全部建出；
`rds_gate.sh --quick` GATE 全绿；`tests/runtime/im + api + arch + contracts + scripts`
与基线的失败集合逐条一致；新增 `tests/api/test_channel_ddl_ownership.py`（10 条守卫用例）。

## 跨域读写的 owner 化（消除 1146 静默失败）

拆库后同一段代码可能在非 owner 进程里执行。若它仍用 `session_manager.db`（进程本域连接），
SQL 会打到错误的库，得到 `ERROR 1146 (table doesn't exist)`，而这些调用点普遍 `except` 兜底，
最终表现成「看板空白 / 健康分 null」而不是报错。

两条成因与对应修法：

1. **任务归属错位**：mgmt 入口曾启动 insight 域的告警/聚合/采样循环，而 `obs_*` 表在 insight 库。
   → 现在按 `db_name_domain()` 判定：非 owner 进程不启动这些循环，并记录跳过原因。
2. **代码不感知 owner**：可观测性模块、kernel 审计表、GSB 路由配置、takeover、health scorer
   都用进程连接。→ 新增 `src/kernel/owner_db.py`：`db_pair(domain)` 返回 db 兼容代理 + 进程级可重入锁。
   分库且有该域 DSN 时用该域线程本地连接（方言翻译保留）；否则回落 `session_manager.db`（单库零回归）。
   `execute()` 与 `MySQLCompatDB` 同语义（返回 cursor）。

落地范围：insight 可观测性整包、`runtime/engine/takeover.py`、`runtime/event_store.py`、
`kernel/persistence/credential_usage_log.py`、`kernel/gsb/route_config.py`（并在首次读表前幂等建表）、
`mgmt/pipeline_config.py`、`runtime/engine/health_scorer.py`；用户 id 迁移脚本按 manifest
只迁本域表，非 owner 表记 `[SKIP] … 由 owner 进程迁移`。

验证：`tests/kernel/test_owner_db.py`（7 条）、`tests/api/test_channel_ddl_ownership.py`（10 条）
全绿；`rds_gate.sh --quick` 全绿；演练环境重启后各进程 `grep -c 1146` 均为 0，
业务接口（看板/需求列表）返回真实数据。

## 维护约定

1. 新增表或裁决变更：先改 `docs/weekly-11/table-owner.tsv`，再同步本文件与 manifest，最后跑
   `python3 scripts/foreign_table_refs.py --owner-json configs/table_ownership.json --check`。
2. **禁止**再用 `information_schema` 快照直接覆盖 manifest——那会把「历史建表位置」当成归属。
3. 本文件是 `table_ownership.json` 里 `_comment` 指名的裁决记录，缺失时归属争议无从追溯。
4. 本文件只写**代码口径**（表名、域、模块路径、规则）：不写环境拓扑、主机地址、部署路径、
   备份文件名与数据行数；运维台账留在私有运维仓。