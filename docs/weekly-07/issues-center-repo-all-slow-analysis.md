# 需求中心「仓库全部」响应慢 — 根因分析与优化方案

日期：2026-10-16
仓库：grape-dev/grape（weekly-06 分支代码，双主 202609252202 运行版本）
现象：需求中心点击「仓库全部」标签页，系统响应很慢（数秒）。

## 一、结论（一句话）

后端接口 `GET /api/issues/{owner}/{repo}`（仓库全部视图）在组装每张卡片时逐条调用
`get_pipeline_config()` 查场景名，该函数无任何缓存、每次要做 3 个 DB 往返
（SHOW COLUMNS + 场景表查询 + scene_market 兜底查询），仓库 348 条 Issue 就是
约 1000 次 DB 往返，单这一项就占 800ms+；再叠加 713KB 的超大响应体和
前端 15 秒全量轮询，用户感知就是"很慢"。

## 二、实测数据（grape-dev/grape，371 张卡片）

用线上同款 venv + 线上 MySQL 库实测接口内部各段耗时：

| 阶段 | 耗时 | 说明 |
|---|---|---|
| `_query_tasks()`（含富化） | **0.938s** | 瓶颈，见下分解 |
| `_group_events_by_issue()` | 0.063s | events 全量聚合，正常 |
| `_issue_stage_map()` | 0.006s | 正常 |
| `_query_pr_subscription_events()` | 0.007s | 正常 |
| 合计服务端 | ~1.01s | 未含鉴权/序列化/传输 |
| 响应体大小 | **713 KB** | 平均每卡 ~2KB（含 agent_statuses 全量数组） |

`_query_tasks()` 内部 0.94s 的分解：

| 操作 | 耗时 |
|---|---|
| 4 条批量 SQL（issue 主查询 / issue_state / agent_tasks / agent_statuses / pr_keys） | ~0.15s（每条单独实测 4~42ms，均已走索引，无慢 SQL） |
| **逐条 `get_pipeline_config(scene_id, repo_key)` × 348** | **~0.80s**（单次实测 ~2.3ms：SHOW COLUMNS FROM devops_pipeline_scene + scene 查询 + scene_market 兜底，scene_id 为空时也要跑满 3 条） |

代码位置：
- `src/api/issues_center.py` `_query_tasks()` L1787-1793：循环内逐条调 `get_pipeline_config`
- `src/api/pipeline_config.py` `get_pipeline_config()` L283 / `_query_scene_by_id()` L196：无缓存，
  且每次调用先执行 `SHOW COLUMNS FROM devops_pipeline_scene` 做列存在性探测（L209-221）

## 三、根因链

1. **主因：场景名查询 N+1 且无缓存**
   `_query_tasks` 对每张卡片调一次 `get_pipeline_config`；该函数没有任何
   request 级或进程级缓存，且内部先 `SHOW COLUMNS` 探测列、再查
   `devops_pipeline_scene`、查不到又查 `scene_market`——同一仓库、同一
   scene_id 的结果被重复查了几百遍。Issue 数越多线性越慢，这是响应慢的主因。

2. **次因：响应体过大（713KB）**
   列表接口声称 "paged"，实际一次性返回全部 371 张卡，每卡带
   `agent_statuses`（6 个 agent 节点状态数组）、`qa_history`（PR 卡全量审查历史）
   等列表页用不到的重字段。传输 + 前端 JSON 解析 + 渲染全量卡片均被拖慢。

3. **放大器：前端 15 秒全量轮询**
   `issues.js` `_startScenePoll()`（L140-157）：只要有 active/processing 任务，
   每 15 秒重新执行一次完整 `loadTasks()`——即重新拉一次 713KB、重跑一次 1s+
   的后端查询。仓库里有任务在跑时，页面会持续被这个开销压制，用户感知"一直很慢"。

## 四、解决技术方案

按收益/风险排序，方案 1+2 为本期必做，3、4 视排期跟进。

### 方案 1（治本，收益最大）：get_pipeline_config 进程级缓存 + 循环外缓存

改动点 A — `src/api/pipeline_config.py`：

