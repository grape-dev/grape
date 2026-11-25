# Plan: Issue #4241 实现方案

目标基线 `arch-weekly-11-ljt`；开发分支 `feat/issue-4241-ljt`（PR #2267 head）。核心思路：**把「实际注册的 hook 地址」落盘**，托管状态改为
「落盘地址 vs 当前权威 `webhook_url` 前缀比对」，零远程调用即可发现漂移。

## 改动清单

### 1. 数据层 `src/domains/mgmt/user/db.py`
- `_MANAGED_REPOS_DDL` 增列 `webhook_url TEXT DEFAULT ''`（新库直接带全）。
- `legacy_column_migrations._MANAGED_REPOS_LEGACY_COLUMNS` 增 `("webhook_url", "TEXT DEFAULT ''")`（存量库幂等补列，
  SQLite/MySQL 同一入口；表归属 mgmt，非本域进程跳过）。
- `add_managed_repo(..., webhook_url="")`：托写成功后独立 guarded `UPDATE managed_repos SET webhook_url=?`；传空串保留存量值，
  列缺失时仅 `log.warning` 不阻断主写（S74 响亮失败语义不变）。
- `get_webhook_url()`：meta 优先（运行时权威），meta 为空时用 `configs/server-config.json` 作一次性种子（进程内
  `_WEBHOOK_URL_SEEDED` 防重复写），文件降级为种子/兜底；`get_server_base_url()` 不动。
- 新增：`normalize_webhook_address()`、`webhook_state_for_row()`、`get_managed_webhook_url()`、`webhook_state()`。
- `get_managed_repos()` 查询补 `COALESCE(m.webhook_url,'')`（含降级 SQL）并随行返回。
- `_update_repo_bot_token(..., webhook_url="")`：bot token 重建 webhook 成功后回写地址；
  `update_managed_repo_bot_token()` / `auto_upgrade_path_b_repos()` 传入按平台卡派生的回调全路径。

### 2. 接口层 `src/domains/mgmt/routes/repos.py`
- `list_managed_repos`：权威地址取一次 → 逐行 `webhook_state_for_row`；输出 `webhook_ok`（ok/unknown 为 true）、
  `webhook_stale`、`webhook_state`。
- `_filter_repos`（`/repos/member`，仓库大厅/ReposView 数据源）：托管行补 `webhook_state`（零远程前缀比对）。
  **值域契约**：`ok` / `stale` / `absent` / `unknown`，未托管行为 `""`；前端一律按 `'stale'` 比较，
  与 dashboard 的 `host_status='webhook_stale'`（带 `webhook_` 前缀）分属两个字段，不可混用。
- 托管/建仓/修复成功路径回写实际注册地址：`create_new_repo`（复用/新建 hook 两分支）、`host_repo` GitHub 分支、`host_repo`
  其它方言分支（复用/新建两分支）、`repair_host`（`_db_kwargs` 增 `webhook_url`）。

### 3. 仪表盘 `src/domains/insight/routes/dashboard.py`
- `_check_host_status`：`webhook_id>0` 之后加漂移判定——`cur` 非 `partial`/`bot_missing`（不覆盖 #2126 诊断）且
  `webhook_state(repo_key)=="stale"` → 落 `webhook_stale`（step=configure_webhook_url）并返回；
  已修复时走既有收尾分支清空状态。跨域读取走 `mgmt.public.webhook_state`（不直连 managed_repos）。

### 4. 跨域门面 `src/domains/mgmt/public/__init__.py`
- 新增 `webhook_state(repo_key)` 惰性转发到 `mgmt.user.db`。

### 5. 前端
- `shell/locales/{zh,en}/common.json`：`host_status_webhook_stale(_compact)`。
- `shell/locales/{zh,en}/dashboard.json`：`dv2.host_webhook_stale`。
- `shell/locales/{zh,en}/repos.json`：`card.status_webhook_stale` / `card.repair_host(_running|_ok|_failed)`。
- `shell/js/host-status.js`：`webhook_stale` 映射（旧壳页面共用）。
- `app/src/views/DashboardView.vue`：`hostBanner` 增 `webhook_stale` 分支（error 样式 + 复用 `repairHost()`）。
- `app/src/views/ReposView.vue`：`repoStatusClass/repoStatusText` 三态徽标；stale 卡片显示「修复托管」按钮
  （`POST /repos/{owner}/{name}/repair-host`，成功/失败 toast + 静默刷新列表），`repairingRepo` 防重复点击。
- `app/src/styles/global/app.css`：`.project-card-status-stale` / `.project-card-btn-warn` 样式。

### 6. 测试
- 新增 `tests/mgmt/test_issue_4241_webhook_state.py`：列存在、地址落盘与保留、ok/stale/absent/unknown、归一化、
  meta 权威 vs server-config 种子、`/repos/managed` 三态输出、存量行 `webhook_ok` 保持 true、
  `_check_host_status` 漂移上报与修复后自愈、partial 诊断不被漂移覆盖。

### 7. 文档
- `docs/issue-4241/{spec,plan,task}.md`（本目录）。

## 设计取舍
- **不新增远程探测**：漂移判定只用本地事实（落盘地址 + 权威地址），不影响既有「防 429」设计。
- **unknown 不误报**：存量行无落盘地址时维持 `webhook_ok=true`（原语义），避免升级后大面积假告警；下次托管/修复自动回填。
- **地址写入独立语句**：不塞进主 upsert 的列清单，保证未迁移库/列缺失时托管主写与凭据写入不受影响。
- **状态值增量**：`host_status` 仍为字符串枚举，新增 `webhook_stale`；前端按增量分支渲染，不动既有 `bot_missing`/
  `webhook_missing`/`partial` 分支。

## 验证
```bash
python3 -m pytest tests/mgmt/test_issue_4241_webhook_state.py -q
python3 -m pytest tests/mgmt/test_host_status.py tests/mgmt/test_issue_2126_host_diagnostics.py \
  tests/mgmt/test_issue_3354_host_after_create_retry.py tests/mgmt/test_issue_3427_credential_read_errors.py -q
make lint && python3 scripts/check-arch.py
```
已知存量失败（与本改动无关，基线同样失败）：`tests/mgmt/test_s62_repos_hosting_migration.py::test_s62_host_repo_gitcode_parity`、
`::test_s62_host_repo_gitee_provider_ports`、`tests/kernel/test_global_token_swap_r21.py::test_valid_swap_updates_repos`。