# Project Graph: grape-dev/grape

> Last updated: 2026-10-19T15:02:22.305659+08:00 | Branches: main, weekly-08, weekly-07-clean, weekly-07, weekly-06, weekly-05, weekly-05-lyz, weekly-04, weekly-03-hc, weekly-03, weekly-02, weekly-01, weekly-07-clean-mys, feat/issue-369, feat/issue-370, weekly-08-ljt, local-liufei, weekly-08-yaw, feat/issue-356-auth-ux-cleanup, docs/failure-analysis-20261019, weekly-07-lyz, feat/issue-356-setup-wizard, weekly-07-lyz-oss-backup, docs/failure-analysis-20261018, fix/hermes-premature-end-creds-lock-lease, fix/legacy-user-id-collision, feat/issue-366, lvjuntao-clean, weekly-07-xhy, weekly-07-yaw, main-opensource, weekly-06-lyz, weekly-05-opensource, weekly-yaw, weekly-04-lyz, weekly-03-lyz, feat/issue-171-v2, feat/issue-165, feat/issue-167, weekly-02-lyz, weekly-02-mys, weekly-02-xhy, new-version3 | Commit: 00835d2e0202f0bb4161a2d12a0f13b5a6d635a1

## Architecture Overview
**Exploration: main entry point application startup**

Found 17 symbols across 1 file.

**Blast radius — what depends on these (update/verify before editing)**

- `main` (scripts/migrate_chat_cwd.py:25) — 1 caller in `scripts/migrate_chat_cwd.py`; ⚠️ no covering tests found
- `main` (scripts/release_admin_pack.py:255) — 1 caller in `scripts/release_admin_pack.py`; ⚠️ no covering tests found
- `main` (scripts/migrate_user_accounts_to_grape.py:1019) — 1 caller in `scripts/migrate_user_accounts_to_grape.py`; ⚠️ no covering tests found
- `main` (src/runtime/agent.py:2670) — 1 caller in `src/runtime/agent.py`; ⚠️ no covering tests found
- `main` (scripts/release_pack.py:543) — 1 caller in `scripts/release_pack.py`; ⚠️ no covering tests found

**Relationships**

**calls:**
- main → parse_repo_key
- main → fetchall
- main → execute
- main → commit
- _sync_baseline_files → parse_repo_key
- _execute_vod_agent → parse_repo_key
- __init__ → parse_repo_key
- workspace_repo_dir → parse_repo_key
- knowledge_obs_prefix → parse_repo_key
- clone_url_for → parse_repo_key
- ... and 298 more

**references:**
- main → DRY_RUN
- main → NEW_BASE
- main → OPCODE_DB
- main → GRAPE_DB
- main → PREFIX
- main → OUT_DIR
- main → ADMIN_ROOT
- build_admin → ADMIN_ROOT
- pack_frontend → ADMIN_ROOT
- pack_frontend → OUT_DIR
- ... and 23 more

**instantiates:**
- _amain → RuntimeClient

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`scripts/release_pack.py`** — make_subtar_gz(calls), fetchone(calls), _sql_escape_str(calls), sha256_file(calls), imports(imports), ROOT(variable), OUT_DIR(variable), PREFIX(variable), SKILLS_REPO(variable), TEMPLATE_REPO(variable), +19 more

