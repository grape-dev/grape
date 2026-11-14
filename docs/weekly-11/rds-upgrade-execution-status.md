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
| P0-1 删除 60 个零引用视图 | ⏸ 待人工放行 | dry-run 已生成 60 条 DROP：`docs/weekly-11/backup/p0-prune-dryrun.txt`；`DROP` 操作曾被拒绝，按"不破坏线上库"口径停在 dry-run |
| P0-2 外域引用统计 + 棘轮守卫 | ✅ 完成 | `scripts/foreign_table_refs.py --check` 通过；`tests/arch/test_foreign_table_refs.py` 3 项通过；基线 138 对 / 1306 处 |
| P0-3 `sync_domain_views.py` 只减不增 | ✅ 完成 | 视图白名单 122 条（`docs/weekly-11/view-allowlist.txt`）；不在白名单的建视图请求 `DENY` 并 exit 1；`--prune` 支持删白名单外视图 |
| P0-3 第二条建视图路径同样受控 | ✅ 完成 | `scripts/domain_view_freshness.py --repair` 对白名单外视图 `DENY`（生产库名族生效，dev 库族不误拦） |
| P0-4 依赖矩阵看板 | ✅ 完成 | `docs/weekly-11/dependency-matrix.md`（70 表 / 138 对 / 1306 处）；`docs/weekly-11/dependency-board.html`；`--check` 防过期 |
| P1 R8 守卫（kernel 不得写域表） | ✅ 完成 | `scripts/check-arch.py` R8 硬规则 + 棘轮白名单（77 处存量，10 文件）；`check-arch` RESULT: PASS |
| P1 30 张表归属重定 / 模块归位 | ⏳ 未完成 | 见 §2 缺口；当前 kernel 侧 38 个文件×表对、api 侧 32 文件×124 对仍需迁移 |
| P2 N1–N10 服务接口 | ✅ 完成 | runtime 7 个（skills 列表/详情、engine-capabilities、runtime-credentials、meta、tasks/batch、stats/tasks）+ mgmt 4 个（repo bundle、account、knowledge、user-mapping）；mgmt 只读白名单 11 条 |
| P2 N13 补偿应用端点 | ✅ 完成 | `POST /internal/xwrite/apply`（runtime 9 个 action、mgmt 5 个 action）；幂等键登记表 `grape_t_xwrite_applied`（`cross_write.claim_applied`） |
| P2 内部契约快照 | ✅ 完成 | `docs/weekly-11/internal-contract.json`（36 条：runtime 17 / mgmt 12 / insight 7），全部 `<domain>_<resource>_<action>` |
| P3 W1/W2/W3 写分流 | 🟡 部分 | 31 张写表已有 7 张具备 owner public 写函数（22%）；W2 缺 14、W3 缺 6，清单见 `docs/weekly-11/write-channel-coverage.md` |
| P3.5 剩余跨域读接口化 | ⏳ 未完成 | 10 处引用尚未切换调用点 |
| P4 三实例与搬迁 | ⏳ 阻塞 | 缺云账号/实例创建权限；工具已就绪：`scripts/rds_cutover_check.py`（baseline 实测 102 表 / 382,976 行） |
| P5 权限收敛与清视图 | ⏳ 阻塞（依赖 P4） | 验收器已就绪：`scripts/verify_domain_isolation.py`（当前如实报红：视图 44/57/81、跨域引用 138 对） |

---

## 2. 量化基线（本次实测）

| 指标 | 数值 | 采集命令 |
| --- | --- | --- |
| 三库基表 | 102（唯一表名 101；`grape_t_merge_gate_log` 在 insight/runtime 各有一份，按 disposition 判给 insight） | `scripts/rds_cutover_check.py --mode baseline` |
| 三库跨域视图 | 182（mgmt 44 / runtime 57 / insight 81） | `information_schema.views` |
| 视图覆盖的非本域表 | 91 张；其中 30 张（60 视图）代码零跨域引用 → P0 快赢删除对象 | `docs/weekly-11/backup/p0-prune-dryrun.txt` |
| 代码外域表引用 | 70 张表 / 1306 处跨域（`表×消费域` 138 对）；含归属域共 2689 处 | `scripts/foreign_table_refs.py --owner docs/weekly-11/table-owner.tsv` |
| 域×域分布 | api→mgmt 252 / api→runtime 388；mgmt→runtime 193；kernel→mgmt 88、→runtime 89 | `docs/weekly-11/dependency-matrix.md` |
| kernel 写域表（R8 存量） | 77 处 / 10 文件（R8 已锁死不得新增） | `scripts/check-arch.py` |
| 内部端点 | 36 条（P2 新增 12 条 + 既有补齐 operationId 16 条） | `docs/weekly-11/internal-contract.json` |
| 跨域写表 | 31 张（W1 4 / W2 16 / W3 9 / X 1 + 1 张补录） | `docs/weekly-11/cross-domain-write-channel.csv` |

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
| `scripts/verify_domain_isolation.py` | P5 验收（A1/A2/A3/A6） |
| `docs/weekly-11/table-owner.tsv` | P0 冻结归属清单（101 张唯一基表） |
| `docs/weekly-11/foreign-refs-allowlist.csv` | 棘轮基线 138 对 / 1306 处 |

---

## 4. 未完成项与原因

1. **P0 删 60 视图**：`DROP VIEW` 被人工拒绝 → 停在 dry-run。放行命令：
   ```bash
   DEVOPS_DB_HOST=<host> DEVOPS_DB_USER=<user> DEVOPS_DB_PASSWORD=<pwd> \
     python3 scripts/sync_domain_views.py --prune      # 先不加 --dry-run 即为执行
   ```
   备份：`docs/weekly-11/backup/cross-domain-views-20261114.sql`。
2. **P1 30 张表归属重定**：需要按 `cross-domain-disposition.csv` 逐表把实现搬进 owner 域，
   并把 api/kernel 调用点改到 `/internal/*` 或 public 函数。当前仅完成守卫（R8 + 棘轮），
   迁移量：kernel 38 个文件×表对、api 124 个文件×表对。
3. **P3 W2/W3 缺口**：14 张 W2 表缺 owner public 写函数、6 张 W3 表缺 outbox action
   （见 `write-channel-coverage.md`）。缺函数前**不得**切断同库写路径。
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

## 7. 提交记录

| 提交 | 内容 | 状态 |
| --- | --- | --- |
| `6a275f0da` | feat(db): add domain split guards and cutover tools | 已推送 `grape-dev/arch-weekly-11-ljt` |
| `f75acecf5` | feat(api): add P2 internal service endpoints for RDS split | 已推送 `grape-dev/arch-weekly-11-ljt` |

验证（提交后复跑）：
- `pytest tests/contracts tests/arch tests/scripts tests/api/test_mgmt_no_internal_routes.py tests/kernel/test_gsb_route.py` → **189 passed**
- `scripts/check-arch.py` → RESULT: PASS（R1–R8）
- `scripts/foreign_table_refs.py --check` → OK（棘轮未上升，138 对 / 1306 处）
- `tests/kernel` 其余 44 项失败为分支既有（已在 HEAD 干净树 `git worktree` 复现，与本次改动无关）