- 新增模块级缓存 `_SCENE_CACHE: dict[tuple[str, str], tuple[dict|None, float]]`，
  key 为 `(scene_id, repo_key)`，TTL 建议 60s（场景配置低频变更，管理端改场景后
  60 秒内可见，可接受；如需即时生效，在 create/update scene 的写路径里主动失效）。
- `SHOW COLUMNS FROM devops_pipeline_scene` 的列探测结果提升为模块级一次性
  变量（表结构只有启动迁移才会变，进程内缓存安全）。
- 兜底链（DB scene → scene_market → pipelines.json）整体缓存，`None` 结果也缓存，
  避免空 scene_id 每次都打穿三条查询。

改动点 B — `src/api/issues_center.py` `_query_tasks()` L1787-1793：

- 循环外先收集去重后的 `(scene_id, repo_key)` 组合（绝大多数卡片是
  repo_default + 少数 VOD scene_id，实际只有个位数组合），先批量解析成
  `scene_name` 映射，循环内查字典。即使不加全局缓存，仅此改动就把
  348 次调用降为 2~5 次。

预期：`_query_tasks` 0.94s → 0.2s 以内。

### 方案 2（响应瘦身）：列表接口去重字段 + 真分页

- 列表卡片移除 `agent_statuses`、`qa_history`、`task_description` 长文本
  （这些只有详情页用，详情已有独立接口 `/issues/{id}/pipeline`）；
  `latest_event` 只保留 `seq/status/title/created_at` 四字段（现为整条事件）。
  预期响应从 713KB 降到 ~150KB。
- 真分页：`GET /issues/{owner}/{repo}` 加 `page/page_size`（默认 50），
  SQL 加 `LIMIT/OFFSET` + `COUNT(*)`；注意 mode=default 的
  `CASE WHEN status...` 排序走文件排序，数据量到万级前可接受，万级后可给
  `issue(repo_key, status, created_at)` 复合索引。前端 `issues.js` 已有
  `_taskPage` 分页状态，改为服务端分页即可（需检查 `events_count/latest_event`
  聚合只对当前页 50 条做）。

### 方案 3（轮询降载）：前端轮询改轻量

- `_startScenePoll()` 轮询时改调轻量接口（如新增
  `GET /issues/{owner}/{repo}/status-summary`，只返回 id/status/is_running/
  stage 四字段，或复用现有 `/issues/{issue_id}/status` 批量版），
  检测到变化才触发一次完整 `loadTasks()`。
- 短期不改后端时，至少将轮询间隔 15s → 30s，并复用上一次响应的
  ETag/total 做条件请求。

### 方案 4（DB 加固，顺带做）

- `events` 表目前只有 `repo_key`、`issue_id` 单列索引；
  `_batch_fetch_agent_statuses` / `_batch_fetch_pr_issue_keys` 的
  `(repo_key=? AND issue_id=?)` OR-pair 查询各做两次全扫。
  加复合索引 `events(repo_key, issue_id)`（前缀长度 191/191）
  后单条查询可走索引定位。当前量级（9733 行）收益小，但随事件增长可防退化。

## 五、风险与兼容性评估

- 方案 1 缓存：场景在管理端被编辑后最长 60s 延迟可见；写路径主动失效可消除。
  读路径无行为变化，风险低。
- 方案 2 字段裁剪：需全仓 grep 前端对 `agent_statuses/qa_history` 在
  列表渲染路径的引用（`issues.js` 卡片模板），确认仅详情页使用后再移除；
  CLI `grape issues` 若消费这些字段需保留（对照 `docs/openapi/11-issues.md` 字段表）。
- 方案 2 分页：对外 API 行为变化，CLI 与 A2A 调用方需确认未假设全量返回；
  兼容做法：默认仍全量，仅当请求带 `page` 参数时启用分页。
- 双主部署：改动涉及 174/79 两台，scp 同步 + 分别重启。

## 六、验证方式

改动后用本文第二节的同款计时脚本（线上 venv + 线上库）复测：
- `_query_tasks` < 0.2s、接口总耗时 < 0.4s、响应体 < 200KB 为达标。
- 前端实测：点击「仓库全部」首屏 < 1s；processing 任务存在时页面操作不再周期性卡顿。
