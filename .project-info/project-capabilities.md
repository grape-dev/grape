# Project Capabilities: grape-dev/grape

> Last updated: 2026-10-19T15:02:22.305659+08:00

## Tech Stack
**Exploration: tech stack dependencies**

Found 17 symbols across 3 files.

**Blast radius — what depends on these (update/verify before editing)**

- `TaskResourceStack` (src/runtime/resource_stack.py:17) — 7 callers in `src/agent/hwcloud_acp.py`, `src/agent/hermes_acp.py`, `src/runtime/agent.py`; tests: `tests/test_health_scorer.py`
- `DependencyError` (src/runtime/dependency_check.py:13) — 1 caller in `src/runtime/dependency_check.py`; ⚠️ no covering tests found
- `validate_task_dependencies` (src/runtime/dependency_check.py:22) — 1 caller in `src/runtime/agent.py`; ⚠️ no covering tests found
- `_CAP_TEMPLATE` (src/project_info/builder.py:58) — 1 caller in `src/project_info/builder.py`; ⚠️ no covering tests found

**Relationships**

**instantiates:**
- stream_execute → TaskResourceStack
- stream_execute → TaskResourceStack
- _execute_task → TaskResourceStack
- test_get_engine_health → TaskResourceStack
- test_is_engine_healthy → TaskResourceStack
- test_filter_healthy_engines → TaskResourceStack
- test_filter_healthy_engines_exception → TaskResourceStack
- validate_task_dependencies → DependencyError

**calls:**
- stream_execute → track_overlay
- track_proc → _track_proc
- stream_execute → track_proc
- stream_execute → track_proc
- untrack_proc → _untrack_proc
- untrack_proc → remove
- _execute_task → track_tmp_pattern
- dispose → _kill_proc
- dispose → _untrack_proc
- dispose → close
- ... and 137 more

**references:**
- _build_capabilities_md → _CAP_TEMPLATE
- _build_capabilities_md → _SECTION_CAP
- list_posts → _CAT_LABELS
- create_post → _CAT_LABELS
- get_post → _CAT_LABELS
- GET /posts → list_posts
- POST /posts → create_post
- GET /posts/{pid} → get_post
- _build_graph_md → _SECTION_CAP

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`src/runtime/dependency_check.py`** — DependencyError(class), validate_task_dependencies(function), __init__(method)

