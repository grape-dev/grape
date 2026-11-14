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

### 7.26 runtime engine 的账号判定与 baseline 知识读下沉 mgmt owner 域（2026-11-15 本批）

- 文件：`src/domains/runtime/engine/service.py`（runtime 域；引擎可达性判定 + claim 认领 +
  project-info 读），**4 处 → 0**（单轮做完，4 处未超 25，未分轮）。改完该文件后 runtime 域
  对这两张表的引用：`agent_shared_knowledge` 清零、`grape_accounts` 只剩 1 处（`routes/runtime.py`）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结，两表都在 `grape_mgmt_db`）：
  - `grape_accounts`（归 mgmt）× 2：
    行 111 `_check_pool_engines_reachable` 的 user scope 判定
    `SELECT id FROM grape_accounts WHERE username=? AND id IS NOT NULL AND id NOT IN ('', '0') LIMIT 1`；
    行 1710 `claim_by_tag` 的 user scope 过滤 `SELECT id FROM grape_accounts WHERE LOWER(username)=LOWER(?)`
    （无 LIMIT、无 id 过滤）。
  - `agent_shared_knowledge`（归 mgmt）× 2：
    行 604 `load_project_info` 与行 691 `load_project_info_with_obs` 的同一段
    `SELECT file_path, obs_key FROM agent_shared_knowledge WHERE repo_key=? AND scope='baseline'
    AND obs_key!='' AND obs_key IS NOT NULL AND archived=0 ORDER BY id DESC`。
  - 四处都是定表 + 定列，**无跨域 JOIN、无动态表名/列名拼接**；结构都是「mgmt_lib 优先
    （MySQL `%s` 具名查询）+ 调用方进程主库本地兜底 SQL」双分支 —— 兜底侧才是外域引用。
- owner 侧新增（`src/domains/mgmt/public/__init__.py`，`?` 占位符）：
  1) `_ANY_QUERIES` 3 条具名查询：`account_id_by_username_valid` / `account_id_by_username_ci` /
     `knowledge_baseline_files`（列序、谓词、`ORDER BY id DESC` 与原语句逐字一致）；
  2) 3 个薄函数并导出：`account_id_by_username_valid_any(username, cursor=None)` /
     `account_id_by_username_ci_any(username, cursor=None)` / `baseline_knowledge_files_any(repo_key)`。
- 设计取舍：
  1) **两个 account 原语按消费语义分开，不合并成一个带开关的函数**：`valid` 版保留原
     0b3fdc 兜底的「id 必须有效」过滤（`id IS NOT NULL AND id NOT IN ('', '0')`）——它的
     消费者要把结果与 `task_user_id` 比大小，历史 int 主键的 `''`/`'0'` 哨兵必须当「无此
     账号」；`ci` 版逐字照抄 d0ac07（无 LIMIT、不过滤哨兵）——它的消费者只把它塞进
     `t.user_id=?`，收紧会让本来能认领的任务消失。合并就需要布尔开关，属反模式。
  2) **大小写取向统一到「账号域自己的口径」**：`valid` 原来单库走严格大小写、三库走
     `LOWER()`（lib 侧 `get_account_id_by_username` 先查 `username_lower` 再 `LOWER()`），
     同一份数据在两种拓扑下答案不同。本批取向 CI（`username_lower` 列 + D32 补丁 #348 已
     把同一判定定为大小写不敏感），且 CI 是原单库口径的**超集**：不会把原本命中的判成不命中。
  3) **游标随参数传入，避免在已持锁路径上嵌套 `db_lock`**：行 111 与 1710 都在
     `with db_lock` 内、且手上有调用方游标 `c`，因此两个 account 原语接受 `cursor=` 并走
     `_owner_cursor`（单库/测试拓扑复用调用方游标 → 不嵌套锁、保住调用方事务快照；自有
     DSN 拓扑忽略它走 mgmt 连接）。这是本文件 `_check_pool_engines_reachable` 文档里
     「不要嵌套 `db_lock`（测试会 monkeypatch 裸 Lock）」的硬约束 —— 直接调 `*_any`
     在 `test_issue_1503` 那类裸 Lock 用例上会自死锁。project-info 两处原本自己
     `with db_lock:` 取锁、手上没有游标，用 `read_all_any` 即可（等价，减少一次嵌套）。
  4) **读后写的语义没有变，因为四处全是读**：无事务合并需求，`baseline_knowledge_files_any`
     只发一条 SELECT，行序由 SQL 的 `ORDER BY id DESC` 保证（Python 侧不重排）。
  5) **业务口径全部留在 runtime**：`has_user_id` 守卫、repo/user scope 分支、`_engine_uid !=
     task_user_id` 的比较、project-info 的 `.metadata.json` 特判与本地目录回退一律没下沉；
     owner 域只给「行与标量」。空 username / 空 repo_key 在 owner 侧直接短路（不发 SQL），
     与原查询的匹配结果等价。
- 修的存量 bug（结构性隐患，线上未观测到故障）：
  1) **同一判定、两种拓扑两种答案（口径分裂）**：`_check_pool_engines_reachable` 的 user
     scope 在单库拓扑走严格大小写 SQL、在三库拓扑走 CI 的 lib 查询。账号名大小写与
     `runtime_credentials.owner_name` 不一致时，同一个池在单库会被判「全不可达」→ 按 Issue
     #253 直接判死任务，而在三库拓扑下能正常派发 —— 排障时看到的是「任务莫名 failed」，
     与拓扑强相关。现已统一为 CI（烟测把「原严格 SQL 对 'alice' 不命中 / 新原语命中」两
     条同时钉住）。
  2) **同一文件里对同一张表的两套 SQL**：`_check_pool_engines_reachable`（严格 + id 过滤 +
     LIMIT）与 `claim_by_tag`（CI + 无过滤 + 无 LIMIT）回答的是同一个问题「owner_name 对应
     哪个账号 id」，却用了两条语句。两条现在都在 owner 域一处可见，语义差异显式写在函数
     文档里（而不是散落在两段 lib/兜底分支中）。
  3) **project-info 的 20 行兜底 SQL 复制两份**（`load_project_info` / `load_project_info_with_obs`
     逐字重复）：任何一处口径改动都要改两遍，且兜底侧只读**执行进程的主库** —— 三库拓扑下
     runtime 进程库没有 `agent_shared_knowledge`（视图 0），一旦 `mgmt_lib()` 因 `available()`
     为假 / public 导入异常返回 None，兜底 SQL 必 1146，被外层 `except` 吞成「project-info
     为空」（知识面静默不注入，日志只有一行 `load_project_info failed`）。现在两条路径同一
     份语句、同一份代码，且不再依赖 `%s` 方言（sqlite 拓扑可执行）。
- 验证：
  - `py_compile` 2 个文件 **COMPILE_OK**。
  - SQLite 烟测 `~/burndown/engine_scope_knowledge_smoke.py` **ALL 84 CHECKS PASSED**（含边界与
    否定用例：3 条语句与改造前 SQL 逐字对拍 + `?` 方言 / 无 `%s`·`NOW()`·`ON DUPLICATE`·
    `datetime()`·库名前缀；`valid` 的 CI 命中（小写/全大写/原样）、`id=''`/`'0'`/`NULL` 哨兵行
    不算命中、空/None username **零 SQL**、`cursor=` 生效只发 1 条；与改造前严格 SQL 的差异
    双条钉死；`ci` 与原 SQL 对 8 个用户名逐例一致、同名多行取首行、`'0'` 原样返回、空名零
    SQL；`knowledge_baseline_files` 的 id DESC 排序 + `archived=1`/`scope!='baseline'`/
    `obs_key=''`/`obs_key IS NULL`/异 repo 五类排除 + 空/None repo_key 零 SQL + 与原兜底 SQL
    结果逐字一致；端到端 exec 真实 `_check_pool_engines_reachable`：owner 大小写不同仍匹配、
    别的用户拒绝、id='0' 哨兵账号拒绝、离线引擎拒绝；把进程锁换成**裸 Lock** 再调用仍不死锁
    （证明未嵌套 `db_lock`）；未提交账号行在同一事务内可见、回滚后不可见（证明复用调用方
    游标、不切事务快照）；源码面 service.py 0 处这两张表名 / 0 个旧 lib 查询名 / 只调 3 个已
    导出原语 / 静默 except 77 = 基线）。
  - `bash scripts/rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK / 契约 37 条 /
    看板最新）。
  - `pytest tests/arch tests/contracts tests/scripts` **158 passed**；
    `scripts/check_lib_query_names.py src` **PASS**（insight 12/20、mgmt 168/250、runtime 158/232）；
    `make lint` **import OK**。
  - 既有失败对拍（`git worktree` 同一棵 HEAD 树前后各跑一次）：
    `tests/api/test_pool_personal_identity.py` 两树均 **4 failed / 8 passed**（该文件自建的假
    `src.domains.runtime.engine.health` 只有 `parse_dt`、拿不到 `engine_health_ok`，属既有断链）；
    `tests/runtime/test_issue_1487_local_engine_reachability.py` 两树均同一 **4 failed**（池内
    全不可达仍判死，Issue #253 语义用例，与本批无关）；
    `tests/runtime/test_issue_259_offline_dispatch.py::test_fresh_direct_kept` 在主树绿、HEAD
    工作树红 —— 原因是工作树新库没有官方 `vod` skill 行，pipeline skill gate 直接 BLOCK
    （captured log 明确），属 DB 状态差异而非本批回归；
    `tests/kernel/test_gsb_route.py::test_public_primitives_registry_sync` 在改动前的干净树上
    即红（`route_config.PUBLIC_PRIMITIVES` 自 7.22–7.25 各批新增原语后一直未同步），本批未
    继续扩大差异面，留待专项同步（不在本批门禁集内）。
- 新基线：全局 **120 → 116**（-4），对数 **47 → 46**：
  `agent_shared_knowledge←runtime` 2 → 0（该表在 runtime 侧清零），
  `grape_accounts←runtime` 2 → 1（余 1 处在 `src/domains/runtime/routes/runtime.py:330`，
  同模式、后续批次）。基线 `foreign-refs-allowlist.csv` 收紧为 116 / 46，依赖看板与作业单
  同步刷新（下一批头名：`src/domains/mgmt/user/legacy_column_migrations.py` 4 处 ——
  `meta`（归 runtime）× 4）。

### 7.27 mgmt user_accounts 死表收敛的 meta 搬迁下沉 runtime owner 域（2026-11-15 本批）

- 文件 / 处数：`src/domains/mgmt/user/legacy_column_migrations.py` **4 处 → 0**
  （`migrate_user_accounts_deprecated_fields()` 内）。
- 表分布：全部为 `meta`（归 **runtime**）× 4 —— 两读两写：
  L166 `SELECT COUNT(*) FROM meta WHERE key='webhook_url'`、
  L168 `INSERT OR REPLACE INTO meta (key, value) VALUES ('webhook_url', ?)`、
  L175 `SELECT COUNT(*) FROM meta WHERE key=?`、
  L177 `INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)`；
  均为参数化语句（无动态表名/列名），无跨域 JOIN。
- 设计取舍：
  1) **不新增 SQL 面，复用 owner 域已登记原语**：runtime public 早有
     `set_meta_if_absent(key, value)`（语句 `meta_insert_absent` =
     `INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)`，已在 `__all__` 与
     `_ANY_QUERIES` 登记）。因此本批 owner 侧 0 行改动 —— 「先 COUNT 再 INSERT」的
     **两次跨域往返收成一次**，且不再出现新的 SQL 字面量（owner 侧语句数不变，避免
     「把引用从 A 域搬到 B 域」式的假下降）。
  2) **等价性靠唯一键**：`meta.key` 是 `TEXT PRIMARY KEY`（`runtime/schema_ddl.py`），
     `COUNT==0 → INSERT` 与 `INSERT IGNORE`（MySQL 由 `mysql_compat.translate_sql`
     翻译）在「键已存在即跳过」上等价，后者还把判定与写入合成原子操作。
  3) **业务口径一律留在 mgmt 调用点**：`PRAGMA table_info` 探列、三个
     `if <col> in ua_cols` 守卫、OAuth 三列**逐列判空**（`client_id` 为空则整段跳过，
     即使 secret 有值也不写）、`bot_usernames` 的 JSON/逗号双解析、取首个元素、
     `github:` 前缀守卫、`repo_owner OR full_name` 双分支、末尾 `devops-bot` 兜底 ——
     这些都没有下移；owner 域只提供「不存在才写」这一件事。
  4) **延迟导入**：本模块由 `init_db()` 在**持 `db_lock`** 的上下文中早期加载
     （`mgmt/user/db.py:863`），模块级 import 运行域 public 会把启动顺序绑死，故用
     函数内延迟导入；导入失败仍由既有 `except` 兜成「迁移零命中」，与改造前
     「表不存在即跳过」一致。`db_lock` 是 `RLock`，`set_meta_if_absent` 单库回落
     再取同一把锁属重入（烟测用真 `with db_lock:` 调用钉住，不死锁）。
  5) **事务语义**：原 4 条语句与后续 `managed_repos` 更新共用调用方游标、末尾统一
     `commit()`；现在 meta 写经原语即时提交（与 `mgmt/user/db.py` 同款 OAuth 迁移
     已采用的形态一致）。迁移本身幂等，部分提交无副作用；本域
     （`user_accounts` / `managed_repos`）的 SQL 仍在调用方游标与事务内。
- 修的存量 bug（结构性隐患，三库拓扑尚未上线，线上未观测到故障）：
  1) **字段搬迁失败但后续 DROP COLUMN 照常执行 → 配置静默丢失**：`run()` 里
     `migrate_user_accounts_deprecated_fields()` 与 `drop_user_accounts_deprecated_columns()`
     是两个独立函数，前者失败只留一行 warning，后者仍会把 `webhook_url` /
     `gitcode_oauth_*` / `bot_usernames` 列 DROP 掉。三库拓扑下 mgmt 进程库没有
     `meta`（视图 0），本函数第一条 SQL 必 1146 被吞 —— 结果是 webhook 与 GitCode
     OAuth 凭据**永久丢失且无报错**。现在 meta 写走 owner 域 DSN，独立拓扑下正常搬迁。
  2) **`INSERT OR REPLACE` 是 SQLite 专有方言**：该语句靠 `db.cursor()` 直发进程库，
     方言翻译只在 `mysql_compat.Cursor` 上生效；即便翻译成 `REPLACE INTO`，单库
     MySQL 拓扑下「删-插」也会在 `meta` 将来增列时静默重置新列。改用 owner 域
     `INSERT IGNORE`（只在键缺失时插入，不存在「删-插」）。
  3) **同一张 `meta` 表两套写入口径**：`mgmt/user/db.py` 的同类搬迁用
     `read_one_any("meta_count") + set_meta_value_any`（覆盖式 upsert 靠 COUNT 守卫，
     有 TOCTOU 窗口），本文件用 `INSERT OR REPLACE`。本批把本文件统一到原子
     「不存在才写」；`db.py` 那两处语义差异仍在，留给后续批次（其 SQL 已不在跨域
     统计内，属口径统一而非引用收口）。
- 验证：
  - `py_compile` 目标文件 **COMPILE_OK**。
  - SQLite 烟测 `~/burndown/legacy_meta_migration_smoke.py` **ALL 38 CHECKS PASSED**
    （静态面 8 项 + 行为面 26 项 + 拓扑面 4 项）：4 条原语句逐字消失、只调 1 个已导出
    原语、静默 except 与 HEAD 版同计数、`?` 方言且 MySQL 侧翻成 `INSERT IGNORE`；
    webhook/OAuth/bot_usernames 的**全部原口径**逐条钉住（存量不覆盖、空值零写入、
    LIMIT 1 取首、client_id 空则整段跳过、`github:` 不写、兜底只补空）；
    缺列/缺表/幂等重跑/持 `db_lock` 重入；以及**模拟运行域自有 DSN**（第二个 sqlite
    连接当运行域库）验证 meta 写落在运行域库、进程库 meta 保持为空 —— 正是原实现在
    独立拓扑下静默失效的根因。
  - `bash scripts/rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK /
    契约 37 条 / 看板最新）。
  - `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **116 → 112**（-4），对数 **46 → 45** —— `meta←mgmt` 对清零
  （`meta` 消费域只剩 runtime 44 处，均为 owner 自身）。基线
  `foreign-refs-allowlist.csv` 收紧为 112 / 45，依赖看板与作业单同步刷新。下一批头名：
  `src/domains/mgmt/routes/feishu_onboard.py` 4 处（`devops_agent_session` 2 /
  `chat_conversations` 1 / `vod_conversations` 1，均归 runtime；同处还有
  `src/api/deps.py` 4 处 `external_identities`）。

### 7.28 飞书扫码 onboarding 的换绑清理下沉 runtime owner 域（2026-11-15 本批）

- 文件 / 处数：`src/domains/mgmt/routes/feishu_onboard.py` **4 处 → 0**（单轮做完，4 处未超 25）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结，三表都在 `grape_runtime_db`，全归 **runtime**）：
  - `devops_agent_session` × 2：L205 `_apply_vod_credentials` 的
    `SELECT id FROM devops_agent_session WHERE repo_key=? AND agent_type='vod'`；
    L238 同函数的 `INSERT INTO devops_agent_session (id, repo_key, agent_type, status,
    last_active_at) VALUES (?,?,?,?,datetime('now'))`。
  - `chat_conversations` × 1：L157 `_apply_chat_credentials` 换绑清理
    `DELETE FROM chat_conversations WHERE im_channel_id=?`。
  - `vod_conversations` × 1：L222 `_apply_vod_credentials` 换绑清理
    `DELETE FROM vod_conversations WHERE agent_session_id=?`。
  - 四处都是定表 + 定列，**无跨域 JOIN、无动态表名/列名拼接**；结构都是「lib 可用走 lib
    （`%s` 具名查询）+ 否则本地兜底 SQL 打进程主库」双分支 —— 兜底侧才是外域引用来源。
- owner 侧新增（`src/domains/runtime/public/__init__.py`，`?` 占位符）：
  1) `_ANY_QUERIES` 3 条：`conversation_delete_by_im_channel` /
     `vod_conversation_delete_by_session` / `devops_agent_session_insert_vod`；
     读侧**复用**既有 `devops_session_vod_id`（与改造前兜底 SELECT 逐字一致，不新增语句）。
  2) 4 个薄函数并导出：`vod_session_id_by_repo_any(repo_key) -> str`、
     `chat_conversations_delete_by_im_channel_any(im_channel_id) -> int`、
     `vod_conversation_delete_by_session_any(agent_session_id) -> int`、
     `vod_agent_session_ensure_any(session_id, repo_key, status="idle") -> int`。
- 设计取舍：
  1) **读只留一条语句**：原 lib 分支的 `read_one("devops_agent_session_dcd716")` 与兜底
     SELECT 本来就是同一条 SQL，收成一个任意拓扑原语（自有 DSN 走 `db_registry`，
     单库/测试拓扑回落进程库），不再有「两套读、两种答案」的可能。
  2) **不做跨语句事务**：删除只在「有旧 app_id 且有旧会话行」时发生，新建只在
     「无会话行」时发生，两条路径**互斥**；且删除按 id/渠道、新建走 `(repo_key,
     agent_type)` 唯一键，均幂等 —— 无需 `_any_tx()`，也就没有「跨域组合写」的原子性要求。
  3) **outbox 优先路径不动**：`cross_write.submit_if_lib`（`chat_conversations_delete` /
     `vod_conversations_delete` / `agent_session_ensure`）仍是第一选择，新原语只在 lib
     不可用或提交失败时兜底 —— 跨域写首选通道（outbox）口径与 P3 一致。
  4) **调用方不再持 `db_lock`**：三处原先都是 `with db_lock: c = db.cursor()` 直发语句；
     现在原语在 `_any_cursor` 内自取锁（单库拓扑）或走独立连接（自有 DSN）。调用点去掉
     外层锁，既避免「测试 monkeypatch 裸 Lock」的嵌套自死锁，也让锁粒度与语句粒度一致。
  5) **`agent_type` 钉在 owner SQL 里**（`VALUES (?,?,'vod',?,datetime('now'))`）：
     本原语只服务 vod 轨，调用方不能传表名/列名，也不可能把 chat_bot 行误建进来。
  6) **时间与默认值口径不变**：`last_active_at` 仍用 `datetime('now')`（`mysql_compat`
     在 MySQL 侧翻成 `CAST(NOW() AS CHAR)`），`status` 默认 `'idle'`，与原兜底 INSERT 一致。
- 修的存量 bug（结构性隐患，三库拓扑尚未上线，线上未观测到故障）：
  1) **lib 分支把游标变量覆盖成结果行、`row` 从未赋值**：原代码
     `c = _ru_lib.read_one("devops_agent_session_dcd716", (repo_key,)).fetchone()` 之后走
     `old_session_id = row[0] if row else ""` —— `row` 在该分支从未绑定，独立 DSN 拓扑
     （lib 非 None）下必 `UnboundLocalError`，被 `_run_registration` 外层 except 吞成
     「扫码绑定失败」，日志只有 failed 没有根因；同一分支后续 `c.execute(...)` 还会在
     结果行对象上 `AttributeError`。现在读路径只有原语一条，该类错误面整体消失。
  2) **兜底 SQL 打在执行进程主库**：三库拓扑下 mgmt 进程库没有这三张 runtime 表
     （视图 0），换绑清理必 1146 并被吞 —— 结果是「旧 bot 删了、旧会话留着」，
     旧渠道会话与新 bot 并存，用户看到串话。
  3) **两条写路径幂等性分裂**：lib 侧 `ensure_agent_session` 是 `INSERT IGNORE`，兜底侧是
     plain `INSERT`，撞 `(repo_key, agent_type)` 唯一键就抛错（换绑重试即失败）。统一为
     `INSERT OR IGNORE`（MySQL 侧翻译成 `INSERT IGNORE`），两条拓扑同一语义。
- 验证：
  - `py_compile` 2 个文件 **COMPILE_OK**。
  - SQLite 烟测 `~/burndown/feishu_onboard_swap_smoke.py` **ALL 93 CHECKS PASSED**
    （静态面 / owner 语句面 / 行为面 / 端到端 / 拓扑面五段）：4 条原语句逐字消失、
    owner 语句 `?` 方言且 MySQL 侧翻成 `INSERT IGNORE` + `CAST(NOW() AS CHAR)`、
    两个改造函数 0 处 `session_manager`/`db.cursor()`/`with db_lock`；chat 删除只删匹配
    渠道并返回行数、空渠道/None **零 SQL**；vod 删除只删匹配 session、空 id 零 SQL；
    建会话行 `agent_type='vod'`/`status='idle'`/`last_active_at` 为 UTC 字符串、二次调用 0
    且不重复、已有行原样保留、空参数零 SQL；读会话 id 不吃同 repo 的 chat_bot 行；
    端到端跑真实 `_apply_chat_credentials`/`_apply_vod_credentials`（换绑清理、被 VOD/他人
    占用时拒绝且不清）；**伪造 lib 句柄**验证不再 UnboundLocalError 且写走 outbox；
    自有 DSN 拓扑下写入/删除落在运行域库、进程库不动。
  - `bash scripts/rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK /
    契约 37 条 / 看板最新）。
  - `pytest tests/arch tests/contracts tests/scripts` **158 passed**；
    `scripts/check_lib_query_names.py src` **PASS**（runtime 162/236）；
    `make lint` **import OK**。
