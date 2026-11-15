# 表归属裁决记录（table_ownership.json 的出处与变更依据）

唯一权威：`docs/weekly-11/table-owner.tsv`（P0 冻结的 101 张唯一基表）。
`configs/table_ownership.json` 由它派生，供 `src/kernel/table_ownership.py` 的 DDL 守卫使用：
**进程只允许创建/修改自己域的表**，从而在拆库后阻止「四进程各建一份全量表」。

历史上本文件是从 dev 库 `information_schema` 物理快照生成的（`generated_from` 记的
`information_schema of <dev-db-host> (dev)`）——它把「历史上谁建过表」当成了归属，
与 P0 判决相反，直接造成：

1. `scripts/foreign_table_refs.py --check` 误报 181 处「跨域引用」（14 项新增，棘轮红）；
2. 本机 stage 三库出现 10 张重复基表（守卫允许 runtime 建 mgmt 域的表）。

## 2026-11-15 对齐（14 张表，逐条有证据）

判定口径（三条同时成立才改归属）：
- **代码消费域**：`scripts/foreign_table_refs.py` 实测的 `consumer_domains` 与候选 owner 一致（同域=无跨域引用）；
- **SQL 落点**：读写语句实际所在的域模块（谁执行 SQL 谁就是 owner）；
- **DDL 落点**：建表语句所在域模块（缺失另行标注）。

| 表 | 旧(快照) | 新(P0) | 代码消费域实测 | SQL 落点 | DDL 落点 |
|---|---|---|---|---|---|
| event_action_filter | runtime | **mgmt** | mgmt:19 | mgmt（`src/domains/mgmt/routes/settings.py` + `mgmt/public`） | mgmt `schema_ddl.py:142` |
| event_route_config | runtime | **mgmt** | mgmt:2 | mgmt | mgmt `schema_ddl.py:9` |
| pipeline_instances | runtime | **mgmt** | （无引用） | mgmt | mgmt `schema_ddl.py:71` |
| grape_t_channel_installation | runtime | **mgmt** | mgmt:43 | mgmt `public/__init__.py`（`upsert_channel_installation`） | **缺失**（靠历史库/P4 dump） |
| grape_t_channel_lease | runtime | **mgmt** | mgmt:19 | mgmt `public/__init__.py:877-924`（acquire/renew/release），runtime 仅转发 `_mgmt_lease` | **缺失** |
| grape_t_channel_chat_session_binding | runtime | **mgmt** | mgmt:7 | mgmt public | **缺失** |
| grape_t_channel_inbound_dedup | runtime | **mgmt** | mgmt:7 | mgmt public（`claim_inbound_dedup`） | **缺失** |
| grape_t_channel_inbound_message | runtime | **mgmt** | mgmt:2 | mgmt public | 在 runtime `im/engine_wiring.py:160`（待归位） |
| grape_t_channel_outbound_card | runtime | **mgmt** | mgmt:3 | mgmt public | **缺失** |
| grape_t_channel_sender_identity | runtime | **mgmt** | mgmt:6 | mgmt public | **缺失** |
| grape_t_merge_gate_log | mgmt | **insight** | insight:2 | insight | insight `schema_ddl.py:9` |
| grape_t_repo_credentials | runtime | **insight** | insight:16 | insight | `src/api/db_setup.py:2505`（待归位） |
| meta | mgmt | **runtime** | runtime:44 | runtime | runtime `schema_ddl.py:99` |
| scene_category | mgmt | **runtime** | runtime:11 | runtime | runtime `schema_ddl.py:387` |

对齐后实测：这 14 张表的「代码消费域」与 owner 全部同域 → 外域引用 **181 → 0**，
`foreign_table_refs.py --check` 由红转绿（余下基线仅 kernel 侧 2 项）。

## 同一轮的两处配套修正

1. **`tasks` 从 manifest 移除（135 张）**：`tasks` 是历史表，早已 `ALTER TABLE tasks RENAME TO issue`；
   P0 的 101 张清单里没有它，本机 stage 与生产 59 库也没有这张基表（只剩 dev 库残留）。
   它留在 manifest 里会把 `src/api/db_setup.py` 里 legacy rename 的语句算成 `tasks←api` 跨域引用。
2. **`scripts/foreign_table_refs.py` 剔除 DDL**：`CREATE/ALTER/DROP/RENAME TABLE …` 是 schema
   定义，不是跨域数据访问（DDL 由 DDL 守卫单独治理）。此前 `ALTER TABLE tasks RENAME TO issue`
   被 `_REF_RE` 里的 `TABLE` 关键字命中，产生 4 处假引用；现已先 `_DDL_RE.sub()` 再统计。

