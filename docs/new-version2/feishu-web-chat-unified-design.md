# Feishu Chat 与 Web Chat 统一设计方案

> 状态：设计方案（v0.2，待评审）｜ 分支：new-version2
> 目标：飞书对话与 Web 对话共享同一份 conversation，飞书侧可灵活选择加入不同仓库/任务的对话。
> v0.2 变更：重建至 docs/new-version2（对齐 2026-08-16 文档目录重组），并纳入同一时段"优化 session"变更影响评估（见 §9）。

## 1. 背景与目标

当前飞书 chat 与 web chat 各自维护一套对话，互不相通：

- Web 对话存在于 `chat_conversations` + `chat_messages`，网页可见完整对话。
- 飞书对话虽也建 `chat_conversations` 行（`im_channel_id=chat_id`），但消息走 history 文件、不回写 `chat_messages`，且一条飞书聊天被永久钉死在一个 conversation 上，用户无法切换。

**目标**：
1. 飞书和 Web 共享同一份 conversation（同一 `chat_conversations` + `chat_messages`）。
2. 飞书侧列出该用户（open_id）参与的仓库及其 conversation，可自由选择"加入"。
3. 飞书发送给 chat agent 的消息，Web 对话框可见；chat agent 的答复，Web 与飞书对话框均可见。
4. Web 用户消息因无法以用户名义代发到飞书，不呈现在飞书侧（可接受）。

## 2. 现状梳理（代码依据）

| 维度 | Web chat（chat.py） | Feishu chat（handler_async） |
|---|---|---|
| conversation 绑定 | 无 im_channel_id（按 repo/title/conv_type/issue_number 建行） | `get_conversation_by_im(chat_id)` 幂等建行，`im_channel_id=chat_id` 永久绑定（handler_async.py:440-445） |
| 消息存储 | 写 `chat_messages`（user/agent 双向） | 不写 `chat_messages`；上下文走 history 文件（`dump_history_to_file`，handler_async.py:545） |
| 答复 | `add_message(agent)` 存库，网页可见 | `reply_card` 回发飞书（handler_async.py:552），不回写 |
| 引擎选择 | 本机 direct / 队列（engine-select v2） | 同 engine-select v2（chat_runtime_id → resolve_local_engine） |
| 会话归属 | `user_id` = 操作的用户 | `user_id` = `get_user_id_by_app_id(ctx.app_id)`（bot 应用所有者）；`repo_key` = `/bind` 绑定的仓库 |

**核心差异**：`im_channel_id` 当前是"绑定"（钉死），方案改为"指针"（可移动）；飞书路径需补齐"读写 chat_messages"。

> **归属口径**：`get_user_conversations(user_id, repo_key)`（session_manager.py:460-470）只按 `user_id`(+可选 `repo_key`) 过滤、不区分来源，故飞书产生的会话行**今天已在 Chat Agent 页可见**（但为"空壳"，无消息内容）。合并方案补上的是消息完整性。

## 3. 总体设计

### 3.1 数据模型

`chat_conversations.im_channel_id` 从"永久绑定"降级为"当前飞书接入指针"：

- 任一时刻，一个飞书接入点（chat_id）只能指向一条 conversation。
- 飞书加入新 conversation 时，原子执行"清旧设新"：
  - 旧：`UPDATE chat_conversations SET im_channel_id='' WHERE id=旧conv_id`
  - 新：`UPDATE chat_conversations SET im_channel_id='<chat_id>' WHERE id=新conv_id`
  - 两语句在 `db_lock` 内同事务执行，防竞态。

> ⚠️ `im_channel_id` 为 TEXT，置空用 `''`（非 `0`）。
> ⚠️ 单字段只支持一个飞书接入点。若后续需要同时支持多个群聊，应引入独立指针表：

```sql
CREATE TABLE IF NOT EXISTS feishu_chat_bind (
    app_id TEXT NOT NULL,          -- bot 的 app_id
    chat_id TEXT NOT NULL,         -- 飞书 chat_id（P2P 时即用户 open_id）
    user_id INTEGER NOT NULL,      -- 绑定的平台用户
    conversation_id TEXT NOT NULL, -- → chat_conversations.id
    updated_at TEXT,
    PRIMARY KEY (app_id, chat_id)
);
```

