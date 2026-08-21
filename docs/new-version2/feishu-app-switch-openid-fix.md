# 飞书 Chat 用户 feishu_name 丢失修复方案

> 版本：v1.0（方案稿）
> 日期：2026-08-19
> 说明：修复换飞书 App 后 prompt 中 feishu_name 丢失、Bot 无法识别用户身份的问题。根因确认：`get_feishu_open_id` 未按 `app_id` 过滤 + 群聊无 sender_name 兜底。**不含代码改动。**

---

## 一、问题现象

飞书群聊消息 prompt 中 "User:" 行显示空，Bot 不知道说话人是谁：

```
User:  (ID: )
Feishu open_id: ou_910b753f083efa53085fd90396f07ddd
```

## 二、根因

### 2.1 直接原因：`get_feishu_open_id` 不按 app_id 过滤

`src/session_manager.py:342`：

```python
def get_feishu_open_id(gitcode_user):
    c.execute("SELECT feishu_open_id FROM unified_user_mapping WHERE gitcode_user=?", (gitcode_user,))
```

**仅按 `gitcode_user` 查询**，不区分飞书 App。当用户更换 Chat Bot 的飞书 App 后：

```
chat_agent_session:     app_id=cli_aae1bb5f（新）→ user_id=7 (Jason7602)
unified_user_mapping:   app_id=cli_aaf6d6（旧）→ open_id=ou_046b... → gitcode_user=Jason7602
```

飞书消息到达（app_id=cli_aae1bb5f, open_id=ou_910b...）：

```
get_user_id_by_app_id("cli_aae1bb5f") → 7 ✅
get_feishu_open_id("Jason7602") → "ou_046b..."（旧 App 的 open_id）
handler: if existing: pass → 跳过 P2P，新 open_id 永远写不进 mapping
```

### 2.2 次要原因：群聊不触发 P2P，且 sender_name 无兜底

- `handler_async.py:445` — `chat_type == "p2p"` 才触发 P2P 绑定，群聊直接跳过
- `handler_async.py:629` — `sender_name` 仅靠 `sender_id`，群聊 WS 消息无 `user_id` → 始终为空
- `unified_user_mapping` 虽然存了 `feishu_name`，但群聊不触发绑定 → 无记录可查

### 2.3 时序

换飞书 App（如在 Chat Agent 页重新扫码配置）时：
1. `chat_agent_session.im_app_id` 更新为新 App ← ✅ 会更新
2. `unified_user_mapping.app_id` 仍指向旧 App ← ❌ 未同步更新
3. `get_feishu_open_id` 查旧记录返回非空 → P2P 被跳过 → 永远无机会更新

## 三、修复

### 3.1 `get_feishu_open_id` 增加 `app_id` 过滤（主要）

`src/session_manager.py:342`：

```python
# 改前
def get_feishu_open_id(gitcode_user):
    c.execute("SELECT feishu_open_id FROM unified_user_mapping WHERE gitcode_user=?", (gitcode_user,))

# 改后
def get_feishu_open_id(gitcode_user, app_id=""):
    c.execute("SELECT feishu_open_id FROM unified_user_mapping WHERE gitcode_user=? AND app_id=?", 
              (gitcode_user, app_id))
```

**影响**：10 个调用点需传 `app_id`（`handler_async.py` 内 9 处 + 可能外部 1 处）。调用方已有 `ctx.app_id` 可用。

**效果**：换 App 后 `get_feishu_open_id("Jason7602", "cli_aae1bb5f")` 返回 None → P2P 正常触发 → `update_unified_user_info` 写新 mapping。

### 3.2 群聊 sender_name 兜底（次要）

`src/im/handler_async.py` 第 629 行后，sender_name 为空时用 open_id 直接查飞书昵称：

```python
sender_name = await api.get_user_name(ctx, sender_id) if sender_id else ""
if not sender_name and open_id:
    sender_name = await api.get_user_name(ctx, open_id, user_id_type="open_id") or ""
```

**效果**：即使没有 unified_user_mapping 记录，群聊也能通过飞书 API 拿到用户昵称填入 prompt。

### 3.3 数据修复（一次性，可选）

生产中 Jason7602 的旧 mapping 需清理（或直接用 `/refresh` 在群内重新同步）：

```sql
DELETE FROM unified_user_mapping WHERE gitcode_user='Jason7602' AND app_id='cli_aaf6d61ac0b89d27';
```

修复代码部署后，用户在群聊发消息 → P2P 中判定 "无当前 App mapping" → 但群聊仍不触发 P2P → **需在私聊窗口发一条消息触发 P2P 确认，或等 `/refresh` 同步**。

## 四、改动范围

| 文件 | 改动 | 行数 |
|---|---|---|
| `src/session_manager.py` | `get_feishu_open_id` 签名加 `app_id`，SQL 加 `AND app_id=?` | ~3 行 |
| `src/im/handler_async.py` | 所有调用 `get_feishu_open_id` 处传 `ctx.app_id`（~10 处）| ~10 行 |
| `src/im/handler_async.py` | sender_name 兜底：用 open_id 调 `get_user_name(open_id_type="open_id")` | ~2 行 |

## 五、测试

| 用例 | 覆盖点 |
|---|---|
| `test_get_feishu_open_id_filters_by_app` | 同用户两个 app_id → `get_feishu_open_id(name, app_a)` 只返回匹配的 |
| `test_get_feishu_open_id_no_app_returns_none` | 旧签名（无 app_id 参数）调用方传空 → 向后兼容 `AND app_id=''` |
| `test_p2p_retriggers_on_app_change` | 模拟换 App 场景：旧 mapping 存在但 app_id 不同 → P2P 确认流程触发 |
| `test_sender_name_fallback_open_id` | sender_id 为空 → 用 open_id 调 get_user_name |
| 回归 | `tests/test_p2p_bind.py`、`tests/test_unified_user_info.py` 全绿 |

---

## 六、结论

根因是 `get_feishu_open_id` 不按 `app_id` 过滤，导致换飞书 App 后旧映射阻塞新绑定。修复分两步：
1. **`get_feishu_open_id` 加 app_id 过滤** → 换 App 后自动触发重新绑定
2. **群聊 sender_name 兜底** → 即使无 mapping 也能显示用户昵称

改动集中在 `session_manager.py`（~3 行）+ `handler_async.py`（~12 行），配合数据清理即可恢复。