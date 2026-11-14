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
