# Issue user_id=0 根因分析与修复方案

## 一、问题现象

用户 songspace（user_id=39）创建的 Issue #185 在需求中心"我的需求"中不可见。数据库中 #185 的 user_id=0、assignee=''。近 99 条 Issue 记录存在同样问题（占总数 213 条的 47%）。

## 二、根因定位

### 2.1 数据链路

```
GitCode webhook (issue_opened)
  → payload.user.username = "songspace"  ✅ 存在
  → payload.sender = None
  → payload.assignees = [{username: "songspace"}]  ✅ 存在

event_pipeline.py 提取参数：
  → _asg3 = payload.assignees[0].username = "songspace"  ✅
  → _sender3 = (payload.sender or payload.user).username = "songspace"  ✅
  → 调用 ensure_issue_task(assignee="songspace", sender="songspace")

ensure_issue_task 内部：
  → assignee = "songspace" (传入值非空，跳过 issue_state/sender fallback)
  → target = assignee or owner = "songspace"
  → user_id = _get_user_id_by_gitcode("songspace") = 39  ✅ 应该正确
  → 查 issue 表：WHERE status IN ('active','processing') → 找到已有记录（幂等复用）
```

### 2.2 根因：幂等复用时不更新 user_id

`ensure_issue_task()` 的幂等复用逻辑（tasks.py 第 205-220 行）：

```python
# 已有记录时复用
if row:
    task_id = row[0]
    # 只更新 title
    if title:
        c.execute("UPDATE issue SET title=? WHERE id=?", (title, task_id))
    # 只在 target 非空 AND 已有 assignee 非空 AND 两者不同时才更新
    if target and _existing_assignee and target != _existing_assignee:
        c.execute("UPDATE issue SET assignee=?, user_id=? WHERE id=?", (target, user_id, task_id))
```

**问题在第 215 行的条件：`if target and _existing_assignee and target != _existing_assignee`**

三个条件必须同时满足才更新 user_id：
1. `target` 非空 — "songspace" 非空 ✅
2. `_existing_assignee` 非空 — 已有记录的 assignee 是空字符串 `''` ❌
3. `target != _existing_assignee` — 满足 ✅

**条件 2 不满足**：已有记录的 assignee 是空字符串 `''`，在 Python 中 `if '' and ...` 为 False，所以整个条件不成立，**不执行 UPDATE，user_id 保持为 0**。

### 2.3 为什么已有记录的 assignee 是空的

#185 的 issue 记录可能先由 VOD agent 处理时创建（issue_opened 事件先到，此时 assignee 为空），后续 webhook 再次触发时找到已有记录，但因为已有 assignee 为空所以不更新。同理，#169 以后的所有 Issue 都是这个模式。

### 2.4 分界点验证

- #168（2026-09-15 21:21:30）：assignee='mm6667'，user_id=40 ✅ — assignee 非空，幂等复用时能更新
- #169（2026-09-15 21:47:45）：assignee=''，user_id=0 ❌ — assignee 为空，幂等复用时不更新

#169 之后的 Issue 全部 assignee=''，说明从 #169 开始 Issue 创建时的 assignee 就没被正确设置（可能第一次创建时 assignee 参数为空，后续复用时又因条件 2 不满足而不更新）。

---

## 三、修复方案

### 3.1 P1-A：修复幂等复用时 user_id 不更新的逻辑

文件：`src/api/routes/tasks.py`，函数 `ensure_issue_task`，约第 215 行

修改前：
```python
if target and _existing_assignee and target != _existing_assignee:
    c.execute("UPDATE issue SET assignee=?, user_id=? WHERE id=?", (target, user_id, task_id))
```

修改后：
```python
# 已有 assignee 为空时也需要更新（修复 user_id=0 问题）
if target and target != _existing_assignee:
    c.execute("UPDATE issue SET assignee=?, user_id=? WHERE id=?", (target, user_id, task_id))
    # ... 原有 _close_other_assignee_tasks 逻辑
```

去掉 `_existing_assignee` 非空条件。当 target 有值且与已有 assignee 不同时（包括已有为空的情况），都更新 assignee 和 user_id。

### 3.2 P1-B：历史数据回填

对 user_id=0 的 Issue，按 events 表 issue_opened 事件的 actor 回填正确的 user_id 和 assignee：

```sql
-- 回填 issue 表的 user_id 和 assignee
UPDATE issue t
JOIN (
    SELECT 
        e.issue_id,
        e.actor,
        u.user_id
    FROM events e
    JOIN unified_user_mapping u ON u.gitcode_user = e.actor AND u.user_id > 0
    WHERE e.event_type = 'issue_opened'
      AND e.repo_key = 'gitcode:grape-dev/grape'
    GROUP BY e.issue_id, e.actor, u.user_id
) sub ON t.issue_number = CAST(REPLACE(sub.issue_id, '#', '') AS UNSIGNED)
    AND t.repo_key = 'gitcode:grape-dev/grape'
SET t.user_id = sub.user_id,
    t.assignee = sub.actor
WHERE t.user_id = 0
  AND t.repo_key = 'gitcode:grape-dev/grape';
```

回填后验证：
```sql
SELECT COUNT(*) FROM issue WHERE repo_key='gitcode:grape-dev/grape' AND user_id=0;
-- 预期：大幅减少（仅剩 actor 未注册平台用户的 Issue）
```

---

## 四、修改清单

| 步骤 | 内容 | 文件/位置 | 工作量 |
|------|------|----------|--------|
| 1 | 修复幂等复用条件 | src/api/routes/tasks.py:215 | 0.5h |
| 2 | 历史数据回填 SQL | DB 一次性执行 | 0.5h |
| 3 | 回填后验证 | DB 查询 | 0.5h |
| 合计 | | | 1.5h |

---

## 五、验证方案

### 5.1 代码修复验证

1. 创建一个 Issue（通过 GitCode webhook）
2. 等待 webhook 触发 ensure_issue_task
3. 确认 issue 表记录的 user_id 和 assignee 正确（非 0/非空）
4. 再次触发同一 Issue 的 webhook（如评论），确认幂等复用时不丢失 user_id

### 5.2 历史数据验证

1. 执行回填 SQL
2. 查询 `SELECT COUNT(*) FROM issue WHERE user_id=0` 确认大幅减少
3. songspace 登录，需求中心"我的需求"确认 #185 可见

### 5.3 回归验证

1. 确认 user_id > 0 的已有 Issue 不被错误覆盖
2. 确认 assignee 已有值的 Issue 不被错误修改（target == _existing_assignee 时不更新）