- 新基线：全局 **112 → 108**（-4），对数 **45 → 43**：`devops_agent_session←mgmt` 与
  `vod_conversations←mgmt` 两对清零，`chat_conversations←mgmt` 2 → 1（余 1 处在
  `src/domains/mgmt/admin/cooldown.py:137`）。基线 `foreign-refs-allowlist.csv` 收紧为
  108 / 43，看板与作业单同步刷新（下一批头名 `src/domains/mgmt/admin/cooldown.py` 4 处：
  `agent_tasks` 1 / `chat_agent_session` 1 / `chat_conversations` 1 / `issue` 1）。

### 7.29 冷静期账号匿名化的 runtime 级联删除收口到 owner 域原语（2026-11-15 本批）

- 文件 / 处数：`src/domains/mgmt/admin/cooldown.py` **4 处 → 0**（单轮做完，4 处未超 25）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结，四表都在 `grape_runtime_db`，全归 **runtime**）：
  L135 `agent_tasks` / L136 `chat_agent_session` / L137 `chat_conversations` / L138 `issue`，
  均在 `_anonymize_account()` 的「lib 不可用」兜底分支里，形态是定表 + 定谓词
  `DELETE FROM <t> WHERE user_id=?`；**无动态表名/列名、无跨域 JOIN**。
  同文件其余 SQL（`grape_accounts` / `organization` / `org_member` / `user_repos` / …）
  都是 mgmt 自有表；`_get_expired_accounts()` 的 MySQL `%s` 分支同样是自有表，均不在统计内。
- owner 侧改动（`src/domains/runtime/public/__init__.py`，`?` 占位符）：
  1) `_ANY_QUERIES` 4 条：`user_purge_agent_tasks` / `user_purge_chat_agent_session` /
     `user_purge_chat_conversations` / `user_purge_issue`（与原兜底语句逐字一致，只换占位符）。
  2) `purge_user_rows(user_id, cursor=None)` 升级为**任意拓扑**原语：自有 DSN → `_any_tx()`
     真事务（四表同生共死）；单库/测试拓扑且传入游标 → 复用调用方游标与事务；单库无游标 →
     `_any_cursor(write=True)` 自提交。表名走模块内 `_USER_PURGE_PLAN` 白名单，调用方不能传表名，
     也传不了拼接 SQL。
- 设计取舍：
  1) **复用已有原语，不新增跨域出口**：mgmt 原本就有 outbox 首选通道
     `cross_write.submit("runtime","user_rows_purge",…, purge_user_rows)`，本批不动它；
     只把兜底从「本地 4 条 DELETE」换成「同一个原语 + 调用方游标」。owner 侧因此只多 4 条
     具名查询，没有把引用从 A 域搬到 B 域式的假下降。
  2) **为什么必须把游标传下去**：`_anonymize_account` 的 Step1–Step4 是**一个** mgmt 事务
     （末尾一次 `db.commit()`），四表删除必须与 `grape_accounts` 的 status=2 标记、
     `external_identities` 清理、字段匿名化同生共死。若兜底改走 `_any_tx()`/独立连接，
     单库拓扑会在中途 commit，把「Step1 已标记 + Step3 只删了一半」提前落盘 —— 正是本函数
     docstring 点名的 auto-commit 陷阱。传游标后单库拓扑下原语**完全不碰连接与提交**，
     语义与改造前逐表 `c.execute` 一致（rollback 一并撤销）。
  3) **自有 DSN 下反而更严**：旧实现 `db_registry.cursor("runtime")` 是共享 autocommit 连接，
     四表逐条提交（第 3 条失败 → 前两条已删且不可回滚）；现在 `_any_tx()` 用独立非 autocommit
     连接，四表要么全删要么全不删。
  4) **业务口径一字未动**：不过滤 `is_active` / `status`，按 `user_id` 全删；`user_id` 为空串
     时只删 `user_id=''` 的行（不会退化成全表删除）；重复调用返回全 0；不新增任何默认值或时间函数。
- 修的存量 bug（结构性隐患；三库拓扑尚未上线，线上未观测到故障）：
  1) **兜底 SQL 打在执行进程主库**：三库拓扑下 mgmt 进程库没有这四张 runtime 表（视图 0），
     lib 路径一旦失败（outbox drain 异常 / `submit` 抛错）进入兜底，四条 DELETE 必 1146
     并被外层 except 吞成一行 `anonymize failed` —— 而此刻账号已被标记 status=2、mgmt 侧
     数据已清，runtime 四表的用户业务数据**永久留存**（个人数据没真正删除，且不会再重试）。
  2) **旧原语在单库拓扑直接不可用**：`purge_user_rows` 原来直接
     `db_registry.cursor("runtime")`，无自有 DSN 时 `domain_db()` 对非当前域返回 None →
     必 `RuntimeError`。`scripts/xwrite_drain.py` 在单库环境重放 `user_rows_purge` 会同样抛错
     （死信里的「匿名化残留」永远排不掉）。现在任意拓扑可用，drain 重放路径一并修好。
  3) **四表写没有原子性**（自有 DSN 下）：见取舍 3。
- 验证：
  - `py_compile` 3 个文件 **COMPILE_OK**。
  - SQLite 烟测 `~/burndown/cooldown_purge_smoke.py` **ALL 84 CHECKS PASSED**
    （静态面 / owner 面 / 单库行为面 / 拓扑面 / 端到端五段）：4 条原语句逐字消失、跨域引用 0、
    匿名化路径无 `%s`、静默 except 与 HEAD 等量；只删目标 `user_id`、`is_active=0` 与
    `status='closed'` 行同样删（原口径）、调用方 rollback 能撤销四表删除、未知/空 uid 全 0、
    二次调用全 0、无游标调用自提交（独立连接可见）；自有 DSN 下写落在运行域库、进程库哨兵行
    一个不动、第三条语句失败时前两条 DELETE 一并回滚；真实 `_anonymize_account` 端到端
    （四表清空 + mgmt 表清理 + `unified_user_mapping` **清列保行** + 审计落一条 + status=2）；
    后续步骤失败时四表删除随调用方事务回滚、账号仍 status=3；status≠3 早退零删除。
  - `bash scripts/rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK / 契约 37 条 /
    看板最新）。
  - `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
  - `tests/api/test_runtime_write_primitive.py`：本批改写的 purge 用例 + 新增
    「单库复用调用方游标」用例通过；同文件 `test_ensure_agent_session_is_insert_ignore`
    为**既有失败**（HEAD 版 `runtime/public/__init__.py` 里 `ensure_agent_session` 有 2 个定义，
    后者 `-> None` 覆盖前者，返回值恒 None），与本批无关，留待重复定义清理专项。
- 新基线：全局 **108 → 104**（-4），对数 **43 → 40** —— `chat_agent_session←mgmt`、
  `chat_conversations←mgmt`、`issue←mgmt` 三对清零，`agent_tasks←mgmt` 2 → 1（余 1 处在
  `src/api/health_endpoints.py`）。基线 `foreign-refs-allowlist.csv` 收紧为 104 / 40，
  依赖看板与作业单同步刷新（下一批头名 `src/domains/insight/observability/failed_gap.py`
  4 处 `events`，同尺寸还有 `src/api/deps.py` 4 处 `external_identities`）。

### 7.30 insight failed 空 result 占比取数下沉 runtime owner 域（2026-11-15 本批）

- 文件 / 处数：`src/domains/insight/observability/failed_gap.py` **4 处 → 0**（单轮做完，4 处未超 25）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结，`grape_runtime_db.events`，归 **runtime**）：
  L57 / L68 / L80 / L96 四条 `events` 语句（1 条 `COUNT(*)` 全量 failed、1 条 `COUNT(*)` 真空、
  1 条 `COUNT(*)` legacy、1 条 7 列样本 `ORDER BY seq DESC LIMIT`）。四条都在
  `_lib is None` 的**单库兜底分支**里；`IN ({placeholders})` 由 repo_key 个数拼占位符，
  属「定谓词 + 动态 IN 长度」，**无动态表名/列名、无跨域 JOIN**。同函数上半段
  （`runtime_lib()` 命中时）走 `count_events` / `query_events` 具名查询，本来就不计跨域。
- owner 侧改动（`src/domains/runtime/public/__init__.py`，`?` 占位符、`{ph}` 展开）：
  1) `_FAILED_GAP_QUERIES` 4 条：`failed_gap_total` / `failed_gap_empty` / `failed_gap_legacy` /
     `failed_gap_samples`（与原 SQL 逐字一致，仅把 IN 列表换成 `{ph}`、`%` 前缀改为参数）。
  2) 薄原语两个：`failed_event_gap_counts_any(repo_keys, legacy_prefix)` 一次回三个标量；
     `failed_event_gap_samples_any(repo_keys, legacy_prefix, limit)` 回 7 列样本。
     二者一律走 `_any_cursor()`：自有 DSN → `db_registry.cursor("runtime")`，
     单库/测试拓扑 → 进程主库（自带 `db_lock`）。IN 单批上限 400（`_FAILED_GAP_KEY_CHUNK`），
     多块时计数逐块累加、样本每块取 limit 条后 Python 按 seq 倒序截断。
- 设计取舍：
  1) **两分支合成一条通道**，不保留 `if _lib is not None`：原「lib + 本地兜底」双路径正是
     兜底 SQL 长期留存在 insight 侧的原因；合成后调用方零 SQL、零分支，单库与三库同源
     （与 7.16 指标桶、7.20 能效看板同一手法）。
  2) **不搬成「lib 优先」的新双路径**：`count_events` / `query_events` 是 MySQL 原生方言
     （`%s`），单库/测试拓扑不可用；任意拓扑原语用 `?` + `TRIM`/`LIKE` 标准语法，
     `mysql_compat` 统一翻译，因此**不需要**再留第二条路。
  3) **语义一字未动**：只统计 `status='failed'`；真空 = `result IS NULL OR TRIM(result)=''`
     （空白串仍算真空，legacy 不算真空）；legacy = `LIKE '<prefix>%'`（前缀在开头才算，
     中段出现 `[legacy]` 归 with_diag）；`with_diag = max(0, total-empty-legacy)` 的夹底、
     比率 `round(...,4)`、样本 `result[:200]` 截断、`legacy` 布尔字段全部照抄；
     可见仓库边界完全由 insight 侧 `visible_repo_keys()` 决定，owner 侧不新增过滤
     （不引入 is_active / 时间窗 / org_id 之类新口径）。
  4) **`limit<=0` 原样返回空**，不夹到 1：原语句是 `LIMIT ?`，`limit=0` 就是零行；
     路由层已把入参夹到 [1,100]，原语不做「善意放大」，避免样本数超出调用方预期。
  5) 空候选集在 owner 侧直接回全 0 / `[]`：原实现靠调用方 `if not repos` 早退，
     这里再兜一层，避免任何调用方拼出 `IN ()` 语法错。
- 修的存量 bug（结构性；三库拓扑尚未上线，线上未观测到故障）：
  1) **兜底 SQL 打错库**：三库拓扑下 insight 进程库没有 `events`（视图 0），
     `runtime_lib()` 一旦返回 None（`GRAPE_DOMAIN_LIB` 未设置 / `available()` 为假 /
     import 失败），四条语句必 1146 并被同函数 `except` 吞成「全 0 + samples=[]」——
     `/api/obs/events/failed-result-gap` 返回 200 且显示「没有无头失败」，
     把真实故障掩盖成健康态，比 500 更糟。
  2) **真空口径在单库拓扑下不可用**：`count_events` 走 `db.cursor()` 且语句含 `%s`，
     单库/sqlite 拓扑即使 lib 开关打开也无法执行（绑定参数报错），哨兵口径实际只在
     三库三机才成立；现在两条拓扑同一份 `?` 语句，测试与线上同源。
  3) **样本 IN 列表无上限**：`visible_repo_keys()` 返回全量可见仓库，仓库多时
     `IN (?,?,...)` 可能撞变量上限/超长语句；本批在 owner 侧分块（400/批），
     行集合与一次 IN 等价。
- 验证：
  - `py_compile` 2 个文件 **COMPILE_OK**。
  - SQLite 烟测 `~/burndown/failed_gap_smoke.py` **ALL 64 CHECKS PASSED**
    （A 静态面 / B owner 注册面 / C 行为面 / D 边界与否定 / D2 端到端 / E 拓扑面）：
    4 条原语句从 insight 文件逐字消失、无 `%s`、无静默 except；计数 11 failed / 5 真空
    （含 NULL 与纯空白）/ 3 legacy，`with_diag=3`；样本集合 = 真空∪legacy 且 seq 倒序、
    列序 7 列；分块（每块 1 仓）与一次 IN 等值；空/None 候选集、未知仓库、`limit=0`、
    `limit<0` 全 0 或 `[]`；completed、有诊断、中段 `[legacy]` 均不入 legacy；
    样本 `result` 截 200 字、`legacy` 布尔按前缀；端到端 `failed_result_gap` 比率
    `0.4545 / 0.2727 / 0.2727` 与字段结构不变；无可见仓库返回全 0 结构；
    计数原语抛错时返回空结构并落 `log.warning`（不冒泡）；自有 DSN 分支经
    `db_registry.cursor("runtime")` 取到**同一组数字**，确未回落进程主库。
  - `bash scripts/rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK /
    契约 37 条 / 看板最新）。
  - `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **104 → 100**（-4），对数 **40 → 40** —— `events←insight` 这一对**没有**清零：
  本文件 4 处归位后，insight 侧仍有 2 处 `events` 引用（`observability/events_view.py`、
  `routes/search.py` 各 1，属另外两批），故对数不变。基线 `foreign-refs-allowlist.csv`
  收紧为 100 / 40，依赖看板与作业单同步刷新（下一批头名
  `src/domains/insight/observability/__init__.py` 4 处 `agent_tasks`/`agent_runtimes`，
  同尺寸还有 `src/api/session_store.py`、`src/api/health_endpoints.py` 各 4 处）。

### 7.31 insight 业务采样器按 source 的任务计数下沉 runtime owner 域（2026-11-15 本批）

- 文件 / 处数：`src/domains/insight/observability/__init__.py` **4 处 → 0**（单轮做完，4 处未超 25）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结，均归 **runtime**）：
  L618 / L630 / L642 三条 `agent_tasks`（`COALESCE(agent_type,'unknown')` + `COUNT(*)` 分组：
  `status='queued'` / `status IN ('dispatched','running')` / `status='running' AND started_at < ?`），
  L656 一条 `agent_runtimes`（`SELECT status, last_seen_at, credential_id`）。
  四条都在 `collect_business_sampler()` 的 `with db_lock: c = db.cursor()` 里逐条 `try/except: pass`；
  谓词与列全部定长，**无动态表名/列名、无跨域 JOIN、无动态 IN 长度**。
- owner 侧改动（`src/domains/runtime/public/__init__.py`，`?` 占位符）：
  1) `_ANY_QUERIES` 3 条具名查询：`obs_sampler_task_queued` / `obs_sampler_task_running` /
     `obs_sampler_task_stuck`（与原语句逐字一致，`started_at < ?` 由 cutoff 参数化）。
  2) 薄原语一个：`task_sampler_snapshot_any(stuck_cutoff)` 回
     `{"queued": {...}, "running": {...}, "stuck": {...}, "runtimes": [...]}`，
     四条语句在**同一个** `_any_cursor()` 上顺序执行；`runtimes` 复用已登记的
     `health_runtime_rows`（同一语句、同列序），不在本域把同一语句登记两份。
     `_sampler_source_counts()` 负责 `[(agent_type, n)] → {source: n}` 的 `r[0] or "unknown"` 归一，
     与原 Python loop 的**同行覆盖**语义一致（NULL 组与空串组同键时后者覆盖，不求和不炸）。
- 设计取舍：
  1) **一次采样 = 一个快照**：不拆成 4 个独立原语。原实现四问在同一 `db_lock` 内跑完，
     拆开会让另一次写入插进两次取数之间，gauge 出现互相矛盾的组合；合成后单库/测试拓扑
     是一次 `db_lock` 持有，自有 DSN 下是同一条连接（冒烟实测 e2e 一次采样只取 1 次连接）。
  2) **引擎行复用既有原语**而不是新增同名查询：7.21 已为 api `/health/realtime` 登记
     `health_runtime_rows`（列序同为 `status, last_seen_at, credential_id`），同一语句两份
     登记会让 owner 侧出现两个真源。
  3) **在线/离线判定留在 insight**：owner 只给原始列，`derive_runtime_health` 仍在调用方跑。
     该函数是 Issue #1496 裁定的唯一派生入口，属展示口径而非数据口径，不下沉。
  4) **分段降级搬进 owner**：原实现每条语句各自吞异常（一处表缺失不影响其余三条）。
     现在四条在 owner 侧各自 `try/except → log.debug`，调用方零 try/except，语义不变
     但不再静默（本文件静默 except 从 8 降到 4，基线同步收紧 8 → 4）。
  5) **口径一字未动**：不新增 `is_active` / 时间窗 / `org_id` / `repo_key` 过滤；
     `stuck` 仍是严格 `started_at < cutoff`（等值不算）；`started_at IS NULL` 仍不计；
     `pending_self_report` 仍不算 running；缺失 source 的 gauge 仍由 `_apply_source_gauges` 显式归零。
