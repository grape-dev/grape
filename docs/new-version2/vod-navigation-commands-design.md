# VOD Agent 飞书命令卡片化优化方案（编号选择，对齐 Chat）

> 状态：设计方案（v0.1，待评审）｜ 分支：new-version2
> 目标：把 VOD bot 的纯文本命令升级为与 Chat 一致的"卡片 + 编号选择"，`/repo` 列该 bot 名下 VOD 仓库编号绑定。

## 1. 现状（VOD 绑定流程与命令）

### 1.1 绑定流程
- **扫码 onboard（per-repo）**：`POST /feishu/onboard/start`，`bot_type=vod_bot` + `repo_key` → 创建新 App → `_apply_vod_credentials` 落库 `devops_agent_session`（repo_key + agent_type='vod'）的 `im_app_id/im_app_secret`。**一个 VOD bot ↔ 一个仓库**。
- **消息处理**（`vod_handler_async.process_vod_async`）：命令区 → 普通消息区：
  - 查 `vod_conversations`（im_chat_id）→ 有绑定处理；
  - 无绑定 → 该 bot 名下 VOD 仓库数：0→提示联系管理员；1→**自动绑定**；多个→纯文本列仓库 + 提示 `/bind`。

### 1.2 当前命令（纯文本 `reply_text`）
| 命令 | 行为 |
|---|---|
| `/help` | 列出命令 |
| `/status` | 查看当前绑定仓库 |
| `/bind owner/repo` | 绑定当前群到仓库（需该仓库已配 VOD 且 im_app_id 匹配） |
| `/unbind` | 解绑 |
| `/clean` / `/reset` | 清除会话 |

## 2. 目标

1. **命令卡片化**：`/repo` `/where` `/help` 用 `reply_card`（lark_md 子集）。
2. **编号选择**：`/repo` 列该 bot 名下**已配置 VOD 的仓库**，回复编号即绑定（写 `vod_conversations`）。
3. **多仓库自动弹卡片**：无绑定且该 bot 名下 VOD 仓库 ≥1 时，自动弹 `/repo` 卡片（替代纯文本列仓库）。
4. 与 Chat 复用同一套 pending 状态机 / 候选卡片 / 分页。

## 3. 命令设计

| 命令 | 行为 | 候选 → 编号动作 |
|---|---|---|
| `/repo` | 卡片列出**该 bot 名下已配置 VOD 的仓库**（编号 + full_name；当前已绑定标注「绑定中」） | 回复编号 → 为该群绑定该仓库（写 vod_conversations，卡片确认） |
| `/where` | 卡片显示当前群绑定仓库 + 所属 VOD Agent（**与 chat 一致用 `/where`**） | —（卡片） |
| `/help` | 卡片列出全部命令 | —（卡片） |
| `/bind owner/repo` | **弃用**（保留兼容，不推荐使用；建议用 `/repo` 编号选择） | — |

> **命令调整**：`/status` 更名 `/where`（与 chat bot 一致）；`/clean`、`/reset`、`/unbind` **去掉**（VOD 无需手动清会话/解绑，`/repo` 可重绑）；`/bind` 弃用（保留兼容）。最终命令集：**`/repo /where /help`**（+ 兼容 `/bind`）。

### 3.1 候选范围（与 Chat 差异）
- Chat `/repo`：`get_managed_repos(uid)`（该用户托管仓库）。
- **VOD `/repo`：`_list_vod_repos(ctx.app_id)`**（`devops_agent_session WHERE agent_type='vod' AND im_app_id=当前bot`）——VOD 是 per-bot 的，只列**绑定到当前 bot 的仓库**。

### 3.2 状态机（复用）
- 复用 `handler_async._pending_selections` / `_set_pending_selection` / `_get_pending_selection` / `_clear_pending_selection` / `_candidate_card` / `_PENDING_SELECT_PAGE_SIZE`（按 chat_id 隔离、TTL 120s、`0` 取消、新命令覆盖）。
- 新增 action：**`vod_repo`**（VOD handler 处理编号选择）。
- VOD `process_vod_async`：命令区前，`text.strip().isdigit()` 且存在未过期 pending → 走 `_handle_vod_selection`。

