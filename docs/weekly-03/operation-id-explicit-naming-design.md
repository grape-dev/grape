# WebAPI 显式 operation_id 补齐方案（CLI 命令全量可见）

> 目标：给 156 个**未显式声明 `operation_id`** 的对外端点补显式命名，使契约不再产生 `*_api_*` 自动丑名，
> 从而让 CLI（`parse_operations`）不再丢弃这些命令——管理类端点全部解锁为 `grape <tag> <verb>`。
> 现状分析见 §一；本方案已按 P1–P6 全部实施（2026-09-16）。
> 分支：`weekly-03-lyz`；状态：✅ 已实施；日期：2026-09-16

> **实施记录（2026-09-16）**：17 个路由文件全部补上显式 `operation_id`（+`summary`）；契约重生成
> 175 paths、0 个 `api_` 自动丑名、无重名；`test_openapi.EXPECTED_PUBLIC` 全量同步（deprecated 旧名清零）；
> 特例：`api-keys` 子资源命名避免 `api_` 子串（`settings_keys`/`settings_key_create`/`settings_key_delete`），
> 防 CLI 朴素子串过滤误杀。回归：test_openapi/test_api_docs_sync/help 106 passed；`check_api_docs --strict` 零缺口；
> devstage_filter 6 个失败为预存基线，与本次无关。

---

## 一、现状分析（已核对当前契约 175 paths / 220 ops）

### 1.1 数据：220 ops 中 64 显式 / 156 自动

| 类型 | 数 | 命名示例 | CLI 是否可见 |
|------|:---:|----------|:---:|
| 显式 `operation_id=`（见附录 B） | 64 | `skills_list` / `gitrepo_branches` / `issues_mine` / `tasks_complete` | ✅ |
| 自动生成（装饰器未写 operation_id，见附录 A） | **156** | `sync_repos_api_repos_sync_post` / `create_new_repo_api_repos_create_post` | ❌ |

### 1.2 根因：FastAPI 自动命名 `*_api_*` + CLI 过滤

- FastAPI 对未声明 `operation_id` 的路由自动生成：`<函数名>_api_<路径段>_<方法>`。
  `/api/...` 前缀里的 `api` 直接进入名字，所以**必然含 `api_`**（`sync_repos` + `/api/repos/sync` + `_post`）。
- `grape-cli/src/registry.py::parse_operations`：
  ```python
  if "api_" in oid:
      continue    # 过滤 autogen 丑名，宁可少命令也不暴露丑名
  ```
  → **含 `api_` 的 156 个端点整条不进 CLI**，与 GRAPE_CLI_MODE（WORKER/USER/ALL）无关。

### 1.3 分布（按 tag）

| tag | 数 | tag | 数 | tag | 数 |
|------|:---:|------|:---:|------|:---:|
| settings | 34 | scenes | 24 | runtime | 17 |
| repos | 12 | chat | 12 | events | 11 |
| skills | 10 | knowledge | 10 | devstage | 8 |
| project-templates | 4 | feishu-onboard | 3 | agents | 2 |
| auth | 2 | task-logs | 3 | dashboard | 2 |
| pipeline | 1 | search | 1 | | |

> 服务端路由文件：skills / agents / auth / chat / scenes / settings / runtime / repos / events / knowledge /
> devstage / templates / task_logs / search / dashboard / pipeline / feishu_onboard（17 个文件）。
> 全量清单见**附录 A**（method + path + 建议 operation_id），可直接照抄。

---

## 二、目标

1. 契约中 **zero `*_api_*` 自动丑名**（每个公开 op id 都为人工可读的 `<tag>_<resource>_<action>`）。
2. CLI：156 个命令全部解锁，`grape settings api-keys`、`grape scenes market list`、`grape skills upload` 等可用；
   帮助/过滤/守卫沿用 `x-token-scope`，权限语义不变。
3. `test_openapi.EXPECTED_PUBLIC` 同步为新的显式 operation_id 集合（精确匹配测试）。

---

## 三、命名规范

### 3.1 规则

`<tag>_<resource>_<action>`，全小写 snake_case，**不含 `api_`、不含路径分隔符**。

