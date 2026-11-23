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

## 三库模式冷启动（全新实例）

四进程 + 三库（`grape_mgmt` / `grape_runtime` / `grape_insight`）在**三个空库**上不能直接起：
`GRAPE_DOMAIN_LIB=1` 只跑登记的增量迁移，基表来自历史单库时代的 legacy 轨道；而 legacy 建表会被
表归属守卫按域跳过（`meta` 属 runtime，mgmt 进程不建），随后 `_seed_from_json` 写 `meta` 直接
`(1146) Table '...meta' doesn't exist` 启动失败（2026-11-20 实测）。

冷启动按这个顺序：

```bash
# 1) 三个空库（utf8mb4）
#    CREATE DATABASE grape_mgmt|grape_runtime|grape_insight CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

# 2) env 文件（建议 chmod 600）：GRAPE_DOMAIN_LIB=1 + 三条 DSN（口令 URL 编码，parse_dsn 会 unquote）
#    DEVOPS_DB_PASSWORD 写原文（mysql_compat 不解码）

# 3) 每个库按「单库模式 + 归属守卫关闭」一次建齐（幂等，可重复跑）
python scripts/bootstrap_3db.py --env-file ~/.grape-3db.env        # 加 --check 只体检

# 4) 起四进程：mgmt/event/worker/insight，端口 38081-38084，
#    GRAPE_MIGRATION_OWNER 分别 mgmt / runtime / 空 / insight
```

`scripts/bootstrap_3db.py` 会先体检版本台账形状再动手。**被旧的 `scripts/migrate_schemas.py`
改过的库不能用**：那是 schema-split 工具把 `grape_t_schema_migrations` 建成了
`(table_name, target_schema, migrated_at)`，与应用要的 `(version, name, ...)` 形状不同，
`CREATE TABLE IF NOT EXISTS` 会静默跳过 → 版本表读不了 → 退回 legacy → 同样死在 `meta`。
脚本对这类库直接 FAIL 并给处置指引（换新库，或人工改名后重跑并确认迁移重跑的影响）。

## 四进程模式下接入 worker（执行器）

三库/四进程拓扑里 `/api/runtime/*` 挂在 **worker 进程**（默认 38083），不在 mgmt：
`RUNTIME_SERVER_URL` 指错进程时，注册会拿到 `405 Method Not Allowed`、认领也 405，日志像
「平台不处理任务」。正确取值：

```
RUNTIME_SERVER_URL=http://<host>:38083     # worker 进程：register / claim / heartbeat / install
GRAPE_BASE_URL=http://<host>:38081         # mgmt 进程：业务 API（issue / artifacts / settings）
```

凭据作用域决定能领到哪些任务（`service.claim_by_tag` 的 scope 隔离）：

- `scope=global`（机器级，凭据名=主机名）：可领任意 `queued` 任务 —— 一台机器给整个实例当执行器时用它；
- `scope=user`（个人）：只领 `agent_tasks.user_id` 等于该用户的任务；
- `scope=repo`：只领 `owner_id` 对应仓库的任务。

生成方式（明文只回一次）：`POST /api/settings/runtime-credentials {"name": "...", "scope": "global"}`。

> 已知缺口：网页手工建单落下来的 `agent_tasks.user_id` 目前是 `'0'`（未归属），
> 因此 `scope=user` 的引擎**永远领不到**这类任务，表现为任务一直 `queued`。机器级 worker
> 请用 `scope=global`；归属修复另行跟进（需先定建单侧 creator 写入口径）。

## 容器 overlayfs 与 SQLite（Issue #3588）

overlayfs copy-up 可能以「删旧建新」替换 `data/sessions.db-wal` / `-shm`，进程持有的
连接随即失效（`disk I/O error`、接口持续 500）。运行期自愈已内置（失效检测 + 连接重建，
日志 `event=db_stale_detected` / `event=db_reconnect_ok`，`/healthz` 报 `db_status` /
`wal_valid`）。部署建议：

1. 数据目录落持久卷（compose 已挂 `grape-data:/app/data`；裸容器用 `DEVOPS_DB_PATH`
   指到卷内路径），不要让 WAL/SHM 落在容器可写层；
2. 反复触发时可用 `DEVOPS_DB_JOURNAL_MODE=truncate|delete` 规避（默认 `wal` 不变）。

## 滚动重启 SOP（Issue #3969）

**目标**：单台 worker 重启/升级不再引发 `/api/runtime/*` 全量 502。基线事故为
91s 内 1,178 次 502（详见 [`docs/deployment/worker-restart-slo.md`](../docs/deployment/worker-restart-slo.md)）。

前置：
- 网关 worker upstream 已配置多上游（`/etc/nginx/conf.d/grape-worker-peers.conf`
  注入对等主控 worker 端点）+ `proxy_next_upstream`；`nginx -t` 通过。
- worker systemd 单元已启用 `ExecStop` 摘流脚本与 `ExecStartPost` readiness 门禁；
  `/tmp/grape-worker-draining` 标记存在时 `/readyz` 返回 503。

步骤（**逐台**执行，禁止多台并行）：
1. `systemctl restart grape-worker-server`（ExecStop 会先摘流 → 等待 ≥5s 排空 →
   SIGTERM；启动后 `ExecStartPost` 轮询 `/readyz`）。