### 3.3 自动绑定逻辑升级
无绑定（无 `vod_conversations` 行）时按该 bot 名下 VOD 仓库数：
- **0** → 卡片提示"未绑定仓库，请联系管理员配置"；
- **1** → 自动绑定（沿用）；
- **≥2** → **自动弹 `/repo` 卡片**（编号选择，替代纯文本列表）。

## 4. 关键变更点

| 模块 | 变更 |
|---|---|
| `src/im/vod_handler_async.py` | ① 命令改 `reply_card`（/repo /status /clean /help）；② `/repo` 候选 = `_list_vod_repos(ctx.app_id)` + pending；③ 数字选择分发 `_handle_vod_selection`（绑定 vod_conversations）；④ 自动弹卡片逻辑 |
| `src/im/handler_async.py` | `_pending_selections`/`_candidate_card` 等**导出复用**（或提取共享模块）；无逻辑改动 |
| 无 DB 变更 | vod_conversations / devops_agent_session 已存在 |

## 5. 边界与风险

1. **多 bot 隔离**：pending 按 chat_id 键，天然隔离；候选按 `ctx.app_id` 过滤，不串 bot。
2. **`/bind` 兼容**：保留手打路径，不影响旧群。
3. **卡片≤2000 字符**：仓库多时分页（复用 `_PENDING_SELECT_PAGE_SIZE`=20）。
4. **与 Chat 共用 pending 状态机**：VOD 与 Chat 是不同 bot，chat_id 不同，不会互相覆盖；但同 chat 若同时被 chat_bot 与 vod_bot 接入（罕见），pending 键冲突——可接受（新命令覆盖旧）。

## 6. 测试用例设计

| # | 用例 | 前置 | 断言 |
|---|---|---|---|
| V01 | `/repo` 列该 bot 名下 VOD 仓库 | bot 绑定 ≥2 仓库 | 卡片含编号+full_name+「绑定中」；pending action=vod_repo |
| V02 | 回复编号绑定仓库 | 有 vod_repo pending | 写 vod_conversations；卡片确认；pending 清除 |
| V03 | 无绑定且 1 仓库自动绑定（回归） | bot 名下 1 仓库 | 自动绑定 + 卡片确认 |
| V04 | 无绑定且 ≥2 仓库自动弹卡片 | bot 名下 2 仓库 | 自动 reply_card（/repo 候选），非纯文本 |
| V05 | 无绑定且 0 仓库提示 | 无仓库 | 卡片提示联系管理员 |
| V06 | 回复 `0` 取消 | 有 vod_repo pending | 取消，不绑定 |
| V07 | `/where` 卡片 | 已绑定 | 卡片含仓库；未绑定提示 |
| V08 | `/help` 卡片列命令 | — | 卡片含 `/repo /where /help` |
| V09 | `/clean`/`/reset`/`/unbind` 不再响应 | — | 按普通消息处理（不触发清会话/解绑） |
| V10 | 与 Chat pending 互不干扰 | chat 有 pending + vod 群选择 | 各自独立生效 |

## 7. 实施步骤（每步独立可测）

1. **复用 pending 状态机**：`vod_handler_async` 导入 handler_async 的 `_pending_selections/_set/_get/_clear/_candidate_card/_PENDING_SELECT_PAGE_SIZE`（或提取共享模块）。
2. **`/repo` 卡片 + 编号绑定**：候选=`_list_vod_repos(ctx.app_id)`、pending、`_handle_vod_selection` 写 vod_conversations。
3. **数字分发 + 自动弹卡片**：`process_vod_async` 纯数字先查 pending；无绑定多仓库自动弹 `/repo` 卡片。
4. **`/where` `/help` 卡片化；移除 `/clean`/`/reset`/`/unbind`；`/bind` 标记弃用**。
5. 测试 V01-V10 + 回归（vod_handler / feishu_handler / nav）→ 部署。

## 8. 结论

把 VOD bot 的命令升级为"卡片 + 编号选择"，`/repo` 列当前 bot 名下 VOD 仓库编号绑定，多仓库自动弹卡片，与 Chat 复用同一套交互状态机。改动集中在 `vod_handler_async.py`，无 DB 变更。