- `<tag>`：契约 `tags[0]`（`skill`→`skills`、`git-repo`→`gitrepo`、`project-templates`→`project_template`、`feishu-onboard`→`feishu_onboard`、`task-logs`→`task_logs`）。
- `<resource>`：子资源（单数化/去连字符），如 `api-keys`→`api_key`、`deploy-url`→`deploy_url`、`runtime-credentials`→`runtime_credential`、`advanced-options`→`advanced_options`。
- `<action>`：动词词典见 3.2；**集合类增删查 → 配对对称**（`_list/_create/_update/_delete`），单一资源操作 → `_get/_set`。

### 3.2 动作动词词典（统一，不随函数名漂移）

| 语义 | 动词 | 例 |
|------|------|-----|
| 列表/查询集合 | `list` | `scenes_market_list` |
| 获取单条资源 | `get` / `{resource}_get` | `settings_oauth_get` |
| 创建 | `create` | `settings_api_key_create` |
| 更新整体/部分 | `update` / `set` | `repos_deploy_url_set` |
| 删除 | `delete` | `skills_delete` |
| 启停/切换 | `enable/disable/toggle` | `skills_like_toggle` |
| 同步/刷新 | `sync/refresh` | `repos_sync` |
| 动作（非 CRUD） | 语义动词 | `events_redispatch` / `events_terminate` / `feishu_onboard_start` |

### 3.3 冲突规避

- **与既有 64 个显式名不重名**：命名前 `grep operation_id=` 全仓核对（如 `scenes_list`、`skills_list`、`chat_conversations`、`chat_messages`、`repos_managed`、`repos_member` 已占用，不重复分配）。
- HTTP 方法同 path 不同语义（如 `GET/PUT /settings/oauth`）：`_get` / `_update` 区分。
- 嵌套子命令链（如 `scenes/market/{id}/apply`）：`scenes_market_apply`（**不用** `_api_` 也不把整路径拉平）。

### 3.4 兼容性

- **不破坏既有 64 个**：只改自动名端点；已显式命名的保持不动。
- `operation_id` 变更会影响：契约（CLI 命令名）、`test_openapi.EXPECTED_PUBLIC`、CLI 侧 `openapi.en.json`/`examples.json`/`token-scope.json` 中可能引用的旧 id（若 CLI 引用则以契约 op id 为准，需同步刷新）。**HTTP 路径不变，不影响存量调用方与技能。**

---

## 四、实施步骤（分 6 批，每批独立提交 + 契约重生成 + 测试）

| 批 | 文件（路由） | 端点数 | 说明 |
|----|-------------|:---:|------|
| P1 | skills.py（10，upload 已先行）/ agents.py（2）/ auth.py（2） | 14 | 高频、CLI 最需要 |
| P2 | repos.py（12）/ settings.py（34） | 46 | 平台管理 |
| P3 | scenes.py（24）/ devstage.py（8） | 32 | 场景/开发环境 |
| P4 | chat.py（12）/ events.py（11）/ knowledge.py（10） | 33 | 对话/事件/知识 |
| P5 | runtime.py（17）/ task_logs.py（3） | 20 | 运行时/日志 |
| P6 | templates.py（4）/ feishu_onboard.py（3）/ dashboard.py（2）/ pipeline.py（1）/ search.py（1） | 11 | 其余 |

每批内部步骤：
1. 按附录 A 命名给对应路由补 `operation_id=`（必要时补 `summary=` 供 CLI 帮助）。
2. `python scripts/regenerate_openapi.py` → 契约重生成（保持 175 paths，仅 opId 变化）。
3. `tests/test_openapi.py`：`EXPECTED_PUBLIC` 对应项改为新 opId（其余不动）。
4. 回归 + `scripts/check_api_docs.py --strict` 零缺口。
5. 提交（`feat(api): 给 <tag> 端点补显式 operation_id——CLI 命令解锁`）。

> P1 先行验证模式（skills_upload 已示范：`operation_id="skills_upload"` 后 `grape skills upload` 在 USER/ALL 可见）。

---

## 五、验证

1. `grep 'api_ in oid'` 契约扫描：**0 个自动丑名**（220 个 op 全部显式）。
2. `test_openapi.test_public_ops_have_operation_id_summary_security`：`op_ids == EXPECTED_PUBLIC` 精确匹配通过。
3. `scripts/check_api_docs.py --strict`：0 缺口（文档 ↔ 契约）。
4. 抽样 CLI：`grape skills upload`、`grape settings api-keys --json-out`、`grape scenes market list` 在 USER/ALL 模式出现；WORKER 模式按 scope 正确隐藏 user_only、保留 session_only。
5. `grape <cmd> --help` 文案走 `summary/description` 正常；HTTP 路径与行为零变化。

