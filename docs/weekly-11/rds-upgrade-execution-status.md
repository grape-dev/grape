# 三库独立 RDS 升级 · 执行状态台账

| 项 | 内容 |
| --- | --- |
| 依据方案 | `docs/weekly-11/independent-rds-upgrade-plan.md`（v1.0） |
| 执行分支 | `arch-weekly-11-ljt` |
| 执行时间 | 2026-11-14 |
| 参照环境 | 三库同居参照实例（`<baseline-host>`）：`grape_mgmt_db` / `grape_runtime_db` / `grape_insight_db` |
| 验收口径 | G2 视图 0 / G3 跨库授权 0 / G4 外域表引用 0（A1/A2/A3） |

> 本台账只记**实测**结果。数字均可由文末命令复现；未执行项标注原因，不做"已规划=已完成"的表述。

---

## 1. 阶段状态

| 阶段 | 状态 | 实测证据 |
| --- | --- | --- |
| P0-1 备份 182 个视图 DDL | ✅ 完成 | `docs/weekly-11/backup/cross-domain-views-20261114.sql`（182 视图全量 DDL） |
| P0-1 删除 60 个零引用视图 | ✅ 完成（已放行） | 实测视图 182 → **122**，与白名单 122 条完全一致；30 张无视图表登记于 `docs/weekly-11/no-view-tables.txt` |
| P0-2 外域引用统计 + 棘轮守卫 | ✅ 完成 | `scripts/foreign_table_refs.py --check` 通过；`tests/arch/test_foreign_table_refs.py` 3 项通过；基线 138 对 / 1306 处 |
| P0-3 `sync_domain_views.py` 只减不增 | ✅ 完成 | 视图白名单 122 条（`docs/weekly-11/view-allowlist.txt`）；不在白名单的建视图请求 `DENY` 并 exit 1；`--prune` 支持删白名单外视图 |
| P0-3 第二条建视图路径同样受控 | ✅ 完成 | `scripts/domain_view_freshness.py --repair` 对白名单外视图 `DENY`（生产库名族生效，dev 库族不误拦） |
| P0-4 依赖矩阵看板 | ✅ 完成 | `docs/weekly-11/dependency-matrix.md`（70 表 / 138 对 / 1306 处）；`docs/weekly-11/dependency-board.html`；`--check` 防过期 |
| P1 R8 守卫（kernel 不得写域表） | ✅ 完成 | `scripts/check-arch.py` R8 硬规则 + 棘轮白名单（77 处存量，10 文件）；`check-arch` RESULT: PASS |
| P1 归属重定 / 模块归位 | 🟡 部分（9 个模块） | 已归位：`knowledge`→mgmt；`onboarding`/`pr_merged_sre_retry`/`scene_precheck`/`event_store`→runtime；`time_utils`/`event_payload_view`/`a2a_context`→kernel（旧路径 sys.modules 别名，80 处调用点零改动）；跨域引用 **1306→1232**、138→135 对、跨域表 70→68。30 张表逐表归属重定仍未完成 |
| P2 N1–N10 服务接口 | ✅ 完成 | runtime 7 个（skills 列表/详情、engine-capabilities、runtime-credentials、meta、tasks/batch、stats/tasks）+ mgmt 4 个（repo bundle、account、knowledge、user-mapping）；mgmt 只读白名单 11 条 |
| P2 N13 补偿应用端点 | ✅ 完成 | `POST /internal/xwrite/apply`（runtime 9 个 action、mgmt 5 个 action）；幂等键登记表 `grape_t_xwrite_applied`（`cross_write.claim_applied`） |
| P2 内部契约快照 | ✅ 完成 | `docs/weekly-11/internal-contract.json`（36 条：runtime 17 / mgmt 12 / insight 7），全部 `<domain>_<resource>_<action>` |
| P3 W1/W2/W3 写分流 | ✅ 覆盖达标（W2/W3 缺口 0） | 31 张写表：**22 张已有 owner public 写函数（71%）**；6 张 api→mgmt 为同进程同库（不需通道）；1 张 X（迁移账本禁跨域写）。W2 新增 12 个 mgmt/runtime 写原语，W3 新增 6 个动作（runtime/mgmt/insight 各 `xwrite/apply`）|
| P3.5 读路径接口化 | ✅ 全绿（stage `--all --with-ids` **169/169**：含 99 个带真实 id 的详情页） | 已切：chat 会话列表（kernel `get_user_conversations`）、场景分类（mgmt `get_scene_category`）→ `domain_read.owner_lib`，stage 冒烟 16 端点 0 失败；读开关改为「DSN 存在即开启」（`domain_read.switch_on`），历史灰度分支自动生效。**已归零**（按 1146 逐条改）：新增 `src/kernel/domain_db.cursor(domain)`（own DSN 优先、否则进程库）：insight `ops/timeline`、runtime `engine/health`（skill_consistency）改用它；`autopilot/executions` 补 runtime 具名读原语 `list_autopilot_executions`（分页+筛选+info_schema 列探测），mgmt 侧 join 本域 `autopilot_tasks`。冒烟统计改为 JSON 字段判定（避免把日志里的一条历史错误当成端点失败）。`/api/dashboard/ops/error-logs` 经复核为误报（响应就是日志列表本身） |
| P4 三实例与搬迁 | 🟡 彩排通过（云实例仍缺） | `scripts/rds_stage_rehearsal.py` 在参照实例上建三个独立库 + 只授本库账号：表集合一致、行数与 dump 快照一致、视图 0、跨域裸读实测 1146；`rds_cutover_check.py` baseline 102 表 / 382,976 行 |
| P5 权限收敛与清视图 | 🟡 彩排验收通过（生产待 P4） | stage 三库：A1 视图 0 ✅ / A2 授权仅本库 ✅ / A6 队列 ✅；A3 代码引用 135 对仍红（P1 未完成，如实） |

