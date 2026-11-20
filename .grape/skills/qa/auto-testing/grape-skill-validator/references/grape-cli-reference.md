# grape CLI 参考基线（交叉验证用）

> 事实来源：cli-code-analysis-spec.md（CLI v0.2.8，58 paths / 68 operations）
> 用途：skill-validator 交叉验证被检查 skill 的命令、参数、schema、不存在能力声明是否与事实基线一致。只读不修改。

---

## 1. CLI 总览

| 项 | 内容 |
|----|------|
| 入口 | `grape` = `src/cli:main`；`python -m src` 备选 |
| 版本 | `0.2.8` |
| 框架 | Python ≥3.10 / click ≥8.0 + httpx ≥0.24 |
| 机制 | 读取 `/openapi.json` → 按 `operationId` 自动生成命令树 |
| 命令总数 | 68 契约 + 4 内置 = 72 |
| 契约路径 | 58 paths / 68 operations（GET 48 / POST 15 / PUT 1 / PATCH 2 / DELETE 1） |
| 能力边界 | 只读 48 / 变更 19 / 异步轮询 1（wait）/ 本地透传 1（git） |
| 认证三通道 | Bearer（默认）/ PRIVATE-TOKEN / access_token（query） |
| Token 优先级 | `--token` > `GRAPE_SESSION_TOKEN`（tk_）> `GRAPE_API_TOKEN`（grape_）> config.json |
| base_url 优先级 | `--base-url` > `GRAPE_BASE_URL` > `GRAPE_SERVER_URL` > config.json > localhost:8000 |

---

## 2. 安装检查（grape CLI）

`grape` 通常预装；**未装/版本过低可自行安装**（见 `references/grape-cli-installation.md`）。使用前验证：

```bash
grape --version    # 确认安装（应 >= 0.3.2）
grape commands     # 验证命令树可用
```

> ⚠️ grape 未安装且无法安装时，输出「任务无法完成的理由」，不 curl 直连 API。401 报错说明 token 失效。

---

## 3. 内置命令（4 个）

| 命令 | 用途 | 变更性 |
|------|------|--------|
| `grape refresh` | 强制重新拉取 OpenAPI 契约并缓存 | 只读 |
| `grape commands [pattern]` | 列出全部可调用契约命令，支持 pattern 过滤 | 只读 |
| `grape tasks wait --task-id <id> [--timeout=120] [--interval=3] [--json-out]` | 轮询任务直至终态 | 只读（轮询） |
| `grape git <args>` | 本地 git 透传 + 进程内凭据注入（仅 gitcode.com） | 取决于 git 子命令 |

> ⚠️ v0.2.2 移除 `grape api` 通用兜底命令。契约未覆盖端点无 CLI 逃生舱。
> ⚠️ `grape commands` 输出空格分隔可调用形式（如 `tasks mine`），可直接执行。

---

## 4. 契约命令清单（68 操作，按 tag 分组）

> ⚠️ 本文件为交叉验证基线，§4 列全量 68 命令供核对被检查 skill 的命令是否存在，非本 skill 常用命令。

### agents（1）
| 命令 | 方法 | 路径 | 变更性 |
|------|------|------|--------|
| `agents detail --name --owner --repo [--issue_number] [--keyword] [--page] [--page_size]` | GET | `/api/agents/{name}/{owner}/{repo}` | 只读 |

### auth / chat / events（4）
| `auth profile get` | GET | `/api/auth/profile` | 只读 |
| `chat conversations [--repo_key]` | GET | `/api/chat/conversations` | 只读 |
| `chat messages --conv_id [--before=0]` | GET | `/api/chat/messages/{conv_id}` | 只读 |
| `events detail --seq` | GET | `/api/events/detail/{seq}` | 只读 |

