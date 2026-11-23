# Grafana 看板（可恢复）

LGTM 容器**没有挂卷**，容器一重建，Grafana 里的数据源和看板就全没了。
所以看板 JSON 必须留在仓库里 —— 这里就是它的家。

## 恢复（容器重建后一条命令）

```bash
# 默认 127.0.0.1:3000, admin:admin
bash deploy/grafana/import-dashboards.sh

# 指向兄弟机
GRAFANA_URL=http://<sibling-host>:3000 bash deploy/grafana/import-dashboards.sh

# 只查现状，不写
GRAFANA_URL=http://<sibling-host>:3000 bash deploy/grafana/import-dashboards.sh --check
```

脚本做三件事，都是幂等的：

1. 探 `GRAFANA_URL/api/health`（连不上直接 fail，不假装成功）
2. 补数据源：`prometheus` / `loki` / `tempo`（按固定 uid，已存在的跳过 —— 不覆盖你手改的连接参数）
3. 用 `overwrite=true` 导入 `dashboards/*.json`（以仓库为准）

脚本只自动补**无认证**的数据源（prometheus / loki / tempo）。
`grape-mysql` 带库口令，故意不写进脚本 —— 重建后手动建一次（或从 `/etc/grape/` 下取）。
看板里如果用了它，重建时会提示数据源不存在，那时再补。

## 现有看板

| 文件 | uid | 说明 |
|---|---|---|
| `dashboards/grape-gsb-bus.json` | `grape-gsb-bus` | GSB 服务总线调用总览：走总线的调用速率/占比、流向、延迟、状态、实例分布，以及 HTTP 接口侧对照 |
| `dashboards/grape-task-pipeline.json` | `grape-task-pipeline` | 任务流水线：入队/派发/终态漏斗、队列深度（按来源）、排队与运行时长分位、卡死任务、Runtime 在线。回答「任务卡在哪一段」 |
| `dashboards/grape-entry-slo.json` | `grape-entry-slo` | 入口与 SLO：QPS、真实 5xx/4xx 比例、p95、错误预算（99.5%/7d）、未匹配路由占比、Top 路由速率/错误/延迟三张表。回答「用户看到的好不好」 |
| `dashboards/grape-db-health.json` | `grape-db-health` | 数据库连接健康：连接数/活跃比、中断与新建连接速率、上限利用率、按 (节点,角色) 明细。回答「连接层是不是健康」 |

三张新看板共用变量 `env`（Prometheus 的 `env` 标签）与 `host`/`role`，同一份 JSON 可同时服务多环境，环境选择在顶栏。

## 告警规则

`alerts/alerts-grape-sre.json` 是 Grafana 统一告警规则的导出（rule group `grape-sre`，label `severity`/`team`）。
`import-dashboards.sh` 按 `(folder, title)` 幂等 upsert：缺的建、有的改。

这里**只放 `deploy/alerts.yml` 没覆盖的规则**，避免同一指标出现两套阈值：

| 规则 | 条件 | 持续 | 级别 | 为什么放这里 |
|---|---|---|---|---|
| 业务进程不可达 | `min by (job,host)(up{job=~"grape-.+-(mgmt\|event\|worker\|insight)"}) < 1` | 1m | P1 | `alerts.yml` 只看业务指标，不看进程/scrape 存活 |
| 节点指标抓取掉线 | `min by (host)(up{job=~"grape-.+-node"}) < 1` | 2m | P2 | 同上，主机层盲区 |
| 入口 5xx 超阈值（排除 RUM 反代） | 排除 `/faro/collect` 后 5xx 比例 ≥ 0.5% | 5m | P1 | 阈值与 `alerts.yml` 的 `GrapeHTTPErrorRateWarning`（0.5%）一致；差别只在于排除 `/faro/collect`（见下） |

任务卡死、任务队列积压、任务失败率、DB 连接池使用率、DB 中断连接这五条已经在 `deploy/alerts.yml` 里，
**不要在这里复制一份**（同一指标两套阈值必然漂移）。`alerts.yml` 没被加载的环境，按它的头部注释启用：

```bash
promtool check rules deploy/alerts.yml
# 在 prometheus.yml 里加 rule_files: [ /path/to/alerts.yml ]，然后 SIGHUP
```