---

## 六、影响面与风险

| 项 | 影响 |
|----|------|
| 代码 | 17 个路由文件补 `operation_id=`（纯装饰器参数，函数/路径/守卫不动） |
| 契约 | opId 变化（路径不变）；`test_openapi.EXPECTED_PUBLIC` 同步 |
| CLI | 解锁 156 命令；命令名变化（`repos sync` 等），依赖旧命令名的脚本需换名 |
| 文档/技能 | help/api 已按路径描述，不受 opId 影响；CLI-en/examples/token-scope 若引用旧 op id 需随契约刷新 |
| 风险 | 命名不统一 → 由 §三规范 + 附录 A 约束；与既有 64 个冲突 → 3.3 预检 |
| 不动点 | HTTP 路径、认证/scope、返回值、前端行为全部不变 |

---

## 附录 A：156 个待命名端点全量清单（建议 operation_id）

> 格式：`METHOD 路径` → `建议 operation_id`。命名已按 §三规范给出，可直接照抄。

### agents（2）
| 端点 | 建议 operation_id |
|------|------------------|
| PUT /api/agents/{name}/{owner}/{repo} | `agents_update` |
| POST /api/agents/{name}/{owner}/{repo}/clean | `agents_clean` |

### auth（2）
| 端点 | 建议 operation_id |
|------|------------------|
| GET /api/auth/profile | `auth_profile` |
| POST /api/auth/logout | `auth_logout` |

### chat（12）
| 端点 | 建议 operation_id |
|------|------------------|
| POST /api/chat/conversations | `chat_conversations_create` |
| DELETE /api/chat/conversations/{conv_id} | `chat_conversations_delete` |
| POST /api/chat/conversations/ensure-issue | `chat_conversations_ensure_issue` |
| POST /api/chat/send | `chat_send` |
| POST /api/chat/send/async | `chat_send_async` |
| POST /api/chat/send/stream | `chat_send_stream` |
| POST /api/chat/submit | `chat_submit` |
| POST /api/chat/feedback | `chat_feedback` |
| GET /api/chat/config | `chat_config_get` |
| PUT /api/chat/config | `chat_config_update` |
| GET /api/chat/task/{task_id}/status | `chat_task_status` |
| GET /api/chat/task/{task_id}/stream | `chat_task_stream` |

### dashboard（2）
| 端点 | 建议 operation_id |
|------|------------------|
| GET /api/dashboard/ops/overview | `dashboard_ops_overview` |
| GET /api/dashboard/{owner}/{repo} | `dashboard_repo` |

### devstage（8）
| 端点 | 建议 operation_id |
|------|------------------|
| GET /api/devstage/config | `devstage_config_get` |
| PUT /api/devstage/config | `devstage_config_update` |
| GET /api/devstage/templates | `devstage_templates` |
| GET /api/devstage/specs | `devstage_specs` |
| GET /api/devstage/environments | `devstage_environments` |
| POST /api/devstage/environments | `devstage_environment_create` |
| DELETE /api/devstage/environments/{devstage_id} | `devstage_environment_delete` |
| POST /api/devstage/environments/{action} | `devstage_environment_action` |

### events（11）
| 端点 | 建议 operation_id |
|------|------------------|
| GET /api/events/{owner}/{repo} | `events_list` |
| GET /api/events/{seq}/thread | `events_thread` |
| DELETE /api/events/{seq}/chat | `events_chat_delete` |
| DELETE /api/events/{seq}/knowledge/{scope} | `events_knowledge_delete` |
| POST /api/events/{seq}/chat | `events_chat_send` |
| POST /api/events/{seq}/chat/stream | `events_chat_stream` |
| POST /api/events/{seq}/chat/submit | `events_chat_submit` |
| GET /api/events/chat/task/{task_id}/status | `events_chat_task_status` |
| GET /api/events/chat/task/{task_id}/stream | `events_chat_task_stream` |
| POST /api/events/{seq}/terminate | `events_terminate` |
| POST /api/events/{seq}/redispatch | `events_redispatch` |

