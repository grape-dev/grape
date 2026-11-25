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
| 3 | 熔断 + 退避（5m→15m→1h 封顶，仓库级共享窗口；到点**原子占位**只放行一次） | `issue_poll_breaker.allow_read/note_permanent_failure` |
| 4 | 逐 issue 读短路（熔断期 0 上游请求，仅平台凭据的读路径） | `_vcs_get_issue` |
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
- **仓库级事实的门槛（复审后）**：只有「404/410 **且**平台仓储凭据」才能写
  `vcs_status=vcs_missing`。400/401/403/422 与任何调用方凭据的失败只走
  `note_caller_error`（零写库、零探测），否则私有仓/机器人失可见性（上游 `/repos`
  同样 404）会把健康仓库判死。
- **到点放行 = 原子占位**：`allow_read()` 在 30s 缓存的 due 判定后单次持锁置
  `_PROBE_LEASES[repo]` 并把 `vcs_next_probe_at` 前移一个租约（60s），8 并发批量复核
  同一时点只放行 1 个线程，其余继续短路。
- **探测 unknown 不解除熔断**：已确认 `vcs_missing` 的仓库，探测超时/401/403 只把
  下一次探测时间再推迟一档，不得把 `vcs_status` 洗回 `ok`。
- **合成 missing 不落删除**：熔断期 `_vcs_get_issue` 返回的 missing 是 0 上游请求的
  合成结论；`issue_vcs_index._confirm_missing` 在熔断中整体 fail-open（候选全部
  unresolved），不软删 issue 卡。
- **fail-open**：台账读不到（无卡 / DB 不可用 / 非 owner 拓扑）→ 不短路。宁可多打一次
  上游，也不能把健康仓库当孤儿熔断。
- **跨域写**：runtime 进程写 mgmt 归属表一律经 `mgmt.public`（`set_repo_vcs_health`）
  并走 `cross_write` 幂等意图；写失败只降级为进程内状态，抛错不外泄到读路径。
  运维 reset 反向：由 mgmt 路由直接写自己的表，再让 runtime 清进程缓存（不做跨域写）。

## 6. 复审口径（PR #2164 QA 报告 comment 203631490）

| 复审项 | 落点 | 看护用例 |
|--------|------|----------|
| Required 1：仓库级事实与调用方凭据解耦 | `note_caller_error`（零写库）+ `note_permanent_failure(credential)` + `is_repo_absence_status` | `TestCredentialDecoupling` |
| Required 1 影响链：合成 404 不软删卡片 | `issue_vcs_index._confirm_missing` 熔断期整体 fail-open | `TestConfirmMissingFailsOpen` |
| Required 2-①：到点只放行一次 | `allow_read()` 原子占位 + 60s 租约 | `TestProbeRelease::test_concurrent_release_allows_exactly_one_probe` |
| Required 2-②：unknown 不解除熔断 | `note_permanent_failure` 的 unknown 分支保留 `vcs_missing` | `test_unknown_probe_keeps_confirmed_fuse` |
| Optional：成功读不再逐条查库 | `note_recovery` 先用缓存判环境卫生 | `TestRecoveryCache` |
| Optional：记账节流 TOCTOU | `_throttle_note` 单次持锁完成「判断 + 置位」 | `test_repeated_failures_are_throttled_for_db_writes` |
| Optional：API 短路分支 / api_client 折叠无用例 | 用户态不再短路（改为用例固定「透传 + 零台账写」）；api_client 键归一与折叠 | `TestCredentialDecoupling::test_user_path_404_*`、`TestApiClientLogDedupe` |
| Optional：契约 change_reason 未列明吸收的漂移 | `--update-baseline --reason` 写明 401→402 与 2 处无关漂移 | `help/api/openapi-baseline.json`、`contracts/openapi_baseline.json` |
| Nit：`VCS_HEALTH_COLUMNS` 扩大 public 面 | 私有化为 `_VCS_HEALTH_COLUMNS` | `tests/kernel/test_gsb_route.py` |
| Nit：mgmt 路由跨域回写成环 | reset 写语义下沉 mgmt，runtime 只清缓存 | `test_ops_reset_clears_ledger_through_mgmt` |

## 7. 陷阱（下个同类 Issue 必看）

1. **用户态读路径不得写仓库级 VCS 台账**。调用方凭据的 400/401/403/422/404 只说明
   「这一方看不到/没权限」，写进 `managed_repos.vcs_*` 会让**所有**调用方吃到合成
   404，并连锁触发 issue 卡软删。仓库级事实只能由「读与探测同凭据」的平台链路落账。
2. **熔断放行必须占位**。只判 `now >= next_probe_at` 的「到点即放行」在 8 并发批量
   复核下等于整批打上游；unknown 探测也不得清熔断（一次 5s 超时会把已确认的
   `vcs_missing` 洗白）。