2. 观察该台 5 分钟窗口内 `/api/runtime/` 的 502 计数（Loki / `nginx_upstream_502_total`）。
3. **等 502 归零且无新 502 再处理下一台**；若 5min 502 率 >1%，触发
   `GrapeRuntime502RateHigh` 告警，暂停滚动并排查上游可达性/readiness。
4. 全部完成后复核 `/readyz`、runtime 心跳与在线引擎数回到基线。

注：滚动重启只用 `systemctl restart`，不要用 `scripts/restart-four-process.sh`
强杀（会绕过摘流，见脚本自身提示）。

### 拓扑与 worker 端口对应关系

| 网关配置 | 拓扑 | worker 上游 | 说明 |
|---|---|---|---|
| `nginx-grape.conf` | 三进程（A/B/C 同机） | `grape_worker_server` → `127.0.0.1:38082` + peers | 旧三进程反代，已同步多上游/切换 |
| `nginx-grape-four-process.conf` | 四进程（同机 systemd） | `grape_worker_server` → `127.0.0.1:38082` + peers | 生产四进程主配置 |
| `nginx-grape-cluster.conf` | 四进程（集群端口 38081–38084） | `grape_worker` → `127.0.0.1:38083` + cluster peers | 集群端口整体上移一位 |
| `nginx-grape-compose.conf` | Docker Compose | `grape_worker_server` → `grape-worker-server:38082` + peers | 服务名寻址，容器网络 |

事故现场 `127.0.0.1:8083` 是 master-002 上旧版单进程 worker 端口，与仓库四份配置的
38080/38082/38083 均不匹配；上线前必须确认该机实际加载的 `conf.d/*.conf` 与 worker
监听端口一致（`ss -ltnp | grep -E '38082|8083'` + `nginx -T | grep proxy_pass`）。
四份配置现均含 `include /etc/nginx/conf.d/grape-worker-peers.conf*`（缺省仅本机）。

### 502 告警验证（可执行步骤）

```bash
# 1) 语法与抓取自检
nginx -t
curl -s http://127.0.0.1:12345/metrics | grep -E 'nginx_upstream_(502|requests)_total'
# 无输出时：确认 alloy-config.alloy 已加载 prometheus.scrape "alloy_self"

# 2) 故障注入：临时把对端 worker 端口改为拒绝连接，压 /api/runtime/heartbeat
#    （会在 nginx access.log 产生 502 行）
for i in $(seq 1 20); do curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://127.0.0.1/api/runtime/heartbeat -H 'Content-Type: application/json' -d '{}'; done

# 3) 规则验证
promtool check rules deploy/alerts.yml
# Prometheus 侧确认指标出现，且 2m 内 502 增量 >50 时 GrapeRuntime502Burst 触发；
# 5m 率 >1% 时 GrapeRuntime502RateHigh 触发。
```

## 网关发布窗口优雅摘流（Issue #3847）

发布/热加载窗口内曾出现网关对健康 upstream 的 502 风暴（10:12–10:14 共 1,173 次、
5xx 率 1.63%，基线 0.077%）。修复由三部分组成，部署时必须一起下发：

1. **公共 nginx 片段**（放到 `/etc/nginx/conf.d/`，与网关配置同目录）：
   - `nginx-grape-upstream.inc`：每个 `server { }` 内 `include`，提供
     `proxy_next_upstream error timeout http_502 http_503` + `tries 2`、
     `proxy_http_version 1.1`、`Connection ""` 与客户端 `keepalive_timeout 65s`。
   - `nginx-grape-upstream-keepalive.inc`：每个 `upstream { }` 内 `include`，提供
     `keepalive` / `keepalive_timeout 90s`（必须大于客户端侧 65s）。
   - 四份模板（单机 / `-cluster` / `-four-process` / `-compose`）已接入上述 include。
2. **nginx 优雅退出**：把 `nginx-worker-shutdown.inc` 手动 include 到主
   `/etc/nginx/nginx.conf` 的 **main 上下文**（`worker_processes` 之后、`events` 之前），
   设置 `worker_shutdown_timeout 30s`；`deploy/systemd/*.service` 已补
   `KillSignal=SIGTERM` / `KillMode=mixed` / `TimeoutStopSec=45` 与之对齐。
3. **发布流程与门禁**：`scripts/deploy-drain.sh` 按「摘流 → 排空 → 退出」执行，
   重启后采样 `/api/health`、`/api/runtime/heartbeat`、`/static/openapi.json`，
   502 率 > 0.5%（`GATEWAY_502_THRESHOLD=0.005`）自动回滚。发布前可 `--dry-run` 预演：

   ```bash
   GATEWAY_DRAIN_CMD='<摘流钩子>' GATEWAY_STOP_CMD='systemctl restart grape-server' \
     bash scripts/deploy-drain.sh --dry-run --unit grape-server
   ```

   告警侧 `deploy/alerts.yml` 已把 5xx 由单阈值 10% 收紧为分级 0.5%/2%，并新增
   网关 502 速率规则，阈值与脚本保持一致。

> 注意：生产 502 记录落在 `127.0.0.1:8081/8083`，而仓库模板为 `38081/38083`。
> 下发前请先 `nginx -T` 确认生产实际加载的配置与端口映射；若生产另有旁路配置，
> 需同时完成「生产配置与仓库模板对齐」，避免改到未被加载的文件。
