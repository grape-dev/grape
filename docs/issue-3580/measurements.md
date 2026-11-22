# 数值来源与复核方式（webhook 收件箱 / 命名空间清单 / 上游超时）

> 目的：把「这些魔法数字哪来的」变成可独立复核的产物（架构审查 B2）。
> 本文件记录取值时的**实测 SQL 与结果**；任何人可对生产库重跑同一查询核对。

## 1. 收件箱消费耗时与重试（对应 `runtime/webhook_inbox.py`）

```sql
-- 投递量 / 消费成功率 / 重试次数
SELECT COUNT(*) AS rows_all,
       SUM(status='consumed') AS consumed,
       MAX(attempts) AS max_attempts_seen
FROM grape_t_webhook_inbox;

-- 单条消费耗时分布（秒）
SELECT ROUND(MIN(d),2) AS min_s, ROUND(AVG(d),2) AS avg_s,
       ROUND(MAX(d),2) AS max_s, COUNT(*) AS n
FROM (
  SELECT TIMESTAMPDIFF(
           SECOND,
           STR_TO_DATE(claimed_at,'%Y-%m-%dT%H:%i:%s'),
           STR_TO_DATE(finished_at,'%Y-%m-%dT%H:%i:%s')) AS d
  FROM grape_t_webhook_inbox
  WHERE claimed_at<>'' AND finished_at<>'' AND status='consumed'
) t;
```

实测结果（2026-11-23，生产库快照）：

| 指标 | 值 |
|---|---|
| 投递总数 | 330 |
| 已消费 | 330（100%） |
| 观测到的最大 attempts | 0（零重试） |
| 消费耗时 min / avg / max | 0s / 0.31s / 5s |

据此取值：

| 常量 | 值 | 依据 |
|---|---|---|
| `DEFAULT_LEASE_SECONDS` | 300 | 实测最大消费 5s 的 60 倍 |
| `DEFAULT_MAX_ATTEMPTS` | 5 | 实测零重试；上限只用于吸收上游瞬时故障 |
| `base_backoff` / `max_backoff` | 5s / 900s | 5 次尝试压在 ~15 分钟内，给上游恢复留窗口 |
| `webhook_inbox_loop(poll_interval)` | 1.0s | 相对 5s 上界，端到端延迟增量 <1s |
| `METRICS_INTERVAL_SECONDS` | 60 | 与 `obs_metric_rollup.resolution='minute'` 桶粒度对齐 |
| `CONSUMED_RETENTION_HOURS` / `DEAD_RETENTION_DAYS` | 24h / 7d | 排障窗口（日/周两级） |
| `PENDING_ARCHIVE_DAYS` / `ARCHIVE_RETENTION_DAYS` | 30d / 180d | 归档保留期为最长活跃保留期 30d 的 6 倍 |

## 2. 上游 API 往返（对应 `mgmt/vcs/base.py`、`kernel/upstream_http.py`、`mgmt/vcs/namespaces.py`）

```bash
# gitlab.com /api/v4/user 连续 10 次，记录 time_total
for i in $(seq 1 10); do
  curl -s -o /dev/null -m 20 -w "%{time_total}\n" \
    -H "PRIVATE-TOKEN: $TOKEN" https://gitlab.com/api/v4/user
done
```

实测结果（2026-11-23，自建 worker 出口）：0.72s / 0.77s / 0.77s / 0.96s / 0.96s /
0.97s / 1.09s / 1.12s / 1.16s / 1.74s —— p50 ≈ 0.97s，上界 1.74s。

据此取值：

| 常量 | 值 | 依据 |
|---|---|---|
| 上游 `timeout` | 30s | 实测上界的 17 倍，可吃掉跨区抖动 |
| `_NS_TTL_SECONDS`（清单短缓存） | 30s | 一次清单 = 2 次上游往返（2–4s）；30s 让用户重试必然过 TTL |
| `DEFAULT_TTL_SECONDS`（create-targets） | 1800s | 组织成员变更是低频运维动作，30 分钟内可见可接受 |

## 3. 模板推送超时（对应 `mgmt/user/repo_manager.py`）

```bash
python3 - <<'PY'
import subprocess, time, shutil
t0=time.time(); subprocess.run(["git","clone","--depth","1","-q",URL,"/tmp/pp"],check=True)
t1=time.time(); subprocess.run(["git","push","-q","origin","HEAD:refs/heads/probe"],cwd="/tmp/pp",check=True)
print(f"clone={t1-t0:.2f}s push={time.time()-t1:.2f}s")
PY
```

实测结果（2026-11-23，主控 → gitlab.com，小仓约 95KB）：clone 2.48s、push 2.16s。
`timeout=180s` 为该量级的 80 倍，可覆盖数百 MB 模板仓，同时低于 CI 侧作业上限。

## 4. 未取到实测的部分（需业务口径）

- `push_template_to_repo` 大仓（>100MB）耗时分布：当前环境无大仓样本；
- `A3` 50tps 压测、`A4` `/check-name` p95：需压测环境。