> v0.1 建议范围：仅支持 P2P（个人）接入，先落地"移动 im_channel_id"，指针表作为 v0.2 演进项。
>
> **`feishu_chat_bind.chat_id` 与 `conversation.im_channel_id` 的关系**：两者存的是同一个值（飞书 chat_id），表达的是同一个关系（飞书聊天 ↔ conversation），但位置/方向/约束不同——`im_channel_id` 挂在 conversation 侧（"这条对话被哪条飞书聊天接入"，单字段仅一个接入点，切换时改写持久实体行）；`feishu_chat_bind` 以 `(app_id, chat_id)` 为主键独立存映射（"这条飞书聊天连到哪条对话"，支持多接入点，数据库层保证唯一）。**两者互斥二选一**：采用 bind 表后无需再改 `im_channel_id`（可保留作历史兼容）。

### 3.2 消息互通

统一以 `chat_messages`（conversation_id 维度）作为对话的唯一事实源：

- **飞书 → agent → 双方可见**
  1. 飞书消息到达 → 解析当前 conversation（指令/或绑定指针）→ `add_message(conv_id, 'user', text)`。
  2. agent 运行（direct/队列，同现有 engine-select）→ 得到最终答复 → `add_message(conv_id, 'agent', reply)` + 飞书侧 `reply_card/reply_text` 回发。
  3. Web 读取侧已现成，两端自动同步。

- **Web → agent → Web 可见 + agent 答复推飞书**
  1. Web 消息照常 `add_message(conv_id, 'user', text)` 入库。
  2. agent 答复 `add_message(conv_id, 'agent', reply)`。
  3. 若该 conversation 的 `im_channel_id` 非空（已被飞书接入）→ 用对应 bot 推送答复到飞书：
     - P2P：`chat_id == 用户 open_id`，可 `send_card_to_user(ctx, open_id, ...)` 或按 chat_id 发送。
     - 推送前缀建议 `[来自Web]`，缓解"飞书侧看到无来由答复"的困惑。

### 3.3 飞书侧对话列表与切换指令

新增指令（带前缀，避免吞掉正常聊天），在 handler_async 消息处理入口加分支：

| 指令 | 行为 |
|---|---|
| `/对话` | 列出该 open_id 对应 gitcode_user 参与的仓库 → 每个仓库的 conversation 列表（标注 conv_type=general/task、title、issue_number） |
| `/对话 <repo_key>` | 列出该仓库下该用户的 conversation |
| `/加入 <conversation_id>` | 原子切换指针：清旧 im_channel_id，新 conversation 写入当前 chat_id |

- 仓库来源：`managed_repos` 中该用户是 owner/collaborator 的仓库（用 `unified_user_mapping` 的 gitcode_user 关联）。
- conversation 来源：`get_user_conversations(user_id, repo_key)`（已返回 conv_type / issue_number，可区分任务/一般对话）。
- 首次无任何接入（`/对话` 无结果且 im_channel_id 空）→ 保持现状兜底：为该用户默认仓库创建/绑定一条 general conversation。

## 4. 关键变更点

| 模块 | 变更 |
|---|---|
| `src/im/handler_async.py` | ① 指令分支（/对话 /加入）；② 飞书消息+答复写 `chat_messages`；③ 切换指针原子操作；④ 上下文从 chat_messages 组装替代 history 文件 |
| `src/api/routes/chat.py` | web 答复路径：`im_channel_id` 非空时推飞书（`send_card_to_user`/发 chat_id） |
| `src/prompt_builder.py` | chat 上下文按 conversation 组装（复用 `build_task_chat_prompt` 模式） |
| `src/session_manager.py` | 新增 `join_feishu_chat(chat_id, conv_id)` 原子切换；`get_conversation_messages(conv_id, limit)` 供上下文 |
| `src/api/db_setup.py` | 存量 conversation 兼容（无需迁移列）；v0.2 建 `feishu_chat_bind` |

## 5. 风险与兼容

1. **存量飞书 conversation**：已绑定的 `im_channel_id` 无需迁移；首次 `/加入` 时自然"清旧"生效。
2. **上下文断片**：存量飞书历史在 history 文件，切到 chat_messages 组装后旧上下文不可见；建议切换口径"新对话从当前消息开始"，可后续补导入。
3. **竞态**：清旧设新必须 `db_lock` 同事务；两端同时发消息需容忍乱序（以 seq 排序展示）。
4. **任务对话混入**：`conv_type='task'` 的「Issue #N」对话可被飞书 join 后直接对聊，与 notify 的"任务通知"职责重叠——注意不与任务卡片流程冲突。
5. **UX 不对称**：飞书侧会看到 web 触发的 agent 答复（无对应 web 用户消息），用 `[来自Web]` 前缀缓解。

## 6. 测试影响与测试用例设计

### 6.1 测试影响

- `tests/test_chat_task_scene.py`：conv_type 分流不变，需补 join 切换用例。
- `tests/test_web_chat_tooluse.py`：web 答复推飞书分支新增用例。
- `tests/test_feishu_handler.py`：指令解析、指针切换、chat_messages 写入用例。
- 新测试文件：`tests/test_feishu_chat_unified.py`（join/leave/列表/推送）。

