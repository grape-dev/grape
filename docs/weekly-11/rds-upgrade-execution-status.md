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

> 交接文档（新会话从这里开始）：`docs/weekly-11/handoff-2026-11-14.md`

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

### kernel/lock 分布式锁协议下沉 owner 域（-9，本文件清零）
- 文件：`src/kernel/lock.py`（9 处 → **0**）；全局静态 **294 → 285**（-9，71 → 70 对，
  `distributed_locks,kernel` 这一对整对消失）。
- 表分布（消费域 kernel，唯一外域表）：`distributed_locks`（归 **runtime**）9 处 =
  **写 6**（`INSERT` 首抢 L34 / 冲突重试 L50、条件 `UPDATE` 抢占 L61 / 续期 L103、
  `DELETE` 释放 L84 / 清理 L123）+ **读 3**（L44 查 holder+expires_at、L67 抢占后验 holder、
  L108 续期后验 holder）。
- owner 侧新增（runtime public，全部「任意拓扑」`?` 方言，SQL 只在 runtime 域一份）：
  · `_ANY_QUERIES` 7 条具名查询：`lock_insert` / `lock_row` / `lock_takeover` / `lock_holder` /
    `lock_release` / `lock_renew` / `lock_cleanup`（列序、谓词与原语句逐字一致）；
  · 4 个协议原语：`acquire_distributed_lock(lock_key, holder, ttl, now)` /
    `release_distributed_lock(lock_key, holder)`（返回删除行数）/
    `renew_distributed_lock(lock_key, holder, ttl, now)` /
    `cleanup_expired_distributed_locks(now, holder)`（返回删除行数）。
- **设计取舍（关键：为什么不是「kernel 调四个具名查询自己拼」）**：
  1. 抢锁是**四步读-改-写**（INSERT 冲突 → 查过期 → `WHERE expires_at < now` 条件 UPDATE →
     验 holder），原实现整段持 `db_lock`。只有把四步放进 owner 域**同一个** `_any_tx()`
     才能保住单进程原子性（自有 DSN = 真事务，单库/测试拓扑回落进程库 + `db_lock`）；
     拆成 3 次 `exec_any` 会把一次临界区变成三次，属语义降级。
  2. `INSERT` 主键冲突**吞掉后在同一事务内继续**（SQLite/MySQL 都是语句级回滚，事务仍可用），
     与原实现 `except Exception: db.rollback()` 后继续查行等价；两处吞异常均改 `log.debug`，
     不触发静默 except 棘轮。
  3. `now` 由调用方传入（`int(time.time())` 每调用取一次）：保持原口径「同一次调用内
     locked_at / update_at / created_at 同一秒、expires_at = now+ttl」，也让时间边界可测。
  4. kernel 只保留 holder 标识（`_holder`，`tests/runtime/test_issue_993_branch_guard.py`
     等用例 `monkeypatch.setattr(lock, "_holder", ...)` 依赖它）与 strip 口径；不再 import
     `db`/`db_lock`，也不再出现任何 SQL。
  5. kernel 顶层 import domains 会被 check-arch R1 拦：改**函数内延迟导入**
     （`from src.domains.runtime import public as runtime_public`），并在
     `scripts/check-arch.py` WHITELIST 以**精确 targets**（`["src.domains.runtime"]`）登记，
     不用字符串形态整条豁免（避免吞掉该文件今后其它跨域 import）；同时删掉已清零的
     R8 条目 `src/kernel/lock.py: {"distributed_locks": 6}`（棘轮只减不增）。
- **修的存量问题**：无功能 bug 修复。记录 3 条如实事实：
  1. 三库独立后 kernel 进程库里没有 `distributed_locks`（视图已退役），原实现必然 1146，
     而抢锁失败会被上层 `except` 吞成「放弃执行」——迁移任务、上传清理、技能同步、分支守卫
     租约都会静默失效。现在走 runtime DSN，单库/三库同一条路径。
  2. 原 `acquire_lock` 末尾 `return row2 and row2[0] == _h` 在「抢占后行被第三方删除」的极端
     竞态下返回 `None`（非 False）；新原语统一返回 `bool`。真值语义不变，类型更严（已记录）。
  3. `src/domains/insight/observability/sample_lease.py` 仍有自己的 `distributed_locks`
     读点（`_READ_QUERIES.distributed_locks_4e8732` + 本地兜底 SQL，属 insight 消费域另一个
     文件），本轮不在单文件范围内，未动；基线里 `distributed_locks,runtime,insight,1` 保留。
- **新基线**：**285 处 / 70 对**（`--update-baseline` 已收紧）；依赖看板与
  `next-batches.md` 已刷新（下一批 `src/kernel/gsb/registry.py` 9 处）。
- 验证（全部真实退出码）：
  · `py_compile` 三文件 → COMPILE_OK；
  · `~/burndown/distributed_lock_smoke.py`（sqlite，`DEVOPS_DB_BACKEND=sqlite`）**51 项全通过**
    （`ALL distributed_lock smoke PASSED`）：首抢六列同秒快照 / 他人持有不写行 / 同 holder 不重入 /
    `expires_at == now` 不过期与 `now-1` 可抢占 / 0 值与 NULL(NOT NULL 拒绝) /
    ttl=0 同秒不接管、下一秒接管 / 抢占保留 created_at / 释放只删自己且幂等 /
    续期只续自己、`locked_at` 不动、他人续期零写入 / 清理只删「过期且非本实例 holder」且
    `expires_at == now` 不删 / kernel 包装层默认 holder 与 per-call holder 隔离 /
    holder strip / 未登记查询名 KeyError / 静态否定「lock.py 无 SQL 字面量、不直连进程库」；
  · `check-arch.py` **RESULT: PASS**（R1–R8；R8 条目已删，R1 精确 targets 登记）；
    `check_lib_query_names.py` **PASS**（runtime 115 funcs + 42 queries used / 186 + 61 registered）；
  · `rds_gate.sh --quick` **GATE: 全绿**（棘轮未上升、内部契约 37 条最新、看板已刷新）；
  · `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` import OK；
  · 定向回归 `test_issue_993_branch_guard` / `test_issue_2028_branch_guard_fail_open` /
    `test_obs_sample_lease` / `test_issue_699_phase4_servers`：**19 failed / 112 passed**，
    在 `git worktree add … HEAD` 干净树上重跑得到**完全相同的 19 条**（diff 为空，均为既有失败）。
- **未验证项（如实）**：本轮未跑 `p4_p5_loop.sh` 三实例彩排——会话内无 `SRC_PWD`/`REMOTE_PWD`
  环境变量，且本批无 schema/迁移/契约面变化（不改表、不新增路由）；运行面证据为 sqlite 烟测 +
  单库 pytest，不把彩排结果算进来。

### patrol_compensate 补偿责任链接口化（-10，本文件清零）
- 文件：`src/api/patrol_compensate.py`（10 处 → **0**）；全局静态 **304 → 294**（73 → 71 对，
  `grape_t_patrol_findings` 与 `managed_repos` 各掉一对）。
- 表分布（消费域 api）：`grape_t_patrol_findings`（归 mgmt）7 处 —— 载入 finding 1、回执读 1、
  回执+状态回写 1、抢占 `open→claiming` 1、租约超窗扫描 1、回收回写 1、批处理捞单 1；
  `managed_repos`（归 mgmt）1 处（host_user_id）；`user_repos`（归 mgmt）1 处（首个管理员）；
  `agent_tasks`（归 runtime）1 处（终检读原任务终态）。
- **设计取舍**：责任链跨三个进程（api/mgmt/runtime 各自可能跑巡检），原实现用进程库 `db` +
  `db_lock`。三库独立后进程库里没有这两张表（无跨库视图 → 1146），而且 `db_lock` 只能序列化
  **同进程线程**——抢占（`status='open'` 条件写）本来就是靠 DB 语句原子性，不是靠锁。因此：
  ① 语句全部上移 owner 域 public（mgmt 6 条具名查询 + 5 个薄原语、runtime 1 条 + 1 个薄原语）；
  ② 抢占改成 `exec_any("patrol_finding_claim")` 的 rowcount 判据，跨进程语义反而变强；
  ③ 两处「读-改-写」合并成**一个** `_any_tx()` 组合原语（`patrol_finding_mark` 回执追加、
  `patrol_reclaim_stale_claimings` 扫+逐行回写），不拆成多次 `exec_any`——否则一次原子写变多次提交；
  ④ 回执合并/2000 截断/冷却窗/`ORDER BY created_at`（UUID id 排序=随机挑）这些**口径**跟着语句
  进 owner 域，调用方只剩业务分支；⑤ `managed_repos.host_user_id`、`user_repos` 首个管理员复用
  mgmt public 已有的 `managed_repo_host_user` / `repo_admin_user_id`（不新造重复原语）。
- **口径保持（未擅自"修好"）**：`host_user_id='0'`（TEXT）在旧实现里是真值 → 仍算有主体，
  本轮照抄；`action_meta=''` 的 finding 不受 30 分钟冷却限制、`updated_at=''` 视同过期回收，
  两条边界都进了烟测。
- **测试 seam 迁移**：`tests/mgmt/test_patrol_unit.py` 的 12 处 `patch.object(pc, "db"/"db_lock")`
  改指 `src.kernel.session_manager.db/db_lock`（`*_any` 的回落路径在函数内 import 该句柄，
  打模块别名会静默打空）；3 处读源码文本的 SQL 守卫（ORDER BY / `||` 方言 / 冷却谓词 /
  `needs_human` 不在捞单 status）改为守 owner 域具名查询文本——守卫存活，没有为了绿而删断言。
  该文件失败集合与改前基线逐条比对：**42 failed / 72 passed → 42 / 72，新增 0**。
- **存量 bug（本轮发现，未修，超本批单文件范围）**：`src/api/patrol.py` 的
  `_NEXT_STEP_RE` 只被使用（L342）没有定义，`parse_target_agent` 恒 `NameError` 被上层
  `except` 吞成「next_step 不可解析」→ 补偿终检一律判 `target_unresolvable`。
  丢失点在 `9b5bc6e05 refactor(patrol): route the detection and finding paths through owner
  primitives (-25)`（该常量原为 `re.compile(r"^a2a[-:]\s*([a-zA-Z0-9_\-]+)", re.I)`）。
  它是 `tests/mgmt/test_patrol_unit.py` 42 条既失败的主要成因（改前基线同样 42 条），下一批修复。
- **新基线**：**294 处 / 71 对**（`--update-baseline` 已收紧）；依赖看板与矩阵已刷新。
- 验证：`check-arch.py` RESULT PASS（R4 report-only 198）；`rds_gate.sh --quick` GATE 全绿
  （棘轮未上升、内部契约 37 条、看板最新）；`pytest tests/arch tests/contracts tests/scripts`
  **158 passed**；`make lint` import OK；sqlite 行为烟测 46 项全通过
  （`~/burndown/patrol_compensate_smoke.py`：列序/回执合并与截断/抢占否定用例/租约窗与
  `updated_at=''` 边界/幂等/捞单 FIFO 与冷却豁免/身份降级/批处理 limit）。

### event_pipeline 事件管道场景/仓库接口化（-10，本文件清零）
- 文件：`src/domains/runtime/pipeline/event_pipeline.py`（10 处 → **0**）；全局静态 **314 → 304**（73 对）。
- 表分布（消费域 runtime，两表都归 mgmt）：`devops_pipeline_scene` 6 处（QA 节点 2、PR 订阅列表 1、
  关联仓库 1、场景部署地址登记 1、启用位判定 1）、`managed_repos` 4 处（bot 用户名 1、org_id 1、
  仓库部署地址登记 1、默认场景 1）。
- owner 侧新增（mgmt public，全部「任意拓扑」`?` 方言，SQL 只在 owner 留一份）：
  · 具名查询：`scene_nodes_by_repo_scene` / `pr_subscription_scenes` / `scene_active_nodes_by_repo_scene` /
    `scene_linked_repos` / `scene_active_flags` / `scene_deploy_url_set` / `repo_bot_username` /
    `repo_org_id` / `repo_deploy_url_set`（列序、WHERE 通配口径与原 SQL 逐字一致）
  · 薄函数：`scene_nodes_any` / `list_pr_subscription_scenes_any` / `scene_active_nodes_any` /
    `scene_linked_repos_any` / `scene_active_flags_any` / `set_scene_deploy_url_any` /
    `repo_bot_username_any` / `repo_org_id_any` / `set_repo_deploy_url_any`；`repo_dev_scene` 复用上一批已建的原语。
- 关键设计取舍：
  1. **每个站点原本都是三段式兜底**（`mgmt_lib()` → `mgmt_lib(foreign_only=True).read_one()` → 本地兜底 SQL），
     只有最后一段被静态统计计入。本轮统一塌缩成一次 owner 原语调用：自有 DSN 走 mgmt 连接，单库/测试走进程库，
     同一份语句两条拓扑都能跑，调用方不再留任何 SQL（兜底段消失，双重 lib 探测也一并删掉）。
  2. **不过滤 `is_active` 的那条照抄**：`_scene_qa_skill` 读场景节点时历史口径就是「不看启用位」
     （停用场景的 QA 节点仍可解析），与 PR 订阅/关联仓库/部署登记三处的 `is_active=1` 口径不同。
     九条语句各自把过滤条件写进 owner 侧，没有为了「统一」改语义——`scene_nodes_any` 的 docstring 已标注。
  3. **写路径保留 outbox 优先**：`register_sre_deploy_url` / `register_scene_deploy_url` 仍先走
     `cross_write.submit`（幂等键 + 失败排队重试），只在 lib 不可用/异常时回落 owner 原语自提交；
     调用方事务语义简化为「rowcount>0 即登记成功」，与原 `db.commit()` 分支等价。
  4. **`repo_key` 唯一键的前提下删掉 `ORDER BY org_id DESC`**：原语句带排序，但 `managed_repos.repo_key`
     是主键，排序对结果集无影响，保留只会让人误以为存在多组织行（已在调用点注释写明）。
  5. **`to_repo_key()` 的 provider 前缀不动**：`_resolve_bot_username` 先对 repo_key 做规范化
     （`gitcode.com/a/b` → `gitcode:gitcode.com/a/b`）再查库，这是既有存储口径，原语签名沿用同一入参，
     不在这里「顺手修正」。
- 修的存量问题（无功能 bug，但都是三库独立后必然 1146 的隐性跨库依赖）：
  1. 上述三段式兜底在独立拓扑下第二段返回 None、第三段在 runtime 库上查 mgmt 表 → 异常被外层 `except`
     吞成空值：QA skill 解析恒空（PR 订阅不派 QA）、org_id 恒 0（引擎选型退化为「个人组织」直选本机）、
     bot 用户名恒空（回落平台全局 bot）、部署链接永不登记。现在同一条 owner 原语路径覆盖单库与多库。
  2. `_match_pr_subscription` / `_dispatch_pr_subscription_redispatch` 过去先 `with db_lock:` 再调
     mgmt lib——为一条 mgmt 读把 runtime 进程库锁一起持住；现在该锁只覆盖 runtime 自己的查询。
  3. 静默 except 未动（本文件 `except Exception: pass` 7 处是已审阅豁免，改文案会让豁免名额失配），
     棘轮基线保持 56 不变。
- **未做（如实标注）**：`_READ_QUERIES` 里 5 条 index 型具名查询（`devops_pipeline_scene_8b6d22/7a48a2/
  b1eaa2/ab0110`、`managed_repos_3564e6`）本轮失去调用方，属多库拓扑既有读面，**保留在 `_READ_QUERIES`**
  （删除需跨分支确认无其他消费方），不随本批清空。
- 验证：`py_compile` COMPILE_OK；`~/burndown/event_pipeline_smoke.py`（sqlite，DEVOPS_DB_BACKEND=sqlite）
  覆盖 10 个站点 + 特殊口径（停用场景 QA 节点仍可解析、订阅三条件过滤、空 bot 回落、写语句只在用行/
  已托管仓库才落库、启用位含 `repo_key=''` 通用行、无行=有效、`repo_org_id` 无行 None/0 值原样、
  未登记查询名 KeyError、静态否定「本文件两表可执行 SQL 为 0」），**ALL EVENT_PIPELINE SMOKE CHECKS PASSED**；
  `rds_gate.sh --quick` **GATE: 全绿**（R1-R7 PASS / 棘轮未上升 / 内部契约 37 条 / 看板已刷新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` import OK；
  `check_lib_query_names.py` PASS（mgmt 101 funcs + 19 queries used / 163 funcs + 35 queries registered）；
  定向回归 `tests/runtime/test_issue_468_sre_deploy_url_register.py` +
  `test_issue_426_pr_sub_binding` / `test_pr_subscription_dedup` / `test_issue_528_pr_sub_env_integration` /
  `test_issue_568_scene_visibility_unification` / `test_issue_259_offline_dispatch` **40 passed**。
- 已知非本批（HEAD 同源）：`tests/runtime/test_issue_596_p0_direct_executor_migration.py` 4 failed
  （re-export/无定义/RE 池断言），在 `git worktree add … HEAD` 上复现完全相同的 4 条，与本批无关。
- 基线：**304 处 / 73 对**（`devops_pipeline_scene,mgmt,runtime` 12→6、`managed_repos,mgmt,runtime` 15→11）；
  `next-batches.md` 已刷新（下一批 `src/api/patrol_compensate.py` 10 处）。
- 未做（如实标注）：本轮未跑 `p4_p5_loop.sh` 三实例彩排——改动只涉及 mgmt owner 原语与 runtime 调用点，
  无 schema / 迁移变化，跨域写仍走 outbox；运行面证据为 sqlite 烟测 + 单库 pytest，未把彩排结果算进来。

### scene_sync 场景同步接口化（-10，本文件清零）
- 文件：`src/domains/runtime/storage/scene_sync.py`（10 处 → **0**）；全局静态 **324 → 314**（73 对）。
- 表分布（消费域 runtime，两表都归 mgmt）：`scene_market` 8 处（读取 3 / 精选计数与标记 2 / upsert 2 / 图标回写 1）、
  `scene_skill_ref` 2 处（DELETE + INSERT）。
- owner 侧新增（mgmt public，全部「任意拓扑」`?` 方言，SQL 只在 owner 留一份）：
  · 具名查询：`scene_official_id_by_name` / `scene_official_row_by_name` / `scene_official_id_names` /
    `scene_featured_official_count` / `scene_set_icon_by_id`（列序与 scene_sync 原查询逐字一致）
  · 薄读函数：`official_scene_id_any` / `official_scene_row_any` / `list_official_scene_ids_names_any` /
    `count_featured_official_scenes_any`；薄写函数：`mark_default_featured_any` / `set_scene_icon_any`
  · 组合写（`_any_tx()`，多语句原子）：`upsert_scene_content_any`（hash 幂等 upsert）、
    `replace_scene_skill_ref_any`（DELETE + `INSERT OR IGNORE`）
- 关键设计取舍：
  1. 原实现每条跨域语句写了两遍：`mgmt_lib` 句柄非空走 lib/outbox，为空走「本地兜底 SQL」，
     兜底正是本文件跨域引用的全部来源。改成「一份 SQL + 任意拓扑执行」后兜底消失；
     **跨域写仍只走 outbox**（`scene_upsert` / `scene_skill_ref_replace` / `scene_featured_init` 三条幂等意图，
     payload 带完整内容可重放），owner 直写只发生在单库拓扑（表就在进程库里，不构成跨域写）。
  2. 跨库原子事务不可得，所以组合写按「先解析、后一次重建」拆：技能解析（`skill` 是 runtime 自有表）留在调用方
     的 `db_lock` 事务里，refs 收集齐后交给 `replace_scene_skill_ref_any` 在 owner 侧一次 `_any_tx()` 删+插，
     **不再有「先 DELETE 再远端失败留空引用」的窗口**（与既有 lib 路径同口径）。
  3. 动态 IN 列表（精选初始化）留在 owner：`mark_default_featured_any` 只在「名字个数」上拼占位符，
     表名/列名不来自外部；过滤口径 `source='official' AND is_active=1` 原样保留。
  4. 历史键名 `market_desc` / `tags_str` 由 owner 侧 `_SCENE_FIELD_ALIASES` 映射为 `market_description` / `tags`，
     调用方字段字典不用改。顺带修掉一个静默缺口：MySQL 版 `upsert_scene_content` 过去按列名过滤 payload，
     这两个键对不上列名 → lib 路径长期丢「市场描述 / 标签」两列；映射放进 owner 后两条路径都写全。
- 修的存量 bug（两处静默失效）：
  1. `_detect_stale_official_scenes`：`_mg_lib_xxx.read_all(...).fetchall()` 的结果被赋给了**游标变量**，
     `_rows_stale` 没赋值 → NameError 被外层 `except Exception` 吞掉，该分支下 stale 检测永久返回 0（名单从不产生）。
     改成单一具名查询后该分支不复存在。
  2. `sync_official_scenes` 解包方向反了：`_persist_scene_row` 按 docstring 返回 `(scene_id, action)`，
     调用方写成 `action, market_id = ...` → `action` 恒为整数，`skipped` 短路失效；`_apply_scene_icon` 拿到的是
     `"added"/"updated"` 字符串当 id → `UPDATE ... WHERE id=?` 命中 0 行，**同步进来的场景图标从来没落过库**。
     已改为 `market_id, action = ...`。
- 验证：`py_compile` COMPILE_OK；`/tmp/scene_sync_smoke.py`（sqlite，DEVOPS_DB_BACKEND=sqlite）覆盖
  新建/幂等跳过（updated_at 不变）/更新保留 `is_active` 与 `is_featured`/official 与启用过滤/IN 列表过滤/
  ref 重建去重与清空/图标两列回写/unknown query KeyError/遍历不完整跳过，**ALL SCENE_SYNC SMOKE CHECKS PASSED**；
  `rds_gate.sh --quick` **GATE: 全绿**；pytest arch/contracts/scripts **158 passed**；`make lint` import OK；
  `tests/mgmt/test_scene_sync.py` + `tests/mgmt/test_scene_skill_ref_authoritative.py` + `tests/runtime/test_r31_p45_p46.py`
  **69 passed**（其中两处 mock 的 DB 句柄改为指向内核句柄：写入路径已归位，再 mock `scene_sync.db` 已无效）。
- 已知口径：把 arch/contracts/scripts 与 tests/mgmt、tests/runtime 混在同一进程里跑，`skill.scope` / `scene_market`
  会因前述测试的进程库污染而报 missing；HEAD 同一组合也有 7 项失败（本批 10 项里多出的 3 项是同一污染的连带），
  属既有测试隔离问题，故按仓库惯例分域跑。
- 基线：**314 处 / 73 对**；`next-batches.md` 已刷新（下一批 `src/api/patrol_compensate.py` 10 处）。
- 未做（如实标注）：本轮未跑 `p4_p5_loop.sh` 三实例彩排——改动只涉及 mgmt owner 原语与 runtime 调用点，
  无 schema / 迁移变化，独立拓扑的 outbox 写路径未改；运行面证据为 sqlite 烟测 + 单库 pytest，未把彩排结果算进来。

### kernel/prompt_builder 跨域读归 owner public（-8，可执行 SQL 清零）
- 文件：`src/kernel/prompt_builder.py`（11 处 → **3**，余下 3 处是 prompt 文案，见下）；全局静态 **332 → 324**（75 对）。
- 表分布（消费域 kernel）：`agent_runtimes` 2 + `runtime_credentials` 1（归 runtime）；
  `scene_knowledge_ref` 2 + `agent_shared_knowledge` 2（归 mgmt，同域 JOIN）；`managed_repos` 1（归 mgmt）；
  `unified_user_mapping` 3（**非本进程 SQL**）。
- owner 侧新增（一律「任意拓扑」`?` 方言，SQL 只在 owner 留一份，单库/多库同一份）：
  · runtime public：具名查询 `runtime_locality`（`agent_runtimes LEFT JOIN runtime_credentials`
    一次取 `(credential_id, last_ip)`）、`runtime_engine_name`；薄函数同名两个。
  · mgmt public：具名查询 `knowledge_ref_manual_exists`、`knowledge_ref_agent_rows`、
    `repo_dev_scene`；薄函数 `has_manual_knowledge_ref()` / `list_agent_knowledge_refs()` / `repo_dev_scene()`。
- 关键设计取舍：
  1. **本地性判定的两条单点查合成一次同域 JOIN**：原实现先取 `credential_id`、再按 cid 取 `last_ip`，
     第二句在凭据行缺失时返回 None 行 → 判「非本地」。LEFT JOIN 的「无行」语义与之等价，顺带消掉
     两查之间被并发改写的窗口。`cid==0 → 本机直连`、`last_ip 前缀 127.` 两条**业务口径留在调用方**——
     那不是域数据口径，放进 owner 会让 public 长出业务分支。
  2. **跨域 JOIN 的处理是「搬进 owner」而不是「拆成两次往返 + Python 内连接」**：
     `scene_knowledge_ref × agent_shared_knowledge` 两张都属 mgmt，JOIN 本身不跨域，搬进 mgmt public
     后原 SQL 的通配口径（`col = ? OR col = ''` / `= 0`）、`enabled=1`、`accepted`、`archived=0`、
     `DISTINCT` 一次性照抄，零语义漂移；拆成两次往返反而要在 kernel 里复刻过滤逻辑（更容易漂）。
  3. **列序逐字照抄**：`list_agent_knowledge_refs` 返回 6 列 `(id, scope, source, obs_key, file_path,
     content_hash)`，调用方仍按下标解包，既有 `read_entry_content` 调用链一行未改。
  4. **失败口径照旧**：owner 原语抛错时 kernel 仍走原 except 分支（本地性 → `None`「未知」并触发
     localhost 回退、manual 判定 → `False`、知识段落 → 回退 scope 三级注入且 `injected_ids=[]`）。
- 修的存量问题（无功能 bug，但都是**三库独立后必然 1146 的隐性跨库依赖**）：
  本地性判定与「引擎名注入」原先直接读进程主库，分库后被 `except` 吞掉 → 静默退化成「非本地 / unknown」
  （任务回连地址会错指公网）。现在同一条 owner 原语路径覆盖单库与多库。另把「引擎名注入」的
  `except: pass` 改成 `_log_safe("debug", ...)`，静默 except 棘轮本文件 19 → **18**（只减不增）。
- **未纳入本轮（如实，不假收口）**：`unified_user_mapping` 3 处（行 621/627/630）全部位于
  `_build_feishu_context_section` 的 f-string 里，是**教 agent 用 sqlite3 自写 sessions.db 的说明文本**，
  本进程不执行。改它得先给 agent 一个合法写入通道（现只有 mgmt `/internal` 服务端点，agent 持的是会话
  token，公开面没有 mapping 写接口），属独立设计项；本轮保留文案并在此记明——不为了让正则掉数字而改文案。
  同时该文案在分库拓扑下本就不可执行（`unified_user_mapping` 不在 runtime 库），建议单列为「给 agent 的
  用户映射写接口」设计项；R8 白名单该文件 `unified_user_mapping: 2` 因此保持不变（真写语句仍是 2 条文案）。
- 基线收紧 **324 / 75 对**（3 张表的 3 个消费对随文件清零消失）。
- 验证：`rds_gate.sh --quick` → **GATE 全绿**（R1-R7 PASS、棘轮未上升、契约 37 条最新、看板已刷新）；
  `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；`make lint` → import OK；
  sqlite 烟测 `/tmp/prompt_builder_smoke.py` → **ALL PASSED**（本地性五种判定 + 凭据行缺失/NULL、
  引擎名兜底、manual 通配与非对称匹配、agent 知识 DISTINCT 去重/六列列序/正文注入/8000 字截断、
  dev_scene 有值·空值·无行、三条失败回退口径、静态否定「这五张表的可执行 SQL 已为 0」）。
- 彩排（三实例/两节点，正确账号口径 `REMOTE_USER=rds_admin`）：P4 **PASS**（mgmt 54 / insight 12 / runtime 36
  基表，视图 0，行数等于快照，账号仅本库）；P5 `--env-only` **PASS**（G2/G3/G4/A6 全绿，跨库授权 0）；
  全 API 冒烟 **172 端点 0 失败**（含 3 个写场景）。

### engine_select 引擎选型/绑定维护接口化（-18，本文件清零）
- 文件：`src/domains/runtime/engine/engine_select.py`（18 处 → **0**）；全局静态 **350 → 332**（79 对）。
- 表分布（消费域 runtime）：`grape_accounts` 5、`autopilot_tasks` 4、`user_repos` 3、`devops_pipeline_scene` 3、
  `external_identities` 2、`unified_user_mapping` 1。
- 新增（mgmt public）：`_ANY_QUERIES` 具名查询表（`?` 占位符）+ `read_one_any/read_all_any/exec_any`
  （任意拓扑：自有 DSN 走 db_registry，单库/测试回落进程主库 + db_lock），共 11 条查询；
  薄函数 10 个：`mapping_user_id_by_column` / `account_id_by_platform_login` /
  `repo_user_id_by_platform_login` / `first_join_user_id_by_repo` / `user_repo_keys_by_username` /
  `list_active_scene_nodes` / `list_active_scene_bindings` / `list_enabled_autopilot_engine_ids` /
  `list_autopilot_engine_ids_nonempty` / `set_autopilot_engine_ids_any`。
