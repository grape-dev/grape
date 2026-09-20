# 仓库全部视图下需求详情看不见流水线场景问题分析与方案

## 一、问题定位

### 1.1 现象

- "我的需求"中点击需求卡片进入详情，可以看到流水线场景内容
- "仓库全部"中点击归属其他人的需求卡片进入详情，看不见流水线场景

### 1.2 根因

前端 `_openBoardDetail()` 点击卡片时调用接口加载流水线：

```js
API.get('/issues/' + taskId + '/pipeline')
```

对应后端接口 `/api/issues/{issue_id}/pipeline`（`issues.py` 第 454 行），核心查询条件：

```python
c.execute(
    "SELECT repo_key, issue_number FROM issue WHERE id=? AND (user_id=? OR user_id=0)",
    (issue_id, user["id"])
)
```

**查询条件 `user_id=? OR user_id=0`：** 只返回 `user_id` 等于当前登录用户 或 `user_id=0` 的 Issue。

当 songspace（user_id=39）在"仓库全部"中点击 Jason7602 创建的 #167（user_id=38）时：
- 查询条件：`WHERE id=1239 AND (user_id=39 OR user_id=0)`
- #167 的 user_id=38，既不等于 39 也不等于 0
- 查询返回空 → 接口返回 404 → 前端 catch 错误 → `_boardPipelineSection` 清空

**流水线场景区域显示"加载中..."后被清空，用户看不到任何流水线内容。**

### 1.3 为什么"我的需求"能看到

"我的需求"只展示 `user_id=当前用户` 或 `user_id=0` 的 Issue，所以点击任何卡片的 taskId 都满足 `user_id=? OR user_id=0` 条件，接口能正常返回。

### 1.4 对比：tasks.py 的同类接口

`tasks.py` 第 1713 行有一个功能相同的接口 `/api/tasks/{task_id}/pipeline`，查询条件也是：

```python
c.execute("SELECT repo_key, issue_number FROM issue WHERE id=? AND (user_id=? OR user_id=0)",
          (task_id, user["id"]))
```

同样的 `user_id=? OR user_id=0` 限制，同样的问题。

---

## 二、解决方案

### 2.1 修改查询条件：按仓库权限而非 user_id 过滤

"仓库全部"视图下，用户能看到某个 Issue 说明该用户有该仓库的访问权限。流水线详情接口不应限制 `user_id`，而应校验用户是否有该仓库的访问权限。

文件：`src/api/routes/issues.py`，函数 `get_issue_pipeline_by_id`

修改前：
```python
c.execute(
    "SELECT repo_key, issue_number FROM issue WHERE id=? AND (user_id=? OR user_id=0)",
    (issue_id, user["id"])
)
```

修改后：
```python
# 仓库全部视图下，用户可以看到其他人的 Issue
# 按 id 查出 Issue，再校验用户是否有该仓库的访问权限
c.execute(
    "SELECT repo_key, issue_number FROM issue WHERE id=?",
    (issue_id,)
)
row = c.fetchone()
if not row:
    raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
repo_key, issue_number = row[0], row[1]

# 校验仓库访问权限（与"仓库全部"列表接口一致）
from src.api.deps import check_repo_access
check_repo_access(user, repo_key)
```

### 2.2 同步修改 tasks.py 的同类接口

文件：`src/api/routes/tasks.py`，函数 `get_task_pipeline`（第 1713 行）

同样的修改：
```python
# 修改前
c.execute("SELECT repo_key, issue_number FROM issue WHERE id=? AND (user_id=? OR user_id=0)",
          (task_id, user["id"]))

# 修改后
c.execute("SELECT repo_key, issue_number FROM issue WHERE id=?", (task_id,))
row = c.fetchone()
if not row:
    raise HTTPException(status_code=404, detail={"code": 404, "message": "任务不存在"})
repo_key, issue_number = row[0], row[1]
from src.api.deps import check_repo_access
check_repo_access(user, repo_key)
```

---

## 三、修改清单

| 步骤 | 文件 | 函数 | 修改内容 |
|------|------|------|---------|
| 1 | src/api/routes/issues.py | get_issue_pipeline_by_id | 去掉 user_id 过滤，改为 check_repo_access |
| 2 | src/api/routes/tasks.py | get_task_pipeline | 同步修改 |

---

## 四、验证

1. songspace 登录，进入"仓库全部"
2. 点击 Jason7602 创建的 #167，确认能看到流水线场景内容
3. 点击 songspace 自己的 #170，确认仍能看到（不回归）
4. 确认无仓库权限的用户访问其他仓库的 Issue 仍返回 403/404（权限校验生效）