### 6.2 测试用例设计（`test_feishu_chat_unified.py`）

| # | 用例 | 前置 | 断言 |
|---|---|---|---|
| T01 | `join_feishu_chat` 首次接入：无绑定时 join | 用户有 conversation A（im_channel_id=''） | A.im_channel_id=chat_id；返回成功 |
| T02 | 切换指针：已有绑定再 join 新对话 | A 已绑 chat_id，再 join B | A.im_channel_id=''，B.im_channel_id=chat_id（原子，同事务） |
| T03 | 重复 join 同一对话 | A 已绑 chat_id，再 join A | 幂等：A.im_channel_id 不变，不产生脏数据 |
| T04 | 并发 join 竞态 | 两任务同时 join B、C | 终态只有一个 conversation 持有 chat_id（db_lock 保证） |
| T05 | `/对话` 列表：列出该用户仓库与对话 | 用户有 general + task 对话各一 | 返回按仓库分组；标注 conv_type/issue_number/title |
| T06 | `/加入 <conversation_id>` 正常切换 | 未绑定 | 成功且返回当前对话信息 |
| T07 | `/加入 <不存在的id>` | — | 返回错误提示，指针不变 |
| T08 | 未绑定用户普通消息兜底 | 无任何接入 | 自动创建/绑定默认仓库 general 对话 |
| T09 | 飞书消息写 chat_messages | 飞书发消息 | `chat_messages` 新增 role=user 一行，conversation_id 正确 |
| T10 | 飞书 agent 答复回写+回发 | agent 完成 | `chat_messages` 新增 role=agent；飞书 `reply_card` 被调用 |
| T11 | web 消息不推飞书用户侧 | 普通 web 消息 | 不调用 send_card_to_user（仅入库） |
| T12 | web agent 答复推飞书 | conversation 已绑定（im_channel_id 非空） | agent 答复入库 + send_card_to_user 被调用（含 `[来自Web]` 前缀） |
| T13 | web agent 答复不推飞书 | conversation 未绑定 | 仅入库，不调用飞书 API |
| T14 | 上下文组装：按 conversation 消息组装 | conversation 有 N 条消息 | prompt 含最近 K 条（role+content），不再读 history 文件 |
| T15 | 指令不吞正常聊天 | 普通文本不以 `/` 开头 | 走正常 chat 处理，不触发指令分支 |
| T16 | 存量兼容：已绑定 conversation | 旧数据 A 已绑 chat_id | join 时旧指针被正确清空，无异常 |

## 7. 实施步骤（每步独立可测）

1. **共享消息存储**：飞书路径改读写 `chat_messages`（不动切换逻辑）——先让两边对话互通。
2. **指针切换**：`join_feishu_chat` 原子切换 + `/对话` `/加入` 指令。
3. **web→飞书推送**：web 答复检测 `im_channel_id` 非空则推送飞书。
4. （v0.2）`feishu_chat_bind` 指针表，支持多飞书接入点。

## 8. 结论

方向可行且价值大：把"任务对话 + 飞书对话 + Web 对话"统一为一个 conversation 实体，`im_channel_id` 作为可移动接入指针。建议按 §7 分步实施，v0.1 聚焦 P2P 场景。

## 9. 关联变更影响评估（2026-08-16 "优化 session"）

本方案重新对齐时评估了同一时段其它 session 的提交，结论如下：

| 提交 | 变更 | 对方案影响 |
|---|---|---|
| `6baf5e3b` fix(feishu) OAuth 空 open_id 不覆盖绑定 | `set_feishu_open_id` 空 open_id（OAuth 登录只带昵称）时保留已有 feishu_open_id/feishu_name，避免清掉 p2p 确认的绑定 | ✅ **增强，无冲突**。§3.3 的仓库来源依赖 `unified_user_mapping`（open_id→gitcode_user），该修复让 p2p 确认的绑定不再被后续登录清空 → `/对话` 列表来源更可靠。方案无需改动 |
| `ba3a8580` docs 目录重组 | `docs/new-version2/`→`docs/new-version1/`、根目录→`docs/new-version/`；database-tables-overview 补 conv_type/issue_number | ✅ 本文档重建至 `docs/new-version2/`；DDL 文档已含 `conv_type`/`issue_number`，与本方案 §3.1/§3.3 引用一致 |
| `4470352a`/`2eb18bae` 刷新 database-tables / user-workflow 并拷贝至 docs/new-version2 | DDL 与工作流文档对齐最新实现 | ✅ 无冲突；后续实现可对照 `docs/new-version2/database-tables-overview.md` |
| `d068cd3a` feat(ui) Event 对话框 markdown | events.js 消息用 renderMarkdown（对齐 chat 对话框） | ✅ 纯前端，无影响 |