- 关键取舍：
  1) **原实现对每个站点都是「lib 优先 + 本地兜底 SQL」双份**（`mgmt_lib(foreign_only=True)` 无自有 DSN 时返回 None，
     兜底 SQL 就是跨域统计来源）。本轮把兜底路径换成 owner 域的**任意拓扑原语**——单库拓扑由 owner 自己路由连接，
     同一份 SQL 两条连接都能跑，调用方不再需要兜底，语义不变而引用清零。
  2) **动态列名收回 owner 域**：`unified_user_mapping` 的列名原由调用方用 `um_column_for_repo()` 拼进 f-string。
     改为 mgmt 侧 `_UM_COLUMN_QUERIES` 白名单（`gitcode_user` / `github_login`）挑选具名查询；
     未知平台（如 gitee）列名为空串 → 抛 KeyError → 调用方原有的 `except → 0` 兜住，与原「空列名拼出语法错 → 返回 0」逐字同口径。
  3) **跨域 JOIN 不拆**：`user_repos JOIN grape_accounts ... EXISTS external_identities` 涉及的三张表全归 mgmt，
     按「JOIN 留在 owner 域、一次具名查询」处理，比拆成两段 + Python 连接更省往返且不丢 `LIMIT 1` 语义。
  4) **写路径保留幂等意图**：`remap_engine_references` 的任务引擎回写仍优先走 `cross_write.submit`（失败排队重试），
     只在 lib 不可用/失败时回落 `set_autopilot_engine_ids_any`（owner 域自提交），不再有本域 SQL。
  5) 场景读（`devops_pipeline_scene`）原本走进程库游标：三库拓扑下该游标是 runtime 库，读 mgmt 表必 1146。
     改用 owner 原语后 **顺带修掉这个存量缺陷**（`cleanup_scene_engine_bindings` /
     `remap_scene_engine_bindings` / `_find_scene_bindings_for_runtime` 三处）；后者与
     `_find_autopilot_tasks_for_runtime` 已不依赖调用方游标（`c` 仅为兼容签名保留）。
- 语义验证：sqlite 烟测 `/tmp/engine_select_smoke.py`（五级归属链 + 列白名单 + status/LOWER/is_join/id NOT IN 口径 +
  只动活跃场景 + 停用任务不进提示面 + 写回写）**ALL PASSED**；
  另做「改动前 vs 改动后」对拍：同一批 18 个 (repo_key, issue_creator) 输入，返回值 **逐字一致**（含 gitee 未知平台 → 0）。
- 门禁：`rds_gate.sh --quick` **GATE 全绿**（check-arch R1–R8 / 棘轮 / 内部契约 37 条 / 看板）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` → import OK；
  `check_lib_query_names.py` PASS（mgmt 88 funcs + 28 named queries / 143 funcs + 35 queries registered）。
- 已知非本批问题：`tests/api/test_named_read_queries.py::test_account_queries_match_the_real_call_sites` 因
  `runtime public._READ_QUERIES` 缺 `account_id_by_platform_login` 键而 KeyError（该键在本分支 HEAD 就不存在，
  与本批无关；tests/api 不在门禁集内）。

### 三实例彩排首次全绿（此前唯一未验项，含 3 个真缺陷修复）
```bash
SRC_HOST=… SRC_PWD=… REMOTE_HOST=… REMOTE_PORT=3307 REMOTE_USER=rds_admin REMOTE_PWD=… \
  LOOP_MAX=1 bash scripts/p4_p5_loop.sh
```
- **P4 PASS**：三实例 / 两节点，表集合一致、行数等于 dump 快照、视图 0、账号授权仅本库。
- **P5 PASS**：G2 / G3 / G4 / A6 全绿（`--env-only`，A3 静态面单列）。
- **冒烟：172 个端点，失败 0**（169 读 + 3 写场景 + 46 写端点空体探测）。
- `RESULT: 环境侧全绿（P4 落库 + P5 A1/A2/A6 + 冒烟）`。

过程中修掉三个真缺陷（都不是本批引入，但此前一直没被发现）：
1. **`rds_p4_provision.py` 生成的 stage env 漏写 `GRAPE_DOMAIN_LIB=1`** →
   `db_registry.uses_own_dsn()` 恒 False，所有「任意拓扑」原语回落进程库（mgmt）→ 查
   runtime/insight 表统一 1146。补写该键后 1146 告警成片消失。
2. **`runtime/im/engine_wiring.py` 用了 `_mgmt_public` 却没 import**（3921a54c6 归位时漏的）→
   运行期 `NameError` 被 except 吞成「读不到安装 → None」，IM 推送静默失效。补 import，
   7 个调用点（`installation_*`）全部恢复。
3. **`runtime/engine/instances.py` 的 4 条 `agent_runtimes` / `runtime_credentials` SQL 走进程库** →
   `PUT /api/chat/config` 报 `1146 ... agent_runtimes`（交接文档 §5.3 坑 2 的最后一处；之前只修了
   `_chat_config_row_engine`）。改走 runtime 具名查询（`runtimes_for_instance_scan*` /
   `runtime_credential_id|scope` / `runtime_instance_row`）。

第 1 条也解释了历史记录里「彩排 0 失败」与坑 2 并存的矛盾：上一轮 stage env 同样缺 lib 开关，
多数端点在内部 catch 了 1146，只有写探测把 `PUT /api/chat/config` 的异常记成了失败。

### IM 引擎装配的渠道表接口化（-12，本文件清零）
- 12 处跨域（5 张 mgmt 表：入站去重 4、入站消息 1、会话绑定 3、发送者身份 2、统一映射 1）下沉成
  mgmt public 的 9 个任意拓扑原语（`inbound_dedup_seen/claim/set_state/release`、
  `inbound_message_save_and_mark`、`chat_binding_get/upsert`、`sender_identity_user/bind`、
  `mapped_user_by_channel`）。
- **不复用 W2 那批渠道原语**：它们是 `db_registry.cursor("mgmt") + %s + NOW()`，实测在单库/sqlite
  拓扑下直接抛 `RuntimeError: 需要设置 GRAPE_DSN_MGMT`（只服务三库拓扑）。新一批走
  `_any_cursor/_any_tx`，单库回落进程库、三库走 mgmt DSN，方言可移植。
- 两处 sqlite 专有写法改成可移植两段式：绑定的 `ON CONFLICT(id) DO UPDATE`、消息落库后的
  `ON CONFLICT(...) DO UPDATE`（先 UPDATE 判 rowcount，未命中再 INSERT OR IGNORE）。
- **顺带修掉「连接归属」隐患**（调研报告点出、非计数项）：`_update_installation_config_sync` /
  `_lookup_installation_by_app_sync` 原先拿 IM 库的 `_connect()` 连接去查 mgmt 的安装表，
  三库独立后必然 1146。改为 `installation_*_any` 三个任意拓扑原语，连接由 owner 域选。
- 修一个改造中暴露的 NameError：`_bind_identity_sync` 的 except 分支还留着 `conn.rollback()`，
  而 conn 已随原语下沉消失。
- 全局 **373 → 361**（-12）；基线 **361 / 81 对**。
- 新增 `im_wiring_smoke`（5 组）：去重位 seen/首占/mark/释放后可重投、绑定首次插入与二次更新
  不增行、消息落库 + marked 同事务（重投不产生第二行）、身份解析优先级与渠道列白名单、
  绑定的 user 轨限制与幂等。

### scenes 场景页读路径接口化（-11，本文件清零）
- 11 处清零，全部指向运行域：`skill` 5、`agent_runtimes` 3、`devops_agent_session` 2、
  `runtime_credentials` 1（与 agent_runtimes 同一 JOIN）、`engine_capabilities` 1、`agent_tasks` 1。
- 技能下拉的可见范围（官方 + 本人 + 该仓）、关键词、范围过滤、分页统一进
  `scene_skill_search(...) -> (total, rows)`；调用方不再拼 `where_sql`。
- 引擎校验的三处读数收敛成 `runtime_with_credential`（引擎 + 凭据 scope/owner 一次取回）、
  `runtime_capability_match`（`tag='all'` 通配）、`runtime_status_healthy`、
  `devops_session_runtime_id`。
- **修掉一处存量 bug**：引擎健康与旧绑定回退两处都写成 `c = lib.read_one(...).fetchone()` ——
  对已取回的行再 `.fetchone()`（AttributeError 被吞），且把游标变量 `c` 覆盖成行，
  后续 `c.execute(...)` 直接失败 → 「离线引擎被当成健康」与「旧绑定读不到」两个静默错。
  改成原语后两个分支只有一条路径。
- 全局 **361 → 350**（-11）；基线 **350 / 80 对**。
- 新增 `scenes_smoke`（6 组）：可见范围（含停用行的原口径）、关键词/范围/分页、
  批量取技能与启用校验、引擎+凭据 LEFT JOIN（缺凭据时后两列为空）、能力标签通配与停用标签、
  健康位/会话绑定/任务归属。

### 租约存储 + 告警求值接口化（-22，两个文件清零）
- **`runtime/im/bot_lease.py`（11 处）**：渠道租约/安装表归 mgmt，SQL 下沉成 8 个原语
  （`lease_acquire_row` 的「惰性建行 + CAS 抢占」两句同事务、`lease_renew_expiry` 只推到期时间、
  `lease_renew_all_by_owner`、`lease_tokens_by_keys`、`lease_keys_valid`、`lease_owner_info`、
  `installation_status`、`installation_active_rows`）。
  顺带删掉 `_installation_active` 的「主连接查不到再试 wiring 连接」双路探测 —— 单库拓扑由
  owner 原语自己回落，三库拓扑直接走 mgmt DSN，两路探测没有存在意义。
- **`insight/observability/alerts.py`（11 处）**：告警求值的聚合全部下沉
  （`alerts_task_inflight` 排队/卡住、`alerts_task_terminal` 窗口内终态、`alerts_task_timeout`
  超时签名、`alerts_task_mutation` 两窗口突变、`alerts_task_rows_total`、`alerts_credential_heartbeats`、
  `credential_ids_visible` 三口径可见性），收件人两类走 mgmt public
  （`repo_member_user_ids` / `mapped_feishu_user_ids`）。
- 顺手修掉原代码里一个真 bug：收件人分支里 `rk` 路径只给 `_rows_ur` 赋值，末尾却统一读
  `_rows_um` —— 单仓库告警的收件人查询一直抛 NameError 被外层 except 吞成「无收件人」。
  改成两条原语后该分支自然消失。
- 全局 **395 → 373**（-22）；基线 **373 / 83 对**。
- 新增两份烟测：`lease_ops_smoke`（两实例互斥 / 续期凭 token 不凭 owner 的原语义 / 吊销安装拒锁 /
  释放后可抢 / 批量续期判定 / 无租约清单 / 过期回收）、`alerts_smoke`（卡住阈值边界 / 窗口内外 /
  超时签名命中与不命中 / 两窗口突变 / 心跳清单排除 revoked / 可见性三口径 / 收件人并集）。

### 并行调研的交叉核对（三份只读报告 vs 落地实现）
三份报告与实现方向一致（都是「按域拆成 owner 内单事务 + kernel 只编排」），核对后无返工，两处细节已确认：
- 报告提示 `session_token_set_expiry` 参数序可能反转 —— 核对：原 SQL 是 `SET expires_at=? WHERE session_key=?`，
  原语内部按 `(expires_at, session_key)` 绑定、对外签名是 `(session_key, expires_at)`，调用侧传 `(session_key, 值)`，
  三层都正确；`token_smoke` 的宽限期语义断言也覆盖了这条路径。
- 报告提示 `service.py` 的 `sync_task_session_token` 调用点未改 —— 该处依赖「先 rotate 再回写 env_extra」的顺序，
  已在调用点加注释固定这个约定（回写失败时新 token 已有效，下次 sync 再轮换即收敛）。
- 计数口径差异说明：报告的 21 / 15 条包含注释、docstring、以及两条**动态拼表名**的循环语句；静态判据
  （`foreign_table_refs.py`）只认字面量文本，所以本文件的账面是 16 / 14 / 15。动态语句已在 owner 域内
  用固定白名单循环实现（`REPO_KEY_TABLES`），不再由 kernel 拼表名。

### kernel 三件套：令牌 / 改名迁移 / 级联清理（-45，三个文件清零）
- **`auth_sdk/auth_token.py`（15 处）**：`session_api_tokens` / `api_tokens` 归 mgmt →
  12 个原语进 mgmt public（insert/by_sha/set_expiry/shrink_active/delete_expired/delete_by_session/
  renew/rotate + api_token 的 insert/by_sha/touch/revoke/list）。
  跨域后 `rotate` 的「同一事务」保证不再成立：改为「先落新 token（本域原子）→ 调用方再回写
  `agent_tasks.env_extra` 快照」，由调用方既有实例锁串行化、失败重试收敛；`commit` 参数保留以兼容
  既有调用方但不再控制事务边界（docstring 写明）。
- **`repo_migrate.py`（16 处）**：仓库改名拆成两段 —— mgmt 段（`managed_repos` 主键重建保留全部托管字段、
  `user_repos` 复合主键冲突删旧留新、4 张 repo_key 表）与 runtime 段（11 张 repo_key 表、
  `skill(scope='repo')` 的 owner_id 改写、`meta` 的 onboarded/cleanup_pending 前缀键）。各自本域一个事务；
  两段都是「删旧留新」语义 → 重复执行幂等；第二段失败时第一段已提交，审计里显式标 `mgmt_committed`。
- **`repo_cleanup.py`（14 处）**：级联清理拆成两段 `purge_repo_rows`（dry-run 只统计直删表，与原先口径一致）。
  子表先删（`task_messages` / `vod_conversations` / `chat_messages` / `scene_knowledge_ref`）在各自域内
  一条子查询完成；`meta` 的 5 处收敛成 set / delete_keys / keys_like 三个原语。
- **架构约束处理（R1 硬约束）**：kernel 顶层不得依赖 domains。`auth_token` 用延迟导入
  （`_tokens_lib()`）并在 `check-arch` 白名单登记「deferred token-table access; Phase 5 → contracts」；
  同时把三条**已清零**的 R8 白名单条目删除（该棘轮只减不增），避免存量登记与实际漂移。
- 全局 **440 → 395**（-45）；基线 **395 / 85 对**。
- 新增两份烟测：`token_smoke`（会话 token 生命周期 / 轮换后旧 token 失效且不碰已过期行 /
  接管 token 命名空间隔离与续期 / API Key 只存 sha256 且软吊销）、
  `migrate_cleanup_smoke`（14 张表迁移 + 托管字段保留 + 幂等 / dry-run 不删 / 级联实删 + 幂等）。

### 并行实施方式（本轮首次启用）
- 三个只读调研子代理并行（互不冲突）；**owner public 的追加仍是单点串行**（同一个文件，
  并行必冲突）；调用点替换按文件并行；验证与提交串行。回滚粒度 = 单文件 `git checkout`。

### mgmt skills 技能页接口化（-20，本文件清零）
- 20 处清零（19 条 SQL + 1 处 OpenAPI summary 文案命中正则，按判据口径改述）。
- **可见范围条件是本批核心**：`skill_search` 把 scope 分支（`all` / `official` / `repo` / `user` /
  默认「官方+个人」/ 带仓库）、组织隔离、keyword 三列 LIKE、agent_type、category、三种排序、
  分页全部收进 owner 域，返回 `(total, rows)`；mgmt 侧只剩「repo 参数为空时早退」这一条策略判断。
- `skill_agent_counts` 复用同一套 `_skill_scope_conditions`（连 agent_type 的展示排序 CASE 也在 owner 域）。
- 单行读取全部具名化：official 精确/全量、详情（by id 与 by name 两种优先级）、下载行
  （含 scope 可见性校验）、meta、path、repo 全列。
- 删除与同名解析：`skill_delete_by_id`、`skill_repo_id_by_name`、`skill_repo_count_by_name`、
  `skill_exists_active`。
- 全局 **460 → 440**（-20）；基线 **440 / 96 对**。
- 新增 `skills_smoke`（7 组断言）：五条可见范围分支 + 带仓库组合、keyword/agent_type/category
  过滤、hot/latest 排序、分页 total 与行数一致、agent 计数、official 精确与全量、下载行四条
  可见性（归属仓库/别的仓库/official/他人技能）、repo 同名 id 与计数、删除幂等、四个单行查询。
- 又一处自己引入的语义缺陷被烟测抓住：第一版 `_skill_scope_conditions` 把
  `scope in (None, "", "all")` 都当「不加 scope 条件」，而原语义只有 `"all"` 是全量、
  空 scope 是「官方+个人」——第一组断言即红，改回后七组全绿。

### mgmt agents 会话配置页接口化（-23，本文件清零）
- 23 处清零，全部指向 runtime 域：`agent_runtimes` 5、`runtime_credentials` 5、
  `devops_agent_session` 4、`vod_conversations` 3、`events` 3、`skill` 3、`agent_type` 2、
  `scene_engine_detach_log` 1。
- **动态 token 列**（provider 能力位给出列名）走组合原语 `agent_session_row(repo_keys,
  agent_type, token_field)`：列名在 owner 域按 `^[a-z_]+$` 校验后才拼进 SQL（与
  `managed_repos` 补列同一道护栏），非法列名直接 `ValueError`，列序与原 SQL 逐字一致。
- **事件检索合并成一次调用**：`events_search` 返回 `(total, rows)`，keyword 的 6 个 LIKE 条件
  与 `LIMIT/OFFSET` 都在 owner 域构造，调用方只给关键词与页码。
- **VOD 会话清理两条路径**都成原语：按会话 id 批量、按仓库子查询（`DELETE ... WHERE
  agent_session_id IN (SELECT ...)`，两表同域可一条 SQL 走完）。
- 同名技能解析收敛成 `skill_id_by_name_scope(name, scope, owners)`，owner 集合可选
  （原先「有 owner / 无 owner」两个分支各自一条 SQL）。
- 全局 **483 → 460**（-23）；基线 **460 / 96 对**。
- 新增 `agents_smoke`（7 组断言）：动态 token 列读取与非法列名拒绝、事件状态分布按
  agent_type 隔离、事件检索（seq 倒序/分页/关键词/无命中）、VOD 清理两条路径、
  同名技能（owner 集合命中/不在集合/不限 owner/`is_active=0` 排除）、解绑提示回绑幂等、
  `agent_type` 单行与全量。

### insight dashboard 看板聚合接口化（-24，本文件清零）
- 24 处清零，全部是「跨域只读聚合」，所以给的不是单表查询而是**聚合原语**：
  · runtime public `dashboard_*` 12 个：任务状态分布、issue 最新任务口径、stuck 窗口、
    failed 明细、按来源分组、今日分布、worker 三分支可见性、本机引擎、running 计数、
    平均耗时（sqlite `strftime` / MySQL `TIMESTAMPDIFF` 双分支）、引擎行 LEFT JOIN 凭据、
    凭据 scope/owner 与 name/device 单条；
  · mgmt public 3 个：账号名批量（`account_names_by_ids`）、`repo_key_in_candidates`、
    `pipeline_scene_advanced_options`。
- **过滤语义收进 owner 域**：`_dash_where(repo_keys, org_id)` 统一「repo_keys 走 IN、org_id 等值、
  两者可组合」，调用方只传数据不再拼 WHERE（原先 dashboard 里有 6 处 f-string 过滤片段）。
- 两处跨域 JOIN 一并下沉：`dashboard_issue_contrib`（issue 单表 + `is_subtask_mode=0` 口径）、
  `dashboard_task_issue_rows`（`agent_tasks ⋈ issue` 回链，分块规避 SQLite 变量上限）。
- 全局 **507 → 483**（-24）；基线 **483 / 98 对**。
- 新增 `dash_smoke`（8 组断言）：过滤组合（全量/按仓库/无命中）、issue 一 issue 一任务口径
  （#2 取最新 failed）、stuck 阈值与 limit、failed 倒序、worker 三种可见性（含 revoked 排除）、
  本机引擎、running 计数、平均耗时数值（7200s）、引擎行 LEFT JOIN、协作统计口径（子任务排除）、
  token 回链、mgmt 三个查找。
- 本批自己引入的一个缺陷被烟测立刻抓住：`_dash_where` 第一版追加条件漏了 `AND`，repo 过滤时
  SQL 语法错误——补上后八组全绿，也是「每批都配烟测」的直接收益。

### patrol 巡检全链路接口化（-25，本文件清零）
- `src/api/patrol.py` 的 25 处清零：`agent_tasks` 10、`grape_t_patrol_findings` 4、`meta` 4、
  `events` 3、`issue` 2、`agent_runtimes` 1、`managed_repos` 1、`user_repos` 1（另 1 处注释）。
- **主选举下沉为原语**：`claim_patrol_leader` 的四步 CAS（自续约 → 读现值 → CAS 接管 →
  引导期 INSERT）原本是 `db_lock` 里连发四条 SQL，三库独立后 `meta` 在 runtime 库、patrol 在
  api 层，必须整体下沉。`runtime.claim_meta_lease(key, holder, new_val, cutoff)` 在**同一事务**里
  完成四步（拆开会留出「两次 UPDATE 之间被接管」的窗口），`_meta_lease_expired` 一并搬过去。
- **findings 落库走 owner 域**：`patrol_findings_insert_many`（executemany 分块、单块失败吞掉、
  返回真实插入数）与 `patrol_findings_insert_one`、`patrol_findings_known`、
  `patrol_findings_delete_terminal`（保留期清理，status IN 动态列表）四个原语进 mgmt public。
- **读路径全部具名化**：issue status/stage、events 按类型取 created_at（动态 IN 走薄函数
  `event_created_ats_by_types`）、a2a 事件的 payload/status 与 created_at+agent_type、
  agent_tasks 的 event_seq/created_at/分页候选/批量 result_text、agent_runtimes 健康位；
  仓库归属 `managed_repo_host_user` / `repo_admin_user_id` 走 mgmt public。
- 全局 **532 → 507**（-25）；基线 **507 / 99 对**。
- 新增 `patrol_smoke`（6 组断言）：租约 CAS 五态（无行插入/他人新鲜让位/自续约/过期接管/
  刚接管不让）、`claim_patrol_leader` 三态、findings 去重+批量计数+已知集+保留期清理、
  巡检读路径、引擎健康位、仓库归属解析。

### db_setup 迁移/DDL 按归属域下沉（-33，本文件清零）
- `src/api/db_setup.py` 的 33 处清零。内联 DDL/DML 按归属下沉：
  · **runtime**：`agent_runtimes` 补列与重建、`events` 补 `completed_at`（两个版本）、
    `takeover_sessions` 补 host 三列、遗留 `health` 回填、`chat_agent_session` 重建；
  · **mgmt**：`organization` 文本重建的搬数据、`login_whitelist` 字面 `'NULL'` 清洗、
    `managed_repos` 补平台列与列读取、`unified_user_mapping.dingtalk_user_id` 加宽、
    `scene_market` 旧唯一索引删除；
  · **insight**：`api_latency_log` 保留期清理。
- 动态列名/表名一律走**owner 域函数返回 SQL**（不 import 常量属性——门禁要求），
  执行顺序仍由 db_setup 的迁移编排保证。
- 两处跨域关联子查询拆成原语：`org_id` 回填（`mgmt.repo_org_map` → 各域
  `backfill_org_id_by_repo`，表名固定白名单）与 `skill.category_id` 回填
  （`mgmt.skill_category_map` → `runtime.backfill_skill_category_id`）。
- 另有 12 处是注释/日志文案命中正则（不是 SQL），按判据口径改述。
- 全局 **565 → 532**（-33）；基线 **532 / 100 对**。
- 等价性硬证据：空 sqlite 跑 `init_database()` → **98 张表**（与 §5.2 记录完全一致），
  `agent_runtimes` 模型三列、`events.completed_at`、`takeover_sessions` host 三列均在。

### runtime public 自身去跨域（-32，本文件清零）
- `src/domains/runtime/public/__init__.py` 的 28 处外域引用清零，分四类处理：
  · **`grape_t_repo_credentials`（insight，10 处）**：本文件的 `list_repo_credentials` /
    `upsert_repo_credential` / `clear_repo_credential` 改为**转发** insight public
    （`list_repo_credential_rows` / `upsert_repo_credential_value`→返回 {action,id} /
    `clear_repo_credential_value(cred_type=...)`），SQL 只在 insight 一份。
  · **`grape_t_channel_lease`（mgmt，5 处）**：租约 CAS（抢占/续约/释放）搬到 mgmt public，
    runtime 三个函数改为转发。原先的 `_mgmt_channel_lib()` 分支其实一直无效——mgmt public
    里从来就没有这三个函数，`AttributeError` 被兜底吞掉后永远走本地 SQL；现在是真的走 owner 域。
  · **`event_action_filter`（mgmt，4 处）**：add/update/delete 搬到 mgmt public，runtime 转发。
  · **白名单里的跨域读（9 条）**：删掉无调用的 4 条（2 条 `grape_t_repo_credentials` 死条目、
    `repo_bot_token_value`、`account_id_by_platform_login`）与随调用方改造后的 4 条
    （两条 `autopilot_executions JOIN autopilot_tasks`、`event_action_filter_3d22a6`、
    `grape_user_id_by_external_id`）。跨域 JOIN 一律拆成「owner 原语 + 本域查询 + Python 内连接」。
- `ensure_issue_card` 的 `INSERT ... SELECT COALESCE((SELECT m.user_id ... EXISTS(账号表校验)))`
  是跨域子查询，改为「mgmt.mapped_user_id 解析 → 本域插入」；新增 mgmt 原语
  `mapped_user_id(login, um_column)` / `external_user_id(provider, external_id)`，两条都任意拓扑。
- 全局 **597 → 565**（-32）；基线收紧 **565 / 109 对**。
- 验证：`rds_gate --quick` GATE 全绿；`pytest arch+contracts+scripts` **158 passed**；
  四份 sqlite 烟测全通过（新增租约 CAS 8 断言：抢占/他人持有/同 token 重入/续约/释放/过期回收；
  过滤器 add/update/delete + 非法列拒绝；凭据转发的 inserted/updated 语义；建档 user_id 解析与禁用账号排除）。
- 已知口径（既有事实，非本轮引入）：`unified_user_mapping.user_id` 是 INTEGER 主键，而
  `grape_accounts.id` 是 UUID 字符串列；原 SQL 依赖 MySQL 隐式转换，`mapped_user_id` 保持同一比较方式，未改语义。

### settings 引擎管理台接口化（-31，本文件清零）
- `src/domains/mgmt/routes/settings.py` 的 31 处外域引用清零：`runtime_credentials` 12、
  `engine_capabilities` 9、`agent_runtimes` 6、`repo_im_group_binding` 2、
  `devops_agent_session` 1、`obs_llm_usage` 1（insight）。
- 前置改造：runtime public 的 17 个读写原语（凭据 CRUD / 能力 CRUD / agent_tags / 群绑定开关 /
  吊销 / 切 scope）从「只支持自有 DSN」改为**任意拓扑**（`_any_cursor`/`_any_tx` + 可移植 `?` 方言 +
  `datetime('now')`），单库拓扑回落进程库。这是本轮最有复用价值的一步：后续 skills/agents 批次
  可以直接调这些原语。
- 两个方言陷阱一并修掉：
  · `upsert_engine_capability` 原来是 MySQL 专用 `ON DUPLICATE KEY UPDATE ... VALUES(x)`，
    sqlite 下非法，调用点靠 `REPLACE INTO` 兜底——而 REPLACE 会把 `max_concurrent` 重置为 1。
    改为可移植两段式（UPDATE→rowcount==0→INSERT），覆盖同样三列、语义等价、不依赖方言。
  · `purge_runtime_credential` / `change_credential_scope` 改任意拓扑后，settings 的两处 fallback
    （吊销的 5 步展开、切 scope 的 3 步展开）各收敛为**一次原语调用**，事务边界回到 owner 域。
- insight public 补 `aggregate_llm_usage`（`obs_llm_usage` 聚合；按 Issue #865 只取模型名字段）。
- 全局 **628 → 597**（-31）；基线收紧 **597 / 110 对**。
- 验证：`rds_gate --quick` GATE 全绿；`pytest arch+contracts+scripts` **158 passed**；
  三份 sqlite 烟测全通过（会话/消息 8 组、user-db 6 组、public 原语 6 组——含凭据插入幂等冲突、
  能力 upsert 覆盖/删除计数、切 scope 的同名引擎合并、吊销的会话摘绑+级联删除、用量聚合）。

### mgmt/user/db 引擎/技能/凭据/场景接口化（-48，本文件清零）
- `src/domains/mgmt/user/db.py` 的 48 处外域引用**全部清零**：`agent_runtimes` 15、`skill` 10、
  `grape_t_repo_credentials` 10、`meta` 6、`engine_capabilities` 2、`runtime_credentials` 2（JOIN）、
  `scene_category` 2、`devops_agent_session` 1。
- **归属更正**：`grape_t_repo_credentials` 归 **insight**（不是 runtime，交接文档 §6 已点名）。
  insight public 原只有 W3 派生写，本次补上任意拓扑面（`_REPO_CRED_QUERIES` 8 条具名查询 +
  `repo_credential_value/meta`、`upsert_repo_credential_value`、`clear_repo_credential_value`、
  `any_repo_credential`、`repo_keys_sharing_value`、`repo_keys_with_nonempty_credential`）。
  原实现借 `runtime_lib` 读/写这张表，三库独立后会打到 runtime 库（1146/写错库），本轮一并纠正。
- `NOT EXISTS (SELECT 1 FROM grape_t_repo_credentials ...)` 关联子查询无法跨库执行，改为
  「owner 域取有凭据的仓库集合 → 本域行集过滤」，语义等价（空值视同无凭据）。
- `save_agent_runtimes` 的整段「探测即快照」（逐引擎 upsert + prune + dedupe）原先在 mgmt 侧用
  本域连接跑 8 条 SQL；现统一走 `runtime.public.upsert_local_runtimes`，该原语改用 `_any_tx()`
  与可移植方言（`?`），单库拓扑回落进程库 —— 事务边界不变（一次快照仍是一次事务）。
  `ensure_default_runtime` 同样改为任意拓扑原语。
- 全局 **676 → 628**（-48）；基线收紧 **628 / 112 对**。
- 验证：`rds_gate --quick` GATE 全绿；`pytest tests/arch tests/contracts tests/scripts` 158 passed；
  本地 sqlite 六组烟测全通过（引擎快照 upsert/prune/dedupe、默认引擎幂等、场景分类、技能列表、
  仓库凭据 upsert/读/批量判存）。
- 已知差异（如实）：`get_agent_runtimes_age` 用 `strptime("%Y-%m-%d %H:%M:%S")` 解析 `updated_at`，
  sqlite 下该列是 TEXT（ISO 串）会抛 ValueError；MySQL DATETIME 列会自动归一，属**改造前既有**行为，
  本轮未改语义（烟测跳过该断言）。

### session_manager 会话/消息/群绑定接口化（-56，本文件清零）
- `src/kernel/session_manager.py` 的 56 处外域引用**全部清零**：`chat_agent_session` /
  `chat_conversations` / `chat_messages` / `repo_im_group_binding` / `issue` / `agent_runtimes`
  改走 runtime public 的 33 条具名查询 + 6 个薄原语；`grape_accounts`（mgmt 归属）走 mgmt public
  新增的 `account_auth_row(username, conn=None)`（单库传调用方连接，三库走自有 DSN）。
- 建表/旧表迁移/兼容列补齐（14 处 DDL）改为 `ensure_chat_session_schema(conn)` /
  `ensure_repo_im_binding_schema(conn)`：DDL 仍落**调用方连接**（与 `ensure_user_mapping_schema`
  同口径），单库 sqlite 启动引导实测表结构与原 DDL 逐列一致（chat 14 列 / bind 11 列）。
- 多语句组合写保持原子性：`upsert_chat_config_row`（收敛重复行 + upsert）、`join_feishu_chat`
  （校验属主 + 清旧 + 设新）、`mark_issue_processing`（按会话优先、退化按仓库+序号）用 owner 域
  事务：自有 DSN 新开非 autocommit 连接，单库回落进程库 + `db_lock`（与原 kernel 语义一致）。
- `get_user_conversations` 删除本地兜底 SQL，改「任意拓扑」具名查询（handoff §5.3 第 3 条口径）。
- 全局 **732 → 676**（-56，含 1 处日志文案误报）；基线收紧 **676 / 113 对**（4 对随文件清零消失）。
- 验证：`rds_gate.sh --quick` → GATE 全绿（R1-R7 PASS、棘轮未上升、契约 37 条最新、看板已刷新）；
  `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；本地 sqlite 八组功能烟测全通过
  （会话 CRUD、群绑定开关/幂等 upsert、会话幂等创建、飞书指针原子切换、消息撤回、任务转
  processing、配置行收敛、会话删除）。