看板里的数值口径都有出处，不是拍脑袋：

- 「错误预算剩余（99% · 7d）」的目标 99% 取自 `src/domains/insight/observability/slo.py` 的默认 `target`（0.99），
  窗口用 7 天（数据保留期内的滚动窗口），不是另外发明一个 SLO。
- 「入口 5xx 超阈值」的 0.5% 与 `deploy/alerts.yml` 的 `GrapeHTTPErrorRateWarning` 同口径
  （`alerts.yml` 的 Critical 为 2%；Grafana 侧这一条只覆盖排除 `/faro/collect` 的口径）。
- `grape_db_*` 的聚合用 `max()`：这些值取自 MySQL `SHOW GLOBAL STATUS`，每个进程都会把同一份数字报一遍，
  用 `sum()` 会把真实值放大 N 倍（实测把 48/min 显示成 384/min）。

注意：`/faro/collect` 在 `GRAPE_FARO_UPSTREAM` 未配置时固定返回 503（代码里的 feature-off 分支），属于设计行为。
它会长期占据 5xx 的绝大部分，所以 5xx 面板与告警都排除它，另用「RUM 反代 503」面板单独跟踪：
该面板为 0 表示反代已配置并在转发，持续 > 0 表示浏览器 RUM 数据在丢。

## 数据源 uid 不是固定的

Grafana 用 API 建数据源时给的 uid 是随机的（例如 `PBFA97CFB590B2093`），而看板 JSON 里按 uid 引用。
`import-dashboards.sh` 导入前会按**数据源类型**把看板里的 uid 改写成目标环境的实际 uid：

```bash
GRAFANA_URL=http://<host>:3000 GRAFANA_AUTH=admin:pw \
GRAFANA_DS_PROM_UID=<prom uid> GRAFANA_DS_LOKI_UID=<loki uid> GRAFANA_DS_TEMPO_UID=<tempo uid> \
bash deploy/grafana/import-dashboards.sh
```

不设这三个变量就用 `prometheus`/`loki`/`tempo` 当默认 uid（与 `_ensure_ds` 建出来的一致）。
脚本按 uid **和 name** 双重判重，已经存在同名数据源时不会再报一次假失败。

告警规则走同一套映射：`alerts/*.json` 里写 `prometheus`，导入时按类型换成目标环境的 uid；
规则归属的 folder 由 `GRAFANA_FOLDER`（默认 `Grape`）决定，**导入不会回写 repo 里的 JSON**——
仓库文件始终是「与环境无关的源」，环境差异只体现在运行时的映射参数上。

`--check` 覆盖看板与规则两侧（只报存在/缺失，不比对内容）；任何一项导入失败，脚本以非 0 退出，
不会在失败后照样打印「完成」。

## 新增看板的做法

```bash
# 1) 在 Grafana UI 里调好，导出 JSON（Share → Export）
# 2) 确认 uid 是稳定字符串（不要留空，否则重建后 uid 变化，脚本会重复建）
# 3) 放进 dashboards/，跑一次 import 验证幂等
bash deploy/grafana/import-dashboards.sh --check
```

约定：

- **uid 必须显式写死**（如 `grape-gsb-bus`），不能留空——空 uid 每次导入都会新建一份。
- 看板只用固定 uid 的数据源（`prometheus` / `loki` / `tempo` / `grape-mysql`），不要用变量或"默认数据源"，否则换环境后面板全空。
- 中文标题 + 判读说明（最后一个 markdown 面板写清"这块看板回答什么问题"），方便别人接手时不用猜。

## GSB 看板的现状（重要）

`grape-gsb-bus` 现在大部分面板是 "No data"，这是**真实情况**而不是面板坏了：

```
grape_gsb_client_* 只在 contracts/remote/*_remote.py 的 RemoteProvider(HTTP) 模式被调用。
当前跨域走的是：
  读  → owner 域 *_any 原语（DSN 直连）
  写  → outbox 异步写回
RemoteProvider 自身有回环风险（provider.py 注释：同域调用会 HTTP 打回自己），所以未启用。
```

所以"通过 GSB 的调用占比"现在是 0。这块看板的作用是**基线**：将来如果有人把跨域调用从 DSN 直连改成 RemoteProvider，占比会从 0 跳起来，那时一眼能看出边界被重划了。