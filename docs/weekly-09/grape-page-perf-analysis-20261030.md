# grape-dev/grape 需求中心 / 项目场景 / 流水线 页面慢 —— 原因分析与技术方案

日期：2026-10-30
环境：双主 174/79，grape-main 单 uvicorn 进程 :8000，MySQL 8.0.43 @172.16.11.181
数据规模：events 全表 31103 条，本仓 3457 条（10 月单月已 +3930，增长加速）；issue 表本仓 538 行（其中 completed 227 行）；766 个 issue_id 维度（含 PR 订阅伪 ID）。

## 一句话结论

页面慢不是机器慢、不是数据库慢、也不是网络慢——是三个"全量接口"每次请求都在单进程里把全仓几百个 Issue、几千条事件从头到尾重算一遍（Python 端 CPU 打满 1.5 秒），再把 0.8~1.4MB 的未压缩 JSON 一次吐给浏览器；并发时全部串行排队，所以越用越慢。

## 实测证据（本机 174 直连 8000，用户级 API Key）

| 接口 | 页面 | 耗时 | 响应体积 | 备注 |
|---|---|---|---|---|
| GET /api/pipeline/grape-dev/grape | 流水线 | 1.6~1.9s | 1407KB | 每次 CPU 消耗 ~1.5s（/proc jiffies 实测） |
| GET /api/issues/grape-dev/grape | 需求中心 | 0.7~1.2s | 854KB | page/per_page 参数被忽略，全量返回 538 条 |
| GET /api/scenes/branches/... | 项目场景 | ~1.7s | 小 | 实时调 GitCode/GitHub 外部 API 拉分支，外部时延 |
| GET /api/scenes、/scenes/with-pipeline、/issues/mine | 各页 | 5~35ms | 小 | 不慢 |
| 4 个慢接口并发 | — | 单个变 3.6~4.3s | — | 全局 db_lock + 单进程，串行排队 |
| MySQL 端同款 SQL | — | 0.03s | — | 数据库不是瓶颈 |

79 主（m2）直连同接口 1.44s，行为一致；ELB/nginx 增量 <0.1s，不是瓶颈。系统 load 1.15/8 核、内存充足，资源不是瓶颈。

## 根因分解（按贡献排序）

1. 【主因·算法】/api/pipeline 的 `_get_issue_pipeline()`（pipeline.py:101）对每个 issue 做 `issue_events = [e for e in events if e["issue_id"]==issue_id]`——代码里明明预建了 `_events_by_issue` 索引（pipeline.py:478），但没传进函数，仍传全量 events 列表。766 个 issue × 3457 条事件 ≈ 265 万次 dict 扫描/请求，纯 Python CPU ~1.5s。历史上做过一轮"消除 N+1"（批量 title/stage/cfg 缓存，注释都在），但漏掉了这个最后一层 O(issues×events)。
2. 【主因·无分页】/api/issues/{o}/{r} 描述写 "paged" 但函数签名根本没有 page/per_page 参数，538 条全量返回 854KB；每个用户每次进需求中心都全量拉。
3. 【放大·响应体积】nginx 全局未开 gzip，1.4MB JSON 明文传输（内网快感觉不到，公网用户弱网下翻倍慢）。
4. 【放大·并发串行】grape-main 是单 uvicorn worker，且 session_manager 的 `db_lock = threading.RLock()` 全局锁包住所有 DB 访问——并发请求既抢不到第二个进程也抢不到并行线程，4 个并发 = 排队 4 秒。用户多时"每个人都在等别人的请求"。
5. 【持续恶化·数据只增不减】events 无归档/截断，10 月单月 +3930 条；completed 的 227 个 Issue 仍在每次请求里全量重算（active 只占少数）。
6. 【项目场景页特有】/api/scenes/branches 每次实时穿透调 GitCode API（分页 5 次×100），外部时延 ~1.7s 且无缓存——这个页面慢主要是外网调用，与上面不同源。

## 建议技术方案

### 第一步：零代码风险，立竿见影（当天可做）

1. **nginx 开 gzip**（application/json）：1.4MB → 约 100~150KB，公网用户体验直接改善。
   ```
   gzip on; gzip_types application/json; gzip_min_length 10k; gzip_comp_level 5;
   ```
2. **流水线/需求中心接口加"最近 N 条"上限**：pipeline 响应里 history 截断为最近 30~50 个 Issue（前端本来就分页/滚动展示）；issues 列表默认只返回 active/processing + 最近 30 条 completed，翻页再拉。可以作为纯查询参数默认值调整，前端不改也能先受益（数据变少）。

### 第二步：代码修复（一个小 patch，收益最大）

3. **把 `_events_by_issue` 索引真正用起来**：`_get_issue_pipeline()` 增加 `events_by_issue` 参数，函数内直接 `issue_events = events_by_issue.get(issue_id)`，删掉全量线性过滤。O(265万) → O(3457)，预计 1.9s → 0.2~0.3s。改动局限在 pipeline.py 单文件、纯读路径，风险低；双主同步部署。
4. **issues 列表实现真分页**：签名加 page/per_page（带默认值兼容旧前端），SQL 加 LIMIT/OFFSET 或 keyset（issue_number < last）。
5. **branches 接口加缓存**：结果 TTL 60~120s（仓库分支变化频率低），或 ETag；避免每次都穿透 GitCode。

### 第三步：结构性治理（排期）

6. **数据生命周期**：用已有的 event_archive 机制把 completed 超 30 天的 Issue 事件归档出主表，控制热数据规模；或给 pipeline 接口按月分窗查询。
7. **横向扩容评估**：MySQL 后端已就绪，理论上可多 worker/多实例；但需先盘点进程内状态（session_manager 内存缓存、login_throttle、_pending_active、get_pipeline_config 的进程级 TTL 缓存等——多进程后行为变化要逐一确认）。短期不建议动，先吃掉前两步收益。
8. **监控**：给 /api/* 加耗时埋点（或 nginx access log 的 $request_time 报表），慢接口持续可见。

## 预期收益

- 步骤 1+2：响应体积降 90%，传输时间大幅下降；服务端不变。
- 步骤 3：pipeline 接口 1.9s → ~0.3s（约 6 倍）。
- 步骤 4+5：需求中心/项目场景进入秒开。
- 步骤 6：保证半年后不复现。

## 附：本次测量用临时 API Key

为复现登录态耗时，在 api_tokens 表为 uid=39 插入了一条 `grape_` 前缀临时 Key（name=perf-diag-temp），**测完建议撤销**：
`UPDATE api_tokens SET revoked=1 WHERE name='perf-diag-temp';`