- **未验证项（如实）**：三实例彩排 `p4_p5_loop.sh` 本轮未执行——会话内没有 `SRC_PWD` /
  `REMOTE_PWD` 凭据环境变量（口令只走环境变量，不入仓库），需在具备凭据的会话补跑后才能声称
  「运行面 0 失败」。本批不把彩排当已通过。

### 渠道安装表 SQL 归位（-7）
- `runtime/im/engine_wiring.py` 里 7 条 `grape_t_channel_installation`（mgmt 归属）语句改为
  mgmt public 的 `installation_*` 助手（调用方仍传自己的连接，事务/关闭语义不变）。
  这也解释了上一轮那次回退：该文件同时服务 mgmt 的安装表与 runtime 的引擎表，
  「整条连接改指某域」必然打断另一侧，正确做法就是把 SQL 按归属域分开。
- 全局 **739 → 732**；基线收紧 732 / 117 对；门禁 158 passed；三实例彩排 LOOP_EXIT=0、运行面 0 失败。

### session_store 接口化（-31）
- `src/api/session_store.py`：issue / devops_agent_session / issue_agent_session / agent_type /
  agent_runtimes 等直接 SQL 全部改走 runtime public 具名查询（新增 20 条查询 + 6 个薄原语），
  该文件跨域 **39 → 3**，全局 **770 → 739**。
- 两处基线「上升」为归属重映射（同一批 SQL 从 api 消费改为 runtime 消费）：
  grape_accounts 14→16、unified_user_mapping 17→19，已按新归属落数并记录。
- 门禁：check_lib_query_names PASS；pytest arch/contracts/scripts **158 passed**；
  三实例彩排循环 LOOP_EXIT=0（P4 PASS / P5 PASS / 运行面 0 失败，170+ 端点含写场景）。

### 三批并行收口（-64）
- `kernel/session_manager.py`：unified_user_mapping 读写改走 mgmt public 原语（resolve_feishu_open_id /
  list_user_mappings / bind_feishu_mapping_row / ensure_unified_user_mapping_schema 等），该表引用 21 → 0；
  文件跨域 84 → **56**（余下 chat_agent_session 19 / chat_conversations 13 / repo_im_group_binding 11）。
- `mgmt/user/db.py`：skill 系列改走 runtime public 具名查询，skill 30 → **10**；文件 68 → **48**。
- `insight/routes/dashboard.py`：新增 19 个 runtime 聚合原语（events 指标、task 状态/卡住/失败分布、
  worker/credential JOIN 等），该文件 40 → **24**。
- 全局静态 **834 → 770**；棘轮基线收紧 770 / 121 对。
- 门禁：check_lib_query_names PASS（mgmt 16 funcs + 35 queries 全登记；为此把 DDL 常量改成
  `unified_user_mapping_ddl()` 访问函数，kernel 不再取常量属性）；静默 except 棘轮 0 新增（两处 `except: pass`
  已改为带日志）；pytest arch/contracts/scripts **158 passed**；三实例彩排循环 LOOP_EXIT=0、运行面 0 失败。

### owner 侧「任意拓扑」原语（-7）
- runtime public 新增 `_ANY_QUERIES` 具名查询白名单与 7 个原语（read_one_any / read_all_any / exec_any /
  get_meta_value_any / set_meta_value_any / set_meta_if_absent / list_featured_skills_any）：SQL 只存在 owner 域一份，
  自有 DSN 走 db_registry、单库拓扑回落进程库，调用方因此不需要再留本地兜底 SQL（兜底正是跨域统计的来源）。
- `mgmt/user/db.py` 的 get_meta_value / set_meta_value / migrate_oauth_providers / _sync_providers_from_config
  已改走新原语，该文件 75 → 68。
- 验证：pytest（arch/contracts/scripts）158 passed；相关域用例与改动前基线一致（11 failed/46 passed，同为既有失败）；
  三实例彩排循环 LOOP_EXIT=0、运行面 0 失败。

### issues_center 归位（-53）
- `src/api/issues_center.py`（3535 行）→ `src/domains/runtime/issues_center.py`，旧路径保留 sys.modules 别名；
  issue/event/agent_task 属运行域，逻辑回位。该文件跨域引用 **60 → 7**（余下是 mgmt 的
  unified_user_mapping / grape_accounts / managed_repos）。
- 棘轮出现 3 处「上升」实为归属重映射（同一批 SQL 从 api 消费改为 runtime 消费）：grape_accounts 12→14、
  managed_repos 13→15、unified_user_mapping 14→17，已在基线按新归属落数并在本节记录。
- 全局静态 **894 → 834**；check-arch PASS（R4 report-only 174→179，新增 5 条均为 runtime→mgmt 只读边）；
  pytest arch/contracts/scripts 158 passed。

### 迁移 SQL 按域拆分（-313）
- `src/api/db_setup.py` 的迁移 SQL 按归属域搬到 `src/domains/{mgmt,runtime,insight}/migrations_ddl.py`
  （工具 `scripts/split_migration_sql.py`：AST 定位字面量 + 精确文本替换，只搬「命中表全属同域」的；
  `migrations = [...]` 里连续同域段落折叠成 tuple 常量并原位 `*alias.CONST` 展开，**执行顺序不变**）。
- 静态跨域引用 **1207 → 894**（-313，db_setup 自身 346 → 33），棘轮基线收紧到 894 / 122 对。
- 等价性实测：空 sqlite 重建 98 张表、逐表列名与拆分前快照完全一致（0 缺失/0 多余/0 列差）；
  pytest（arch/contracts/scripts）158 passed；三实例彩排循环 LOOP_EXIT=0、运行面 0 失败。
- 未搬走：22 处（多域真跨域 1 处 `_backfill_skill_category_id`（skill×skill_category）+ 运行期拼名语句），
  留在 db_setup 待接口化，属下一批工作。

### 归位批次（-25）
- `src/api/pipeline_config.py` → `src/domains/mgmt/pipeline_config.py`（+ `src/api/` 同名 sys.modules 别名）：
  流水线配置读写的是 devops_pipeline_scene / scene_market（管理域表），放 api 层属错位。
- 静态跨域引用 **1232 → 1207**（-25），棘轮基线同步收紧到 1207；`tests/arch` 全绿。
- 新增 `scripts/rds_burndown.py --rank`：按「文件→跨域引用数」排序，直接给出下一批归位候选
  （当前 Top：db_setup 346 / kernel session_manager 84 / mgmt user/db 75 / issues_center 60 / insight dashboard 40）。

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

### kernel/gsb/registry 注册表 SQL 下沉 mgmt（-9，本文件清零）
- 文件：`src/kernel/gsb/registry.py`（258 行，GSB 服务注册表）。9 处跨域引用全部指向
  `gsb_service_registry`（**归 mgmt**，`table-owner.tsv` / `table_ownership.json` 一致），
  消费方是 kernel：`ALTER`(49) / `INSERT OR REPLACE`(87) / `UPDATE`(101, 240, 255) /
  `SELECT`(118, 142, 182, 218)。动态拼接站点 2 处（`list_instances` 按 include_offline 拼
  `AND status='online' AND last_heartbeat > ?`；`list_all_services` 拼 `WHERE status='online'`），
  其余为定值语句。
- 归位：mgmt public 新增 10 条可移植具名查询（`?` 占位符）+ 1 个 DDL 函数 + 9 个薄原语
  （`gsb_registry_ddl` / `ensure_gsb_registry_schema` / `gsb_register` / `gsb_heartbeat` /
  `gsb_discover_row` / `gsb_online_services` / `gsb_instances` / `gsb_all_services` /
  `gsb_mark_offline` / `gsb_purge_stale`），SQL 只在 mgmt 一份；kernel 侧只留 GSB 语义
  （心跳超时判定、`service`/`instance`/`age_seconds`/`alive` 派生、负载均衡排序口径）。
- **修的存量 bug**：改前各域进程直连「进程主库」写注册表——单库拓扑正常，三库拓扑下
  mgmt/runtime/insight 各写各的库，注册不报错、但跨域互相发现不到（静默失效，连 1146
  都看不到）。收口后任一路径（`/internal/gsb/services`、`router` 启动注册、`client`
  负载均衡）都读同一张 mgmt 表。
- 设计取舍：
  1) 走 owner public 原语**不**走 mgmt `/internal` 端点（R8 白名单备注原写「P3 走端点」）：
     注册是 30s 心跳的进程内高频动作，不该依赖 mgmt HTTP 可用性；单库/测试拓扑也没有
     任何 HTTP 跳。端点化留 Phase 5 contracts。
  2) 动态谓词拆成定值变体（`gsb_instances` / `gsb_instances_online`、
     `gsb_services_all` / `gsb_services_online_only`），调用方不能再传 where 片段；
     `service@%` 通配符值在 owner 侧拼（是参数值，不是表名/列名，无需白名单但也不外泄）。
  3) `INSERT OR REPLACE` 保留（`mysql_compat` 翻译成 `REPLACE INTO`），不引入
     `ON DUPLICATE KEY` 专有语法；DDL 的 `TEXT PRIMARY KEY` 由同一层翻成 `VARCHAR(191)`，
     `ADD COLUMN ... DEFAULT '1'` 由 ALTER 分支翻成表达式默认值。
  4) 行→dict 的转换收在 owner（依赖 `cursor.description`，属驱动差异）；列序与派生字段
     留在 kernel（GSB 语义），调用方按下标/键取值的写法未变。
  5) 无多语句组合写（register/heartbeat/offline/purge 全是单语句幂等写）→ 不需要
     `_any_tx()`，只 `_any_cursor`。
- 棘轮：`scripts/check-arch.py` 删掉已清零的 R8 条目
  `src/kernel/gsb/registry.py: {gsb_service_registry: 4}`（该棘轮只减不增）；R1 以精确
  target 形态登记延迟导入（`src.domains.mgmt(.public)`，注「due Phase 5 -> contracts」）。
  旧库补列那处 `except: pass` 改成 `log.debug`（静默 except 棘轮）。
- 验证：`py_compile` COMPILE_OK；SQLite 烟测 `~/burndown/gsb_registry_smoke.py`
  **ALL 71 CHECKS PASSED**（含 heartbeat 复活 offline 行、重新注册=上线、purge 不删行、
  `LIKE 'svc@%'` 不误伤 `svc2`/`svcx`、旧表无 version 列补列后仍可注册等边界与否定用例）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch PASS / 棘轮 OK / 契约 37 条 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**；
  `check_lib_query_names` PASS（mgmt 115 funcs + 19 queries 全登记）。GSB 相关内核用例
  67 passed，`tests/kernel/test_gsb_route.py::test_public_primitives_registry_sync` 为
  **既有红**（HEAD 干净树即 108 项 `PUBLIC_PRIMITIVES` 漂移，本轮新增的 10 个 public 函数
  也落在同一条断言里；该文件不在门禁三组内，未借机改它的白名单以免夹带）。
- 新基线：全局 **285 → 276**（-9），对 **70 → 69**；基线
  `foreign-refs-allowlist.csv` 收紧为 276 / 69；依赖看板与作业单同步刷新
  （下一批头名：`insight/observability/trace.py` 9 处）。

### runtime/im/channel_directory 飞书通道列 SQL 上移 mgmt（-9，本文件清零）
- 文件：`src/domains/runtime/im/channel_directory.py`（210 行，IM 通道目录、um 通道数据唯一访问层）。
  9 处跨域引用全部指向 `unified_user_mapping`（**归 mgmt**），消费方 runtime：`INSERT OR IGNORE`(73,273)、
  `UPDATE`(75,209,237,274)、`SELECT`(106,144,176)。表分布：`unified_user_mapping` × 9（单表）；
  动态拼接 1 处（`resolve_open_id_for_login` 用 `repo_key` 推平台列名拼 `WHERE {col}=?`，列名集合
  实际只有 `gitcode_user`/`github_login` 两值）。
- 归因：这批不是「老代码忘了走接口」，而是 **owner_lib 兜底的残留**——每个站点先走
  `mgmt_lib(foreign_only=True)` + cross_write 幂等意图，之后为单库拓扑保留了一段本地 SQL。
  lib 句柄只在「配了 mgmt 自有 DSN」时可用，兜底 SQL 却正是静态统计的来源；三库拓扑下进程库
  没有该表（无视图 → 1146），所以兜底侧既不该留、也不该跑。
- 归位：mgmt public 新增 9 条可移植具名查询（`um_insert_ignore_user` / `um_set_feishu_by_user` /
  `um_feishu_open_id_by_user` / `um_row_by_feishu_open_id` / `um_clear_feishu_by_app` /
  `um_clear_feishu_by_user` / `um_set_nickname_by_user` / `um_feishu_open_id_by_gitcode_user` /
  `um_feishu_open_id_by_github_login`）+ 7 个薄原语（`write_back_feishu_identity` /
  `feishu_open_id_by_user` / `user_mapping_row_by_open_id` / `feishu_open_id_by_platform_column` /
  `clear_feishu_by_app` / `clear_feishu_by_user` / `set_gitcode_nickname_any`）。
  列名白名单 `_UM_OPENID_COLUMN_QUERIES` 收在 owner（沿用 `_UM_COLUMN_QUERIES` 口径：未登记列返回空串）。
- 设计取舍：
  1) 写路径保持**双通道**：自有 DSN 下仍走 `mgmt_lib(foreign_only=True)` + `cross_write` 幂等意图
     （重试/死信语义不变），只有兜底侧换成 mgmt public 原语；单库/视图期 SQL 与三库期是同一份。
  2) 多语句写（建行 + 写列）用 `_any_tx()`：自有 DSN → `db_registry.transaction("mgmt")` 真事务；
     单库 → 进程库 + `db_lock` 且回滚，与原「同一 db_lock 内两条语句后 commit」等价。
  3) 方言：具名查询按 sqlite 方言书写（`?` / `INSERT OR IGNORE` / `datetime('now')`），
     由 `mysql_compat` 翻译；不引入 `NOW()`/`ON DUPLICATE KEY` 专有语法。
  4) `write_back` 的 `USER_ID>0` 是**双层**：调用方前置守卫 + owner 侧 UPDATE 的
     `user_id IS NOT NULL AND user_id != '' AND user_id != '0'`。后者照抄原 SQL，绕过调用方也写不进 0 行。
  5) `clear_for_app`/`clear_for_user` 的 `updated_at` 仍是 `datetime.now().isoformat()`
     （本地时间、非 UTC）——原口径，不在本批改成 UTC，避免与 W2 原语的时间基线混用。
  6) 反查/解析两个站点原来各有「lib 优先」与「db_lock 内再取 foreign lib」两层，属重复判定，
     合并成一次 `*_any` 调用（`_any_cursor` 已按拓扑自适应）。
- 修的存量 bug：`resolve_open_id_for_login` 的兜底分支在单库拓扑下走 `except Exception: return ""`
  吞掉一切 SQL 错误（含列名拼错、表缺失），排障时看不到原因；上移后 owner 侧 `log.debug` 记录
  失败查询名与异常，行为仍是返回空串（不改变对调用方的语义）。
- 棘轮：本文件跨域引用 **9 → 0**；`check-arch` R1 无需新白名单（runtime → `src.domains.mgmt.public`
  的函数内延迟导入是既有形态，且不新增顶层 import）。无静默 `except: pass` 新增。
- 验证：`py_compile` 两文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/channel_directory_smoke.py`
  **ALL 51 CHECKS PASSED**（覆盖 write_back 的 0 占位行双层守卫、只写三列不碰平台列/昵称、
  open_id 空串与未命中返回空、平台列白名单命中/未登记列返回空串/表缺失仍返回空串不抛、
  app 级只清该 app、user 级同名 `gitcode_user` 不误清（FR-21.10）、昵称建行+二次只更一行且不动
  open_id/app_id、S54 身份优先与 github 无 um 列跳过、调用方零 um SQL 动词等边界与否定用例）；
  `tests/runtime/im/test_s99_channel_directory.py` **8 passed**（S99 六类行为回归全绿）；
  `rds_gate.sh --quick` **GATE: 全绿**；`pytest tests/arch tests/contracts tests/scripts`
  **158 passed**；`make lint` **import OK**。
  `tests/mgmt/test_s110_um_boundary_guard.py::test_s110_um_channel_columns_boundary` 为**既有红**
  （HEAD 干净树即报 `issues_center.py` / `migrations_ddl.py` / `mgmt/public/__init__.py` 三个快照外
  触点；本轮未新增文件，`channel_directory` 本就在白名单内；该文件不在门禁三组内，未借机改其快照）。
- 新基线：全局 **276 → 267**（-9），对 **69 → 69**（同一对 mgmt→runtime 内消化，未跨对）；
  基线 `foreign-refs-allowlist.csv` 收紧为 267 / 69；依赖看板与作业单同步刷新
  （下一批头名：`insight/observability/trace.py` 9 处）。

### insight/observability/trace 任务轨迹取数下沉 runtime（-9，本文件清零）

- 文件：`src/domains/insight/observability/trace.py`（349 → 286 行，任务轨迹重建：任务 → 消息步骤 →
  引擎标注 → 流水线事件）。9 处跨域引用指向 **runtime** 的五张表：`agent_tasks` × 5
  （回填候选 80 / 逐行补写 107 / 任务号定位 245 / 候选集取行 255 / trace 直取 262）、
  `task_messages` × 1（129）、`agent_runtimes` × 1 与 `runtime_credentials` × 1（同一条 LEFT JOIN，184）、
  `events` × 1（322）。动态拼接 2 处（任务号入口的 `trace_id IN (…) OR pipeline_trace_id IN (…)`、
  事件按 `seq IN (…)`），其余为定值语句。`obs_log` / `obs_llm_usage` 属 insight 本域，继续留在原处。
- 归位：runtime public 新增 5 条可移植具名查询（`trace_tasks_unstamped` / `trace_task_stamp` /
  `trace_task_trace_ids` / `trace_task_messages` / `trace_runtime_label`，全部 `?` 占位符）+ 7 个薄原语
  （`trace_unstamped_tasks` / `stamp_task_traces_any` / `trace_tasks_for_trace` / `trace_tasks_for_task` /
  `trace_task_steps` / `trace_runtime_label` / `trace_events_by_seq`）。SQL 只在 runtime 一份；
  insight 侧只留确定性 trace id 生成、时间换算与可见性过滤（业务语义）。
- **修的存量 bug**：
  1) `resolve_tasks` 是**半截改造**：任务号入口先用 lib 具名查询定位 trace/pipeline id，紧接着的主查询
     却仍走进程主库游标裸 SQL。三库拓扑下定位走的是 runtime 自有连接、取行走的是 insight 进程库
     （无视图 → 1146），lib 只覆盖了一半，等于「点进来能看到 trace 存在、但取不回任务行」。
     改为整段走 owner 原语后两条路径同源。
  2) 事件段取 lib 句柄时漏了 `foreign_only=True`（本文件其余四处都带），单库拓扑下会拿到非空句柄、
     再往 owner 传 `select`/`where` 片段；与其它站点口径不一致且把 SQL 片段留在调用方。
     现在事件列序与谓词都在 owner，调用方只传 seq 列表。
  3) 回填兜底分支是 `except Exception: pass` + 末尾 `if done: db.commit()`：中途异常时已执行的语句
     可能不提交却被计进返回值（静默漏回填）。改为 owner 侧单事务 + `log.debug` 记录被跳过的任务号。
  4) `_engine_label` 的异常分支是裸 `except Exception: return f"#{id}"`，排障看不到原因；
     改为带 `log.debug`。
- 设计取舍：
  1) 拆掉「lib 优先 + 本地兜底 SQL」双路径：`_any_cursor` 已按拓扑自适应（自有 DSN → `db_registry`，
     未配 → 进程主库 + `db_lock`），单库与三库跑同一份语句，调用方不再有第二条读法。
  2) `agent_runtimes × runtime_credentials` 的 LEFT JOIN **不拆**：两张表都归 runtime，属域内 JOIN；
     拆成两跳只会把「无 runtime 行 → 无行」的语义在 Python 里重做一遍。
  3) 跨域 IN 列表在 owner 侧拼占位符并分块（500/段）；`trace_events_by_seq` 跨块后再整体按 seq 排序，
     保证与单条 `ORDER BY seq` 的输出顺序一致（分块不能改变语义）。
  4) 行 → dict 的转换收在 owner（列名元组即契约，列序照抄原 SQL）；原来靠 `cursor.description` 取名，
     属驱动差异，不该留在消费方。
  5) 回填写入口径由「每执行一行 +1」改为 `rowcount`：写一个不存在的任务号不再虚增计数，
     与既有的 `stamp_task_traces`（自有 DSN 版）口径统一。
  6) 回填写仍保留 `cross_write` 意图路径（P3-5g 的「整批一条意图」语义不变），只有单库兜底侧
     换成 owner 原语；两条路径的 SQL 与列序完全一致。
  7) 方言：`?` 占位符、`datetime('now')` 由 `mysql_compat` 翻译，未引入 `NOW()` / `ON DUPLICATE KEY`。
  8) 无多语句组合写需要「先读后写的原子性」——回填是「取候选集 → 逐条幂等 UPDATE」，
     单事务即可，不需要在调用方持锁跨语句。
- 棘轮：本文件跨域引用 **9 → 0**；`check-arch` R1 无需新白名单（insight → `src.domains.runtime.public`
  的延迟导入与 `dashboard.py` / `alerts.py` 既有形态一致），R8 条目不涉及（该棘轮只管 kernel 写域表）；
  静默 `except` 基线 `trace.py` 1 → 0（棘轮只减不增，属正常下调）。
- 验证：`py_compile` 两文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/trace_smoke.py`
  **ALL 81 CHECKS PASSED**（含：候选集只认 `trace_id IS NULL OR ''`、无 issue 行的 pipeline id 退回任务级、
  二次回填幂等、`limit` 生效、写不存在的任务号计 0、任务号入口在 trace 两列为空时仍按 id 命中、
  未知 trace/任务号返回空且不回落全表、引擎标注三级回退与 runtime 行缺失、设备名空串**不**回退
  （COALESCE 只吃 NULL）、事件跨 500 分块后仍整体有序、他人仓库 trace 不可见等边界与否定用例）；
  `tests/insight/test_obs_trace.py` **8 passed / 1 既有红**
  （`test_routes_trace_and_404_for_out_of_scope` 在 HEAD 干净树即因 TestClient 异步取消报
  `CancelledError`，与本次改动无关；该文件不在门禁三组内）；`rds_gate.sh --quick` **GATE: 全绿**
  （check-arch PASS / 棘轮 OK / 契约 37 条 / 看板最新）；`pytest tests/arch tests/contracts tests/scripts`
  **158 passed**；`make lint` **import OK**；`check_lib_query_names` **PASS**
  （runtime 122 funcs + 39 queries 全登记）。测试产物 `src/api/frontend/shell/openapi.json` 已还原。
- 新基线：全局 **267 → 258**（-9），对 **69 → 68**（insight → runtime 这一对归零）；
  基线 `foreign-refs-allowlist.csv` 收紧为 258 / 68；依赖看板与作业单同步刷新
  （下一批头名：`mgmt/routes/feedback.py` 8 处）。

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

### 7.3 mgmt 反馈路由读路径收口（2026-11-14 本批）

- 文件：`src/domains/mgmt/routes/feedback.py`（mgmt 域消费、8 处跨域 SQL → 0）。
  表分布（全部归 **runtime**，`docs/weekly-11/table-owner.tsv` 冻结）：
  `skill` 1 / `events` 1 / `issue` 1 / `agent_tasks` 2 / `devops_agent_session` 1 /
  `agent_runtimes` 1 / `runtime_credentials` 1（后者与 `agent_runtimes` 同属一条 LEFT JOIN）。
- owner 侧新增 7 条具名查询（`?` 占位符、列序照抄原 SQL）到 runtime `public._ANY_QUERIES`：
  `skill_resolve_preferred`（official→repo→其他 的排序 + `is_active=1`）、`event_feedback_row`、
  `issue_id_by_repo_number`、`task_latest_by_event`（`created_at DESC` 取一）、`task_brief_row`、
  `devops_session_runtime_id_by_repo_agent`（`runtime_id>0`）、`runtime_engine_attribution`。
- 设计取舍：
  1) 这 8 处原本是「`runtime_lib(foreign_only=True)` 有 DSN 就走 lib，否则本地兜底 SQL」的双路径，
     **兜底 SQL 才是跨域统计的来源**（交接文档 §5.3 坑 3）。改成任意拓扑具名查询后，
     自有 DSN 走 `db_registry`、单库/测试回落进程主库 + `db_lock`，调用方只剩一条读法。
  2) `agent_runtimes × runtime_credentials` 的 LEFT JOIN **不拆**：两张表都归 runtime，属域内 JOIN；
     拆两跳要把「凭据行缺失 → 空串」的 COALESCE 语义在 Python 里重做一遍，反而稀释契约。
  3) 未新增薄函数包装：站点只有一个调用方、无参数化需求，直接 `read_one_any(name, params)`
     比再套一层 `def` 更少维护面；`check_lib_query_names` 已在 `_ANY_QUERIES` 注册面满足。
  4) 删掉 `_task_via_lib()`（`GRAPE_TASKS_VIA_LIB` 灰度开关的历史入口），它指向的
     `latest_task_for_event` / `task_brief` 是 `%s` 方言函数，单库/sqlite 下不可用；
     开关默认关闭，删除不影响任何现网路径。
  5) `db_lock` / `db` 仍保留（`user_feedback` 写读是 mgmt 本域表），只在跨域站点上去掉多余的外层锁
     （`_any_cursor` 回落路径自带 `db_lock`，自有 DSN 路径是 autocommit）。
- 修的存量 bug：
  1) 三库拓扑下未开 `GRAPE_TASKS_VIA_LIB` 时，反馈上下文查的是进程主库 → `1146`，
     整段 `except` 后 task/engine 全空（功能静默降级）；现按 owner 域读，两条拓扑同结果。
  2) 四处吞异常（技能解析、卡片 id、引擎绑定、token 上下文）中三处是 `except Exception: pass`，
     排障只能靠猜；统一改为带 `log.debug` 的显式处理（静默 except 棘轮只减不增）。
  3) `_resolve_skill` 的 lib 分支用 `%s` 方言、兜底分支用 `?`，同一语义两套写法；
     现只有 `?` 一套，sqlite 冒烟即可覆盖。
