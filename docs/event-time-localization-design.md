# 控制台时间本地化展示设计 v1.0（Event History 时间错位 +8h 修复）

## 一、背景与问题

grape 开发者控制台多个页面（Event History / Recent Activity / Event 详情 / 运行日志 / 知识库）
展示的时间不是本地时间：Event History 中实际 20:46 发生的事件显示为 04:46（次日）。

## 二、根因（2026-08-17 实测确认）

**跨表时间存储语义不一致**，而前端 `fmtLocalTime` 假定所有输入都是 UTC：

| 时间来源 | 存储格式 | 语义 |
|---|---|---|
| events.created_at（MySQL 默认 `cast(now() as char)`） | `2026-08-17 20:46:19` 无时区 | CST（服务器本地，UTC+8） |
| events.started_at / completed_at、agent_tasks 时间 | `2026-08-17T12:46:19.163342+00:00` | UTC ISO（带时区） |
| SQLite 后端的 created_at（`datetime('now')`） | `2026-08-17 12:46:19` 无时区 | UTC |

前端 `fmtLocalTime`（src/api/static/js/api.js）用 `Date.UTC(...)` 解析无时区字符串再转浏览器本地时区：
- 对 UTC ISO（带 `+00:00`/`Z`）：正确
- 对 MySQL 的 CST 无时区字符串：被当 UTC → 浏览器 +8 显示 → **错位 +8h 且跨天**

纯字符串无法区分「无时区 = UTC（SQLite）还是 CST（MySQL）」，必须由后端按 backend 语义归一化。

## 三、方案（用户已确认：方案 A）

后端序列化层统一 + 前端统一转换，分三层：

### 3.1 后端：时间归一化工具函数

新增 `src/api/time_utils.py`（或挂到 event_store）：

```python
def normalize_event_time(raw: str) -> str:
    """把存储层时间统一输出为 UTC ISO（带时区），供 API 序列化使用。
    - 带时区（Z / +HH:MM）→ 原样（已是 UTC ISO）
    - 无时区 'YYYY-MM-DD HH:MM:SS' → 按 backend 语义：mysql=CST(+08:00) 转 UTC；sqlite=UTC 直接加 Z
    - 解析失败 → 原样返回（不破坏现有数据）
    """
```

backend 判定用 `src/mysql_compat.load_db_backend_config()` 的 `backend` 字段（sqlite/mysql）。

### 3.2 后端：events 相关 API 序列化点统一调用

输出 created_at / started_at / completed_at 的序列化点全部过 `normalize_event_time`：
- `src/api/event_store.py`：`get_events`（列表）、事件详情、`get_events_grouped_by_issue`
- `src/api/routes/dashboard.py`：Recent Activity（activity 列表）
- `src/api/routes/agents.py`：recent_events

> 范围聚焦 events 链路（Event History / Recent Activity / Event 详情）。chat_messages / knowledge / task_logs
> 等其它时间字段本次一并纳入（同一函数，直显点见 3.3），但不在本次范围的可后续跟进。

### 3.3 前端：fmtLocalTime 增强 + 直显点补转换

- `src/api/static/js/api.js` `fmtLocalTime`：
  - 支持 ISO 毫秒 + 时区偏移的健壮解析（`2026-08-17T13:27:53.481678+00:00`、`...Z`）
  - 保持「输入 = UTC ISO → 本地时间」单一语义（后端归一化后无歧义）
- Event History（`agents.js`）：显示完整日期时间（`MM-DD HH:MM` 或 `YYYY-MM-DD HH:MM:SS`），不再只取 `substring(11,16)`
- 运行日志（`events.js` `_tlLine`）：`created_at.slice(11,19)` 直显 → 改 `fmtLocalTime` 取 HH:MM:SS
- 知识库（`knowledge.js`）：`replace('T',' ').substring(0,16)` 直显 → 改 `fmtLocalTime`

## 四、影响面

- 后端：新增 1 个工具函数 + 3 个序列化点接入；输出格式从「CST 无时区」变「UTC ISO 带时区」——**任何直接消费 created_at 原始字符串的调用方需复核**（目前控制台前端是唯一消费方，且前端本就按 UTC 解析，语义反而被修正）
- 前端：api.js / agents.js / events.js / knowledge.js 四处
- 存量数据：读时转换，不迁移 DB，不改历史数据

## 五、验证方案

1. 后端：pytest 单测 `normalize_event_time`（mysql 语义 CST→UTC、sqlite 语义原样、带时区原样、坏值兜底）+ 相关路由测试
2. 前端：`node --check` 语法 + jsdom 行为验证 `fmtLocalTime('2026-08-17 20:46:19' 经后端归一化后)` 输出本地时间
3. 集成：curl `:8000/api/...` events 列表接口确认 created_at 输出为 UTC ISO；浏览器对照 Event History 显示与真实发生时间一致（当前 +8h 错位消失）
4. 回归：合并验证快速子集（21 文件 + test_api_routes.py，2 deselect 基线）

## 六、部署

- 静态文件即时生效（无需重启）；后端工具函数/序列化改动需重启 grape-main + grape-webhook
- 特性分支验证通过后合 dev，走正常部署流程