### git-repo（23）
| 命令 | 方法 | 路径 | 变更性 |
|------|------|------|--------|
| `git-repo repo --owner --repo` | GET | `/api/git-repo/{owner}/{repo}` | 只读 |
| `git-repo branches --owner --repo [--per_page=50]` | GET | `/api/git-repo/{owner}/{repo}/branches` | 只读 |
| `git-repo commits --owner --repo [--per_page=20]` | GET | `/api/git-repo/{owner}/{repo}/commits` | 只读 |
| `git-repo contents --owner --repo --path [--ref] [--raw]` | GET | `/api/git-repo/{owner}/{repo}/contents/{path}` | 只读 |
| `git-repo contents root --owner --repo [--ref]` | GET | `/api/git-repo/{owner}/{repo}/contents` | 只读 |
| `git-repo tree --owner --repo [--ref] [--depth=single]` | GET | `/api/git-repo/{owner}/{repo}/tree` | 只读 |
| `git-repo labels list --owner --repo` | GET | `/api/git-repo/{owner}/{repo}/labels` | 只读 |
| `git-repo labels create --owner --repo --json` | POST | `/api/git-repo/{owner}/{repo}/labels` | 变更 |
| `git-repo milestones --owner --repo [--state=all]` | GET | `/api/git-repo/{owner}/{repo}/milestones` | 只读 |
| `git-repo issues list --owner --repo [--state=open] [--page] [--per_page=50]` | GET | `/api/git-repo/{owner}/{repo}/issues` | 只读 |
| `git-repo issues create --owner --repo --json` | POST | `/api/git-repo/{owner}/{repo}/issues` | 变更 |
| `git-repo issues get --owner --repo --number` | GET | `/api/git-repo/{owner}/{repo}/issues/{number}` | 只读 |
| `git-repo issues update --owner --repo --number --json` | PATCH | `/api/git-repo/{owner}/{repo}/issues/{number}` | 变更 |
| `git-repo issues comments list --owner --repo --number` | GET | `/api/git-repo/{owner}/{repo}/issues/{number}/comments` | 只读 |
| `git-repo issues comments create --owner --repo --number --json` | POST | `/api/git-repo/{owner}/{repo}/issues/{number}/comments` | 变更 |
| `git-repo pulls list --owner --repo [--state=open] [--page] [--per_page=50]` | GET | `/api/git-repo/{owner}/{repo}/pulls` | 只读 |
| `git-repo pulls create --owner --repo --json` | POST | `/api/git-repo/{owner}/{repo}/pulls` | 变更 |
| `git-repo pulls get --owner --repo --number` | GET | `/api/git-repo/{owner}/{repo}/pulls/{number}` | 只读 |
| `git-repo pulls update --owner --repo --number --json` | PATCH | `/api/git-repo/{owner}/{repo}/pulls/{number}` | 变更 |
| `git-repo pulls merge --owner --repo --number` | PUT | `/api/git-repo/{owner}/{repo}/pulls/{number}/merge` | 变更 |
| `git-repo pulls comments list --owner --repo --number` | GET | `/api/git-repo/{owner}/{repo}/pulls/{number}/comments` | 只读 |
| `git-repo pulls comments create --owner --repo --number --json` | POST | `/api/git-repo/{owner}/{repo}/pulls/{number}/comments` | 变更 |
| `git-repo pulls files --owner --repo --number` | GET | `/api/git-repo/{owner}/{repo}/pulls/{number}/files` | 只读 |

> v0.2.x 路径前缀 `/api/repos/` → `/api/git-repo/`；`--provider` 已移除。

### issues（5+ 流程状态）
| 命令 | 方法 | 路径 | 变更性 |
|------|------|------|--------|
| `issues list --owner --repo` | GET | `/api/issues/{owner}/{repo}` | 只读 |
| `issues create --owner --repo --json` | POST | `/api/issues/{owner}/{repo}` | 变更 |
| `issues detail --owner --repo --number` | GET | `/api/issues/{owner}/{repo}/{number}` | 只读 |
| `issues events --owner --repo --number` | GET | `/api/issues/{owner}/{repo}/{number}/events` | 只读 |
| `issues pipeline --owner --repo --number` | GET | `/api/issues/{owner}/{repo}/{number}/pipeline` | 只读 |

**流程状态命令（审查/交付决策用，交叉验证 qa 等 skill 时核对）：**
| `issues awaiting-input --issue_id --json` | POST | 待澄清（置 awaiting_input + 触达用户） | 变更 |
| `issues pending-review --issue_id --json` | POST | 置待人工拍板审核点（审查通过未合入） | 变更 |
| `issues review-pass --issue_id --json` | POST | 审核通过（target=done 完成 / processing 继续） | 变更 |
| `issues review-reject --issue_id --json` | POST | 审核打回 | 变更 |