- 验证：`py_compile` 两文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/feedback_smoke.py`
  **ALL 30 CHECKS PASSED**（含边界与否定用例：official 优先于 repo、停用的 official 不参与、
  技能不存在返回原名、同事件多任务取最新、`issue_id` 无数字 → issue_number 空、卡片缺失不影响其余上下文、
  摘要回退 `events.result` 并截断 500、任务无 `runtime_id` 时按会话绑定回退且**忽略 `runtime_id=0`**、
  `events.runtime` 优先于 `provider`、token 仓库优先于任务仓库、任务不存在保留 token 字段、
  源码断言调用点 0 条跨域 SQL）；`rds_gate.sh --quick` **GATE: 全绿**（check-arch PASS / 棘轮 OK /
  契约 37 条 / 看板最新）；`pytest tests/arch tests/contracts tests/scripts` **158 passed**；
  `make lint` **import OK**。
- 新基线：全局 **258 → 250**（-8），对 **68 → 67**（`events → mgmt` 这一对归零；
  其余 6 张表在 mgmt 侧仍有别的文件引用，故只减计数不减对）；
  基线 `foreign-refs-allowlist.csv` 收紧为 250 / 67；依赖看板与作业单同步刷新
  （下一批头名：`mgmt/admin/routes.py` 8 处）。

---

### 7.4 mgmt admin 路由跨域读写收口（2026-11-15 本批）

- 文件：`src/domains/mgmt/admin/routes.py`（mgmt 域消费、8 处跨域 SQL → 0）。
  表分布（`docs/weekly-11/table-owner.tsv` 冻结）：
  `meta`（归 runtime）7 处——行 961 vcs 平台卡、975 bot token、987 webhook token、
  1066 oauth 渠道卡读、1074 渠道卡写、1078 client_id、1081 client_secret；
  `obs_metric_rollup`（归 insight）1 处——行 751 `scope=all` 的维度枚举。
- owner 侧新增（列序照抄原 SQL、`?` 占位符、可移植方言）：
  1) runtime public 两个薄函数：`set_meta_values_any(items)`（批量 upsert，`_any_tx()`
     单事务）、`merge_meta_json_any(key, patch, base, extra)`（读-合并-写同事务）；
     复用已登记的 `meta_value` / `meta_insert_absent` / `meta_upsert_update` 三条具名查询，
     未新增 SQL 字符串。
  2) insight public 新增查询表 `_ROLLUP_QUERIES`（`rollup_dim_keys`：
     `SELECT DISTINCT dim_key FROM obs_metric_rollup WHERE dim_key != ?`）与同名薄函数；
     `_any_sql()` 改为「repo 凭据表 → 留存表」两表联合查找，未登记名仍抛 `KeyError`。
- 设计取舍：
  1) **为何要薄函数而不是直接 `exec_any` × N**：这两个站点都是多键组合写——vcs 保存写
     「平台卡 + N 个 bot token + M 个 webhook token」，渠道保存写「渠道卡 + N 个
     client_id + M 个 client_secret」。逐个 `exec_any` 会让每个键各自提交，中途 400
     就留半份配置；`_any_tx()` 把整批收进一个事务（自有 DSN 真事务，单库回落
     `db_lock` + 显式 rollback）。
  2) **为何 upsert 用「INSERT OR IGNORE → UPDATE」而非 `INSERT OR REPLACE`**：
     REPLACE 是删-插语义（meta 以后增列会被静默重置）；而「先 UPDATE、rowcount==0
     再 INSERT」在 MySQL 下遇到同值重写会误判行不存在而撞主键。先补行后改值两条
     拓扑都安全，同值重写幂等。
  3) **读-合并-写必须在一个事务里**：原实现 `with db_lock: SELECT → merge → 多写` 在
     单进程内是原子的，但 `db_lock` 只保护进程内、且自有 DSN 拓扑下根本没走到这段；
     把「读存量 + 合并 + 落库」搬进 owner 域的同一个事务，语义跨拓扑一致，也顺手
     消掉「读后写之间被并发保存覆盖」的丢渠道窗口。
  4) **`obs_metric_rollup` 只加一条具名查询**，没有把 insight 的 `metric_rollup` 模块
     整体暴露：管理台只需要维度键，暴露面越小越好；`_all_rollup_dims()` 保留原
     `try/except → []` 的降级口径（看板维度取不到不该 500）。
  5) 列序/口径逐字照抄：维度枚举仍是 `dim_key != 'global'` 且**保留空串维度**；凭据
     通道仍是「留空=不改」（空值不落库、不会清空存量密文），只是这条判断从调用点
     挪进 owner 原语的 `extra` 过滤，避免第二个调用方漏判。
- 修的存量 bug：
  1) `vcs_platforms_put` 边写边校验：`bot_tokens` / `webhook_tokens` 含不在册平台时抛
     400，但此时 `vcs_providers_config` 已经写进未提交事务，release `db_lock` 后既没
     commit 也没 rollback，会被下一次任意 `db.commit()` 顺带落库（隐性部分写）。
     现改为「先整批校验、再一次性事务写入」，400 时零写入。
  2) `auth_channels_put` 的存量 JSON 解析是 `except Exception: pass`（静默）：现在解析
     失败降级为「无存量」并留 `log.debug`，且解析结果非 dict（历史脏值可能是数组）
     也按无存量处理，不再靠 `dict.update` 抛错兜底。
  3) 同一函数把「同值重写」在 MySQL 下会撞主键的 upsert 形态（UPDATE 后 rowcount=0
     再 INSERT）换成先补行后改值，重放保存不再产生 IntegrityError 噪声。
  4) 顺手清掉本文件另一处静默 except（恢复账号的通知邮件失败 `except: pass` → 带
     `log.debug`），`tests/silent_except_baseline.json` 里本文件的 1 条基线随之删除，
     `EXEMPT_FILES` 里「旧渠道卡存量 JSON 解析防御」豁免名额一并撤销（棘轮只减不增）。
- 验证：`py_compile` 三文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/admin_meta_smoke.py`
  **ALL 58 CHECKS PASSED**（含边界与否定用例：空批次 0 写、空 key 跳过、空串值/None 值
  不撞 NOT NULL、同批同键后者胜、同值重写幂等、批量写中途失败整批回滚、非法 JSON 与
  数组存量降级、空凭据不清空存量密文、二次保存不挤掉 gitcode/github、空表维度枚举、
  自定义排除值、归属域抛错时 helper 回 `[]`、调用点 0 条 SQL 的源码断言、未登记查询名
  fail-closed）；`rds_gate.sh --quick` **GATE: 全绿**（check-arch PASS / 棘轮 OK /
  契约 37 条 / 看板最新）；`pytest tests/arch tests/contracts tests/scripts`
  **158 passed**；`make lint` **import OK**。
- 新基线：全局 **250 → 242**（-8），对 **67 → 66**（`obs_metric_rollup → mgmt` 归零；
  `meta → mgmt` 11 → 4，另 4 处在 `mgmt/user/db.py` 等文件，故只减计数不减对）；
  基线 `foreign-refs-allowlist.csv` 收紧为 242 / 66；依赖看板与作业单同步刷新
  （下一批头名：`runtime/routes/im.py` 7 处）。

---

### 7.5 runtime IM 路由渠道安装表收口（2026-11-15 本批）

- 文件：`src/domains/runtime/routes/im.py`（runtime 域消费、7 处跨域 SQL → 0）。
  表分布（`docs/weekly-11/table-owner.tsv` 冻结）：`grape_t_channel_installation`（归 mgmt）7 处——
  行 118 状态分组计数、159 安装列表八列、234 启停前归属判定、246 启停写 status、
  271 钉钉安装详情五列、363 写 config + 指纹、372 钉钉活跃安装 config 枚举。
- owner 侧新增（`?` 占位符、列序/排序逐字照抄原语句、写语句用可移植 `datetime('now')`）：
  1) `_ANY_QUERIES` 补 7 条具名查询：`installation_status_counts`、`installation_list_all`、
     `installation_owner_by_id`、`installation_detail_by_id`、`installation_update_status`、
     `installation_update_config_fp`、`installation_active_configs_by_type`。
  2) 同名薄函数 7 个（`*_any`，不接收拼接 SQL 也不接收外部连接）：列表类走 `read_all_any`、
     单行类走 `read_one_any`、写类走 `exec_any`。
- 设计取舍：
  1) **为什么不沿用 `installation_*(conn)` 家族**（那是 `3921a54c6` 给 `im/engine_wiring` 铺的）：
     SQL 搬进归属域只解决「语句在哪」，不解决「连接连哪个库」。`engine_wiring._connect()` 钉的是
     IM 侧库（`GRAPE_IM_DB_NAME` 优先，dev/prod 为运行域），三库独立后拿它读 mgmt 表读到的是
     **副本**——不报 1146，是静默数据分裂，比报错更难发现。故本文件 5 个站点全部改走不带 `conn`
     的 `*_any`，路由不再自持连接（`_db_conn()` 随之删除）。
  2) **读-判权-写仍是两次原语调用**：与原实现同形（同一连接先 SELECT 再 UPDATE，两步之间本来
     就没有事务边界），没有引入新的窗口；写原语自带提交（自有 DSN autocommit / 单库回落
     `db_lock` + commit），因此去掉外层 `conn.commit()`。若将来要把「判权 + 改状态」做成原子的，
     应在 owner 侧加组合原语，而不是在调用方套事务。
  3) **config 与 `config_fingerprint` 用同一条 UPDATE**：指纹必须跟着配置走，拆两条会出现
     「配置变了指纹没变」的中间态（引擎按指纹判断是否重建渠道）。
  4) **探活先于写保持不变**：模板探活失败仍是 400 且零写入，只是落库动作改由归属域提交；
     空 `template_id` 的「只关启用、不清模板 ID」口径逐字保留。
  5) **`WHERE channel_type='dingtalk'` 改参数化**：渠道类型由调用方传、语句在归属域，
     口径不变（仍只统计 `status='active'`），避免下个渠道再复制一条几乎相同的语句。
- 修的存量 bug：
  1) **连接归属（功能面）**：三库拓扑下 `/api/im/status`、`/api/im/installations`、启停、
     钉钉卡片读的安装表都来自 IM 侧库，而 mgmt 侧同表另有数据——两侧都不报错，前端看到的
     「安装数/状态」与 mgmt 真相不一致。现按 owner 域读，两条拓扑同结果。
  2) **本文件是 `mgmt/routes/im.py` 的陈旧分叉**（`arch-weekly-11-ljt` 合并 weekly-11 时
     新路由落在 mgmt，runtime 侧这份没删）：它没有被任何进程挂载（`grape_server` / `main` 都
     从 `mgmt.routes` 取），缺 #2261 的 outbox 段、`_engine()` 还在 `sys.modules` 里猜入口，
     而 `tests/runtime/im/test_im_routes.py` 只 import 它——**四个用例守护的是死代码**。
     本批只做 SQL 归位（不删文件：删它要同步重指用例与 `_engine` 取法，属独立一批），
     已列为后续动作。
  3) **测试夹具只重定向了 `engine_wiring` 的库路径**：归位后「写入 tmp 库、读进程主库」两边
     看不到彼此（4 个用例红）。夹具补钉 `src.kernel.session_manager.db` 到同一临时库——
     这本来就是 `_any_cursor` 回落路径的连接，属口径对齐而非迁就实现。
- 验证：`py_compile` 三文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/im_routes_smoke.py`
  **ALL 76 CHECKS PASSED**（含边界与否定用例：空表三类计数 0、列表**不过滤 status**、
  owner 为空串对所有人可见、repo 轨「传 repo 精确匹配 / 不传逐行问权限且无权限即隐藏」、
  secret 掩码与坏 JSON/数组配置降级、启停 404/403/400 三条否定路径零写入、
  状态值 `' DISABLED '` 归一化、`updated_at` 由 `datetime('now')` 刷新且写已提交、
  repo 轨无权限时权限异常原样透出、钉钉空模板只关启用不清模板、探活失败零写入、
  探活成功落指纹、全局开关「还有别的 active 安装启用着就不能关」、坏 JSON / revoked /
  非钉钉渠道的启用都不计入、7 条具名查询已登记且未登记名 `KeyError`、
  **有自有 DSN 时 SQL 走 `db_registry.cursor('mgmt')` 不碰进程库**、源码 0 条跨域 SQL）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch PASS / 棘轮 OK / 契约 37 条 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**；
  `pytest tests/runtime/im` 同组 **10 passed**（HEAD worktree 基线同组 1 failed
  `test_enable_disable_writes_audit` 为既有，与本次改动无关）。
- 新基线：全局 **242 → 235**（-7），对 **66 → 66**（该表在 runtime 侧另 9 处由
  `im/engine/installations.py` 1、`im/handler_async.py` 2、`im/notify.py` 1、
  `im/vod_handler_async.py` 2、`routes/chat.py` 3 承担，故只减计数不减对）；
  基线 `foreign-refs-allowlist.csv` 收紧为 235 / 66；依赖看板与作业单同步刷新
  （下一批头名：`mgmt/routes/autopilot.py` / `runtime/issues_center.py` /
  `runtime/routes/events.py` 各 7 处）。
- 遗留：`engine_wiring.get_active_installation` / `sync_installation` / `revoke_installation`
  仍把自己的 `_connect()` 连接传给 `installation_*(conn)`——同一张表的写方向仍钉在 IM 侧库，
  属 §"连接归属" 的下一批（与 `routes/chat.py`、`im/handler_async.py` 一起改）。

---

### 7.6 runtime 事件路由账户名与知识清理收口（2026-11-15 本批）

- 文件：`src/domains/runtime/routes/events.py`（runtime 域消费、7 处跨域 SQL → 0）。
  表分布（`docs/weekly-11/table-owner.tsv` 冻结）：
  - `grape_accounts`（归 mgmt）3 处，行 875 / 1009 / 1224：重派/重派队列/重派重读三处
    「引擎候选 scope 过滤」里的 `rc.owner_name = (SELECT username FROM grape_accounts WHERE id=?)`；
  - `agent_shared_knowledge`（归 mgmt）3 处，行 2035 / 2071 / 2077：obs_key 枚举、
    knowledge_id 枚举、按作用域删主表；
  - `scene_knowledge_ref`（归 mgmt）1 处，行 2075：按 knowledge_id 删关联。
- owner 侧新增（`?` 占位符、列序/谓词逐字照抄原语句、写语句不含 SQL 专有语法）：
  1) `_ANY_QUERIES` 补 4 条具名查询：`knowledge_obs_keys_scoped`、`knowledge_ids_scoped_portable`、
     `knowledge_delete_scoped`、`account_username_by_id`；
  2) 4 个薄函数（`*_any` 风格，不接收拼接 SQL）：`knowledge_obs_keys_scoped`、
     `knowledge_ids_scoped`、`delete_event_knowledge_any`、`account_username_by_id_any`。
     前三个走 `read_all_any`/`_any_tx`；`delete_event_knowledge_any` 在 `_any_tx()` 里
     「先删 ref 再删主表」，ref 的 IN 列表按 400 分块（超过 MySQL/sqlite 参数上限前先切开）。
- 调用方改造：
  1) 三处重复的 scope 子句收敛成模块内 `_engine_scope_extra(repo_bare, account_id)`：
     先由 mgmt public 解析 username 成标量，再作为参数参与 `rc.owner_name = ?` 比对，
     runtime 语句里不再出现外域表；`account_id` 为空/账户不存在/username 为空 → 不生成
     user 轨分支（原 SQL 的 `= NULL` 恒不成立，两端口径一致）。
  2) 知识删除的「mgmt_lib 优先 + 本地兜底 SQL」兜底侧整体换成
     `delete_event_knowledge_any`；obs_key 枚举同样改走 owner 域原语（此前是
     `mgmt_lib` 优先 + 兜底 SELECT）。
- 设计取舍：
  1) **只拆子查询、不拆外层语句**：外层 SQL（`agent_runtimes` × `engine_capabilities` ×
     `runtime_credentials`）三张表全归 runtime，动的只有夹在 scope 子句里的 mgmt 标量子查询，
     所以是「本域取标量 + 参数化比对」，不是跨域 JOIN 拆解；比较仍在 SQL 里按列排序规则做，
     大小写口径与原语句逐字一致。
  2) **兜底侧上移而不是删兜底**：原代码留兜底 SQL 的动机是「单库拓扑不装 lib」，而兜底 SQL
     正是跨域统计来源。新原语走 `_any_cursor/_any_tx`（自有 DSN → mgmt 连接；单库 → 进程库
     + `db_lock`），两条拓扑同一份语句，调用方不再需要兜底。
  3) **删除顺序与返回口径照抄**：`delete_event_knowledge_any` 与原 `delete_event_knowledge`
     （自有 DSN / `%s` 版）返回同样的 `knowledge / refs_deleted / rows_deleted`，且都是
     「先 ref 后主表」；原先兜底路径的 `for kid in rows: DELETE ... WHERE knowledge_id=?`
     逐条删改为分块 IN，语义等价、往返次数下降。
  4) **issue_num 不强制 int**：`_issue_num_from` 返回字符串，gitee 的 issue 号可能是非数字串；
     原兜底路径直接绑定原值，新原语同样按原值绑定（跨域写 payload 里的 `int(_in)` 是 lib 路径
     的既有约束，不在本批改动范围）。
- 修的存量 bug：
  1) **连接归属（功能面）**：三库拓扑下事件删除原先只在 `mgmt_lib` 可用时走 mgmt 库；一旦
     lib 不可用（单库/视图期/`foreign_only` 拒绝）就落回进程库——runtime 进程库里
     `agent_shared_knowledge` 是另一份数据，删的是自己的副本，mgmt 侧真身不动（不报错、
     静默数据分裂）。现统一由归属域原语落库，两条拓扑同一结果。
  2) **OBS 清理一直是死分支（未修，仅记录）**：`from src.kernel.storage import
     get_storage` 指向的模块已随模块归位消失（现为 `src/domains/runtime/storage`），
     ImportError 被外层 `except Exception: pass` 吞掉——OBS 对象从未被删过。本批只把该分支里的
     SQL 归位，**不**顺手改 import：改对等于打开一条从未跑过的删除链路（OBS 侧生命周期策略
     需产品确认），已列为后续独立动作。
- 验证：`py_compile` 两文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/runtime_events_smoke.py`
  **ALL 60 CHECKS PASSED**（含边界与否定用例：账户名未知 id / None / 空串 / username 为
  NULL 或空 → 空串；scope 三轨命中集合与 `credential_id>0`、online+healthy、离线行排除、
  `ORDER BY busy_slots ASC LIMIT 1` 选中忙闲最少者；obs_key 只取非空非 NULL 且按
  repo/scope/agent/issue 精确作用域、`issue_num` 传字符串 '42' 亦命中；删除三计数、
  重复删全 0、850 条触发 400 分块、别的 scope/agent/仓库不动；未登记查询名 `KeyError`
  且 `_READ_QUERIES` 的名字不走 `_ANY_QUERIES`；路由 400/403 零写入、storage 可用时只删
  非空 obs_key 且文件与 DB 一并清、storage 缺失时 fail-open 但 DB 清理照旧、issue_agent
  只带 agent_type、project 级 issue_num 恒 0 不受 issue_id 影响；源码 0 条三表 SQL 引用）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch PASS / 棘轮 OK / 契约 37 条 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**；
  `check_lib_query_names` **PASS**（mgmt 129 函数 + 16 具名查询在用，全部登记）。
  `pytest tests/runtime/test_event_conversation.py` 同组 **10 failed / 20 passed**，
  HEAD worktree 同组 **11 failed / 19 passed**（失败集合逐条比对：本树是 HEAD 的子集，
  多出的一条 `test_queue_task_packs_event_dialogue_skill` 为顺序敏感抖动；抽查
  `test_redispatch_resolves_runtime_and_new_cwd` 失败原因是既有断言 `cwd` 未走新结构，
  与 SQL 归属无关）。
- 三实例彩排（`LOOP_MAX=1 bash scripts/p4_p5_loop.sh`，`REMOTE_USER=rds_admin`）：
  P4 **RESULT: PASS**（mgmt 54 / insight 12 / runtime 36 基表，视图 0，账号仅本库授权）；
  P5 **RESULT: PASS**（A1 三库视图 0、A2 三账号各 2 条授权且跨库 0、A6 死信队列无表）；
  全 API 冒烟 **0 失败**；轮末 **RESULT: 环境侧全绿**（静态余量 228 只反映代码面 A3，
  与环境侧验收分开）。
  环境侧前置修复（不属于代码改动）：本机 3311/3312 两实例的 socket 文件此前被外部清理
  unlink + `~/rds-local/{mgmt,insight}/files` 目录被删，导致 P4 首轮 `(2003) Can't connect
  ... localhost`；`mkdir` 两处 files 后按 §5.4 重启两实例（`BOTH_UP`）即恢复。远端 116:3307
  的管理账号是 `rds_admin`（用 `root` 会 1045：来源地址被拒）。
- 新基线：全局 **235 → 228**（-7），对 **66 → 66**（三张表在 runtime 侧余量：
  `agent_shared_knowledge` 8 → 5、`grape_accounts` 8 → 5、`scene_knowledge_ref` 2 → 1，
  均由 `issues_center.py`、`pipeline/*` 等文件继续承担，故只减计数不减对）；
  基线 `foreign-refs-allowlist.csv` 收紧为 228 / 66；依赖看板与作业单同步刷新
  （下一批头名：`mgmt/routes/autopilot.py` / `runtime/issues_center.py` 各 7 处）。

---

### 7.7 需求中心外域读收口（2026-11-15 本批）

- 文件：`src/domains/runtime/issues_center.py`（runtime 域消费、7 处跨域 SQL → 0）。
  表分布（`docs/weekly-11/table-owner.tsv` 冻结）：
  - `unified_user_mapping`（归 mgmt）3 处，行 111 / 117 / 195：`_get_user_id_by_gitcode`
    的两跳平台列查找（优先「登录账号存在且启用」→ 任意映射行）与
    `_get_gitcode_user_by_user_id` 的平台登录名兜底（`COALESCE(NULLIF(gitcode_user,''),
    NULLIF(github_login,''))`）；
  - `grape_accounts`（归 mgmt）2 处，行 113（上条优先链的 `EXISTS ... ga.status=1` 子查询）、
    行 229（`_ensure_assignee_task_card` 的 created_by 回填 `SELECT username ... WHERE id=?`）；
  - `managed_repos`（归 mgmt）2 处，行 792（`_find_github_mirror_repo_key` 单键存在性）、
    行 3282（`_mirror_repo_keys` 动态 IN 交集 + `ORDER BY repo_key`）。
- owner 侧新增（`?` 占位符、列序/谓词/排序逐字照抄；写语句本批为 0 条）：
  1) `_ANY_QUERIES` 补 4 条具名查询：`um_account_user_id_by_gitcode_user`、
     `um_account_user_id_by_github_login`（带 EXISTS 校验的优先链两变体）、
     `um_platform_login_by_user`（登录名兜底）、`repo_key_row`（仓库键存在性）；
  2) 白名单映射 `_UM_ACCOUNT_COLUMN_QUERIES`（平台列 → 优先链具名查询），
     与既有 `_UM_COLUMN_QUERIES` 并列——动态列名仍只由归属域挑选；
  3) 4 个薄函数：`mapping_user_id_preferring_account(column, value)`、
     `platform_login_by_user_id_any(user_id)`、`repo_key_exists_any(repo_key)`、
     `repo_keys_existing_any(repo_keys)`（IN 列表按 400 分块，块内 `ORDER BY repo_key`）。
- 设计取舍：
  1) **优先链两跳留在归属域**：`EXISTS (grape_accounts ga.status=1)` 是 mgmt 本域子查询，
     拆到调用方只会变成第二条跨域语句（`grape_accounts` 引用减不下去），所以整条
     优先链做成两条具名查询，调用方只拿到标量 user_id；原「先优先、无行再兜底」
     的两次往返顺序与 `LIMIT 1` 语义不变。
  2) **动态列名不下放**：`um_column_for_repo` 推出的列名照旧由 kernel 工具给出，
     值通过 owner 侧白名单映射取具名查询；未登记列抛 `KeyError`，被调用方原有的
     `except` 捕获后仍返回 None/空串（与原「拼出空列名 → SQL 语法错 → 返回 0」口径一致）。
  3) **IN 交集仍由归属域执行**：`repo_keys_existing_any` 在 owner 侧拼占位符（与控制点
     一样只拼 `?`，不拼值），400 分块只影响 >400 候选的极端场景；候选集来自平台卡前缀，
     实际规模远小于分块上限，单块即与原 `IN (...) ORDER BY repo_key` 逐字一致。
  4) **`db_lock` 保留**：`_get_user_id_by_gitcode` 的原实现在 `with db_lock` 下查询，
     本批保留该锁（自有 DSN 拓扑下是多余串行，但保持原调用语义不变；单库回落路径
     `_any_cursor` 用的正是同一把 RLock，重入安全）。
- 修的存量 bug：
  1) **连接归属（功能面）**：三库拓扑下 `_get_user_id_by_gitcode` 的 um 兜底、
     `_get_gitcode_user_by_user_id` 的登录名兜底、created_by 回填、仓库镜像链全读
     runtime 进程库——那里面没有三张 mgmt 表（1146，均被内部 `except` 吞成
     None/空串/空表），或存在同表副本时静默按副本判定，导致 `issue.user_id` 落 0、
     `created_by` 退化成 assignee、多平台镜像关单漏关。现统一走 mgmt 归属域原语。
  2) **静默 except（卫生面）**：created_by 回填原来的 `except Exception: _cb = ""`
     改为带 `log.debug` 的显式处理，与静默 except 棘轮口径一致。
- 验证：`py_compile` 两文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/issues_center_smoke.py`
  **ALL 68 CHECKS PASSED**（含边界与否定用例：优先链只在 `status=1` 的账号上命中、
  `status=0` 行被 EXISTS 过滤后由兜底取低 id、`user_id=0` 行两跳都不命中、
  未登记列 `KeyError`、登录名 gitcode 优先 / 空则 github / 两列 NULL 为空串 /
  空 id 与 `'0'` 不查库、仓库键命中返回库里原值 / 未登记空串 / 空候选不查库、
  500 候选跨 400 分块不丢结果且块内有序、未登记查询名 `KeyError`、新语句不含
  `%s`/`NOW()`/`ON DUPLICATE`、`_ensure_assignee_task_card` 未知 id/空 id/空 username
  三种回退 assignee 且复用卡不覆盖 created_by、镜像集除自身且有序、
  **有自有 DSN 时 SQL 走 `db_registry.cursor('mgmt')` 不碰进程库**、源码 0 条三表 SQL）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch PASS / 棘轮 OK / 契约 37 条 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **228 → 221**（-7），对 **66 → 66**（三张表在 runtime 侧仍有其它消费点，
  故只减计数不减对）；基线 `foreign-refs-allowlist.csv` 收紧为 221 / 66；依赖看板与作业单
  同步刷新（下一批头名：`mgmt/routes/autopilot.py` 7 处）。

---

### 7.8 巡检任务执行记录外域读收口（2026-11-15 本批）

- 文件：`src/domains/mgmt/routes/autopilot.py`（mgmt 域消费、7 处跨域 SQL → 0）。
  表分布（`docs/weekly-11/table-owner.tsv` 冻结）：
  - `autopilot_executions`（归 runtime）5 处，行 448 / 505 / 506 / 596 / 703：
    删除执行记录、执行列表分页（主查询 + COUNT）、单行读 `findings_json, result`、
    回写 `findings_json`；
  - `agent_runtimes`（归 runtime）1 处，行 117：`engine_runtime_ids` 归属校验的
    `id IN (...)` 批量取 `credential_id`；
  - `skill`（归 runtime）1 处，行 983：技能显示名（`is_active=1 AND scope='official'`）。
- owner 侧新增（`?` 占位符、列序/谓词/排序逐字照抄）：
  1) `_ANY_QUERIES` 补 4 条具名查询：`autopilot_executions_delete_by_task`、
     `autopilot_findings_set`、`autopilot_execution_findings_result`、
     `skill_display_name_official`；
  2) 5 个薄函数：`runtimes_credential_ids_any(ids)`（IN 列表按 400 分块，返回
     `{id: credential_id}`，NULL → 0）、`delete_autopilot_executions_by_task_any`、
     `set_autopilot_findings_any`、`autopilot_execution_findings_result_any`、
     `skill_display_name_official_any`（空名不查库）；
  3) `list_autopilot_executions` 由 MySQL 方言（`%s` + `db_registry.cursor`）改为
     **可移植方言**（`?` + `_any_cursor`），并新增两张白名单：`_AUTOPILOT_EXEC_FILTERS`
     （过滤谓词）与 `_AUTOPILOT_EXEC_COMPAT_COLS`（迁移兼容列的「有列表达式/无列空串占位」
     两态）。名与返回结构不变，mgmt 侧不再需要 else 分支。
- 调用方改造：
  1) `list_executions` 的「lib 优先 + 本地兜底 SQL」双分支整体收敛为一次
     `_rt_ap.list_autopilot_executions(...)`；`_autopilot_exec_cols()`（在 mgmt 连接上
     PRAGMA 探测运行域表）随之删除；
  2) `delete_task` 换成「跨域删除（cross_write 优先、原语兜底）→ 本域删任务行」，
     跨域那一步移出本域 `db_lock`；
  3) `get_findings` / `_save_findings_json` / `_skill_display_name` / 引擎校验
     全部只调 public，不再有 lib/兜底分支与本地游标。
- 设计取舍：
  1) **分页查询整体上移，而不是逐条照抄 8 条具名查询**：可变部分只有「过滤组合」和
     「兼容列存在性」两项，二者都做成归属域白名单（谓词只拼 `?`、列表达式只从白名单里挑），
     值一律绑定。这样 4 种过滤组合 × 2 种兼容态不必展开成 16 条静态 SQL，且列名永不来自调用方。
  2) **兼容列探测用游标描述符（`SELECT * … LIMIT 0`）而不是 PRAGMA**：`db_registry` 的薄
     兼容游标只做 `?`→`%s` 等方言翻译，**不做 PRAGMA 特判**（只有 `mysql_compat.Cursor`
     有），直发 PRAGMA 在 MySQL 上 1064——本轮彩排首跑就撞了这条（见下「验证」）。
  3) **lib 分支不再保留**：`list_autopilot_executions` 改成可移植方言后，单库与自有 DSN
     两条拓扑跑同一份语句，mgmt 侧留着 lib 分支只会多一条永不测试的路径。
  4) **跨域删除先于本域删除**：两者跨库本来就不在一个事务里（原实现同样是 lib else 本地），
     顺序照原样保留；区别只是不再被 mgmt 全局锁串行化。
- 修的存量 bug：
  1) **`get_findings` 在 lib 可用时必 500（功能面）**：原括号错位写成
     `c = _ru_lib.read_one(...)` 后接 `).fetchone()`，`row` 在该分支未定义 → `NameError`；
     只有「无自有 DSN 的兜底分支」能跑，即三库拓扑下这个端点从来没成功过。现两条拓扑同一路径。
  2) **执行列表的关单号恒为空（功能面）**：兼容列探测原在 mgmt 连接上跑 PRAGMA，三库拓扑下
     探测结果恒为空集，于是即使运行库里有 `issue_number` 列也一律渲染空串，历史执行记录
     的 Issue 号前端全丢；现由归属域连接探测。
  3) **静默 except（卫生面）**：`_skill_display_name` 的 `except Exception: return ""`
     改为带 `log.debug` 的显式处理。
- 验证：`py_compile` 两文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/autopilot_smoke.py`
  **ALL 108 CHECKS PASSED**（含边界与否定用例：13 列同序、`ORDER BY id DESC`、
  task/status/双过滤与空串 status 不过滤、limit 夹 [1,200] / offset 负数归 0 / 越界空表但
  total 不变、缺 `issue_number` 列时空串占位且 ALTER 补列后立即读真值、
  `agent_task_id` 读真值、单行读列序与未知 id → None、回写只影响指定 id / 未知 id 0 行、
  删除只删本 task / 二次 0 行、credential_id NULL → 0 / 未知 id 不出现 / 空列表与 None 零 SQL /
  450 个 id 跨 400 分块、技能名下架与非官方与空 display_name 均空串、4 条新具名查询无
  `%s`/`NOW()`/`ON DUPLICATE`、未登记名 KeyError、自有 DSN 时 SQL 全走
  `db_registry.cursor('runtime')`、消费侧 5 个入口行为逐条对照、源码 0 条三表 SQL）；
  `rds_gate.sh --quick` **GATE: 全绿**；`pytest tests/arch tests/contracts tests/scripts`
  **158 passed**；`make lint` **import OK**；`check_lib_query_names` **PASS**
  （runtime 128 函数 + 32 具名查询在用，全部登记）。
  三实例彩排（`LOOP_MAX=1 bash scripts/p4_p5_loop.sh`，`REMOTE_USER=rds_admin`）：
  首跑 **1 失败**——`GET /api/autopilot/executions` 报
  `(1064 … near 'PRAGMA table_info(autopilot_executions)')`，即上条设计取舍 2）的坑；
  改成描述符探测后复跑 P4 **RESULT: PASS**、P5 **RESULT: PASS**（A1 视图 0 / A2 授权仅本库 /
  A6 无队列），全 API 冒烟 **0 失败**，轮末 **RESULT: 环境侧全绿**（静态 214 只反映代码面 A3）。
