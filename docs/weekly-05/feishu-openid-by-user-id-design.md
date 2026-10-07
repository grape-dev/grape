# 方案：飞书通知 open_id 改为按 user_id 定位（assignee 为空时通知丢失修复）

> 状态：技术方案（2026-10-07）｜目标分支：weekly-05-lyz
> 关联：production seq=2253 — QA 显式 `issues pending-review` 后未收到飞书「待审核」卡片；
> 根因：通知 open_id 按 `issue.assignee`（GitCode 用户名）查 `unified_user_mapping`，而该卡 `assignee=''`
> （实际归属人 `user_id=7`/`Jason7602` 有 `feishu_open_id` 且已配本人 chat bot）→ open_id 空 → 静默跳过。

---

## 1. 现状与问题

### 1.1 触发路径（pending-review / awaiting-input 的飞书通知）

`issues.py` 两个等用户状态 handler：

```python
_open_id = _get_feishu_open_id(it["assignee"])        # 按 GitCode 用户名查 feishu_open_id
ctx = _resolve_user_chat_bot(bm, it["user_id"])        # 发送 bot 却按 user_id
api = bm.api if bm else None
asyncio.create_task(_run_pending_review_notify(..., open_id=_open_id, ...))
...
# _run_pending_review_notify / _run_awaiting_notify 内
if open_id and api and ctx:
    await api.send_card_to_user(ctx, open_id, notify_text, title="…")   # 条件全满足才发
```

### 1.2 根因

- `_get_feishu_open_id(gitcode_user)`（`issues_center.py`）＝ `SELECT feishu_open_id FROM unified_user_mapping WHERE gitcode_user=?`
- A2A/流水线产生的需求卡片 `assignee` 常为空（归属人由 `user_id` 承载，如 seq=2253 的卡 assignee=''、user_id=7）；
- → `_get_feishu_open_id('')` 返回空 → `if open_id and …` 为假 → **静默跳过飞书卡片**（只写会话消息）。
- 同一缺陷存在于 **both pending-review 与 awaiting-input**（都用 assignee）。

### 1.3 影响

- 关键“等用户”状态（待澄清/待审核）不触达用户，等待期不可控；
- 发送 bot（`_resolve_user_chat_bot(bm, user_id)`）本就按 `user_id`，与 open_id 的 `assignee` 维度**不一致**——通知对象与发送通道错位。

---

## 2. 目标

- 飞书 open_id 以 **user_id（需求归属人）** 为准定位，`assignee` 为兜底，保证 TEAQ 等用户通知在卡片 assignee 为空时仍能触达。
- pending-review 与 awaiting-input 两处统一；既有 `notify`（仍用 assignee 语义）不受影响。

---

## 3. 方案设计

### 3.1 新增 `_get_feishu_open_id_by_user_id(user_id)`（issues_center.py）

```python
def _get_feishu_open_id_by_user_id(user_id: int) -> str:
    """按平台 user_id 查飞书 open_id（需求归属人的通知定位）。

    A2A/流水线产生的需求卡 `assignee` 常为空，归属由 user_id 承载；飞书 open_id 应随 user_id 查询，
    而非 GitCode 用户名（assignee）。返回 open_id（空串=未配置/缺失）。
    """
    if not user_id:
        return ""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT feishu_open_id FROM unified_user_mapping WHERE user_id=? AND feishu_open_id!='' LIMIT 1",
                (user_id,))
            r = c.fetchone()
            return r[0] if r else ""
        except Exception:
            return ""
```

- 保留既有 `_get_feishu_open_id(gitcode_user)`（`notify` 等以 assignee 语义调用方仍用）。

### 3.2 两个 handler 的 open_id 定位改为「user_id 优先、assignee 兜底」

`issues.py`：

```python
open_id = _get_feishu_open_id_by_user_id(it["user_id"]) or _get_feishu_open_id(it["assignee"])
```

应用位置：
- `issue_pending_review`（pending-review 通知）
- `issue_awaiting_input`（awaiting-input 通知）

→ 与 `_resolve_user_chat_bot(bm, it["user_id"])` 的通知对象维度对齐；`user_id` 也无 → 空 open_id，静默跳过（保持现状行为，不误发）。

### 3.3 不修改的部分

- `_run_pending_review_notify` / `_run_awaiting_notify` 内部逻辑不变（仍 require open_id+api+ctx）；
- `notify`（任务分配通知）仍按 assignee 语义（`_get_feishu_open_id(assignee)`），不受影响；
- 消息写入会话照常（不依赖 open_id）。

---

## 4. 各模块改动

| 模块 | 改动 |
|---|---|
| `src/api/issues_center.py` | 新增 `_get_feishu_open_id_by_user_id(user_id)`（unified_user_mapping 按 user_id 查 feishu_open_id） |
| `src/api/routes/issues.py` | `issue_pending_review`、`issue_awaiting_input` 的 `open_id = _get_feishu_open_id_by_user_id(user_id) or _get_feishu_open_id(assignee)` |
| 测试 | §5 |
| 文档 | `11-issues.md`、设计同步 |

---

## 5. 测试

| 用例 | 断言 |
|---|---|
| UT-01 卡 assignee=''、user_id=7 有 feishu_open_id → pending-review 通知 open_id 取到 `ou_…` | user_id 优先 |
| UT-02 同条件 awaiting-input → 同样触达 | awaiting 统一 |
| UT-03 user_id 无 open_id、assignee 有 → 回退 assignee open_id | 兜底 |
| UT-04 user_id 与 assignee 均无 → open_id 空，跳过（不报错） | 静默跳过 |
| 回归 | `notify`（assignee 语义）、既有 await/pending 状态迁移不变 |

---

## 6. 兼容性 / 上线要求

- 纯通知定位修正，不改变接口契约/CLI/状态机；`is_running` etc. 不受影响。
- 上线后 seq 2253 类「assignee 空但 user_id 有 open_id」的 issue，pending-review/awaiting-input 会开始触达飞书。

---

## 7. 验收对照

| 项 | 结果 |
|---|---|
| pending-review 通知 open_id 按 user_id 定位 | ✅ 3.2 / UT-01 |
| awaiting-input 通知同样按 user_id 定位 | ✅ 3.2 / UT-02 |
| assignee 兜底保留 | ✅ 3.2 / UT-03 |
| 无 open_id 时静默跳过不误发 | ✅ 3.2 / UT-04 |
| `notify`/既有行为回归 | ✅ 6 |