# 流水线页面加载慢分析（2026-10-16）

## 结论（一句话）

流水线页面本身的数据接口 `/api/pipeline/{owner}/{repo}` 并不慢（p50 约 330-350ms）；慢的是页面加载链路上的另外三个接口：**仓库列表 `/api/repos/member`（缓存过期时同步去 GitCode/GitHub 拉全量仓库+权限，观测到 1.2s~7.4s）、分支列表 `/api/scenes/branches/{owner}/{repo`（串行翻页调 GitCode API，p50 1.4~2.6s）、需求列表 `/api/issues/{owner}/{repo`（p50 1.4~1.7s）**。流水线页与这些接口共享首屏加载链路（进入任何依赖仓库的视图前都要先 `loadRepos()`），所以用户体感是"流水线页面慢"。

## 数据依据（应用日志全量分桶统计，10-12 ~ 10-16，节点 B）

### 1. `/api/pipeline/{owner}/{repo}` — 不是瓶颈

| 时段 | n | p50 | max |
|---|---|---|---|
| 10-15 09 时 | 17 | 332ms | 675ms |
| 10-15 12 时 | 5 | 345ms | 418ms |
| 10-15 13 时 | 2 | 354ms | 354ms |

后端 `get_pipeline`（`src/api/routes/pipeline.py:342`）已经做过 N+1 消除（title/stage/scene/bot_usernames 全部批量化），当前耗时合理。grape-dev/grape 仓 1866 条事件全量取回后组装，约 300ms，可接受但有增长风险（见建议 4）。

### 2. `/api/repos/member` — 真正的慢源之一

| 时段 | n | p50 | max |
|---|---|---|---|
| 10-16 01 时 | 22 | 1201ms | 4535ms |
| 10-16 00 时 | 1 | 7308ms | 7308ms |
| 10-15 13 时 | 1 | 7449ms | 7449ms |
| 10-15 09 时 | 2 | 5606ms | 5606ms |

慢的原因（读 `src/api/routes/repos.py:278` 确认）：

- 缓存 TTL 30 分钟（`REPOS_CACHE_TTL=1800`）。缓存命中时 p50 仅几十 ms；**过期后同步执行** `with_oauth_token`（含 token 预刷新）+ 每平台 `_fetch_and_cache` → `fetch_repos_with_role` 从 GitCode `/user/repos` 翻页拉全量仓库，再逐仓做权限查询（N+1）。
- 仓库多的用户（如 wanglei 类 70+ 仓）一次过期刷新就是数秒，且发生在页面导航的关键路径上。
- 注释里写的"27 仓 ≈ 6-10s"（stale probe）已经丢回后台线程了，但**主列表的过期刷新仍是同步的**。
- 前端虽有 localStorage 缓存先渲染（`app.js loadRepos`），但 `/api/pipeline` 只有在拿到 currentRepo 后才发起——首次登录/换页签/缓存清空时，整条链都被 member 拖住。

### 3. `/api/scenes/branches/{owner}/{repo}` — 慢源之二

| 时段 | n | p50 | max |
|---|---|---|---|
| 10-15 13 时(grape-dev/grape) | 2 | 2957ms | 2957ms |
| 10-15 08 时(GTDTeam/GTD-Backend) | 4 | 2191ms | 2582ms |
| 10-15 09 时(grape-dev/grape) | 7 | 1500ms | 2181ms |

原因（`src/api/routes/scenes.py:819 list_repo_branches`）：GitCode 分支按 `per_page=100` 最多翻 5 页，**串行**调用 GitCode API（单次外网调用日志普遍 300-800ms），2-3 页就是 1.5-3s。无任何缓存，每次进 dashboard/场景/autopilot 相关页面都重新拉。该接口虽然不直接属于 pipe 视图，但属于用户常一起打开的页面群，是"平台页面慢"的主要成分。

### 4. `/api/issues/{owner}/{repo}` — 慢源之三

grape-dev/grape p50 1411~1692ms（n=60~86/小时）。需求中心与流水线页数据重叠，用户常从流水线跳需求详情，体感上算同一页面群。

### 外部依赖实测

master 上直接 curl GitCode API：本次手头两个存量 token 均已 401（用户凭证表里的 VCS access token 与任务表里的 repo token 都过期/被刷新），无法给出精确单次延迟；但从应用日志中大量 `GET /api/git-repo/...` 代理请求统计，单次 GitCode 往返普遍 300-800ms（contents 类 p50≈350ms，pulls 类 p50≈800ms）。**每个"翻页/逐仓"循环乘以这个基数就是页面耗时**，这是结构性问题不是网络偶发。

## 建议方案（按收益排序）

1. **`/api/repos/member` 过期刷新异步化（收益最大）**
   与 stale probe 同样处理：缓存过期时先返回旧缓存（`_stale:true`），后台线程刷新 VCS，前端收到 `_stale` 后静默再拉一次。页面不再被 3-7s 的同步外呼卡住。风险低：只是把"同步等"换成"先用旧数据"，与现有 `_cached/_stale` 语义一致。

2. **分支列表加缓存**
   `scenes/branches` 结果写 DB/内存缓存（分支变更频率低，TTL 10-15 分钟足够），并考虑 GitCode 翻页改并发（2-3 页并发取）。高级选项的分支选择器并不要求每次实时。创建分支成功后主动失效对应仓缓存。

3. **fetch_repos_with_role 的逐仓权限 N+1 收敛**
   member 过期路径里每仓一次权限查询，改为一次拉取后批量判定（或权限结果随 user_repos 一起缓存），进一步压缩后台刷新耗时。

4. **`/api/pipeline` 加限流保护（预防性）**
   目前全量 `SELECT ... FROM events WHERE repo_key IN (...) ORDER BY seq` 无 LIMIT，grape-dev/grape 已 1866 条。短期够用；建议给 events(repo_key) 确认索引、并给接口加"最近 N 天 / 最近 M 条"窗口 + 前端分页参数，防止事件量线性增长后 p50 恶化到秒级。

5. **`/api/issues/{owner}/{repo}` 单独复查**
   1.4-1.7s 的 p50 与 pipeline 同为 events/issue 大表查询，建议按慢接口流程单独做一次拐点+索引分析（本次未展开，避免结论发散）。

## 附：涉及代码位置

- 前端入口：`src/api/static/js/app.js:1373-1376`（pipe 视图）、`loadRepos()`（`app.js:661`）
- 仓库列表：`src/api/routes/repos.py:278 list_member_repos`、`_fetch_and_cache`、`_probe_stale_repos`
- 分支列表：`src/api/routes/scenes.py:819 list_repo_branches`（GitCode 翻页循环 873-888）
- 流水线：`src/api/routes/pipeline.py:342 get_pipeline`
- 缓存 TTL：`repos.py:16 REPOS_CACHE_TTL=1800`
