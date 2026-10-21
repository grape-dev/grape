# Token 注入链路与脱敏

> 提取自设计文档 `repo-token-config-design.md` R20.24–R20.25（第 841–903 行）。

---

## 需求项

### R20.24 token 注入 3 层链路

TokenResolver 改造只覆盖了"从 DB 获取 token"，未覆盖"把 token 注入 agent 进程"的完整链路。agent 进程通过 **3 层链路**获取 token：

1. **task 行**写入 `repo_gitcode_token`（pipeline 创建 task 时，`_get_task_repo_token`）
2. **env_extra** 注入 `BOT_GITCODE_TOKEN` / `BOT_GITHUB_TOKEN` 环境变量（`runtime/agent.py` line 1878: `bot_token=_get_task_token(task)`）
3. **GIT_ASKPASS** 脚本从环境变量读 token 做 git 认证（`repogit.py` line 270-289: `_write_askpass`）

- **R20.24.1**：token 注入链路 3 层全覆盖：task 行 → env_extra → GIT_ASKPASS。TokenResolver 改造后每层都要适配。
- **R20.24.2**：env_extra 注入 `BOT_GITCODE_TOKEN` / `BOT_GITHUB_TOKEN` 时，如果 `TokenResolver.get_token()` 返回空串（短路）→ env 变量为空 → GIT_ASKPASS 读空 → 需 fallback 到 `USER_*_TOKEN`。
- **R20.24.3**：GIT_ASKPASS fallback 链修正——路径 B 仓库 `BOT_*_TOKEN` 为空时 fallback 到 `USER_*_TOKEN`。
- **R20.24.4**：agent 同时收到 `bot_token` 和 `user_token` 两个参数（`runtime/agent.py` line 1878-1879）——`bot_token` 注入 `BOT_*_TOKEN`，`user_token` 注入 `USER_*_TOKEN`。路径 B 仓库 `bot_token=空` 但 `user_token` 非空 → GIT_ASKPASS fallback 到 `USER_*_TOKEN` → git clone 成功。
- **R20.24.5**：env_extra 注入逻辑不经过 TokenResolver——env 变量值来自 task 行 `repo_gitcode_token`（bot_token）和 runtime 实时获取（user_token）。TokenResolver 改造影响 task 行写入的 `repo_gitcode_token` 值，不影响 env 注入逻辑本身。
- **R20.24.6**：路径 B 仓库 `BOT_*_TOKEN=空` 时不报错——agent 通过 grape CLI（`GRAPE_SESSION_TOKEN`）调 API 不依赖 `BOT_*_TOKEN`，只有 git clone/push 依赖 GIT_ASKPASS。GIT_ASKPASS fallback 到 `USER_*_TOKEN` 保证 git 操作可用。

### R20.25 token 脱敏全覆盖

当前 `result_text` 脱敏只 replace `repo_gitcode_token`（`event_pipeline.py` line 3310, 3418），**不脱 `user_token`**。agent 可能在输出中复述 `USER_GITCODE_TOKEN` 的值 → 写入 `agent_tasks.result_text` → dashboard 可见 → token 泄露。

- **R20.25.1**：`result_text` 脱敏覆盖所有注入的 token 值——`repo_gitcode_token` + `user_token` + `webhook_secret`（R24.7 扩展：webhook_secret 从 `managed_repos.webhook_secret` 查询加入脱敏列表）。
- **R20.25.2**：`agent_logs` 脱敏同步覆盖（line 3310 的 `_msg.replace` 也脱 `user_token`）。
- **R20.25.3**：fail_task 的 `comment_token` 脱敏——fail_task 发 Issue 评论时评论内容不含 token 原文（已有 grape-auto-comment 标记，但评论内容可能含 token 复述）。
- **R20.25.4**：脱敏列表从 task 行动态构建——`repo_gitcode_token`（task 行值）+ `user_token`（`runtime/agent.py` 实时获取值），两者都可能被 agent 复述。
- **R20.25.5**：R19.1.3 "error_code 中不包含 token 片段"已有，但脱敏范围需扩展到 `result_text` / `agent_logs` 的所有 token 值。

---

## 设计方案

### 3 层注入链路详解

agent 进程获取 token 的完整链路：

| 层级 | 职责 | 代码位置 | 数据来源 |
|------|------|----------|----------|
| 1. task 行 | pipeline 创建 task 时写入 `repo_gitcode_token` | `_get_task_repo_token` | TokenResolver → DB |
| 2. env_extra | 注入 `BOT_*_TOKEN` / `USER_*_TOKEN` 环境变量 | `runtime/agent.py` line 1878 | task 行值（bot）+ runtime 实时获取（user） |
| 3. GIT_ASKPASS | 脚本从 env 读 token 做 git 认证 | `repogit.py` line 270-289 `_write_askpass` | 环境变量 |

TokenResolver 改造后，每层都要适配路径 B 的"token 短路返回空串"行为。

### 路径 B GIT_ASKPASS bug

路径 B 仓库 task 行 `repo_gitcode_token=空`（防过期 token 落库）→ agent 运行时 `BOT_GITCODE_TOKEN=空` → GIT_ASKPASS 脚本读 `BOT_GITCODE_TOKEN=空` → **git clone/push 失败**。