修正后实测：`foreign_table_refs.py --check` → `OK: 外域引用未上升（棘轮通过）`，
跨域引用 192 处 / 16 对 → **6 处 / 2 对**（仅剩 kernel 侧 2 项基线：`unified_user_mapping` 3 处、
`grape_t_credential_usage_log` 3 处）；`rds_gate.sh --quick` → GATE 全绿；
`tests/api/test_table_ownership.py + tests/arch/test_foreign_table_refs.py + tests/scripts` → 67 passed。

## 仍待处理（不在本次清单变更范围）

- **物理重复基表**：本机 stage 三库 10 张（6 张 channel + event_action_filter/event_route_config/
  pipeline_instances 的 runtime 副本 + merge_gate_log/repo_credentials 的 runtime 副本）；
  生产 59 库 1 张（`grape_t_merge_gate_log` 在 runtime + insight 双基表）。
  收敛口径：只保留 owner 库基表，其余库按 `docs/weekly-11/view-allowlist.txt` 建只读视图或直连 owner 域。
- **DDL 归位**：6 张 channel 表补 `domains/mgmt/schema_ddl.py`；`grape_t_repo_credentials` 从
  `src/api/db_setup.py` 搬到 `domains/insight/schema_ddl.py`；`grape_t_channel_inbound_message`
  从 `domains/runtime/im/engine_wiring.py` 搬到 mgmt。

## 2026-11-15 物理清理（本机 stage 三库，验证后执行）

按 owner 收敛掉 36 个非 owner 副本（先 mysqldump 备份，再 DROP）：

- 第一批 17 个（14 张表相关的多份基表）：
  `grape_stage_runtime` 11 个（6 张 channel + event_action_filter/event_route_config/pipeline_instances
  + merge_gate_log/repo_credentials）；`grape_stage_insight` 6 个（channel 族）。
  备份：`/home/grape/table-cleanup-backup-20261115.sql`（17 张，含 CREATE + 数据）。
- 第二批 19 个（其余跨域基表副本，全部 0 行）：
  `grape_stage_runtime` 7 个（managed_repos/oauth_states/unified_user_mapping/user_accounts/
  user_credentials/user_repos/grape_t_patrol_findings）、`grape_stage_insight` 9 个（含 agent_runtimes、
  chat_agent_session、repo_im_group_binding）、`grape_stage_mgmt` 3 个（bot_seen_messages、
  chat_agent_session、repo_im_group_binding）。
  备份：`/home/grape/table-cleanup-backup-20261115-b.sql`（19 张）。

数据风险说明：`event_action_filter` 两份内容**不同**（mgmt 为新版、含 `vcs_source`；runtime 为旧种子），
按 P0 归属保留 mgmt 版，runtime 旧版已随备份归档。其余副本 0 行，无数据损失。

清理后实测（`information_schema`）：
```
grape_stage_mgmt    : 基表 55 / 视图 0 / 跨域基表副本 0
grape_stage_runtime : 基表 35 / 视图 0 / 跨域基表副本 0
grape_stage_insight : 基表 13 / 视图 0 / 跨域基表副本 0
```
四个进程（nginx 38080 → mgmt 38081 / event 38082 / worker 38083 / insight 38084）健康 200，
worker claim 仍 200 OK，清理后新日志无被删表的 1146。

**未做**：生产 `192.168.0.59` 的 `grape_t_merge_gate_log` 双基表（runtime + insight）尚未删除——
116 集群在线，需先确认写入方再动。

**遗留（下一步代码批次）**：stage 仍有跨域 `schema` 缺口导致的 1146（`insight.runtime_health_scores`、
`insight.agent_tasks`、`insight.devops_pipeline_scene`、`runtime.scene_market`、`runtime.external_identities`），
属读数仍走本地连接的残留，需按 owner 提供具名查询或 GSB 原语。

## 2026-11-15 ③ DDL 归位（渠道族 + repo_credentials）

| 表 | 原 DDL 位置 | 归位后 |
|---|---|---|
| 6 张 `grape_t_channel_*` + `grape_t_channel_inbound_message` | `scripts/migrations/001_channel_tables.py`（脚本内联）/ `runtime/im/engine_wiring.py:160` | `src/domains/mgmt/schema_ddl.py::CHANNEL_TABLE_DDL`（7 条），进入 `db_setup._NEW_TABLES` |
| `grape_t_repo_credentials` | 迁移函数内联于 `src/api/db_setup.py` | `src/domains/insight/schema_ddl.py::_REPO_CREDENTIALS_DDL`，迁移函数只引用常量 |

配套：建表收敛闸门 `table_ownership.db_name_domain()`（仅看库名）——
`None`（单库/共享库）与 `mgmt` 才建渠道族表，`runtime`/`insight` 跳过并记
`IM channel tables owned by mgmt, skipped in domain=…`。`001` 迁移改为
`executescript(";\n".join(CHANNEL_TABLE_DDL))`，不再自带副本。

