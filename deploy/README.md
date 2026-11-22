# 部署（四进程 / 分域库）

## 顺序（不可颠倒）：迁移 → 视图修复 → 复核 → 重启

分域部署下，非归属库通过跨库视图读 owner 基表；视图列是**建视图时的快照**。
迁移在进程 import 期执行（`init_database()`），所以：

```bash
# 1) 迁移 + 视图修复 + 复核（fail-closed：非 0 不得重启）
DEVOPS_DB_HOST=... DEVOPS_DB_USER=... DEVOPS_DB_PASSWORD=... \
DEVOPS_DB_NAME=grape_mgmt_db \
  bash scripts/deploy-after-migrate.sh

# 2) 上一步 exit 0 才重启
systemctl restart grape-systemd@mgmt.service grape-systemd@event.service \
                 grape-systemd@worker.service grape-systemd@insight.service
```

`deploy-after-migrate.sh` 依次执行：

1. `init_database()`（幂等迁移）
2. `scripts/domain_view_freshness.py --repair`
   —— 缺列 / 视图多余列 / 缺视图（仅清单内表）/ 类型漂移；孤儿视图只报告
3. 复核（仍有漂移则非 0）

失败语义：

| 退出码 | 含义 | 处置 |
|---|---|---|
| 3 | 库不可用 / 库族不确定 / 缺 `CREATE VIEW` 权限 | 修环境，**不要重启** |
| 2 | 存在未登记的同名多份基表 | 先定 owner（登记或清理），**不要重启** |
| 1 | 仍有视图漂移 / 孤儿视图 | 按输出逐条处理，**不要重启** |

## 发布包解包 → 预检（不可颠倒，Issue #3756）

后端包解包到部署目录（如 `/opt/grape`）后，**先跑部署预检再起服务**：

```bash
tar xzf grape-backend-<ts>.tar.gz -C /opt/grape
python3 /opt/grape/scripts/deploy_preflight.py \
  --db-path /opt/grape/data/sessions.db --role mgmt \
  --contracts-dir /opt/grape/contracts   # 默认也取 <repo>/contracts
```

预检的 `contracts_registry` 项要求 `contracts/` 整目录随包落盘：
`deprecated_endpoints.json`、`deprecated_exemptions.json`、
`openapi_baseline.json`、`required_operations.json` 与 `released/*.json`。
缺任一件时预检以退出码 1 拦在启动前（运行期 `GET /openapi.json` 会 500 或降级），
**不要跳过预检直接重启**。若契约目录不在默认位置，必须显式传 `--contracts-dir`。

## 清单文件

| 文件 | 作用 |
|---|---|
| `scripts/view_owner_known_duplicates.txt` | 已知"多份基表"登记（owner 不确定 → 不判定、不修复） |
| `scripts/view_create_missing_allowlist.txt` | 允许自动补建缺失视图的表（不在清单内只报告） |
| `scripts/view_narrowed_allowlist.txt` | 有意只暴露部分列的表（不自动补列） |

## 权限前提

修复需在**每个非归属库**具备 `CREATE VIEW`，owner 库具备 `SELECT`；
孤儿视图检测需要 `SHOW VIEW`（否则视图定义不可见，脚本会打印 `[UNKNOWN]` 并在 `--strict` 下失败）。
域账号默认只有 `SELECT/INSERT/UPDATE/DELETE`；部署收尾请用具备 `CREATE VIEW` 的运维账号，
否则脚本以 exit 3 明确失败。

## 缺视图（1146）策略

缺失视图默认**只报告、不判红**（避免 owner-only 新表把部署永久卡住），也不会自动补建；
需要跨域读的表登记到 `scripts/view_create_missing_allowlist.txt` 后由 `--repair` 补建。
需要缺视图即失败时加 `--fail-on-missing-view`。

## 白名单（多份基表）表的视图也会校验

登记的 11 张多份基表仍会校验其非归属库视图：缺少所有副本都有的列 → 失败（owner 需先定）。
各副本之间列集合不一致 → 告警（不阻断），需人工定 owner 后收口。

## 关于 DEFINER

`CREATE OR REPLACE VIEW` 会把视图 DEFINER 重置为执行账号；若线上视图依赖特定 definer
授权，用 `--definer user@host` 显式指定。

## 契约版本与「测试契约通道」（Issue #2784）

- **测试契约通道**：`grape.example.com` 承载**预发布契约**（`info.version` 带
  `a1`/`b1`/`rc1` 后缀，例如 `2.3.0a1`），仅测试环境可查；现网契约是去掉后缀的正式版。
  预发布契约不得直接对现网用户发布，CLI 升级提醒也不会以预发布版本作为升级目标。
- **版本号来源**：所有进程（mgmt / event / worker / insight，以及 webhook-only 入口）
  的 `/openapi.json` 与 `/health` 里的 `version` 都取自
  `src/api/contract_versioning.py::CONTRACT_VERSION`；**不要**在部署脚本或环境变量里
  另设一份契约版本号——四进程之间版本漂移时，CLI 会按不同号的契约来回刷新。
- **变更通知**：契约根 `x-changelog` 给出本次相对现网快照的新增/删除/弃用端点
  （`added` / `removed` / `deprecated`）。基线 `help/api/openapi-baseline.json` 是
  发布产物，随发布构建刷新；部署副本若不带 `help/`，`x-changelog.baseline_available`
  为 `false`、清单为空，属预期降级，不是故障。
- 规则全文（禁跳版、预发布后缀、单一事实源、`x-changelog` 通知）见
  [`CONTRIBUTING.md` §9.1「契约版本号策略」](../CONTRIBUTING.md)。