当前 GIT_ASKPASS fallback 链（`repogit.py` line 283）：
```bash
github:  ${BOT_GITHUB_TOKEN:-${BOT_TOKEN:-$BOT_GITCODE_TOKEN}}
gitcode:  ${BOT_GITCODE_TOKEN:-${BOT_TOKEN:-$BOT_GITHUB_TOKEN}}
```
只读 `BOT_*_TOKEN` 和 `BOT_TOKEN`，**不 fallback 到 `USER_*_TOKEN`**。路径 B 仓库 `USER_GITCODE_TOKEN` 有值（用户 token）但 GIT_ASKPASS 不读它。

### 修正后 fallback 链

```bash
github:  ${BOT_GITHUB_TOKEN:-${USER_GITHUB_TOKEN:-${BOT_TOKEN:-$BOT_GITCODE_TOKEN}}}
gitcode:  ${BOT_GITCODE_TOKEN:-${USER_GITCODE_TOKEN:-${BOT_TOKEN:-$BOT_GITHUB_TOKEN}}}
```

新增 `USER_*_TOKEN` 作为第二优先级 fallback——`BOT_*_TOKEN` 为空时尝试用户 token，再 fallback 到 `BOT_TOKEN` 全局 bot token。

### agent 同时收 bot_token + user_token 两参数

`runtime/agent.py` line 1878-1879 同时接收两个 token 参数：

- `bot_token` → 注入 `BOT_GITCODE_TOKEN` / `BOT_GITHUB_TOKEN`
- `user_token` → 注入 `USER_GITCODE_TOKEN` / `USER_GITHUB_TOKEN`

路径 B 仓库 `bot_token=空` 但 `user_token` 非空 → GIT_ASKPASS fallback 到 `USER_*_TOKEN` → git clone 成功。

agent 通过 grape CLI（`GRAPE_SESSION_TOKEN`）调 API 不依赖 `BOT_*_TOKEN`，只有 git clone/push 依赖 GIT_ASKPASS——因此 `BOT_*_TOKEN=空` 不影响 API 调用。

### 脱敏列表动态构建 redact_tokens 代码示例

当前代码（只脱 `repo_gitcode_token`）：
```python
# event_pipeline.py line 3418
if repo_gitcode_token and result_text:
    result_text = result_text.replace(repo_gitcode_token, "[REDACTED]")
# 只脱 repo_gitcode_token，user_token 未脱
```

修正后脱敏逻辑（动态构建列表，覆盖所有注入 token）：
```python
_redact_tokens = []
if repo_gitcode_token:
    _redact_tokens.append(repo_gitcode_token)
if user_token:
    _redact_tokens.append(user_token)
for _tok in _redact_tokens:
    result_text = result_text.replace(_tok, "[REDACTED]")
    _msg = _msg.replace(_tok, "[REDACTED]")
```

脱敏列表从 task 行值 + runtime 实时获取值动态构建，确保 agent 复述的任何 token 都被 `[REDACTED]` 替换。

---

## 涉及代码文件

| 文件 | 位置 | 职责 |
|------|------|------|
| `src/runtime/agent.py` | line 1878-1879 | agent 同时收 `bot_token` + `user_token` 两参数，注入 `BOT_*_TOKEN` / `USER_*_TOKEN` |
| `src/runtime/repogit.py` | `_write_askpass` (line 270-289) | GIT_ASKPASS 脚本生成，fallback 链修正 |
| `src/runtime/repogit.py` | `_run_git` | git clone/push 执行，依赖 GIT_ASKPASS |
| `src/api/event_pipeline.py` | line 3310 | `agent_logs` 脱敏（`_msg.replace`），需扩展覆盖 `user_token` |
| `src/api/event_pipeline.py` | line 3418 | `result_text` 脱敏，需扩展覆盖 `user_token` |
| `src/prompt_builder.py` | `_vcs_bot_token_env` | env_extra 注入逻辑，`BOT_*_TOKEN` / `USER_*_TOKEN` 环境变量构建 |

---

## 验收标准

- [ ] 路径 B 仓库 `BOT_*_TOKEN=空` 时 git clone/push 成功（fallback 到 `USER_*_TOKEN`）
- [ ] GIT_ASKPASS fallback 链包含 `USER_*_TOKEN` 层级
- [ ] agent 同时收到 `bot_token` + `user_token` 两个参数
- [ ] env_extra 注入 `BOT_*_TOKEN` 和 `USER_*_TOKEN` 两组环境变量
- [ ] `result_text` 脱敏覆盖 `repo_gitcode_token` + `user_token`
- [ ] `agent_logs` 脱敏覆盖 `repo_gitcode_token` + `user_token`
- [ ] fail_task Issue 评论内容不含 token 原文
- [ ] 脱敏列表从 task 行值 + runtime 实时获取值动态构建
- [ ] 路径 B 仓库 `BOT_*_TOKEN=空` 时不报错（grape CLI 调 API 正常）

---

## 依赖场景

- **路径 B 仓库**：task 行 `repo_gitcode_token=空`（防过期 token 落库），依赖 GIT_ASKPASS fallback 到用户 token 完成 git 操作
- **agent 输出复述 token**：agent 可能在 `result_text` / `agent_logs` 中复述环境变量值，需脱敏覆盖
- **TokenResolver 改造**（R20.7）：TokenResolver 短路返回空串的行为影响 3 层注入链路每层，需逐层适配
- **dashboard 可见性**：`result_text` 通过 dashboard 展示给用户，token 泄露风险需脱敏覆盖