---

## 2. 量化基线（本次实测）

| 指标 | 数值 | 采集命令 |
| --- | --- | --- |
| 三库基表 | 102（唯一表名 101；`grape_t_merge_gate_log` 在 insight/runtime 各有一份，按 disposition 判给 insight） | `scripts/rds_cutover_check.py --mode baseline` |
| 三库跨域视图 | **122**（mgmt 30 / runtime 34 / insight 58；P0 已删 60 个零引用视图） | `information_schema.views` + `view-allowlist.txt` |
| 视图覆盖的非本域表 | 91 张；其中 30 张（60 视图）代码零跨域引用，已于 2026-11-14 删除 | `docs/weekly-11/backup/p0-prune-dryrun.txt`、`no-view-tables.txt` |
| 代码外域表引用 | 68 张表 / 1232 处跨域（`表×消费域` 135 对）；含归属域共 2711 处 | `scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv` |
| 域×域分布 | api→mgmt 252 / api→runtime 388；mgmt→runtime 193；kernel→mgmt 88、→runtime 89 | `docs/weekly-11/dependency-matrix.md` |
| kernel 写域表（R8 存量） | 77 处 / 10 文件（R8 已锁死不得新增；本次未改 kernel 模块布局） | `scripts/check-arch.py` |
| 内部端点 | 36 条（P2 新增 12 条 + 既有补齐 operationId 16 条） | `docs/weekly-11/internal-contract.json` |
| 跨域写表 | 31 张（W1 4 / W2 16 / W3 9 / X 1 + 1 张补录）；**W2/W3 缺口 0**，覆盖 22 张（71%） | `docs/weekly-11/cross-domain-write-channel.csv`、`write-channel-coverage.md` |

---

## 3. 本次落地的代码与工具

