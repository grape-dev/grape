# 组织下派/上下文运行时数值登记（B2 数值证据门）

> 用途：登记本 PR 新增的运行时阈值/预算，作为代码注释 `rationale` 的引用出处。
> 口径遵循架构守护规则 B2：无出处不得给数字；尚未实测的值进入 §3「待决策」，
> 不伪装成实测。代码侧均已 env 可调（有 env 名的项）。

## §1 登记范围

| 位置 | 常量 | 默认值 | 作用 |
|---|---|---|---|
| `src/domains/mgmt/org_provision.py` | `_ORG_LEASE_TTL` | 600s | 组织供给跨进程租约 |
| `src/domains/mgmt/user/org_db.py` | `_ORG_REPO_BIND_LEASE_TTL` | 30s | 组织仓库绑定/解绑租约 |
| `src/domains/mgmt/org_context_store.py` | `_PUBLISH_LOCK_TIMEOUT` | 10s | 上下文发布串行化锁 |
| `src/kernel/context_digest.py` | `_MAX_CONCLUSION` / `_MAX_SUMMARY` / `_MAX_ITEM` / `_MAX_ITEMS` / `_MAX_TOTAL_ITEMS` | 1200 / 600 / 360 / 8 / 16 | 下派结论摘要预算 |
| `src/domains/mgmt/org_context_snapshot.py` | `_SECTION_CAPS` / `_TOTAL_CAP` / `_FILE_CAP` | 8/32/64/32KB；256KB；64KB | L0~L3 快照预算 |

## §2 取值依据

- **600s 组织供给侧租约**：一次供给包含建仓、建场景、首次绑定三步外部调用；需求
  Issue #2403 给出的目标为“组织创建后 10 分钟内可用”。租约覆盖“建仓失败重试 +
  平台限流等待”的最长单轮路径；供给循环每轮 `renew_lock`，长任务不会因 TTL 到期
  丢锁，TTL 只用于进程静默死亡后的故障接管。
- **30s 绑定租约**：绑定/解绑是单条事务内的写操作，目标耗时在毫秒级
  （`tests/mgmt/test_org_manual_bind.py` 覆盖并发绑定）；30s 用于覆盖慢 SQL 与瞬时
  锁竞争。内核默认 60s 面向分钟级任务，绑定场景取更短值让用户重试不被长时间挡住。
  `attempts=20 × interval=0.05s` 与 `src/kernel/lock.py` 默认一致。
- **10s 发布锁**：正文上限 5MB（`MAX_CONTENT_BYTES`），发布含一次版本查询、一次旧
  active 降级、一次新行置顶，均走 `(org_id, kind, ctx_key)` 索引；10s 覆盖慢盘/锁
  等待的最坏路径，超时失败而不是无限挂请求。
- **摘要预算 1200/600/360 字符，8/16 条**：中文约 1 token / 2 字符，折算约
  300/150/90 tokens；16 条上限约 1.4k tokens，占 L3 32KB 快照预算不足 5%，避免摘要
  挤占执行侧规则正文。
- **快照预算 8/32/64/32KB（总 256KB，单文件 64KB）**：按子任务注入侧可读体积上限
  设计，正文落盘、DB 只存 manifest；超限部分按 section 优先级截断并记录。

`env` 可调项：`ORG_DISPATCH_TARGET_TIMEOUT`、`ORG_DISPATCH_SWEEP_INTERVAL`、
`ORG_DECISION_MAX_BYTES`、`ORG_DISPATCH_MAX_TARGETS`、`ORG_CONTEXT_REPO_MAX_FILES`、
`ORG_CONTEXT_REPO_MAX_BYTES`。

## §3 待决策（未实测，状态 open）

| key | 待决策内容 | 缺哪种测量 | 建议方案 |
|---|---|---|---|
| `_ORG_LEASE_TTL` | 600s 是否覆盖目标环境 P99 供给时长 | 建仓 + 建场景 + 绑定三段 P95/P99 实测 | 接指标后跑 2 周，按 P99 × 1.5 调整 |
| `_PUBLISH_LOCK_TIMEOUT` | 10s 是否覆盖最大正文写库 | 发布正文大小分布 + 写库时延 | 用 `grape_org_context_publish_duration_seconds` 观测窗口确定 |
| 摘要预算 | 1200/600/360 字符对结论信息密度的影响 | prompt token 计量与人工抽检 | 用真实下派样本统计截断率后调整 |

> 说明：待决策项不影响门禁结论（已登记出处与缺口）；实测完成后回填 §2 并关闭本节。

## §4 保留策略（A7）

组织下派/快照表在组织存活期内会随需求增长，采用「TTL + 周期清理」而非分区（SQLite/MySQL
双后端统一口径）：

- `ORG_DISPATCH_RETENTION_DAYS`（默认 90 天）：仅清理**终态**批次（completed /
  needs_review / partial_failed / failed / cancelled）且 `created_at` 早于截止时间的行；
  非终态批次永不清理。
- 清理顺序：聚合台账 → 目标 → 快照行 → 批次行；快照文件目录
  （`WORKSPACE_DIR/context-snapshots/...`）同步删除。
- `ORG_RETENTION_INTERVAL_SECONDS`（默认 86400s）：由既有下派 sweeper 节流触发，
  避免每小时全表扫描；设 0 可关闭周期清理（仍可手工调用）。
- 组织删除时仍有即时清理（`delete_org_with_cleanup`），与该 TTL 策略互补。

## §5 可观测性接入（A13）

| 维度 | 接入点 |
|---|---|
| Metrics | `src/kernel/org_metrics.py`（可安装收集器，由 insight metrics server `install(registry)`）：`grape_org_dispatch_total` / `grape_org_dispatch_duration_seconds` / `grape_org_dispatch_inflight` / `grape_org_provision_total` / `grape_org_provision_duration_seconds` |
| Logs | `org_dispatch` / `org_provision` / `org_retention` 全链路 `log.*`，经既有 Loki 链路 |
| Traces | `src/kernel/otel.feature_span`：`org.provision`、`org.dispatch.create_child`（Tempo） |
| Profile | 进程级 Pyroscope 由 Grafana Alloy 采集（无按特性埋点；与仓库其他后台任务一致） |