### feishu-onboard（3）
| 端点 | 建议 operation_id |
|------|------------------|
| POST /api/feishu/onboard/start | `feishu_onboard_start` |
| GET /api/feishu/onboard/status | `feishu_onboard_status` |
| POST /api/feishu/onboard/cancel | `feishu_onboard_cancel` |

### knowledge（10）
| 端点 | 建议 operation_id |
|------|------------------|
| GET /api/knowledge/{knowledge_id} | `knowledge_detail` |
| PUT /api/knowledge/{knowledge_id} | `knowledge_update` |
| DELETE /api/knowledge/{knowledge_id} | `knowledge_delete` |
| PUT /api/knowledge/{knowledge_id}/archive | `knowledge_archive` |
| POST /api/knowledge/entry | `knowledge_entry_create` |
| DELETE /api/knowledge | `knowledge_files_delete` |
| GET /api/knowledge/{knowledge_id}/refs | `knowledge_refs_list` |
| POST /api/knowledge/{knowledge_id}/refs | `knowledge_ref_create` |
| POST /api/knowledge/{knowledge_id}/refs/batch | `knowledge_refs_create_batch` |
| PUT /api/knowledge/ref/{ref_id} | `knowledge_ref_update` |
| DELETE /api/knowledge/ref/{ref_id} | `knowledge_ref_delete` |

### pipeline（1）
| 端点 | 建议 operation_id |
|------|------------------|
| GET /api/pipeline/{owner}/{repo} | `pipeline_get` |

### project-templates（4）
| 端点 | 建议 operation_id |
|------|------------------|
| GET /api/project-templates | `project_templates_list` |
| GET /api/project-templates/{template_id}/readme | `project_template_readme` |
| GET /api/project-templates/{template_id}/preview | `project_template_preview` |
| POST /api/project-templates/create | `project_template_create` |

### repos（12）
| 端点 | 建议 operation_id |
|------|------------------|
| GET /api/repos/check-name | `repos_check_name` |
| POST /api/repos/sync | `repos_sync` |
| POST /api/repos/create | `repos_create` |
| POST /api/repos/host | `repos_host` |
| POST /api/repos/remove | `repos_remove` |
| POST /api/repos/{owner}/{repo}/repair-host | `repos_repair_host` |
| GET /api/repos/bot-token/{owner}/{repo_name} | `repos_bot_token_get` |
| PUT /api/repos/bot-token | `repos_bot_token_set` |
| GET /api/repos/deploy-url/{owner}/{repo_name} | `repos_deploy_url`（已显式，保留） |
| PUT /api/repos/deploy-url | `repos_deploy_url_set` |
| GET /api/repos/{owner}/{repo}/project-info | `repos_project_info` |
| GET /api/repos/{owner}/{repo}/scene-recommend-config | `repos_scene_recommend_config_get` |
| PUT /api/repos/{owner}/{repo}/scene-recommend-config | `repos_scene_recommend_config_set` |

### runtime（17）
| 端点 | 建议 operation_id |
|------|------------------|
| GET /api/runtime/admin/event-bus | `runtime_event_bus` |
| GET /api/runtime/admin/machine-health | `runtime_machine_health` |
| GET /api/runtime/admin/machine-health/{runtime_id} | `runtime_machine_health_detail` |
| POST /api/runtime/admin/batch-recover | `runtime_batch_recover` |
| GET /api/runtime/tasks/{task_id}/status | `runtime_task_status` |
| POST /api/runtime/tasks/{task_id}/cancel | `runtime_task_cancel` |
| GET /api/runtime/runtimes/{runtime_id}/tasks | `runtime_tasks_list` |
| GET /api/runtime/runtimes/{runtime_id}/sessions | `runtime_sessions_list` |
| GET /api/runtime/credentials/{cid}/tasks | `runtime_credential_tasks` |
| POST /api/runtime/runtimes/{runtime_id}/refresh-skills | `runtime_refresh_skills` |
| GET /api/runtime/runtimes/{runtime_id}/role-tags | `runtime_role_tags` |
| PUT /api/runtime/runtimes/{runtime_id}/tags | `runtime_tags_set` |
| GET /api/runtime/runtimes/{runtime_id}/scene-tags | `runtime_scene_tags_get` |
| PUT /api/runtime/runtimes/{runtime_id}/scene-tags | `runtime_scene_tags_set` |
| GET /api/runtime/scene-tags | `runtime_scene_tags_list` |
| PUT /api/runtime/runtimes/{runtime_id}/alias | `runtime_alias_set` |
| PUT /api/runtime/runtimes/{runtime_id}/max-concurrent | `runtime_max_concurrent_set` |

