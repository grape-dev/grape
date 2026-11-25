# Issue #3759 — 方案设计（plan）

## 1. 分层

```
上游 4xx ──► git_repo_api._vcs_req ──► (分类 permanent) ──┐
           └─ api_client._do_request ──► (窗口聚合日志)   │
                                                          ▼
                                    issue_poll_breaker（runtime 域）
                                      ├─ probe_repo：一次 /repos/{o}/{r}
                                      ├─ 状态台账：managed_repos.vcs_*（mgmt 域）
                                      └─ backoff 5m→15m→1h（仓库级共享）
                                                          │
逐 issue 读 ──► _vcs_get_issue / GET issues/{n} ──► blocked? ──► 短路（missing / 404）
                                                          │ 否
                                                          ▼
                                            真实读 / 失败落台账 / 成功清熔断
```

## 2. 模块与改动

| 文件 | 类型 | 说明 |
|------|------|------|
| `src/kernel/log_dedupe.py` | 新增 | `WindowDeduper`：按 key 固定窗口（默认 60s）去重；窗口滚动回传上一窗口折叠数 |
| `src/domains/runtime/vcs/issue_poll_breaker.py` | 新增 | 永久失败判定 + 仓库级探测 + 退避 + 熔断判定 + 自愈 + 运维 reset；台账读写（mgmt.public / 30s 缓存） |
| `src/api/git_repo_api.py` | 修改 | `_vcs_req` 日志窗口聚合（key=provider+仓库+归一资源+错误码；永久失败附处置建议）；`get_issue` 熔断短路 + 失败落台账 + 成功自愈 |
| `src/domains/runtime/vcs/api_client.py` | 修改 | HTTP 错误 WARNING 同款窗口聚合（脱敏 URL 语义不变） |
| `src/domains/runtime/pipeline/event_pipeline.py` | 修改 | `_vcs_get_issue` 熔断短路；404/401/403 落台账；成功清熔断 |
| `src/domains/mgmt/public/__init__.py` | 修改 | 新增 `get_repo_vcs_health` / `set_repo_vcs_health`（列白名单 + 单库拓扑回落） |
| `src/domains/mgmt/user/db.py` | 修改 | 新装库 DDL 带 8 列；`get_managed_repos` 暴露 `vcs_*` 字段 |
| `src/domains/mgmt/migrations/repo_vcs_health_columns.py` | 新增 | 迁移 `202611250001`：存量库幂等补 8 列 + 后置校验 |
| `src/api/db_setup.py` | 修改 | 注册迁移（只接线，实现住 mgmt 域） |
| `src/domains/mgmt/routes/repos.py` | 修改 | `/managed` 暴露 `vcs_missing` 等字段；新增 `POST /{o}/{r}/vcs-health/reset` |
| `src/kernel/gsb/route_config.py` | 修改 | 登记两个新 public 原语（PUBLIC_PRIMITIVES） |

## 3. 关键决策

| 决策点 | 选项 | 选择 | 理由 |
|--------|------|------|------|
| 台账落点 | 新表 / 仓库卡列 | **仓库卡列**（`managed_repos`） | 需求明确「仓库卡落 `vcs_missing`」；卡已有跨域写白名单机制，运维可见 |
| 失败判定 | 仅 404 / 4xx 除 429 | **4xx 除 429** | 401/403 同为不可自愈（权限/归属错配）；429 限流可重试 |
| 熔断粒度 | 逐 issue / 仓库级 | **仓库级共享窗口** | 告警量与上游调用量不随 issue 数线性增长（验收硬项） |
| 探测策略 | 每次失败都探 / 退避探 | **首次 + 退避到点** | 首次失败只多一次上游请求；熔断期 0 请求 |
| 探测不确定 | 熔断 / 不熔断 | **不熔断（fail-open）** | 401/403 可能只是 token 局部失效，误熔断会伤健康仓库 |
| 日志去重 | 永久抑制 / 窗口 | **固定窗口 + 滚动汇总** | 永久抑制会把「反复失败」静默掉 |
| 线程安全 | 无锁 / 单锁 | **单锁 + 惰性单例** | 读路径 8 并发复核会同时打同一 key |

## 4. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 误判永久失败错误熔断健康仓库 | 仓库级探测必须 `missing` 才熔断；探测 unknown/401/403 一律不熔断；`vcs-health/reset` 一键清除 |
| 台账读写放大（每条读一次库） | 30s 进程缓存；`note_recovery` 仅在有脏状态时写库（健康仓库零写） |
| 跨域写失败 | `cross_write` 幂等意图 + lib 失败回落；写失败降级为进程内状态，不改变读路径 404/403 契约 |
| MySQL 补列默认值坑 | 8 列全部 `VARCHAR(255)/INTEGER NOT NULL DEFAULT`，`allow_copy=True` + `raise_on_guard=True` + 后置校验（不盖章即重试） |
| 熔断误伤批量删除复核（`_confirm_missing`） | 该路径复用 `fetch_issue_via_vcs`，熔断期返回 `missing` 与「仓库已不存在」事实一致 |

## 5. 运维操作

```bash
# 查看熔断状态（列表侧字段）
GET /api/repos/managed            # 每项含 vcs_status / vcs_missing / vcs_next_probe_at

# 上游恢复或重绑 provider 后清除熔断（下一次读自动重新探测）
POST /api/repos/{owner}/{repo}/vcs-health/reset

# 日志窗口可配（默认 60s）
GRAPE_UPSTREAM_LOG_WINDOW_SECONDS=60
```