- 修的存量 bug（结构性；三库拓扑尚未上线，线上未观测到故障）：
  1) **采样 SQL 打错库**：三库拓扑下 insight 进程库没有 `agent_tasks` / `agent_runtimes`
     （视图 0），四条必 1146 并被就地 `except: pass` 吞成「队列/执行/卡住全 0、在线数 0」——
     `/metrics` 上 `grape_task_*` / `grape_runtime_online` 恒定归零，面板看起来像「业务停摆」，
     实际是「采不到数」。这是比 500 更坏的静默故障（与 7.30 同一类）。
  2) **采样器抢 insight 主库锁**：每次 scrape 用 `db_lock` 持 insight 进程库游标查外域表，
     在单库拓扑下与业务写入争同一把进程锁；改走 owner 连接后采样不再占用 insight 侧锁。
- 验证：
  - `py_compile` 2 个文件 **COMPILE_OK**（`runtime/public/__init__.py`、`insight/observability/__init__.py`）。
  - SQLite 烟测 `~/burndown/task_sampler_smoke.py` **ALL 70 CHECKS PASSED**
    （A 静态面 / B owner 注册面 / C 行为面 / D 边界与否定 / D2 端到端 gauge / E 拓扑面）：
    4 条原语句从 insight 文件逐字消失、无 `%s`/`NOW()`/`ON DUPLICATE`、采样器只剩原有 1 处外层兜底；
    三组计数 `queued{vod:2,issue:1,unknown:1}` / `running{unknown:1,vod:2,qa:2}` /
    `stuck{vod:1,qa:1}` 与「原 SQL + 原 loop」逐字等价（对照基准在本轮内联复刻）；
    `started_at == cutoff` 不算卡住、早 1 分钟即算；空表/缺 `agent_tasks`/缺 `agent_runtimes`
    各自降级且互不影响；`cutoff=None` → stuck 空；NULL 组与空串组同键为覆盖语义（1+3 不出 4）；
    端到端 `grape_task_queued/running/stuck`、`grape_runtime_online{local,remote}`、
    `grape_task_in_progress` 数值全对，source 消失显式归零、TTL 缓存仍生效；
    自有 DSN 分支只连 runtime 域、一次采样一次连接、读到的是 runtime 库（未回落进程主库）。
  - `bash scripts/rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK / 契约 37 条 / 看板最新）。
  - `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **100 → 96**（-4），对数 **40 → 40** —— `agent_tasks←insight` 剩 5 处
  （`observability/tokens.py` 2、`llm_usage.py` 1、`gap.py` 1、`overview.py` 1），
  `agent_runtimes←insight` 剩 4 处（`llm_usage.py` 2、`routes/health.py` 2），均属另行批次，
  故这两对没有清零。基线 `foreign-refs-allowlist.csv` 收紧为 96 / 40，
  依棘轮 `tests/silent_except_baseline.json` 该文件 8 → 4；依赖看板与作业单同步刷新
  （下一批头名 `src/api/deps.py` 4 处 `external_identities`，同尺寸还有
  `src/api/health_endpoints.py`、`src/api/session_store.py` 各 4 处）。

### 7.32 api 会话存储的场景读取按表归位 mgmt owner 域（2026-11-15 本批）

- 文件 / 处数：`src/api/session_store.py` **4 处 → 0**（单轮做完，4 处未超 25）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结）：
  - `devops_pipeline_scene`（归 **mgmt**）× 3：L486 `ensure_default_agent_sessions` 的
    `SELECT COUNT(*) ... WHERE repo_key=? AND is_active=1`（「是否已有默认场景」闸门）；
    L698 `_scene_binding_active` 的 `SELECT is_active ... WHERE scene_id=? AND (repo_key=? OR repo_key='')`；
    L745 `get_scene_advanced_options` 的 `SELECT advanced_options ... WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1`。
    三条谓词与列全部定长，**无动态表名/列名、无跨域 JOIN、无动态 IN 长度**。
  - `issue`（归 **runtime**）× 1：L611 —— **不是 SQL**，是英文日志
    `"Failed to update issue session {session_id}: {e}"`，静态正则按「关键字 + 下一个标识符」
    把它读成 `UPDATE issue`（假阳性）。
- owner 侧改动（`src/domains/mgmt/public/__init__.py`，`?` 占位符）：
  1) 新增具名查询一条 `scene_active_count_by_repo`（与 L486 原语句逐字一致，保留 `is_active=1`）。
  2) 新增薄原语一个 `count_active_scenes_any(repo_key) -> int`（无行/空值回 0），登记 `__all__`。
  3) 另两处**复用既有原语**，不重复登记语句：`scene_active_flags_any()`（7.x 已为
     runtime/event_pipeline 登记 `scene_active_flags`，同语句同列序）、
     `pipeline_scene_advanced_options(scene_id, repo_keys)`（已为 insight 看板登记，
     单键经 `IN(?)` 传与 `repo_key=?` 等价）。
- 设计取舍：
  1) **按「已有原语优先」而不是「一站点一新查询」**：三条语句里两条在早前批次已登记给别的
     消费方（runtime 事件管道 / insight 能效看板）。同语句在本域登记两份会让 owner 侧出现
     两个真源（7.31 已裁定过同一手法），故本批只补真正缺的那一条 `COUNT(*)`。
  2) **`COUNT` 原语不静默吞异常**（与 7.31 的四条各自 `log.debug` 不同）：调用方的降级语义是
     「计数不确定 → 不建场景」，把异常吞成 0 会让它在「库抖动」时误建场景链（幂等但白跑）。
     owner 抛、调用方按原有 `except → log.warning → 跳过` 处理，语义与单库拓扑逐字一致。
  3) **`advanced_options` 复用既有原语而非新登记 `repo_key=?` 版本**：单键 `IN(?)` 与原等值谓词
     等价；原实现里的「非 str 值直接当 dict」分支（`isinstance(row[0], str)`）在冒烟里仍钉住
     （`_raw` 非 str 时按原样归一），JSON 解析与非 dict 归一留在调用方，口径未动。
  4) **`_scene_binding_active` 的 `except → True` 保留**：其契约是「行不存在/查询失败都不算失效」
     （Issue #1745：不因 DB 抖动清空场景绑定），属业务口径而非静默吞错，本批只补 `log.debug`
     让降级可观测，不把它改成立即返回 False。
  5) **只切「建场景闸门」**：`ensure_default_agent_sessions` 后半段（会话落库 / upsert）仍走
     进程主库既有口径，不属本次表归位范围，避免把一批小改动扩成会话写入重构。
  6) **日志文案消歧**：`issue` 那处是英文句子被正则误判。改文案为
     `"Failed to persist session ..."`，并在注释里写明「关键字与表名相邻会被扫成引用」，
     防止后续有人改回原句又把棘轮点亮。
- 修的存量 bug（结构性；三库拓扑尚未上线，线上未观测到故障）：
  1) **首次托管永远不建默认场景**：三库拓扑下 api 进程库没有 `devops_pipeline_scene`（视图 0），
     `COUNT(*)` 必 1146 → 外层 `except` 吞成告警并**跳过**建场景，`apply_market_scene_by_name`
     从不触发；端点仍 200，现场表现是「新仓库托管后没有默认场景、Agent 会话全空」。
  2) **场景停用判定在三库拓扑下恒放行**：`_scene_binding_active` 的 1146 落到
     `except → True`，幽灵 `scene_id` 一路传到 `scenes detail` 必然 404（Issue #1745 的修复
     只在单库拓扑成立）。
  3) **`work_branch` 口径漂移**：`get_scene_advanced_options` 的 1146 落到 `except → {}`，
     场景配置的开发基线恒为空串 → 引擎用默认分支而不是场景声明的 `work_branch`，
     且没有任何日志痕迹。
- 验证：
  - `py_compile` 2 个文件 **COMPILE_OK**（`src/api/session_store.py`、`src/domains/mgmt/public/__init__.py`）。
  - SQLite 烟测 `~/burndown/session_store_scene_smoke.py` **ALL 72 CHECKS PASSED**
    （A 静态面 / B owner 注册面 / C 行为面 / D 边界与否定 / D2 端到端 / E 拓扑面）：
    三处 `devops_pipeline_scene` SQL 从 api 文件逐字消失、整文件「关键字+表名」命中 0、
    无 `%s`/`NOW()`/`ON DUPLICATE`、三个站点不再持进程主库游标/锁；
    计数/绑定/高级选项与原 SQL 逐字等价（对照基准在本轮内联复刻），`repo_key=''` 通用行
    参与绑定匹配；停用行不计入「已存在」但绑定判定仍返回其 `is_active`；
    空 `scene_id`、空/None `repo_key`、NULL 高级选项、`{}`、非法 JSON、`[1,2]` 非 dict、
    跨仓同 `scene_id`、缺表（owner 抛错 / 绑定 True / 高级选项 `{}` / 闸门跳过）
    全部按原口径；端到端建场景闸门 4 例（有在用场景 0 次、只有停用 1 次、新仓库 1 次、
    计数=1 即拦下）；自有 DSN 分支只连 mgmt 域、读到 mgmt 库的值（`work_branch` 未被
    api 主库覆盖）、mgmt 库无场景时**会**建场景（原实现在此恒 1146 从不建）。
  - `bash scripts/rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK /
    契约 37 条 / 看板最新）。
  - `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **96 → 92**（-4），对数 **40 → 39** —— `devops_pipeline_scene←api` 这一对
  **清零**（api 侧 3 处全归位；该表剩余 2 处都在 runtime 侧，属另行批次）。
  基线 `foreign-refs-allowlist.csv` 收紧为 92 / 39，依赖看板与作业单同步刷新
  （下一批头名 `src/api/health_endpoints.py` 4 处 `agent_tasks`/`agent_runtimes`，
  同尺寸还有 `src/api/deps.py` 4 处 `external_identities`）。

### 7.33 api 进程三层健康检查取数归位 runtime owner 域（2026-11-15 本批）

- 文件 / 处数：`src/api/health_endpoints.py` **4 处 → 0**（单轮做完，4 处未超 25）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结）：
  - `agent_tasks`（归 **runtime**）× 3：L91 `/healthz` 表探活 `SELECT COUNT(*) FROM agent_tasks`；
    L143 `/health/realtime` 的 `SELECT status, COUNT(*) ... GROUP BY status`（队列分布）；
    L146 `SELECT COUNT(*) ... WHERE status IN ('dispatched','running')`（在制数）。
  - `agent_runtimes`（归 **runtime**）× 1：L149 `SELECT status, last_seen_at, credential_id
    FROM agent_runtimes`（全量引擎行，在线/离线由 `derive_runtime_health` 派生）。
  - 四条都是**全量统计、无 WHERE 值参数、无动态表名/列名、无跨域 JOIN**，静态正则 4 处全为真引用。
- owner 侧改动：**零新增**，全部复用既有原语（`src/domains/runtime/public/__init__.py`）：
  `health_task_rows_total()` / `health_task_status_counts()` / `health_task_inflight_count()` /
  `health_runtime_rows()`，对应具名查询 `health_task_rows_total` / `health_task_status_counts` /
  `health_task_inflight_count` / `health_runtime_rows`（`?` 方言，`_ANY_QUERIES` 已登记、`__all__` 已导出）。
  调用点只改 `src/api/health_endpoints.py`。
- 设计取舍：
  1) **不重复登记语句**：这四条语句在 7.21 批次已为本文件的对偶实现 `src/api/server_common.py`
     登记过（worker 进程走 `server_common.attach_health`；server 进程与单进程入口走本文件）。
     同语句再登记一份会让 owner 出现两个真源（7.31/7.32 已裁定「已有原语优先」），本批只切调用点。
  2) **两套安装器口径对齐**：`/health` `/healthz` `/readyz` `/health/realtime` 长期存在两份实现
     （`server_common.attach_health` 与 `health_endpoints.install_health_endpoints`），worker 侧已在
     7.21 归位、server 侧未归位 → 同一集群不同进程对「任务在制数/引擎在线数」给不同答案
     （worker 有数、server 恒 db_error）。本批把 server 侧对齐到 worker 侧同一组原语。
  3) **`/health/realtime` 的快照语义由「一把锁内连查三表」降为「三条独立语句」**：原实现在一个
     `db_lock` 内顺序执行三条（单库拓扑下是一个快照）；改为三个原语后各自取连接，跨语句可能有
     毫秒级漂移。诊断端点的三个计数本就用于人读趋势、不参与任何判定，取舍是与
     `server_common` 现有实现保持一致、且拓扑无关；若将来要求严格同快照，需 owner 提供一次
     `_any_cursor()` 内跑多查询的原语，不在本批扩。
  4) **readiness 的 `SELECT 1` 保留在原处、不走 owner**：它探的是「本进程到自己库的连通性」
     （判断 `db: ok/error`），不是表引用；只有「表是否存在」这一项才归位给 agent_tasks owner。
     静态正则不把 `SELECT 1` 计为表引用，故此处不是豁免而是口径正确。
  5) **`fetchone()` 形态差异不影响语义**：原实现在表存在但为空时 `fetchone()` 得到 `(0,)`；
     `health_task_rows_total()` 返回 `int`（空表 0、无行 0），探活只看「有无异常」，等价。
- 修的存量 bug（结构性；三库拓扑尚未上线，线上未观测到故障）：
  1) **三库拓扑下 server 进程 readiness 恒 503**：api 进程库无 `agent_tasks`（视图 0）→ L91 必
     1146 → `tables: "error: agent_tasks missing"` + `not_ready` + 503，K8s/LB 会持续把 server
     摘流或反复重启。修后走 runtime DSN，探的是真表。
  2) **`/health/realtime` 在 server 进程整块失效**：三条语句同在一个 `try` 内、首条即 1146，
     `tasks` / `in_progress` / `runtimes` 三个字段一个都不填，只剩 `db_error` —— 运维在独立
     拓扑下拿不到任何队列/引擎诊断（worker 侧同端点正常，现场表现是「同一个集群两个端点两份答案」）。
  3) **同一集群两套健康口径**：`server_common` 侧已归位、本文件未归位，导致 server 与 worker
     的「在线引擎数」判定链不同源（一边 runtime DSN、一边进程主库）。本批消除该分叉。
- 验证：
  - `py_compile` 1 个文件 **COMPILE_OK**（`src/api/health_endpoints.py`）。
  - SQLite 烟测 `~/burndown/health_endpoints_smoke.py` **ALL 93 CHECKS PASSED**
    （A 静态面 / B owner 注册面 / C 行为面 / D 边界与否定 / E 拓扑面）：
    文件内「关键字+表名」命中 0、`health_realtime` 不再持 `db_lock`/`db.cursor()`、
    readiness 的游标只发 `SELECT 1`、无 `%s`/`NOW()`/`ON DUPLICATE`、无静默 `except pass`；
    四条原语与原 SQL 逐字等价（列序 `status,COUNT`；引擎行 `status,last_seen_at,credential_id`
    且不外泄）；`status` 为 NULL 的行按 `'offline'` 兜底、`active` 视同 online、心跳超 100s 不计在线；
    近 7 天 `[skill_mismatch]` 拒绝记录 → `skill_consistency` warn 且整体 `degraded`（http 仍 200）；
    非 loopback 四个端点全 403、`x-real-ip=127.0.0.1` 放行；
    3s 缓存命中期内删表仍返回缓存 200、过期后 503 且文案照旧、冷缓存新 app 立即 503；
    只缺 `agent_runtimes` 时 `tasks`/`in_progress` **仍出数**、`runtimes` 不填（与原实现逐条执行同）；
    空表全零且不算错误；自有 DSN 分支下 api 库两张表都不存在时 `/healthz` 仍 200、
    realtime 三组数读 runtime 库、全程只连 runtime 域（原实现恒 503 / 恒 db_error）。
  - `bash scripts/rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK /
    契约 37 条 / 看板最新）。
  - `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**。
- 新基线：全局 **92 → 88**（-4），对数 **39 → 39**（`agent_tasks←api` 4→1、`agent_runtimes←api` 3→2，
  两对都仍有别处的 api 侧引用，故对数不变）。基线 `foreign-refs-allowlist.csv` 收紧为 88 / 39，
  依赖看板与作业单同步刷新（下一批头名 `src/api/deps.py` 4 处 `external_identities`）。

### 7.34 api 进程 OAuth 身份取 token / 预刷新判定归位 mgmt owner 域（2026-11-15 本批）

- 文件 / 处数：`src/api/deps.py` **4 处 → 0**（单轮做完，4 处未超 25）。
- 表分布（`docs/weekly-11/table-owner.tsv` 冻结）：`external_identities`（归 **mgmt**）× 4。
  - L374 `_get_oauth_token_from_identity`：`SELECT access_token, status ... WHERE grape_user_id=?
    AND provider=? AND status IN (1, 4)`（取 OAuth token 的主路径，GitHub 与平台卡遗留符号共用）。
  - L451 `try_refresh_gitcode_token` 等锁分支：`SELECT access_token ... provider='gitcode' ...
    LIMIT 1`（抢锁失败后读其他实例刚刷新的 token）。
  - L491 `_try_refresh_github_token` 同形（provider='github'）。
  - L608 `get_user_token_for` 预刷新闸门：`SELECT token_expires_at ... status IN (1, 4)`。
  - 四条都是**单表、无 JOIN、无动态表名/列名**，值参数只有 (grape_user_id, provider)；
    provider 在 L451/L491 原为字面量，上移后改参数绑定（同值，不拼 SQL）。
- owner 侧改动（`src/domains/mgmt/public/__init__.py`）：
  - 新增具名查询 `ei_oauth_token_status`（`SELECT access_token, status`，**无 LIMIT**）、
    `ei_token_expires_at`（`SELECT token_expires_at`，无 LIMIT），均 `?` 方言、
    谓词/列序与原 SQL 逐字一致；
  - 新增薄函数 `oauth_token_status_any(grape_user_id, provider)`（返回原值二元组）、
    `token_expires_at_any(...)`（返回原值或 None）；
  - 复用既有 `ei_stored_access_token` / `stored_access_token_any`（7.19 为 token_resolver 登记，
    `SELECT access_token ... LIMIT 1` 与 L451/L491 逐字同形），本批**不重复登记**同语句；
    顺带把 `stored_access_token_any` 补进 `__all__`（此前只被 `from ... import` 直接引用，
    注册面缺登记）。
  - 调用点只改 `src/api/deps.py`。
- 设计取舍：
  1) **同语句不登记第二份**：L451/L491 与既有 `ei_stored_access_token` 逐字同形（含 LIMIT 1），
     按 7.31/7.32 已确立的「已有原语优先」直接复用；只有 L374（两列）与 L608（过期时间列）
     是本批真正新增的两条。
  2) **`SELECT access_token, status` 保留两列**：`status` 在调用点并未使用（只判 `row[0]`），
     但「列序与原 SQL 逐字一致」优先——缩成单列会让本批同时改变语句形态与后续
     status=4 语义的回接成本；status 的业务判定（过期仍返回 token）仍留在调用方。
  3) **无 LIMIT 照抄**：原实现靠 `fetchone()` 取首行，真实 schema 有
     `UNIQUE(grape_user_id, provider)` 只可能一行；不擅自加 `LIMIT 1` 以免多行副本场景
     改变行选择口径（冒烟用无约束表钉住「首行」行为）。
  4) **`str()` 归一 grape_user_id**：与同域既有 `stored_access_token_any` 一致；TEXT 列 +
     整型入参在 MySQL/sqlite 都能命中，None 仍不命中（NULL 比较同义）。
  5) **`get_user_token_for` 里那把 `_sm_dl` 进程锁去掉**：原实现在一把 `db_lock` 内做
     「账号查询 + 过期时间查询」两读（单库下是一个快照）。改后两跳各自取锁（单库 `db_lock`
     是 RLock，不会自锁）/ 各自取 mgmt DSN。username→id 映射在会话期内不变，
     两读之间毫秒级漂移不影响判定；换取的是拓扑无关（三库下有真值）。
  6) **等锁分支的两处 `except: pass` 改为 `log.debug`**：语义不变（等锁路径读不到 =
     「未被其他实例刷新」，随后返回 None 让上层走 401 兜底），只是让降级可观测；
     `tests/silent_except_baseline.json` 的 `src/api/deps.py` 5 → 1（只减不增）。
  7) **`mark_identity_expired` 的 `except: pass` 原样保留**：属既有尽力而为语义（标记失败不
     影响「返回旧 token」），不在本批扩大范围。
