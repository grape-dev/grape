# Grape 平台系统健康分析 — 2026-10-16「整体卡」问题

日期：2026-10-16
范围：双主 174/79（uvicorn 8000 + webhook 8080）、nginx、MySQL（172.16.11.181 RDS）、前端轮询行为
数据来源：live 系统指标 + api_latency_log（今日 2154 条实测请求延迟） + information_schema + 代码

## 一、总体结论（一句话）

系统资源本身不缺（CPU 87% 空闲、内存剩 5GB、磁盘 65%），卡的根因是**应用层吞吐结构**：
每台主机的 API 是 uvicorn **单进程**，且所有 SQL 被一把**全局 db_lock 串行化**——
全平台的数据库操作排成一条队，任何慢接口（issues 列表 N+1、scenes 大查询、今天一次 124 秒的
experience 请求）都会把队堵死，所有用户一起等。再加上接口延迟连续三天持续恶化
（10-14 全站均值 763ms → 10-15 914ms → 10-16 1208ms），用户感知就是"今天整体特别卡"。

## 二、健康度体检表

| 检查项 | 实测值 | 评价 |
|---|---|---|
| 174 CPU | 8 核，load 1.88，idle 87.4% | ✅ 正常 |
| 174 内存 | 14G 总 / 7.8G 用 / 5.1G 可用 | ✅ 正常 |
| 174 磁盘 | / 用 65%（24G/39G），无 >100M 日志 | ✅ 正常 |
| 79 CPU/负载 | load 0.21，正常 | ✅ 正常 |
| API 进程 | **两台均为 uvicorn 单 worker**，8000 进程 CPU 瞬时 81%（GIL 单核上限） | ⚠️ 结构性瓶颈 |
| /health、静态资源 | 2~5ms | ✅ 正常 |
| MySQL 连接 | Threads_connected 63→96 波动，Threads_running 2 | ⚠️ 见连接问题 |
| MySQL 连接建立 | 20 秒内新建 40 个连接（约 2 个/秒），**历史累计创建 770 万次** | ⚠️ 连接churn严重 |
| MySQL Aborted_clients | 51.8 万 | ⚠️ 连接未优雅关闭 |
| MySQL 慢查询 | 476 条（long_query_time=1s），Threads_running 低位 | ✅ DB 本身不慢 |
| MySQL 死锁 | 10-15 20:53 user_credentials 清理 DELETE 与业务写死锁 | ⚠️ 见死锁问题 |
| performance_schema | **关闭**（无法做 SQL 级归因，靠慢日志兜底） | ⚠️ 可观测性缺口 |
| DB 表容量 | task_messages 234 万行/497MB；agent_tasks 6474 行但 **1.58GB**（prompt/result 大字段） | ⚠️ 膨胀 |
| 大表索引 | events 无 (repo_key, issue_id) 复合索引 | ⚠️ 见索引 |

### 今日接口延迟实测（api_latency_log，10-16）

| 接口 | 次数 | 平均 | 最大 | 说明 |
|---|---|---|---|---|
| /scenes/market/19/experience | 1 | **124369ms** | 124s | 单请求堵队 2 分钟 |
| /api/scenes/skills/coding?page_size=100 | 2 | **24332ms** | 30s | 场景技能大查询 |
| /api/settings/agent-runtimes | 12 | 8898ms | 30s | |
| /api/repos/member | 49 | **3671ms** | 11s | 成员列表慢 |
| /api/scenes（列表） | 15 | 3364ms | 22s | |
| /api/issues/grape-dev/grape | 114 | 1638ms | 4s | 前一天已定位的 N+1（见 issues-center-repo-all-slow-analysis.md） |
| /api/runtime/grape-worker.tar.gz | 5 | 8826ms | 12s | 大文件经 API 进程下发，占 worker |

趋势：全站均值 10-14 → 10-15 → 10-16 = 763 → 914 → **1208ms**，持续恶化。

## 三、根因链（按影响排序）

### 根因 1（结构性，影响最大）：单进程 + 全局 db_lock = 全平台 DB 排队

- `src/mysql_compat.py`：`MySQLCompatDB` 模拟 sqlite3，注释明确"所有操作需在外部
  db_lock 串行化"；`session_manager.db_lock` 是一把全局 `threading.RLock`，
  全仓 60+ 处 `with db_lock` 包住每条 SQL（含网络往返 2~4ms/条）。
- uvicorn 以 `python -m uvicorn src.api.main:app` 启动，**没有 --workers**，
  两台双主各只有一个进程。FastAPI 同步路由靠线程池并发，但线程再多，
  SQL 仍被全局锁串成一条队。
- 推论：一个 124 秒的 experience 请求只要在锁内做长查询/多次往返，
  期间所有请求的 DB 操作全部排队——这就是"一个慢请求拖垮整站"的机制，
  也是为什么 /health（不查 DB）始终 2ms 而查库接口全体变慢。

### 根因 2：已知慢接口集中在锁内长占用

- issues「仓库全部」N+1（get_pipeline_config 无缓存 ×348）——上一份文档已定位，未修。
- /scenes/skills page_size=100、/scenes/market experience、/repos/member 等
  2~30 秒级接口：同类"循环内逐条查库/查外部市场"的模式（本次未逐个展开，
  修法与 issues 相同：循环外批量 + 缓存）。
- grape-worker.tar.gz 走 API 进程下发，下载期间占用进程 IO，应剥离。

### 根因 3：连接 churn 与每线程裸连接

