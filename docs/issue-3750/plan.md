# Issue #3750 方案设计（Plan）

## 1. 结论

代码侧交付三件事：**拒绝事实落库**（`grape_t_obs_webhook_rejects`）、**按仓库速率告警**（insight 指标 +
种子规则 + Prometheus 规则）、**拒绝日志字段补全**（`event`/`reason`，404 补 `event`/`ip`）。
403 的 token 指纹（#3897）与 `hmac.compare_digest`（#860）保持现状，一张表 + 一条规则 + 一处日志
收口，不引入新链路。

## 2. 数据层

### 2.1 表 `grape_t_obs_webhook_rejects`（归 insight 域）

```sql
CREATE TABLE IF NOT EXISTS grape_t_obs_webhook_rejects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key    TEXT NOT NULL DEFAULT '',   -- gitcode:owner/repo
    provider    TEXT NOT NULL DEFAULT '',   -- gitcode / github
    event       TEXT NOT NULL DEFAULT '',   -- X-Gitcode-Event（unknown 表示未带）
    ip          TEXT NOT NULL DEFAULT '',   -- client_host
    token_fp    TEXT NOT NULL DEFAULT '',   -- sha256:<12> / absent
    expected_fp TEXT NOT NULL DEFAULT '',   -- sha256:<12> / absent
    reason      TEXT NOT NULL DEFAULT '',   -- absent / mismatch
    created_at  TEXT NOT NULL DEFAULT ''    -- ISO(UTC)
)
```

- DDL 常量放 `src/domains/insight/schema_ddl.py`（属主域），由 `src/api/db_setup.py` 的
  `_NEW_TABLES` 登记建表（新装/存量库启动即建；`_owned_skip` 守卫按 `configs/table_ownership.json`
  判定，本表登记为 `insight`）。
- 索引 `(repo_key, created_at)` 走既有建索引清单，服务「按仓库 + 时间窗口计数」。
- **时间列统一写 ISO(UTC) 字符串**，窗口过滤用字符串比较（与 `obs_alert_events` 同口径，字典序=时间序，
  SQLite/MySQL 行为一致）。

### 2.2 写入路径（有界非阻塞）

`src/domains/insight/observability/webhook_rejects.py`：

| 函数/类 | 职责 |
|---------|------|
| `token_fingerprint(v)` | `sha256(v)[:12]` 带 `sha256:` 前缀；空值 → `absent`（唯一实现，#3897 断言形态不变） |
| `reject_reason(token)` | `absent`（未带 token）/ `mismatch` |
| `RejectRecorder` | `queue.Queue(maxsize=1024)` + daemon 线程批量 `INSERT`（≤100 行/批，1s 或队满触发）；队满 `put_nowait` 丢弃并计数 |
| `record(...)` | 组装行 → `put_nowait`，**任何异常都不上抛**（403 返回不被影响） |
| `count_recent(cursor, cutoff_iso)` | `SELECT repo_key, COUNT(*) ... WHERE created_at >= ? GROUP BY repo_key`（供告警求值，SQL 留在属主模块） |
| `maybe_cleanup(days=14)` | 按留存期删过期行（小时级限流，同 `logs.maybe_cleanup` 形态） |

- `OBS_WEBHOOK_REJECTS=0` 可整体关闭（与 `OBS_LOG_DB` 同风格）；`OBS_WEBHOOK_REJECTS=sync`
  走同步落库（同一 `_write_rows` SQL 出口），供测试使用——daemon 线程跨「换测试库 close/重绑
  连接」落库会在 sqlite C 层撞 UAF（与 `OBS_LOG_DB=0` 同族根因，见 `tests/conftest.py` 注释）。
- 同时对 Prometheus 计数器 `grape_webhook_token_mismatch_total{repo}` 自增（拒绝数看板/告警同源），
  计数器自增在 `record()` 同函数内，**与落库成败无关**（落库失败也保计数）。