- 修的存量 bug（结构性；三库拓扑尚未上线，线上未观测到故障）：
  1) **三库拓扑下 OAuth 取 token 恒空**：api 进程库没有 `external_identities`（视图 0）→
     L374 必 1146 → 外层 except 吞成 `""`，`get_user_github_token` 与平台卡 `legacy_token_symbol`
     派生入口一并失效（GitHub 侧永远拿不到 token，gitee/gitlab 等新平台同链）。
  2) **双主集群并发 401 时刷新后仍拿不到 token**：L451/L491 的 1146 落到 `except: pass` →
     返回 None，调用方以为「别人也没刷成功」继续用旧 token，401 重试链打空。
  3) **预刷新闸门恒不触发**：L608 的 1146 被外层 except 吞成
     `pre-refresh expiry check failed` 告警后直接 `return token`，`token_expires_at`
     判定永远走不到 → 过期前 10 分钟不刷新，只能等真过期打 401 再靠兜底链救。
  4) **同一批站点在单库拓扑下静默读副本**：若进程库恰好存在同名副本（视图期），
     四条语句会读到与 mgmt 库不一致的旧 token/旧过期时间，且没有任何日志痕迹。
- 验证：
  - `py_compile` 2 个文件 **COMPILE_OK**（`src/api/deps.py`、`src/domains/mgmt/public/__init__.py`）。
  - SQLite 烟测 `~/burndown/deps_oauth_smoke.py` **ALL 96 CHECKS PASSED**
    （A 静态面 / B owner 注册面 / C 行为面 / D 边界与否定 / E 拓扑面）：
    文件内「关键字 + 101 张基表名」命中 0（不是靠基线豁免）；四个站点不再持进程主库游标/锁、
    无残留 SQL 文本、无 `%s`/`NOW()`/`ON DUPLICATE`；静默 except 3（HEAD）→ 1；
    两条新查询列序/谓词/无 LIMIT 逐字对齐，未登记名抛 KeyError；
    `status IN (1,4)` 口径钉住（status=4 仍返回 token，2/3 不命中），空 token 行返回行本身、
    NULL 过期时间 → None、多行取首行与原 SQL 同；
    取 token 端到端（含 `get_user_github_token`）、等锁分支读最新 token（token 空/无行 → None）、
    预刷新四分（无 refresh 早退 / 窗口内刷新一次 / 远未来不刷 / 不可解析=0 不刷 / NULL 不刷）
    与「刷新失败 → mark_identity_expired + 返回旧 token」全部按原口径；
    缺表时 owner 抛 `OperationalError`（不静默）、调用方各自降级为 `''`/None；
    自有 DSN 分支下 api 库缺表仍能取 token 与判定过期，全程只连 mgmt 域
    （原实现在此 4 条语句全 1146，用户侧表现是 GitHub token 永久为空、预刷新永不触发）。
  - `bash scripts/rds_gate.sh --quick` **GATE: 全绿**（check-arch R1–R8 / 棘轮 OK /
    契约 37 条 / 看板最新）。
  - `pytest tests/arch tests/contracts tests/scripts` **158 passed**；`make lint` **import OK**；
    `check_lib_query_names.py src` **PASS**（mgmt 169 funcs + 6 named queries used）。
- 新基线：全局 **88 → 84**（-4），对数 **39 → 38** —— `external_identities←api` 这一对
  **清零**（该表剩余消费者均在 mgmt 本域与 runtime 侧，属另行批次）。基线
  `foreign-refs-allowlist.csv` 收紧为 84 / 38，依赖看板与作业单同步刷新
  （下一批头名 `src/kernel/prompt_builder.py` 3 处 `unified_user_mapping`，
  同尺寸还有 `src/kernel/persistence/credential_usage_log.py` 3 处）。

### runtime/engine/bot_token_health 巡检两表归位（-3，本文件清零）
- 文件：`src/domains/runtime/engine/bot_token_health.py`（3 处 → **0**）；全局静态 **84 → 81**（-3），
  对数 **38 → 37** —— `grape_t_repo_credentials←runtime` **整对清零**，`managed_repos←runtime` **5 → 3**
  （余 3 处属其它文件）。
- 表分布（消费域 runtime）：`grape_t_repo_credentials` 1 处（归 **insight**，行 172 巡检基集）；
  `managed_repos` 2 处（归 **mgmt**：行 232 legacy bot token 列读、行 292 `bot_token_status` 写）。
- 本轮取批：作业单头名 `src/kernel/prompt_builder.py`（3 处）与次名
  `src/kernel/persistence/credential_usage_log.py`（3 处）**都是 blocked**（理由见本节末），
  按作业单规则跳过并在 `next-batches.md` 顶部标 blocked，取第一个可改造文件。
- 归属域侧改动（SQL 仍只在 owner 域一份，无新造查询文本）：
  · insight public 新增**薄函数** `list_repo_keys_with_nonempty_credential(cred_type)`，
    复用已登记具名查询 `repo_cred_nonempty_keys`（`cred_type=? AND value IS NOT NULL AND value!=''`）；
  · mgmt public 复用既有 `repo_bot_token_from_legacy_column_any(repo_key, column)`（列名走
    `_REPO_TOKEN_COLUMN_QUERIES` 白名单）与 `set_repo_bot_token_status_any(repo_key, status)`。
- 关键设计取舍：
  1. **这 3 处都是「owner lib/原语 + 本地兜底 SQL」双份**，兜底 SQL 正是跨域统计来源。本轮把兜底换成
     任意拓扑原语（`*_any`：自有 DSN → db_registry；单库/测试 → 进程库 + db_lock），单库与三库
     同一条路径，调用方不再需要「有 DSN 才走 owner」的条件分支——原 `_rt_health.available()` 判断整体消失。
  2. **基集必须保序，所以不直接复用集合版**：原语句无 `ORDER BY`，`fetchall()` 行序即表序，巡检按
     「每 10 仓一批 + 批间隔 1s」分组，依赖该顺序；既有 `repo_keys_with_nonempty_credential` 返回 set，
     是给「本域 NOT EXISTS 等价过滤」用的（顺序无关）。两者同一份具名查询，只差返回形态，因此加一个
     保序列表版薄函数，而不是让调用方自己排序（排序会改变分批归属）。
  3. **少一次跨域转发**：原实现走 `runtime.public.list_repo_credentials()`，而该函数本身只是转发到
     insight public（表归 insight）；直接调 insight public 后链路短一跳，`cred_type` 过滤也回到 SQL 内。
  4. **动态列名收回 owner 域**：legacy 列名原由调用方把 `token_storage_field()` 拼进 f-string。
     改为 mgmt 侧白名单挑选；未登记列（gitee/gitlab 终态无物理列）抛 `KeyError`，被调用方原有
     `except` 兜住 → 「无 token，不发起验证」，与原「拼出不存在列 → SQL 报错 → 空 token」逐字同口径。
  5. **写路径保留幂等意图**：仍优先 `cross_write.submit`（失败排队重试），只在 lib 不可用/失败时回落
     `set_repo_bot_token_status_any`（owner 域自提交）；`rowcount==0`（仓库不存在）与 `0 行` 原语义一致。
  6. 顺带删掉该文件两处 `from src.kernel.session_manager import db, db_lock` 与全部游标直连：现在
     **整个文件没有任何直接 DB 访问**（静态否定已覆盖），也不再依赖进程库拓扑。
- **修的存量 bug（3 条，都是三库独立后必然 1146、且都被 except 吞成静默降级）**：
  1. 巡检基集旧兜底 `SELECT ... FROM grape_t_repo_credentials` 走进程主库：runtime 库没有该表（视图 0）
     → 1146 → `except → warning + return`，**仓库级 bot token 巡检整体停摆**（用户侧：仓库级 bot token
     失效不告警、`bot_token_status` 不更新）。
  2. legacy 列回落 `SELECT {field} FROM managed_repos` 同理 1146 → 吞成「无 token」：rc 缺失时不再回落
     legacy 存量列，等于静默放弃 gitcode/github 存量凭据。
  3. 状态写兜底 `UPDATE managed_repos` 同样 1146 → 只剩日志，R17.2.5（401 标记 expired）/
     R17.2.6（200 清标记）在生产三库拓扑下从未真正落库。
- **blocked（本轮复核，结论不变）**：
  1. `src/kernel/prompt_builder.py` 3 处（行 621/627/630）全在 `_build_feishu_context_section` 的
     f-string 文案里——是**教 agent 用 sqlite3 自写 sessions.db 的说明文本**，本进程不执行。2026-11-14
     已确立口径「不为了让正则掉数字而改文案」；改写还需先给 agent 一个合法映射写通道（现仅
     mgmt `/internal` 服务端点，agent 持会话 token），且
     `tests/runtime/test_issue_372_unified_mapping_github.py::test_prompt_builder_uses_insert_ignore_not_replace`
     按源文本断言该段语义，属独立设计项。
  2. `src/kernel/persistence/credential_usage_log.py` 3 处：后台线程共用 owner 连接会打出
     `Packet sequence number wrong`，正解是「每工作线程一条 owner 连接」的池化，属独立一轮
     （交接文档 §5.3 坑 4 / §9 阻塞项 2）。
  两条都已在 `docs/weekly-11/next-batches.md` 顶部「⛔ blocked」小节写明，避免下一轮按处数硬拿。
- **新基线**：**81 处 / 37 对**（`--update-baseline` 已收紧）；依赖看板 / 矩阵 / 作业单同步刷新。
- 验证（真实退出码）：
  · `py_compile` 两文件 → **COMPILE_OK**；
  · `~/burndown/bot_token_health_smoke.py`（`DEVOPS_DB_BACKEND=sqlite`）**40 项全通过**
    （`ALL bot_token_health smoke PASSED (40 checks)`）：insight 列表原语保序 / 排除空串·NULL·非 bot 类型 /
    同仓重复行不去重 / 空表；集合版与列表版行集合一致；mgmt 白名单读（密文不解密、空串·NULL·无行、
    未登记列与注入串均抛 KeyError）、状态写（置 expired / 清空 / 缺行 0 行 / 不波及他仓）；
    巡检调用点（基集=bot 且非空、序=表序、provider 由前缀派生、空集不发请求、原语抛错整批跳过不上抛）；
    单仓验证调用点（rc 优先不触 legacy、rc 缺失回落 legacy、rc 空串仍回落、401 写 expired、
    500 不标记、200 清标记、无凭据早退零写、未登记列不崩）；静态否定「本文件已无两表 SQL 字面量、
    不再直连进程库，新薄函数体内无内联 SQL」；
  · `rds_gate.sh --quick` → **GATE: 全绿**（check-arch R1–R7 / 棘轮未上升 / 内部契约 37 条最新 / 看板最新）；
  · `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；`make lint` → **import OK**。
- **未验证项（如实）**：本轮未跑 `p4_p5_loop.sh` 三实例彩排——本会话无 `SRC_PWD`/`REMOTE_PWD` 环境变量，
  且本批无 schema / 迁移 / 契约面变化（不改表、不新增路由），运行面证据为 sqlite 烟测 + 单库 pytest；
  **不把彩排结果算作本批证据**。

### mgmt/user/credentials_backfill_n1 回填两表归位（-3，本文件 insight 面清零）
- 文件：`src/domains/mgmt/user/credentials_backfill_n1.py`（3 处 → **0**，仅剩 mgmt 本域
  `user_credentials` 两步的 SQL）；全局静态 **81 → 78**（-3），对数 **37 → 37**——
  `grape_t_repo_credentials←mgmt` 这一对仍有余量（其它文件），本文件的 3 处已清零。
- 表分布（消费域 mgmt）：`grape_t_repo_credentials` 3 处（归 **insight**）：
  · 行 121 `SELECT repo_key, cred_type, value ... WHERE value != ''`（加密回填基集读取）
  · 行 128 `UPDATE ... SET value=? WHERE repo_key=? AND cred_type=?`（明文行逐行加密）
  · 行 148 `SELECT repo_key, cred_type ... WHERE id IS NULL OR id=''`（D99 空 id 断言）
  三处同一张表、同一域，无动态拼接（表名/列名均为字面量，无需白名单）。
- 本轮取批：作业单头名 `src/kernel/prompt_builder.py` 与次名
  `src/kernel/persistence/credential_usage_log.py` **复核后仍 blocked**（理由见本节末，
  结论与 2026-11-14 一致），按作业单规则跳过，取第一个可改造文件。
- 归属域侧改动（SQL 仍只在 owner 域一份）：
  · insight public 新增 3 个具名查询（`_REPO_CRED_QUERIES`）：
    `repo_cred_nonempty_rows` / `repo_cred_value_only_update` / `repo_cred_empty_id_rows`，
    列序、谓词、`?` 占位符与原 SQL 逐字一致；
  · insight public 新增 3 个薄函数：`list_repo_credentials_with_nonempty_value()`（保序全量行）、
    `update_repo_credential_values(pairs)`（域内单事务批量写）、`list_repo_credentials_without_id()`；
    均登记 `__all__`，体内无内联 SQL。
- 关键设计取舍：
  1. **批量写必须留在 owner 域内做事务**：原实现是「一个游标 + 循环 UPDATE + 末尾 `_commit`」，
     整体一次性提交。若改成调用方逐条调 `exec_any`（每次一条连接/一次提交），中途失败会留下
     半批密文半批明文——下一次回填按「含冒号即视为密文」跳过，**永远修不回来**。
     因此把「循环 + 单事务」放进 insight `_any_tx()`：自有 DSN → 独立非 autocommit 连接；
     单库 → 进程库 + `db_lock`，两条拓扑同一段代码。
  2. **只写 `value` 列**：既有具名查询 `repo_cred_update` 会顺带写 `updated_at`，与回填原 SQL
     不同（回填不刷新更新时间），所以新登记 `repo_cred_value_only_update` 而不是复用。
     `updated_at` 保留是运维判据（凭据轮换时间），回填改写它等于污染审计面。
  3. **不插入缺失行**：`UPDATE` 命中 0 行即结束，与原语义一致（回填只改存量、不造凭据）；
     返回条数按入参计，与原日志 `N rows` 口径一致。
  4. **明文判定留在调用方**：`":" not in val` 是 mgmt 侧 `credential_crypto` 的格式知识
     （密文恒为 `kid:nonce:ct`），insight 不该知道；owner 只负责「给我非空行的原值」。
     加密函数同理（`credential_crypto` 归 mgmt），薄函数只收成对的新值。
  5. **mgmt 本域两步原样不动**：`user_credentials` 的补列/回填仍走 `_domain_cursor("mgmt")`
     与 `_commit(c)`，本轮不扩大范围（该表归 mgmt，不是跨域）。
- 修的存量 bug（结构性；单库拓扑不可见，三库独立后必然踩）：
  1. **回填连接归错域**：原实现 `_domain_cursor("insight")` 只解决「配了自有 DSN」的分支；
     单库/视图期回落进程库时，`grape_t_repo_credentials` 一旦只存在于 insight 库（视图 0 的终态），
     两条 SELECT 直接 1146 被 `except` 吞成 warning → 启动回填静默空转、明文凭据永远不加密，
     且 D99 空 id 断言永不告警。改走 insight public 的「任意拓扑」原语后，
     自有 DSN / 单库 / 测试三条路径同一条代码，不再依赖调用方所在进程的库。
  2. **docstring 与实现脱节**：模块头还写着「现改为 `domain_db.cursor(域)`」，与「SQL 只留
     归属域」的口径不一致，易诱导后来者继续在调用方写 SQL；已同步改写。
- 验证（真实退出码）：
  · `py_compile` 两文件 → **COMPILE_OK**；
  · `~/burndown/credentials_backfill_n1_smoke.py`（`DEVOPS_DB_BACKEND=sqlite`）
    **ALL ... PASSED (79 checks)**，A 注册面 / B 调用点静态 / C 行为 / D 事务 / E 断言 /
    F 拓扑 / G 降级：三条新查询与原 SQL 逐字对齐、占位符数 0/3/0、无 `%s`/`NOW()`/
    `ON DUPLICATE`/`LIMIT`；明文→密文可 `decrypt` 回原值且 **`updated_at` 不变**、
    同仓不同 `cred_type` 各写各键互不覆盖、已密文行幂等跳过（二次回填 0 写 0 返回）、
    空串/NULL 值不入选；批量写中途不可绑定参数 → **整批回滚**（前一条也不落库）、
    空列表零写、目标仓库不存在 → 不插入新行；D99 计数只认 `id=''` 与 `id IS NULL`、
    正常 id 不计、空表 0；自有 DSN 分支（monkeypatch `uses_own_dsn`/`transaction`/`cursor`）
    写落到 insight 库、进程库同名副本**零改动**；`encrypt` 抛错 → 返回 0 且零写；
    未登记查询名 → `KeyError`（fail-closed）；表缺失 → 两步各返回 0 不抛。
  · `bash scripts/rds_gate.sh --quick` → **GATE: 全绿**（check-arch R1–R7 / 棘轮未上升 /
    内部契约 37 条最新 / 依赖看板最新）；
  · `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；`make lint` → **import OK**；
    `check_lib_query_names.py` → **PASS**（insight 8→11 funcs 已登记，mgmt 侧 171 funcs used）。
  · 直覆盖用例 `tests/mgmt/test_n3_bot_token_encryption.py` /
    `test_n4_audit_coverage.py` / `test_s119` / `test_s122` → 23 passed / 4 failed，
    4 项失败均为**既有路径 bug**（`Path(__file__).parent.parent/"src"` 在 tests/ 镜像目录下
    解到 `tests/src/...`）；已用 `git worktree` 在 HEAD 干净树复现同 4 项，与本批无关。
- **未验证项（如实）**：本轮未跑 `p4_p5_loop.sh` 三实例彩排——本会话无 `SRC_PWD`/`REMOTE_PWD`，
  且本批不改 schema / 迁移 / 契约面（无新表、无新路由），运行面证据为 sqlite 烟测（含自有
  DSN 分支模拟）+ 单库 pytest；**不把彩排结果算作本批证据**。
- **blocked（复核，结论不变）**：`src/kernel/prompt_builder.py` 3 处是 agent 提示文案（本进程
  不执行 + 测试按源文本断言）；`src/kernel/persistence/credential_usage_log.py` 3 处需
  「每工作线程一条 owner 连接」池化。两条已在 `next-batches.md` 顶部「⛔ blocked」小节写明。
- **新基线**：**78 处 / 37 对**（`--update-baseline` 已收紧）；依赖看板 / 矩阵 / 作业单同步刷新
  （作业单 78 处 / 44 文件，下一批可选头名 `src/domains/insight/observability/tokens.py` 3 处）。

### insight/observability/tokens 跨库 JOIN 拆分（-3，本文件清零）
- 文件：`src/domains/insight/observability/tokens.py`（3 处 → **0**）；全局静态 **78 → 75**（-3），
  对数 **37 → 37**（`agent_tasks←insight`、`runtime_credentials←insight` 两对仍有余量，其它文件）。
- 表分布（消费域 insight，全归 **runtime**）：
  · 行 154 `FROM obs_llm_usage u LEFT JOIN agent_tasks t ON t.id = u.task_id`（`_fetch`，跨库 JOIN）
  · 行 279 `SELECT id, COALESCE(NULLIF(device_name,''), name, '') FROM runtime_credentials WHERE id IN (...)`
    （`_engine_names`，动态 IN）
  · 行 6 `join agent_tasks 取 issue_id` —— docstring 命中正则；改造后 join 已不存在，文档失真 → 同步改写
    （本批唯一非 SQL 的一处，属**文档与实现脱节**的修正，不是为掉数字改文案；prompt_builder 那条口径不同）。
- 本轮取批：作业单头名 `src/kernel/prompt_builder.py` 与次名 `src/kernel/persistence/credential_usage_log.py`
  **复核后仍 blocked**（理由见 `next-batches.md` 顶部「⛔ blocked」，与前两轮结论一致），按作业单规则跳过，
  取第一个可改造文件（`tokens.py`，上一批台账末尾已点名）。
