# 方案：Chat 配置重复行导致解绑失效修复（sess-cfg-N 残留双行 + 解绑清群绑定）

> 状态：方案评审中（2026-10-17）｜目标分支：weekly-07-lyz
> 全部内容已脱敏（真实用户名 / bot app_id / user_id 用 `<A>/<B>/app_<X>` 占位）。
> 事发现场：grape 前端 IM 渠道页点「解绑」后，飞书 bot 从运行时移除（`BotManager: removed bot`）、
> `_purge_chat_im_binding` 执行（删 `global_im_bot_config` / 清 `unified_user_mapping`），但
> **`chat_agent_session` 真实绑定行仍保留 app** → 前端仍显示「已绑定」、BotManager 下一轮
> `loaded N eligible bot(s)` 靠该行 secret **重新登记 bot**（`feishu_async.py:1560-1570`）→ 解绑"明明执行却被复活"。
> 另：解绑后 `repo_im_group_binding`（该 bot 的群绑定）未清 → 脏数据残留，`resolve_repo` 仍命中旧仓库。

---

## 一、现象与证据

| 项 | 值 |
|----|----|
| 用户 | user_id=<A>（<owner>/<user>），bot `app_<X>` |
| 解绑日志 | `Chat IM: unbound old app_id=app_<X> user=<A> (other_refs=0)` + `BotManager: removed bot` + `PUT /api/chat/config 200` |
| 解绑后 DB | `chat_agent_session` 真绑定行 `im_app_id` 仍=`app_<X>`（未清）；`global_im_bot_config` 数分钟后复活 active=1 |
| `/where` / 前端 | 仍显示绑定；日志里该 bot 反复 `connected`/`loaded eligible` |

**实证链（服务器直接执行）**：
- 裸 `UPDATE chat_agent_session SET im_app_id='' WHERE id=...` ✅ 生效
- 裸 `INSERT OR REPLACE ... im_app_id=''` ✅ 生效
- `get_chat_config(<A>)` 返回 `im_app_id=''`（**读错行**）
- 直接查：user_id=<A> 有**两条** `repo_key=''` 行 → 见 §二

## 二、根因：同用户 `chat_agent_session` 重复配置行 + `get_chat_config` 无序 `LIMIT 1`

DB 现状（user_id=<A>）：

| id | user_id | repo_key | im_app_id | 来源 |
|----|---------|----------|-----------|------|
| `sess-cfg-<A>` | <A> | '' | ''（空）| 当前正常配置行（`_write_chat_config_row` 生成 id=`sess-cfg-{user_id}`=`sess-cfg-<A>`）|
| `sess-cfg-<B>` | <A> | '' | **app_<X>** | 历史残留：原属 user_id=<B> 的行，账号迁移后 user_id 被改成 <A>，**id 仍 `sess-cfg-<B>`**，im 配置未随迁移清理 |

`get_chat_config(user_id)`（session_manager.py:777）：
```sql
SELECT ... FROM chat_agent_session
WHERE user_id=? AND (repo_key='' OR repo_key IS NULL) LIMIT 1   -- 无 ORDER BY
```
→ SQLite 默认返回先插入的行 `sess-cfg-<A>`（空配置）→ `im_app_id=''`。

**于是解绑链路全程读错行**：
```
前端解绑 PUT /chat/config {feishu:{app_id:''}}
  → update_chat_config else 分支
      old_app_id = get_chat_config(<A>).im_app_id = ''    ← 读到 sess-cfg-<A> 空行
  → 1389 条件 and old_app_id 不成立 → _purge 不清真行
  → _set_chat_config(<A>, im_app_id='') → set_chat_config 读 get_chat_config(<A>)=空行
      → _write_chat_config_row(id='sess-cfg-<A>', im_app_id='')   ← 只更新空行
  → 真绑定行 sess-cfg-<B> 永不被清
```

**且 BotManager 真实行仍在**：`feishu_async.py:1543-1548` 加载 eligible bots 用
`SELECT im_app_secret FROM chat_agent_session WHERE im_app_id=? ... AND im_app_secret!=''`
→ 命中 `sess-cfg-<B>` 的 app+secret → 把该 bot 重新登记 `global_im_bot_config` + eligible → **bot 复活**。

**一句话根因**：账号迁移遗留了第二条 `repo_key=''` 配置行（id=sess-cfg-<B> / user_id=<A> / 带 im 配置），
`get_chat_config` 无 `ORDER BY` 的 `LIMIT 1` 命中空行，导致解绑/配置读写落到错误行，真绑定行永久残留并被 BotManager 复用。

## 三、修复方案

### 方案 1（推荐，根因）：`get_chat_config` 优先返回「已配置行」

`chat_agent_session` 同 user 多行时，按「im_app_id 非空优先 / id 有序」确定性选行，避免命中空行：

