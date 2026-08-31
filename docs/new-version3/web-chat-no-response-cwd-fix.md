# Web Chat 无回应根因与修复方案（工作目录不一致）

> 版本：v1.0
> 日期：2026-08-31
> 状态：**已分析，待实施**
> 影响：Web Chat Agent（`/api/chat/submit`）发送消息无回应；飞书 IM 同一对话正常。

---

## 1. 现象

- Web Chat Agent（页面发送消息）**无回应**：SSE 为空、前端一直转圈。
- 同一 bot（chat_bot_cli_aae1bb5f）、同一仓库（hello-html2）、同一 opencode 引擎下，**飞书 IM 消息正常**（有回复、能推卡片）。

## 2. 日志/DB 证据

- `POST /api/chat/submit` 200，`[ChatTaskManager] agent_worker starting task=chat_...` 有日志；
- `opencode args: run ... --session ses_fb76...` + `cwd: /opt/grape_workspace/chat/7/518a...` 有日志；
- 之后**无任何执行输出 / agent_worker finished**，opencode 子进程存活数分钟无输出后消失；
- `opencode.db` 两会话对比：

| 会话 | `directory`(opencode.db) | 实际 cwd | messages/parts | 最近 tokens |
|---|---|---|---|---|
| web `ses_fb76...` | **`/home/grape/chat/7/518a`...** | `/opt/grape_workspace/chat/7/518a`... | 6 / 13 | **input:0, output:0（空转）** |
| 飞书 `ses_fdcb...` | `/opt/grape-workspaces/feishu/hello-html2/...` | 同名 | 17 / 48 | input:23610（正常） |

- 两目录 inode 不同（非同软链），均为空目录；
- 服务以 root 运行（`HOME=/root`）。

## 3. 根因

**Web Chat 的 cwd 与 opencode 会话历史归属目录不一致，导致 `--session` 恢复历史失败 → 模型收到空输入（tokens 0）→ 卡死无输出 → SSE 无响应。**

关键差异源于 `src/config.py` 新增的两套工作区基准：

```
WORKSPACE_DIR  = ~/grape-workspaces     // 连字符 + s，飞书/通用工作区
CHAT_CWD_BASE  = ~/grape_workspace      // 下划线无s，web chat（dev 合并新增）
```

- Web 路径：`_get_chat_cwd()` 用 `CHAT_CWD_BASE` → root 下 `/opt/grape_workspace/chat/{uid}/{conv}` 作为 cwd 传 opencode；
- 但该会话在 opencode.db 的 `directory` 是历史的 `/home/grape/chat/...`；
- cwd 与 session 归属错位 → opencode 恢复历史失败 → 模型调用空转。
- 飞书路径用 `WORKSPACE_DIR`（`/opt/grape-workspaces`），与 session 记录一致 → 正常。

`_remap_cwd`（agent.py）设计上会把 `CHAT_CWD_BASE` 映射到用户 home，但**本机 direct 执行未 remap**，root 直接用 `/opt/grape_workspace` 与历史 `/home/grape` 脱节。

## 4. 修复方向（总原则：同一对话，飞书与 Web 工作目录一致）

> **决策：采用方案 A + 一次性存量数据修正。** 方案 B/C 记为备选，不采用（B 保留双基准易再漂移；C 仅能修存量复发）。

### ✅ 方案 A（采用）：统一 Web Chat 工作区基准到 `WORKSPACE_DIR` + 含 repo 维度

> **前置约定（已确认）**：web chat 对话必然绑定仓库，`repo_key` 恒非空（无仓库则不会进入 chat 会话）。因此 `{owner}/{repo}` 段可安全假设存在，**无需空仓库 fallback 分支**。

**路径规范（决策）**：

```
WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}
# 例：~/grape-workspaces/Jason7602/hello-html2/chat/7/518a373d-1c77-4457-aac0-274253fdef7e/
```