```python
15	  - skills 已独立项目 (grape-dev/grape-skills)，打包时从独立仓库读取
16	  - examples/templates 已独立项目 (grape-dev/grape-template)，打包时从独立仓库读取
17	"""
18	import hashlib, os, subprocess, sys, tarfile, io, shutil, datetime, gzip, json
19	
20	ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
21	OUT_DIR = "/root/release"
22	BUCKET = "obs-grape-release"
23	PREFIX = "release"
24	
25	# 独立项目路径（可通过环境变量覆盖）
26	SKILLS_REPO = os.environ.get("GRAPE_SKILLS_ROOT", "/opt/grape-skills")
27	TEMPLATE_REPO = os.environ.get("GRAPE_TEMPLATE_ROOT", "/opt/grape-template")
28	
29	# ---- 工具函数 ----
30	
31	def _sql_escape_str(v):
32	    """SQL 字符串字面量转义（导出种子 INSERT 用）：反斜杠与单引号。
33	
34	    库内 value 含单引号/反斜杠时，未转义的 f-string 拼接会破坏导出 SQL
35	    语法（QA 审查 Required 发现）；所有 INSERT 值生成统一复用本函数。
36	    """
37	    return str(v).replace("\\", "\\\\").replace("'", "\\'")
38	
39	def sha256_file(path):
40	    h = hashlib.sha256()
41	    with open(path, "rb") as f:
42	        for chunk in iter(lambda: f.read(65536), b""):
43	            h.update(chunk)
44	    return h.he
...(已截断，完整内容用 `grape knowledge detail` 获取)

## Module Dependency Graph
**Exploration: module import dependency**

Found 17 symbols across 4 files.

**Blast radius — what depends on these (update/verify before editing)**

- `DependencyError` (src/runtime/dependency_check.py:13) — 1 caller in `src/runtime/dependency_check.py`; ⚠️ no covering tests found
- `_MODULES` (src/vcs/providers/__init__.py:11) — 2 callers in `src/vcs/providers/__init__.py`; ⚠️ no covering tests found

**Relationships**

**instantiates:**
- validate_task_dependencies → DependencyError

**calls:**
- __init__ → __init__
- _execute_task → validate_task_dependencies
- _execute_task → _remap_cwd
- _execute_task → track_tmp_pattern
- _execute_task → dispose
- _execute_task → get_task_env
- _execute_task → get
- _execute_task → _worker_server_url_reachable
- _execute_task → issue_num_from
- _execute_task → is_git_agent
- ... and 79 more

**references:**
- translate_inbound → _MODULES
- translate_query_state → _MODULES
- setup_module → _cat_cache_snapshot
- teardown_module → _cat_cache_snapshot

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`src/runtime/dependency_check.py`** — DependencyError(class), __init__(method), validate_task_dependencies(function)

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
24	
25	    Checks:
26	    1. Prompt is non-empty (the most common failure cause)
27	    2. Skill is available (skill_obs_url or skill_files or local skill_dir)
28	    3. cwd exists or can be created
29	    4. Repo token is present for git agents
30	    5. Issue session is valid for git worktree agents
31	
32	    Raises DependencyError with a list of missing items if any check fails.
33	    """
34	    missing: list[str] = []
35	    tid = task.get("id", "")
36	    agent_type = task.get("agent_type") or ""
37	
38	    # 1. Prompt
39	    prompt = task.get("prompt") or ""
40	    if not prompt or not prompt.strip():
41	        missing.append(f"prompt is empty (task={tid})")
42	
43	    # 2. Skill availability - at least 
...(已截断，完整内容用 `grape knowledge detail` 获取)

## Core Call Chains
**Exploration: API routes endpoints handlers**

Found 37 symbols across 4 files.

**Blast radius — what depends on these (update/verify before editing)**

- `handler` (src/api/static/js/pages/account.js:600) — 4 callers in `src/api/static/js/pages/account.js`; ⚠️ no covering tests found
- `handleResp` (src/api/static/js/api.js:194) — 5 callers in `src/api/static/js/api.js`; tests: `tests/js/test_issue_297_multi_image_error_and_persist.js`
- `API` (src/api/static/js/api.js:47) — 1 caller in `src/api/static/js/api.js`; ⚠️ no covering tests found
- `api` (src/api/static/js/admin.js:33) — 22 callers in `src/api/static/js/admin.js`; ⚠️ no covering tests found

**Relationships**

**calls:**
- handler → cleanup
- handler → _accountConfirmBindSuccess
- handler → _accountFetchTransferContext
- handler → _accountCloseModal
- handler → _accountTranslateOAuthError
- _accountDoOauthBind → _isMobile
- _accountDoOauthBind → _oauthOpenCenteredPopup
- _accountDoOauthBind → cleanup
- _accountDoOauthBind → _accountConfirmBindSuccess
- _accountDoOauthBind → _accountCloseModal
- ... and 127 more

**references:**
- cleanup → handler
- _accountDoOauthBind → handler
- cleanup → handler
- _accountDaOauth → handler
- _accountDoOauthBind → _accountProfile
- _accountConfirmBindSuccess → _accountProfile
- _accountCloseModal → _accountModalEl
- _redirectToGitcodeAuth → API
- _accountShowResetPassword → _accountProfile
- _accountDoResetPassword → _accountProfile
- ... and 1 more

**instantiates:**
- manager → BotManager
- test_load_from_db_loads_all_users → BotManager
- test_load_from_db_dedup_same_app_id_two_users → BotManager
- test_load_from_db_empty → BotManager
- lifespan → BotManager

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`src/api/routes/health.py`** — router(variable)

```python
1	"""系统健康检查端点——免鉴权基本检查。
2	
3	供 K8s livenessProbe / nginx 健康检查 / Docker healthcheck 使用。
4	不查数据库、不暴露系统规模。
5	"""
6	from fastapi import APIRouter
7	
8	router = APIRouter()
9	
10	
11	@router.get("/api/health")
12	def health():
13	    """免鉴权健康端点——只返回服务是否在线。
14	
15	    server_base_url（2026-10-15）：CLI/worker 可达的服务端权威基址（meta.server_base_url，
16	    无则 server-config.json 兜底）。前端 devspace/设备接入用它替代 window.location.origin
17	    （用户浏览地址 ≠ CLI 可达地址，如生产 www.grapedev.cn vs worker.grapedev.cn）。
18	    仅供前端构造安装命令，不含敏感信息；查询失败返回空串（前端回退 origin）。
19	    """
20	    base = ""
21	    try:
22	        from src.user.db import get_server_base_url
23	        base = get_server_base_url() or ""
24	    except Exception:
25	        base = ""
26	    return {"server": "ok", "server_base_url": base}
```

**`src/api/routes/webhook.py`** — router(variable)

```python
7	from src.api.deps import get_user_or_none
8	from src.api.event_pipeline imp
...(已截断，完整内容用 `grape knowledge detail` 获取)

