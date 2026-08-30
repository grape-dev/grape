# 阶段 1 详细改动方案——理由/方案/影响点/合理性分析（v2 修正版）

## 修正说明

v2 修正内容（基于全局流程审查）：
1. 去掉改动点 5（report_messages 加 task_status）——410 机制已有取消检测
2. 去掉改动点 7（client.py report_messages 改返回值）——已返回 dict
3. 去掉改动点 9（agent.py report_messages 检查 task_status）——410 已处理
4. heartbeat 端点返回字段从 {"ok": true} 修正为 {"status": "ok"}（实际代码）
5. 新增遗漏：index.html 需要加 `<script>` 引入 health.js
6. 新增遗漏：health.js 需要定义 renderHealth 函数
7. 改动点从 13 个减少到 10 个

## 任务取消机制说明

### 当前已有的取消检测（不需要改）

report_messages 端点（runtime.py 第 311 行）已经检查 task 终态：
```python
task = service.get_task(task_id)
if task and task.get("status") in ("failed", "cancelled", "completed"):
    raise HTTPException(status_code=410)  # 返回 410 Gone
```

流程：用户终止 → status=cancelled → 下次 report_messages（1-5 秒）返回 410 → client.py 收到 None → agent.py `if _resp is None` → break + _kill_stale_engine

**这是第 1 层保险——已存在，不需要改。**

### 需要新增的取消检测

heartbeat 端点（runtime.py 第 172 行）——覆盖 step 很长时 report_messages 不触发的场景。

**这是第 2 层保险——需要新增。**

### 三层保险

| 层级 | 机制 | 延迟 | 状态 |
|------|------|------|:---:|
| 第 1 层 | report_messages 返回 410（已有） | 1-5 秒 | 已有不改 |
| 第 2 层 | heartbeat 响应带 cancelled_tasks（新增） | 15 秒 | 需要新增 |
| 第 3 层 | 看门狗轮询（已有） | 60 秒 | 已有不改 |

---

## 改动点 1：新增 src/api/routes/health.py（健康端点，分两层）

### 理由
当前 grape 没有健康检查端点——只有 `GET /` 返回 HTTP 200，但这只说明服务进程活着，不能反映数据库、OBS、Worker 是否正常。

### 调用场景

| 场景 | 频率 | 需要鉴权 |
|------|------|:---:|
| 前端系统监控页面 | 30 秒一次 | 需要（用户已登录） |
| K8s livenessProbe/readinessProbe（阶段 4） | 10 秒一次 | 不需要（探针不带 token） |
| nginx 健康检查 | 定期 | 不需要 |
| 外部监控工具 | 定期 | 不需要 |
| 运维手动 curl | 排查时 | 不需要 |

### 方案：分两层端点

**GET /api/health（免鉴权）：**
```json
{"server": "ok"}
```
- 不查数据库、不含凭据/版本/IP/路径
- 比 grape 已有的 `GET /`（返回完整 HTML）暴露的信息更少
- 不增加额外风险
- DDoS 影响小（只返回 JSON 字符串）

**GET /api/health/detail（需登录）：**
```python
@router.get("/api/health")
async def health():
    return {"server": "ok"}

@router.get("/api/health/detail")
async def health_detail(user: dict = Depends(get_current_user)):
    return {
        "server": "ok",
        "database": check_db(),        # SELECT 1
        "obs": check_obs(),             # OBS_ENABLED + get_storage()
        "workers": check_workers(),     # 查 agent_runtimes 在线/离线
        "timestamp": datetime.utcnow().isoformat()
    }
```

### 影响点
| 影响项 | 说明 | 严重性 |
|--------|------|:---:|
| 数据库查询 | /api/health/detail 每次查 1 次 SELECT 1 + 1 次 agent_runtimes | 低 |
| /api/health 查询 | 不查数据库，只返回 JSON | 无 |
| 鉴权 | /api/health 免鉴权 / /api/health/detail 需登录 | 低 |
| 恶意访问 | /api/health 免鉴权但不暴露敏感信息 | 无 |

### 合理性
合理。分两层解决鉴权冲突。

---

## 改动点 2：新增 src/api/routes/dashboard.py（Dashboard API，需登录 + 用户隔离）

### 理由
events 表有完整任务数据但没有聚合查询接口——用户看不到统计信息。

### 方案
新增 5 个聚合端点。所有端点需要登录。repo_key 过滤时查 user_repos 确认权限。

