# 方案：移除 Agent 环境中的 VCS 平台 token（BOT_*/USER_*），仅删注入、服务端保留

> 状态：现状评估 + 方案（2026-10-12 修订）｜分支：weekly-07（新建）
> 背景：已引入 `GRAPE_SESSION_TOKEN`（tk_，grape CLI/API 会话凭据）。因把 `BOT_GITCODE_TOKEN` 等平台 token 直接注入 agent 环境存在**极大的安全隐患**，
> 拟**删除 agent 环境注入**的 `BOT_GITCODE_TOKEN / BOT_GITHUB_TOKEN / USER_GITCODE_TOKEN / USER_GITHUB_TOKEN`。
> ⚠️ **范围澄清（2026-10-12）**：删除**仅限 agent 一侧的环境变量注入**；**grape 服务端必须继续维护这些 GitCode/GitHub token**——
> 因为 `grape CLI → 服务端` 之后，最终仍是服务端拿着这些 token 去完成真实的 GitCode/GitHub 操作（平台透传模型）。

---

## 1. 凭据模型（为什么"只删注入"就够）

```
Agent（技能）
  └─ grape git / git-repo / issues / …（命令行，携带 GRAPE_SESSION_TOKEN=tk_）
       └─ GET/POST /api/git-repo/*（_SESSION_ONLY_PATH_PREFIXES，openapi.py:79）
            └─ 服务端：Repo(repo_key).bot_token / get_gitcode_token(user)/get_github_token  → 执行真实 VCS 动作
```

- Agent 只持**短期、任务作用域、终态 revoke** 的 `tk_` 会话 token；
- GitCode/GitHub 长期凭据只在**服务端进程**内流转，从不注入 agent env；
- 删除 agent env 注入后：Agent 无法也无需裸用平台 token（`git push`/API 直调），全部经由 `grape` CLI 转发。

## 2. 服务端必须保留的 token（不可删）

| 持有/使用点 | 说明 |
|------------|------|
| `managed_repos.bot_gitcode_token` / `Repo(repo_key).bot_token` | `/api/git-repo` 平台透传、事件流水线 comment 回写、PR merge、onboarding、events 同步 |
| `get_gitcode_token(repo_key)` / `get_github_token(repo_key)` | 服务端各种 VCS 调用的凭据底座 |
| `get_gitcode_token(user_id)` / `get_github_token(user_id)` | 服务端"以用户身份"执行（chat 场景后续承接） |
| `repogit` 服务端 askpass / `ensure_repo_base` | webhook 拉取、worktree 刷新、PR merge 等**服务端进程** git 操作 |

> 唯一变化：这些 token **不再出现在 agent 的执行环境变量里**。

## 3. 删除精确范围（agent 注入侧）

| 删除项 | 位置 |
|--------|------|
| 事件任务 agent env 注入 `BOT_GITCODE_TOKEN / BOT_GITHUB_TOKEN` | `executor._assemble_env`（原 `<repo_gitcode_token> → env` 复制） |
| Chat agent env 注入 `USER_GITCODE_TOKEN / USER_GITHUB_TOKEN` | `executor` chat 分支 + `chat.py` 相关 |
| agent 侧 `GIT_ASKPASS`（`repogit.askpass_path` 下发） | 避免 agent 裸 `git push` 取平台 token；统一 `grape git push` |
| prompt SECURITY「Use ONLY … ({_bot_token_env})」 | `prompt_builder` 改为「一律 grape git/git-repo/issues（带 GRAPE_SESSION_TOKEN），禁止裸调 VCS/使用平台 token」 |
| `scene_custom_env` 保留集 | 保留 `BOT_*/USER_*` 条目（防御，防自定义变量起这些名被注入），无副作用 |
| 相关测试 | `test_executor_token_env` 等语义反转为"断言不含平台 token" |

## 4. 两个硬前提（均已具备，2026-10-12 代码 + 单机生产实测）

1. **`grape git push/fetch/pull/clone` 在 agent 机上不再依赖 agent 注入的平台 token / GIT_ASKPASS** —— **成立**：
   - 实测：单机生产 `/usr/local/bin/grape` v0.3.3，`grape git --help` 输出为 **git 内置 usage（本地 git 包装）**；service 内凭据由 `GET /api/repos/{owner}/{repo}/git-credential`（repos.py:1337，**session-only → 会话 token（tk_）鉴权**、TTL 600s、一次性 Basic `username=x-access-token`、进程内不落盘）供应；
   - `grape git-repo` 为全 **session-only 平台透传**（无额外 token 配置，用会话 token）——两个层都不再需要 agent env 里的 `BOT_*/USER_*`。