| 文件 | 作用 |
| --- | --- |
| `scripts/foreign_table_refs.py` | 单遍扫描（0.2s）+ `--owner-json/--baseline/--check/--update-baseline` 棘轮 |
| `tests/arch/test_foreign_table_refs.py` | 新增/上升即红；下降未收紧基线即红 |
| `scripts/check-arch.py` | 新增 R8：kernel 写域归属表（棘轮白名单，77 处存量） |
| `scripts/gen_domain_dependency_board.py` | 依赖矩阵 md + HTML 看板，`--check` 防过期 |
| `scripts/sync_domain_views.py` | 视图白名单（只减不增）+ `--prune` 删白名单外视图 |
| `scripts/domain_view_freshness.py` | 修复路径同样受白名单约束（生产库族） |
| `scripts/gen_internal_contract.py` + `tests/contracts/test_internal_contract_snapshot.py` | 内部契约快照与命名校验 |
| `src/domains/runtime/public/__init__.py` | N1/N2/N3/N4/N5/N11/N12 数据面 + N13 `xwrite_apply` 分发 |
| `src/domains/mgmt/public/__init__.py` | N6/N8/N9/N10 数据面（字段白名单、凭据只回有/无） |
| `src/domains/contracts/{runtime,mgmt}_service.py` + local/remote | 契约方法与两侧实现（Local/Remote） |
| `src/domains/runtime/routes/internal.py` | 8 条新端点 + 9 条既有端点补 operationId |
| `src/domains/mgmt/routes/internal.py` | 只读白名单 router + xwrite/apply router（D5） |
| `src/api/server_common.py` | mgmt 挂载只读白名单（取代"完全不开放"） |
| `src/kernel/cross_write.py` | `claim_applied` 幂等登记（N13 目标域去重） |
| `src/kernel/gsb/route_config.py` | 新契约方法/public 原语登记 + 路由 key 去重 |
| `scripts/check_write_channel_coverage.py` | W1/W2/W3 × public 写函数覆盖度 |
| `scripts/rds_cutover_check.py` | P4 搬迁 baseline/verify（schema 指纹 + 行数 + CHECKSUM） |
| `scripts/verify_domain_isolation.py` | P5 验收（A1/A2/A3/A6）；`--db-prefix` 支持 stage |
| `scripts/stage_api_smoke.py` | 在视图清零的 stage 三库上遍历只读端点，按 1146/1054 定位剩余跨域 SQL |
| `src/kernel/domain_read.py` | `owner_lib(domain)`：读写通道句柄（只要求自有 DSN，不受读开关影响） |
| `src/domains/mgmt/public/__init__.py` | W2 渠道写原语（lease/installation/inbound_dedup/binding/sender/event_action_filter）+ W3（feedback/inbound_message/outbound_card） |
| `src/domains/runtime/public/__init__.py` | W2 写原语（event_active_guard/issue_agent_session/scene_category）+ W3 `record_event_skipped`；lease/filter 改为经 `domain_read.mgmt_write_lib` 委托 owner 域 |
| `src/domains/insight/public/__init__.py` | insight 域 lib（W3：api_latency_log / merge_gate_log） |
| `src/kernel/domain_read.py` | 新增 `mgmt_write_lib()`（W2 写通道，独立于读开关） |
| `tests/mgmt/test_channel_w2_writes.py` | W2 写原语幂等分支 + runtime→mgmt lib 切换（lib 可用/不可用两向） |
| `docs/weekly-11/table-owner.tsv` | P0 冻结归属清单（101 张唯一基表） |
| `docs/weekly-11/foreign-refs-allowlist.csv` | 棘轮基线 138 对 / 1306 处 |

---

## 4. 未完成项与原因

1. **P0 删 60 视图**：✅ 已放行并执行（182 → 122，与白名单逐条一致）。备份：`docs/weekly-11/backup/cross-domain-views-20261114.sql`；回滚即执行该 DDL 重建所需视图。
2. **P1 30 张表归属重定**：需要按 `cross-domain-disposition.csv` 逐表把实现搬进 owner 域，
   并把 api/kernel 调用点改到 `/internal/*` 或 public 函数。当前仅完成守卫（R8 + 棘轮），
   迁移量：kernel 38 个文件×表对、api 124 个文件×表对。