- 每线程 `pymysql.connect` 新建连接（无连接池），线程结束连接即弃；
  Threads_created 累计 770 万、Aborted_clients 51.8 万，20 秒窗口仍在以
  ~2 个/秒 新建。DB 侧频繁握手 + RDS 连接表压力大，也放大每条 SQL 首次延迟。

### 根因 4：DB 侧隐患（当前不致命，正在恶化）

- task_messages 234 万行、agent_tasks 1.58GB 大字段行（prompt/result_text
  mediumtext 常驻 InnoDB，未归档）；Innodb_row_lock_time 累计 14251 秒。
- user_credentials 悬空清理 `DELETE ... NOT IN (SELECT ...)`（src/user/db.py:896）
  与业务写并发产生死锁（10-15 实录）；MySQL 1093 派生表改写后仍锁行 96 条。
- events 表缺 (repo_key, issue_id) 复合索引，OR-pair 查询靠单列索引回表。
- performance_schema 关闭，慢日志阈值 1s，问题定位只能事后靠 api_latency_log。

### 放大器：前端轮询

issues.js 15 秒轮询 + 心跳 60s：今天 frontend 延迟上报 1723 条，
只要有 processing 任务，每个打开页面的用户都持续打重接口（/api/issues 全量 713KB），
与根因 1、2 叠加形成常态负载。

## 四、优化解决方案

### P0（当天可做，不改代码）

1. **MySQL 开 performance_schema**（重启实例生效）：开启 digest 统计后，
   慢 SQL 可精确归因，后续优化有依据。
2. **api_latency_log 已有清理任务**，确认保留阈值 ≤ 7 天（当前 5.4 万行无压力）。
3. 慢日志阈值 1s → 0.5s，配合归因。

### P1（结构性修复，本周）

4. **消除全局 db_lock 串行**（治本）：
   - 方案 A（推荐）：`MySQLCompatDB` 内部改为真正的**连接池**（DBUtils PooledDB
     或 pymysql 池，池大小 16~32），SQL 级锁改为"连接借还"粒度；`with db_lock`
     的外部调用点保持接口不变（兼容层把锁语义内化），分批改造高频路径
     （issues_center、session_manager 读路径先行）。
   - 方案 B（快速止血）：uvicorn 加 `--workers 4`（每台）。进程级隔离天然绕开
     单进程锁排队，改造量最小；注意每 worker 独立内存/定时任务去重
     （cleanup、latency 清理等需加分布式锁或只跑单 worker）。
   - 建议 B 先止血、A 治本，两者不冲突。
5. **修已知 N+1 慢接口**：按 issues-center-repo-all-slow-analysis.md 方案 1/2
   （get_pipeline_config 缓存 + 批量化 + 响应瘦身）；/scenes/skills、
   /repos/member、/scenes/market 同模式排查批量化。
6. **experience 124 秒请求专项排查**：/scenes/market/19/experience 单次 124s，
   大概率是锁内调外部场景市场（HTTP 出网慢）——外部调用必须移出 db_lock、
   加超时（建议 10s）与缓存。

### P2（两周内）

7. **连接池化**（与 P1-4A 合并落地）：消灭 770 万次连接创建；wait_timeout
   28800 偏大，池化后可降到 600。
8. **大表治理**：agent_tasks 归档策略（completed 超 30 天的 prompt/result_text
   移冷表或置空）；task_messages 按月分区/归档（234 万行持续增长）。
9. **索引**：`events(repo_key(191), issue_id(191))` 复合索引；
   `task_messages` 按查询模式补索引（结合 P0-1 的 digest 数据）。
10. **死锁修复**：user_credentials 清理 DELETE 改为低峰定时任务 +
    LIMIT 分批（DELETE ... LIMIT 100 循环），避开业务高峰；加 `repo_gitcode_token`
    等敏感列确认不进清理 SELECT。
11. **worker 包下载剥离**：grape-worker.tar.gz 改由 nginx 静态直出，
    不占 API 进程。
12. **前端轮询降载**：按前份文档方案 3（轻量状态接口 + 变化才全量刷新）。

### 监控建议

- api_latency_log 增加 per-minute 汇总看板（avg/p95/慢请求数），
  当前已有数据源，缺展示；接入现有 CloudEye 告警（p95 > 2s 持续 5 分钟告警）。
- 增加 uvicorn worker 数/队列深度、MySQL Threads_running/连接churn 速率的常规巡检项。

## 五、预期效果

| 指标 | 现状 | P1 后 | P2 后 |
|---|---|---|---|
| 全站平均延迟 | 1208ms | < 500ms | < 300ms |
| 单慢请求对其他用户的影响 | 全站排队 | 限于本 worker | 基本消除 |
| DB 连接 churn | 2/s | 2/s | ~0（池化） |
| /api/issues 列表 | 1.6s avg | < 400ms | < 300ms |

## 六、风险提示

- uvicorn --workers 改造：多 worker 后进程内定时任务（cleanup.py 兜底回收、
  latency 清理、scene 缓存失效）会多实例并发执行，需逐一确认幂等或加锁，
  这是方案 B 唯一的风险点，上线前必须过一遍定时任务清单。
- db_lock 池化改造（方案 A）动的是全平台 DB 访问层，必须灰度：
  先读路径、观察一周、再写路径；回滚开关保留旧锁路径。
- 双主部署：以上代码改动均需 174/79 同步 + 分别重启，重启窗口选低峰。