| 端点 | 数据源 | 说明 |
|------|--------|------|
| GET /api/dashboard/runtime/status | agent_runtimes | Worker 在线/离线/槽位 |
| GET /api/dashboard/task/daily | events | 每日任务统计 |
| GET /api/dashboard/task/by-agent | events | 按 agent 统计 |
| GET /api/dashboard/failures/daily | events | 每日失败 |
| GET /api/dashboard/failures/by-agent | events | 按 agent 失败 |

参数：`?days=N`（默认 7，最大 30）+ `?repo_key=`（只返回用户有权限的仓库）

### 影响点
| 影响项 | 严重性 |
|--------|:---:|
| 数据库查询压力 | 中（聚合 SQL） |
| events 表数据量 | 中（大时可能慢） |
| 用户隔离 | 低（多 1 条查询） |

### 合理性
合理。数据已有只缺聚合接口，不加表/字段。

---

## 改动点 3：新增 src/api/static/js/pages/health.js（系统监控前端）

### 理由
没有系统级健康监控页面。

### 方案
新增前端页面，定义 `renderHealth()` 函数，调 /api/health/detail + /api/dashboard/* 渲染：

| 模块 | 数据源 | 展示 |
|------|--------|------|
| 系统健康概览 | /api/health/detail | 4 个状态灯 |
| Worker 列表 | /api/dashboard/runtime/status | 槽位利用率进度条 |
| 任务统计 | /api/dashboard/task/daily | 成功率/失败率 |
| Agent 统计 | /api/dashboard/task/by-agent | 统计表格 |
| 失败分析 | /api/dashboard/failures/by-agent | 失败表格 |

**renderHealth() 函数在此文件内定义。** 每 30 秒自动刷新。

**注意：前端调 /api/health/detail 不是 /api/health。**

### 影响点
| 影响项 | 严重性 |
|--------|:---:|
| 前端体积 | 低 |
| API 调用 | 低（5 个并发） |

### 合理性
合理。独立页面不影响已有功能。

---

## 改动点 4：注册路由 + index.html 加载 health.js

### 理由
新增的 health.py 和 dashboard.py 需要注册路由。新增的 health.js 需要在 index.html 里引入。

### 方案
src/api/main.py 注册路由：
```python
from .routes import health, dashboard
app.include_router(health.router)
app.include_router(dashboard.router)
```

index.html 加载 health.js（在已有 `<script>` 后面加）：
```html
<script src="/static/js/pages/skills.js"></script>
<script src="/static/js/pages/health.js"></script>  <!-- 新增 -->
```

### 影响点
| 影响项 | 严重性 |
|--------|:---:|
| 路由冲突 | 低（/api/health 和 /api/dashboard 不和已有路由冲突） |
| JS 加载顺序 | 低（health.js 在 app.js 之后加载） |

### 合理性
合理。标准路由注册 + JS 引入。

---

## 改动点 5：runtime.py heartbeat 响应加 cancelled_tasks（第 172 行）

### 理由
report_messages 只在 step 间触发——step 很长时（pip install 5 分钟）worker 不调 report_messages 就感知不到取消。heartbeat 每 15 秒一次，在响应里带取消信号覆盖此场景。

**注意：report_messages 已有 410 取消检测（第 311 行），不需要改。本次只改 heartbeat。**

### 方案
runtime.py 第 172 行 `runtime_heartbeat` 端点，响应里加 `cancelled_tasks` 列表：

```python
# 当前
return {"status": "ok"}

# 改后
cancelled = []
with db_lock:
    c = db.cursor()
    for rid in body.runtime_ids:
        rows = c.execute(
            "SELECT id FROM agent_tasks WHERE runtime_id=? AND status='cancelled'",
            (rid,)
        ).fetchall()
        cancelled.extend([r[0] for r in rows])
return {"status": "ok", "cancelled_tasks": cancelled}
```

**注意：当前返回字段是 `{"status": "ok"}` 不是 `{"ok": true}`。改后保持 `status` 字段不变，只加 `cancelled_tasks`。**

### 影响点
| 影响项 | 说明 | 严重性 |
|--------|------|:---:|
| 数据库查询 | 每次 heartbeat 多查 1 条 SQL（按 runtime_id 查 cancelled） | 低（有索引） |
| 响应体积 | 多一个字段（通常空列表） | 无 |
| 向后兼容 | 老版本 worker 不解析 cancelled_tasks——不影响 | 无 |
| heartbeat 频率 | 每 15 秒一次 → 每次多 1 条查询 | 低 |

### 合理性
合理。覆盖 report_messages 盲区。15 秒延迟可接受。查询有 runtime_id 索引。向后兼容。

---

## 改动点 6：client.py heartbeat 返回值解析（第 104 行）

### 理由
服务端 heartbeat 响应加了 cancelled_tasks，client.py 需要返回完整 dict 给 agent.py。

### 方案
client.py 第 104 行 heartbeat：

```python
# 当前
async def heartbeat(self, runtime_ids: list[int]) -> bool:
    result = await self._request("POST", "/api/runtime/heartbeat", {...}, retries=2)
    return result is not None  # 返回 bool，丢弃 dict

# 改后
async def heartbeat(self, runtime_ids: list[int]) -> dict | None:
    result = await self._request("POST", "/api/runtime/heartbeat", {...}, retries=2)
    return result  # 返回完整 dict（含 cancelled_tasks）或 None
```

**report_messages 不需要改——当前已返回 dict 或 None。**

### 影响点
| 影响项 | 说明 | 严重性 |
|--------|------|:---:|
| heartbeat 返回值变化 | 从 bool 改为 dict 或 None | 中（1 个调用点需适配） |
| report_messages | 不改——已返回 dict | 无 |

### 合理性
合理。只有 heartbeat 返回值变化，影响 1 个调用点。

---

## 改动点 7：agent.py heartbeat 调用后检查取消（第 695 行）

### 理由
client.py heartbeat 返回 dict 后，agent.py 需要检查 cancelled_tasks——发现取消杀进程。

### 方案
agent.py 第 695 行，heartbeat 调用后改判断逻辑 + 加取消检查：

```python
# 当前
ok = await client.heartbeat(runtime_ids)
if ok:                          # bool 判断
    heartbeat_fail_streak = 0
else:
    heartbeat_fail_streak += 1
    ...

# 改后
result = await client.heartbeat(runtime_ids)
# ⚠️ 返回值从 bool 改为 dict——判断逻辑必须调整
# 当前端点返回 {"status": "ok"} 不是 {"ok": true}
if result is not None and result.get("status") == "ok":
    heartbeat_fail_streak = 0
    # 检查取消信号
    cancelled_tasks = result.get("cancelled_tasks", [])
    if cancelled_tasks:
        for _tid in cancelled_tasks:
            if _tid in active_tasks:
                _sid = active_tasks[_tid]
                _task = active_task_objs.get(_tid)
                log.warning(f"heartbeat: task {_tid} cancelled, killing engine")
                if _task:
                    _kill_stale_engine(_task, _backend_for_engine(_task.get("_engine_name", "")))
                active_sessions.discard(_sid)
                active_tasks.pop(_tid, None)
                active_task_objs.pop(_tid, None)
else:
    heartbeat_fail_streak += 1
    ...
```

### heartbeat 调用点确认

全局搜索结果——只有 1 处调用点：
```
agent.py 第 695 行：ok = await client.heartbeat(runtime_ids)
```

**只有这 1 处需要适配。**

### 影响点
| 影响项 | 说明 | 严重性 |
|--------|------|:---:|
| heartbeat 判断逻辑 | `if ok:` → `if result is not None and result.get("status") == "ok":` | 中——写错会导致 heartbeat 判断错误 |
| 取消杀进程 | 复用 _kill_stale_engine | 低 |
| 状态清理 | 和看门狗逻辑一致 | 低 |

### 合理性
合理。只有 1 处调用点需要适配。判断逻辑明确——检查 `result is not None`（网络错误）+ `result.get("status") == "ok"`（服务端响应）。

---

## 改动点 8：index.html 新增导航项

### 理由
前端需要入口让用户访问系统监控页面。

### 方案
在 index.html 导航栏 Knowledge 分组后面新增 System 分组：

```html
<div class="grp">System</div>
<a id="nav-health" onclick="app.nav('health')"><span class="ic">⚕</span>System Health</a>
```

### 影响点
无——纯 HTML 新增。

### 合理性
合理。不影响已有功能。

---

## 改动点 9：app.js 新增 nav('health') 分支

### 理由
前端路由需要处理 nav('health')。

### 方案
app.js nav() 函数加分支 + VIEWS 数组加 'health'：

```javascript
const VIEWS = ['dash','pipe','vod','issue','coding','qa','sre','chat','settings','skills','knowledge','runtimes','event','repos','skill','tasks','health'];

if (view === 'health') {
    title.textContent = 'System Health';
    const loadHealth = () => Promise.all([
        API.get('/api/health/detail'),
        API.get('/api/dashboard/runtime/status'),
        API.get('/api/dashboard/task/daily?days=7'),
        API.get('/api/dashboard/task/by-agent?days=7'),
        API.get('/api/dashboard/failures/by-agent?days=7'),
    ]).then(([health, runtime, taskDaily, taskByAgent, failuresByAgent]) => {
        renderHealth({health, runtime, taskDaily, taskByAgent, failuresByAgent});
    }).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    loadHealth();
    _refreshTimer = setInterval(loadHealth, 30000);
}
```

**注意：前端调 /api/health/detail 不是 /api/health。renderHealth 在 health.js 里定义。**

### 影响点
| 影响项 | 严重性 |
|--------|:---:|
| 已有 nav() 逻辑 | 无 |
| VIEWS 数组 | 低 |
| API 地址 | 低——写错返回不完整数据 |

### 合理性
合理。标准 nav 分支新增。

---

## 改动点 10：apple-theme.css 系统监控页面样式

### 理由
系统监控页面需要样式——状态灯、进度条、图表。

### 方案
apple-theme.css 加样式：
- `.health-grid`——健康概览网格布局
- `.health-light`——状态灯（绿 .ok / 红 .error）
- `.worker-bar`——槽位利用率进度条
- `.stat-table`——统计表格
- 对齐已有 :root 变量（--bg / --a / --r / --t2）

### 影响点
无——纯 CSS 新增。

### 合理性
合理。对齐已有设计规范。

---

## 总体合理性评估

| 维度 | 评估 | 说明 |
|------|:---:|------|
| 解决核心问题 | ✅ | 取消延迟 60 秒→15 秒（heartbeat）+ 1-5 秒（410 已有）+ 健康可见性 |
| 零新依赖 | ✅ | 不加 Redis / Prometheus / 新 Python 包 |
| 向后兼容 | ✅ | 老 worker 忽略新字段 / 已有功能不改动 |
| 集群兼容 | ✅ | 查共享数据库，不依赖实例内存状态 |
| 性能影响 | ✅ | heartbeat 每次多 1 条索引查询 |
| 改动风险 | 低 | 只有 heartbeat 返回值变化（1 个调用点） |
| 改动量 | 小 | 4 个新增 + 6 处修改 |

## 改动汇总

**新增文件（4 个）：**

| # | 文件 | 内容 |
|---|------|------|
| 1 | src/api/routes/health.py | GET /api/health（免鉴权）+ GET /api/health/detail（需登录） |
| 2 | src/api/routes/dashboard.py | 5 个聚合端点（需登录 + 用户隔离） |
| 3 | src/api/static/js/pages/health.js | 系统监控前端（含 renderHealth 函数） |
| 4 | （无新文件——注册路由在 main.py 里加 2 行） | — |

**修改文件（6 处）：**

| # | 文件 | 改动位置 | 改动内容 |
|---|------|---------|---------|
| 5 | src/api/routes/runtime.py | 第 172 行 runtime_heartbeat | 响应加 cancelled_tasks 列表 |
| 6 | src/runtime/client.py | 第 104 行 heartbeat | 返回值 bool→dict（只有 1 个调用点） |
| 7 | src/runtime/agent.py | 第 695 行 heartbeat 调用 | 判断逻辑适配 + 检查 cancelled_tasks → 杀进程 |
| 8 | src/api/static/index.html | 导航栏 + script 标签 | 新增 nav-health + `<script>` 引入 health.js |
| 9 | src/api/static/js/app.js | nav() + VIEWS | 新增 nav('health') 分支 + VIEWS 加 'health' |
| 10 | src/api/static/css/apple-theme.css | 样式 | 系统监控页面样式 |
| — | src/api/main.py | 路由注册 | include_router(health + dashboard) |

**不改动：**
- report_messages 端点（410 机制已有取消检测）
- client.py report_messages（已返回 dict）
- agent.py report_messages 调用（410 已处理中断）
- 看门狗（第 718 行——保留作为第 3 层兜底）
- cancel_task 接口
- 数据库（不加表/字段）
- 已有 nav-dash（仓库级 Dashboard）

## 实施顺序

1. 先做改动点 5+6+7（heartbeat 取消实时性）——核心问题，3 处改动
2. 再做改动点 1+2+4（健康端点 + Dashboard API + 路由注册）——后端新增
3. 最后做改动点 3+8+9+10（前端）——前端展示

每步做完验证再进下一步。