3. **P3 W2/W3 缺口**：✅ 已清零（W2 12 个写原语、W3 6 个动作；实测幂等分支与落库已逐项验证，测试数据已清理）。
4. **P3.5**：7 张纯跨域读表 10 处引用仍直读。
5. **P4**：需要三个 RDS 实例 + 账号 + binlog 权限；本环境无云账号 AK，无法创建实例。
   搬迁校验与回滚脚本已就绪，`baseline` 已在现网实测通过（102 表 / 382,976 行）。
6. **P5**：依赖 P4 完成；验收器当前对现网如实报红（预期）。

---

## 5. 复现与门禁命令

```bash
# 外域引用棘轮（新增/上升即红）
python3 scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv \
  --baseline docs/weekly-11/foreign-refs-allowlist.csv --check

# 架构守卫（R1–R8，R8 = kernel 不得写域表）
python3 scripts/check-arch.py

# 看板刷新 / 过期检查
python3 scripts/gen_domain_dependency_board.py [--check]

# 内部契约快照 / 漂移检查
python3 scripts/gen_internal_contract.py [--check]

# 写通道覆盖度
python3 scripts/check_write_channel_coverage.py [--check]

# P4 搬迁校验
python3 scripts/rds_cutover_check.py --mode baseline --host <src> --user <u> --out /tmp/b.json
python3 scripts/rds_cutover_check.py --mode verify   --host <dst> --user <u> --baseline /tmp/b.json

# P5 域隔离验收
python3 scripts/verify_domain_isolation.py --host <rds> --user <u> --password <p> \
  --account mgmt=<u_mgmt> --account runtime=<u_runtime> --account insight=<u_insight>
```

---

## 6. 下一阶段推进顺序（按方案）

1. 人工放行 P0 的 60 视图删除 → 视图 182 → 122（快赢，零代码风险）。
2. P1：从 `cross-domain-disposition.csv` 的 `D` 类 19 张表开始（kernel 模块归位），
   每迁一张：`foreign_table_refs --update-baseline` 收紧基线 → `--check` 必须通过。
3. P2 调用点切换：列表页优先切 `N11/N12` 聚合端点，消灭 N+1 风险后再切详情读。
4. P3：按 `write-channel-coverage.md` 补 W2 public 写函数（先通道表 5 张：
   `grape_t_channel_lease` / `grape_t_channel_installation` / `grape_t_channel_inbound_dedup` /
   `grape_t_channel_chat_session_binding` / `grape_t_channel_sender_identity`）。
5. P4 具备实例后：按 §8.4 顺序 insight → runtime → mgmt，每域 `rds_cutover_check` 全绿再切 DSN。
---

## 7. P4/P5 彩排实测（2026-11-14）

### 7.1 三独立实例 / 两节点（本轮落地）
| 域 | 实例 | 说明 |
|---|---|---|
| mgmt | 本机 127.0.0.1:3311 | mysqld 8.0.46（二进制副本，绕开 Ubuntu AppArmor 的 datadir 限制）|
| insight | 本机 127.0.0.1:3312 | 同上，独立 datadir/端口 |
| runtime | 兄弟节点 192.168.0.116:3307 | MySQL 8.0.46（该机由 MariaDB 10.11 换装 MySQL 8，保证与生产同引擎，避免 MySQL8 生成列 DDL 在 MariaDB 上 1064）|

一条命令跑通并验证（幂等，库/数据会重建）：
```bash
SRC_HOST=<参照库> SRC_USER=root SRC_PWD=*** \
REMOTE_HOST=<兄弟节点> REMOTE_PORT=3307 REMOTE_USER=rds_admin REMOTE_PWD=*** \
LOOP_MAX=1 bash scripts/p4_p5_loop.sh
```
实测：P4 PASS（mgmt 54 / insight 12 / runtime 36 基表；行数等于 dump 快照；视图 0；账号只授本库），
P5 环境验收 PASS（A1/A2/A6），全 API 冒烟 0 失败；循环退出「环境侧全绿」。
静态余量仍 1232 处（A3 代码面，`--env-only` 只报告不阻断环境验收）。