- **repo 保留 owner/repo 两级目录**（非拍平 `_`）：`WORKSPACE_DIR/{owner}/{repo}/...`，层级清晰、与 GitCode 组织一致；owner/repo 由 `repo_key.py:parse_repo_key` 权威解析。
- **飞书与 Web 严格共用同一结构**：同一对话 = 同一 `conv_id`（双方都从 `chat_conversations` 取同一个 id），cwd 一律 `WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}`；**去掉飞书原有的 `feishu/` 段与 `sess` 尾巴差异**。
  - web `_get_chat_cwd(uid, repo_key, conv_id)` → 上述路径
  - 飞书 `handler_async.py:920` `wsd` 改用 `conv_id`（非 `sess.split("-")[-1]`），去掉 `feishu` 段 → 与 web 指向完全一致
- 取消 `CHAT_CWD_BASE` 独立基准（不再作为 web chat cwd 来源；若保留常量，默认对齐 `WORKSPACE_DIR`，绝不产生第二条路径）。
- 对本机 direct 执行做 cwd 归一：若历史 session directory 以 `/home/grape/` 开头，统一映射为当前用户 home 下对应路径（root → `/opt/grape-workspaces/{owner}/{repo}/chat/{uid}/{conv_id}`），保证 `--session` 能加载历史消息。
- 一次性迁移存量数据：把 opencode.db 中 web/飞书会话的 `directory` 从旧路径改写为新结构 `WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}`，并确保目录存在。

> **决定：repo 用 `{owner}/{repo}` 两级目录；飞书与 web 共用同一 `conv_id`、同一路径，去掉 `feishu/` 段。**

### 方案 B（备选，不采用）

- 保留 `CHAT_CWD_BASE = ~/grape_workspace`，本机 direct 统一走 `_remap_cwd`（逻辑基准→实际用户 home）。
- 缺点：与飞书 `WORKSPACE_DIR` 仍是两套基准（`grape_workspace` vs `grape-workspaces`），易再漂移，违背"同一对话目录一致"原则。

### 方案 C（备选，不采用）

- 仅修正存量 `session.directory` 对齐新 cwd。
- 缺点：只修当前卡住的会话，无法根治新会话的路径分歧。

### 一次性存量数据修正（方案 A 配套，必须做）

| 动作 | 说明 |
|---|---|
| 扫描 opencode.db `session` 表 | 找 `directory` 含 `chat/{uid}` / `feishu/` 且不以新结构的 web/飞书会话 |
| 解析 repo | 从 `chat_conversations.repo_key`（权威）取 `owner/repo` → `{owner}/{repo}` |
| 解析 conv_id | 飞书旧目录用 `feishu/{repo}/{sess}` 尾巴 → 归档到该对话最新 `conv_id`（经 `get_conversation_by_im(chat_id)` 或 `chat_messages` 反查）；web 旧目录直接有 `conv_id` |
| 改写 `directory` / `path` | 旧路径（`/home/grape/chat/{uid}`、`/opt/grape_workspace/chat/{uid}`、`WORKSPACE_DIR/chat/{uid}`、`WORKSPACE_DIR/feishu/{repo}/{sess}`）→ `WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}` |
| 确保目录存在 | 迁移后 cwd 为空目录可写（`os.makedirs`） |

> 迁移脚本需记录"旧→新"映射，供校验与回滚；迁移后再改动路径规范需严格评估存量成本。

## 5. 涉及文件（预估）

| 文件 | 改动 |
|---|---|
| `src/config.py` | `CHAT_CWD_BASE` 不再作为 web chat cwd 基准（默认对齐/移除），保证不产生第二基准 |
| `src/api/routes/chat.py` `_get_chat_cwd` | 改为 `WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}`（repo_key→`parse_repo_key` 解析 owner/repo） |
| `src/im/handler_async.py` `process_chat_async` `wsd` | 改用 `conv_id` 派生 cwd（去掉 `feishu/` 段与 `sess` 尾巴），与 web 严格一致 |
| `src/runtime/agent.py` `_remap_cwd` | 本机 direct 也应用目录归一（`/home/grape` ↔ 实际 user home） |
| 存量数据 | 一次性迁移 opencode.db `session.directory`（web/飞书会话）到新结构 |

## 6. 验证