### scenes（24）
| 端点 | 建议 operation_id |
|------|------------------|
| POST /api/scenes | `scenes_create` |
| GET /api/scenes/with-pipeline | `scenes_with_pipeline` |
| GET /api/scenes/check-name | `scenes_check_name` |
| GET /api/scenes/{scene_id} | `scenes_detail` |
| PUT /api/scenes/{scene_id} | `scenes_update` |
| DELETE /api/scenes/{scene_id} | `scenes_delete` |
| GET /api/scenes/{scene_id}/advanced-options | `scenes_advanced_options_get` |
| PUT /api/scenes/{scene_id}/advanced-options | `scenes_advanced_options_set` |
| GET /api/scenes/skills/{agent_type} | `scenes_skills_for_agent` |
| POST /api/scenes/apply/{owner}/{repo} | `scenes_apply` |
| GET /api/scenes/branches/{owner}/{repo} | `scenes_branches` |
| POST /api/scenes/branches/{owner}/{repo} | `scenes_branch_create` |
| GET /api/scenes/market | `scenes_market_list` |
| POST /api/scenes/market | `scenes_market_create` |
| GET /api/scenes/market/categories | `scenes_market_categories` |
| GET /api/scenes/market/{market_id} | `scenes_market_detail` |
| PUT /api/scenes/market/{market_id} | `scenes_market_update` |
| DELETE /api/scenes/market/{market_id} | `scenes_market_delete` |
| GET /api/scenes/market/{market_id}/applied | `scenes_market_applied` |
| POST /api/scenes/market/{market_id}/apply | `scenes_market_apply` |
| POST /api/scenes/market/{market_id}/experience | `scenes_market_experience` |
| GET /api/scenes/market/{market_id}/experience-check | `scenes_market_experience_check` |
| POST /api/scenes/market/{market_id}/like | `scenes_market_like_toggle` |
| POST /api/scenes/market/icon-upload | `scenes_market_icon_upload` |

### search（1）
| 端点 | 建议 operation_id |
|------|------------------|
| GET /api/search | `search_global` |

### settings（34）
| 端点 | 建议 operation_id |
|------|------------------|
| GET /api/settings/credentials | `settings_credentials_get` |
| PUT /api/settings/credentials | `settings_credentials_update` |
| GET /api/settings/notifications | `settings_notifications_get` |
| PUT /api/settings/notifications | `settings_notifications_update` |
| GET /api/settings/oauth | `settings_oauth_get` |
| PUT /api/settings/oauth | `settings_oauth_update` |
| GET /api/settings/oauth-github | `settings_oauth_github_get` |
| PUT /api/settings/oauth-github | `settings_oauth_github_update` |
| GET /api/settings/gitcode-repo/{owner}/{repo} | `settings_gitcode_repo_get` |
| PUT /api/settings/gitcode-repo/{owner}/{repo} | `settings_gitcode_repo_update` |
| GET /api/settings/agent-runtimes | `settings_agent_runtimes` |
| POST /api/settings/agent-runtimes/refresh | `settings_agent_runtimes_refresh` |
| GET /api/settings/backend-agents | `settings_backend_agents` |
| POST /api/settings/backend-agents/refresh | `settings_backend_agents_refresh` |
| GET /api/settings/agent-runtimes/{rid}/tags | `settings_engine_tags` |
| POST /api/settings/agent-runtimes/{rid}/tags | `settings_engine_tag_create` |
| PUT /api/settings/agent-runtimes/{rid}/tags/{tag} | `settings_engine_tag_update` |
| DELETE /api/settings/agent-runtimes/{rid}/tags/{tag} | `settings_engine_tag_delete` |
| GET /api/settings/runtime-credentials | `settings_runtime_credentials` |
| POST /api/settings/runtime-credentials | `settings_runtime_credential_create` |
| PUT /api/settings/runtime-credentials/{cid} | `settings_runtime_credential_rename` |
| PUT /api/settings/runtime-credentials/{cid}/scope | `settings_runtime_credential_scope` |
| POST /api/settings/runtime-credentials/{cid}/revoke | `settings_runtime_credential_revoke` |
| GET /api/settings/api-keys | `settings_keys` |
| POST /api/settings/api-keys | `settings_key_create` |
| DELETE /api/settings/api-keys/{key_id} | `settings_key_delete` |
| GET /api/settings/execution-mode | `settings_execution_mode_get` |
| PUT /api/settings/execution-mode | `settings_execution_mode_set` |
| GET /api/settings/pipeline-mode | `settings_pipeline_mode_get` |
| PUT /api/settings/pipeline-mode | `settings_pipeline_mode_set` |
| GET /api/settings/event-action-filters | `settings_event_action_filters` |
| POST /api/settings/event-action-filters | `settings_event_action_rule_create` |
| PUT /api/settings/event-action-filters/{rule_id} | `settings_event_action_rule_update` |
| DELETE /api/settings/event-action-filters/{rule_id} | `settings_event_action_rule_delete` |