- 新基线：全局 **221 → 214**（-7），对 **66 → 64**（`autopilot_executions` 在 runtime 侧还有
  `scheduler.py` 等消费点、`agent_runtimes` 仍有 `devstage.py` 等，故只减计数不减对的部分）；
  基线 `foreign-refs-allowlist.csv` 收紧为 214 / 64；依赖看板与作业单同步刷新
  （下一批头名：`kernel/token_resolver.py` 6 处 / `kernel/outbound_ledger.py` 6 处）。

---

### 7.9 令牌解析器外域读写收口（2026-11-15 本批）

- 文件：`src/kernel/token_resolver.py`（kernel 域消费、6 处跨域 SQL → 0）。
  表分布（`docs/weekly-11/table-owner.tsv` 冻结）：
  - `managed_repos`（归 mgmt）5 处，行 794 / 830 / 850 / 867 / 1275：
    `RepoBotToken._fetch` 的 legacy 列回落读（动态列 `{field}` + `IS NOT NULL` + `!=''`）、
    `_is_expired` 的状态列单行读、`mark_expired` / `clear_expired` 的两条状态列 UPDATE、
    `RepoBotTokenHandler.is_self_loop_actor` 的 `bot_username` 单行读；
  - `external_identities`（归 mgmt）1 处，行 977：`UserToken._fetch_stored` 的
    `access_token` 读（`status IN (1, 4)`）。
- owner 侧新增（`?` 占位符、列序/谓词逐字照抄）：
  1) `_ANY_QUERIES` 补 5 条具名查询：`repo_bot_token_legacy_gitcode`、
     `repo_bot_token_legacy_github`（两条物理列的 legacy 回落）、`repo_bot_token_status`、
     `repo_bot_token_status_set`、`ei_stored_access_token`；
  2) 白名单 `_REPO_TOKEN_COLUMN_QUERIES`（legacy token 列 → 具名查询），与既有
     `_UM_COLUMN_QUERIES` 并列——动态列名仍只由归属域挑选；
  3) 5 个薄函数：`repo_bot_token_from_legacy_column_any(repo_key, column)`、
     `repo_bot_token_status_any(repo_key)`、`set_repo_bot_token_status_any(repo_key, status)`、
     `stored_access_token_any(grape_user_id, provider)`，以及**复用既有**
     `repo_bot_username_any(repo_key)`（见取舍 2）。
- 设计取舍：
  1) **两条状态写合并成一条参数化 UPDATE**：原 `mark_expired` / `clear_expired` 是两条
     字面量语句，唯一可变的是状态值（`'expired'` / `''`）。合并后谓词
     （`WHERE repo_key=?`）、rowcount 语义（未托管仓库 → 0 行）与 commit 时机都不变，
     语义仍由两个名字清晰的薄函数承载，具名查询少一条。
  2) **`bot_username` 不另立具名查询**：原语句是无非空过滤的 `SELECT bot_username ...
     LIMIT 1`，但取到值后仍判真值（`if row and row[0]`）再比较，空串与 NULL 都落到
     「不拦截」——与既有 `repo_bot_username`（带 `bot_username!=''`）在本调用点完全等价，
     故复用 `repo_bot_username_any` 并在 docstring 写明等价性（避免近重复查询堆积）。
  3) **动态 token 列不下放**：`{field}` 来自平台卡的 `token_storage_field`，物理列只有
     `bot_gitcode_token` / `bot_github_token`（gitee/gitlab 平台卡显式置空，调用方在进 SQL
     前已返回）。列名由归属域白名单挑选，未登记列抛 `KeyError`，被调用方原有
     `except` 捕获后返回空串——与原「拼出不存在的列 → SQL 报错 → 空串」同口径，
     没有把列名拼接能力交给调用方。
  4) **解密留在调用方**：owner 只交出库里密文原值（`managed_repos` 列与
     `external_identities.access_token` 同走通道加密），`_decrypt_repo_secret` / `decrypt`
     的导入与调用仍在 kernel 函数内，读侧密钥口径与审计口径不变。
  5) **锁语义保持**：单库回落路径 `_any_cursor` 内部持同一把 `db_lock`（RLock 可重入），
     自有 DSN 拓扑走 autocommit 连接——与原「进程库游标 + `db_lock` + `db.commit()`」
     在单库下等价；三库下不再打进程库。
- 修的存量 bug：
  1) **连接归属（功能面）**：三库拓扑下这 6 条语句全部打在进程主库上，而 `managed_repos`
     与 `external_identities` 只有 mgmt 库有表（视图 0）→ 1146 被内部 `except` 吞成
     「无凭据 / 未过期 / 非自环」：仓库级 bot token 恒不可用（策略链静默降级到全局 bot
     或用户 token，运维只看到「token 解析失败」）、过期标记写不进真实库、
     `read_stored_token` 恒空（A2A 鉴权与 session 写入拿不到原始值）、回帖链自环判定失效
     （机器人可能回复自己）。同表副本存在时更糟：静默按副本判定，过期标记落到副本。
     现统一走 mgmt 归属域原语（自有 DSN → mgmt 连接，单库 → 进程库，同一份语句）。
  2) **静默 except（卫生面）**：`_is_expired`、`is_self_loop_actor` 的
     `except Exception: pass` 改为带 `log.debug` 的显式处理（与静默 except 棘轮口径一致，
     基线未上调）。
  3) **R8 棘轮只减不增**：`managed_repos` 的两条 UPDATE 下沉后 kernel 侧写清零，
     删掉 `scripts/check-arch.py` 里 `"src/kernel/token_resolver.py": {"managed_repos": 2}`
     条目，改为「已清零」注释。
- 验证：`py_compile` 两文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/token_resolver_smoke.py`
  **ALL 92 CHECKS PASSED**（含边界与否定用例：legacy 列命中 gitcode/github 两列 / 空串 /
  NULL / 无行 → 空串、未登记列 `bot_gitlab_token` → 空串不抛、平台卡无物理列 → 不查库、
  路径 B 跳过、rc 命中优先且完全不查 `managed_repos`、命中时仍打 historical-hit 观测日志、
  `'expired'` 才判过期而 `''`/NULL/无行/缺表 → False、get_token 过期短路、
  `mark/clear_expired` 写后另一连接可见（已提交）、未知 repo → 0 行不抛、
  ei `status IN (1, 4)` 命中而 0/2 不命中、空/NULL token → 空串、provider 不串号、
  host_user_id 空/'0' → 不查库、actor 等值/他人/空串/NULL/无行/空 repo_key → 非自环、
  缺表全部降级不抛、5 条新语句无 `%s`/`NOW()`/`ON DUPLICATE`、未登记查询名与未登记列
  `KeyError`、语句文本逐字比对、**有自有 DSN 时 SQL 走 `db_registry.cursor('mgmt')`
  不碰进程库**、源码 0 条两表 SQL 且 5 个原语全部接上）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch PASS / 棘轮 OK / 契约 37 条 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **214 → 208**（-6），对 **64 → 62**（`managed_repos←kernel` 5 → 0、
  `external_identities←kernel` 1 → 0）；基线 `foreign-refs-allowlist.csv` 收紧为 208 / 62；
  依赖看板与作业单同步刷新（下一批头名：`kernel/outbound_ledger.py` 6 处）。

---

### 7.10 代发台账外域读写收口（2026-11-15 本批）

- 文件：`src/kernel/outbound_ledger.py`（kernel 域消费、6 处跨域 SQL → 0）。
  表分布（`docs/weekly-11/table-owner.tsv` 冻结）：
  - `outbound_content_ledger`（归 mgmt）6 处，行 137 / 166 / 224 / 234 / 240 / 249：
    `_cleanup_expired` 的保留期 DELETE、`record_outbound` 的 INSERT OR IGNORE、
    `is_our_outbound` 的 4 条读（issue_edit 指纹双命中 / issue_close·issue_meta 时效窗口 /
    精确 ID / 指纹旁路核对）。全部为静态 SQL + `?` 占位符，无动态拼表名。
  同批还下沉了原 `_ensure_table` 里的 **DDL 与索引引导**（`CREATE TABLE IF NOT EXISTS`
  不计入静态统计，但同样只该打在归属库上）。
- owner 侧新增（`?` 占位符，列序/谓词逐字照抄）：
  1) `_ANY_QUERIES` 补 6 条具名查询：`outbound_ledger_insert`、`outbound_ledger_purge_before`、
     `outbound_ledger_exists_edit`、`outbound_ledger_exists_within`、`outbound_ledger_exists_id`、
     `outbound_ledger_row_by_hash`；
  2) `ensure_outbound_ledger_table()` 承载建表 + 唯一索引/指纹索引 + 旧 3 列索引迁移，
     另留 `outbound_ledger_ddl()` 只读快照（DDL 走函数，不 import 常量属性）；
  3) 7 个薄函数：`record_outbound_ledger`、`purge_outbound_ledger_before`、
     `outbound_ledger_has_edit` / `_has_within` / `_has_id`、`outbound_ledger_row_by_hash`。
- 设计取舍：
  1) **DDL 与 6 条 DML 一起下沉，而不是只搬 DML**：原 `_ensure_table` 把建表、唯一索引、
     `information_schema` 3 列索引迁移、指纹索引全打在进程库上；只搬读写会留下
     「在 A 库建表、在 B 库读写」的错位，三库拓扑下第一次记账就 1146。
  2) **「一次性建表」缓存留在 kernel，不搬进 owner**：原审查 D6 优化针对的是热路径上
     每条 webhook 事件 3-5 次带 `db_lock` 的 DDL 往返，而「同一连接」这个身份只有调用方
     （进程主库句柄）知道——owner 端每次幂等执行、调用方按连接对象缓存，语义与性能意图都保留。
  3) **指纹旁路核对返回原始行而不是 content_id 字符串**：原实现是 `if _row:` 命中后取
     `_row[0]` 打告警，空 content_id 的行也会触发；返回字符串会把空串当「无行」，
     静默吞掉一条漂移告警。
  4) **截止值仍由调用方在 Python 侧算出传参**：保留期 30 天、`issue_close`/`issue_meta`
     1800s 时效窗口两个口径原样；SQL 里不出现 `NOW()` / `datetime('now','-N days')` 修饰符方言。
  5) **重复记账靠 `INSERT OR IGNORE`，不换 `ON DUPLICATE KEY UPDATE`**：后者会覆盖首次
     记账的指纹/actor，破坏「第一次是谁发的」这一语义；`INSERT OR IGNORE` 由方言桥翻译成
     `INSERT IGNORE`，幂等且不改原行。
- 修的存量 bug：
  1) **三库拓扑下整条台账防线失效（功能面/安全面）**：6 条语句与 DDL 全部打在进程主库，
     而 `outbound_content_ledger` 只有 mgmt 库有表（视图 0）→ 1146 被内部 `except` 吞掉：
     写侧恒 `False`（记账只剩进程内 TTL 集合，多进程 / 重启 / 超过 300s 的回声全部漏拦），
     读侧恒「放行」（我们自己的代发回声被当用户消息重新开工，S11 的核心价值归零），
     运维只能看到一条 warning。现读写都走 mgmt 归属域原语，两条拓扑同一份语句。
  2) **同表副本存在时静默读写副本（数据面）**：单库回落路径建表/记账/清理都落在副本上，
     读也读副本——过期标记与命中判定与实际库不一致。现统一按归属域连接。
  3) **静默 except（卫生面）**：原 `_ensure_table` 的 3 处 `except Exception: pass` 随 DDL
     下沉改为带 `log.debug` 的显式处理；kernel 侧吞噬点清零（`silent_except_baseline.json`
     中该文件 3 处的计数已不触顶，基线未上调）。
  4) **R8 棘轮只减不增**：`scripts/check-arch.py` 的
     `"src/kernel/outbound_ledger.py": {"outbound_content_ledger": 1}` 条目删除，改为「已清零」注释；
     kernel 延迟导入 `mgmt.public` 按 R1 精确登记（targets 只列 `src.domains.mgmt[.public]`）。
- 验证：`py_compile` 三文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/outbound_ledger_smoke.py`
  **ALL 125 CHECKS PASSED**（含边界与否定用例：4 列唯一索引列序、DDL 二次幂等、
  sqlite 下 `information_schema` 探测降级不抛、同键重复记账 1 行且不改原行、同 ID 跨仓
  记 2 行、`purge` 只删 `< cutoff` 且返回行数、edit 需 ID+指纹双命中（指纹/编号/仓库/类型
  四种不匹配全 False）、`created_at` 恰等于 cutoff 算窗口内 / 早 1 秒不算 / 空串不算、
  精确 ID 命中与类型/仓库隔离、指纹旁路 `None` 三态、kernel 写侧四条前置校验全不查库、
  无独立 ID 时 issue_number 兼值入账、落库失败仍靠 TTL 拦住竞态回声、issue_edit 不吃 TTL、
  meta 记录 1 小时前放行、查询异常一律放行、同一连接只引导一次而换连接后重跑、
  清理每日一次节流且只删 30 天前、6 条新语句无 `%s`/`NOW()`/`ON DUPLICATE`/`datetime('now'`、
  未登记查询名 KeyError、**有自有 DSN 时 SQL 走 `db_registry.cursor('mgmt')` 不碰进程库**、
  源码 0 条台账表 SQL 且 7 个原语全部接上、R8 条目已删）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 PASS / 棘轮 OK / 契约 37 条 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；
  该模块自带回归 `tests/runtime/test_issue_453_outbound_ledger.py` **69 passed**
  （含 S11/S12-3/S13/S14/S16/S17/S19 全族，仍全部走 mock 内存库，说明单库拓扑零回归）；
  `make lint` **import OK**；`check_lib_query_names` **PASS**（mgmt 137 函数 + 16 具名查询在用，
  全部登记）。
- 新基线：全局 **208 → 202**（-6），对 **62 → 61**（`outbound_content_ledger←kernel` 6 → 0）；
  基线 `foreign-refs-allowlist.csv` 收紧为 202 / 61；依赖看板与作业单同步刷新
  （下一批头名：`runtime/routes/webhook.py` 6 处 / `runtime/routes/chat.py` 6 处）。

---

### 7.11 runtime webhook/A2A 托管仓库与流水线场景收口（2026-11-15 本批）

- 文件：`src/domains/runtime/routes/webhook.py`（runtime 域），**6 处 → 0**（单轮做完，未分两轮）。
- 表分布（全部归 mgmt）：
  - `managed_repos` × 4：原行 90（删仓前取 owner/repo 名）、841（无前缀 repository 反查托管前缀）、
    1687（A2A 回退补录最小行）、1803（A2A 真伪快判是否存在）；
  - `devops_pipeline_scene` × 2：原行 913（关联仓信任域扫全仓场景 linked_repos）、
    1029（#568 目标场景在用性快判）。
- owner 侧新增（`?` 占位符，列序/谓词逐字照抄）：
  1) `_ANY_QUERIES` 补 5 条：`repo_owner_name`、`repo_registered_flag`、`repo_insert_ignore`、
     `scene_linked_repos_by_repo`、`scene_id_active_by_repo_scene`；
  2) 5 个薄函数：`repo_owner_name_any`、`repo_registered_any`、`ensure_repo_row_any`、
     `scene_linked_repos_rows_any`、`scene_id_exists_any`；
  3) 841 处复用既有 `repo_key_in_candidates`（github/gitcode 双候选，无 ORDER BY 的库序与
     改前 `IN (?, ?)` + `fetchone` 逐字一致——烟测用原语句直接对拍，不假设插入序）。
- 设计取舍：
  1) **删掉「lib 命中 + 本地兜底 SQL」双分支**：原 90/1029 两处是
     `mgmt_lib(foreign_only=True)` 命中走 lib、否则打本地 `SELECT` 的写法。兜底分支正是跨域
     统计来源，且它在单库彩排里一直“绿”，掩盖了三库拓扑下 runtime 进程本地库根本没有这两张表
     （无视图 → 1146）。统一到任意拓扑原语（自有 DSN → mgmt 连接；单库 → 进程主库 + db_lock），
     SQL 只在 mgmt 留一份，调用方不再需要兜底。
  2) **复制 `repo_key_row` 而不复用**：`repo_registered_any` 单独登记 `SELECT 1 … LIMIT 1`。
     `repo_key_exists_any` 对空键短路返回空串，而原语句是「空键也照查表」的纯存在性判定；
     两者只在空键行存在时分歧，为保证逐条等价不合并（烟测把这条写成了显式用例）。
  3) **写原语而不是本地 `_reg_db.commit()`**：`ensure_repo_row_any` 走 `exec_any` 的
     `_any_cursor(write=True)`，三库拓扑下写进 mgmt 库，单库拓扑仍提交进程库；`INSERT OR IGNORE`
     由 `mysql_compat` 译为 `INSERT IGNORE`，两条后端 rowcount 同为 1=新建 / 0=已存在。
  4) **场景两条读不合并**：913 要「该仓**全部在用场景**」（多行、无 LIMIT），与既有单场景
     `scene_linked_repos`（scene_id + LIMIT 1）谓词不同；合并会漏扫场景，A2A 关联仓闭环会误拒。
- 修的存量 bug：
  1) `_authorize_a2a` 检查 2、`_resolve_a2a_repo_key`、A2A 真伪校验原来在 **runtime 进程**里直读
     `managed_repos` / `devops_pipeline_scene`，三库拆分后是 1146 被 `except` 吞成「不信任 /
     未登记」——表现为关联仓信任域与托管快路径**静默失效**（不是统计问题）。本批改为 owner 原语，
     两条拓扑都命中。
  2) `_a2a_repo_fallback` 的兜底分支在 lib 失败时用进程库游标 `commit()`：三库下会把补录写进
     runtime 库（写入成功但没人读得到，下次仍走可达性回查）。改为 mgmt 写原语；日志措辞
     「fallback to local SQL」同步改成「portable primitive」，避免继续误导。
  3) 删掉 `_handle_repo_deleted` 里已无用途的 `from src.kernel.session_manager import db as _db, db_lock`
     （游标只剩兜底那一处，搬走后就是死导入）。
- 验证：`py_compile` 两文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/webhook_a2a_smoke.py`
  **ALL 87 CHECKS PASSED**（含边界与否定用例：双候选命中时与原语句逐字对拍、空候选不拼空 IN、
  停用场景不返回、他仓同名 scene_id 不命中、空 owner/空 name 仍返回并触发调用点回落、
  补录幂等 0 行且不覆盖既有列、`full_name` 缺省拼 owner/name、写后另一连接可见、
  **空键也照查表**（有 '' 行 → True，对照 `repo_key_exists_any` 短路返回空串）、
  5 条新语句无 `%s`/`NOW()`/`ON DUPLICATE`/`datetime('now'`、未登记查询名 KeyError、
  自有 DSN 时读写 SQL 全走 `db_registry.cursor('mgmt')` 不碰进程库、源码 0 条两表 SQL 且
  6 个调用点全接上）；`rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK /
  契约 37 条 / 看板最新）；`pytest tests/arch tests/contracts tests/scripts` **158 passed**；
  `make lint` **import OK**；`check_lib_query_names` PASS（5 个新函数均已登记）。
- 新基线：全局 **202 → 196**（-6），对 **61 → 61**（两条表仍有其他消费点）；
  基线 `foreign-refs-allowlist.csv` 收紧为 196 / 61；依赖看板与作业单同步刷新
  （下一批头名：`runtime/routes/chat.py` 6 处 / `runtime/im/handler_async.py` 6 处）。

---

### 7.12 runtime Chat IM 解绑/回显跨域读写收口（2026-11-15 本批）

- 文件：`src/domains/runtime/routes/chat.py`（runtime 域），**6 处 → 0**（单轮做完，未分两轮）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结，全部归 mgmt）：
  - `grape_t_channel_installation` × 3：原行 1561（解绑前判「除本人外还有几条 active 引用」）、
    1703（保存前查 app_id 的全部 active 持有轨）、2792（前端多渠表单回显：该用户各渠道安装行）；
  - `unified_user_mapping` × 2：原行 1600（app 释放清飞书映射）、1606（按 user_id 精确清本人映射）；
  - `feishu_onboard_task` × 1：原行 1631（解绑删扫码任务）。
  均为静态 SQL + `?` 占位符，无动态拼表名/列名；1703 / 2792 走
  `engine_wiring._connect(default_db_path())` 的 **runtime 库句柄**，1600/1606/1631 是
  「`mgmt_lib` 命中走 lib、否则本地兜底 SQL」的兜底侧。
- owner 侧新增（`?` 占位符，列序/谓词逐字照抄）：
  1) `_ANY_QUERIES` 补 4 条：`installation_other_refs_count`、`installation_holders_by_app`、
     `installation_user_channels`、`onboard_task_delete_by_app`；
  2) 5 个薄函数：`installation_other_refs_any`、`installation_holders_any`、
     `installation_user_channel_rows_any`、`clear_user_mapping_feishu_any`、
     `delete_feishu_onboard_task_any`；
  3) 用户映射两条复用**既有**具名查询 `um_clear_feishu_by_app` / `um_clear_feishu_by_user`
     （channel_directory 批次已登记，谓词与本批原语句逐字相同），不重复登记。
- 设计取舍：
  1) **安装表的读也走任意拓扑原语，而不是复用 `installation_* (conn, ...)`**：那批函数的
     `conn` 由调用方给，本文件能拿到的只有 runtime 库句柄——正是三库下读不到 mgmt 表的根因。
     新原语自带拓扑自适应（自有 DSN → `db_registry.cursor("mgmt")`；单库 → 进程主库 + `db_lock`），
     调用方不再需要（也拿不到）跨域连接。
  2) **解绑清理拆成「mgmt 写 → runtime 删 → mgmt 写」三段，各自提交**：跨域原子在三库拓扑下
     本就不可得（不同实例不同连接），硬凑只会让单库拓扑「看起来原子」而在三库下照样半途。
     解绑是 best-effort 清理（整个函数外层就是 `try/except + log.warning`），三段各自落库、
     失败只记一条 warning；这与改前三库拓扑的实际行为一致，单库拓扑的最终数据也一致。
  3) **`other_refs` 的「不含本人」用 `NOT (scope='user' AND owner_user_id=?)` 原样搬**：
     不改成 `scope='repo' OR owner_user_id!=?`（NULL owner 行会因三值逻辑漏算），
     可移植方言两条后端同义。
  4) **`updated_at` 仍由调用方算**：原实现是 `datetime.now().isoformat()`（本地时区 ISO 串），
     不是库时间；薄函数收 `updated_at` 参数，SQL 里不出现 `NOW()` / `datetime('now')`。
  5) **按 app 清映射保留「清该 app 全部行」的宽口径**：`WHERE app_id=?` 会清掉同 app 的他人行，
     但这条只在 `other_refs == 0`（确认无人再引用该 app）时执行，宽口径才是原意——不做收紧。
- 修的存量 bug（三库拓扑，均非统计口径问题）：
  1) **保存 IM 配置直接 500（功能面，最严重）**：1703 的 app_id 唯一性校验在 runtime 进程里直查
     `grape_t_channel_installation`，三库下该库没有这张表 → 1146 未被捕获 → `PUT /api/chat/config`
     带渠道载荷必失败。现走 mgmt 原语，两条拓扑都命中。
  2) **app 释放误清他人映射（数据面）**：1561 的引用计数被 `except` 吞成 `other_refs=0`，
     于是「还有别人在用这个 app」被判成「没人用」→ 按 app 全清，其他用户的飞书 open_id /
     app_id 一起被抹（他们下次推送落不到飞书）。
  3) **前端多渠表单恒「未接入」（功能面）**：2792 读不到安装行 → `_user_im_installations()`
     返回 `{}`，微信 / 企微 / 钉钉各渠道回显全空，用户以为没绑、重复绑定。
  4) **兜底分支的错位写**：1600/1606/1631 的兜底 SQL 打在进程库上——单库拓扑是同一份库看不出，
     三库拓扑下「写到 runtime 库、读在 mgmt 库」，清理实际无效且留下副本脏行。