2. **Chat「用户身份」** —— **服务端已实现**（见 §A 追加）：`tk_` 携带真实 `user_id`，`git-repo`/`git-credential` 按 `agent_type=="chat"` 取**该用户** GitCode token 执行。

### §A 追加：凭据解析与服务端以"会话主体"执行（实现核对 2026-10-12）

`git_repo_api._repo_token`（L81-130）—— session token 的 `agent_type` 决定平台凭据：

```
agent_type == "chat"  → 用户 token（user.get("id")/username → mod.user_token）优先，bot 兜底
其余（devops 事件）   → 仓库 bot token（mod.bot_token(repo_key)）优先，user 兜底
```

- token 签发：chat → `issue_session_token(session_key=conv:{conv_id}, repo_key, agent_type="chat", user_id=真实用户)`（chat.py:543）；devops → `task_id, agent_type=事件agent, user_id=0`。
- 即：**Chat 的 GRAPE_SESSION_TOKEN 已内嵌 user_id，服务端用它去执行用户 GitCode 身份的动作**（git-repo / git-credential），无需再给 agent 下发 `USER_*`。
- 生产实测：`grape git-repo --help` 全子命令标注 `(session-only)`，说明 CLI 只依赖会话 token。

---

## 5. 安全收益

- Agent 进程不再持有**平台长期凭据**，仅持 `tk_`（TTL 6h、`(repo_key, agent_type)` 作用域、任务终态 revoke、`GRAPE_CLI_MODE=WORKER` 过滤）；
- 泄露/越权/被拼接利用的爆炸半径显著收敛；平台 token 只存在于服务端受控进程。

## 6. 建议分步

| 阶段 | 内容 |
|------|------|
| **P1 迁移** | ✅ **已完成（2026-10-12 其他会话）**：技能全量切 `grape git/git-repo/issues`；grape-skills 60+ 处 `BOT_*/USER_*` 引用已修 |
| **P2 核心** | ✅ **已完成（2026-10-12 本会话）**：agent env 停发 `BOT_*/USER_*/GIT_ASKPASS`（executor `_assemble_env` 不再注入、prompt 指引改 grape CLI + GRAPE_SESSION_TOKEN、测试反转、保留集保留）；服务端 token 存储与使用全部不动。验证：全量测试失败节点与基线零差异 |
| **P3 可选** | ~~Chat 用户身份服务端承接~~ —— **服务端已实现**（`_repo_token` chat→用户 token 已上线）；仅需技能侧使用 CLI 即可，无独立开发量 |

## 7. 保留不变项（对照）

| 项 | 状态 |
|----|------|
| `DEVOPS_EVENT_TYPE`（事件类型，机制元数据） | 保留（技能×39 引用、hermes 任务类型判断） |
| `DEVOPS_SKILLS_DIR`（技能目录） | 保留（各引擎/worker 物化用） |
| `GRAPE_*`（SCENE_ID/WORK_BRANCH/LINKED_REPOS/ISSUE_ID 等） | 保留 |
| 服务端每仓/用户 token 存储 | 保留（§2 全表） |
| `GIT_ASKPASS` 服务端用途 | 保留（仅不下发 agent） |

## 8. 验证

| 用例 | 断言 |
|------|------|
| devops 任务（coding）| `env` 不含 `BOT_GITCODE_TOKEN/BOT_GITHUB_TOKEN/USER_GITCODE_TOKEN/USER_GITHUB_TOKEN/GIT_ASKPASS`；`grape git push` 仍能推码（服务端转发） |
| chat 会话 | `env` 不含 `USER_*`；`grape issues …` 以 bot 身份可建/回 Issue（P2 语义） |
| 事件分派 | `DEVOPS_EVENT_TYPE`/`DEVOPS_SKILLS_DIR` 注入不受影响 |
| 技能迁移 | grape-skills 中无残留 `$BOT_GITCODE_TOKEN` 等直接使用（除注释说明） |
| 服务端 | `/api/git-repo/*` 平台透传正常（服务端 `Repo().bot_token` 生效） |

---

## 9. 落档

- **结论（2026-10-12）**：`BOT_* / USER_*` 从 **agent 环境注入** 中移除可行且推荐（安全收益大）；
  **grape 服务端对 GitCode/GitHub token 的维护与使用不变、不可删**——它们是 `grape` CLI 全部实际动作的凭据底座。
- 本方案替换/收敛了上一版「保留 6 个 env」的保守结论：在 P1 技能迁移完成后，agent 注入侧可删，机制元数据（DEVOPS_EVENT_TYPE/SKILLS_DIR）与非注入平台侧全部保留。