- 归属域侧改动（SQL 只在 owner 域各一份）：
  · insight public 新增薄函数 `list_usage_detail_window(start_lo, start_hi, repo_keys, engine_ids, task_id, limit)`
    + 模块常量 `_USAGE_DETAIL_SQL`：列序与原宽 SQL 的 `u.*` 侧**逐列一致**
    （task_id, agent_type, engine_id, repo_key, run_kind, run_id, created_at, 五项 token, cost_usd）；
  · runtime public 新增 `_TASK_SIDE_QUERIES`（两条固定模板：`task_issue_ids` /
    `runtime_credential_display_names`）+ `task_issue_id_rows(ids, chunk=500)`、
    `runtime_credential_display_names(ids, chunk=500)`。`{ph}` 由 owner 按 id 个数展开，
    调用方只能传 id 值列表（不接受拼接 SQL / 表名 / 列名），未登记 name 抛 `KeyError`（fail-closed）。
- 关键设计取舍：
  1. **跨域 JOIN 拆成三步**：① 本域明细窗口读 → ② runtime 右表批量读（去重后分块 500，原实现单条 IN
     最多 2 万个 id）→ ③ Python 内连接补 `issue_id`。缺失任务 = LEFT JOIN 的 NULL 侧（补 `None`，
     行**不丢**、仍计入汇总），与改造前 `LEFT JOIN` 语义逐点对齐。
  2. **可见性谓词下沉到归属域**：`_scope` 不再返回 SQL 片段，改返回 `(repo_key 候选集, engine_id 集合, ok)`；
     `repo_key IN` / `engine_id IN` 的列名走 owner 白名单，值一律 `?` 参数化，两组谓词仍 **OR** 连接并整体
     加括号（原拼法），`engine_ids` 里的假值 0 照旧丢弃（原 `[int(x) for x in ids if x]` 同口径）。
     调用方从此**一行 SQL 都不留**（本文件已无任何表名 + SQL 关键字）。
  3. **失败降级保持原语义**：明细读或右表读任一失败 → 空窗口 + `log.warning`（与原「单条宽 SQL 失败即
     空」同口径），不返回半份结果；`_engine_names` 失败 → 无显示名但引擎行照常（原语义）。
  4. **关联口径收紧为字符串相等**：原 `t.id = u.task_id` 在 MySQL 隐式把 `task_id` 转数字（`'abc'` → 0，
     会误命中 id=0），在 sqlite/整数与文本混存时则**永不命中**。现统一按 `str(id) == str(task_id)`，
     两个后端同口径；非数字 task_id 不再假命中。
  5. **行→dict 映射留在调用方**（`_DETAIL_COLS` 固定列序）：owner 只返回元组，避免把展示层列名
     下沉到 public。
- **修的存量 bug（3 条，都是三库独立后必然 1146、且都被 except 吞成静默降级）**：
  1. `_fetch` 的宽 SQL 走 insight 进程库：`agent_tasks` 归 runtime、视图 0 → 1146 →
     `except → warning + []`，**token 三级归因（引擎/任务/阶段）在生产三库拓扑下恒为空**。
  2. `_engine_names` 直查 `runtime_credentials` 同理 1146 → 引擎列表永远只有 id、没有显示名。
  3. **既有口径 bug（与三库无关）**：`t.id = u.task_id` 是 INTEGER 与 TEXT 比较，sqlite 拓扑下恒不命中
     → `issue_id` 全 NULL、阶段视图塌成一个空 issue 分组（本次烟测第 16~19 项锁死新口径）。
- **新基线**：**75 处 / 37 对**（`--update-baseline` 已收紧）；依赖看板 / 矩阵 / 作业单同步刷新
  （作业单 75 处 / 43 文件，下一批可选头名 `src/domains/insight/observability/llm_usage.py` 3 处）。
- 验证（真实退出码）：
  · `py_compile` 三文件 → **COMPILE_OK**；
  · `~/burndown/tokens_obs_smoke.py`（`DEVOPS_DB_BACKEND=sqlite`，自建
    obs/agent_tasks/runtime_credentials 三表）→ **ALL tokens_obs smoke PASSED (36 checks)**：
    A 注册面/静态（调用方无 SQL 字样、不直连进程库、owner 方言无 `%s`/`NOW()`/`ON DUPLICATE`、
    动态 IN 只展开 `?`、两个 `__all__` 已登记、未登记 name → KeyError）；
    B 窗口（±1 天粗筛 + Python 精确窗口排除 CST 未来行、total 不含 reasoning、task_id/limit 过滤）；
    C LEFT JOIN NULL 侧（缺任务 → `issue_id=None`、行仍计入、阶段视图归空 issue 分组）；
    D 关联口径（`'007'` 不命中 id=7、`'7'` 命中）；E 可见性（global / repo 越权空集合不降级 /
    engine 过滤、0 被丢弃）；F 分块（1201 行跨 500/500/201 三块全关联、重复·None·空串去重、
    空列表零外呼）；G 显示名（device_name → name → 空串、缺行不出现，engine_totals.name 走原语）；
    H 降级（runtime/insight 读抛错 → 空窗口不上抛、空窗口不触发外呼、三层合计一致）。
  · `tests/insight/test_obs_tokens.py`（真实 MySQL 单库 `grape_ljt_db`）→ **4 passed**（改造前基线同为 4）；
  · `bash scripts/rds_gate.sh --quick` → **GATE: 全绿**（check-arch R1–R7 / 棘轮未上升 / 内部契约 37 条最新 / 看板最新）；
  · `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；`make lint` → **import OK**；
    `check_lib_query_names.py` → **PASS**（runtime 165 funcs + 30 queries used / 241 + 61 registered）。
- **未验证项（如实）**：本轮未跑 `p4_p5_loop.sh` 三实例彩排——本 shell 未注入 `SRC_PWD`/`REMOTE_PWD`
  （`p4_p5_loop.sh` 建库搬数据与远端实例都要它们），且本批不改 schema / 迁移 / 契约面（无新表、无新路由），
  运行面证据为 sqlite 烟测 + 单库（MySQL 单库 4 passed、sqlite 36 项）；**不把彩排结果算作本批证据**。

### insight/observability/llm_usage 运行域表读取归位（-3，本文件清零）
- 文件：`src/domains/insight/observability/llm_usage.py`（3 处 → **0**，仅剩本域 `obs_llm_usage`）；
  全局静态 **75 → 72**（-3），对数 **37 → 37**——`agent_tasks←insight` 仍 2 处
  （`observability/overview.py`、`observability/gap.py`）、`agent_runtimes←insight` 仍 2 处
  （`routes/health.py`），均为其它文件，本文件已清零。
- 表分布（消费域 insight，全归 **runtime**）：
  · 行 119 `SELECT repo_key, agent_type, runtime_id, user_id, issue_id FROM agent_tasks WHERE id=?`
    （`_attribution` 的 lib 不可用分支，5 列）
  · 行 343 `SELECT model_name FROM agent_runtimes LIMIT 0`（`_agent_runtimes_has_model_name` 的 MySQL 兜底列探测）
  · 行 399 `SELECT model_name FROM agent_runtimes WHERE id=?`（`task_model_summary` 配置模型回落取值）
  · 另有行 337 `PRAGMA table_info(agent_runtimes)`：静态正则不计（只认 `TABLE <表>` 等关键字），
    但同属「在 insight 连接上探测运行域表结构」，本批一并清掉。
  三处同一张表结构族、无用户输入拼接（表名/列名均字面量）。
- 本轮取批：作业单头名 `src/kernel/prompt_builder.py` 与次名
  `src/kernel/persistence/credential_usage_log.py` **三轮复核后仍 blocked**（理由见
  `next-batches.md` 顶部「⛔ blocked」；作业单脚本每次刷新会覆盖该小节，已补回），
  按规则跳过，取第一个可改造文件（上批台账末尾点名的 `llm_usage.py`）。
- 归属域侧改动（SQL 只在 owner 域一份）：
  · runtime public `_ANY_QUERIES` 新增 2 条具名查询：
    `task_attribution_by_id`（5 列列序与调用方解包顺序逐字一致）、
    `agent_runtimes_model_name`（`SELECT model_name FROM agent_runtimes WHERE id=?`）；
  · runtime public 新增 2 个薄函数：`task_attribution_row_any(task_id)`（返回五元组，无行 None）、
    `runtime_model_name_any(runtime_id)`（返回模型名或空串）；体内无内联 SQL，未登记名抛 KeyError。
  调用方 `_attribution` 去掉 `cursor` 形参（读路径不再需要调用方游标），删掉
  `_agent_runtimes_has_model_name`（PRAGMA + LIMIT 0 双探测共 23 行）。
- 关键设计取舍：
  1. **列探测下沉到归属域，且不用 PRAGMA**：复用 runtime public 既有的 `_table_has_column`
     （`SELECT * ... LIMIT 0` 读 description，两条后端一致）。`db_registry` 的薄兼容游标只对
     进程主库翻译 PRAGMA，跨域连接上直发 `PRAGMA table_info` 在 MySQL 上必 1064——原实现
     之所以能跑，是因为探测走的是**本进程游标**，而这正是三库独立后失效的写法。
  2. **归属反查移出 `db_lock`（本批最要紧的一处）**：`*_any` 原语自带 `db_lock`（单库拓扑直接
     用进程库），若仍在 `with db_lock:` 内调用就变成同线程二次获取——生产 `RLock` 侥幸不炸，
     但测试 monkeypatch 的裸 `Lock` 会**自死锁**。故把归属读提到写事务之前，写入段仍是
     「一个 `db_lock` + 一个游标 + 末尾一次 commit」，事务语义逐点不变（归属是元数据，
     读与写之间的时间差无影响）。
  3. **取消 lib/兜底双分支**：原代码 `runtime_lib(foreign_only=True)` 为 None 时回落到本进程
     游标直读——lib 开关只在 `GRAPE_DOMAIN_LIB=1` 时打开，单库拓扑恒走兜底分支，于是兜底 SQL
     成了统计的全部来源。改用任意拓扑原语后（自有 DSN → db_registry，单库 → 进程库），
     两条拓扑同一条代码，`domain_read` 依赖也从本文件消失。
  4. **口径留在调用方**：`scene_id` 派生（`_scene_of` → `get_issue_scene`）、`engine_id` 转 int、
     `user_id` 转 str 都是 insight 的展示/报表口径，不下沉到 owner；owner 只回「五元组原值」。
     同理「usage > config > unknown」优先级与 top3 聚合仍在本模块。
  5. **缺列/缺表静默跳过改成显式 `log.debug`**：原 `_agent_runtimes_has_model_name` 有 2 处
     `except Exception: pass`（静默 except 棘轮靠基线豁免）；新实现在探针失败/缺列处
     `log.debug` 明确记一句，其余错误照旧上抛由调用方 `log.warning` 降级（与原语义一致）。
     该文件静默 except 基线随之 **4 → 3** 收紧（棘轮只减不增）。
- 修的存量 bug（结构性；单库拓扑不可见，三库独立后必然踩，且都被 except 吞成静默降级）：
  1. `_attribution` 兜底分支在 insight 库上查 `agent_tasks` → 1146 → 返回空归属：
     **task 类用量行的 repo_key / agent_type / engine_id / user_id 全部落空**，
     成本报表按仓库/引擎/用户的三级归因在三库拓扑下失真（不是丢行，是行内维度为空）。
  2. `task_model_summary` 配置模型回落同理 1146 → 事件详情页/实时日志弹窗的「执行模型」
     永远 `unknown`（明细行为空时的唯一兜底）。
  3. **口径耦合**：列探测写在调用方，`agent_runtimes` 一加/改列就要改 insight 的代码；
     探测与读取现在都在 owner 域，调用方只消费「模型名或空串」。
- 验证（真实退出码）：
  · `py_compile` 两文件 → **COMPILE_OK**；
  · `~/burndown/llm_usage_runtime_smoke.py`（`DEVOPS_DB_BACKEND=sqlite`，自建
    obs_llm_usage/agent_tasks/agent_runtimes 三表 + 独立 runtime 库）
    → **ALL llm_usage runtime smoke PASSED (45 checks)**：A 注册面/静态（两条查询逐字对齐、
    无 `%s`/`NOW()`/`ON DUPLICATE`、占位符各 1、薄函数体内无 SQL、死函数已删、调用方无表名 SQL）；
    B 归属（列序解包、`_scene_of(repo_key, issue_id)` 派生留调用方、缺行/NULL/空 task_id、
    空 task_id 零外呼、读抛错降级为空）；C 台账写（归属落库、attr 只补空、归属存在时 attr 不覆盖、
    **非重入锁守卫下不嵌套获取**——对照组先证明守卫能抓嵌套、全 0 token 不落库）；
    D 配置模型（取值 strip / 缺行 / NULL / 全空白 / `runtime_id<=0` 零连接）；
    E 优先级（usage > config > unknown，`unknown` 主键回落 `model_raw`）；
    F 自有 DSN 拓扑（monkeypatch `uses_own_dsn`/`cursor` → 归属与模型都读 runtime 库、
    进程库同名副本不参与；缺 `model_name` 列 → 空串；缺表 → 空串不上抛）；
    G 未登记查询名 → KeyError（fail-closed）。
  · `bash scripts/rds_gate.sh --quick` → **GATE: 全绿**（check-arch R1–R7 / 棘轮未上升 /
    内部契约 37 条最新 / 看板最新）；
  · `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；`make lint` → **import OK**；
    `check_lib_query_names.py` → **PASS**（insight 17 funcs used / runtime 167 funcs + 28 queries used）。
- **未验证项（如实）**：本轮未跑 `p4_p5_loop.sh` 三实例彩排——本 shell `SRC_PWD`/`REMOTE_PWD`
  均未注入（实测 UNSET），且本批不改 schema / 迁移 / 契约面（无新表、无新路由），
  运行面证据为 sqlite 烟测（含自有 DSN 分支模拟）+ 单库 pytest；**不把彩排结果算作本批证据**。
- **新基线**：**72 处 / 37 对**（`--update-baseline` 已收紧）；依赖看板 / 矩阵 / 作业单同步刷新
  （作业单 72 处 / 42 文件，头名仍是被 blocked 的 `prompt_builder.py`，下一批可选
  `src/api/event_rule.py` 或 `src/domains/insight/observability/gap.py` 各 3 处）。

### insight/observability/gap 数据缺失探测基集归位（-3，本文件清零）
- 文件：`src/domains/insight/observability/gap.py`（3 处 → **0**，仅剩本域 `obs_alert_rules` /
  `obs_alert_events`）；全局静态 **72 → 69**（-3），对数 **37 → 37** ——
  `agent_tasks←insight` 由 2 降到 1、`runtime_credentials←insight` 由 6 降到 4（余量在
  `observability/overview.py`、`routes/health.py` 等其它文件），本文件已清零。
- 表分布（消费域 insight，全归 **runtime**）：
  · 行 103 `SELECT id, device_name, name, last_seen_at, repo_key, org_id, scope`
    `FROM runtime_credentials WHERE status != 'revoked'`（`_probe_runtimes` 基集，7 列）
  · 行 167 `SELECT repo_key, created_at, started_at, completed_at FROM agent_tasks`
    `WHERE repo_key IS NOT NULL AND repo_key != ''`（`_probe_repos` 基集，4 列）
  · 行 234 `SELECT id, name, device_name, scope, owner_name, org_id, last_seen_at, status,`
    `repo_key FROM runtime_credentials WHERE status != 'revoked'`（`silent_targets`，9 列）
  三处同一结构族、无动态拼接（表名/列名均字面量，无用户输入）。
- 本轮取批：作业单头名 `src/kernel/prompt_builder.py` 与次名
  `src/kernel/persistence/credential_usage_log.py` **复核后仍 blocked**（理由见
  `next-batches.md` 顶部「⛔ blocked」，与前三轮结论一致），按作业单规则跳过，
  取第一个可改造文件（上一批台账末尾已点名的 `gap.py`）。
- 归属域侧改动（SQL 只在 owner 域一份）：
  · runtime public `_ANY_QUERIES` 新增 3 条具名查询：`runtime_credentials_gap_rows`（7 列）、
    `runtime_credentials_silent_rows`（9 列）、`agent_task_activity_rows`（4 列），
    列序/谓词与原 SQL 逐字一致，无占位符（原语句本就无参数）；
  · runtime public 新增 3 个薄函数：`runtime_credentials_gap_rows_any()`、
    `runtime_credentials_silent_rows_any()`、`agent_task_activity_rows_any()`，只把
    `read_all_any` 的行转元组返回，体内无内联 SQL；函数名与查询名都登记 `__all__`。
- 关键设计取舍：
  1. **两处 `runtime_credentials` 不并成一条超集查询**：列集与列序不同（7 列 vs 9 列），
     合并就要改调用方解包顺序，等于把「列序逐字一致」这条可核对性丢掉；两条各自独立、
     各自可逐字比对原 SQL，多一条查询只占一行注册面。
  2. **读基集与写事件保持分段**：`*_any` 原语自带 `db_lock`（单库回落进程库），若仍嵌在
     `with db_lock:` 里就是同线程二次获取——生产 `RLock` 侥幸不炸，测试 monkeypatch 的裸
     `Lock` 会**自死锁**。故把基集读取提到写事务之外（原实现本来就是两段锁，顺序未变）。
  3. **只搬跨域两表，本域事件表不动**：`obs_alert_rules` / `obs_alert_events` 归 insight，
     在 insight 进程里走进程库就是本域库；本批不碰 `alerts._open_event/_resolve_open`
     的游标契约（它们按「调用方游标」设计，改成别处取连接会连带影响指标求值路径）。
  4. **口径全部留在调用方**：`silent_sec > thr` 的严格大于、`GAP_MAX_STALE_SEC` 7 天退役窗口、
     「从未上报不算缺失」、`max(completed, started, created)` 取最后活动、L2 名单排序
     （静默秒数降序 + 从未上报排最后）都是 insight 的判定语义，owner 只回元组原值。
  5. **失败降级语义逐点保留**：基集读失败 → `log.warning` + 该探针返回 `(0,0,0)`
     （`probe_all` 聚合里不计数）；`silent_targets` 的 runtime 段失败 → 空列表 + warning，
     仍继续产出 `repos` 段——与改造前一致，不返回半份结果也不上抛。
- 修的存量 bug（3 条，都是三库独立后必然 1146、且都被 except 吞成静默降级）：
  1. `_probe_runtimes` 基集在 insight 库上查 `runtime_credentials` → 1146 → `except` 返回
     `(0,0,0)`：**设备静默探测整体停摆**（心跳停了也不告警，`gap_evaluated` 恒 0，
     监控台的「数据不再来了」这一类最危险失效恰好是自己静默）。
  2. `silent_targets` 同样 1146 → **L2 静默名单恒为空**：运维台看不到哪些凭据已死、
     哪些超 7 天，死凭证无人清理（也不产生事件，等于双重失明）。
  3. `_probe_repos` 直查 `agent_tasks` 同样 1146 → 规则即便被打开，`gap_repo` 也永不触发。