- 验证：`py_compile` 两文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/chat_unbind_smoke.py`
  **ALL 100 CHECKS PASSED**（含边界与否定用例：4 条新语句与调用点 SQL 逐字对拍、无
  `%s`/`NOW()`/`ON DUPLICATE`/`datetime('now'`/`IFNULL(`、未登记查询名 KeyError；
  引用计数只数 active 且排除本人、他人 user 行与 repo 轨行算引用、revoked/他 app 不算、
  空 app_id 短路不把 `''` 脏行当引用；持有行列序与多行全返回；渠道回显只 active/disabled、
  `updated_at DESC`、同渠道多行不去重、他人行不串；按 app 清命中他人行 / 按 user 清只命中本人 /
  四列清空且 `updated_at` 取参值、平台登录名与昵称不被波及、两者都不给 ValueError；
  扫码任务空 app_id 返回 0 且不删 `''` 行；调用点整链路四场景——other_refs>0 只清本人、
  =0 按 app 全清、dingtalk 不碰飞书映射/扫码任务、空 old_app_id 直接返回，且
  `chat_conversations`/`repo_im_group_binding`/`bot_seen_messages` 逐字删除；
  自有 DSN 时 5 个原语全部走 `db_registry.cursor('mgmt')` 不碰进程库；源码面 0 条三表 SQL、
  5 个原语全接上、不再借 engine_wiring 句柄）；`rds_gate.sh --quick` **GATE: 全绿**
  （check-arch R1–R8 / 棘轮 OK / 契约 37 条 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**；
  `check_lib_query_names` PASS（5 个新函数均已登记）。
- 新基线：全局 **196 → 190**（-6），对 **61 → 60**（`feishu_onboard_task←runtime` 1 → 0，
  `grape_t_channel_installation←runtime` 9 → 6，`unified_user_mapping←runtime` 3 → 1）；
  基线 `foreign-refs-allowlist.csv` 收紧为 190 / 60；依赖看板与作业单同步刷新
  （下一批头名：`runtime/im/handler_async.py` 6 处 / 多个 6 处文件并列）。

---

### 7.13 runtime IM 异步处理器身份解析 / 巡检执行卡收口（2026-11-15 本批）

- 文件：`src/domains/runtime/im/handler_async.py`（runtime 域），**6 处 → 0**（单轮做完，未分两轮）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结，全部归 mgmt）：
  - `grape_t_channel_installation` × 2：原行 347（身份反查与身份表 JOIN）、359（user 轨安装的
    owner_user_id 兜底）；
  - `grape_t_channel_sender_identity` × 1：原行 346（身份反查的 FROM 侧，与上行是同一条语句）；
  - `autopilot_tasks` × 1：原行 391（`autopilot_executions JOIN autopilot_tasks`，
    执行表归 runtime、任务表归 mgmt，是真正的跨域 JOIN）；
  - `devops_pipeline_scene` × 2：原行 397（巡检执行卡里的场景列表）、431（派发时的场景选择），
    431 是「lib 命中走 lib，否则进程库兜底 SQL」的兜底侧。
- owner 侧新增（`?` 占位符，列序/谓词逐字照抄）：
  1) mgmt `_ANY_QUERIES` 补 4 条：`sender_identity_user_by_app`、
     `installation_owner_by_app_user_scope`、`autopilot_task_brief_row`、`scene_names_by_repo`；
  2) mgmt 侧 4 个薄函数：`sender_identity_user_by_app_any`、`installation_owner_by_app_any`、
     `autopilot_task_brief_any`、`scene_names_by_repo_any`；
  3) runtime `_ANY_QUERIES` 补 1 条 `autopilot_execution_detail`（`task_id, findings_json,
     result, status`）与薄函数 `autopilot_execution_detail_any`（执行表归本域，不产生跨域引用，
     但同样是「原 JOIN 里属于本域的那半条语句」）。
- 设计取舍：
  1) **跨域 JOIN 拆成两段 + Python 内连接**：`autopilot_executions ⋈ autopilot_tasks` 原先在
     runtime 进程库里一次查全；现改为 runtime 取执行行 → 把 `task_id` 交给 mgmt 取
     `(repo_key, name)`，在 Python 里按原列序（`t.repo_key, t.name, e.findings_json,
     e.result, e.status`）拼回。任务行缺失时与原 INNER JOIN 一样返回 `None`（烟测有显式用例）。
  2) **身份反查的两表 JOIN 不下放拆解**：身份表与安装表同属 mgmt，JOIN 不是跨域；拆成两次
     往返反而把「一次命中」变成「两次可能不一致的读」。所以整条语句进 mgmt 具名查询。
  3) **删掉 lib 优先 / 本地兜底双分支**：397 原走 `domain_read` 双 lib（`runtime_lib` +
     `mgmt_lib`），431 原走 `mgmt_lib(foreign_only=True)`；兜底 SQL 正是跨域统计来源，且在
     单库彩排里一直“绿”，掩盖了三库拓扑下 runtime 进程库根本没有这四张表（无视图 → 1146）。
     统一到任意拓扑原语（自有 DSN → 归属域连接；单库 → 进程主库 + `db_lock`），SQL 只在
     归属域留一份。
  4) **「一次查全」的性能意图显式放弃**：原实现靠同一把 `db_lock` + 同一游标省往返；现在三段
     各自持锁。执行卡是低频路径（点开一条巡检记录），换来的是两条拓扑同源与可移植，代价是
     2 次额外往返——在台账里写明，避免被误读为性能回归。
  5) **`get_user_id_by_app_id` 的返回类型不动**：两跳都仍返回 `str`（D105 uuid 保形），
     函数注解里的 `int | None` 是既有口径，不在本批改签名（调用方统一 `or 0`）。
- 修的存量 bug：
  1) **三库拓扑下钉钉等新引擎渠道消息 uid=0（功能面）**：身份反查与安装兜底都打在 runtime
     进程主库，三库拆分后这两张表只在 mgmt 库 → 1146 被 `except: pass` 吞成「查不到用户」，
     消息走「uid=0 → 名下没有仓库」的降级分支。现走 mgmt 原语，两条拓扑都命中。
  2) **巡检执行卡场景下拉恒空（功能面）**：`_autopilot_get_scenes` 的 lib 命中分支把
     `c` 重新赋值成结果列表，随后 `return [... for r in rows]` 读的 `rows` **在该分支从未绑定**
     → `NameError` 被外层 `except` 吞掉，函数恒返回 `[]`。也就是说凡有 mgmt lib 的拓扑
     （含生产），派发时的场景选择根本没数据。改为单一原语路径后该分支消失。
     397 的同一查询在 `_autopilot_exec_row` 里则会走 `c.execute` 兜底——同一份 SQL 两个函数
     两种结果，本批一并统一。
  3) **静默 except（卫生面）**：`get_user_id_by_app_id` 的两处 `except Exception: pass` 改为
     带 `log.debug` 的显式降级（行为不变：第一跳失败仍进第二跳，第二跳失败返回 `None`），
     该文件静默 except 计数下降，基线未上调。
- 验证：`py_compile` 三文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/im_handler_async_smoke.py`
  **ALL 68 CHECKS PASSED**（含边界与否定用例：5 条新语句与调用点原 SQL 逐字对拍、无
  `%s`/`NOW()`/`ON DUPLICATE`/`datetime('now'`、未登记查询名 KeyError；身份命中优先于安装
  owner、不给 sender_id 跳过身份跳、身份 `user_id` 为 `'0'`/空串/NULL 三态都不算命中、
  `scope='repo'` 不返回 owner、owner 空串/NULL/无行/空 app → None、两个原语都异常时返回
  None 不抛；执行行列序拼装、findings 坏 JSON 与空串降级 `{}`、执行行缺失与**任务行缺失**
  都返回 None、场景只取 `is_active=1` 且按 name 排序、`name` 为 NULL 回退 `scene_id`；
  场景函数无场景/空键/缺表三种降级、lib 分支变量错位 bug 不再存在；空参数路径 0 次查询、
  场景查询按原语句照查空键；**有自有 DSN 时 5 个原语全部走 `db_registry` 不碰进程库**；
  源码面 0 条四表 SQL、5 个原语全部接上、不再借 `engine_wiring`/lib 句柄查场景）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK / 契约 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **190 → 184**（-6），对 **60 → 59**（`autopilot_tasks←runtime` 6 → 5、
  `grape_t_channel_sender_identity←runtime` 1 → 0（该对清零，故对数减 1）；
  `grape_t_channel_installation←runtime` 6 → 4、`devops_pipeline_scene←runtime` 4 → 2，
  这两张表仍有其它消费者）；基线 `foreign-refs-allowlist.csv` 收紧为 184 / 59；
  依赖看板与作业单同步刷新（下一批头名：`runtime/engine/scheduler.py` 6 处）。

### 7.14 runtime 巡检任务调度跨域读写收口（2026-11-15 本批）

- 文件：`src/domains/runtime/engine/scheduler.py`（runtime 域），**6 处 → 0**（单轮做完，未分两轮）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结，两张表都归 mgmt）：
  - `autopilot_tasks` × 5：原行 909（`_notify_subscribers` 的 JOIN 右侧）、993（`_get_enabled_tasks`
    取启用任务全列）、1089（`_update_task_run_info` 回写运行时间）、1629（`scheduler_loop`
    取 last_run_at）、1687（`trigger_task_now` 取单任务全列）；
  - `autopilot_subscriptions` × 1：原行 908（`_notify_subscribers` 订阅者列表的 FROM 侧，
    与 909 是同一条语句的两行）。
  六处全部是「`mgmt_lib` 优先 / 进程主库兜底 SQL」双分支的**兜底侧**（964/988 行两条是
  `mgmt_lib()` 非空时再判 `foreign_only` 的死分支）。
- owner 侧新增（`?` 占位符，列序/谓词与原 SQL 逐字一致）：
  1) mgmt `_ANY_QUERIES` 补 5 条：`autopilot_subscribers_by_task`、`autopilot_tasks_enabled_full`、
     `autopilot_task_full_row`、`autopilot_task_last_run`、`autopilot_run_info_set`；
  2) mgmt 侧 5 个薄函数：`autopilot_subscribers_any`、`list_enabled_autopilot_tasks_any`、
     `autopilot_task_row_any`、`autopilot_task_last_run_any`、`set_autopilot_run_info_any`
     （写语句一行 `UPDATE ... SET last_run_at=?, next_run_at=?, updated_at=? WHERE id=?`，
     `updated_at` 与原实现一致 = `last_run_at`）；
  3) 同时**下线 4 条 MySQL 专用副本**（`_READ_QUERIES` 的 `autopilot_tasks_e3fa0b`、
     `autopilot_subscriptions_13648d`、`autopilot_tasks_enabled_full`、`autopilot_task_last_run`）：
     同一语义只留一份可移植 SQL，避免「`%s` 版能跑单库、三库拓扑却要另写」的两套真源。
- 设计取舍：
  1) **同域 JOIN 不拆**：订阅表与任务表都归 mgmt，`s ⋈ t` 不是跨域 JOIN，整条进具名查询；
     拆成两次往返只会把「一次命中」变成「两次可能不一致的读」。
  2) **写仍然走 outbox 意图 + 兜底原语**：`_update_task_run_info` 是跨域写，保留
     「lib 可用 → `cross_write.submit(autopilot_run_info_set, 幂等键)` 落意图 + 立即尝试」的
     既有 durability 设计；只有 lib 不可用（单库 / 未配 mgmt DSN）时才落到
     `set_autopilot_run_info_any`（自有 DSN → mgmt 连接；单库 → 进程主库 + `db_lock`）。
     该写自身幂等（同值 0 行变更），不需要跨域原子，因此**没有**引入 `_any_tx()`。
  3) **读一律单原语**：四处读删掉 lib/兜底双分支，只调 `*_any`。原双分支在单库彩排里一直绿，
     掩盖了三库拓扑下 runtime 进程库没有 `autopilot_*`（视图 0 → 1146，被调度循环的 `except`
     吞成「没有任务 / 没有订阅者 / 没有 last_run」）。
  4) **`autopilot_task_last_run_any` 返回整行而不是标量**：原口径是
     `row = ...fetchone(); last_run = row[0] if row else ""`——行在但 `last_run_at IS NULL` 时
     `last_run` 是 `None`（走 `_is_cron_due` 的「无 last_run」分支），和「无行 → 空串」不是同一分支。
     返回 `(value,)` 保住这个区分（烟测 B9/B10 两条断言）。
- 修的存量 bug：
  1) **终态通知在 lib 拓扑下永不发送（功能面）**：`_notify_subscribers` 的 lib 分支写的是
     `c = lib.read_all(...).fetchall()`（错赋给 `c`，`rows` 在该分支从未绑定）→ 紧跟的
     `if not rows:` 抛 `UnboundLocalError`，被本函数的 `except` 吞掉只留一条 warning。
     也就是凡配了 mgmt DSN 的拓扑（含生产），巡检执行完成后的飞书订阅通知一条也发不出去。
  2) **手动触发恒报「任务不存在」/ 直接崩（功能面）**：`trigger_task_now` 的 lib 分支写的是
     `c = lib.read_one(...).fetchone()`——`read_one` 已经返回行元组，再 `.fetchone()` 是
     `AttributeError`；即便不抛，`row` 在 lib 分支也从未绑定。现在统一成单原语路径，两个分支
     一起消失。
  3) **`_get_enabled_tasks` 的死分支（卫生面）**：外层 `mgmt_lib()`（不带 `foreign_only`）已保证
     lib 可用，内层再判 `mgmt_lib(foreign_only=True)` 不可能为真；且 `enabled_full` 与
     `list_enabled_autopilot_tasks()` 是同一条 SELECT 的两份副本。合并成一条原语后
     `%s`/`?` 两套 SQL 只剩一份。
- 验证：`py_compile` 两文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/scheduler_autopilot_smoke.py`
  **ALL 57 CHECKS PASSED**（含边界与否定用例：5 条新语句与调用点原 SQL 逐字对拍、无
  `%s`/`NOW()`/`ON DUPLICATE`/`datetime('now'`、未登记查询名 KeyError；enabled 过滤与 12 列列序、
  `last_run_at` 为 NULL 时仍返回行（不等于无行）、无订阅 / 任务行缺失 → 空列表（INNER JOIN 语义）、
  写命中 1 行 / 不存在 0 行 / `updated_at = last_run_at`；`_get_enabled_tasks` 字典键与空串列不被吞；
  `trigger_task_now` 不存在 / 重复触发 / 正常触发三态（含原 UnboundLocalError 用例）；
  `_notify_subscribers` 扇出两人各一次、第二次终态不重发、已通知执行不发、
  **任务行缺失 noop**、**订阅表缺失降级不抛**；`_update_task_run_info` lib 优先时意图行 applied
  且**不**落兜底、无 lib 时才写三列；`scheduler_loop` 单轮对每个启用任务各取一次 last_run；
  **有自有 DSN 时 5 个原语全部走 `db_registry`、不碰进程库**）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK / 契约 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；
  `pytest tests/mgmt/test_autopilot_feishu_notify.py tests/api/test_autopilot_write_primitive.py`
  **13 passed**（既有两条告警通知 / 写原语用例未受影响）；`make lint` **import OK**。
- 新基线：全局 **184 → 178**（-6），对 **59 → 57**（`autopilot_tasks←runtime` 5 → 0、
  `autopilot_subscriptions←runtime` 1 → 0，**两对同时清零**，故对数 -2、跨域表 41 → 39）；
  基线 `foreign-refs-allowlist.csv` 收紧为 178 / 57；依赖看板与作业单同步刷新
  （下一批头名：`src/domains/mgmt/routes/devstage.py` 6 处）。

### 7.15 mgmt DevStage 环境凭据清理收口（2026-11-15 本批）

- 文件：`src/domains/mgmt/routes/devstage.py`（mgmt 域），**6 处 → 0**（单轮做完，未分两轮）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结，三张表全归 runtime）：
  - `runtime_credentials` × 3：原行 56（`_annotate_worker_online` 取全部未吊销凭据的
    name/status/last_seen_at）、371（删除环境时按 `env.name` 定位凭据 id）、415（删凭据行）；
  - `agent_runtimes` × 2：原行 385（按 credential_id 取引擎实例 id）、414（删引擎行）；
  - `devops_agent_session` × 1：原行 412（会话摘绑 `runtime_id=0`）。
  六处全部是「`runtime_lib` 优先 / 进程主库兜底 SQL」双分支的**兜底侧**；`devops_agent_runtime_binding`
  清理留在 mgmt（该表归 mgmt，不在棘轮统计内）。
- owner 侧新增（`?` 占位符，列序/谓词与原 SQL 逐字一致）：
  1) runtime `_ANY_QUERIES` 补 2 条：`credential_health_rows`（
     `SELECT name, status, last_seen_at FROM runtime_credentials WHERE status != 'revoked'`）、
     `credential_id_by_name`（`SELECT id FROM runtime_credentials WHERE name=?`）；
  2) runtime 侧 3 个薄函数：`credential_health_rows_any()`、`credential_id_by_name_any(name)`、
     `purge_env_credential_any(env_name)`；
  3) `purge_env_credential_any` **复用既有 `purge_runtime_credential`**（后者已覆盖「按 credential_id
     取 rids → 摘绑会话 → 删 engine_capabilities → 删 agent_runtimes → 删 runtime_credentials」，
     域内 `_any_tx()` 一个事务），只把入口从 id 换成 name（先按名取 id 再委托），不复制第二条 SQL。
- 设计取舍：
  1) **五个站点合成一个原语**：371/385/412/414/415 是同一次「删环境 → 清 runtime 半区」的五步，
     拆成五个原语会让调用方重新编排跨域顺序、也重新长出「先摘绑还是先删行」的隐式约定。
     合成后 mgmt 侧只剩一句 `purge_env_credential_any(env_name)` + 清理自家 binding，
     runtime 侧的五步由归属域在一个事务里定序。
  2) **删掉 lib 优先 / 兜底双分支**：两条分支原本就是同一件事的两个写法，且 lib 分支里的
     `submit_if_lib(...)` 只有在 lib 存在时才可能进入 —— 那时 `purge_runtime_credential` 已经摘绑过
     会话，这段 outbox 逻辑在两种拓扑下都不产生效果（死分支）。统一到任意拓扑原语后一并移除，
     顺带少一个 `cross_write` 依赖。
  3) **健康注记仍由调用方派生 health**：原语只返回 `(name, status, last_seen_at)`，`derive_runtime_health`
     的判定（心跳 100s 阈值 / recently_lost / about_to_gc）留在 devstage 现成的调用里，
     避免把展示口径搬进 public 面。
  4) **空名凭据不当命中**：`credential_id_by_name_any("")` 直接返回 None。原兜底 SQL 的
     `WHERE name=?` 传空串会命中历史 `name=''` 脏行并把它的引擎行/能力行一起删掉；
     实际调用点有 `if env_name:` 前置，此处再兜一层。取 id 不做 strip（与原语句逐字匹配同口径）。
  5) **`rids` 交回调用方**：mgmt 侧要删的 `devops_agent_runtime_binding` 是 mgmt 表，
     跨域边界不在原语里出现 mgmt 表名；调用方按 400 分块拼 IN 列表（原实现单条 IN 无分块）。
  6) **`revoked` 凭据照清**：健康注记过滤 `status='revoked'`（展示上不该再挂一个已吊销的设备），
     但删除路径两种拓扑原本都不按 status 过滤，行为保持一致（烟测 C17/C18 明确覆盖）。
- 修的存量 bug（三库拓扑，均非统计口径问题）：
  1) **删除环境后本地凭据/引擎行残留（数据面，最严重）**：兜底分支在 mgmt 进程库里
     执行 `UPDATE devops_agent_session ...` → 该库没有这张表 → 1146 被内层 `except` 吞成一条
     warning，**后面的 `DELETE FROM agent_runtimes` / `DELETE FROM runtime_credentials` 全被跳过**。
     也就是三库拆分后，云上环境删掉了、本地凭据还在，同名 Worker 可以再次注册并拿到旧凭据。
  2) **能力行孤儿（数据面）**：即便兜底分支走通也不删 `engine_capabilities`，
     与 lib 分支口径不一致，引擎列表会留下指向已删引擎的能力行。现统一走
     `purge_runtime_credential`（删能力）。
  3) **DevStage 环境列表「未上线」恒真（功能面）**：`_annotate_worker_online` 在进程库直查
     `runtime_credentials`，三库拓扑下 1146 被 `except` 吞成空 map → 所有环境 `worker_online=false`，
     开机按钮/在线标识与实际 Worker 状态脱节。
- 验证：`py_compile` 两文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/devstage_credential_smoke.py`
  **ALL 84 CHECKS PASSED**（含边界与否定用例：2 条新语句与调用点原 SQL 逐字对拍、无
  `%s`/`NOW()`/`ON DUPLICATE`/`datetime('now'`、未登记查询名 KeyError；只排除 revoked
  且不过滤主动态/空名、同名多行全返回、按名逐字匹配不 strip、空名短路不查库；
  purge 摘绑只动本凭据引擎的会话、删能力/引擎/凭据行、无引擎仍删凭据、**revoked 也照清**、
  不存在与空名零写；健康注记五态（新鲜/离线/revoked/无心跳/过期）+ 空 envs 不查库 +
  查询异常降级不上抛；删除链路正常清理、列表缺失先 404 不发云删除、list 故障穿透、
  上游 not_found→404 / 其它→502、env 无凭据零写、purge 异常只 warning；
  **401 个引擎行跨 400 分块全清**；有自有 DSN 时三原语全走 `db_registry` 且进程库零改动；
  源码面 0 条三表 SQL、不再借 `domain_read`/`runtime_lib`、两处调用点均已接上原语）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK / 契约 37 条 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **178 → 172**（-6），对 **57 → 55**：`runtime_credentials←mgmt` 3 → 0、
  `agent_runtimes←mgmt` 2 → 0（**两对清零**），`devops_agent_session←mgmt` 3 → 2（该表仍有其它消费者）；
  基线 `foreign-refs-allowlist.csv` 收紧为 172 / 55；依赖看板与作业单同步刷新
  （下一批头名：`src/api/issue_vcs_index.py`、`scene_binding_repair.py`、`skill_display.py`、
  `metric_rollup.py` 四个 6 处文件并列）。

---

### 7.16 insight 指标聚合桶取数下沉 runtime（2026-11-15 本批）

- 文件：`src/domains/insight/observability/metric_rollup.py`（insight 域），**6 处 → 0**（单轮做完，未分两轮）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结，三张表全归 runtime）：
  - `agent_tasks` × 4：原行 147（按 `created_at` 的 `(repo_key, status)` 计数）、155（按 `completed_at`
    同款）、173（`status='failed'` 且 `result_text|errors` 命中超时签名的按仓库计数）、
    199（按 `started_at` 取 `repo_key, created_at, started_at, completed_at` 四列）；
  - `events` × 1：原行 188（`report_missing=1` 按仓库计数，Issue #1938 补救指标）；
  - `runtime_credentials` × 1：原行 230（在线凭据数，桶末 5min 心跳窗）。
  六处全部落在 `compute_bucket()` 一个函数里（分钟/小时桶共用），原实现整段持 `db_lock`
  用进程主库游标直查。
- owner 侧新增（`?` 占位符，列序/谓词与原 SQL 逐字一致）：
  1) runtime `_ANY_QUERIES` 补 6 条：`rollup_task_counts_by_created`、`rollup_task_counts_by_completed`、
     `rollup_task_timeout_counts`、`rollup_task_timing_rows`、`rollup_event_cascade_counts`、
     `rollup_online_credentials`；
  2) runtime 侧 6 个薄函数：`task_status_counts_by_created_any(s, e)`、`task_status_counts_by_completed_any(s, e)`、
     `task_timeout_counts_by_repo_any(s, e, signature)`、`task_timing_rows_any(s, e)`、
     `event_cascade_counts_by_repo_any(s, e)`、`online_credential_count_any(since)`。
- 设计取舍：
  1) **六条查询逐条照抄成具名 SQL，不合并不参数化**：调用方按行下标解包（`for repo_key, status, n in ...`），
     列序错位就是静默串列；具名查询是静态文本，能和原语句逐字对拍（烟测 A1）。也没有复用
     `query_events_any` 这类「传 where 片段」的接口 —— 那等于把 SQL 拼接面搬到 insight 侧。
  2) **超时签名的 LIKE 通配由 owner 拼**：调用方只传 `_TIMEOUT_FAIL_SIGNATURE` 常量，不再自建
     `f"%{sig}%"`；空签名直接返回空（原写法会退化成 `LIKE '%%'` 全命中，等于把所有 failed
     都算成超时失败）。
  3) **`events.report_missing` 的列探测搬进 owner，并且不用 PRAGMA**：`db_registry` 的薄兼容游标
     只对进程主库翻译 PRAGMA（MySQL 上必 1064），三库拓扑下 insight 进程库又没有 `events`；
     改成 `SELECT * ... LIMIT 0` 读 `description`，两条后端同一写法。探测在归属域连接上做，
     老库（#1938 前）返回空列表、不抛异常。
  4) **放弃「桶内一致快照」**：原实现整段持 `db_lock` 用一个游标取 ①②③，三库拓扑下做不到
     （每域一条独立连接）。桶是**已结束**的时间片、事后不重算，桶内/桶间轻微漂移不影响
     趋势口径；`write_bucket` / `series` / `fetch_usage_rows` 仍持 `db_lock` 直查本域表不变。
  5) **⑤ 的 status 口径照抄黑名单而不是白名单**：只排除 `revoked`，`offline` 等其它状态照数，
     `last_seen_at` 为 NULL 或早于窗口的不算 —— 与「设备记账在线数」的原语义一致。
- 修的存量 bug（三库拓扑，功能面）：
  1) **分钟/小时桶在拆分后基本全空（最严重）**：insight 进程库没有 `agent_tasks` / `events` /
     `runtime_credentials`，①②③②c 全 1146，被上层 `except` 吞成空桶；`catch_up` 于是只写出
     `global.runtimes_online=0` + 本域 `obs_runtime_samples` 的 `engine_avail_*`。也就是
     任务量/成功率/排队耗时/token 趋势与 SLO 小时桶底座在独立部署下**没有数据**，看板回退直查。
  2) **`cascade_compensation` 从来没落过数**：即便单库拓扑，`PRAGMA table_info(events)` 只在
     SQLite 有效，MySQL 上探测永远失败（1064 被吞）→ Issue #1938 的补偿计数在指标留存里恒缺。
- 验证：`py_compile` 两文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/metric_rollup_smoke.py`
  **ALL 56 CHECKS PASSED**（含边界与否定用例：6 条具名查询与原 SQL 逐字对拍 + 占位符个数 +
  无 `%s`/`NOW()`/`ON DUPLICATE`/`datetime('now'`、未登记查询名 KeyError；半开窗口三条时间列各按
  自己的列过滤、`repo_key` NULL/'' 一律排除；超时只数 failed 且 `result_text`|`errors` 任一命中、
  不命中/空签名均空；`report_missing` 列缺失 → 空且不抛、桶仍算完；`compute_bucket` 逐指标对账
  含 global 加权/p95 取最大/未归属 token 只进 global 不造空维度；自有 DSN 时六原语全走
  `db_registry` 且进程库零改动；源码面 0 条三表 SQL、0 处 PRAGMA、不再自拼 LIKE）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK / 契约 37 条 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **172 → 166**（-6），对数 **55 不变**：`agent_tasks←insight` 13 → 9、
  `events←insight` 7 → 6、`runtime_credentials←insight` 8 → 7（该文件三对全部清零并有下降）；
  基线 `foreign-refs-allowlist.csv` 收紧为 166 / 55，依赖看板同步刷新。

---

### 7.17 api 技能显示名解析下沉 runtime（2026-11-15 本批）

- 文件：`src/api/skill_display.py`（api 域），**6 处 → 0**（单轮做完，未分两轮）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结，六处全为 `skill`，归 runtime）：
  - 原行 52（`skill_id` 精确档）、60（显式 `(scope, owner_id)` 档）、69（`scope='user'` +
    `owner_id=user_id`）、78（`scope='repo'` + `owner_id=repo_key`）、86（`scope='official'` 兜底）、
    113（批量版一次取全部 active 行）。
  六处都在两个函数里（`resolve_skill_display_name` 回退链 / `build_skill_display_map` 批量映射），
  全是静态 SQL，无动态拼接、无跨域 JOIN。
- owner 侧新增（`?` 占位符，列序/谓词与原 SQL 逐字一致）：
  1) runtime `_ANY_QUERIES` 补 3 条：`skill_display_name_by_id`、`skill_display_name_by_scope`
     （`skill_name=? AND scope=? AND owner_id=? AND is_active=1 LIMIT 1`）、
     `skill_active_display_rows`（`skill_name, display_name, scope, owner_id ... WHERE is_active=1`）；
  2) runtime 侧 3 个薄函数：`skill_display_name_by_id_any(skill_id)`、
     `skill_display_name_by_scope_any(skill_name, scope, owner_id)`、`skill_active_display_rows_any()`；
  3) official 档**复用既有** `skill_display_name_official_any`（上批 autopilot 收口时已建），不复制第二条 SQL。
- 设计取舍：
  1) **user / repo 两档合成一条参数化语句**：原实现是两条只差字面量的 SQL（`scope='user'` /
     `scope='repo'`），档位本就是谓词值，`scope` 交给调用方传。省一条具名查询，语义不变
     （owner_id 仍按身份原样进谓词，不做归一化）。
  2) **`None` 与 `""` 必须分开返回**：`None` = 无 active 行（回退链继续往下），`""` = 有行但
     `display_name` 为空（调用方回退 skill_name **并终止**回退链）。原实现在「本人 user 档的
     display_name 为空」时返回 skill_name、不再看 official 档；若把两者都压成空串，会滑到官方名，
     破坏「展示名与实际绑定档位一致」这条 Issue #980 的口径。烟测 C17/C18 明确覆盖两侧。
  3) **official 档复用而非新建**：既有查询谓词集与原语句相同（`is_active=1` + `scope='official'` +
     `skill_name=?`），原语句多写的 `LIMIT 1` 由 `read_one_any` 的单行读吸收（两者都无 `ORDER BY`，
     行选择同口径）。重复一条几乎相同的静态 SQL 只会让两处将来改过滤条件时漏改。
  4) **回退链不再持有 `db_lock`**：原实现整段持锁一次游标查五层，三库拓扑下 `skill` 不在 api
     所在进程的库里，锁内也拿不到；现在每层一次任意拓扑读，锁由 owner 侧原语自己管。
     展示名解析是逐层「命中即返回」，本来也不需要跨语句的一致快照。
  5) **批量版仍然一次查询**：`skill_active_display_rows_any()` 一次取全部 active 行，优先级
     （3=user 本人 / 2=repo 本仓 / 1=official，prio=0 不写映射）留在调用方 Python 侧 —— 档位判定
     是展示策略，不该搬进 owner public 面。
- 修的存量 bug（三库拓扑，功能面）：
  1) **节点显示名降级为原始 skill_name（Issue #980 的修复整体失效）**：该模块由 mgmt 服务
     （`routes/scenes.py`）与 insight 服务（`routes/dashboard.py`）共同导入，两个进程的主库分别是
     mgmt / insight 库，都不含 `skill`（归 runtime）→ 每次查询 1146。`build_skill_display_map` 把异常
     吞成空 map、`resolve_skill_display_name` 吞成 skill_name，于是 scenes（with-pipeline）与
     dashboard 的节点名全部回退成技能名，三档同名技能依旧无法区分 —— 拆分后这两条路由的
     显示名修复等于停摆。
  2) **批量路径静默无日志**：`except Exception: return {}` 不带任何记录，三库部署下「映射恒空」
     无法从日志看出来。现改为 `logger.debug` 留痕（仍不上抛，保持调用方零回归）。
- 验证：`py_compile` 两文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/skill_display_smoke.py`
  **ALL 81 CHECKS PASSED**（含边界与否定用例：3 条具名查询与原 SQL 逐字对拍 + official 谓词集对拍 +
  占位符个数 + 无 `%s`/`NOW()`/`ON DUPLICATE`/`datetime('now'`、未登记查询名 KeyError；
  回退链逐档命中/压过关系、失活行不命中、同名失活+active 取 active、**本人档 display 为空即终止
  链**、official display 为 NULL 回退名、逐字匹配不 strip、user_id 转 str 比较、user_id=0 跳过、
  非数字 skill_id 与 DB 异常均回退 skill_name 不上抛；批量映射 prio 判定、他人 user 档/他仓 repo 档
  不写映射、同档先到者胜、空上下文只有 1 次查询、DB 异常回 {}；节点级分支 skill_id 真值/0/None
  三条路径；自有 DSN 时三原语全走 `db_registry` 且进程库零改动；源码面 0 条 skill SQL、0 个 SELECT、
  不再 import `kernel.session_manager`、只用 4 个已登记原语）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK / 契约 37 条 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **166 → 160**（-6），对数 **55 不变**：`skill←api` 8 → 2（该文件 6 处清零，
  余 2 处在其它 api 文件）；基线 `foreign-refs-allowlist.csv` 收紧为 160 / 55，依赖看板与作业单
  同步刷新（下一批头名：`src/api/scene_binding_repair.py` 6 处）。

### 7.18 api 存量场景绑定修复迁移下沉 owner 域（2026-11-15 本批）

- 文件：`src/api/scene_binding_repair.py`（api 域，Issue #541 启动期迁移），**6 处 → 0**（单轮做完）。
- 表分布（`table-owner.tsv` 冻结）：
  - `managed_repos`（归 mgmt）× 2：原行 76 `SELECT repo_key, dev_scene ... WHERE dev_scene != ''`、
    行 85 `UPDATE ... SET dev_scene=? WHERE repo_key=? AND dev_scene=?`；
  - `issue`（归 runtime）× 2：原行 99 `SELECT id, repo_key, issue_number, scene_id ... WHERE scene_id != ''`、
    行 108 `UPDATE ... SET scene_id=? WHERE id=? AND scene_id=?`；
  - `scene_market`（归 mgmt）× 1：原行 34（同名 official + `is_active=1`）；
  - `devops_pipeline_scene`（归 mgmt）× 1：原行 48（`market_scene_id` + repo 副本，`updated_at DESC, id DESC`）。
  六处全是静态 SQL，无 f-string 拼接；无跨域 JOIN（解析链本就是三次独立查询）。
- owner 侧新增（`?` 占位符，列序/谓词逐字照抄）：
  1) mgmt `_ANY_QUERIES` 4 条：`scene_active_official_id_by_name`、`scene_copy_by_market_id`、
     `repo_bound_scene_rows`、`repo_dev_scene_set_cas`；薄函数
     `scene_active_official_id_by_name_any` / `scene_copy_id_by_market_any(market_scene_id, repo_key)` /
     `repo_bound_scene_rows_any()` / `set_repo_dev_scene_cas_any(repo_key, old, new)`；
  2) runtime `_ANY_QUERIES` 2 条：`issue_bound_scene_rows`、`issue_scene_set_cas`；薄函数
     `issue_bound_scene_rows_any()` / `set_issue_scene_cas_any(issue_id, old, new)`。