### skills（10）
| 端点 | 建议 operation_id |
|------|------------------|
| GET /api/skills/featured | `skills_featured` |
| GET /api/skills/search | `skills_search` |
| GET /api/skills/stats | `skills_stats` |
| DELETE /api/skills/by-id/{skill_id} | `skills_delete` |
| GET /api/skills/by-id/{skill_id}/download | `skills_download` |
| GET /api/skills/{skill_id}/sub | `skills_sub_list` |
| GET /api/skills/{skill_id}/sub/{sub_name} | `skills_sub_get` |
| GET /api/skills/package-sub/{pkg_id}/{sub_name} | `skills_package_sub_get` |
| POST /api/skills/convert | `skills_convert` |
| POST /api/skills/{skill_id}/like | `skills_like_toggle` |

### task-logs（3）
| 端点 | 建议 operation_id |
|------|------------------|
| GET /api/task-logs/{task_id}/info | `task_logs_info` |
| GET /api/task-logs/{task_id}/messages | `task_logs_messages` |
| GET /api/task-logs/{task_id}/stream | `task_logs_stream` |

---

## 附录 B：已显式命名的 64 个端点（保持不动）

> 已声明 `operation_id=`（不含 `api_`），CLI 现可见。**附录 A 的命名不得与本表冲突**；后续若改动这些端点，
> 应保留现有 operation_id 以维持契约/CLI 稳定。

### agents（1）
| 端点 | operation_id |
|------|--------------|
| GET /api/agents/{name}/{owner}/{repo} | `agents_detail` |

### a2a（1）
| 端点 | operation_id |
|------|--------------|
| POST /api/a2a | `a2a_handoff` |

### chat（2）
| 端点 | operation_id |
|------|--------------|
| GET /api/chat/conversations | `chat_conversations` |
| GET /api/chat/messages/{conv_id} | `chat_messages` |

### events（1）
| 端点 | operation_id |
|------|--------------|
| GET /api/events/detail/{seq} | `events_detail` |

### git-repo（23）
| 端点 | operation_id |
|------|--------------|
| GET /api/git-repo/{owner}/{repo} | `gitrepo_repo` |
| GET /api/git-repo/{owner}/{repo}/branches | `gitrepo_branches` |
| GET /api/git-repo/{owner}/{repo}/commits | `gitrepo_commits` |
| GET /api/git-repo/{owner}/{repo}/contents | `gitrepo_contents_root` |
| GET /api/git-repo/{owner}/{repo}/contents/{path} | `gitrepo_contents` |
| GET /api/git-repo/{owner}/{repo}/issues | `gitrepo_issues_list` |
| POST /api/git-repo/{owner}/{repo}/issues | `gitrepo_issues_create` |
| GET /api/git-repo/{owner}/{repo}/issues/{number} | `gitrepo_issues_get` |
| PATCH /api/git-repo/{owner}/{repo}/issues/{number} | `gitrepo_issues_update` |
| GET /api/git-repo/{owner}/{repo}/issues/{number}/comments | `gitrepo_issues_comments_list` |
| POST /api/git-repo/{owner}/{repo}/issues/{number}/comments | `gitrepo_issues_comments_create` |
| GET /api/git-repo/{owner}/{repo}/labels | `gitrepo_labels_list` |
| POST /api/git-repo/{owner}/{repo}/labels | `gitrepo_labels_create` |
| GET /api/git-repo/{owner}/{repo}/milestones | `gitrepo_milestones` |
| GET /api/git-repo/{owner}/{repo}/pulls | `gitrepo_pulls_list` |
| POST /api/git-repo/{owner}/{repo}/pulls | `gitrepo_pulls_create` |
| GET /api/git-repo/{owner}/{repo}/pulls/{number} | `gitrepo_pulls_get` |
| PATCH /api/git-repo/{owner}/{repo}/pulls/{number} | `gitrepo_pulls_update` |
| GET /api/git-repo/{owner}/{repo}/pulls/{number}/comments | `gitrepo_pulls_comments_list` |
| POST /api/git-repo/{owner}/{repo}/pulls/{number}/comments | `gitrepo_pulls_comments_create` |
| GET /api/git-repo/{owner}/{repo}/pulls/{number}/files | `gitrepo_pulls_files` |
| PUT /api/git-repo/{owner}/{repo}/pulls/{number}/merge | `gitrepo_pulls_merge` |
| GET /api/git-repo/{owner}/{repo}/tree | `gitrepo_tree` |