### knowledge / repos / scenes（11）
| `knowledge list [--repo] [--source] [--page] [--page_size=50] [--show_archived=False] [--sort] [--q]` | GET | `/api/knowledge` | 只读 |
| `knowledge detail --knowledge_id` | GET | `/api/knowledge/{knowledge_id}` | 只读 |
| `knowledge content [--repo] [--file] [--obs_key]` | GET | `/api/knowledge/content` | 只读 |
| `knowledge context [--repo] [--agent_type] [--issue_num=0] [--scene_id]` | GET | `/api/knowledge/context` | 只读 |
| `knowledge stats [--repo]` | GET | `/api/knowledge/stats` | 只读 |
| `repos member` | GET | `/api/repos/member` | 只读 |
| `repos managed` | GET | `/api/repos/managed` | 只读 |
| `repos deploy url --owner --repo_name` | GET | `/api/repos/deploy-url/{owner}/{repo_name}` | 只读 |
| `repos git credential --owner --repo` | GET | `/api/repos/{owner}/{repo}/git-credential` | 只读 |
| `repos workspace --owner --repo` | GET | `/api/repos/{owner}/{repo}/workspace` | 只读 |
| `scenes list [--repo]` | GET | `/api/scenes` | 只读 |

### skills（13）
| `skills list [--repo] [--scope] [--q] [--sort] [--page] [--page_size=0]` | GET | `/api/skills` | 只读 |
| `skills detail --skill_id [--repo]` | GET | `/api/skills/by-id/{skill_id}` | 只读 |
| `skills search [--q] [--repo] [--page] [--page_size=20]` | GET | `/api/skills/search` | 只读 |
| `skills stats [--repo]` | GET | `/api/skills/stats` | 只读 |
| `skills featured [--repo] [--page] [--page_size=20]` | GET | `/api/skills/featured` | 只读 |
| `skills upload --file <path> [--json]` | POST | `/api/skills/upload` | 变更 |
| `skills convert --json` | POST | `/api/skills/convert` | 变更 |
| `skills delete --skill_id` | DELETE | `/api/skills/by-id/{skill_id}` | 变更 |
| `skills download --skill_id` | GET | `/api/skills/by-id/{skill_id}/download` | 只读 |
| `skills like --skill_id --json` | POST | `/api/skills/{skill_id}/like` | 变更 |
| `skills sub list --skill_id` | GET | `/api/skills/{skill_id}/sub` | 只读 |
| `skills sub read --skill_id --sub_name` | GET | `/api/skills/{skill_id}/sub/{sub_name}` | 只读 |
| `skills sub package --skill_id --pkg_id --sub_name` | GET | `/api/skills/package-sub/{pkg_id}/{sub_name}` | 只读 |

### tasks（10 契约 + 1 内置）
| `tasks mine [--status] [--repo_key] [--sort_order=desc] [--mode=default] [--created_by_me] [--assignees]` | GET | `/api/tasks/mine` | 只读 |
| `tasks start --task_id` | POST | `/api/tasks/{task_id}/start` | 变更 |
| `tasks status --task_id --json` | PUT | `/api/tasks/{task_id}/status` | 变更 |
| `tasks status get --task_id` | GET | `/api/tasks/{task_id}/status` | 只读（**遮蔽，不可执行**） |
| `tasks complete --json` | POST | `/api/tasks/complete` | 变更 |
| `tasks close --json` | POST | `/api/tasks/close` | 变更 |
| `tasks notify --json` | POST | `/api/tasks/notify` | 变更 |
| `tasks clarify --task_id --json` | POST | `/api/tasks/{task_id}/clarify` | 变更 |
| `tasks clarifications --task_id` | GET | `/api/tasks/{task_id}/clarifications` | 只读 |
| `tasks repo all --owner --repo [--status] [--sort_order=desc] [--mode=default] [--assignees]` | GET | `/api/tasks/repo/{owner}/{repo}` | 只读 |
| `tasks wait --task-id [--timeout=120] [--interval=3]` | 内置轮询 | 复用 GET status | 只读 |

> ⚠️ **命令遮蔽**：`tasks status get` 不可执行。查询任务状态用 `grape tasks wait`。

### a2a（1）
| `a2a handoff --json` | POST | `/api/a2a` | 变更 |

---

## 5. 参数规则（验证重点）