验证：新建 SQLite 库跑 `init_database()` → 7 张渠道表 + `repo_credentials` 全部建出
（outbound_card 15 列、repo_credentials 5 列）；`rds_gate.sh --quick` GATE 全绿；
`tests/runtime/im + api/迁移 + arch + contracts + scripts` 与基线的失败集合**逐条一致**
（13 failed / 879 passed / 28 errors，全部为改动前既有），新增
`tests/api/test_channel_ddl_ownership.py` 10 条守卫用例全绿。

## 2026-11-15 ④ 生产 merge_gate_log 双基表收口（归属 insight）

`grape_t_merge_gate_log` 是 PR 合入门禁（pre_merge_qa / pre_merge / post_merge 三阶段）
的决策流水，best-effort 只写不读、fail-open；写入方是 `insight/public.record_merge_gate_log`。

实测两份都是空表（0 行），按 P0 归属保留 insight，删 runtime 副本：
```
备份: /home/grape/merge-gate-log-runtime-copy-backup-20261115.sql（1 张 CREATE + 0 行）
DROP: grape_runtime_db.grape_t_merge_gate_log   （59 @192.168.0.59:3306）
保留: grape_insight_db.grape_t_merge_gate_log   （owner，0 行）
守卫: table_ownership.owner_of=insight；should_skip('…','runtime')=True，
      should_skip('…','insight')=False → 其它进程不会再把它建回来
```
说明：59 上另有一个早期彩排遗留库 `grape_stage_runtime`（不是当前在用的 stage runtime，
后者在 116:3307）里也有一份同名表，未动，属非活跃环境残留。

## 2026-11-15 ⑤ 跨域 1146 归零（owner 连接代理 + 任务归属闸门）

**现象**：三库独立后，mgmt 进程 8 分钟内累计 **274 条 1146**（`Table 'grape_stage_mgmt.obs_*' doesn't exist` 等），
全部被 `except` 吞成空结果——看板空白/健康分 null 的根因。

**成因**（两条）：
1. **任务归属错位**：`grape_server.py`（mgmt 入口）用 `src.api.main._start_obs_*` 起了 insight 域的
   告警/聚合/采样三个循环，而 obs_* 表在 insight 库；
2. **代码不感知 owner**：insight 可观测性模块、kernel 审计表、gsb 路由配置、takeover、health_scorer
   都用 `session_manager.db`（进程本域连接），被非 owner 进程调用时必然打错库。

**修复**：
- 新增 `src/kernel/owner_db.py`：`db_pair(domain)` 返回 db 兼容代理 + 进程级可重入锁。
  解析规则 = 分库有 DSN → 该域线程本地自有连接（`db_registry.domain_db`，方言翻译保留）；
  单库/无 DSN → 回落 `session_manager.db`（零回归）。`execute()` 与 MySQLCompatDB 同语义（返回 cursor）。
- `grape_server.py`：`db_name_domain()` 非 insight 时**不启动** obs 三循环（并打 `observability loops owned by insight, skipped in domain=…`）。
- 12 个文件改用 owner 代理：insight/observability 全部（alerts/slo/metric_rollup/llm_usage/rollup/gap/
  slo_backfill/overview/logs/trace/__init__）、`runtime/engine/takeover.py`(runtime)、
  `runtime/event_store.py`(runtime)、`kernel/persistence/credential_usage_log.py`(runtime)、
  `gsb/route_config.py`(mgmt，并首次读表前幂等建表)、`mgmt/pipeline_config.py`(mgmt)、
  `runtime/engine/health_scorer.py`（runtime 表走 runtime、`runtime_health_scores` 走 insight）。
- `scripts/migrate_user_accounts_to_grape.py`：用户 id 迁移扫描按 manifest **只迁本域表**
  （非 owner 表打 `[SKIP] … 由 owner 进程迁移`），避免 mgmt 扫 runtime 表撞 1146。
- stage mgmt 库补建 `gsb_provider_route`（provisioning 缺口）。

**实测（重启后 8 分钟窗口）**：
```
重启前 8 分钟：mgmt 274 条 1146（obs 家族 210 + 其它 64）
重启后 8 分钟：mgmt 0 / event 0 / worker 0 / insight 0
业务面：/api/dashboard/ops/overview 200（带真实任务计数）、/api/issues/… 200
测试：tests/… 与基线失败集合逐条一致（13 failed / 889 passed / 28 errors），
      新增 tests/kernel/test_owner_db.py 7 条 + channel DDL 10 条全绿；rds_gate.sh --quick 全绿
```

## 维护约定

1. 新增表或裁决变更：先改 `docs/weekly-11/table-owner.tsv`，再同步本文件与 manifest，最后跑
   `python3 scripts/foreign_table_refs.py --owner-json configs/table_ownership.json --check`。
2. **禁止**再用 `information_schema` 快照直接覆盖 manifest——那会把「历史建表位置」当成归属。
3. 本文件是 `table_ownership.json` 里 `_comment` 指名的裁决记录，缺失时归属争议无从追溯。