## 3. 路由层（`src/domains/runtime/routes/webhook.py`）

1. `_token_fingerprint()` 改为薄包装：`from src.domains.insight.public import token_fingerprint`（#3897 用例仍绿）。
2. GitCode 403：日志补 `event=`/`reason=`；随后 `record_webhook_reject(...)`（`try/except` 兜底，绝不改状态码）。
3. GitCode/GitHub 404 unmanaged：日志补 `event=`/`ip=`。
4. GitHub token 403：同口径补 `event=`/`ip=`/`reason=` 并落库（同一类缺陷，一并收口；签名不一致 403 只补日志/ip，不落 token 指纹行——那是 HMAC 校验不是 token 比对）。
5. 403 响应体、`compare_digest`、404 语义**全部不变**。

## 4. 告警层

1. `src/domains/insight/observability/alerts.py`
   - `_repo_metrics()` 新增 `webhook_token_mismatch`（近 60s 按仓库计数），**只来自拒绝表**；
     该查询独立 `try/except`，表缺失/查询失败只丢这一项指标，不连带其它指标。
   - 维度集合合并：仅在拒绝表出现、近期无任务的仓库也要进 `repo_m`（否则规则永远求不到值）。
   - `SEED_RULES` 新增：
     `webhook_token_mismatch_rate` / metric `webhook_token_mismatch` / `gt 10` / `window_sec 60` /
     `for_sec 300` / `severity warning` / `level user` / `seed_source GrapeWebhookTokenMismatch`。
2. `deploy/alerts.yml` 新增 `GrapeWebhookTokenMismatch`：
   `sum by (repo) (increase(grape_webhook_token_mismatch_total[1m])) > 10`，`for: 5m`，`severity: warning`。
3. 留存清理挂点：`src/api/main.py` / `src/api/server_tasks.py` 的 60s 采样循环内，
   与 `obs_logs.maybe_cleanup()` 并列调用 `webhook_rejects.maybe_cleanup()`（自身小时级限流）。

## 5. 测试策略（RED → GREEN）

| 用例文件 | 覆盖 |
|----------|------|
| `tests/insight/test_issue_3750_webhook_rejects.py` | 指纹/理由；`record` 落库字段齐全；队满丢弃不抛；`count_recent` 窗口与跨仓库不聚合；留存清理；DDL 建表登记 |
| `tests/runtime/test_issue_3750_webhook_reject_ledger.py` | GitCode 403 落一行且日志含 `event`/`reason`；响应体与不派发；404 日志含 `event`/`ip`；落库异常不影响 403 |
| `tests/insight/test_issue_3750_webhook_mismatch_alert.py` | 指标值；种子规则字段；11 次/分钟持续 5 分钟 → firing、回落 resolved、跨仓库不聚合 |
| `tests/deploy/test_issue_3750_alerts_yml.py` | `deploy/alerts.yml` 含规则且表达式引用 `grape_webhook_token_mismatch_total`；YAML 可解析 |

运行：`python3 -m pytest tests/insight/test_issue_3750_*.py tests/runtime/test_issue_3750_*.py tests/deploy/test_issue_3750_*.py -q`
+ 回归 `tests/insight/test_obs_alerts.py tests/insight/test_obs_retention.py tests/runtime/test_issue_3897_webhook_fingerprint.py`。

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 请求路径被写库拖慢 | 有界队列 + 队满丢弃（非阻塞），daemon 批量落库 |
| 新表在旧库缺失 → 指标报错 | 建表随 `db_setup` 幂等执行；指标查询独立 `try/except` 降级 |
| 告警噪音 | 10/min + `for_sec=300`，`warning` 级不阻断 |
| 指纹被视作弱泄露 | 12 位十六进制、不可逆；禁止明文/前缀的回归用例（#860/#3897）继续看守 |
| 表膨胀 | 14 天留存清理（小时级限流） |