### 5.1 通用参数模式
| 来源 | CLI 映射 | 必填 | 说明 |
|------|----------|------|------|
| path 参数 | `--<name>` | **必填** | URL 路径替换 |
| query 参数 | `--<name>` | 可选 | 空值自动丢弃 |
| requestBody（JSON） | `--json '<JSON>'` | 有 body 时必填 | 非法报错 |
| requestBody（multipart） | `--file <path>` | 上传类必填 | multipart/form-data |
| 机器可读输出 | `--json-out` | 可选 | 单行 JSON，省 30-50% token |
| contents 解码 | `--raw` | 可选 | 直接输出解码内容 |

### 5.2 命名规则
- **path 参数用下划线名**：`--task_id` `--skill_id` `--per_page` `--issue_number` `--knowledge_id` `--conv_id` `--repo_name` `--sub_name` `--pkg_id`
- **内置 wait 参数用连字符名**：`--task-id` `--timeout` `--interval`
- **integer 类型**：`--number`/`--task_id`/`--seq`/`--skill_id` 传数字值
- **占位符**：统一花括号 `{owner}` `{repo}` `{n}` 格式，不用 `<owner>`

---

## 6. requestBody Schema 清单（§3.5 交叉验证基准）

> `issues create` body 为 free-form JSON（`additionalProperties: true`）。

| Body schema | 使用命令 | 必填 | 可选字段 |
|-------------|---------|------|---------|
| `IssueCreateBody` | `git-repo issues create` | `title` | `body=""`, `assignee=""` |
| `IssueUpdateBody` | `git-repo issues update` | — | `labels=""`, `assignee=""`, `milestone_id=0`, `state=""` |
| `CommentBody` | issues/pulls comments create | `body` | — |
| `LabelCreateBody` | `git-repo labels create` | `name` | `color="#d73a4a"` |
| `PRCreateBody` | `git-repo pulls create` | — | `title=""`, `head=""`, `base=""`, `related_issue_nums=[]`, `auto_close_issues=false` |
| `PRUpdateBody` | `git-repo pulls update` | — | `title=""`, `body=""`, `labels=""` |
| `A2AHandoffBody` | `a2a handoff` | — | `agent=""`, `payload={}` |
| `TaskStatusBody` | `tasks status` | — | `status=""` |
| `TaskSelfCompleteBody` | `tasks complete` | — | `task_id=""`, `result_text=""`, `next_step=""`, `actions=[]`, `conclusion=""`, `artifacts=[]`, `issue_summary=""` |
| `TaskClarifyBody` | `tasks clarify` | — | `message=""` |
| Free-form JSON | `issues create` | — | title/description/assignee/scene_id/is_subtask_mode 等 |
| multipart | `skills upload` | `--file` | 表单字段 |

> openapi.json `components.schemas` 共 75 个 schema。

---

## 7. 不存在的能力（§7.3 防幻觉边界）

| 能力 | 说明 |
|------|------|
| 仓库创建/删除 | 无 `repos create`/`repos delete` |
| 任务删除 | 无 `tasks delete`（有 `tasks close` 但非删除） |
| 知识上传 | 无 `knowledge upload` |
| Issue/PR 删除 | 无 issues delete / pulls delete |
| 分支创建/删除 | 无 branches create / branches delete |
| `grape api` 逃生舱 | v0.2.2 移除 |
| `tasks create` | 已移除，用 `issues create` 代替 |
| 流式输出 | 全部 JSON 一次性输出，无 SSE |
| 交互式提示 | 缺 token 直接报错，无 prompt |
| Token 管理 | 无 CLI 子命令创建/吊销/刷新 token |
| 配置初始化 | 无 `grape init`/`grape config` |

> ⚠️ skills upload/delete **已存在**（v0.2.6 新增），不应列为不存在。

---

## 8. 退出码与认证

**退出码**：0 成功 · 1 通用错误 · 2 用法错误 · ≥400 HTTP 透传 · 130 Ctrl+C · git 退出码透传

**认证**：`$GRAPE_SESSION_TOKEN` 服务端注入自动读取；`$GRAPE_SERVER_URL` 服务端注入；token 任务级临时；`GRAPE_CLI_MODE`（WORKER/USER/ALL）过滤可见命令；grape 未安装且无法安装时输出「任务无法完成」理由，不 curl 直连。

**命令验证**：`grape commands | grep <关键词>` 或 `grape <group> <verb> --help`