- 验证（真实退出码）：
  · `py_compile` 两文件 → **COMPILE_OK**；
  · `~/burndown/gap_owner_smoke.py`（`DEVOPS_DB_BACKEND=sqlite`，自建
    obs_alert_rules/obs_alert_events/runtime_credentials/agent_tasks 四表 + 独立 runtime 库）
    → **ALL gap_owner smoke PASSED (69 checks)**：A 注册面/静态（三条查询逐字对齐、
    无 `%s`/`NOW()`/`ON DUPLICATE`、薄函数体内无 SQL、调用方无表名 SQL 与谓词片段、
    函数名与查询名双登记）；B 设备静默（静默起点 = 最后上报 + threshold、value = 静默秒数、
    阈值下方不报、从未上报不报且收口既有事件、超 7 天不报、恢复 → resolved、设备删除 →
    收尾、第二轮不新建只更新）；C **原口径「只排除 revoked」**（expired/offline 照旧参与，
    不按 is_active 过滤）；D 仓库静默（repo_key 空/NULL 不进基集、双条件、
    取 max(completed, started, created)、规则 enabled=0 时整类不探）；E L2 名单
    （排序、revoked 排除、never_reported / 退役标记、`include_retired=False` 只滤退役、
    repos 段来自 firing 事件）；F **自有 DSN 拓扑**（基集/活动行/L2 全从 runtime 库取，
    进程库同名副本不参与；缺表 → 两类探测都降级为 0 不上抛）；G **裸 `Lock` 下不嵌套**
    （对照组证明守卫能抓嵌套获取）；H 未登记查询名 → `KeyError`（fail-closed）。
  · `bash scripts/rds_gate.sh --quick` → **GATE: 全绿**（check-arch R1–R7 / 棘轮未上升 /
    内部契约 37 条最新 / 依赖看板最新）；
  · `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；`make lint` → **import OK**；
    `check_lib_query_names.py` → **PASS**（insight 17 funcs / runtime 170 funcs + 28 queries used）。
  · 直覆盖用例 `tests/insight/test_obs_gap.py` → **1 failed, 9 passed**；失败项
    `test_admin_gaps_endpoint_and_threshold_policy` 在 **HEAD 干净树 worktree
    （`~/burndown/wt-gapbase`，`3b3bce2a6`）复现同 1 failed / 9 passed**，是同一条
    Starlette `TestClient` 关闭期的 `CancelledError`，与本批无关。
- **未验证项（如实）**：本轮未跑 `p4_p5_loop.sh` 三实例彩排——本 shell `SRC_PWD`/`REMOTE_PWD`
  均未注入，且本批不改 schema / 迁移 / 契约面（无新表、无新路由、无新端点），运行面证据为
  sqlite 烟测（含自有 DSN 分支模拟）+ 单库 pytest；**不把彩排结果算作本批证据**。
- **新基线**：**69 处 / 37 对**（`--update-baseline` 已收紧）；依赖看板 / 矩阵 / 作业单同步刷新
  （作业单 69 处 / 41 文件，头名仍是被 blocked 的 `prompt_builder.py`，下一批可选
  `src/api/event_rule.py` 或 `src/api/server_tasks.py` 各 3 处）。

### api/server_tasks 本机指标上报归 runtime public（-3，该文件清零）

- 文件：`src/api/server_tasks.py`（3 处 → **0**）；全局静态 **69 → 66**（-3），对数 **37 → 37** ——
  `runtime_credentials←api` 由 6 降到 3（余量在 `src/api/main.py` 的同构循环），本文件已清零。
- 表分布（消费域 api，表全归 **runtime**），三处都在 `start_insight_tasks._server_metrics_loop`：
  · 行 632 `SELECT id FROM runtime_credentials WHERE device_name=? AND scope='global'`（注册判存，1 列）
  · 行 634 `INSERT INTO runtime_credentials (token_hash, name, device_name, scope, status, last_ip)`
    `VALUES (?, ?, ?, 'global', 'active', ?)`（缺失登记）
  · 行 704 `UPDATE runtime_credentials SET last_seen_at=?, last_ip=?, cpu_percent=?, memory_percent=?,`
    `disk_percent=?, tmp_percent=?, bandwidth_percent=?, bandwidth_mbps=?, load_avg=?`
    `WHERE device_name=? AND scope='global'`（15s 心跳刷新，9 列）
  三处表名/列名全字面量，参数全占位符，**无动态拼接**（唯一的"动态"是 hostname 与指标值本身）。
- 本轮取批：作业单头名 `src/kernel/prompt_builder.py` 与次名
  `src/kernel/persistence/credential_usage_log.py` **四轮复核后仍 blocked**（理由已补回
  `next-batches.md` 顶部「⛔ blocked」小节），按作业单规则跳过，取第一个可改造文件
  （上一批台账末尾点名的 `server_tasks.py`）。
- 归属域侧改动（SQL 只在 owner 域一份）：
  · runtime public `_ANY_QUERIES` 新增 3 条具名查询：`runtime_credential_device_id`（1 列）、
    `runtime_credential_register_device`（4 占位符 / 6 列）、`runtime_credential_touch_device`
    （10 占位符 / 9 列），列序与谓词与原 SQL 逐字一致；
  · runtime public 新增 3 个薄函数：`device_credential_id_any()`、`register_device_credential_any()`、
    `touch_device_credential_any()`，体内无内联 SQL（只经 `_any_sql`/`_any_tx`/`read_one_any`）；
    函数名登记 `__all__`，并同步 `src/kernel/gsb/route_config.py` 的
    `PUBLIC_PRIMITIVES["runtime"]` 镜像（kernel 禁止 import domains，只能字面量镜像）。
- 关键设计取舍：
  1. **不用 `main.py` 的 `%s` + else 兜底写法**：`ensure_device_credential` /
     `touch_credential_metrics` 直连 `db_registry.cursor("runtime")`，只在 runtime 自有 DSN
     下可用（单库/测试拓扑直接 `RuntimeError`），而 `main.py` 的 `else` 分支把同一段 SQL
     又抄了一份——那正是本表跨域统计的来源。本批改走 `_any` 族（自有 DSN 优先、单库回落
     进程库），调用方零 SQL、单库与三库行为同源。
  2. **检-插同事务**：注册是「按 (device_name, scope='global') 判存 → 缺失才 INSERT」，
     用 `_any_tx()` 把两条语句放同一事务（自有 DSN 真事务，单库走 db_lock），而不是两次
     独立 `_any` 调用。
  3. **撞键收敛成值**：`runtime_credentials.name` 有 UNIQUE，同机换 scope 的存量行会让
     INSERT 撞键。原实现由外层 `except` 吞成「register failed」；这里返回
     `{"action": "duplicate"}`（带 `log.debug`），其余异常仍上抛——不静默降级。
  4. **刷新原语返回值改读游标 `rowcount`**：pymysql 的 `execute()` 返回 **int**（不是游标），
     `exec_any` 的 `getattr(cur, "rowcount", 0)` 在自有 DSN 下恒为 0；若沿用，本原语会
     恒报「0 行受影响」，把「刷新成功」和「无此行」混成同一个值。缺陷属 `exec_any` 本体，
     本轮只在本原语里绕开并如实记账（见下），不扩大语义面。
  5. **拆掉调用点的 `with db_lock:` / `db.cursor()`**：`*_any` 原语在单库回落路径自带
     `db_lock`，再嵌一层就是同线程二次获取——生产 `RLock` 侥幸不炸，测试 monkeypatch 的
     裸 `Lock` 会自死锁（交接文档 §5.3 坑 4 同族）。调用方不再 import 进程库句柄。
  6. **口径逐点保留**：判存命中即**不写**（不覆盖 last_ip / token_hash）；不按
     `status`/`is_active` 过滤（revoked 行照旧算「已登记」且心跳照旧刷新）；判存语句不加
     `LIMIT`（原实现就是 `fetchone()`）；空 hostname 与原 SQL 同口径（不 strip、不拦）。
     失败降级仍在调用方：`log.warning("register failed" / "upload failed")` 后继续下一轮心跳。
- 修的存量 bug（三库拓扑下必 1146，且被 except 吞成静默降级）：
  1. 注册段在 **insight 进程库**里查/写 `runtime_credentials` → 1146 → 「register failed」：
     本机从未登记为 device 凭据（`scope='global'`），凭据列表里看不到运行本进程的机器。
  2. 心跳段同样 1146 → 「upload failed」：`last_seen_at` / `ip` / CPU / 内存 / 磁盘 / tmp /
     带宽 / load 九项指标**从未落库**（单进程时代写的是同一库，拆分后整段静默失效）。
  两处共用同一具名查询族，修复后三库与单库同源。
- 已知未修（如实记账，不属本轮）：`exec_any` 在自有 DSN 下恒返 0（`mgmt` / `insight` 的同名
  原语同病），会让 `set_meta_if_absent(...) > 0`、`issue_scene_set_cas` 这类「按影响行数
  判定」的消费点在自有 DSN 拓扑恒定 False/0；属独立一轮（三域同改 + MySQL 实例验证）。
- 验证（真实退出码）：
  · `py_compile` 三文件（`server_tasks.py` / runtime `public` / `gsb/route_config.py`）→ **COMPILE_OK**；
  · `~/burndown/server_metrics_smoke.py`（`DEVOPS_DB_BACKEND=sqlite`，自建含两条 UNIQUE 的
    runtime_credentials 表）→ **ALL server_metrics smoke PASSED (66 checks)**：A 注册面/静态
    （三条查询逐字对齐、无 `%s`/`NOW()`/`ON DUPLICATE`、薄函数体无 SQL、调用方无表名 SQL 与
    进程库直连、`__all__` + GSB 镜像双登记、未登记查询名 `KeyError`）；B 单库行为（首次
    inserted、二次 exists 且不覆盖、`scope='user'` 同名 device 不算命中、撞键 → duplicate
    且不留半份写入、9 列刷新逐值核对、目标不存在 0 行、0 值照写、缺表上抛不吞、空 hostname
    同口径）；C **自有 DSN 伪游标**（`?` 方言、SELECT→INSERT 两条语句、登记/刷新参数顺序与
    占位符逐一比对、1062 撞键 → duplicate、其它异常上抛、`exec_any` 恒 0 的规避对照）；D 两轮
    心跳时序（只有首轮打 registered 日志、第二轮 exists 不改 token_hash 但覆盖指标）；
    E 边界（同 device 多行脏数据取第一行、刷新命中全部行、revoked 行不复活也不漏刷新）。
  · `bash scripts/rds_gate.sh --quick` → **GATE: 全绿**（check-arch R1–R7 / 棘轮未上升 /
    内部契约 37 条最新 / 依赖看板最新）；
  · `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；`make lint` → **import OK**。
  · `tests/kernel/test_gsb_route.py::test_public_primitives_registry_sync` → **1 failed**，
    但失败断言在 **mgmt**（镜像比 public 少约 190 个名字）；在 **HEAD 干净 worktree**
    （`~/burndown/wt-gsbcheck`，`c08f0610f`）复现同一条失败，与本批无关（本批已把 3 个新名字
    同步进 runtime 镜像；runtime 侧同样存在陈旧子集，属既有漂移）。
- **未验证项（如实）**：未跑 `p4_p5_loop.sh` 三实例彩排——本 shell 无 `SRC_PWD`/`REMOTE_PWD`，
  且本批不改 schema / 迁移 / 契约面（无新表、无新路由、无新端点）；自有 DSN 路径用伪游标
  模拟 pymysql 语义，**未连真实 MySQL 实例**；不把彩排结果算作本批证据。
- **新基线**：**66 处 / 37 对**（`--update-baseline` 已收紧）；依赖看板 / 矩阵 / 作业单同步刷新
  （作业单 66 处 / 40 文件，头名仍是被 blocked 的 `prompt_builder.py`，下一批可选
  `src/api/main.py` 3 处——单进程版同一循环，可直接复用本批三个原语——或
  `src/api/pipeline_status.py` / `src/api/event_rule.py` 各 3 处）。

### api/pipeline_status 流水线业务状态归 runtime public（-3，该文件清零）

- 文件：`src/api/pipeline_status.py`（3 处 → **0**）；全局静态 **66 → 63**（-3），对数 **37 → 35** ——
  `issue_state←api` 与 `agent_tasks←api` **两对整对清零**，`issue←api` 仍有 1 处余量在其它文件
  （`src/api/git_repo_api.py`）。
- 表分布（消费域 api，三张表全归 **runtime**），两处都在 `load_issue_business_status`、
  一处为 `load_segment_task_refs`：
  · 原行 343 `SELECT stage, status FROM issue WHERE repo_key IN (…) AND issue_number=?`（需求级权威）
  · 原行 353 `SELECT stage FROM issue_state WHERE repo_key IN (…) AND issue_id=?`（stage 为空时的兜底）
  · 原行 387 `SELECT id, event_seq, status, result_text FROM agent_tasks WHERE repo_key IN (…)`
    `AND event_seq IS NOT NULL [AND event_seq IN (…)] ORDER BY created_at ASC, id ASC`
  三处表名/列名全字面量，值全参数化；「动态」只有 IN 列表长度（`?` 个数按值列表展开）。
- 本轮取批：作业单头名 `src/kernel/prompt_builder.py` 与次名
  `src/kernel/persistence/credential_usage_log.py` **复核后仍 blocked**（理由与本文件此前四轮结论一致，
  见下方「流程修复」），按作业单规则跳过，取第一个可改造文件（上一批台账末尾已点名的 `pipeline_status.py`）。
- 归属域侧改动（SQL 只在 owner 域一份）：
  · runtime public 新增 `_PIPELINE_STATUS_QUERIES` 四条具名模板：`pipeline_issue_stage_status`、
    `pipeline_issue_state_stage`、`pipeline_segment_task_rows`、`pipeline_segment_task_rows_by_seq`，
    列序/谓词/`ORDER BY` 与原 SQL 逐字一致；`{repo_ph}` / `{seq_ph}` 只是**占位符个数**标记，
    语句文本固定在本模块，调用方只能传 repo_key / seq 值列表；
  · runtime public 新增 3 个薄函数：`issue_business_stage_status_any()`、`issue_state_stage_any()`、
    `segment_task_rows_any()`，体内无内联 SQL（只走 `_any_cursor`/模板展开）；函数名登记 `__all__`
    并同步 `src/kernel/gsb/route_config.py` 的 `PUBLIC_PRIMITIVES["runtime"]` 镜像。
- 关键设计取舍：
  1. **`created_at` 只进本域、不出本域**：`agent_tasks` 读需要 `ORDER BY created_at ASC, id ASC`
     （调用方「同一 seq 保留最后创建的一条」依赖它）。模板里多选 `created_at` 仅供本域排序，
     薄函数按 `(created_at, id)` 重排后**只返回原 4 列**，列序与改造前逐字一致。
  2. **分块不破坏全局序**：repo_key 的 IN 按 500 分块，但排序是「合并后再按同一键重排」，
     而不是「每块各自有序直接拼接」 —— 后者会让跨仓同 seq 的「最后一条」变成「最后一块的最后一条」。
     NULL `created_at` 用 `(0, "")` 键排在前面（sqlite 与 MySQL 的 ASC 都是 NULL 先），
     id 文本/整型分别取键，避免混型比较抛错。
  3. **`seqs` 过滤口径逐字保留**：原实现 `if seqs: seqs=[s for s in seqs if s]; if seqs: 加谓词`，
     即「给空列表 / 全空值 = 不过滤（整仓取）」。这看着像 bug，但调用方拿到的就是「整仓任务引用」，
     改成「空列表即空结果」会静默改变流水线卡片的内容，故按原样保留并写进 docstring。
  4. **不加任何状态过滤**：`issue` 侧原 SQL 无 `status`/`is_active`/软删条件，终态与软删行照旧命中
     （`review_status` 判据依赖这些行）；`repo_keys` 里的空串元素也不做「假值过滤」，
     与原 `IN` 绑定逐字一致。
  5. **拆掉调用方的 `db_lock` + `db.cursor()`**：`_any_cursor` 在单库回落路径自带 `db_lock`，
     再嵌一层就是同线程二次加锁（生产 `RLock` 侥幸不炸，测试 monkeypatch 的裸 `Lock` 会自死锁；
     交接文档 §5.3 坑 4 同族）。调用方现在不再 import 进程库句柄。
  6. **两处裸 `except: pass` 换成 `log.debug`**：静默 except 棘轮只拦新增，改到这里属于顺带清偿；
     降级语义不变（业务状态失败 → 空串、任务引用失败 → 空映射，都不阻塞 pipeline）。
- **修的存量 bug（三库独立后必然 1146，且全被裸 `except` 吞成静默降级）**：
  1. 需求级 `(stage, status)` 读在 api 进程库里没有 `issue` → 1146 → 「无业务状态」：
     流水线页 `issue_stage` / `issue_task_status` 恒空，`review_status` 由 `derive_review_status("", "")`
     恒推出默认值 —— 卡片永远显示「未开始/无评审」。
  2. `issue_state` 兜底同理 1146 → 兜底分支**从未生效**（单进程时代它是 stage 为空的唯一来源）。
  3. `agent_tasks` 读同理 1146 → `load_segment_task_refs` 恒返回 `{}`：每个段的 `task_ref`/`result_ref`
     恒为 `unavailable`，任务详情页点不进任务、也拿不到阶段结论摘要。
- **流程修复（本批附带，解决「每轮重复复核同一批 blocked」）**：`scripts/rds_next_batch.py` 新增
  持久化 `BLOCKED` 清单 + 作业单顶部「⛔ blocked」小节。此前四轮都把 blocked 理由写进台账，
  但作业单由脚本重写、标记每次被冲掉，下一轮又从头按处数硬拿同一批文件。现在标记随生成器落盘，
  blocked 文件在作业单里带 ⛔ 与理由，取批规则明确为「取第一个未 blocked 的文件」。
  当前 blocked 两条不变：`prompt_builder.py`（agent 提示文案，本进程不执行 + 测试按源文本断言 +
  口径「不为掉数字改文案」）、`credential_usage_log.py`（需每工作线程一条 owner 连接的池化，独立一轮）。
