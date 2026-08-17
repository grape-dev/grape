# Feishu open_id app 级作用域修复方案（unified_user_mapping 增加 app_id）

> 状态：设计方案（v0.1，待评审）｜ 分支：new-version2
> 痛点：Feishu open_id 是 app 级（app-scoped），同一人在不同飞书 App 下 open_id 不同；`unified_user_mapping` 未记录 open_id 所属的 app_id，换绑新 bot 后可能用旧 app 的 open_id 发通知 → 静默失败。

## 1. 现状与问题

### 1.1 当前绑定写入（`feishu_onboard.py:239-247`）
```python
open_id = result.user_info.open_id        # 新 App 的 open_id（app 级）
set_feishu_open_id(repo_key, username, open_id, gitcode_nickname=username)
log.info(f"Feishu onboard: bound open_id for {username}")
```
- 扫码每次都写入 open_id（覆盖旧的）；
- **`unified_user_mapping` 无 `app_id` 列**，也不写 `feishu_name`（onboard 只传 gitcode_nickname）。

### 1.2 核心问题
- **Feishu open_id 是 app 级**：同一人在 App A 下的 open_id 在 App B 下无效。
- 通知/`send_card_to_user` 走用户**当前 chat bot**（`chat_agent_session.im_app_id`）→ 要求 open_id 必须是该 app 的。
- **手动配置**（填 App ID/Secret，不扫码）不刷新 open_id → 存的是旧 app 的 open_id → 新 bot 通知用错 open_id → 静默失败。
- onboard 虽刷新 open_id，但无 app_id 无法校验"新 app 的 open_id 与存储是否同源"。

## 2. 修复方案

### 2.1 `unified_user_mapping` 增加 `app_id` 列
```sql
ALTER TABLE unified_user_mapping ADD COLUMN app_id TEXT DEFAULT '';
```
DDL 同步补入 `CREATE TABLE`（`session_manager.py:24`）。

### 2.2 重命名 `set_feishu_open_id` → `update_unified_user_info` + 换 app 清空语义
> **命名修正**：现名 `set_feishu_open_id` 与语义不符——该函数管理整行 `unified_user_mapping`（gitcode_user/gitcode_nickname/feishu_open_id/feishu_name/app_id/updated_at），并承担"open_id 空保留已有"、"换 app 清空重确认"等更新逻辑。改为 `update_unified_user_info(gitcode_user, open_id="", feishu_name="", gitcode_nickname="", app_id="")`（顺带移除函数体内未使用的 `repo_key` 首参），并同步更新 4 处调用方（handler_async / feishu_async / feishu_onboard / auth）与 3 个测试文件。

- 新签名：`update_unified_user_info(gitcode_user, open_id="", feishu_name="", gitcode_nickname="", app_id="")`。
- 判定逻辑（**"清空"仅在"无新 open_id 且换 app"时触发**——扫码/sync/p2p 确认带 open_id 时直接写入，不因 app 变化清空）：
  ```
  if open_id 非空:                    → 写 open_id + feishu_name(有则写) + app_id(有则写)（扫码/sync/确认均带 open_id）
  elif app_id 非空 且 != 存储 app_id: → 刷新 app_id、清空 open_id/feishu_name（手动配置换 app，需重确认）
  else:                               → 仅刷 gitcode_nickname/updated_at（OAuth，保留 open_id/app_id）
  ```

### 2.3 调用方传入 app_id
| 调用方 | app_id 来源 | feishu_name |
|---|---|---|
| `feishu_onboard.py:243`（扫码） | 新创建的 `app_id`（扫码结果 client_id） | 未传（仅 gitcode_nickname） |
| `handler_async.py:460`（p2p 确认） | `ctx.app_id`（当前 bot） | **应传 `sender_name`**（`api.get_user_name`，见 §2.4） |
| `feishu_async.py:455`（sync 昵称匹配） | `ctx.app_id`（app 变时不再把旧昵称匹配的 open_id 直接塞入） | 传匹配到的群成员名 `nick` |
| `chat.py PUT /config`（**手动配置 App ID/Secret**） | 新 `im_app_id`；**检测到 im_app_id 变更时调用 `update_unified_user_info(username, open_id="", app_id=新app_id)` 对账**（换 app → 清 open_id/feishu_name 重确认） | 留空（由重确认/sync 补） |
| `auth.py:141`（OAuth 登录） | 不传（open_id 空 → 按判定逻辑走 else 分支，保留 open_id/app_id） | 空 |

### 2.4 重新确认流程（已具备）
open_id 被清空后，用户在**新 bot** 上 p2p 首条消息 → `get_feishu_open_id` 空 → bot 询问"你的 GitCode 昵称是否是「X」" → 用户确认 → `set_feishu_open_id(..., app_id=ctx.app_id)` 写入**新 app 的 open_id + app_id**。