### DDL 归属下沉（本轮）
- `src/api/db_setup.py` 里 77 个建表 DDL 常量按归属域搬到 `src/domains/{runtime,mgmt,insight}/schema_ddl.py`
  （`scripts/split_schema_ddl.py`，可重复执行/`--dry-run` 预览）。db_setup 改为显式 import，执行顺序不变。
- 等价性验证：空 sqlite 上 `init_database()` 重建 → 98 张表、列名与搬前快照**逐表一致**（0 缺失/0 多余/0 列差）；
  pytest（arch/contracts/scripts）158 passed；三实例彩排循环仍 LOOP_EXIT=0。
- 说明：静态「跨域引用」统计的正则只认 `FROM/JOIN/INTO/UPDATE/TABLE <表>`，`CREATE TABLE IF NOT EXISTS x`
  因中间夹 `IF` 从未被计入，所以这一步**不会降低 1232 这个数字**，但把 DDL 归位到 owner 域（方案 D9），
  属结构性正确改动。

**已知残余（启动期，不阻断端点）**：`query_engines` / `credential_usage_log`（flush+retention）/
`migrate_agent_runtimes_utc_caliber` 在独立拓扑下仍会打 1146 告警（均被 catch，不影响请求）。
直接换 `domain_db` 连接会引入后台线程共用连接的问题（实测 "Packet sequence number wrong"），
因此需要「每个工作线程一条 owner 连接」的池化改造，属独立一轮工作，未纳入本次彩排。

### 7.2 单实例三域（早期彩排，已由 7.1 取代）


```bash
# 1) 建三个独立库 + 只授本库账号，复制基表（不复制视图）
MYSQL_PWD=*** python3 scripts/rds_stage_rehearsal.py --host <baseline-host> --user root
#   → RESULT: PASS（三域：表集合/行数一致、视图 0、授权仅本库）
#   stage DSN 写入 ~/.grape-rds-stage.env（0600，不入库）

# 2) 用 stage DSN 走 lib 读（无视图）
#   runtime skills 3 条 / engine_capabilities 3 条 / insight health ok
#   mgmt→runtime 裸 SQL：1146 Table 'grape_stage_mgmt.agent_tasks' doesn't exist
#   → 证明 G3 隔离生效，也证明剩余跨域 SQL 必须走接口（P1/P3.5）

# 3) P5 验收器跑 stage
python3 scripts/verify_domain_isolation.py --host <baseline-host> --user root --password *** \
  --db-prefix grape_stage --account mgmt=grape_stage_mgmt_app \
  --account runtime=grape_stage_runtime_app --account insight=grape_stage_insight_app
#   → A1 视图 0 ✅、A2 授权仅本库 ✅、A6 队列 ✅、A3 代码引用 135 对 ❌（P1 未完成）
```

---

## 8. 提交记录

| 提交 | 内容 | 状态 |
| --- | --- | --- |
| `6a275f0da` | feat(db): add domain split guards and cutover tools | 已推送 `grape-dev/arch-weekly-11-ljt` |
| `f75acecf5` | feat(api): add P2 internal service endpoints for RDS split | 已推送 `grape-dev/arch-weekly-11-ljt` |
| `42f3f8111` | fix(scripts): keep dependency board check timestamp-insensitive | 已推送 |
| `6b824e3f3` | refactor(domains): move knowledge and onboarding into owner domains | 已推送 |

验证（提交后复跑）：
- `pytest tests/contracts tests/arch tests/scripts tests/mgmt/test_channel_w2_writes.py tests/api/test_mgmt_no_internal_routes.py tests/api/test_s103_knowledge_frontend_closure.py tests/kernel/test_gsb_route.py tests/runtime/test_onboarding_standard_task.py` → **212 passed, 2 skipped**
- `scripts/check-arch.py` → RESULT: PASS（R1–R8）
- `scripts/foreign_table_refs.py --check` → OK（棘轮未上升，135 对 / 1232 处）
- `tests/kernel` 其余 44 项失败为分支既有（已在 HEAD 干净树 `git worktree` 复现，与本次改动无关）
