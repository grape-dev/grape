# account 统一清理方案

## 1. 背景

当前代码中 `account` 参数存在 3 种不同的值（`"default"`、`repo_key`、仓库全名），语义混乱且 `default` 作为兜底账户深度参与业务流程，造成以下问题：

| 问题 | 影响 |
|------|------|
| `default` 兜底账户 | workspace history 目录混乱，会话上下文碎片化 |
| `default` 的 `bot_usernames=["hwc-dev-bot"]` | 导致 VOD Bot（hwc-dev-bot）在 GitCode 的评论被 self-comment 检测误跳过 |
| account 值不统一 | `history/{account}/` 分目录不一致，VOD 和 Chat 上下文不共享 |
| default 的 token 可能是过期/泛化 token | 仓库无 token 时用 default token 调 GitCode API，权限不足 |

## 2. 目标

统一 account 语义，彻底移除 `"default"` 兜底：

| 场景 | account 值 | 说明 |
|------|-----------|------|
| **Agent 执行 / workspace history** | 仓库全名 `owner/repo` | 每个仓库独立会话目录 |
| **Chat Bot 绑定 (feishu_bindings)** | bot app_id | 区分不同 Feishu bot，`chat_id + app_id` 唯一 |
| **VOD Bot** | 仓库全名 `owner/repo` | 已符合，保持现状 |

## 3. 修改清单

### 3.1 `src/im/handler_async.py`

| 行号 | 现状 | 修改 |
|------|------|------|
| 34 | `account = "default"` | `account = ctx.app_id`（用于 feishu_bindings 查询，区分 bot app） |
| 179 | `build_chat_prompt(..., account)` | 改传 `repo_key`（history 目录用仓库全名） |
| 203 | `stream_agent(..., account=account, ...)` | `account=repo_key` |
| 225 | `dump_history_to_file(repo_key, history, account)` | `account=repo_key` |

**注意**：`account`（绑定查询用，值=ctx.app_id）与 `repo_key`（agent 执行用，值=owner/repo）是两个不同变量，不可混淆。

### 3.2 `src/api/event_pipeline.py`

| 行号 | 现状 | 修改 |
|------|------|------|
| 41 | `run_pipeline(..., account="default")` | 默认值改为 `""`，由调用方传仓库全名 |
| 261 | `stream_agent(..., account="default", ...)` | `account=repo_key` |

### 3.3 `src/api/routes/webhook.py`

| 行号 | 现状 | 修改 |
|------|------|------|
| 53 | `run_pipeline(body, headers, repo_key_override=f"gitcode:{repo}")` | 增加 `account=repo` |
| 76 | `account = payload.get("account", "default")` | `account = payload.get("account", repository)` |
| 119,123 | evt 中 `account=account` | 保持一致（传 repository 派生值） |

### 3.4 `src/prompt_builder.py`

| 行号 | 现状 | 修改 |
|------|------|------|
| 272 | `account = evt.get("account", "default")` | `account = evt.get("account") or repo_key` |

### 3.5 `src/config.py`

| 行号 | 现状 | 修改 |
|------|------|------|
| 25 | `_DEFAULT_ACCOUNT = "default"` | 移除或改为 `""` |
| 28-29 | `get_account()` 兜底返回 default 账户 | 改为：无匹配返回 `{}` |

### 3.6 `src/agent/opencode.py` / `src/agent/codearts.py`

| 行号 | 现状 | 修改 |
|------|------|------|
| `get_env(account="default")` | 默认参数 | 改为 `get_env(account="")`，`get_account("")` 返回 `{}` |
| 24-25 | `acc.get("gitcode_api_token")` | 无 token 时不注入（已被 `repo_gitcode_token` 覆盖，无影响） |

## 4. 数据迁移

### 4.1 `feishu_bindings` 迁移（db_setup.py 新增）

```sql
-- 将 account='default' 的记录迁移到 Chat Bot 的 app_id
UPDATE feishu_bindings
SET account = (SELECT bot_app_id FROM im_bot_config WHERE bot_type='chat_bot' LIMIT 1)
WHERE account = 'default';
```

在 `_migrate_tables()` 或启动迁移中执行，保证部署后现有绑定可用。

### 4.2 workspace history 目录

`history/default/` → `history/{owner_repo}/`，历史文件不迁移（新会话用新目录，旧文件保留备份）。

## 5. 风险与注意

1. **Chat Bot account = ctx.app_id 后**：绑定查询 `WHERE chat_id=? AND account=ctx.app_id` 能正确工作，但已有绑定必须迁移到 app_id（见 4.1）
2. **get_account() 无兜底**：`opencode.py`/`codearts.py` 的 `get_env(account)` 若传空 account，`get_account("")` 返回 `{}`，token 为空——但 `stream_agent` 已用 `repo_gitcode_token` 覆盖，所以实际无影响
3. **VOD 已是 repo_key**：无需改动，仅确认
4. **多 Chat Bot 场景**：每个 Chat Bot 用各自 app_id 做绑定隔离，`chat_id + app_id` 唯一，不同 bot 同群可共存

## 6. 验证清单

- [ ] `handler_async.py` 绑定查询用 `ctx.app_id`，`resolve_repo`/`process_command` 正常工作
- [ ] `feishu_bindings` 迁移后，已有绑定在 Chat Bot 下可正常解析
- [ ] Event Pipeline 执行 agent 时 workspace history 目录为 `history/{owner_repo}/`
- [ ] A2A 触发 coding/implementation 时 account 正确传递
- [ ] VOD Bot 无回归
- [ ] 无 `"default"` 兜底残留（grep 验证）