- 设计取舍：
  1) **匹配链规则留在调用方，owner 只暴露四个事实**：候选仓库行、模板 id、副本 scene_id、条件改绑。
     「仓内已存在 → 不修」「模板不存在/失活/非 official → 跳过」是迁移策略，不该固化进 public；
  2) **不复用 `scene_official_id_by_name`**：既有查询缺 `is_active=1`，而原语句带该谓词 —— 复用它会把
     已停用的 official 模板当成可修复目标，故新增 `scene_active_official_id_by_name`（两条并存，
     各自谓词集不同，不做合并）；
  3) **CAS 原语直接沿用原 WHERE 的旧值谓词**：`set_*_cas_any` 不做「先读后判」，rowcount=0 即
     「并发已改走」，与原实现的 `if c.rowcount:` 计数口径完全一致；重复跑二轮 fixed=0；
  4) **锁与提交交给任意拓扑原语**：原来每个仓库/issue 各持一次 `db_lock` + 手动 `db.commit()`，
     三库拓扑下锁的是 api 进程库、表却不在那儿；现在 `exec_any` 在单库回落进程库 + `db_lock`，
     自有 DSN 走域连接 autocommit，两条拓扑同一份 SQL；
  5) **两次查询失败分别保留原返回口径**：模板查询失败 → `""`（跳过该行）；副本查询失败 →
     `""`；异常仍由调用方 `log.warning` 留痕后继续，不上抛（启动期不阻断）。
- 修的存量 bug（三库拓扑，功能面）：
  1) **Issue #541 修复整体失效**：`scene_market` / `devops_pipeline_scene` 在 api 进程库里没有
     （视图 0）→ 模板查询 1146，被 `except` 吞成 `return ""`；`managed_repos` / `issue` 扫描同样
     1146 → `repos_fixed` / `issues_fixed` 恒 0 且每行刷一条 warning。存量错误绑定（模板名误写进
     `dev_scene` / `scene_id`）在独立三库部署下永远修不掉；
  2) **`pipeline_config.scene_exists_in_repo` 直发进程主库**：该函数由 api（本迁移）与 runtime
     （`engine/engine_select.py` 引擎选择）跨域调用，进程库里没有 `devops_pipeline_scene`（归 mgmt）
     → 1146 被吞成 `False`，把「仓内已存在」误判为「不存在」。现改走本域
     `scene_id_exists_any`（任意拓扑），两条调用路径都不再误判；该函数在 mgmt 域内，SQL 仍只此一份。
- 验证：`py_compile` 4 个文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/scene_binding_repair_smoke.py`
  **ALL 72 CHECKS PASSED**（含边界与否定用例：6 条具名查询与原 SQL 逐字对拍 + 占位符个数 +
  无 `%s`/`NOW()`/`ON DUPLICATE`、未登记查询名 KeyError；同名失活 official 不遮蔽 active 行、
  custom 来源与仅失活名字不命中、多副本取 `updated_at` 最新、同刻 `id DESC`、失活新副本不参与、
  仓内已存在短路、跨仓同名 scene 不认「已存在」、空串/NULL 绑定不计入 scanned、二轮幂等 fixed=0、
  竞态下 CAS 不覆盖不重复计数、owner 抛错不上抛且另一段照跑、缓存仅在确实改绑时失效；
  自有 DSN 时全走 `db_registry` 且进程库零 SQL；源码面 0 条 SQL 字面量、无 cursor/execute 直发、
  不 import `kernel.session_manager`、只用 6 个已登记原语）；`rds_gate.sh --quick` **GATE: 全绿**；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **160 → 154**（-6），对数 **55 → 53**：`api←managed_repos`、`api←scene_market`
  两对清零（`api←issue` 12 → 10、`api←devops_pipeline_scene` 4 → 3 仍有其它 api 文件）；
  基线收紧为 154 / 53，看板与作业单同步刷新（下一批头名：`src/api/issue_vcs_index.py` 6 处）。

### 7.19 api 侧 VCS 编号索引台账读写下沉 runtime 域（2026-11-15 本批）

- 文件：`src/api/issue_vcs_index.py`（api 域，Issue #995 provider 感知 VCS 编号索引），**6 处 → 0**（单轮做完）。
- 表分布（`table-owner.tsv` 冻结）：
  - `issue`（归 runtime）× 6：原行 273 `SELECT id, issue_number, vcs_exists, vcs_checked_at
    FROM issue WHERE repo_key=?`（读台账）；行 317/321/325 三组 `UPDATE issue SET
    vcs_exists=0|1|不动, vcs_checked_at=datetime('now') WHERE id IN (…)`（标删/恢复/仅盖戳）；
    行 433 `SELECT DISTINCT repo_key FROM issue WHERE vcs_checked_at IS NULL OR
    vcs_checked_at=''`（存量回填扫仓）；行 451 `SELECT COUNT(*) FROM issue WHERE
    repo_key=? AND vcs_checked_at IS NOT NULL`（回填计数）。
  - 行 317/321/325 是 **f-string 动态拼接 IN 列表**（占位符个数随 500/批的 chunk 变），表名/列名
    不来自外部输入；其余三条静态参数化。无跨域 JOIN（三态判定与复核链都在 Python 侧）。
- owner 侧新增（runtime 域 public）：
  1) `_ANY_QUERIES` 3 条具名读：`issue_vcs_index_rows`、`issue_vcs_backfill_repos`、
     `issue_vcs_checked_count`（列序/谓词逐字照抄）；
  2) `_ISSUE_VCS_QUERIES` 3 条写模板：`issue_vcs_mark_deleted`、`issue_vcs_mark_restored`、
     `issue_vcs_stamp_checked`，`{ph}` 是 IN 列表占位符标记；
  3) 薄函数 4 个：`issue_vcs_index_rows_any` / `issue_vcs_backfill_repo_keys_any` /
     `issue_vcs_checked_count_any` / `apply_issue_vcs_existence_any(deleted, restored, stamped)`。
- 设计取舍：
  1) **动态 IN 留在 owner 展开**：`exec_any` 只认固定 SQL 文本；若在调用方拼 `?,?,…` 等于把 SQL
     拼回 api 文件，跨域引用照样计。owner 侧用 `{ph}` 模板 + **id 值列表**参数，语句文本固定、
     调用方无法注入表名/列名；
  2) **三组 UPDATE 合成一个原子原语**：原实现是一次 `db_lock` + 一次 `db.commit()`；拆成 3 次
     `exec_any` 会变 3 个独立提交（半批可见，且失败只回滚一组）。`apply_issue_vcs_existence_any`
     用 `_any_tx()`：自有 DSN 走真事务，单库回落进程库并提交/回滚，两条拓扑同一份 SQL；
  3) **不在 Python 里算时间戳**：原 SQL 的 `datetime('now')` 由 `mysql_compat` 翻成
     `CAST(NOW() AS CHAR)`，两方言同源；改传 Python 时间会把「DB 时钟」换成「应用时钟」，
     跨机器时钟偏斜直接改变 `vcs_checked_at` 口径；
  4) **读走标准白名单**（`read_one_any`/`read_all_any`），只有带 IN 的三条写才另起
     `_ISSUE_VCS_QUERIES`——不为 3 条写语句再开一套通用注册面；
  5) **分块上限 500 与原实现逐字一致**：原 `_chunks(size=500)` 随迁删除，改由 owner 内部按
     500/批切分（烟测：1200 id → 3 条语句 500/500/200）；
  6) **失败口径不变**：读失败/写失败 → 返回 `None`（调用方映射 `unavailable` → 列表 fail-open）；
     唯一改动的吞噬点是「计数失败」，从 `except: pass` 改为 `log.debug`（少报 `cards` 不影响回填主流程）。
- 修的存量 bug（三库拓扑，功能面）：
  1) **VCS 存在性回写整批 1146**：`_apply_existence` 读 `issue` 在 api 进程库里不存在（视图 0）
     → 异常被吞成 `return None` → `refresh_vcs_existence` 一律 `unavailable` → 列表永远 fail-open
     不过滤，`issue.vcs_exists` 永不落列，**已删除卡片永不标删**（Issue #995 的过滤能力在独立部署
     下整体失效）；写路径同样 1146（即使读到也会整批失败）；
  2) **存量回填恒零**：`backfill_vcs_exists()` 默认入参先扫 `issue` 待校验仓 → 1146 被吞成
     `{"repos":0,"ok":0,"unavailable":0,"cards":0}`，启动期回填（`main.py` 后台任务）等于空转；
  3) 静默吞噬点上岸：回滚 `except: pass` 随 `_any_tx` 迁走、计数失败改 `log.debug`
     （该文件裸计数 7 → 5、应用豁免后净 6 → 4，基线 6 未上调）。
- 验证：`py_compile` 2 个文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/issue_vcs_index_smoke.py`
  **ALL 96 CHECKS PASSED**（含边界与否定用例：6 条语句与原 SQL 逐字对拍 + IN 展开占位符个数 +
  无 `%s`/`NOW()`/`ON DUPLICATE` + 未登记名 KeyError；读列序 `(id, issue_number, vcs_exists,
  vcs_checked_at)`、空仓 → `[]`；待校验扫描 `NULL` **或空串** 都命中、`DISTINCT` 且空 `repo_key`
  不混入；计数口径 `IS NOT NULL` **空串算已校验**（与扫描口径故意不对称，是原实现语义）；
  三态回写：未校验才盖戳 / 已标删恢复为 1 / 已校验完全不动 / 复核 `exists` **不盖戳**（不让 TTL
  内新卡变「已核验」）/ 复核 `unknown` 记 unresolved / `vcs_exists` 为 NULL 按 `!= 0` 视为「存在」
  / 非数字编号整行跳过 / 已标删不重复写；候选 **>100** 整体 unresolved 且零写入、恰 100 正常复核；
  1200 id → 3 条 UPDATE(500/500/200)；中段语句失败整批回滚（第一条也不落地）+ 调用方映射
  `None`；同输入二轮零 UPDATE；时序为 读 → 复核 → 写（网络在锁外）；自有 DSN 时全走
  `db_registry` 且进程库零 SQL；源码面 0 条 SQL 字面量、无 cursor/execute 直发、
  不 import `kernel.session_manager`、不持 `db_lock`、只用 4 个已登记原语）；
  `tests/mgmt/test_issue_995_vcs_exists.py` **12 passed**（既有用例无回归）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK / 契约 37 条 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **154 → 148**（-6），对数 **53 不变**：该文件 6 处清零，`api←issue` 10 → 4
  （其余 4 处在其它 api 文件）；基线 `foreign-refs-allowlist.csv` 收紧为 148 / 53，
  依赖看板与作业单同步刷新（下一批头名：`src/api/a2a_dispatch_guard.py` 5 处）。

### 7.20 insight 能效看板取数下沉 owner 域（2026-11-15 本批）

- 文件：`src/domains/insight/observability/efficiency.py`（insight 域，Flow Efficiency
  看板 10 卡聚合），**5 处 → 0**（单轮做完，未分两轮）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结）：
  - `issue`（归 runtime）× 2：原行 138 `SELECT status, created_at, completed_at,
    created_by, assignee, user_id ... WHERE repo_key IN (…) AND is_subtask_mode=0`
    （看板轻量行，②③①⑥⑤⑦⑧⑨⑩ 的基础数据）；行 619 下钻详情 10 列（`issue_number,
    title, status, COALESCE(stage,''), COALESCE(scene_id,''), created_at, completed_at,
    COALESCE(created_by,''), COALESCE(assignee,''), COALESCE(user_id,0)`，同款谓词）；
  - `agent_tasks`（归 runtime）× 1：原行 451 `SELECT status, started_at, completed_at,
    agent_type ... WHERE repo_key IN (…)`（⑩ 任务交付时长）；
  - `grape_accounts`（归 mgmt）× 1：原行 364 `SELECT id, username ... WHERE id IN (…)`
    （⑨ 在办分布的归属人用户名）；
  - `devops_pipeline_scene`（归 mgmt）× 1：原行 634 `SELECT scene_id,
    COALESCE(NULLIF(display_name,''), NULLIF(name,'')) ... WHERE scene_id IN (…)`
    （下钻列表的场景展示名）。
  五处全是 **f-string 动态拼接 IN 列表**（占位符个数随 500/批的 chunk 变），表名/列名
  不来自外部输入；无跨域 JOIN（聚合链全在 Python 侧）。
- owner 侧新增（`?` 占位符，列序/谓词与原 SQL 逐字一致；IN 展开在 owner 内部）：
  1) runtime public 3 个薄函数：`efficiency_issue_rows(repo_keys, cursor=None)`、
     `efficiency_issue_detail_rows(repo_keys, cursor=None)`、
     `efficiency_task_rows(repo_keys, cursor=None)`；
  2) mgmt public 2 个薄函数：`account_name_rows_by_ids(ids, cursor=None)`、
     `scene_display_name_rows(scene_ids, cursor=None)`；既有 `account_names_by_ids`
     改为其上的 dict 包装（dashboard 的 `_efficiency_uid_name_loader` 调用口径不变）；
  3) 两侧各一个 `_owner_cursor(cursor)` 私有上下文助手：模拟/单库拓扑复用调用方游标，
     自有 DSN 时忽略。
- 设计取舍：
  1) **动态 IN 留在 owner 展开**：`read_all_any` 只认固定 SQL 文本；调用方拼 `?,?,…`
     等于把 SQL 拼回 insight 文件（跨域引用照样计）。owner 侧按 500/批切分、自己展开
     占位符，语句文本固定、调用方无法注入表名/列名。分块只服务 SQLite 变量上限
     （原 issue/task 两条语句一次带走全部 repo_key，仓库候选通常 1~3 个），行集合不变。
  2) **`cursor` 参数不是「把连接交回调用方」，而是「单库拓扑复用同一库」**：本模块的
     设计契约是「喂一个 cursor + SQLite 建表即可单测」（模块头注释），既有 25 个单测
     全部按这个契约写。单库拓扑下调用方游标与进程主库本就是同一个库，复用它省一次嵌套
     `db_lock`（RLock，重入安全）并保住调用方事务快照；三库拓扑下本域有自有 DSN，
     调用方游标指向 insight 进程库（无本域表），**必须忽略** —— 那正是原 1146 的来源。
     这条规则写在两侧 `_owner_cursor` 的 docstring 里，由烟测 I 段两侧验证
     （单库：调用方库有 SQL、进程库零 SQL；自有 DSN：两侧零 SQL、域连接全有）。
  3) **owner 只给事实，回退/去重口径留在 insight**：场景名「display_name 空串或 NULL
     回退 name、两者都空不回映射、同 scene_id 首行优先」与账号名「`int(uid)` 做键、
     空 username 不进映射」都是展示策略，owner 返回原始行，调用方按原规则处理 ——
     两处口径逐字保留（烟测 C/F/G 逐条对拍改造前 SQL 结果）。
  4) **单批失败保留部分结果**：`load_uid_names` / `load_scene_names` 原来在每个 500
     分块上 try/except 并在失败时 `return out`（已取到的部分保留）。现在分块留在调用方
     （owner 内部再切一次单块），逐批失败语义与原来完全一致。
  5) **任务行缺表降级留在调用方**：`_fetch_task_rows` 的 `except → []`（老库/最小测试
     库无 `agent_tasks`）保持不动，owner 不吞异常 —— 谁的业务降级谁处理。
  6) **不为 5 条语句再开 `_ANY_QUERIES`**：它们的 IN 列表是动态的，登记成固定文本
     反而不匹配；同文件既有 `efficiency_issue_contrib` / `agent_sessions_with_runtime`
     就是「owner 内直接构造 ph + 执行」的写法，本批沿用，避免一个文件两套风格。
- 修的存量 bug（三库拓扑，功能面）：
  1) **能效看板在独立部署下基本是空板**：路由层
     `with db_lock: build_board(db.cursor(), …)` 传的是 insight 进程库游标，
     `issue` / `agent_tasks` / `grape_accounts` / `devops_pipeline_scene` 四张表在
     insight 库里都没有（视图 0）→ 首条 `_fetch_issue_rows` 就 1146，被路由的
     顶层 `except` 吞成 `{"error": "aggregation_failed"}`：②总工作量/③在制/①净流入/
     ⑥吞吐/⑤超龄/⑦⑧周期与积压/⑨在办/⑩任务交付**十张卡全空**，
     `GET /api/dashboard/efficiency/board` 等于永久故障页；
  2) **⑨ 在办分布恒为「未分配」**：即便 issue 行侥幸读到，`load_uid_names` 的
     `grape_accounts` 查询同样 1146 → 用户名映射恒 `{}` → `created_by/assignee` 为空
     的行全部落到「未分配」，看起来像「全平台没人认领」；
  3) **下钻列表场景列显示 scene_id**：`load_scene_names` 1146 → `{}` →
     `scene_map.get(scene_id, scene_id)` 回退成原始 id，用户看到 `scene_9f3c…`；
  4) **`_fetch_task_rows` 的降级把真故障也一起吞**：原 `except → []` 没日志，三库拓扑
     下「任务交付恒 0 样本」在日志里看不出（本次保持降级但口径已改走 owner 原语，
     正常拓扑下不再触发）。
- 验证：`py_compile` 3 个文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/efficiency_smoke.py`
  **ALL 81 CHECKS PASSED**（含边界与否定用例：5 条语句与改造前 SQL 逐字对拍 + 无
  `%s`/`NOW()`/`ON DUPLICATE`/`datetime('now'`；`is_subtask_mode=0` 严格等值 —— `=1` 与
  `NULL` 都不计；详情行 10 列含 COALESCE 默认值（NULL stage/scene_id/created_by/assignee
  → ''、NULL user_id → 0）；任务行缺表 → 调用方 `[]` 不抛；账号名 TEXT id → int 键、
  空/NULL username 与未知 id 不进映射、`0/None/'None'/''` 不查、600 id → 2 条语句
  (500/100)、**单批失败保留已取到部分**；场景名 display_name NULL 与空串都回退 name、
  两者都空不回映射、同 id 首行不覆盖、缺表 → `{}`；1200 仓键 → 3 条语句(500/500/200)；
  单库拓扑调用方库有 SQL 且进程库零 SQL、无游标回落进程库、自有 DSN 两侧零 SQL 且
  域连接全有；源码面 0 条 SQL 字面量、无 cursor()/execute() 直发、不 import
  `kernel.session_manager`、不持 `db_lock`、只用 5 个已登记原语）；
  `tests/insight/test_efficiency_flow_board.py` **25 passed**（既有用例无回归）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK / 契约 37 条 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **148 → 143**（-5），对数 **53 → 50**：`issue←insight` 2 → 0、
  `grape_accounts←insight` 1 → 0、`devops_pipeline_scene←insight` 1 → 0 三对清零，
  `agent_tasks←insight` 9 → 8（其余 8 处在 insight 其它文件）；基线
  `foreign-refs-allowlist.csv` 收紧为 143 / 50，依赖看板与作业单同步刷新
  （下一批头名：`src/api/server_common.py` 5 处）。

### 7.21 api 进程运维探活取数下沉 owner 域（2026-11-15 本批）

- 文件：`src/api/server_common.py`（api 域，共享 FastAPI 脚手架：慢请求指标中间件 +
  `attach_health` 的 readiness / realtime 诊断），**5 处 → 0**（单轮做完，未分两轮）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结）：
  - `api_latency_log`（归 insight）× 1：原行 357 慢请求中间件
    `INSERT INTO api_latency_log (request_id, method, path, status, duration_ms,
    repo_key, user_id, source) VALUES (?,…,?, 'backend')`（8 列，created_at 由列默认值给）；
  - `agent_tasks`（归 runtime）× 3：原行 439 readiness 表探活 `SELECT COUNT(*) FROM
    agent_tasks`；行 502 `/health/realtime` 状态分布 `SELECT status, COUNT(*) … GROUP BY
    status`；行 504 在制计数 `WHERE status IN ('dispatched','running')`；
  - `agent_runtimes`（归 runtime）× 1：原行 506 全量引擎行 `SELECT status,
    last_seen_at, credential_id`（在线判定由调用方 `derive_runtime_health` 做）。
  五处都是**固定 SQL 文本**（无动态拼接、无跨域 JOIN、无 IN 列表展开），调用点全部是
  `db.cursor()` / `db.execute()` 直发 api 进程主库。
- owner 侧新增（`?` 占位符，列序/谓词/source 字面量与原 SQL 逐字一致）：
  1) insight public：`_LATENCY_INSERT_SQL` + `record_api_latency_any(request_id, method,
     path, status, duration_ms, repo_key='', user_id='')`，走 `_any_cursor(write=True)`；
  2) runtime public：`_ANY_QUERIES` 增 4 个具名查询（`health_task_rows_total` /
     `health_task_status_counts` / `health_task_inflight_count` / `health_runtime_rows`）
     + 4 个薄函数同名导出（`__all__` 同步）。
- 设计取舍：
  1) **不复用 `dashboard_task_status_counts`**：那条语句带 `WHERE 1=1`（repo/org 过滤原语），
     本批是**全量**运维口径且要求逐字对拍原 SQL，另立 `health_task_*` 具名查询，
     避免为复用而改语义；也不复用 `alerts_task_rows_total`（体量告警语义，非 readiness 探活）。
  2) **写口薄函数而非 `exec_any`**：insight public 没有 `_READ_QUERIES` 注册面（A3 门禁只
     认那个 attr），走具名查询会在 `check_lib_query_names` 报未登记；且既有
     `record_api_latency` 就是函数形态。故用薄函数，SQL 常量留本域。
  3) **保留 readiness 第一段 `SELECT 1`**：那是「进程自身主库连通」探活，不涉任何表，
     仍用 `session_manager.db` + `db_lock`；只有表存在性检查换成 runtime 原语 ——
     两者是两个不同断言（连不上 vs 表缺失），不能合并。
  4) **realtime 段放弃单游标快照**：原来三条语句共用一个 `with db_lock` + cursor。改为
     三次 owner 原语调用（各自 `_any_cursor`）—— 三条是独立统计，无事务/快照要求；
     单库拓扑下 `_any_cursor` 仍拿同一 `db_lock`（RLock 重入安全），不引入新锁序。
  5) **慢请求失败降级保留、但不再静默**：原 `except: pass` 改成 `log.debug(f"…skipped: …")`，
     入库失败依旧不影响请求（口径不变），但三库拓扑下的丢数可在日志看见。
     `tests/silent_except_baseline.json` 中该文件计数 4 → 3（修掉的吞噬点同步下调）。
- 修的存量 bug（三库拓扑，运维面全黑）：
  1) **readiness 恒 `not_ready` / 503**：`SELECT COUNT(*) FROM agent_tasks` 在 api 进程库
     1146（表在 runtime 库，视图 0）→ `checks["tables"]="error: agent_tasks missing"`、
     `status="not_ready"`、HTTP 503。K8s/nginx 的 readiness 探针把**健康进程判成未就绪**，
     四进程拆分后整个集群的就绪门形同永久失败；
  2) **`/health/realtime` 恒 `db_error`**：同一张表的三条语句一起失败，`tasks` /
     `in_progress` / `runtimes` 三个字段永久缺失 —— 运维看不到任务队列深度与引擎在线数；
  3) **慢请求指标在三库拓扑整体丢失**：`INSERT INTO api_latency_log` 打 api 进程库 1146，
     被 `except: pass` 吞掉，`/api/latency/logs` 与 Grafana 延迟面板在独立部署下**零样本**，
     且日志里没有任何痕迹（本次降级改为带 debug 日志）。
- 验证：`py_compile` 3 个文件 **COMPILE_OK**；SQLite 烟测
  `~/burndown/server_common_health_smoke.py` **ALL 71 CHECKS PASSED**（含边界与否定用例：
  5 条语句与改造前 SQL 逐字对拍 + 无 `%s`/`NOW()`/`ON DUPLICATE`/`datetime('now'`；
  写口 8 列取值、`source` 恒 `backend`、`created_at` 走列默认值、同请求两次**不去重**、
  None → 空串；状态分布含 NULL 组且不按 repo 过滤、在制只认 dispatched/running；
  引擎行 3 列含 status NULL 与 offline 全量，派生口径 online/recently_lost/offline 逐条对拍；
  缺 agent_tasks → 探活抛异常、空表 → 0/[] 不抛；端到端 TestClient：`/healthz` 200/503 两态、
  `/health/realtime` 数值、慢请求真实落到 owner 库且 api 进程库零写入；单库走进程库、
  自有 DSN 两侧零串库；源码面 0 条表引用、只剩 `SELECT 1`、中间件无 `except: pass`）；
  `bash scripts/rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK / 契约 37 条 /
  看板最新）；`pytest tests/arch tests/contracts tests/scripts` **158 passed**；
  `scripts/check_lib_query_names.py src` **PASS**（insight 12/20、mgmt 160/237、runtime 152/226）；
  `make lint` **import OK**。
- 新基线：全局 **143 → 138**（-5），对数 **50 不变**：`api_latency_log←api` 1 → 0、
  `agent_tasks←api` 4 → 1、`agent_runtimes←api` 3 → 2（两个表在 api 还有 3 处，
  集中在 `src/api/health_endpoints.py`：agent_tasks 3 + agent_runtimes 1，同模式、下一批候选）。
  基线 `foreign-refs-allowlist.csv` 收紧为 138 / 50，依赖看板与作业单同步刷新
  （下一批头名：`src/api/feedback_status.py` 5 处，并列 `src/api/a2a_dispatch_guard.py` 5 处）。

### 7.22 api 反馈状态联动拆跨域 JOIN（2026-11-15 本批）

- 文件：`src/api/feedback_status.py`（api 域；#673 反馈状态口径 + #22 通知复用的联动同步），
  **5 处 → 0**（单轮做完，5 处未超 25，未分轮）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结）：
  - `user_feedback`（归 mgmt）× 4：原行 152 外部联动待同步页
    `SELECT f.id, f.status, f.repo_key, f.issue_number, f.issue_url FROM user_feedback f
    WHERE f.source='user' AND f.issue_url!='' AND (f.issue_id IS NULL OR f.issue_id=0)
    AND f.is_deleted=0 AND f.status NOT IN ('closed') AND f.id > ? ORDER BY f.id LIMIT ?`；
    行 174 外部联动单行回写 `UPDATE user_feedback SET status=? WHERE id=? AND is_deleted=0`；
    行 204 内部联动 JOIN 左表 `SELECT f.id, f.status, i.stage, i.status FROM user_feedback f
    JOIN issue i ON i.id = f.issue_id WHERE f.issue_url!='' AND f.issue_id IS NOT NULL
    AND f.is_deleted=0 ORDER BY f.id LIMIT ?`；行 217 内部联动单行回写
    `UPDATE user_feedback SET status=? WHERE id=?`（**无** is_deleted 守卫）。
  - `issue`（归 runtime）× 1：行 205 上面那条 JOIN 的右表取列
    （`i.stage` / `i.status`，issue 侧原语句**零谓词**）。
  五处都是固定 SQL 文本（无动态表名/列名），调用点全部是 `db.cursor()` / `db.execute()`
  直发 api 进程主库。
- owner 侧新增：
  1) mgmt public `_ANY_QUERIES` 4 条（`?` 占位符，谓词/列序/ORDER BY 逐字照抄）：
     `feedback_external_sync_rows`（读页，保留原有 `f.id > ?` 游标锚点）/
     `feedback_external_mark_status`（带 `is_deleted=0`）/
     `feedback_internal_sync_page`（左表页，拆分新增 `f.id > ?` 键集锚点）/
     `feedback_internal_set_status`（原语句无守卫，照抄）；
  2) mgmt public 4 个薄函数并导出：`feedback_external_sync_page(after_id, limit)` /
     `feedback_mark_status_if_live(fid, status)`（`exec_any` 自提交）/
     `feedback_internal_sync_page(after_id, limit)` / `feedback_apply_statuses(pairs)`
     （`_any_tx()` 单事务批量回写）；
  3) runtime public：`issue_stage_status_rows(ids, chunk=500)`——按 id 分块 `IN` 取
     `(id, stage, status)`，导出面同步。