> **feishu_name 刷新（重要）**：p2p 确认须**同时传 `feishu_name=sender_name`**（`api.get_user_name(ctx, sender_id)` 为发送者在当前 bot 下的飞书昵称）。
> ⚠️ 现状 `handler_async.py:460` 传 `feishu_name=""` + **非空 open_id** → `set_feishu_open_id` 的"保留已有"仅在 open_id 为空时生效 → 会把 feishu_name **写空**。换 app 重确认时若不同步刷新，下游（`pipeline.py:52` 读 feishu_name、prompt 注入名字）拿不到名字。
> 修复：确认调用传 `feishu_name=sender_name`，与 open_id、app_id 一并更新为新 app 下的值。
>
> **feishu_name 来源（无需手动输入）**：p2p 消息自带 `sender_id`/`sender_open_id` → `api.get_user_name(ctx, user_id)`（`feishu_async.py:460`，`GET /contact/v3/users/{user_id}`）直接返回发送者昵称，**用户不需要手动输入**。**优先用 `sender_open_id` 并以 `user_id_type=open_id` 调用**，使 open_id 与 feishu_name 天然来自同一次查询、保证匹配（`get_user_name` 需支持按 `user_id_type` 传参）。前提：bot 有通讯录只读权限 **`contact:user.base:readonly`**——**onboard `_ADDONS.scopes.tenant` 已申请**（2026-08-17 补），但**已存在的 bot 需在飞书开放平台控制台手动开通该权限或重新扫码**，否则 contact API 返回的 user 无 `name` 字段、feishu_name 留空。失败降级：`feishu_name` 留空，后续由群成员 sync（昵称匹配）补齐，或确认时提示用户回复昵称（备选）。

### 2.5 发送侧校验（可选加强）
`_get_feishu_open_id` 不变；`send_card_to_user` 前（如 `tasks._run_notify`、`chat._push_reply_to_feishu`）可校验 `mapping.app_id == 当前 bot app_id`，不匹配则打 WARNING 提示"需重新绑定"，避免静默失败。

## 3. 边界与代价
- 清空 open_id 期间，该用户任务通知会打"无飞书 open_id" WARNING 并跳过——**预期代价**（避免用错 app 的 open_id 静默失败），直到重新确认。
- 存量数据：老行 `app_id=''` 视为"未记录来源"，首次带 app_id 的绑定会覆盖 app_id 并按 2.2 判定（`''` ≠ 新 app_id → 清空重确认一次，可接受）。

## 4. 测试用例设计

| # | 用例 | 前置 | 断言 |
|---|---|---|---|
| U01 | 首次绑定记录 app_id | 无映射 | 写入 app_id + open_id |
| U02 | 同 app 重复绑定刷新 open_id | app_id 相同 | 更新 open_id，不清空 |
| U03 | 换 app 绑定清空重确认 | app_id 不同 | 刷新 app_id，open_id/feishu_name 清空 |
| U04 | OAuth 空 open_id 保留已有（回归） | 已有绑定 | open_id/app_id 不变，仅刷新 updated_at |
| U05 | p2p 确认写当前 bot app_id | 新 bot | 确认后 open_id + app_id=ctx.app_id |
| U06 | sync 昵称匹配写 app_id | 群同步 | 写入 open_id + app_id=ctx.app_id |
| U07 | p2p 确认刷新 feishu_name | 换 app 重确认 | 确认后 feishu_name=sender_name（非空），不再被清空 |
| U08 | 手动配置换 app 对账清空 | 手动 PUT /config 换 im_app_id | 检测到 app 变更 → app_id 刷新、open_id/feishu_name 清空（需重确认） |
| U09 | OAuth 登录不改 app_id/open_id（回归） | 已有绑定 | open_id/app_id 不变，仅刷 updated_at/nickname |

## 5. 实施步骤
1. `session_manager`：DDL 加 `app_id`（含兼容 ALTER）；`set_feishu_open_id` **重命名**为 `update_unified_user_info` 并按 §2.2 判定逻辑实现（含换 app 清空），同步更新 4 处调用方与 3 个测试文件。
2. 调用方传 app_id（扫码 onboard / p2p 确认 / sync）；**p2p 确认同时传 `feishu_name=sender_name`（`get_user_name` 支持 `user_id_type`，优先以 `sender_open_id` + `user_id_type=open_id` 取）**。
3. **手动配置对账**：`chat.py PUT /config` 检测 `im_app_id` 变更时调用 `update_unified_user_info(username, open_id="", app_id=新app_id)`。
4. （可选）发送侧 app_id 匹配校验。
5. 测试 U01-U09 + 回归（p2p_bind / session_manager / chat_unified / chat 配置）→ 部署。

## 6. 结论
以 `app_id` 记录 open_id 的来源 App，换 app 时清空并要求重新确认，保证 (app_id, open_id) 始终配对，杜绝旧 app open_id 在新 app 上静默失效。