```python
1	"""Dependency injection & pre-execution validation (Cordis inject pattern adapted for Grape).
2	
3	Before Agent execution starts, validate all required resources are available.
4	Fail fast with explicit error messages instead of vague exit=1.
5	"""
6	import logging
7	import os
8	from typing import Optional
9	
10	log = logging.getLogger("devops-bot")
11	
12	
13	class DependencyError(Exception):
14	    """Raised when a required dependency is missing or invalid."""
15	
16	    def __init__(self, missing: list[str]):
17	        self.missing = missing
18	        details = "; ".join(missing)
19	        super().__init__(f"dependency check failed: {details}")
20	
21	
22	def validate_task_dependencies(task: dict, engine_name: str = "") -> None:
23	    """Pre-execution dependency validation (inject pattern).

...(已截断，完整内容用 `grape knowledge detail` 获取)

## Supported Features
**Exploration: features capabilities**

Found 23 symbols across 2 files.

**Blast radius — what depends on these (update/verify before editing)**

- `get_featured_order` (src/featured_order.py:74) — 4 callers in `src/api/routes/skills.py`, `src/api/routes/scenes.py`, `src/user/db.py`; ⚠️ no covering tests found
- `_build_capabilities_md` (src/project_info/builder.py:404) — 1 caller in `src/project_info/builder.py`; ⚠️ no covering tests found
- `_analyze_image_features` (src/agent/image_understanding.py:37) — 2 callers in `src/agent/image_understanding.py`; ⚠️ no covering tests found
- `_init_skill_featured` (src/api/db_setup.py:2064) — 1 caller in `src/api/db_setup.py`; ⚠️ no covering tests found
- `_init_featured_scenes_if_empty` (src/storage/scene_sync.py:280) — 1 caller in `src/storage/scene_sync.py`; ⚠️ no covering tests found

**Relationships**

**calls:**
- get_featured_order → _get_conn
- get_featured_order → fetchall
- get_featured_order → cursor
- list_featured_skills → get_featured_order
- list_scenes_market → get_featured_order
- list_scene_market → get_featured_order
- get_featured_skills → get_featured_order
- _get_conn → _admin_db_config
- fetchall → fetchall
- _query_task_stats → fetchall
- ... and 293 more

**references:**
- get_featured_order → _lock
- _build_capabilities_md → _SECTION_CAP
- _build_capabilities_md → _CAP_TEMPLATE
- understand_video → _VIDEO_MAX_SIZE
- GET /featured → list_featured_skills
- test_already_on_target_branch → git_src
- test_switch_to_existing_branch → git_src
- test_create_new_branch_when_not_exists → git_src
- _build_graph_md → _SECTION_CAP

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`src/user/db.py`** — cursor(calls), get_meta_value(function), fetchone(calls), set_meta_value(function), commit(calls), list_skill_categories(function), batch_skill_scenes(function), get_featured_skills(function), get_featured_order(calls)

```python
2080	    return list(set(r for r in result if r))
2081	
2082	
2083	def get_meta_value(key: str) -> str:
2084	    """从 meta 表读取配置值"""
2085	    with db_lock:
2086	        c = db.cursor()
2087	        try:
2088	            c.execute("SELECT value FROM meta WHERE key=?", (key,))
2089	            row = c.fetchone()
2090	            return row[0] if row else ""
2091	        except Exception:
2092	            return ""
2093	
2094	
2095	def set_meta_value(key: str, value: str):
2096	    """写入配置值到 meta 表"""
2097	    with db_lock:
2098	        c = db.cursor()
2099	        try:
2100	            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))
2101	            db.commit()
2102	        except Exception as e:
2103	            log.warning(f"set_meta_value failed: {key}: {
...(已截断，完整内容用 `grape knowledge detail` 获取)

## API Endpoints
**Exploration: API endpoints**

Found 17 symbols across 2 files.

**Blast radius — what depends on these (update/verify before editing)**

- `API` (src/api/static/js/api.js:47) — 1 caller in `src/api/static/js/api.js`; ⚠️ no covering tests found
- `api` (src/api/static/js/admin.js:33) — 22 callers in `src/api/static/js/admin.js`; ⚠️ no covering tests found
- `a2a_endpoint` (src/api/routes/webhook.py:651) — 3 callers in `src/api/routes/events.py`, `src/api/routes/issues.py`, `src/api/routes/webhook.py`; ⚠️ no covering tests found
- `VcsApiClient` (src/vcs/api_client.py:14) — 14 callers in `src/api/git_repo_api.py`, `src/api/routes/issues.py`, `src/vcs/api_client.py`; tests: `tests/test_issue_209_close_vcs.py`, `tests/test_linked_repos.py`

**Relationships**

**references:**
- _redirectToGitcodeAuth → API
- _try_close_issue_for_repo → _do_close

**calls:**
- handleResp → _redirectToGitcodeAuth
- handleResp → _safeErrJson
- handleResp → handle401
- handleResp → get
- _get → handleResp
- _post → handleResp
- _put → handleResp
- _del → handleResp
- testHandleRespBehavior → handleResp
- api → getToken
- ... and 168 more

**instantiates:**
- manager → BotManager
- test_load_from_db_loads_all_users → BotManager
- test_load_from_db_dedup_same_app_id_two_users → BotManager
- test_load_from_db_empty → BotManager
- lifespan → BotManager
- test_vcs_api_client_class_intact → VcsApiClient
- close_issue → VcsApiClient
- _do_close → VcsApiClient
- _try_close_issue_for_repo → VcsApiClient
- create_pr_for_repo → VcsApiClient
- ... and 5 more

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`src/api/static/js/pages/settings.js`** — _escApi(calls), loadApiKeys(calls), loadApiKeys(function), _escApi(function), showCreateApiKeyDialog(function), revokeApiKey(function)

```javascript
198	
199	// ── Issue #69: API Keys 管理 ──
200	
201	function loadApiKeys() {
202	  API.get('/settings/api-keys').then(d => {
203	    const keys = d.keys || [];
204	    const isZh = I18N.getLang() === 'zh';
205	    const container = document.getElementById('sApiKeys');
206	    if (!container) return;
207	
208	    let rowsHtml = '';
209	    if (keys.length === 0) {
210	      rowsHtml = `<div class="tx t2" style="padding:16px;text-align:center">${isZh ? '暂无 API Key，点击上方按钮生成' : 'No API Keys yet. Create one above.'}</div>`;
211	    } else {
212	      rowsHtml = keys.map(k => `
213	        <div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--b)">
214	          <div style="flex:1;min-width:0">
215	            <div style="font-size:12px;font-weight:600;color:var(--t)">${_escApi(k.name)}</div>
216	            <div style="font-size:11px;color:var(--t3);font-family:monospace;mar
...(已截断，完整内容用 `grape knowledge detail` 获取)

## External Integrations
**Exploration: external integrations third-party sdk api webhook oauth sso**

Found 51 symbols across 3 files.

**Blast radius — what depends on these (update/verify before editing)**

- `oauth_associate` (src/api/routes/grape_auth.py:1911) — 1 caller in `src/api/routes/grape_auth.py`; ⚠️ no covering tests found