1. Web Chat 发消息 → opencode 正常出 tokens → SSE 返回文本 → 前端有回复。
2. 同一对话飞书 + Web 均正常，**且两者 cwd 严格同一路径**（`WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}`，无 feishu 段差异）。
3. 历史会话（`ses_fb76`）恢复后能带历史上下文。
4. `agent_worker finished` 与 DB `add_message`（generate_status=2）正常落库。
5. 新会话 cwd 形如 `WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}`，`_remap_cwd` 保持幂等。

## 7. 测试用例看护设计（防回归）

> 目标：把"web 与飞书 cwd 一致 / web 能正常出 tokens / session 历史可恢复"固化为自动测试，防止未来再漂移。

### 7.1 单元测试（纯函数，`tests/test_chat_cwd.py`）

| 用例 | 断言 |
|---|---|
| `_get_chat_cwd(uid, repo_key, conv_id)` 返回 `WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}` | owner/repo 两级（非拍平）；repo_key 经 `parse_repo_key` 解析 |
| 目录被 `os.makedirs(exist_ok)` 创建 | 调用后目录存在 |
| repo_key 边界 | `gitcode:a/b` → `.../a/b/...`；缺 repo 字段时按既定约定处理（当前对话必绑仓库，不产生 fallback） |
| `_remap_cwd` 归一 | `/home/grape/{owner}/{repo}/chat/1/x` → `/opt/grape-workspaces/{owner}/{repo}/chat/1/x`（root 下）；`/opt/grape-workspaces/...` 原样保持 |
| `_remap_cwd` 幂等 | 已归一路径再归一不变 |

### 7.2 契约/集成测试（`tests/test_chat_cwd_contract.py`）

| 用例 | 断言 |
|---|---|
| web `chat_submit` 与飞书 handler 对**同一 conv_id 产生的 cwd 完全相等** | 同一对话两者 cwd 字符串一致（无 `feishu/` 段差异） |
| 飞书 `wsd` 改用 conv_id | mock handler，断言 `wsd == _get_chat_cwd(uid, repo_key, conv_id)` |
| `stream_agent` 收到的 cwd 与 `_get_chat_cwd` 一致 | mock `stream_agent` 断言实参 |
| 存量会话迁移函数正确改 `directory` | 对临时 sqlite 注入 `/home/grape/chat/{uid}/...` 与 `WORKSPACE_DIR/feishu/{repo}/{sess}`，迁移后均为 `WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}`，且幂等、可回滚 |
| repo/conv 解析 | 从 `chat_conversations.repo_key` 取 owner/repo；飞书旧目录经 im 反查得到 conv_id |

### 7.3 运行级回归（人工/脚本，`scripts/verify_chat_cwd.sh`）

1. 起本地 opencode probe：`opencode run --session <target> -p "你好"` 在 `WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}` 下能返回（tokens>0）。
2. Web Chat 发一条消息 → 日志出现 `opencode args` + 后续 `agent_worker finished status=done text_len>0`。
3. 前端显示回复；DB `chat_messages` 落 `generate_status=2`。
4. 同一 conv 用飞书再发 → 双方 cwd 完全相等（同一路径、同一 inode）。

### 7.4 测试目录与运行命令

```
tests/test_chat_cwd.py          # 纯函数
tests/test_chat_cwd_contract.py # 契约/迁移
scripts/verify_chat_cwd.sh      # 运行级验证（可选）
```

运行：`python -m pytest tests/test_chat_cwd.py tests/test_chat_cwd_contract.py -q`

---

## 附：关键代码位置

- `src/config.py:103-131`：`_resolve_workspace_dir` / `WORKSPACE_DIR` / `CHAT_CWD_BASE`
- `src/api/routes/chat.py:7-15`：`_get_chat_cwd`（改为 `WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}`）
- `src/im/handler_async.py:920`：`wsd = WORKSPACE_DIR/feishu/{repo}/{sess}`（改为 `conv_id` 派生，去 feishu 段）
- `src/repo_key.py:26`：`parse_repo_key`（owner/repo 解析）
- `src/runtime/agent.py:276-301`：`_remap_cwd`（目录映射契约，direct 未生效）
- `src/api/routes/chat.py:1144-1208`：`agent_fn`（将 cwd 传给 stream_agent）
- opencode 数据：`/root/.local/share/opencode/opencode.db`（会话 `directory` 字段）