- 设计取舍：
  1) **跨域 JOIN 不下沉也不建视图**：`user_feedback ⋈ issue` 跨 mgmt / runtime 两库，
     拆成「mgmt 取左表页 + runtime 按 id 取右表列 + 调用方 Python 内连接」，三段都是
     已登记原语；issue 侧零谓词（原 JOIN 不过滤软删/子任务），右表查询照抄不加。
  2) **LIMIT 仍按内连接后的行数计**：原语句的 `LIMIT` 作用在 JOIN 结果集上，所以调用方
     按「已匹配行数」累计分页——右表缺行**不占**窗口，只升不降的相等行**占**窗口。
     这两种相反口径在烟测里各有一条用例钉住（`右表缺行不占窗口`/`相等行占窗口`），
     避免被后来人"顺手简化成先取 N 行左表再过滤"。
  3) **写口提交粒度照抄原实现，不统一**：外部联动原来逐行 `with db_lock: UPDATE; commit`，
     故用 `exec_any` 薄函数保持自提交；内部联动原来整段一次 `commit`，故用 `_any_tx()`
     批量回写保持单事务原子（异常整体回滚，不留半批）。两者形态不同是有意为之。
  4) **两条 UPDATE 不合并**：`is_deleted=0` 守卫只出现在外部联动那条上，内部联动原语句
     没有。合成一条带参数开关的语句会让守卫语义藏进参数里，故分别登记。
  5) **业务口径全部留在 api**：状态推导（stage/任务态映射）、只升不降的 rank 比较、
     外部同步游标轮转与回绕、`limit` 默认值 200 都没下沉 —— owner 域只给数据与回写，
     语义仍在调用方，#22 通知机制复用同一函数不受影响。
  6) **跨后端 id 归一**：右表 id 与 `f.issue_id` 用 `str()` 对齐后匹配。MySQL 两列同为
     整型（其比较本就等价），sqlite 下可覆盖 TEXT/INTEGER 混存的历史行。
  7) `int(limit)` 传入前先判 `<=0` 直接返回：原 `LIMIT 0` 结果集为空，拆页后若不判
     会陷入「零行页循环」。
- 修的存量 bug（三库拓扑，反馈状态面）：
  1) **外部 Issue 关闭永不回流**：`sync_external_feedback_statuses` 首条 SELECT 在 api
     进程库 1146（`user_feedback` 在 mgmt 库，视图 0）→ 顶层 `except` 吞成「0 条更新」。
     user 反馈转换出的外部 GitCode Issue 关闭后，反馈卡片永久停在 open/resolved，
     只能 admin 手工关；
  2) **内部映射反馈状态不再跟随 issue**：`sync_feedback_statuses` 的跨域 JOIN 同样
     1146 → 0 条更新，已转换反馈的 stage/任务态推进（coding→in_progress、done→resolved、
     closed→closed）在独立部署下整体失效，`#673` 的「一套口径」只剩读侧；
  3) 两处降级只留 `log.exception`、返回值 0，reconcile 调度把「故障」和「本来就没
     待同步行」混成同一个数（本次保持返回值语义不变，但正常拓扑下不再触发）。
- 验证：`py_compile` 3 个文件 **COMPILE_OK**；SQLite 烟测
  `~/burndown/feedback_status_smoke.py` **ALL 96 CHECKS PASSED**（含边界与否定用例：
  4 条语句与改造前 SQL 逐字对拍 + 无 `%s`/`NOW()`/`ON DUPLICATE`/`datetime('now'`；
  外部选择面五类行——非 user 来源 / issue_url 空 / 有内部映射 / 软删 / 已 closed——
  零调用零更新；外部 open 不回退 resolved；limit=1 多轮轮转覆盖 3 行且末轮回绕 0；
  整页推进游标停在本页最大 id；limit=0 → 0 且不发起外部拉取；软删行回写 0 影响；
  内部联动 stage/任务态覆盖/NULL stage/issue status=closed 参与推导/未转换与无
  issue_id 不入选；**右表缺行不占窗口**与**相等行占窗口**两条 LIMIT 口径；
  issue 取列 1200 id 分 3 批(500/500/200)、重复 id 只回 1 行、None 元素丢弃；
  缺 user_feedback → 两函数均 0 不抛且零外部调用、缺 issue → 内部 0 不抛而外部照常；
  空表 → 0/[]；单库拓扑进程库上同时出现两表 SQL、自有 DSN 时 mgmt 连接读 user_feedback
  /runtime 连接读 issue/进程库零 SQL；源码面 0 条表引用与 SQL 字面量、无 cursor()/
  execute() 直发、不 import session_manager、无静默 except、只用 5 个已登记原语且
  均已导出）；
  `tests/mgmt/test_feedback_status_sync.py` **13 passed**（既有用例无回归）；
  `pytest tests/arch tests/contracts tests/scripts tests/mgmt/test_feedback_status_sync.py`
  **171 passed**（同会话混跑，见下条）；
  `rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK / 契约 37 条 /
  看板最新）；`pytest tests/arch tests/contracts tests/scripts` **158 passed**；
  `make lint` **import OK**。
- 配套修的测试隔离 bug（本批暴露）：`tests/contracts/test_issue_699_phase5_contracts.py`
  的 `temp_db` fixture 在临时库上 `reload(session_manager)`，teardown 的第二次 reload
  却仍在本 fixture 设的 `DEVOPS_DB_PATH` 下执行 → 该模块被永久绑在只有 5 张表的临时库。
  旧代码因 `feedback_status` 导入期就抓住 `db` 而侥幸不炸；本批改走 owner 域原语后，
  `_any_cursor` 懒解析当前 `session_manager` 才暴露：同会话跑
  `tests/contracts + tests/mgmt` 时 5 条联动用例报 0 条更新（在 `69f0242ce` 干净
  worktree 上对拍为 52 passed，确认是本批新增的顺序敏感性）。修法：teardown 先
  `monkeypatch.undo()` 再 reload，恢复真实配置，不碰生产语义。
- 新基线：全局 **138 → 133**（-5），对数 **50 不变**：`user_feedback←api` 6 → 2、
  `issue←api` 4 → 3（两表在 api 还有 5 处集中在其它的 api 文件，同模式、后续批次）。
  基线 `foreign-refs-allowlist.csv` 收紧为 133 / 50，依赖看板与作业单同步刷新
  （下一批头名：`src/api/a2a_dispatch_guard.py` 5 处）。

### 7.23 api A2A 派发门禁读路径归位 runtime（2026-11-15 本批）

- 文件：`src/api/a2a_dispatch_guard.py`（api 域；#1770 派发去重门禁 + #1962 手动补发排除），
  **5 处 → 0**（单轮做完，5 处未超 25，未分轮）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结）：
  - `events`（归 runtime）× 4：原行 132 下游/排队活跃事件面
    `SELECT seq, agent_type, status, created_at, scene_id FROM events WHERE repo_key=?
    AND issue_id=? AND event_type='a2a_event' AND status IN (…) ORDER BY seq DESC LIMIT 50`；
    行 241 时间窗去重候选 `SELECT seq, created_at, status, scene_id … AND agent_type=?
    ORDER BY seq DESC LIMIT 20`；行 293 活跃重复候选
    `SELECT seq, created_at, status, scene_id, payload … AND agent_type=? AND status IN (…)
    ORDER BY seq DESC LIMIT 20`；行 356 storm 派发计数
    `SELECT created_at FROM events … AND agent_type=? ORDER BY seq DESC LIMIT 50`。
  - `event_skipped`（归 runtime）× 1：行 362 storm 跳过计数
    `SELECT created_at, detail FROM event_skipped WHERE repo_key=? AND issue_id=?
    AND event_type='a2a_event' ORDER BY id DESC LIMIT 100`。
  五处都是固定 SQL 文本（无跨域 JOIN、无动态表名/列名）；唯二的动态成分是状态**值**列表
  （`_ACTIVE_STATUS` / `("queued",)`）与行数上限，调用点全部是 `db.cursor()` / `execute()`
  直发 api 进程主库。
- owner 侧新增（`src/domains/runtime/public/__init__.py`，`?` 占位符）：
  1) `_ANY_QUERIES` 3 条具名查询：`a2a_window_dedup_rows` / `a2a_storm_event_times` /
     `a2a_storm_skip_rows`（列序/谓词/排序/LIMIT 逐字照抄）；
  2) `_A2A_EVENT_QUERIES` 2 条 `{ph}` 模板（`a2a_active_event_rows` /
     `a2a_active_dup_rows`）+ `_a2a_event_stmt()` 展开器，状态值过 `_A2A_EVENT_STATUSES`
     白名单后去重保序，LIMIT 作为绑定参数；
  3) 5 个薄函数同名导出（`__all__` 同步）+ `A2A_ACTIVE_STATUS` 常量。
- 设计取舍：
  1) **动态 IN 用 owner 侧 `{ph}` 模板，不用调用方拼 SQL**：与 `issue_vcs_*` 同一形态 ——
     语句文本固定在 owner，调用方只能传状态**值**，且值先过白名单（表名/列名永不入参）。
     状态值去重保序，未知值丢弃（原 `IN` 对未知状态本就零命中，口径不变）。
  2) **5 条语句分两形态**：三条完全固定（列序 + 谓词 + LIMIT 都是字面量）走
     `_ANY_QUERIES` + `read_all_any`；两条带状态集合的走 `{ph}` 模板 + `_any_cursor`。
     不为了统一形态把固定语句也塞进模板（白名单面越大越难审）。
  3) **LIMIT 仍留在 SQL 里，不搬到 Python 截断**：20/50/100 是门禁语义的一部分
     （「只看最近 20 行」决定更早的 completed 是否会误判重复），下沉到调用方后一次
     `[:20]` 的疏忽就会改变去重结果，故烟测对三个上限各钉一条用例。
  4) **storm 两条语句仍按「同一 try、失败一起归零」降级**：改成两次独立调用后，
     第二条失败不再让第一条的计数被丢掉 —— 但那会把「表缺失」变成「半份计数」，
     与原实现的可观测口径不一致，故异常分支里显式把 `rows, skip_rows` 一起置空。
  5) **`append_guarded_a2a_event` 的原子段不动**：仍持 `db_lock` 包「复查 → 落库」。
     单库拓扑下 `_any_cursor` 拿同一把 RLock（重入安全）+ 同一进程连接，锁语义与事务
     快照都不变；自有 DSN 拓扑下复查读 runtime 库、`append_event` 写进程库的分裂是
     **存量问题**（event_store 归位尚未做），本批不引入也不扩大。
  6) **业务口径全部留在 api**：状态集合（running/dispatched 算活跃、queued 走 #1667）、
     completed-only 时间窗、scene 空串通配、上下文键比对、`exclude_seq` 精确排除、
     `upcoming` 计入与阈值告警都没下沉 —— owner 只给行与列。
- 修的存量 bug（三库拓扑，A2A 门禁整体失效）：
  1) **下游 running 门禁恒「无下游」**：四条门禁查询在 api 进程库 1146（events 在 runtime 库，
     视图 0）→ 全部被 `except` 吞成空结果，`find_running_downstream` 永返 `{}` —— #1770 的
     防抖退回原状，qa 审查期间上游仍被重复派发、白烧执行槽位；
  2) **时间窗去重失效**：`find_window_duplicate` 永返 `{}` → 未带 `idempotency_key` 的派发
     可落两条（唯一索引只兜带键路径），`duplicate_key` 响应分支永不触发；
  3) **活跃重复判定失效**：`find_active_duplicate` 永返 `{}` → 同 issue+scene+agent 的
     异上下文请求各落一条事件；
  4) **storm 观测恒不告警**：`detect_dispatch_storm` 两条查询同时 1146 → count 只剩
     `upcoming`（1），`count >= 2` 永不成立，「反复跳过」在日志里无痕（本批降级路径保持
     不抛，但正常拓扑下不再触发）。
  遗留（不在本批范围，属 event_store 归位批次）：`append_event` / `append_skipped_event`
  仍走进程主库 `db`，三库拓扑下写入侧仍会 1146 —— 读侧就绪后，该缺口只剩「写」一半。
- 验证：`py_compile` 2 个文件 **COMPILE_OK**；SQLite 烟测 `~/burndown/a2a_guard_smoke.py`
  **ALL 108 CHECKS PASSED**（含边界与否定用例：5 条语句与改造前 SQL 逐字对拍 +
  无 `%s`/`NOW()`/`ON DUPLICATE`/`datetime('now'`/库名前缀；状态白名单丢未知值、去重保序、
  全被过滤时零查询；活跃集合只认 running/dispatched、LIMIT 50 + seq DESC；下游门禁的
  queued/completed 不计、queued 进 `blocking_seqs`、scene 空串通配、`exclude_seq`、
  空 nodes 零查询；时间窗只认 completed、窗口外不命中、**只看最近 20 行**、`window<=0`
  零查询；活跃重复空上下文零查询、异上下文命中/同上下文放过/`include_same_context`
  复查命中、payload 非 JSON 与空对象跳过、幂等键优先、**只看最近 20 行**；storm 的
  detail 精确/前缀匹配、别的 agent 与别的 event_type 不计、LIMIT 50/100、`upcoming<0`
  按 0；缺 events / 缺 event_skipped 双向降级不抛且口径不变；`db_lock` 内调用不死锁、
  8 线程并发结果一致；单库走进程库、自有 DSN 两侧不串库；源码面 0 条 SQL 字面量 /
  0 处外域表名 / 无 cursor()·execute() 直发 / 只绑 `db_lock` / 只用 5 个已导出原语）；
  `tests/runtime/test_issue_1770_a2a_dispatch_guard.py` +
  `test_issue_1962_a2a_time_window.py` **34 passed / 4 failed**，与改造前同一棵树上
  对拍（`git checkout` 前后各跑一次）**失败集合与条数完全相同**，均为用例内真实外呼
  GitCode 拿不到 `owner/repo`（HTTP 404）导致的入口 400，属既有环境依赖；
  `bash scripts/rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK /
  契约 37 条 / 看板最新）；
  `pytest tests/arch tests/contracts tests/scripts` **158 passed**；
  `scripts/check_lib_query_names.py src` **PASS**（insight 12/20、mgmt 164/241、runtime 158/232）；
  `make lint` **import OK**。
- 新基线：全局 **133 → 128**（-5），对数 **50 → 49**：`event_skipped←api` 1 → 0（清零）、
  `events←api` 7 → 3（余 3 处在 `src/api/event_archive.py` 1 + `src/api/qa_gate.py` 2，
  同模式、后续批次）。基线 `foreign-refs-allowlist.csv` 收紧为 128 / 49，依赖看板与作业单
  同步刷新（下一批头名：`src/kernel/config.py` 4 处）。

### 7.24 kernel 登录白名单判定下沉 mgmt owner 域（2026-11-15 本批）

- 文件：`src/kernel/config.py`（kernel 域；登录白名单开关判定 + OAuth 放行判定），
  **4 处 → 0**（单轮做完，4 处未超 25，未分轮）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结）：
  - `login_whitelist`（归 mgmt）× 4：
    行 662 `_is_login_whitelist_enabled` 隐式启用判定 `SELECT COUNT(*) FROM login_whitelist`；
    行 730 `is_user_allowed` fail-closed 判定 `SELECT COUNT(*) FROM login_whitelist`；
    行 738 行式新行命中 `SELECT 1 FROM login_whitelist WHERE provider=? AND LOWER(username)=LOWER(?)`
    （绑定 provider、login）；
    行 746 老列兜底 `SELECT 1 FROM login_whitelist WHERE LOWER({provider}_username)=LOWER(?)`
    （**动态列名**：由调用方 provider 经 `^[a-z0-9_]+$` 校验后 f-string 拼入，kernel 内唯一一处
    拼 SQL 标识符的点）。
  四处都是定表 + 定列（除 746 的列名），无跨域 JOIN、无写语句；调用点全部是
  `with db_lock: db.cursor().execute(...)` 直发 kernel 进程主库。
- owner 侧新增（`src/domains/mgmt/public/__init__.py`，`?` 占位符）：
  1) `_ANY_QUERIES` 2 条具名查询：`login_whitelist_count` / `login_whitelist_row_match`
     （列序、谓词、`LOWER()` 双侧大小写口径逐字照抄）；
  2) 老列模板 `_LOGIN_WL_LEGACY_COLUMN_SQL`
     （`SELECT 1 FROM login_whitelist WHERE LOWER({col})=LOWER(?)`）+ `_LOGIN_WL_PROVIDER_RE`；
  3) 4 个薄函数并导出：`login_whitelist_count()` / `login_whitelist_row_match(provider, login)` /
     `login_whitelist_legacy_column(provider)`（列名唯一产生点）/
     `login_whitelist_legacy_column_match(provider, login)`。
- 设计取舍：
  1) **动态列名只由 owner 域产生**：kernel 只传 provider key，列名（`<provider>_username`）
     在 mgmt public 里派生并过严格标识符白名单（`[a-z0-9_]{1,32}`，与 `provider VARCHAR(32)`
     同口径），调用方字符串永不进 SQL。比原实现更严一格：`^[a-z0-9_]+$` 的 Python `$`
     允许尾随换行，改为 `fullmatch` 后 `"gitcode\n"` 也判非法（影响面为零：provider 来自
     固定渠道 key 集合，非法值本就在 kernel 前置校验里被拒）。
  2) **「物理列不存在」的判定跟着语句一起搬**：`no such column` / `unknown column` → 返回
     False（该渠道无老数据）；其它 DB 异常照原样上抛 —— 保持原实现「内层错=拒绝、外层错=
     降级文件」的两级口径，不把两种分支合并成一个返回值。
  3) **`COUNT(*)` 的不过滤口径原样保留**：不按 provider/username 是否为空筛行，只有老列
     填充的存量行也计入 —— 它是「meta 未设置 → 表有数据则隐式启用」与「启用 + 表空 →
     fail-closed 全拒」两个判定的共同输入，任何 `WHERE is_active=1` 式的"顺手优化"都会改变
     登录行为，烟测对这两条各钉一条用例。
  4) **锁与提交粒度**：三处读取都由 `*_any` 原语内部的 `_any_cursor()` 取锁（单库拓扑是
     可重入 `db_lock` + 同一进程连接，自有 DSN 拓扑是 mgmt 连接），调用方不再自己持锁；
     四次调用是顺序读、无写、无事务合并需求，与原实现「同一游标顺序执行、无显式事务」等价。
  5) **业务口径全部留在 kernel**：开关优先级（meta 显式值 > 旧部署隐式启用）、
     60s TTL 缓存与 `invalidate_login_whitelist_cache()`、空 login 直接拒绝、provider 前置
     正则、文件白名单降级与 fail-closed 一律没下沉；owner 域只给行与列。
- 修的存量 bug（归属前提，本批未在线上观测到故障，属结构性隐患）：
  1) **「进程主库 == owner 库」的隐含前提**：这 4 条语句打在**执行进程的主库**上，而
     `login_whitelist` 只在 mgmt 库存在（`db_setup._create_tables` 的 `_owned_skip` 已按
     owner 过滤，三库独立后别的库不再建同名副本，也没有跨库视图）→ 任何非 mgmt 进程读它
     必 1146。当前 OAuth 登录与 admin 白名单状态只在 mgmt 进程执行，所以线上还看不出来；
     一旦有 event/worker/insight 侧的白名单判定需求，或 mgmt 进程改绑 DSN，就会以
     「默认关闭 / 降级文件」的形式静默失效（`except` 吞异常，日志外无迹）；
  2) **同名空副本的静默口径分裂风险**：单库/多库混跑的过渡拓扑里，若某进程库历史遗留过
     同名表，`COUNT(*)` 与命中查询会读空副本 —— 不报错，但白名单实际不生效（本批改为
     按 owner 域取连接后该路径消失）；
  3) **kernel 侧的 SQL 标识符拼接点清零**：`f"...LOWER({provider}_username)"` 是 kernel
     唯一一处把外部字符串拼进 SQL 的地方，现已挪到 owner 域并加白名单。
- 验证：`py_compile` 2 个文件 **COMPILE_OK**；SQLite 烟测
  `~/burndown/login_whitelist_smoke.py` **ALL 76 CHECKS PASSED**（含边界与否定用例：
  3 条语句与改造前 SQL 逐字对拍 + `?` 方言 / 无 `%s`·`NOW()`·`ON DUPLICATE`·`datetime('now')`·
  库名前缀；`COUNT(*)` 计入只有老列填充的空行；行式命中双侧大小写不敏感、provider 隔离、
  空用户名不命中、provider 为 NULL 的存量老行不进行式命中；老列列名派生 8 类非法 provider
  ——空串/大写/连字符/分号注入/反引号/超长/None/尾随换行——全部 None 且**零 SQL**；
  物理列缺失 → False 且**不回退文件白名单**（原实现的内层 return 口径）、缺表 → 上抛供
  调用方降级；meta 未设置 + 表空/有数据 → 隐式启用 False/True（含 60s 缓存清空后复判）、
  meta 显式 false 优先于表数据、启用 + 表空 → 全拒、开关关闭 → 全放行；DB 不可用时文件
  缺失/空/有条目三种降级口径；单库走进程库、自有 DSN 走 mgmt 连接且进程库零 SQL；
  源码面 kernel 0 条 SQL 字面量 / 0 处外域表名 / 无 `cursor()`·`execute()` 直发 /
  不再 import `session_manager` / 只取 3 个已导出原语 / 静默 except 13 = 基线 13）；
  `tests/mgmt/test_login_whitelist.py` + `test_login_whitelist_row_model.py` +
  `test_login_whitelist_null_literal_cleanup.py` **37 passed / 2 failed**，与 `d742f940c`
  干净树对拍**失败集合完全相同**（`test_login_whitelist_empty_warn_banner` /
  `test_login_whitelist_admin_ui` 读已迁移的 `src/api/static/admin.html`，属既有断链）；
  `tests/api/test_auth_routes.py` 的 `test_redirect_uri_fallback_from_host` 在主 checkout 红、
  基线树绿，把本地未跟踪的 `configs/` 复制进基线树后**同样复现**（`data/test_sessions.db`
  里残留了生产入口的 `meta.server_base_url`），确认是环境漂移非本批回归；
  `bash scripts/rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK / 契约 37 条 /
  看板最新）；`pytest tests/arch tests/contracts tests/scripts` **158 passed**；
  `make lint` **import OK**。
- 新基线：全局 **128 → 124**（-4），对数 **49 → 48**（`login_whitelist←kernel` 4 → 0，
  该表在 kernel 侧清零）。基线 `foreign-refs-allowlist.csv` 收紧为 124 / 48，依赖看板与作业单
  同步刷新（下一批头名：`src/domains/runtime/project_info/builder.py` 4 处 ——
  `agent_shared_knowledge` × 3 + `scene_knowledge_ref` × 1，均归 mgmt）。

### 7.25 runtime project-info 知识落库下沉 mgmt owner 域（2026-11-15 本批）

- 文件：`src/domains/runtime/project_info/builder.py`（runtime 域；project-info 的
  OBS + 知识表落库路径 `_write_to_obs`），**4 处 → 0**（单轮做完，4 处未超 25，未分轮）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结，两表都在 `grape_mgmt_db`）：
  - `agent_shared_knowledge`（归 mgmt）× 3：
    行 315 命中读 `SELECT id FROM agent_shared_knowledge WHERE repo_key=? AND
    scope='baseline' AND name=? AND archived=0`；
    行 324 命中后的刷新 `UPDATE agent_shared_knowledge SET status='accepted', obs_key=?,
    content_hash=?, created_by='system', source='system', name=?, description=? WHERE id=?`；
    行 335 未命中时新建 `INSERT INTO agent_shared_knowledge (scope, repo_key, agent_type,
    issue_num, file_path, status, obs_key, content_hash, created_by, source, name,
    description) VALUES ('baseline', ?, '', 0, ?, 'accepted', ?, ?, 'system', 'system', ?, ?)`。
  - `scene_knowledge_ref`（归 mgmt）× 1：行 347 全局关联
    `INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type,
    issue_num, enabled, created_by) VALUES (?, '', '', 0, 1, 'system')`。
  - 四处都是定表 + 定列，**无跨域 JOIN、无动态表名/列名拼接**；调用点全部是
    `with db_lock: c = db.cursor()` 直发 runtime 进程主库，写入序列是
    「读 → UPDATE/INSERT → commit → 取 lastrowid 补 ref → 再 commit」，内层
    `except Exception: pass` 吞掉 ref 失败。
- owner 侧新增（`src/domains/mgmt/public/__init__.py`，`?` 占位符）：
  1) `_ANY_QUERIES` 4 条具名查询：`knowledge_baseline_id_any` / `knowledge_baseline_refresh` /
     `knowledge_baseline_insert_any` / `knowledge_baseline_ref_ensure`（谓词、列序、常量口径
     与原语句逐字一致）；
  2) 2 个薄函数并导出：`upsert_baseline_knowledge_any(repo_key, display_name, description,
     file_path, obs_key, content_hash) -> {"action","id","changed"}`（**单事务** `_any_tx()`：
     读 id + UPDATE/INSERT 一次提交）与 `ensure_baseline_knowledge_ref_any(knowledge_id) -> int`
     （`_any_tx()` 单语句，`id <= 0` 直接返回 0、**不发 SQL**）。
- 设计取舍：
  1) **事务边界跟着语句一起搬**：原「读 → 改/插」靠进程内 `db_lock` 串行化、跨实例靠
     `project_info_build:<md5>` 分布式锁；改 `_any_tx()` 后自有 DSN 拓扑走
     `db_registry.transaction("mgmt")`（一次组合写一条独立非 autocommit 连接，不碰共享
     连接池），单库/测试拓扑回落进程库 + 可重入 `db_lock`。与原「同一游标顺序执行」等价，
     但调用方不再自己持锁，也不再有「读在 A 库、写在 B 库」的可能。
  2) **两段提交与 ref 降级口径原样保留**：ref 仍是独立一次提交，失败不回滚已提交的知识行
     （原实现就是这样）；只把内层 `except Exception: pass` 换成 `log.debug` —— 静默 except
     棘轮只减不增，`tests/silent_except_baseline.json` 里本文件 7 → 6 同步下调。
  3) **`file_path` 不被 UPDATE 改写**：原 UPDATE 的 SET 里没有 `file_path`（同一 name 即使
     目录结构变了也不迁正文路径），新原语照抄该列集合，烟测对「命中后 file_path 保持旧值」
     单独钉一条用例。
  4) **`archived=0` 谓词与「不加 LIMIT」照抄**：归档行**不算命中**，会另插一行 baseline
     （历史行为，不顺手改成「复用归档行」）；原读语句没有 `LIMIT 1`，靠 `fetchone()` 取首行，
     这里不额外收紧。`agent_shared_knowledge` 没有 `(repo_key, scope, name)` 唯一约束，
     去重仍靠「先读后写」，只是把这两步收进一个事务。
  5) **outbox 优先路径不动**：`_write_baseline_db_via_lib`（`cross_write` 幂等意图 +
     `upsert_baseline_knowledge` / `ensure_scene_knowledge_ref` 两个已登记 lib 函数）仍是第一
     选择，只有 lib 不可用（单库 / 测试拓扑）或提交失败才落到新原语直写 —— 跨域写的首选
     通道（outbox）口径不变，新原语只是兜底侧不再自带 SQL。
- 修的存量 bug（结构性隐患，线上未观测到故障）：
  1) **兜底 SQL 打在执行进程主库**：三库拓扑下 runtime 进程库没有这两张 mgmt 表
     （`db_setup._owned_skip` 按 owner 过滤、A1 已 0 视图），四条语句必 1146 → 被
     `except Exception as e: log.warning("project-info OBS write failed ...")` 吞成一行告警，
     结果「OBS 与本地文件写了，知识行和场景关联没写」：project-info 知识不进注入面，
     而日志只显示 OBS 写失败，看不出根因是跨域缺表。
  2) **半截 lib 读造成读错库/写错库**：原代码在 `mgmt_lib(foreign_only=True)` 可用时用
     `read_one("knowledge_id_baseline")` 从 **mgmt 库**读 id，但随后的 UPDATE/INSERT 仍打在
     **进程主库** —— 命中判断与写入落在两个库上，同一条记录可能「读到命中、却更新了另一库
     的 0 行」。语句整体上移后该分叉消失。
  3) **读后写的并发窗口收窄**：原「读 → 插」两步在进程内只有 `db_lock`、跨实例只有分布式锁
     保护；现在 upsert 是单事务，「查重 + 写入」不再可被同进程其它线程插入中间态。
- 验证：`py_compile` 2 个文件 **COMPILE_OK**；SQLite 烟测
  `~/burndown/project_info_knowledge_smoke.py` **ALL 118 CHECKS PASSED**（含边界与否定用例：
  4 条语句与改造前 SQL 逐字对拍 + `?` 方言 / 无 `%s`·`NOW()`·`ON DUPLICATE`·`datetime('now')`·
  库名前缀 / 占位符个数；新建路径逐列钉死 scope='baseline'·agent_type=''·issue_num=0·
  status='accepted'·created_by/source='system'；ref 的 scene_id=''/agent_type=''/
  issue_num=0/enabled=1/created_by='system'；命中路径保留原 id、不写 file_path、被覆盖的
  status/source/created_by 一致改写；`archived=1` 不算命中且归档行不动、`scope≠baseline`
  不命中、repo_key 隔离、name 精确匹配不走 `LOWER`；ref 同 id 连调 1→0 行数不增；
  `id=0`/`None` **零 SQL**；INSERT/UPDATE 中途抛错整笔回滚（无半行知识、无 ref）；
  ref 缺表抛错但知识行仍在；端到端 `_write_to_obs` 真实落库 + 二次调用幂等 + ref 原语抛错时
  builder 不抛且知识行保留；源码面 builder.py 0 条这两张表的 SQL 字面量 / 0 处 `db_lock` /
  无 `cursor()`·`execute()` 直发 / 不再 import `session_manager` / 只调 2 个已导出原语 /
  静默 except 6 = 新基线）；
  `bash scripts/rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK /
  契约 37 条 / 看板最新）；`pytest tests/arch tests/contracts tests/scripts` **158 passed**；
  `scripts/check_lib_query_names.py src` **PASS**（insight 12/20、mgmt 166/247、runtime 158/232）；
  `make lint` **import OK**。
- 新基线：全局 **124 → 120**（-4），对数 **48 → 47**：`agent_shared_knowledge←runtime` 5 → 2
  （余 2 处在 `src/domains/runtime/engine/service.py`，同模式、后续批次），
  `scene_knowledge_ref←runtime` 1 → 0（该表在 runtime 侧清零）。基线
  `foreign-refs-allowlist.csv` 收紧为 120 / 47，依赖看板与作业单同步刷新（作业单已重算：120 处 / 55 文件；下一批头名
  `src/domains/runtime/engine/service.py` 4 处 —— `grape_accounts` × 2 + `agent_shared_knowledge`
  × 2，均归 mgmt）。

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