## Background Work & Scheduling
**Exploration: background jobs scheduled tasks cron workers queue**

Found 26 symbols across 3 files.

**Blast radius — what depends on these (update/verify before editing)**

- `WorkerReportQueue` (src/runtime/worker_report_queue.py:32) — 1 caller in `src/runtime/agent.py`; ⚠️ no covering tests found
- `scheduler_loop` (src/runtime/scheduler.py:755) — 1 caller in `src/runtime/scheduler.py`; ⚠️ no covering tests found
- `_check_stale_queued_tasks` (src/runtime/service.py:2682) — 1 caller in `src/runtime/service.py`; ⚠️ no covering tests found
- `worker_code_version` (src/worker_package.py:347) — 4 callers in `src/api/routes/runtime.py`, `src/api/routes/settings.py`; ⚠️ no covering tests found
- `start_scheduler` (src/runtime/scheduler.py:859) — 1 caller in `src/api/main.py`; ⚠️ no covering tests found

**Relationships**

**instantiates:**
- _report_queue → WorkerReportQueue

**calls:**
- __init__ → _default_path
- __init__ → _load
- _load → exists
- add → _flush
- remove → _flush
- prune_expired → _flush
- add → time
- add → get
- _track_proc → add
- _migrate_scene_table_to_repo_model → add
- ... and 206 more

**references:**
- __init__ → _CAP
- __init__ → _TTL_SECONDS
- scheduler_loop → _SCHEDULER_INTERVAL
- scheduler_loop → _RUNNING_TASKS
- _check_stale_queued_tasks → _STALE_QUEUED_THRESHOLD
- worker_code_version → _PACKAGE_VERSION_TTL
- POST /heartbeat → runtime_heartbeat
- GET /install/latest → runtime_install_latest
- lifespan → _on_message
- lifespan → _on_migrate_done
- ... and 4 more

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`src/runtime/scheduler.py`** — calls(calls), cursor(calls), fetchone(calls), _update_execution_result(calls), sleep(calls), _execute_task(function), add(calls), references(references), scheduler_loop(function), run_with_distributed_lock(calls), +7 more

