# Spec: Issue #4241 — 仓库 webhook_ok 仅判 bool(webhook_id)，地址漂移时仍显示「正常」

> 评级：ready（维持） | 目标开发分支：**arch-weekly-11-ljt** | 本文件为仓库内实现索引副本（Issue 已发布 Spec v3 评论 203783509）。
> PR #2105 按基线裁定**不复用**（其 base=weekly-11-yaw，核心后端文件在新基线不存在），本实现按 arch-weekly-11-ljt 重建。

## 现象（arch-weekly-11-ljt 实测，缺陷仍在）
- `src/domains/mgmt/routes/repos.py` `list_managed_repos`：`"webhook_ok": bool(r["webhook_id"])` —— 唯一判据是 id 非空。
- webhook 地址存在两个来源：DB meta（`src/domains/mgmt/user/db.py:get_webhook_url()`）与 `configs/server-config.json`，
  两源无收敛比对，地址漂移（旧隧道地址 vs 新地址）无法被发现，仓库状态仍显示「正常」。
- `managed_repos` 无「实际注册地址」字段，无法做零远程调用的漂移比对。

## Objective
- 目标：`webhook_ok` 不再以 `webhook_id` 非空即判「正常」；引入地址三态判定（ok / stale / absent / unknown），前
  端提供可点击「修复托管」入口。
- 价值：消除「id 存在但地址漂移、实际投递已坏」的假阳性（#3750、#3906 已验证该类失效真实发生）。
- 成功标准：权威地址改成另一域名后，仓库列表与仪表盘同口径报 stale，并可在页面一键修复。

## Scope

### In Scope
- `src/domains/mgmt/user/db.py`：`managed_repos.webhook_url` 落盘列（DDL + 存量库幂等补列）；`add_managed_repo` /
  `_update_repo_bot_token` 写入实际注册地址；`get_webhook_url()` 优先级反转为 meta 权威、server-config 仅一次性种子；
  新增 `webhook_state()` / `webhook_state_for_row()` / `normalize_webhook_address()` / `get_managed_webhook_url()`。
- `src/domains/mgmt/routes/repos.py`：`GET /api/repos/managed` 输出 `webhook_ok`(bool 兼容) + `webhook_stale` + `webhook_state`；
  `GET /api/repos/member`（仓库大厅/ReposView 数据源）托管行带 `webhook_state`；托管/建仓/修复成功路径回写实际注册地址。
- `src/domains/insight/routes/dashboard.py` `_check_host_status`：webhook 已建立但地址漂移 → `webhook_stale`（保持零远程调用，防 429）。
- `src/domains/mgmt/public/__init__.py`：`webhook_state()` 跨域只读门面（insight 不直连 managed_repos）。
- 前端：`DashboardView.vue` 托管横幅新增 stale 分支（复用 `repairHost()`）；`ReposView.vue` 托管徽标三态 + 「修复托管」按钮；
  `shell/js/host-status.js` 与 `shell/locales/{zh,en}/{common,dashboard,repos}.json` 文案齐备。
- 测试：`tests/mgmt/test_issue_4241_webhook_state.py`（ok/stale/absent/unknown + 列表输出 + 仪表盘自愈）。

### Out of Scope
- webhook 投递链路的重试/告警机制。
- `server-config.json` 结构重构、`get_server_base_url()` 双键合并（既有裁定不合并）。
- 关联仓库 `grape-dev/grape-admin`：其代码只读 events/agent_runtimes/user_accounts，无 managed_repos 托管状态展示，本需求零改动。

## Technical Approach
1. 迁移：`managed_repos` 增列 `webhook_url TEXT DEFAULT ''`（新库走 `_MANAGED_REPOS_DDL`，存量库走
   `legacy_column_migrations._MANAGED_REPOS_LEGACY_COLUMNS` 幂等 `ALTER TABLE`，SQLite/MySQL 同一入口）。
2. 写入：`add_managed_repo(..., webhook_url=)` 与 `_update_repo_bot_token(..., webhook_url=)`；托管/建仓/修复/bot token 重建
   成功路径回写实际注册地址（独立 guarded UPDATE，列缺失时跳过不阻断主写）。
3. 权威源：`get_webhook_url()` 改为 meta 优先；meta 为空且 server-config 有值时一次性播种进 meta（进程内标记防重复），
   server-config 降级为种子/兜底；`get_server_base_url()` 口径不变。
4. 三态：`webhook_state_for_row(webhook_id, registered_url, authority_url)` 纯函数零远程——`absent`（id<=0）/`stale`
   （落盘地址非空且与权威前缀不一致）/`ok`（一致或以权威 + "/" 为前缀）/`unknown`（落盘空或权威未配置，不误报）。
   比对前归一化（scheme/host 小写、去尾斜杠、收敛默认端口）。
5. 接口输出：`/repos/managed` 的 `webhook_ok` = state in (ok, unknown)（存量行不因新增判据变 false），`webhook_stale` 仅 stale 为 true；
   `_check_host_status` 同口径返回 `webhook_stale`（该状态无更具体的 partial/bot_missing 诊断时才接管），修复回写后自动清空。
6. 前端：hostBanner 增加 stale 横幅（文案 `dv2.host_webhook_stale`，复用 repairHost）；仓库卡三态徽标 + `repair-host` 一键修复后静默刷新列表。

## 关键设计决策
| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 漂移检测方式 | 远程列 hooks 比对 / 落盘地址比对 | 落盘地址比对 | 仪表盘明确要求零远程调用（防 429），落盘地址即注册时事实 |
| 权威地址源 | server-config / DB meta | DB meta（server-config 仅种子） | 运行时可变、避免双源漂移（Issue 诉求 A） |
| 状态表达 | 改 bool 语义 / 增量字段 | 保留 `webhook_ok` + 新增 `webhook_stale`/`webhook_state` | 向后兼容既有消费方 |
| 历史空地址 | 判 stale / 判 unknown | unknown（不误报） | 存量行无落盘地址，判 stale 会造成大面积假告警 |

## Acceptance Criteria
- [x] meta `webhook_url` 改成另一域名 → 列表 `webhook_ok=false` / `webhook_stale=true` / `webhook_state=stale`，
      DashboardView 出现漂移横幅且「重检修复」可点击；修复回写后恢复 ok
- [x] 地址一致 → `webhook_ok=true` / `webhook_stale=false` / `webhook_state=ok`，与实际投递成功一致
- [x] `webhook_id=0` → `absent`，`webhook_ok=false`（既有行为不回归），dashboard 仍为 `webhook_missing`
- [x] 存量空 `webhook_url` → `unknown`（不误报 stale）
- [x] 健康检查接口（`GET /dashboard/{owner}/{name}` 的 `host_status`）同口径反映 `webhook_stale`
- [x] 新增测试落位 `tests/mgmt/`，覆盖一致/漂移/未注册/存量空四态；相关存量测试通过

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| Vue 横幅消费字符串分支（bot_missing/webhook_missing/其余） | Med | 保留 `host_status` 字符串语义，`webhook_stale` 为增量分支；前后端同步适配 |
| `managed_repos` 列不存在时 add_managed_repo 有静默降级 | Med | 迁移先行（DDL + 幂等补列）；地址写入独立 guarded UPDATE，失败仅告警不阻断 |
| 双源配置并存 | Low | 只反转 meta/server-config 优先级并一次性播种，不合并键，`get_server_base_url` 不动 |
| QA 环境仓库凭据缺失（前序多轮 `credential_not_configured`） | Med | 与代码无关；提审 QA 前需为仓库配置可用凭据，否则 QA 无法 clone/fetch |

## Open Questions
- 无。