```sql
-- session_manager.py `get_chat_config`：改为确定性选行
SELECT id, runtime_id, skill, skill_id, im_app_id, im_app_secret, im_provider
FROM chat_agent_session
WHERE user_id=? AND (repo_key='' OR repo_key IS NULL)
ORDER BY (im_app_id IS NOT NULL AND im_app_id != '') DESC, id ASC
LIMIT 1
```
同步修改 `_write_chat_config_row` 收敛重复：
```python
# 写配置前先清理同 user 的重复空行（只保留一条），确保后续读写单行
DELETE FROM chat_agent_session
WHERE user_id=? AND (repo_key='' OR repo_key IS NULL) AND id != ?
```

### 方案 2（数据修复，立即恢复）：清理生产重复行

一次性 SQL（D43/账号迁移残留清理，幂等可进 migration）：
```sql
-- 先备份受影响字段
DELETE FROM chat_agent_session
WHERE user_id=<A> AND repo_key='' AND id='sess-cfg-<B>';
-- 泛化：同一 user_id 同 repo_key='' 的重复行只留 id=MIN(id) 一条
DELETE FROM chat_agent_session
WHERE repo_key='' AND id NOT IN (
  SELECT MIN(id) FROM chat_agent_session WHERE repo_key='' GROUP BY user_id
);
```

### 方案 3（强化，防 BotManager 复活）
`feishu_async.py` 登记 eligible bots 时，对 `chat_agent_session` 的查询也**限定取确定性单行**（同 `方案1` 排序），并仅在行存在且是「唯一正确行」时才登记；或登记前核对 `unified_user_mapping`/`global_im_bot_config` 状态，避免删除后又被 `INSERT OR IGNORE` 重建。

--- 建议：方案 1（代码根因，含解绑清群绑定）+ 方案 2（数据清理）一起上；方案 3 不再需要（方案 1 完成后该 bot 不再登记，BotManager 无从复活） ---

## 四、各模块改动

| 模块 | 改动 |
|------|------|
| `src/session_manager.py` | `get_chat_config`：`ORDER BY (im_app_id 非空) DESC, id ASC LIMIT 1`；`_write_chat_config_row`：写前清理同 user 重复空行 |
| `src/api/routes/chat.py` | `_purge_chat_im_binding`：解绑时**一并删除 `repo_im_group_binding WHERE im_app_id=?`**（清群绑定，防脏数据） |
| `db_setup.py` | 幂等清理重复配置行（方案 2，启动迁移）|
| `tests/` | 见 §五 |

## 五、测试用例设计

| 编号 | 用例 | 前置 / 步骤 | 断言 |
|------|------|-------------|------|
| UT-01 | get_chat_config 多行取已配置行 | seed user=<A> 两条 repo_key=''（空行先、配置行后） | 返回配置行的 im_app_id，非空 |
| UT-02 | get_chat_config 仅空行 | 只有空配置行 | 返回空 im_app_id（兜底）|
| UT-03 | set_chat_config 清空 -> 真实行清空 | get_chat_config 命中配置行后 set im_app_id=''  | DB 配置行 im_app_id 变 '' |
| UT-04 | 解绑端到端 | 双行 + 已绑 bot，调 update_chat_config(feishu app_id='') | 配置行 im 三字段清空、global_im_bot_config 对应行删除 |
| UT-05 | _write_chat_config_row 写前清理重复 | 同 user 多空行，调写配置 | 仅保留一条 + 配置写入该条 |
| UT-06 | 重复行清理迁移幂等 | 预置双行，跑迁移两次 | 仅留一条，重跑无副作用 |
| UT-07 | BotManager 不复活 | 登记前真实行已清 | eligible bots 不再含该 app |
| **UT-08** | **解绑清群绑定** | 该 bot 名下多条 repo_im_group_binding，调 `_purge_chat_im_binding` | 该 bot 全部群绑定删除 |
| **UT-08b** | 解绑不影响其他 bot 群绑定 | 另一 bot 有群绑定，调 `_purge` 清当前 bot | 另一 bot 群绑定保留 |

## 六、验证

1. **单测**：UT-01~08b 全绿。
2. **单机 E2E**：IMS 解绑 → `chat_agent_session` 真绑定行清空 → `repo_im_group_binding` 该 bot 群绑定清空 → 前端不显示绑定 → 5min 后 BotManager 不复活该 bot → 发消息走"未绑定仓库"提示。
3. **回归**：正常绑定/换绑（app_id 非空）路径不受影响；多用户隔离不破坏。

## 七、关联与影响面

- 根因来自账号迁移（`sess-cfg-<B>` 残留）与 `get_chat_config` 无序取行的交互；修复对正常单行用户行为不变（排序无副作用）。
- **解绑清群绑定是解绑语义补全**：`repo_im_group_binding` 用 `im_app_id` 关联 bot，bot 解绑后群绑定为脏数据，必须一并清，否则 `resolve_repo` 仍命中旧仓库（用户发消息不会走"未绑定仓库"提示）。
- 数据清理需先备份相关行（避免误删配置）。
- 可与之前 bot 归属修复（cli_aae1bb5 → user2）合并验证：确认该 bot 真归属 user2 且仅一条配置行。