### issues（18）
| 端点 | operation_id |
|------|--------------|
| GET /api/issues/mine | `issues_mine` |
| GET /api/issues/{owner}/{repo} | `issues_list` |
| POST /api/issues/{owner}/{repo} | `issues_create` |
| GET /api/issues/{owner}/{repo}/{number} | `issues_detail` |
| GET /api/issues/{owner}/{repo}/{number}/events | `issues_events` |
| GET /api/issues/{owner}/{repo}/{number}/pipeline | `issues_pipeline` |
| POST /api/issues/{issue_id}/start | `issues_start` |
| GET /api/issues/{issue_id}/status | `issues_status_get` |
| PUT /api/issues/{issue_id}/status | `issues_status` |
| POST /api/issues/{issue_id}/confirm-done | `issues_confirm_done` |
| POST /api/issues/{issue_id}/cancel | `issues_cancel` |
| POST /api/issues/{issue_id}/resume | `issues_resume` |
| GET /api/issues/{issue_id}/pipeline | `issues_pipeline_detail` |
| GET /api/issues/{issue_id}/clarifications | `issues_clarifications` |
| POST /api/issues/{issue_id}/clarify | `issues_clarify` |
| POST /api/issues/{issue_id}/attachments | `issues_upload_attachment` |
| GET /api/issues/{issue_id}/attachments | `issues_list_attachments` |
| DELETE /api/issues/{issue_id}/attachments/{filename} | `issues_delete_attachment` |

### knowledge（5）
| 端点 | operation_id |
|------|--------------|
| GET /api/knowledge | `knowledge_list` |
| GET /api/knowledge/content | `knowledge_content` |
| GET /api/knowledge/context | `knowledge_context` |
| GET /api/knowledge/stats | `knowledge_stats` |
| POST /api/knowledge/{knowledge_id}/refs/batch | `knowledge_add_refs_batch` |

### repos（5）
| 端点 | operation_id |
|------|--------------|
| GET /api/repos/managed | `repos_managed` |
| GET /api/repos/member | `repos_member` |
| GET /api/repos/deploy-url/{owner}/{repo_name} | `repos_deploy_url` |
| GET /api/repos/{owner}/{repo}/git-credential | `repos_git_credential` |
| GET /api/repos/{owner}/{repo}/workspace | `repos_workspace` |

### scenes（1）
| 端点 | operation_id |
|------|--------------|
| GET /api/scenes | `scenes_list` |

### skills（4）
| 端点 | operation_id |
|------|--------------|
| GET /api/skills | `skills_list` |
| GET /api/skills/agent-stats | `skills_agent_stats` |
| GET /api/skills/by-id/{skill_id} | `skills_detail` |
| POST /api/skills/upload | `skills_upload` |

### tasks（3）
| 端点 | operation_id |
|------|--------------|
| POST /api/tasks/complete | `tasks_complete` |
| GET /api/tasks/current | `tasks_current` |
| POST /api/tasks/notify | `tasks_notify` |

---

## 附：与既有设计衔接

- 契约/CLI 命令生成机制：`docs/weekly-02/issue-45-cli-design.md`（operationId → `grape <tag> <verb>`）。
- scope 标注：`openapi-x-token-scope-design.md` / `token-scope-classification-design.md`（本次不改变任何 scope）。
- 文档同步：`api-docs-standardization-design.md`（help/api 按路径描述，opId 变更不影响文档，但 sync 测试需重跑）。