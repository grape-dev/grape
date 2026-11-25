# Issue #3759 — 上游 VCS 永久 404 熔断与日志降噪（实现规格）

> 来源：Issue #3759 原始描述 + Issue Agent 的 Spec（评论 202834547）。本文件只记录
> **本轮落地范围与判定口径**，需求全文以 Issue/评论为准。

## 1. 现象与根因（已核对代码）

- `huaweicloud-mate/repository-requests` 在上游 GitCode 已删除（`Project not found`），
  平台台账仍留有仓库卡与 Issue 62/63；巡检每轮对两条 issue 各打一次上游 GET，
  每小时 76 条 WARNING，永不收敛。
- `src/api/git_repo_api.py::_vcs_req` 把上游 ≥400 原样透传（Issue #549/#558 契约），
  但**每次失败都打完整 URL + 响应体 WARNING**，且无状态、不记忆「已确认的永久失败」。
- `src/domains/runtime/vcs/api_client.py::_do_request` 同样逐次打 WARNING。
- `src/domains/runtime/pipeline/event_pipeline.py::_vcs_get_issue` 逐 issue 读上游，
  不感知「该仓库整体不存在」，告警量随 issue 数线性增长。

## 2. 本轮范围（In Scope）

| # | 能力 | 落点 |
|---|------|------|
| 1 | 永久失败判定：4xx（429 除外）为不可自愈 | `issue_poll_breaker.is_permanent_status` |
| 2 | 仓库级存在性探测（一次 `/repos/{o}/{r}`）+ 台账落仓库卡 | `issue_poll_breaker.probe_repo` / `managed_repos.vcs_*` |
| 3 | 熔断 + 退避（5m→15m→1h 封顶，仓库级共享窗口） | `issue_poll_breaker.blocked/note_permanent_failure` |
| 4 | 逐 issue 读短路（熔断期 0 上游请求） | `_vcs_get_issue`、`GET /api/git-repo/{o}/{r}/issues/{n}` |
| 5 | 日志窗口聚合（首条完整 + 计数 + 处置建议） | `src/kernel/log_dedupe.py` + 两处日志点 |
| 6 | 自动恢复（探测 exists / 读成功 → 清 `vcs_missing`） | `issue_poll_breaker.note_recovery` |
| 7 | 运维入口：一键清除熔断（重绑 provider 后调用） | `POST /api/repos/{o}/{r}/vcs-health/reset` |
| 8 | 列表侧可见 `vcs_missing`（前端标红数据源） | `GET /api/repos/managed` 增 `vcs_*` 字段 |

## 3. 明确不在本轮（Out of Scope）

- 前端渲染层标红（本仓前端 bundle 有体积棘轮与缓存版本闸，需独立 PR）：本轮只提供
  `vcs_missing` / `vcs_missing_since` / `vcs_last_error_code` / `vcs_next_probe_at` 字段。
- 「一键禁用订阅」的订阅开关：熔断本身已把上游调用压到 ≤1 次/小时，未新增无人消费的
  开关列（避免死代码）；`vcs-health/reset` 提供恢复面。
- 历史孤儿仓库卡批量清理迁移（另起运维脚本/Issue）。
- 不改 provider 注册表与既有 VCS 读写 HTTP 契约（#549/#558 状态码透传保持不变）。

## 4. 验收对照

| 验收项 | 实现/证据 |
|--------|-----------|
| 404/403/401（429 除外）判永久失败 | `test_issue_3759_vcs_breaker.py::TestClassification` |
| 首次 404 只探测一次、不随 issue 数增长 | `test_first_404_probes_once_and_fuses` / `test_second_issue_404_does_not_reprobe_within_backoff` |
| `vcs_missing` 后 ≤1 次/小时 | 退避档位封顶 3600s：`test_probe_backoff_ladder_grows_with_failures` |
| 同窗口日志聚合、首条完整、计数可查、message 带处置建议 | `TestUpstreamReqDedupe` + `tests/kernel/test_issue_3759_log_dedupe.py` |
| 运维入口 + 列表可见 | `repos_vcs_health_reset` 路由；`/api/repos/managed` 增字段（`tests/mgmt/test_issue_3759_repo_vcs_health_columns.py`） |
| 上游恢复/重绑后自动恢复 | `test_recovery_clears_fuse` / `test_vcs_get_issue_success_clears_fuse` / `test_reset_clears_all_fields` |

## 5. 关键口径

- **事实源 = 仓库卡**（`managed_repos`，mgmt 域）：多副本一致、重启不丢、可运维可视化；
  进程内 30s TTL 缓存只防「同一轮每个 issue 各查一次库」。
- **fail-open**：台账读不到（无卡 / DB 不可用 / 非 owner 拓扑）→ 不短路。宁可多打一次
  上游，也不能把健康仓库当孤儿熔断。
- **探测不确定不熔断**：探测 401/403/超时 → `unknown` → `vcs_status=ok`，只记错误码。
- **跨域写**：runtime 进程写 mgmt 归属表一律经 `mgmt.public`（`set_repo_vcs_health`）
  并走 `cross_write` 幂等意图；写失败只降级为进程内状态，抛错不外泄到读路径。