# Plan — Issue #543：迁移到其他组织优化

## 方案概述
前端迁移弹窗改造（仓库名只读 + 组织下拉）+ 新增「当前用户 GitCode 组织列表」查询端点，
迁移提交逻辑不变。

## 后端改动

### 1. `src/user/repo_manager.py` — 新增 `fetch_user_orgs(api_token)`
- 调 GitCode V5 API `GET /api/v5/user/orgs`（列表分页，per_page=50）；
- 返回 `[{"login", "name", "avatar_url"}, ...]`；
- 失败抛 `RuntimeError`（与现有 `_req` 一致，由 `with_oauth_token` 统一处理 401/403）。

### 2. `src/api/routes/repos.py` — 新增 `GET /api/repos/migrate-orgs`
- 鉴权：`require_no_session_token`（与 `/migrate` 同口径）；
- 用 `with_oauth_token(user, "gitcode", fetch_user_orgs)` 拉取用户 GitCode 组织；
- 返回 `{"orgs": [{"login", "name", "avatar_url"}, ...]}`；
- 同时返回当前用户个人登录名（`personal`）作为可迁移目标（仓库可迁到个人名下）；
- 拉取失败返回明确错误（401 授权过期 / 502 上游故障）。

## 前端改动（`src/api/static/js/pages/settings.js`）

### 迁移弹窗布局
- 仓库名：`<input readonly>` 只读，置于目标组织选择区上方，value = 当前仓库名；
- 目标组织：`<select id="migrateTargetOwner">` 下拉框（替换原 text input）；
- 弹窗渲染时异步 `API.get('/repos/migrate-orgs')` 填充选项；拉取失败显示提示 + 重试按钮；
- 移除仓库名可编辑入口与目标组织手工输入。

### 提交逻辑（`migrateRepoFromSettings`）
- `target_repo` 固定 = 当前仓库名（不再从可编辑输入取值）；
- `target_owner` = 下拉选中值；
- 沿用 `POST /repos/migrate` + 既有确认对话框/错误提示。

## i18n（`locales/{zh,en}/settings.json`）
新增 key：`migrate_orgs_loading` / `migrate_orgs_load_failed` / `migrate_repo_readonly_hint`。

## 测试（`tests/test_repo_migrate.py` 或新增 `tests/test_repo_migrate_orgs.py`）
- `fetch_user_orgs`：mock `_req` 返回结构校验 + 分页 + 失败抛错；
- `GET /api/repos/migrate-orgs`：未鉴权 401；鉴权后返回结构含 orgs 数组；上游失败 502。