**Relationships**

**calls:**
- oauth_associate → time
- oauth_associate → get_grape_account_by_email
- oauth_associate → verify_grape_account
- oauth_associate → get_external_identity
- oauth_associate → get_external_identity_any_status
- oauth_associate → save_binding_ticket
- oauth_associate → _get_state_access_token
- oauth_associate → create_external_identity
- oauth_associate → _clear_user_repos_cache
- oauth_associate → _issue_jwt
- ... and 113 more

**references:**
- POST /login/{provider}/associate → oauth_associate
- POST /login/{provider}/associate/send-code → oauth_associate_send_code
- GET /login/{provider} → oauth_login

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`src/api/routes/grape_auth.py`** — _get_client_ip(calls), _issue_jwt(calls), _set_refresh_cookie(calls), get_grape_account_by_email(calls), exchange_token(function), check_oauth_rate_limit(calls), calls(calls), OAuthAssociateSendCodeBody(class), POST /login/{provider}/associate/send-code(route), oauth_associate_send_code(references), +14 more

```python
1823	
1824	
1825	@router.post("/exchange")
1826	def exchange_token(body: ExchangeCodeBody, request: Request, response: Response):
1827	    """P0-3: 用一次性 exchange_code 换取 JWT access_token。
1828	
1829	    exchange_code 由 OAuth 回调生成，10 秒过期，一次性消费（查+删）。
1830	    防止 access_token 泄露到 URL/日志/Referer。
1831	    """
1832	    # supp-2: IP 限流
1833	    ip = _get_client_ip(request)
1834	    if not login_throttle.check_oauth_rate_limit(ip, login_throttle.OAUTH_EXCHANGE_LIMIT, login_throttle.OAUTH_EXCHANGE_WINDOW):
1835	        raise HTTPException(status_code=429, detail={"code": 429, "message": "请求过于频繁，请稍后再试"})
1836	
1837	    from src.user import db as user_db
1838	    data = user_db.validate_exchange_code(body.code)
1839	    if not data:
1840	        raise HTTPException(status_code=401, detail={"code": 401, "message": "exchange_code_invalid"})
1841	
1842	    # 验证账号仍然有效
1843	    account = get_grape_account_by_id(data["user_id"])
1844	    if not account or account.get("status", 1) != 1:
1845	        raise HTTPException(status_code=401, detail={"code": 401, "message": "账号不存在或已被禁用"})
1846	
1847	    # 校验 token_version（exchange code 存储时的版本与当前一致）
1848	    current_ver = account.get("token_version", 0)
1849	    if data["token_version"] != current_ver:
1850	        raise HTTPException(status_code=401, detail={"code": 401, "message": "会话已失效"})
1851	
1852	
...(已截断，完整内容用 `grape knowledge detail` 获取)

## Skills & Agents
### skills/
ℹ No files found matching the criteria.

### agents/
ℹ No files found matching the criteria.

### workflows/
ℹ No files found matching the criteria.

## Data Model
**Exploration: data model entities database tables orm schema migration models**

Found 30 symbols across 2 files.

**Blast radius — what depends on these (update/verify before editing)**

- `_migrate_tables` (src/api/db_setup.py:3220) — 1 caller in `src/api/db_setup.py`; ⚠️ no covering tests found
- `_migrate_repo_key_tables` (src/user/db.py:621) — 1 caller in `src/user/db.py`; ⚠️ no covering tests found
- `table` (src/api/static/js/pages/observability.js:349) — 2 callers in `src/api/static/js/pages/observability.js`; ⚠️ no covering tests found

**Relationships**

**calls:**
- _migrate_scene_table_to_repo_model → _cols_old_scene
- _migrate_scene_table_to_repo_model → cursor
- _migrate_scene_table_to_repo_model → fetchall
- _migrate_scene_table_to_repo_model → add
- _migrate_scene_table_to_repo_model → _insert_repo_scene
- _migrate_scene_table_to_repo_model → _strip_repo_prefix
- _migrate_scene_table_to_repo_model → commit
- _cols_old_scene → cursor
- _cols_old_scene → _scene_table_columns
- _update_scene_engine_ids → cursor
- ... and 180 more

**references:**
- _migrate_repo_key_tables → _USER_REPOS_DDL
- _migrate_repo_key_tables → _MANAGED_REPOS_DDL
- renderAnomalyTab → anomalyPageSize
- renderAnomalyTab → _anomalyPage
- renderOverview → _workerCardHTML
- init_db → _MANAGED_REPOS_DDL

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`scripts/migrate_user_accounts_to_grape.py`** — cursor(calls), _ensure_user_credentials(calls), commit(calls), _remap_collisions(calls), calls(calls), _ensure_migrated_column(function), _repair_missing_credentials(function), fetchone(calls), _ensure_external_identity(calls), run_migration(function), +9 more

```python
836	
837	# ── DDL ────────────────────────────────────────────────────────────────
838	
839	def _ensure_migrated_column():
840	    """确保 user_accounts 表有 migrated 列（幂等）。"""
841	    with db_lock:
842	        c = db.cursor()
843	        try:
844	            c.execute("ALTER TABLE user_accounts ADD COLUMN migrated INTEGER DEFAULT 0")
845	            db.commit()
846	            print("[DDL] user_accounts.migrated 列已添加")
847	        except Exception:
848	            pass
849	
850	
851	def _repair_missing_credentials(dry_run: bool) -> None:
852	    """BUG-M1 修复：补迁移已标记 migrated=1 但缺失 user_credentials 的用户。
853	
854	    早期迁移脚本可能因异常被吞导致华为云凭据等未写入 user_credentials 表，
855	    但 migrated=1 已被标记。此函数扫描这类用户并补迁移。
856	    """
857	    with db_lock:
858	        c = db.cursor()
859	        c.execute(
860	            "SELECT ua.id, ua.username, ua.huawei_cloud_ak, ua.huawei_cloud_sk, "
861	            "ua.CODEARTS_CLI_AK, ua.CODEARTS_CLI_SK, "
862	            "ua.feishu_app_id, ua.feishu_app_secret, "
863	            "ua.github_id, ua.github_ap
...(已截断，完整内容用 `grape knowledge detail` 获取)

## Configuration
**Exploration: configuration settings env vars feature flags config**

Found 46 symbols across 5 files.

**Blast radius — what depends on these (update/verify before editing)**

- `renderSettings` (src/api/static/js/pages/settings.js:1) — 7 callers in `src/api/static/js/pages/settings.js`, `src/api/static/js/app.js`, `src/api/static/js/i18n.js`; ⚠️ no covering tests found

**Relationships**

**calls:**
- selectTheme → renderSettings
- selectThemeMode → renderSettings
- togglePipelineModeSetting → renderSettings
- nav → renderSettings
- doSetPassword → renderSettings
- doChangeEmail → renderSettings
- _rerenderCurrentPage → renderSettings
- nav → _obExitMode
- nav → currentRepo
- nav → showToast
- ... and 92 more

**references:**
- renderRepoSelection → _projectsContent
- renderRepoSelection → _projectSearchQuery
- renderRepoSelection → _projectsRepos
- _isCredPlaceholder → _CRED_PLACEHOLDERS
- _SECURITY_CONFIG_PATH → CONFIG_DIR
- _load → _SECURITY_CONFIG_PATH
- LOGIN_WHITELIST_PATH → CONFIG_DIR
- SERVER_CONFIG_PATH → CONFIG_DIR
- resolve_official_skill_path → CONFIG_DIR
- is_user_allowed → LOGIN_WHITELIST_PATH
- ... and 7 more

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`src/api/routes/settings.py`** — GitCodeRepoConfig(class), OAuthConfig(class), GitHubOAuthConfig(class)

```python
466	    }
467	
468	
469	class GitCodeRepoConfig(BaseModel):
470	    bot_token: str = ""
471	    bot_username: str = "devops-bot"
472	
473	
474	@router.put("/gitcode-repo/{owner}/{repo}", operation_id="settings_gitcode_repo_update", summary="Update repo GitCode config", description="Updates GitCode config for a specific repo.")

... (gap) ...

495	    }
496	
497	
498	class OAuthConfig(BaseModel):
499	    gitcode_oauth_client_id: str = ""
500	    gitcode_oauth_client_secret: str = ""
501	    gitcode_oauth_redirect_uri: str = ""
502	
503	
504	@router.put("/oauth", operation_id="settings_oauth_update", summary="Update GitCode OAuth config", description="Updates the GitCode OAuth client config.")

... (gap) ...

513	    return {"message": "OAuth 配置已更新"}
514	
515	
516	class GitHubOAuthConfig(BaseModel):
517	    github_oauth_client_id: str = ""
518	    github_oauth_client_secret: str = ""
519	    github_oauth_redirect_uri: str = ""
520	
521	
522	@router.get("/oauth-github", operation_id="settings_oauth_github_get", summary="Get GitHub OAuth config", description="Returns GitHub OAuth configuration status (secret masked).")
```

**`src/api/static/js/pages/settings.js`** — renderSettings(calls), renderSettings(function), _CRED_PLACEHOLDERS(constant), _isCredPlaceholder(function), saveGitcodeRepoConfig(function), _isCredPlaceholder(calls), unhostFromSettings(function), renderRepoSelection(cal
...(已截断，完整内容用 `grape knowledge detail` 获取)