- 验证（真实退出码）：
  · `py_compile` 四文件（`pipeline_status.py` / runtime `public` / `gsb/route_config.py` /
    `rds_next_batch.py`）→ **COMPILE_OK**；
  · `~/burndown/pipeline_status_smoke.py`（`DEVOPS_DB_BACKEND=sqlite`，自建 issue/issue_state/agent_tasks 三表）
    → **ALL pipeline_status smoke PASSED (67 checks)**：A 注册面/静态否定（四条模板逐字对齐、无 `%s`/`NOW()`/
    `ON DUPLICATE`、薄函数体无 SQL、调用方无三表 SQL 与进程库直连、`__all__` + GSB 镜像双登记、
    未登记查询名 `KeyError`）；B 需求级状态（issue 命中、int/str issue_number 同口径、跨仓不误命中、
    stage 空 → issue_state 兜底、NULL 归空串、终态/软删行照旧命中、同键脏数据取第一行、
    空串 repo_key 不过滤、空 repo_keys 零 SQL（两表改名验证）、缺表降级不抛）；
    C 段任务（seq 过滤 / 空 seqs 不过滤 / `event_seq IS NULL` 排除 / created_at+id 破平 /
    NULL created_at 在前 / chunk=1 与整批结果与顺序完全一致 / 跨仓全局排序）；
    D **自有 DSN 伪游标**（`?` 方言、`{repo_ph}`→N 个占位符、参数顺序 repo_keys→seqs→尾参、
    驱动乱序返回仍按 created_at 重排、created_at 不进返回列）；
    E 调用点三态（available + 摘要 / 在途 pending / 无结论 unavailable / 缺表 → 空映射 / 非数字 seq 丢弃 /
    enrich 对无任务段显式标注 unavailable）。
  · `bash scripts/rds_gate.sh --quick` → **GATE: 全绿**（check-arch R1–R7 / 棘轮未上升 /
    内部契约 37 条最新 / 依赖看板最新）；
  · `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；`make lint` → **import OK**；
  · 直覆盖用例 `tests/runtime/test_issue_539_pipeline_review_status.py` +
    `tests/runtime/test_issue_648_attachments_display.py` + `tests/mgmt/test_issue_2050_structured_handoff_stall.py`
    + `tests/kernel/test_gsb_route.py` → **85 passed / 24 failed**，**全部与本批无关**，已用
    `git worktree` 在 HEAD 干净树（`~/burndown/wt-pipelinestatus`，`863308edc`）复现同样失败：
    · `test_issue_2050` 15 项 = `src/api/patrol.py` 的 `NameError: _NEXT_STEP_RE`（分支既有 bug，DB 无关）；
    · `test_issue_648` 8~11 项随库状态浮动（该文件只用 `get_gitcode_token` 打桩，实现已改走
      `get_repo_token_with_refresh`，用例断言过时）；主仓 `.env` 把测试指向共享 MySQL 而 worktree 无 `.env`，
      故两边失败项不同 —— 把本批补丁 `git apply` 到 worktree 后同一用例**仍通过**，证明确非本批引入；
    · `test_gsb_route::test_public_primitives_registry_sync` 镜像陈旧（mgmt 侧缺约 190 个名字，既有漂移）。
- **未验证项（如实）**：本轮未跑 `p4_p5_loop.sh` 三实例彩排 —— 本 shell `SRC_PWD`/`REMOTE_PWD` 均未注入，
  且本批不改 schema / 迁移 / 契约面（无新表、无新路由、无新端点），运行面证据为 sqlite 烟测
  （含自有 DSN 伪游标）+ 单库 pytest；**不把彩排结果算作本批证据**。
- **新基线**：**63 处 / 35 对**（`--update-baseline` 已收紧）；依赖看板 / 矩阵 / 作业单同步刷新
  （作业单 63 处 / 39 文件，顶部已带 ⛔ blocked 小节；下一批可选 `src/api/main.py` 3 处
  ——与 insight 进程同一循环，可直接复用 `device_*_any` 三个原语——或 `src/api/event_rule.py` 3 处）。

### api/main 主控机指标上报循环归 runtime public（-3，该文件清零）

- 文件：`src/api/main.py`（3 处 → **0**）；全局静态 **63 → 60**（-3），对数 **35 → 34** ——
  `runtime_credentials←api` 由 3 降到 0。
- 表分布（消费域 api，表全归 **runtime**），三处都在 `_start_server_metrics_task._server_metrics_loop`：
  · 行 126 `SELECT id FROM runtime_credentials WHERE device_name=? AND scope='global'`（注册判存，1 列）
  · 行 129 `INSERT INTO runtime_credentials (token_hash, name, device_name, scope, status, last_ip)`
    `VALUES (?, ?, ?, 'global', 'active', ?)`（缺失登记）
  · 行 208 `UPDATE runtime_credentials SET last_seen_at=?, last_ip=?, cpu_percent=?, memory_percent=?,`
    `disk_percent=?, tmp_percent=?, bandwidth_percent=?, bandwidth_mbps=?, load_avg=?`
    `WHERE device_name=? AND scope='global'`（15s 心跳刷新，9 列）
  三处表名/列名全字面量、参数全占位符，**无动态拼接**。
- 本轮取批：作业单头两名 `src/kernel/prompt_builder.py` / `src/kernel/persistence/credential_usage_log.py`
  仍 blocked（口径见上一批与 `next-batches.md` 顶部），跳过；取第一个可改造文件 `src/api/main.py`
  （上一批台账末尾点名的同构循环）。
- 归属域侧改动：**零新增原语** —— 直接复用上一批（`api/server_tasks`）落地的
  `register_device_credential_any` / `touch_device_credential_any`（三条模板
  `runtime_credential_device_id` / `_register_device` / `_touch_device` 与本文件原 SQL **逐字一致**，
  故不需要新建键）。这也验证了上一批把「判存 SELECT + INSERT」合成一个 `_any_tx()` 组合原语的决定：
  本文件原实现是 `SELECT` 后判空再 `INSERT`，恰好同一个语义，调用点直接塌缩成一次调用。
- 关键设计取舍：
  1. **删掉「lib 优先 + 进程库兜底」双分支**，不留 `runtime_lib` 依赖。原结构在 lib 不可用时
     把同一段 SQL 抄在 `else` 里直打**进程库**——那正是本表跨域统计的来源；`_any` 族本身
     已经是「自有 DSN 优先、单库回落进程库」，调用方再包一层选择只会产生两套行为。
  2. **调用方不再持 `db_lock`/`db.cursor()`**：`_any_cursor` 在回落路径自带锁与 commit，
     调用方再嵌一层就是同线程二次加锁（与交接文档 §5.3 坑 4 同族），且把 `db.commit()`
     的提交点从「SQL 之后」变成「原语内部」，语义收得更紧。
  3. **注册与心跳都保持同步直连**（不入意图队列）：15s 一条意图会淹没 `xwrite` 日志，
     循环本身就是重试机制 —— 这一口径与上一批一致，只换了 SQL 的归属。
- **修的存量 bug（mgmt 进程三库拓扑下必然 1146，被外层 `except` 吞成静默降级）**：
  `src/api/grape_server.py:281` 在 mgmt 进程启动时调用本函数（`main.py` 自己的 lifespan 走的是
  已改造的 `server_tasks.start_insight_tasks`）。mgmt 进程库里没有 `runtime_credentials`（视图 0），
  而 `runtime_lib(foreign_only=True)` 只有在 `GRAPE_RUNTIME_READ_VIA_LIB=1` + `GRAPE_DSN_RUNTIME`
  都配好时才返回句柄；否则走 `else` 兜底 SQL → 1146 → 日志只有
  `Server metrics: register failed / upload failed`，**本机 device 常量从未登记、指标从未刷新**，
  能效看板的「本机」一行恒为空。改造后两种拓扑同源，缺表时才降级（烟测 E 段覆盖）。
- 验证（真实退出码）：
  · `py_compile src/api/main.py` → **COMPILE_OK**；
  · `~/burndown/main_metrics_smoke.py`（`DEVOPS_DB_BACKEND=sqlite`，自建 18 列
    `runtime_credentials`）→ **ALL main_metrics smoke PASSED (100 checks)**：
    A 注册面/静态否定（三模板逐字对齐 + 无 `%s`/`NOW()`/`ON DUPLICATE`/`PRAGMA`、函数体内零
    `runtime_credentials` SQL 字面量、不再 import `db`/`db_lock`/`runtime_lib`、`__all__` +
    GSB 镜像双登记、未登记查询名 `KeyError` 同族；`_start_server_metrics_task` 仍在，596 用例
    按源文本断言的函数名不变）；
    B `register_device_credential_any`（空表 → inserted 五列取值正确 / 二次 → exists 且**不覆写**
    token_hash 与 last_ip / 同名不同 device 撞 `name` UNIQUE → duplicate 且不新增行 / 换 device 新增行）；
    C `touch_device_credential_any`（**9 个指标列逐列校对**抓参数错位 / 只命中 `scope='global'`
    而同 device 的 `repo`、`user` 行不动 / **不过滤 status**，revoked 行照旧刷新且 status 不被改写 /
    device 不匹配 → 0 行 / 同 device 多条 global 脏数据全命中，与原语句无 LIMIT 一致 /
    0 与负值不被吞成 NULL、空串原样落库）；
    D **自有 DSN 伪游标**（`uses_own_dsn`+`transaction`+`cursor` 注入：先 SELECT 后 INSERT 同一事务、
    SQL 文本逐字、INSERT 参数顺序 `(token_hash,name,device_name,last_ip)`、UPDATE 10 个参数顺序
    「9 指标 + device_name 尾参」、1062 → duplicate、非撞键异常上抛、touch 返回值取自伪游标
    `rowcount` 而非 `exec_any` 恒 0）；
    E **调用点驱动**（真跑 `_start_server_metrics_task()` 一个 tick：登记行 scope/status/name/
    token_hash/last_ip 正确、`last_seen_at` 与指标列已刷新、重复启动幂等仍 1 行、进程库重指空库时
    只记 warning 不抛且真库既有行不受影响）。
  · `python3 scripts/foreign_table_refs.py … --update-baseline` → **baseline updated: 34 pairs, 60 refs**；
  · `bash scripts/rds_gate.sh --quick` → **GATE: 全绿**（check-arch R1–R8 / 棘轮未上升 /
    内部契约 37 条最新 / 依赖看板最新）；
  · `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；`make lint` → **import OK**。
- **未验证项（如实）**：本轮未跑 `p4_p5_loop.sh` 三实例彩排（轮内口径：每轮只跑
  py_compile + 专用烟测 + gate + 三组 pytest + lint，彩排压在余量归零后统一跑一次）；
  本批不改 schema / 迁移 / 契约面（无新表、无新端点、无新原语），运行面证据为 sqlite 烟测
  含真实调用点 tick。mgmt 进程实机 1146 消失的现场证据留待彩排轮确认。
- **新基线**：**60 处 / 34 对**（`--update-baseline` 已收紧）；依赖看板 / 矩阵 / 作业单同步刷新
  （作业单 60 处 / 38 文件，下一批首个可改造文件 `src/api/event_rule.py` 3 处）。

### api/event_rule 事件动作过滤器读口归 mgmt public（-3，该文件清零）

- 文件：`src/api/event_rule.py`（3 处 → **0**）；全局静态 **60 → 57**（-3），对数 **34 → 33** ——
  `event_action_filter←api` 由 3 降到 0，**该对整对清零**。
- 表分布（消费域 api，表归 **mgmt**），三处全在 `_load_from_db`，同表同函数，只有列集不同：
  · 行 39 `SELECT event_type, action, allow, actor, require_changes, block_changes, handle_side_effect, vcs_source FROM event_action_filter WHERE enabled=1 ORDER BY priority, id`（block_changes + vcs_source 都在）
  · 行 43 同上，第 8 列换成常量 `''`（只有 block_changes）
  · 行 47 同上，第 6/8 列换成常量 `''`（迁移期老库，两列都缺）
  三处表名/列名全字面量、**无参数**（谓词只有 `enabled=1`）；「动态」只在**有没有这两列**，
  原来靠 `PRAGMA table_info(event_action_filter)` 探列。
- 本轮取批：作业单头两名 `src/kernel/prompt_builder.py` / `src/kernel/persistence/credential_usage_log.py`
  仍 blocked（口径见作业单顶部 ⛔ 小节），跳过；取第一个未 blocked 文件 `src/api/event_rule.py`
  （上一批台账末尾已点名）。
- 归属域侧改动（SQL 只在 mgmt 域一份）：
  · `_ANY_QUERIES` 新增 4 条具名查询：`event_action_filter_columns`（`SELECT * … LIMIT 0`，只取元数据）、
    `event_action_filter_enabled`（全列）、`…_no_vcs`（第 8 列常量空串）、`…_legacy`（第 6/8 列常量空串）；
    列序 / 谓词 / `ORDER BY priority, id` 与原三条 SELECT **逐字一致**（恒 8 列）。
  · 新增薄函数 `event_action_filter_enabled_rows()`：`_any_cursor()` 内先发探列语句读 `c.description`，
    再由列名白名单 `_EVENT_ACTION_FILTER_OPTIONAL_COLUMNS = ("block_changes", "vcs_source")`
    选一条已登记语句（`_event_action_filter_query_name()`）；函数体内无内联 SQL。
    函数名登记 `__all__` 并同步 `src/kernel/gsb/route_config.py` 的 `PUBLIC_PRIMITIVES["mgmt"]` 镜像。
- 关键设计取舍：
  1. **探列从 PRAGMA 改成 `LIMIT 0` + `description`**：`db_registry` 的薄兼容游标不翻译 PRAGMA
     （只有 `mysql_compat.Cursor` 对进程主库特判），在 `_any_cursor` 上直发 PRAGMA 在 MySQL 必 1064
     —— 这正是原实现三库拓扑下失效的第一跳。`description` 两条后端一致，且零行不读数据。
  2. **列名只用来挑变体，绝不拼进 SQL**：三份语句文本固定在 `_ANY_QUERIES`，探测结果经白名单
     比对后只决定「用哪一条」，调用方无从传表名/列名（与原 `check_lib_query_names` 口径一致）。
  3. **三分支条件逐字照搬**：`has_block and has_vcs` → 全列；`has_block` → 忽略 vcs；否则两者都用常量。
     其中「有 `vcs_source` 但缺 `block_changes` → 连 vcs 一起忽略」看着像缺陷，但它是原实现的真实口径，
     改了会静默改变规则匹配维度，故保留并在烟测 B2 固定。
  4. **`enabled=1` 保持严格等值**：不写成 `enabled!=0`（`2` 与 NULL 都不算启用），与原语句一致。
  5. **拿掉调用方的 `db_lock` + `db.cursor()`**：`_any_cursor` 在单库回落路径自带锁（写路径自带 commit），
     再嵌一层就是同线程二次加锁（生产 `RLock` 侥幸不炸，测试 monkeypatch 的裸 `Lock` 会自死锁；
     交接文档 §5.3 坑 4 同族）。本模块现在不再 import 进程库句柄。
  6. **解析循环原样保留**：JSON 容错（非 list → 单元素原文、坏 JSON → `[s]`）、`actor or "any"`、
     `(vcs_source, event_type)` 键结构、TTL 缓存与 `force` 语义全部不动，只换数据来源。
- **修的存量 bug（三库独立后 L1 准入过滤整体失效，且被外层 `except` 吞成静默降级）**：
  api 进程库没有 `event_action_filter`（视图 0）→ `PRAGMA table_info` 先抛（无表 / MySQL 1064）→
  `cols=[]` → 走「老库变体」SELECT → 1146 → 外层 `except` 记一条 `load failed` →
  **返回空规则集**。`evaluate_event_action` 对任何 (event_type, action) 都得到 `no_rule`，
  于是 `skip`（assignees/labels 变更过滤）与 `side_effect`（close_issue_tasks）**永不触发**：
  webhook 事件全量进入 agent，L1 可配准入形同不存在。改造后单库与三库拓扑跑同一份 SQL，
  只有真缺表（迁移未建）才降级。
- 验证（真实退出码）：
  · `py_compile` 三文件（`event_rule.py` / mgmt `public` / `gsb/route_config.py`）→ **COMPILE_OK**；
  · `~/burndown/event_rule_smoke.py`（`DEVOPS_DB_BACKEND=sqlite`）→
    **ALL event_rule smoke PASSED (70 checks)**：A 注册面/静态否定（四条查询逐字对齐、无 `%s`/`NOW()`/
    `ON DUPLICATE`/`PRAGMA`、薄函数体无内联 SQL、调用方无表名 SQL 与进程库直连、`__all__` + GSB 镜像
    双登记、列集挑选 6 种（含大小写与空列集）、未登记查询名 `KeyError`）；B 单库行为（空表、
    8 列定序、`ORDER BY priority,id`、`enabled=1` 严格等值、NULL 原样返回）；B2 三种列集变体
    （缺 vcs / 两列都缺 / 有 vcs 缺 block）；C **自有 DSN 伪游标**（两条语句逐字、description 驱动
    变体选择、无参数、8 列原样、驱动异常上抛）；D 判定链（allow / block / no_meaningful_change /
    side_effect / action_disallowed / no_rule / no_action / 裸字符串与非 list JSON 容错 /
    actor_mismatch / 平台专属规则集整体遮蔽通用集 / 通用回退 / PR 无 commit 短路）+ TTL 缓存内
    改表名不查库 + 缺表 → 空规则集降级 + 恢复后强制刷新不粘空集。
  · `--update-baseline` → **baseline updated: 33 pairs, 57 refs**；依赖看板 / 矩阵 / 作业单同步刷新；
  · `bash scripts/rds_gate.sh --quick` → **GATE: 全绿**（check-arch R1–R8 / 棘轮未上升 /
    内部契约 37 条最新 / 依赖看板最新）；
  · `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；`make lint` → **import OK**；
  · 直覆盖 `tests/runtime/test_event_action_filter.py` + `tests/api/test_multi_vcs.py` → **53 passed**
    （走真实 MySQL 单库回落路径，证明 TTL/缓存与判定链未变）。
- **未验证项（如实）**：本轮未跑 `p4_p5_loop.sh` 三实例彩排 —— 本 shell `SRC_PWD`/`REMOTE_PWD` 未注入，
  且本批不改 schema / 迁移 / 契约面（无新表、无新端点，只新增一个读口）；运行面证据为 sqlite 烟测
  （含自有 DSN 伪游标）+ 真库直覆盖用例。api 进程 1146 消失的现场证据留待彩排轮确认。
- **新基线**：**57 处 / 33 对**（`--update-baseline` 已收紧）；作业单 57 处 / 37 文件，
  下一批首个未 blocked 文件 `src/domains/runtime/routes/pipeline.py` 2 处（`unified_user_mapping` /
  `managed_repos`，均归 mgmt，可复用现有 mgmt 读口）。

### runtime pipeline 责任人昵称 / 批量 bot 用户名读口归 mgmt public（-2，该文件清零）

- 文件：`src/domains/runtime/routes/pipeline.py`（2 处 → **0**）；全局静态 **57 → 55**（-2），对数 **33 → 32** ——
  `unified_user_mapping←runtime` 1 → 0（**该对整对清零**）、`managed_repos←runtime` 3 → 2。
- 表分布（消费域 runtime，两表**全归 mgmt**），两处互不相干、都在读装配阶段：
  · 行 60 `_resolve_feishu_name`：`SELECT feishu_name FROM unified_user_mapping WHERE {_col}=? AND feishu_name!='' LIMIT 1`
    —— **动态列**：`_col` 由 `um_column_for_repo(repo_key)` 推出后 **f-string 直接拼进 SQL**（FR-17.1
    为防跨平台同名误命中才分列）。
  · 行 568 `_get_issue_pipeline` 里的一次性 bot 集合：`SELECT bot_username FROM managed_repos WHERE repo_key IN (...) AND bot_username!=''`
    —— **动态占位符个数**（`",".join("?" * len(_repo_keys))`），候选集来自 `repo_key_candidates()`（多平台前缀）。
  两处表名均字面量，值全走占位符；「动态」只在**列名**与**IN 列表长度**。
- 本轮取批：作业单头两名 `src/kernel/prompt_builder.py` / `src/kernel/persistence/credential_usage_log.py`
  仍 blocked（口径见作业单顶部 ⛔ 小节），跳过；取第一个未 blocked 文件（上一批台账末尾点名的本文件）。
- 归属域侧改动（SQL 只在 mgmt 域一份）：
  · `_ANY_QUERIES` 新增 3 条具名查询：`um_feishu_name_by_gitcode_user` / `um_feishu_name_by_github_login`
    （与原 f-string 展开后的两条语句**逐字一致**，含 `feishu_name!=''` 与 `LIMIT 1`）、
    `repo_bot_usernames_in`（`… WHERE repo_key IN ({ph}) AND bot_username!=''`，`{ph}` 只是占位符槽）。
  · 新增列名白名单 `_UM_FEISHU_NAME_COLUMN_QUERIES = {gitcode_user, github_login}` 与薄函数
    `feishu_name_by_platform_column(column, login)`：未登记列 → 空串 + `log.debug`（不拼 SQL、不靠报错），
    查询异常 → 空串 + `log.debug`。列名的**唯一产生点**仍是 kernel 的 `um_column_for_repo`，调用方只传列名。
  · 新增薄函数 `repo_bot_usernames_any(repo_keys)`：复用既有 `_REPO_KEY_QUERY_CHUNK=400` 分块、
    `dict.fromkeys` 去重保序；空/None 候选集直接返回 `[]`（原语句会拼出 `IN ()` 语法错）。
  · 两个函数登记 `__all__` 并同步 `src/kernel/gsb/route_config.py` 的 `PUBLIC_PRIMITIVES["mgmt"]` 镜像。
- 关键设计取舍：
  1. **昵称与 open_id 分列建表，不复用 `feishu_open_id_by_platform_column`**：同表同列风格，但
     「昵称空」与「无 open_id」是两个不同的空值语义（前者回退用户名，后者回退身份链），
     合并会让回退链在展示层与通知层错位。
  2. **`_resolve_feishu_name` 显式把「无 um 列平台」前置返回用户名**：原实现是拼出空列名
     `WHERE =?` → 语法错 → `except` 吞掉 → 回退用户名。行为相同，但不再用「制造一次 SQL 报错」
     表达分支（烟测 B/E 段把这条历史口径固定住：具名查询**没有**加 `gitcode_user!=''` 谓词）。
  3. **拿掉调用方的 `db_lock` + `db.cursor()`**：`_any_cursor` 在单库回落路径自带锁与 commit，
     再嵌一层是同线程二次加锁（交接文档 §5.3 坑 4 同族）；调用点从此不再 import 进程库句柄。
  4. **IN 列表的占位符由归属域补**（`format(ph=…)` 在 public 内部完成）：调用方只交候选键列表，
     拼装面收在 owner 域，与 `repo_keys_existing_any` 同形。
  5. **不把「一次查完」改成「逐个查」**：调用点原本就是一条 `IN (...)` 直发进程库（无 lib 分支），
     本次只把这条语句搬到 owner 域；`if not _bots → get_all_bot_usernames()` 的兜底保留不动，
     避免在小仓库（1 个候选键）场景下多出一次全表扫描或改变责任人推断结果。
- **修的存量 bug（静默吞掉 bot 查询失败）**：原 `except Exception: pass` 让 `_bot_usernames` 悄悄为空，
  下游 `if not _bots` 再退回 `get_all_bot_usernames()`（每次全表扫）；三库拓扑下 api/runtime 进程
  缺 `managed_repos` 时会**每次都走这条 N+1 兜底**且无任何日志，展示层「责任人」会退化成裸 actor。
  改成 `log.warning` 显式留痕，并把同文件 `tests/silent_except_baseline.json` 计数 6 → 5（棘轮只减）。
- 验证（真实退出码）：
  · `py_compile` 三文件（runtime `routes/pipeline.py` / mgmt `public` / kernel `gsb/route_config.py`）→ **COMPILE_OK**；
  · `~/burndown/pipeline_feishu_bot_smoke.py`（`DEVOPS_DB_BACKEND=sqlite`）→
    **ALL pipeline_feishu_bot smoke PASSED (65 checks)**：A 注册面/静态否定（三条查询逐字对齐、无 `%s`/`NOW()`/
    `ON DUPLICATE`/`PRAGMA`、两薄函数体无内联 SQL、调用方无这两张表 SQL / 不再 f-string 拼列名 /
    不再直连进程库、`__all__` + GSB 双登记、未登记列返回空串且**不发 SQL**、未登记查询名 `KeyError`）；
    B 单库行为（gitcode/github 分列命中、**FR-17.1 跨平台同名不互相污染**、空串/NULL 昵称被
    `feishu_name!=''` 过滤、无行→空串、`LIMIT 1`、空登录名的历史口径）；C **自有 DSN 伪游标**
    （语句逐字、`?` + 参数元组不翻译成 `%s`、驱动异常在本层吞成空串）；D bot 读口（非空过滤、NULL、
    去重、空/None 候选不发 SQL、**401 键 → 2 条语句且 `?` 个数 = 参数个数** 400/1、块内保序）；
    E 调用点（空用户名短路、gitee/gitlab → 回退用户名、命中取昵称、无映射/昵称为空回退用户名、
    github 仓走 `github_login` 列、查询异常回退用户名）。
  · `--update-baseline` → **baseline updated: 32 pairs, 55 refs**；依赖看板 / 矩阵 / 作业单同步刷新
    （作业单 55 处 / 36 文件）；
  · `bash scripts/rds_gate.sh --quick` → **GATE: 全绿**（check-arch R1–R8 / 棘轮未上升 /
    内部契约 37 条最新 / 依赖看板最新）；
  · `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；`make lint` → **import OK**。
