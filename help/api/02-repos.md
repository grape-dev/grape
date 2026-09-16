# Repos 仓库接口（/api/repos）

> 对外公开（部分只读），认证：`get_current_user_or_api_key`。
> scope（x-token-scope）：平台业务接口（managed/member/sync/check-name/create/host/repair/bot-token/deploy-url/remove/project-info 等）= **user_only**；
> `{owner}/{repo}/git-credential`、`{owner}/{repo}/workspace`（CLI 私有） = **session_only**（仅 `tk_`）。
> ⚠️ **与 `/api/git-repo` 区分**：本接口（`/api/repos`）是 **grape 平台的仓库接口**，承载 grape 平**台业务属性
> （托管/同步/成员/bot token/部署 URL/项目知识等）；而 `/api/git-repo`（见 10-git-repos.md）是
> **Git 仓库透传接口**（Issue/PR/内容/分支等由 gitcode/github/gitlab 提供，后端由 token 的 repo_key 前缀判定）。

## GET /api/repos/managed — 托管仓库列表

| Query | `status`（可选）等 |
| 出参 | `[{repo_key, repo_owner, repo_name, full_name, webhook_id, bot_username, dev_scene, host_status, deploy_url, created_at}]` |
| 返回 | 200 |

---

## GET /api/repos/member — 成员仓库列表

| 出参 | 用户参与的仓库数组 |
| 返回 | 200 |

---

## POST /api/repos/sync — 同步仓库

| 出参 | `{"ok": true, ...}` |
| 返回 | 200 |

---

## GET /api/repos/check-name — 校验仓库名

| Query | `name` / `owner` 等 |
| 出参 | `{"available": bool, ...}` |
| 返回 | 200 |

---

## POST /api/repos/create — 创建仓库

请求头：`Content-Type: application/json`

请求体（CreateRepoBody）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `name` | string | ✅ | 仓库名 |
| `description` | string | 否 | 描述 |
| `private` | bool | 否 | 私密 |
| `visibility` | string | 否 | 可见性 |
| `auto_init` | bool | 否 | 自动初始化（默认 true） |
| `default_branch` | string | 否 | 默认分支（main） |
| `org` | string | 否 | 组织 |
| `host_after_create` | bool | 否 | 创建后是否托管 |

出参：`{repo_key, full_name, ...}`

返回：200/201；400。

---

## POST /api/repos/host — 托管仓库

请求头：`Content-Type: application/json`

请求体（RepoAction）：`{"full_name": "owner/repo"}`

| 出参 | `{"full_name", "webhook_id", "host_warning", "message"}` |
| 返回 | 200；托管失败不中断，返回 host_warning |

---

## POST /api/repos/{owner}/{repo}/repair-host — 修复托管

| Path | `{owner}` `{repo}` |
| 出参 | `{"ok": true, ...}` |
| 返回 | 200；未托管 404 |

---

## GET /api/repos/bot-token/{owner}/{repo} — 获取仓库 bot token

| Path | `{owner}` `{repo}` |
| 出参 | `{"bot_gitcode_token": "***", ...}`（脱敏） |
| 返回 | 200；未配置 400 |

---

## PUT /api/repos/bot-token — 设置仓库 bot token

请求头：`Content-Type: application/json`

请求体（BotTokenBody）：`{"full_name": "owner/repo", "bot_token": "..."}`

| 出参 | `{"ok": true}` |
| 返回 | 200 |

---

## GET /api/repos/deploy-url/{owner}/{repo} — 获取部署 URL

| Path | `{owner}` `{repo}` |
| 出参 | `{"deploy_url": "..."}` |
| 返回 | 200 |

---

## PUT /api/repos/deploy-url — 设置部署 URL

请求头：`Content-Type: application/json`

请求体（DeployUrlBody）：`{"full_name": "owner/repo", "deploy_url": "..."}`

| 出参 | `{"ok": true}` |
| 返回 | 200 |

---

## POST /api/repos/remove — 解除托管

请求头：`Content-Type: application/json`

请求体（RepoAction）：`{"full_name": "owner/repo"}`

| 出参 | `{"ok": true, ...}` |
| 返回 | 200 |

---

## GET /api/repos/{owner}/{repo}/project-info — 项目知识

| Path | `{owner}` `{repo}` |
| 出参 | `{"project-graph.md": "...", "project-capabilities.md": "..."}` |
| 返回 | 200 |

---

## GET /api/repos/{owner}/{repo}/git-credential — 一次性 git 平台凭据（grape 私有，方案③ CLI 内部使用）

> ⚠️ 非 any gitcode/github/gitlab 平台原生接口——是 **grape 自身能力**：把平台 token 按会话身份
> （chat→用户 token / devops agent→仓库 bot token）以**一次性/短期凭证**形式交 CLI 本地 `git clone/push` 用。

| Path | `{owner}` `{repo}` |
| Provider | 由认证 token 的 `repo_key` 前缀派生（gitcode:/github:），不传 provider 参数 |
| 出参 | `{provider: "gitcode", username: "x-access-token", password: "<一次性 token>", clone_url: "...", expires_in: 600}` |
| 返回 | 200；403 非会话级 token / provider 未接入；502 平台 token 解析失败 |

---

## GET /api/repos/{owner}/{repo}/workspace — 仓库工作区路径（grape 私有）

> ⚠️ 非平台原生——返回 grape 管理工作区（worktree）的本地路径，供 CLI 在本地执行 git。

| Path | `{owner}` `{repo}` |
| Provider | 由认证 token 的 `repo_key` 前缀派生（gitcode:/github:），不传 provider 参数 |
| 出参 | `{workspace: "<本地路径>", clone_url: "https://gitcode.com/{owner}/{repo}.git"}` |
| 返回 | 200 |