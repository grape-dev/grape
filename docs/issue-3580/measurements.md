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
## 5. 常量来源 · 平台/业务口径（非本地实测）

以下常量无法在本机测量，来源为平台厂商文档或业务约定，登记在此供复核。

| 常量 | 位置 | 值 | 来源 |
|---|---|---|---|
| `SOON_DAYS` | `src/domains/mgmt/user/credential_expiry.py` | 14 天 | 业务口径：迭代两周一次，14 天覆盖一个轮换窗口；对 90 天有效期 PAT 相当于剩余约 15.6% 时提醒 |
| `MAX_SEGMENT_CHARS` | `src/kernel/repo_path.py` | 100 | 厂商文档：GitHub 仓库名上限 100 字符；其余在册平台不低于该量级，取四平台最严值 |
| `MAX_PATH_CHARS` | `src/kernel/repo_path.py` | 255 | 厂商文档：GitLab 项目完整路径校验上限 255 字符；其余平台合计远低于此，取交集上界 |

复核方式（只读，不改代码）：

```bash
# 1) 平台侧真实验证：超长名应被平台拒绝（示例为 GitLab，需维护者令牌）
TOKEN=***; HOST=gitlab.example.com
curl -s -o /dev/null -w "%{http_code}\n" -X POST "https://$HOST/api/v4/projects" \
  -H "PRIVATE-TOKEN: $TOKEN" --data "name=$(python3 -c 'print("a"*101)')"
# 期望：400（name/path 超限），而非 201

# 2) 业务口径确认：提醒提前量是否仍按"一个迭代"（14 天）
grep -n "SOON_DAYS" src/domains/mgmt/user/credential_expiry.py
```

## 6. 常量来源 · 分页与上限（平台文档 / 部署口径，非本机实测）

| 常量 | 位置 | 值 | 来源与说明 |
|---|---|---|---|
| `_WEBHOOK_MAX_BODY_BYTES` | `src/domains/runtime/routes/webhook.py` | 10 MB | ① 部署口径：边缘 nginx `client_max_body_size 10m`（应用上限不得高于边缘，否则两处口径分裂）；② 平台口径：GitHub 单次投递载荷硬上限 25MB（厂商文档），10MB 覆盖 push/PR/issue 事件分布；③ 与附件 200MB 上限无关（附件走独立上传通道） |
| `connect_timeout`（视图自检） | `src/kernel/domain_views.py` | 10s | 统一到 `mysql_compat` 的既有连接口径（10s）；启动期检查用更短超时会在多库拓扑下被误判成网络故障而跳过一致性检查 |
| `_PER_PAGE` | `src/domains/mgmt/vcs/base.py` | 100 | 厂商文档：GitHub / GitLab 列表接口 `per_page` 上限均为 100 |
| `_MAX_LIST_PAGES` | `src/domains/mgmt/vcs/base.py` | 20 | 沿用 base `src/kernel/token_resolver.py:387/443` 的历史值（provider 抽取时原样搬入）；100×20 = 2000 条封顶，触顶时打 WARNING 说明结果被截断 |
| `_HOOKS_TREE_MAX_PAGES` | `src/api/git_repo_api.py` | 50 | 100×50 = 5000 条 hook 封顶，用于 hook 树遍历的上界保护 |


## 7. 清扫节奏与配置缓存（对应 `runtime/webhook_inbox.py`、`mgmt/vcs/namespaces.py`）

### 7.1 `PURGE_INTERVAL_SECONDS = 3600`

复核脚本（合成库，不碰生产数据）：建 `grape_t_webhook_inbox` + 归档表，按 1:1:1:1
灌入「48h 前 consumed / 10 天前 dead / 40 天前 pending / 活跃 pending」，对
`purge_finished()` 整体计时（DELETE + 归档 INSERT…SELECT + 再 DELETE）：

| 主表行数 | 单次清扫耗时（多次取样） | 清扫后 主表 / 归档 |
|---|---|---|
| 400（生产 330 行的近邻） | 14.6 / 2.6 / 3.1 ms（median 3.1） | 100 / 200 |
| 10,000 | 38.1 / 33.2 / 33.6 ms（median 33.6） | 2,500 / 5,000 |
| 50,000 | 161.8 / 155.0 ms（median 158.4） | 12,500 / 25,000 |

据此取 3600s：

- 生产量级单次清扫约 3 ms；50k 行（现有投递量的 ~150 倍）也只有 ~0.16 s/小时，
  频率再高收益不抵成本；
- 3600s = 消费保留期 `CONSUMED_RETENTION_HOURS=24` 的 1/24 → 过期行最长滞留 1 小时，
  相对 24h 桶的溢出 ≤4%；
- 归档可见延迟 ≤1 小时，相对「30 天倒查」的排障窗口可忽略。

```bash
# 复跑口径（不改生产库）：sqlite 合成库 + 上面四类行，对 purge_finished() 计时
DEVOPS_DB_BACKEND=sqlite DEVOPS_DB_PATH=/tmp/inbox_bench.db \
  python - <<'PY'
# 灌 400/10k/50k 行 → time purge_finished() 三次 → 记录 median
PY
```

### 7.2 `_TTL_SOURCE_TTL = 30.0`

```python
# 20 次读 DB meta（dev mgmt 库，只读）
from src.domains.mgmt.user import db as user_db
user_db.get_meta_value("create_spaces_cache_ttl_seconds")
```

实测（2026-11-23，dev mgmt 库，20 次）：min 0.50 / p50 0.55 / p95 0.70 / max 0.83 ms
（当前未配置，返回空串）。

据此取 30.0s：

- 该常量缓存的是**配置源读取**（否则 create-targets 每个请求一次 DB 往返），不是上游调用；
- 30s 把「管理面改 `create_spaces_cache_ttl_seconds` → 多实例生效」的延迟限定在 30 秒，
  远小于默认 TTL 1800s，配置修改不会被长缓存掩盖；
- 按 A3 的 50 tps 目标，30s 缓存把 meta 读从 50 次/秒降到 ≤1 次/30 秒，
  DB 时间从约 27 ms/s 降到约 0.02 ms/s。

| 常量 | 位置 | 值 | 来源 |
|---|---|---|---|
| `PURGE_INTERVAL_SECONDS` | `src/domains/runtime/webhook_inbox.py` | 3600s | 实测（7.1：3 ms@400 行、158 ms@50k 行）+ 推导（1h = 24h 保留桶的 1/24） |
| `_TTL_SOURCE_TTL` | `src/domains/mgmt/vcs/namespaces.py` | 30.0s | 实测（7.2：p50 0.55 ms/次）+ 推导（配置生效延迟上限 30s，远小于 1800s 默认 TTL） |