## 10. 事故修复：未绑定仓库兜底误用全局 `ws_feishu_app_id`（2026-08-17）

### 10.1 事故现象
用户 `gcw_7pdiBkYp`（uid70，已配置个人飞书、已绑定 open_id `ou_d417...`）在 P2P 私聊自己的 bot（`cli_aa08d82d`）发首条"你好"时：
1. bot 没有询问"你是谁"；
2. 却把 **Jason7602 所绑定的仓库列表**发给了 gcw 的飞书用户。

### 10.2 根因
1. **未询问"你是谁"（非 bug）**：
   - P2P 身份确认流程（`handler_async.py:237`）只跑 `chat_type=='p2p'`；本消息确为 P2P（Feishu API 拒绝把 `oc_d1f176...` 当群聊查询 → 私聊会话 id 也是 `oc_` 前缀，`chat_type` 字段才是权威）；
   - 且 gcw **已绑定**（`unified_user_mapping.feishu_open_id` 有值）→ 确认被跳过。设计正确。
2. **发 Jason7602 仓库（真 bug）**：该聊天无 `repo_im_group_binding` → `resolve_repo` 返回空 → 走"未绑定仓库"兜底（`handler_async.py:493`）：
   ```python
   meta.ws_feishu_app_id  = cli_aae1bb5fb3b81bde   # Jason7602 的 bot（冻结历史值）
   user_id = get_user_id_by_app_id(ws_feishu_app_id)  # → 7 = Jason7602
   repos = get_managed_repos(7)                       # → Jason7602 的全部托管仓库
   ```
   → 用**全局 `ws_feishu_app_id` 的属主（Jason7602）**列仓库，而非当前聊天 bot 的属主（gcw, uid70）。

### 10.3 `ws_feishu_app_id` 来源（遗留单 bot hack）
- 历史上由**旧 WebSocket 启动流程**写入（`im-ws-async-redesign.md` / `im-ws-multi-boot-fix.md`）：
  ```python
  INSERT OR REPLACE INTO meta (key, value) VALUES ('ws_feishu_app_id', <单 bot app_id>)
  ```
- 单用户/单 bot 时代，"WS 的 bot"即系统主用户（Jason7602）的 bot，未绑定兜底用它反推"当前用户"。
- **多 bot 重构（BotManager 时代）已删除写入逻辑，仅剩 `handler_async.py:493` 这一处死读取**，meta 值冻结为 Jason7602 的 bot。**当前设计没有"全局飞书属主"，业务流程也不需要**。

### 10.4 修复方案（待实施）
1. **未绑定兜底改用当前 bot 属主**（与 `/repo` 一致）：
   - `uid = get_user_id_by_app_id(account)`（account = 当前聊天 bot app_id，P2P/群均可）；
   - `repos = get_user_repos(uid)`（或 `get_managed_repos(uid)`），不再读 `meta.ws_feishu_app_id`；
   - 空则提示"无已同步仓库，先 `/repo` 或网页加入"。
2. **数据清理**：删除 `meta.ws_feishu_app_id` 残留行（可选，随代码一起）。
3. **群聊未绑定引导**：回复 `未绑定仓库，发送 /repo 选择并绑定`（替换现有"列仓库列表"文案，避免把别家仓库当候选）。

### 10.5 测试用例设计

| # | 用例 | 前置 | 断言 |
|---|---|---|---|
| W01 | 未绑定兜底用当前 bot 属主仓库 | bot 属主 uid=70 有仓库；`ws_feishu_app_id`=Jason7602 | 兜底列出 **uid70 的仓库**，不出现 Jason7602 仓库 |
| W02 | 无 meta.ws_feishu_app_id 也能工作 | meta 无该 key | 兜底仍按当前 bot 属主正常返回 |
| W03 | 未绑定引导文案 | 无任何仓库 | 回复含 `/repo` 引导，不列仓库列表 |
| W04 | P2P 已绑定不询问（回归） | gcw 已绑定 | 首条消息不进确认流程、正常进入 chat |

### 10.6 实施步骤
1. `handler_async.py` 未绑定兜底：`ws_feishu_app_id` → `get_user_id_by_app_id(account)` + `get_user_repos(uid)`，文案改 `/repo` 引导。
2. 删除 meta `ws_feishu_app_id` 残留行（可选）。
3. 测试 W01-W04 + 回归 → 部署。
