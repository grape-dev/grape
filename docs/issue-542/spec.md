# Issue #542 需求文档：优化需求中心/项目场景/流水线页面响应

> Spec 来源：Issue #542 评论（issue agent 发布，Developability: ready）。
> 完整性能分析：`docs/weekly-09/grape-page-perf-analysis-20261030.md`（基线分支 `weekly-09-ljt`）。

## 1. 问题陈述

三个核心页面接口慢（实测，grape-dev/grape 生产数据）：

| 接口 | 页面 | 现状 | 目标 |
|---|---|---|---|
| `GET /api/pipeline/{o}/{r}` | 流水线 | 1.6~1.9s / 1407KB | <0.5s |
| `GET /api/issues/{o}/{r}` | 需求中心 | 0.7~1.2s / 854KB | <0.5s |
| `GET /api/scenes/branches/{o}/{r}` | 项目场景 | ~1.7s（穿透外部 API） | 缓存命中 <0.5s |

## 2. 根因（按贡献排序）

1. **算法**：`_get_issue_pipeline()` 对每个 issue 线性过滤全量 events（766×3457≈265 万次扫描/请求）；已预建 `_events_by_issue` 索引但未传入函数。
2. **无分页**：`/api/issues/{o}/{r}` 描述写 paged 但无 page/per_page 参数，538 条全量返回 854KB。
3. **响应体积**：nginx 未开 gzip，1.4MB JSON 明文传输。
4. **branches 无缓存**：每次实时穿透 GitCode/GitHub API（分页 5 次×100）。
5. （后续）events 无归档持续增长、单 worker + db_lock 串行排队。

## 3. 范围

### In Scope（本 PR）
1. nginx 开 gzip（application/json）。
2. pipeline 接口 history 截断（`history_limit` 参数，默认 50）。
3. issues 列表默认终态截断（active/processing 等非终态全量 + 最近 30 条 completed/cancelled，`recent_terminal` 参数）。
4. `_get_issue_pipeline()` 使用 `_events_by_issue` 索引。
5. issues 列表真分页（`page`/`per_page` + LIMIT/OFFSET）+ 事件聚合按返回集合收窄。
6. branches 接口进程内 TTL 缓存（90s）+ `refresh=1` 手动刷新 + 建分支后失效。

### Out of Scope（拆后续任务）
- events 归档（completed >30 天）、横向扩容、慢接口监控埋点（分析文档第三步）。
- 前端框架级重构、数据库换型、多 worker 架构改造。
- 前端改服务端分页交互（当前前端客户端分页天然兼容有界数据；服务端已暴露 page/per_page 供后续演进）。

## 4. 验收标准（对齐 Spec）

- [ ] `_get_issue_pipeline()` 走索引（O(issues×events) → O(events)）
- [ ] pipeline 接口支持 `history_limit` 截断，响应含 `history_total`
- [ ] issues 接口 `page`/`per_page` 生效（LIMIT/OFFSET，非全量返回），响应含 `total/has_more`
- [ ] issues 默认返回非终态 + 最近 30 条终态（`recent_terminal=0` 或显式 status 过滤时全量）
- [ ] branches 接口 TTL 缓存命中不穿透外部 API；`refresh=1` 绕过；建分支后缓存失效
- [ ] nginx 配置开启 gzip（application/json）
- [ ] 现有测试全部通过（基线既有失败除外）；新增测试覆盖索引/分页/截断/缓存路径
- [ ] 运行验证（起服务压测）交由 sre Agent 执行，编码阶段不做运行时验证

## 5. 用户与价值

grape 全体日常使用者；核心页面体验直接提升（P1）。