```python
689	    return "failed"
690	
691	
692	async def _execute_task(task: dict):
693	    """执行单个 autopilot 任务（含重试逻辑）。"""
694	    task_id = task["id"]
695	    if task_id in _RUNNING_TASKS:
696	        log.info(f"Autopilot: task {task_id} already running, skip")
697	        return
698	    _RUNNING_TASKS.add(task_id)
699	    # 触发即写 last_run_at/next_run_at（防执行窗口内主控重启导致 cron 重新判定到期：
700	    # _is_cron_due 用 last_run_at 推算，若只在任务结束后写，执行期间的 20 分钟窗口内
701	    # 服务重启会丢失 _RUNNING_TASKS 内存互斥 → 同一 cron 被再次触发 → 自触发循环。
702	    # 2026-09-28 实际发生：8 次重启 + 17 个僵尸 execution + 生产被误停一轮）
703	    try:
704	        _early_next = _compute_next_run(task["cron"])
705	        _update_task_run_info(task_id, _early_next)
706	    except Exception as _e:
707	        log.warning(f"Autopilot: task {task_id} early next_run write failed: {_e}")
708	    try:
709	        max_retry = task["max_retry"]
710	        agen
...(已截断，完整内容用 `grape knowledge detail` 获取)

## Error Handling & Observability
**Exploration: error handling logging observability middleware**

Found 31 symbols across 7 files.

**Blast radius — what depends on these (update/verify before editing)**

- `handler` (src/api/static/js/pages/account.js:600) — 4 callers in `src/api/static/js/pages/account.js`; ⚠️ no covering tests found

**Relationships**

**calls:**
- handler → cleanup
- handler → _accountConfirmBindSuccess
- handler → _accountFetchTransferContext
- handler → _accountCloseModal
- handler → _accountTranslateOAuthError
- _accountDoOauthBind → _isMobile
- _accountDoOauthBind → _oauthOpenCenteredPopup
- _accountDoOauthBind → cleanup
- _accountDoOauthBind → _accountConfirmBindSuccess
- _accountDoOauthBind → _accountCloseModal
- ... and 27 more

**references:**
- cleanup → handler
- _accountDoOauthBind → handler
- cleanup → handler
- _accountDaOauth → handler
- _accountDoOauthBind → _accountProfile
- _accountConfirmBindSuccess → _accountProfile
- _accountCloseModal → _accountModalEl
- _accountShowResetPassword → _accountProfile
- _accountDoResetPassword → _accountProfile
- _accountShowModal → _accountModalEl

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`src/api/static/js/pages/account.js`** — cleanup(calls), _accountCloseModal(calls), handler(references), _accountProfile(variable), _accountDaOauth(function), _oauthOpenCenteredPopup(calls), cleanup(function), handler(function), _accountDoDeleteAccount(function), calls(calls), +4 more

```javascript
1	/* Grape User Settings — 内嵌 SPA view 逻辑（全屏渲染到 mainView） */
2	/* renderAccount(tab) 由 app.nav('account') 调用，隐藏工作台侧边栏，自带顶栏 */
3	
4	var _accountProfile = null;
5	var _accountCurrentTab = 'profile';
6	
7	function renderAccount(tab) {

... (gap) ...

1273	  else if (method === 'oauth') document.getElementById('daStepOauth').style.display = 'flex';
1274	}
1275	
1276	function _accountDaOauth(provider) {
1277	  // OAuth 重新授权注销——popup 模式
1278	  var popup = _oauthOpenCenteredPopup('');
1279	  if (!popup) {
1280	    app.showToast(I18N.t('app.account.popup_blocked_hint') || I18N.t('app.account.error_failed'));
1281	    return;
1282	  }
1283	  var overlay = document.getElementById('accountModal');
1284	  if (overlay) overlay.dataset.locked = '1';
1285	
1286	  API.get('/auth/delete-account/oauth/' + provider + '?popup=true&prompt=login').then(function(d) {
1287	    if (!d.redirect_url) throw new Error('no_redirect_url');
1288	    popup.location.href = d.redirect_url;
1289	
1290	    var handled = false;
1291	    var timeoutId = null;
1292	
1293	    function cleanup() {
1294	      if (timeoutId) { clearTimeout(timeoutId); timeoutId = null; }
1295	      if (pollId) { clearInterval(pollId); pollId = null; }
1296	      window.removeEventListener('message'
...(已截断，完整内容用 `grape knowledge detail` 获取)

## Auth & Security
**Exploration: authentication authorization security guard middleware**

Found 42 symbols across 6 files.

**Blast radius — what depends on these (update/verify before editing)**

- `guard_issue_session_pointer` (src/api/session_store.py:668) — 8 callers in `src/api/event_pipeline.py`; tests: `tests/test_session_engine_guard.py`
- `_SECURITY_CONFIG_PATH` (src/auth/security_config.py:22) — 1 caller in `src/auth/security_config.py`; ⚠️ no covering tests found
- `metrics_middleware` (src/observability/__init__.py:72) — 1 caller in `src/api/main.py`; ⚠️ no covering tests found

**Relationships**

**calls:**
- guard_issue_session_pointer → get
- test_same_engine_passthrough → guard_issue_session_pointer
- test_cross_engine_dropped → guard_issue_session_pointer
- test_unknown_pointer_engine_passthrough → guard_issue_session_pointer
- test_unknown_current_engine_passthrough → guard_issue_session_pointer
- test_empty_pointer_returns_empty → guard_issue_session_pointer
- test_none_isess_tolerated → guard_issue_session_pointer
- _enqueue_pipeline_task → guard_issue_session_pointer
- _execute_agent_inner → guard_issue_session_pointer
- _format_report → get
- ... and 197 more

**references:**
- _SECURITY_CONFIG_PATH → CONFIG_DIR
- _load → _SECURITY_CONFIG_PATH
- metrics_middleware → HTTP_REQUEST_DURATION
- metrics_middleware → HTTP_REQUESTS_TOTAL
- metrics_middleware → HTTP_IN_FLIGHT
- _record_http_metrics → HTTP_REQUEST_DURATION
- _record_http_metrics → HTTP_REQUESTS_TOTAL
- _record_http_metrics → HTTP_IN_FLIGHT
- is_health_probe_path → _HEALTH_PATHS
- request_id_and_log_middleware → _LOG_SKIP_PREFIXES
- ... and 3 more

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`src/api/main.py`** — imports(imports), calls(calls), NoCacheStaticMiddleware(class), dispatch(method)

```python
778	app.include_router(auth.router)
779	app.include_router(repos.router)
780	# Admin 管理后台（/api/admin/*）
781	from src.admin.routes import router as admin_router
782	app.include_router(admin_router)
783	# Issue-45 M3：Git 仓库统一接口（provider 白名单资源端点；prefix=/api/git-repo，与 grape 托管 repos.py 区分）
784	from src.api.git_repo_api import router as git_repo_router
785	app.include_router(git_repo_router)
786	app.include_router(dashboard.router)
787	app.include_router(agents.router)

... (gap) ...

810	app.include_router(community.router)
811	
812	# ===== Issue-45: OpenAPI 定制渲染（securitySchemes + 仅对外安全路由进契约）=====
813	from src.api.openapi import custom_openapi
814	app.openapi = lambda: custom_openapi(app)
815	# Issue 附件静态服务（Task Center 上传的文件）
816	# 注意：必须在 /static 之前 mount，否则 /static 会拦截 /static/issue_attachments 请求
817	# 路径与 tasks.py _attach_base_dir 对齐（基于 WORKSPACE_DIR）
818	from src.config import WORKSPACE_DIR as _WS_DI
...(已截断，完整内容用 `grape knowledge detail` 获取)

## Deployment & CI-CD
**Dynamic boundaries (the static path ends at runtime dispatch)**

- `build_project_info` (src/project_info/builder.py:431) — dynamic import: `now = __import__("datetime").datetime.now().astimezone().isoformat()`

> These sites choose their call target at runtime (registry / bus / reflection) — the site shown IS where the flow continues. To follow it, run codegraph_explore or codegraph_node on a candidate; source for the sites above is included below.

> Full source for these symbols is below — the call flow among them, followed by their bodies.
**Exploration: deployment build ci cd dockerfile container k8s**

Found 29 symbols across 2 files.

**Blast radius — what depends on these (update/verify before editing)**

- `build_project_info` (src/project_info/builder.py:421) — 14 callers in `src/project_info/builder.py`; tests: `tests/test_project_info_builder.py`
- `build_role_definition` (src/prompt_builder.py:219) — 1 caller in `src/prompt_builder.py`; ⚠️ no covering tests found
- `build_chat_role_definition` (src/prompt_builder.py:362) — 1 caller in `src/prompt_builder.py`; ⚠️ no covering tests found
- `build_issue_role_definition` (src/prompt_builder.py:374) — 1 caller in `src/prompt_builder.py`; ⚠️ no covering tests found
- `build_scene_recommender_prompt` (src/prompt_builder.py:1087) — 1 caller in `src/api/event_pipeline.py`; ⚠️ no covering tests found

**Relationships**

**calls:**
- build_project_info → split_owner_repo
- build_project_info → _read_meta
- build_project_info → _codegraph_engine_version
- build_project_info → _codegraph_available
- build_project_info → _write_meta
- build_project_info → _write_file
- build_project_info → _build_graph_md
- build_project_info → _build_capabilities_md
- build_project_info → _fetch_branches
- build_project_info → _ensure_analysis_worktree
- ... and 164 more

**references:**
- build_general_chat_prompt → _CHAT_PROMPT_SUFFIX
- build_issue_chat_prompt → _CHAT_PROMPT_SUFFIX
- _reference_data → REFERENCE_DIR
- _reference_data → REFERENCE_ORDER
- build_help_html → OPENAPI_STATIC_URL
- build_help_html → _JS_APP
- build_help_html → _CSS
- build_help_html → _MIGRATION_COMMENT
- generate → OUT_PATH
- _docs → API_DIR
- ... and 3 more

**extends:**
- HermesRuntime → AgentRuntime
- HermesRuntime → AgentRuntime
- CodeArtsRuntime → AgentRuntime
- CodexRuntime → AgentRuntime
- OpencodeRuntime → AgentRuntime
- HwcloudRuntime → AgentRuntime
- _SleepBackend → AgentRuntime

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`src/prompt_builder.py`** — get_scenes(calls), calls(calls), get(calls), _vcs_name(calls), _load_scene_recommend_config(function), _sanitize_recommend_config(function), set_scene_recommend_config(calls), build_scene_re
...(已截断，完整内容用 `grape knowledge detail` 获取)

## Branch Structure
- main
- weekly-08
- weekly-07-clean
- weekly-07
- weekly-06
- weekly-05
- weekly-05-lyz
- weekly-04
- weekly-03-hc
- weekly-03
- weekly-02
- weekly-01
- weekly-07-clean-mys
- feat/issue-369
- feat/issue-370
- weekly-08-ljt
- local-liufei
- weekly-08-yaw
- feat/issue-356-auth-ux-cleanup
- docs/failure-analysis-20261019
- weekly-07-lyz
- feat/issue-356-setup-wizard
- weekly-07-lyz-oss-backup
- docs/failure-analysis-20261018
- fix/hermes-premature-end-creds-lock-lease
- fix/legacy-user-id-collision
- feat/issue-366
- lvjuntao-clean
- weekly-07-xhy
- weekly-07-yaw
- ...（共 43 个分支，仅列前 30）

## Key Files
Project Structure (508 files):

├── deploy
│   └── alerts.yml (yaml, 0 symbols)
├── docs
│   └── new-version1
│       └── deploy
│           └── verify-local.py (python, 28 symbols)
├── scripts
│   ├── analyze_task_report.py (python, 11 symbols)
│   ├── backfill_event_archive_to_events.py (python, 9 symbols)
│   ├── backfill_issue_user_id.py (python, 10 symbols)
│   ├── backfill_task_center_issues.py (python, 5 symbols)
│   ├── check-css-vars.py (python, 11 symbols)
│   ├── check-health-contract.py (python, 7 symbols)
│   ├── check-i18n-keys.py (python, 18 symbols)
│   ├── codex_opengw_proxy.py (python, 11 symbols)
│   ├── converge_skill_versions.py (python, 7 symbols)
│   ├── export_mysql_to_sqlite.py (python, 8 symbols)
│   ├── fix_chat_config_local_direct.py (python, 19 symbols)
│   ├── fix_zombie_binding_62_63.py (python, 12 symbols)
│   ├── migrate_chat_cwd.py (python, 9 symbols)
│   ├── migrate_skills_to_obs.py (python, 14 symbols)
│   ├── migrate_user_accounts_to_grape.py (python, 30 symbols)
│   ├── regenerate_openapi.py (python, 8 symbols)
│   ├── release_admin_pack.py (python, 23 symbols)
│   ├── release_pack.py (python, 29 symbols)
│   └── replay_failed_engine_events.py (python, 15 symbols)
├── src
│   ├── admin
│   │   ├── __init__.py (python, 1 symbols)
│   │   ├── config_store.py (python, 15 symbols)
│   │   ├── cooldown.py (python, 19 symbols)
│   │   ├── db.py (python, 26 symbols)
│   │   └── routes.py (python, 72 symbols)
│   ├── agent
│   │   ├── __init__.py (python, 5 symbols)
│   │   ├── base.py (python, 2 symbols)
│   │   ├── codearts.py (python, 2 symbols)
│   │   ├── codex.py (python, 2 symbols)
│   │   ├── executor.py (python, 46 symbols)
│   │   ├── hermes_acp.py (python, 48 symbols)
│   │   ├── hermes.py (python, 2 symbols)
│   │   ├── hwcloud_acp.py (python, 39 symbols)
│   │   ├── hwcloud.py (python, 2 symbols)
│   │   ├── image_understanding.py (python, 31 symbols)
│   │   ├── opencode.py (python, 2 symbols)
│   │   └── sandbox.py (python, 7 symbols)
│   ├── api
│   │   ├── routes
│   │   │   ├── __init__.py (python, 1 symbols)
│   │   │   ├── agents.py (python, 24 symbols)
│   │   │   ├── auth.py (python, 14 symbols)
│   │   │   ├── autopilot.py (python, 56 symbols)
│   │   │   ├── chat.py (python, 84 symbols)
│   │   │   ├── community.py (python, 33 symbols)
│   │   │   ├── dashboard.py (python, 31 symbols)
│   │   │   ├── devstage.py (python, 29 symbols)
│   │   │   ├── events.py (python, 67 symbols)
│   │   │   ├── feedback.py (python, 26 symbols)
│   │   │   ├── feishu_card.py (python, 10 symbols)
│   │   │   ├── feishu_onboard.py (python, 31 symbols)
│   │   │   ├── grape_auth.py (python, 116 symbols)
│   │   │   ├── health.py (python, 5 symbols)
│   │   │   ├── issues.py (python, 90 symbols)
│   │   │   ├── knowledge.py (python, 58 symbols)
│   │   │   ├── latency.py (python, 13 symbols)
│   │   │   ├── orgs.py (python, 34 symbols)
│   │   │   ├── pipeline.py (python, 17 symbols)
│   │   │   ├── repos.py (python, 80 symbols)
│   │   │   ├── runtime.py (python, 118 symbols)
│   │   │   ├── scenes.py (python, 81 symbols)
│   │   │   ├── search.py (python, 7 symbols)
│   │   │   ├── settings.py (python, 114 symbols)
│   │   │   ├── skills.py (python, 50 symbols)
│   │   │   ├── task_logs.py (python, 17 symbols)
│   │   │   ├── tasks.py (python, 25 symbols)
│   │   │   ├── templates.py (python, 27 symbols)
│   │   │   └── webhook.py (python, 27 symbols)
│   │   ├── static
│   │   │   └── js
│   │   │       ├── pages
│   │   │       ├── admin.js (javascript, 53 symbols)
│   │   │       ├── api.js (javascript, 24 symbols)
│   │   │       ├── app.js (javascript, 115 symbols)
│   │   │       ├── auth.js (javascript, 18 symbols)
│   │   │       ├── auto-recovery.js (javascript, 11 symbols)
│   │   │       └── i18n.js (javascript, 16 symbols)
│   │   ├── __init__.py (python, 1 symbols)
│   │   ├── auth_token.py (python, 22 symbols)
│   │   ├── chat_session_env.py (python, 3 symbols)
│   │   ├── chat_task_manager.py (python, 27 symbols)
│   │   ├── db_setup.py (python, 127 symbols)
│   │   ├── deps.py (python, 38 symbols)
│   │   ├── errors.py (python, 17 symbols)
│   │   ├── event_archive.py (python, 28 symbols)
│   │   ├── event_bus.py (python, 28 symbols)
│   │   ├── event_pipeline.py (python, 69 symbols)
│   │   ├── event_rule.py (python, 14 symbols)
│   │   ├── event_store.py (python, 39 symbols)
│   │   ├── git_repo_api.py (python, 71 symbols)
│   │   ├── issues_center.py (python, 90 symbols)
│   │   ├── jwt_utils.py (python, 25 symbols)
│   │   ├── knowledge.py (python, 48 symbols)
│   │   ├── main.py (python, 86 symbols)
│   │   ├── oauth_token.py (python, 5 symbols)
│   │   ├── onboarding.py (python, 19 symbols)
│   │   ├── openapi.py (python, 16 symbols)
│   │   ├── org_context.py (python, 4 symbols)
│   │   ├── pipeline_config.py (python, 43 symbols)
│   │   ├── pipeline_executor.py (python, 16 symbols)
│   │   ├── pipeline_status.py (python, 10 symbols)
│   │   ├── routing.py (python, 10 symbols)
│   │   ├── run_webhook.py (python, 12 symbols)
│   │   ├── run.py (python, 14 symbols)
│   │   ├── session_store.py (python, 39 symbols)
│   │   └── time_utils.py (python, 5 symbols)
│   ├── auth
│   │   ├── __init__.py (python, 1 symbols)
│   │   ├── audit.py (python, 12 symbols)
│   │   ├── cookie_utils.py (python, 11 symbols)
│   │   ├── credential_crypto.py (python, 13 symbols)
│   │   ├── email_service.py (python, 33 symbols)
│   │   ├── grape_account_db.py (python, 54 symbols)
│   │   ├── login_throttle.py (python, 27 symbols)
│   │   ├── password.py (python, 12 symbols)
│   │   ├── providers.py (python, 33 symbols)
│   │   ├── security_config.py (python, 17 symbols)
│   │   ├── signup_policy.py (python, 7 symbols)
│   │   └── verification.py (python, 29 symbols)
│   ├── im
│   │   ├── __init__.py (python, 1 symbols)
│   │   ├── base.py (python, 7 symbols)
│   │   ├── bot_lease.py (python, 24 symbols)
│   │   ├── devstage_client.py (python, 28 symbols)
│   │   ├── feishu_async.py (python, 78 symbols)
│   │   ├── handler_async.py (python, 71 symbols)
│   │   ├── lark_md.py (python, 11 symbols)
│   │   └── vod_handler_async.py (python, 23 symbols)
│   ├── observability
│   │   ├── __init__.py (python, 66 symbols)
│   │   ├── engine_probe.py (python, 24 symbols)
│   │   └── events.py (python, 14 symbols)
│   ├── project_info
│   │   ├── __init__.py (python, 1 symbols)
│   │   └── builder.py (python, 44 symbols)
│   ├── runtime
│   │   ├── __init__.py (python, 9 symbols)
│   │   ├── acp_bootstrap.py (python, 12 symbols)
│   │   ├── agent.py (python, 90 symbols)
│   │   ├── base.py (python, 7 symbols)
│   │   ├── bot_token_health.py (python, 15 symbols)
│   │   ├── cleanup.py (python, 34 symbols)
│   │   ├── client.py (python, 32 symbols)
│   │   ├── codearts.py (python, 7 symbols)
│   │   ├── codex.py (python, 9 symbols)
│   │   ├── context_compaction.py (python, 13 symbols)
│   │   ├── dependency_check.py (python, 8 symbols)
│   │   ├── engine_select.py (python, 40 symbols)
│   │   ├── executor.py (python, 7 symbols)
│   │   ├── health_scorer.py (python, 40 symbols)
│   │   ├── health.py (python, 6 symbols)
│   │   ├── hermes.py (python, 14 symbols)
│   │   ├── hwcloud.py (python, 6 symbols)
│   │   ├── instances.py (python, 13 symbols)
│   │   ├── local_claimer.py (python, 16 symbols)
│   │   ├── local_probe.py (python, 8 symbols)
│   │   ├── opencode.py (python, 7 symbols)
│   │   ├── os_adapter.py (python, 37 symbols)
│   │   ├── registry.py (python, 3 symbols)
│   │   ├── repogit.py (python, 31 symbols)
│   │   ├── resource_stack.py (python, 19 symbols)
│   │   ├── scheduler.py (python, 39 symbols)
│   │   ├── service.py (python, 80 symbols)
│   │   ├── table_maintenance.py (python, 10 symbols)
│   │   ├── task_lock.py (python, 7 symbols)
│   │   ├── task_log_stream.py (python, 13 symbols)
│   │   └── worker_report_queue.py (python, 19 symbols)
│   ├── storage
│   │   ├── __init__.py (python, 72 symbols)
│   │  
...(已截断，完整内容用 `grape knowledge detail` 获取)
