# Spec: Issue #544 — A2A 交接链路容错（L1–L4 服务端实现）

> 本文件为 Issue Agent 已发布 Spec 的开发落地版（原文见 Issue #544 评论），范围一致：L1 统计异步化 → L2 幂等键 → L3 留痕核对 → L4 兜底推广。

## 目标（验收口径）

1. **L1**：SLO 可用率评估（API 请求路径）不再对 rollup 缺失的闭合小时做同步 raw-scan 回填；缺失段改为后台限流队列离线补齐，评估结果透出 `avail_backfill_pending_hours`。头部不完整小时/当前小时的有界小段现算保留（分钟级数据量，非阻塞源）。
2. **L2**：`POST /api/a2a` 支持幂等键（body 顶层 `idempotency_key` 或 `payload.idempotency_key`，客户端由来源 task_id+目标角色派生）；events 表新增 `idempotency_key` 列 + `(repo_key, idempotency_key)` 唯一索引；键冲突返回已有事件（`idempotent: true` + 原 seq），不创建新事件。
3. **L3**：A2A 请求处理异常时在 events 表写入失败留痕记录（含请求指纹 = sha256(repo_key|issue|agent)[:16]，与实现一致，不含 pr）；新增 `GET /api/a2a/status` 核对接口（按幂等键/指纹查询交接事件是否已建）。
4. **L4**：`pull_request_merged` 无任何路由命中时默认派发 sre（限 sre agent 启用的仓库 = 所有启用 A2A 的仓库）；merge→sre 断链（自动重试耗尽/派发失败未安排重试）时向关联 Issue 发提示评论。

## 非目标

- SLO 统计口径修正；其他端点幂等化；grape CLI/技能仓库内的模板文本更新（超时 60s、带键重试 ≤2、超时后自动核对属技能仓库条款，本仓只提供服务端能力 + help 文档）。
- 独立 worker 进程（本期进程内队列+限流，保留演进空间）。

## 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| L1 载体 | 进程内单 worker 线程 + 队列 + 最小间隔限流（`src/observability/slo_backfill.py`） | 最小改动消除 API 卡死；`obs_rollup` 60s 循环已有先例 |
| 零样本小时的覆盖标记 | 回填写 `engine_avail_total=0` 行（行存在 = 已覆盖） | 消除"无样本小时永远缺失 → 每次评估重复 raw-scan"的慢性阻塞源 |
| 幂等键唯一性 | `(repo_key, idempotency_key)` 唯一索引，键空 = NULL | SQLite/MySQL NULL 均可重复；跨仓同键不误伤 |
| 留痕记录 | 失败留痕事件不占幂等键（键仅成功建事件占用），指纹放 payload | 留痕不阻塞同键重试 |
| 断链提示 | 复用 `_post_result_to_issue`（含去重）以 sre 身份发评论 | 复用既有回帖链路与 bot token 解析 |