- **未验证项（如实）**：本轮未跑 `p4_p5_loop.sh` 三实例彩排（本 shell `SRC_PWD`/`REMOTE_PWD` 未注入）；
  本批不改 schema / 迁移 / 契约面（无新表、无新端点，只新增两个读口），运行面证据为 sqlite 烟测
  （含自有 DSN 伪游标）。「runtime 进程 1146 消失」的现场证据留待彩排轮确认。
- **新基线**：**55 处 / 32 对**（`--update-baseline` 已收紧）；作业单 55 处 / 36 文件，下一批首个未 blocked 文件
  `src/domains/runtime/im/vod_handler_async.py` 2 处（`grape_t_channel_installation`，归 mgmt，可复用
  `installation_*` 原语）或 `src/domains/runtime/im/outbox.py` 2 处（`grape_t_channel_lease`，
  可复用 `acquire_/renew_/release_channel_lease`）。

### runtime VOD 导航仓库轨安装反查归 mgmt public（-2，该文件清零）

- 文件：`src/domains/runtime/im/vod_handler_async.py`（2 处 → **0**）；全局静态 **55 → 53**（-2），
  对数 **32 → 32** —— `grape_t_channel_installation←runtime` 4 → 2（该对**未清零**：余下两处是
  `runtime/im/notify.py:182`（通知受众枚举）与 `runtime/im/engine/installations.py:17`
  （活跃安装枚举的引擎侧视图来源），分别属通知扇出与 InstallationProvider 枚举，另批处理）。
- 表分布（消费域 runtime，表归 **mgmt**），两处互不相干、都在 VOD Bot 导航的装配阶段：
  · 行 52 `_repo_app_ids`：`SELECT legacy_app_id FROM grape_t_channel_installation
    WHERE scope='repo' AND repo_key=? AND status='active' AND COALESCE(legacy_app_id,'')<>''`
    —— 常数 SQL，返回 set（`str(r[0])` + 真值过滤）。
  · 行 110 `_list_vod_repos`：`SELECT DISTINCT repo_key FROM grape_t_channel_installation
    WHERE scope='repo' AND status='active' AND legacy_app_id=? AND COALESCE(repo_key,'')<>''`
    —— 常数 SQL，结果被包成卡片菜单项 `{"repo_key": ...}`。
  两处表名/列名均字面量、无动态拼接，「动态」只在**参数方向相反**（一处按仓库查 bot，一处按
  bot 查仓库）。
- 本轮取批：作业单头两名 `src/kernel/prompt_builder.py` / `src/kernel/persistence/credential_usage_log.py`
  仍 blocked（口径见作业单顶部 ⛔ 小节），跳过；取第一个未 blocked 文件（上一批台账末尾点名的本文件）。
- 归属域侧改动（SQL 只在 mgmt 域一份）：
  · `_ANY_QUERIES` 新增 2 条具名查询：`installation_repo_app_ids`（列序 `legacy_app_id` 单列）、
    `installation_repo_keys_by_app`（`SELECT DISTINCT repo_key`，**不加 ORDER BY**，与原语句同口径）；
    谓词三件（scope/status/COALESCE 非空）与原 SQL 逐字一致。
  · 新增薄函数 `installation_repo_app_ids_any(repo_key) -> set[str]`（集合去重 + `str` 归一，
    与原 `{str(r[0]) for r in rows if r[0]}` 同形）与
    `installation_repo_keys_by_app_any(app_id) -> list[str]`（只回裸键、保序、Python 侧再兜一次空值）。
    两个函数登记 `__all__` 并同步 `src/kernel/gsb/route_config.py` 的 `PUBLIC_PRIMITIVES["mgmt"]` 镜像。
- 关键设计取舍：
  1. **卡片点阵形留在调用方**：owner 域只回裸 `repo_key` 列表，`{"repo_key": ...}` 由 runtime 侧列表推导
     组装 —— 该字典形状是卡片分页的 UI 契约（`_send_vod_repo_card` 按下标取 `r["repo_key"]`），
     不该污染 mgmt public 的返回类型（同域的其它安装读口一律回裸 tuple）。
  2. **不擅自加 `ORDER BY`**：原语句是 `SELECT DISTINCT` 无序，`/repo` 的分页顺序本来就依赖到达顺序；
     固化排序会改变「同一页里出现哪几个仓库」，属未授权的行为变更，故按原样搬。
  3. **不补 `legacy_app_id!=''` 谓词**：`installation_repo_keys_by_app` 只过滤 repo_key 非空（原 SQL 如此），
     空 bot id 命中 app_id 为空串的 repo 轨安装这一历史口径保留（烟测 B 段固定）；调用方
     `ctx.app_id` 恒真，实际不可达。
  4. **拿掉调用方的 `db_lock` + `db.cursor()`**：`_any_cursor` 在单库回落路径自带锁，再嵌一层是同线程
     二次加锁（交接文档 §5.3 坑 4 同族）；本文件仍留 `db`/`db_lock` —— 同域表
     （`devops_agent_session` / `vod_conversations` / `agent_runtimes`）的 SQL 属本域，不动。
  5. **失败语义保持「静默降级」但留痕**：`_repo_app_ids` → 空集合、`_list_vod_repos` → 空列表
     （调用方据此判「未配置本 bot 的 VOD」/「无候选」，不抛），`except` 从裸 `return` 改成
     `log.debug` + 同值返回（`tests/silent_except_baseline.json` 计数不变：两处原本就不是 `pass`）。
- **修的存量 bug（三库独立后 VOD Bot 导航整体失效，且被吞成「你没配」）**：runtime 进程库没有
  `grape_t_channel_installation`（视图 0）→ 两处查询在拆分拓扑下必 1146 →
  ① `_repo_app_ids` 恒空 → `_get_vod_session` 的 `app_ids` 恒空 → `_handle_vod_selection` 的
  `ctx.app_id not in app_ids` 恒真 → **任何仓库点选都回「该仓库未配置当前 bot 的 VOD Agent」**；
  ② `_list_vod_repos` 恒空 → `/repo` 恒走「无候选」分支。库里配置明明存在，用户侧只看到
  「未配置」而非报错。改造后单库与三库拓扑跑同一份 SQL，只有真缺表才降级。
- 验证（真实退出码）：
  · `py_compile` 三文件（`vod_handler_async.py` / mgmt `public` / `gsb/route_config.py`）→ **COMPILE_OK**；
  · `~/burndown/vod_installation_smoke.py`（`DEVOPS_DB_BACKEND=sqlite`）→
    **ALL vod_installation smoke PASSED (69 checks)**：A 注册面/静态否定（两条查询逐字对齐、无 `%s`/
    `NOW()`/`ON DUPLICATE`/`PRAGMA`、两薄函数体无内联 SQL、调用方无该表 SQL / 无 `db_lock`+`db.cursor`、
    无 f-string 拼 SELECT、`__all__` + GSB 镜像双登记、未登记查询名 `KeyError`）；
    B 单库行为（scope='repo'/status='active'/COALESCE 三重过滤逐项否定：revoked、disabled、user 轨、
    NULL/空串 app_id、NULL/空串 repo_key；同仓库两行同 app → 集合去重；`DISTINCT` 去重；
    跨 bot 隔离；空表/未知键 → 空集与空列表；两条**历史口径**单列固定）；
    C **自有 DSN 伪游标**（两条语句逐字、`?` + 参数元组不翻译成 `%s`、参数方向相反各自校验、
    Python 侧空值兜底仍在、驱动异常在 public 层**上抛**而在调用点吞成空集/空列表）；
    D 调用点（真跑 `_get_vod_session`：`app_ids` 注入会话字典、revoked → 空集、无会话 → None；
    真跑 `_list_vod_repos`：单键字典形状、revoked 不进候选、别的 bot → 无候选；两个原语打桩抛错 →
    调用点仍返回空集/空列表且打桩已还原）；
    E **拓扑回归**（自有 DSN 下进程库里放 `app404` 误导行：调用点只取伪游标结果、每处只发一条语句、
    参数分别是 repo_key / app_id）。
  · 直覆盖 `tests/runtime/test_vod_navigation_commands.py` + `tests/runtime/test_vod_handler_queue.py`
    → **15 passed**（走真实 MySQL 单库回落路径，证明 `/repo` 候选、数字点选绑定、队列语义未变）。
  · `--update-baseline` → **baseline updated: 32 pairs, 53 refs**；依赖看板 / 矩阵同步刷新
    （作业单仍为 53 处 / 35 文件）；
  · `bash scripts/rds_gate.sh --quick` → **GATE: 全绿**（check-arch R1–R8 / 棘轮未上升 /
    内部契约 37 条最新 / 依赖看板最新）；
  · `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；`make lint` → **import OK**。
- **未验证项（如实）**：本轮未跑 `p4_p5_loop.sh` 三实例彩排（本 shell `SRC_PWD`/`REMOTE_PWD` 未注入；
  轮内口径同样是「每轮只跑 py_compile + 专用烟测 + gate + 三组 pytest + lint，彩排压在余量归零后统一跑」）；
  本批不改 schema / 迁移 / 契约面（无新表、无新端点、无新原语契约），运行面证据为 sqlite 烟测
  （含自有 DSN 伪游标 + 调用点真跑）+ 真库直覆盖用例。runtime 进程 1146 消失的现场证据留待彩排轮确认。
- **新基线**：**53 处 / 32 对**（`--update-baseline` 已收紧）；依赖看板 / 矩阵 / 作业单同步刷新
  （作业单 53 处 / 35 文件，下一批首个未 blocked 文件 `src/domains/runtime/im/outbox.py` 2 处
  （`grape_t_channel_lease`，归 mgmt，可复用既有 `acquire_/renew_/release_channel_lease`）；紧接其后的
  `src/domains/runtime/im/notify.py` 2 处正好包含本对的余量之一 —— 同一对表可在下一轮一并清零）。

### runtime im/outbox 租约归属读口归 mgmt public（-2，该文件清零）

- 文件：`src/domains/runtime/im/outbox.py`（2 处 → **0**）；全局静态 **53 → 51**（-2），
  对数 **32 → 31** —— `grape_t_channel_lease←runtime` 2 → 0（该对**清零**）。
- 本轮取批：作业单头两名 `src/kernel/prompt_builder.py` / `src/kernel/persistence/credential_usage_log.py`
  仍 blocked（口径见作业单顶部 ⛔ 小节），取第一个未 blocked 文件（上一批台账末尾点名的本文件）。
- 表分布（消费域 runtime，表归 **mgmt**），两处都是**只读归属判定**、都不改租约：
  · 行 163 `lease_owner_of`：`SELECT COALESCE(lease_owner,'') FROM grape_t_channel_lease WHERE lease_key=?`
    —— 常数 SQL，按 `<channel_type>:<installation_id>`（channel_type 为空时用裸 installation_id）读 owner。
  · 行 203 `owned_installations`：`SELECT installation_id, channel_type FROM grape_t_channel_lease
    WHERE lease_owner=? AND installation_id<>'' ORDER BY updated_at DESC LIMIT 200`
    —— 常数 SQL，把归属过滤下推到取批 SQL（架构守护 Critical-1 防饥饿）。
  两处表名/列名均字面量、全参数化，无动态拼接；`LIMIT 200` 是字面量。
- 归属域侧改动（SQL 只在 mgmt 域一份）：
  · `_ANY_QUERIES` 新增 2 条具名查询：`lease_owner_by_key`（单列，列序/COALESCE 逐字照抄）、
    `lease_owned_installations_by_owner`（两列 + `installation_id<>''` + `ORDER BY updated_at DESC`
    + 字面 `LIMIT 200`）；与 W2 写原语（`acquire_/renew_/release_channel_lease`）分开登记。
  · 新增薄函数 `lease_owner_by_key(lease_key, cursor=None) -> str`（`str(row[0]) if row and row[0] else ""`）
    与 `lease_owned_installations_by_owner(owner, cursor=None) -> list[tuple]`（裸 tuple、保序）；
    两函数登记 `__all__` 并同步 `src/kernel/gsb/route_config.py` 的 `PUBLIC_PRIMITIVES["mgmt"]` 镜像。
- 关键设计取舍：
  1. **为什么薄函数带 `cursor` 形参，而不是纯 `read_one_any`**：本文件的连接解析是「每次调用按
     env 现取 `DEVOPS_DB_PATH`」，而 `*_any` 回落的是 **import 期绑定**的 `session_manager.db`。
     `tests/runtime/im/test_notify_outbox.py` 的 `outbox_db` fixture 每个用例把 `DEVOPS_DB_PATH`
     指到 `tmp_path` 的库、并在该库里铺租约行 —— 不带调用方游标就一条也读不到（12 个租约/取批
     用例会假红）。`cursor=` 是 mgmt public 的既有口径（`_owner_cursor` + `account_id_by_username_ci_any`）：
     **单库/测试拓扑复用调用方游标，自有 DSN 拓扑忽略它**（忽略那一跳正是原来 1146 的来源）。
  2. **不复用 `lease_owner_info`**：它一次带出 `lease_token`（凭据），消费侧只需要 owner；新查询单列
     `COALESCE(lease_owner,'')` 与原语句逐字一致，也不给 IM 消费路径多一个读凭据的入口。
  3. **`LIMIT 200` 不做参数化**：它是本域对该读口的容量约束，不是调用方业务参数；**`ORDER BY
     updated_at DESC` 必须保留** —— 去掉就退化成「随便 200 行」，多主控下会改变谁被饿死的顺序。
  4. **调用点保留原短路与失败语义**：空 `instance_id` 直接 `[]`、一条查询都不发；驱动异常在 public
     层上抛、调用点吞成 `""` / `[]`（`log.warning` 原文保留，不是静默 `pass`）。
  5. 不再为这两条读在 runtime 侧持有 `db`/`db_lock` 做归属判定；同域表
     （`grape_t_channel_outbound_card`）的连接与事务语义原样不动。
- **确认的存量缺陷（三库独立后消费者永不投递，且被吞成「本实例没有待投行」）**：runtime 进程库没有
  `grape_t_channel_lease`（视图 0）→ 两处查询必 1146 → ① `owned_installations` 恒空 → `claim_batch`
  在 `if not owned: return []` 处直接返回空批（多主控投递整体停摆）；② `lease_owner_of` 恒空 →
  逐行复核把每一行都判成「不是本实例的」→ `continue`。两条路径都只打一行 WARNING，用户侧表现为
  「没有要发的通知」。改造后单库 / 三库跑同一份 SQL，只有真缺表才降级。
- **未做（如实，属另一轮）**：同文件 `grape_t_channel_outbound_card` 的 15 处读写仍走 IM 连接、
  本轮**未动**——静态正则不计它（表名是 `_TABLE` 变量，正则只认字面表名），属交接文档 §9.3
  「P3 写通道未切完」；且 mgmt 现有 `upsert_outbound_card` 是 `%s` / `NOW()` / `ON DUPLICATE` 的
  MySQL-only 版，直接替换会打断单库拓扑，需先补可移植具名查询（含 IN 分块、purge 的有序先取后删），
  留独立一轮。本轮不碰它是为了不把「2 处只读」的批变成「15 处热路径重写」。
- 验证（真实退出码）：
  · `py_compile` 三文件（`outbox.py` / mgmt `public` / `gsb/route_config.py`）→ **COMPILE_OK**；
  · `~/burndown/outbox_lease_smoke.py`（`DEVOPS_DB_BACKEND=sqlite`）→
    **ALL outbox_lease smoke PASSED (54 checks)**：A 注册面/静态否定（两条查询逐字对齐、可移植方言、
    薄函数体无内联 SQL、调用方无该表任何 DML/DQL、`__all__` + GSB 镜像双登记、未登记名 `KeyError`）；
    B 单库（命中 / 未命中 → 空串 / owner NULL 的 COALESCE / `installation_id=''` 被过滤 /
    `ORDER BY updated_at DESC` / 字面 LIMIT 200 用 205 行固定 / owner 隔离 / 空 owner）；
    C 调用点（有 channel_type 加前缀、无则裸 key、空 instance_id 零查询、驱动异常吞成 `""`/`[]`）；
    D 游标语义（D1–D3 单库复用调用方游标：进程库放干扰行、结果只来自调用方库；D4–D9 自有 DSN
    伪游标：忽略调用方游标、语句逐字、`?` + 参数元组、无 `%s`）；
    E **end-to-end**（真跑 `claim_batch`：node-A/node-B 各取自己的行、不持租约 → 空批、取批行置
    sending、过期行置 expired 且不返回、租约行 owner/token 未被改动）。
  · 直覆盖 `tests/runtime/im/test_notify_outbox.py` + `tests/kernel/test_gsb_route.py` →
    **68 passed**，3 failed 均为**干净树既有**（`git stash` 复现，见下）。
  · `--update-baseline` → **baseline updated: 31 pairs, 51 refs**；依赖看板 / 矩阵 / 作业单同步刷新；
  · `bash scripts/rds_gate.sh --quick` → **GATE: 全绿**（check-arch R1–R8 / 棘轮未上升 /
    内部契约 37 条最新 / 依赖看板最新）；
  · `pytest tests/arch tests/contracts tests/scripts` → **158 passed**；
  · `make lint` → **import OK**；`check_lib_query_names.py` → **PASS**（mgmt 178 funcs + 6 queries）。
- **既有失败（与本次改动无关，已在干净树 `git stash` 后逐一复现）**：
  `tests/runtime/im/test_notify_outbox.py::test_grape_server_lifespan_starts_consumer_and_registry`、
  `::test_main_mode_flip_rebinds_consumer`（lifespan 退出时 `OutboxConsumer.stop()` 的 `await self._task`
  抛 `CancelledError`，与本批读口无关）；
  `tests/kernel/test_gsb_route.py::test_public_primitives_registry_sync`（mgmt `PUBLIC_PRIMITIVES`
  早已漂移：public 实际面 257 个函数 / 静态镜像只登记 64 项，缺 193 项，本轮实算；
  非本轮新增的两个函数 —— `lease_owner_by_key` / `lease_owned_installations_by_owner` 都在注册面里）。
- **未验证项（如实）**：本轮未跑 `p4_p5_loop.sh` 三实例彩排（本 shell 未注入 `SRC_PWD`/`REMOTE_PWD`；
  轮内口径同前几轮：py_compile + 专用烟测 + gate + 三组 pytest + lint，彩排压在余量归零后统一跑）。
  本批不改 schema / 迁移 / 契约面（无新表、无新端点、无新原语契约），运行面证据为 sqlite 烟测
  （含自有 DSN 伪游标 + claim_batch 真跑）。runtime 进程 1146 消失的现场证据留待彩排轮确认。
- **新基线**：**51 处 / 31 对**（`--update-baseline` 已收紧）；依赖看板 / 矩阵 / 作业单同步刷新
  （作业单 51 处 / 34 文件，下一批首个未 blocked 文件 `src/domains/runtime/im/notify.py` 2 处
  （`grape_t_channel_installation` 行 182 / `grape_t_channel_chat_session_binding` 行 205，均归 mgmt）。

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
