# 需求文档（Spec 对齐）：Issue #1229

> 来源：Issue #1229 + Issue Agent 已发布 Spec（Issue 评论「📋 Spec Document」）。本文件为编码前的需求锚定，与 Spec 一致，不引入新范围。

## 目标
在运行驾驶舱「任务与日志」模块内新增「能效看板」视图，按**当前仓库**维度、**按协作者**聚合展示：

- 提需求数量（该用户作为创建人提出的 Issue 数）；
- 完成数量（该用户提出的 Issue 中已 completed 的数量）；
- 消耗 token 数（该用户需求驱动的任务产生的 LLM token 用量合计）。

价值：让仓库成员直观查看与对比各协作者的贡献与 AI 资源消耗。

## 范围（In Scope，Spec 四条）
1. 「能效看板」视图入口：运行驾驶舱（可观测性页）「任务与日志」分区内的子 Tab，与现有「任务/事件/错误日志」Tab 同级，风格与现有驾驶舱一致。
2. 后端聚合统计端点（`GET /api/dashboard/efficiency?repo=...`）：按仓库 + 协作者输出 issue 提出/完成数 + token 消耗合计。
3. 协作者识别：按 Issue 创建人聚合（`issue.created_by`，空回退 `assignee`）；token 消耗关联该用户需求驱动的任务用量记录。
4. 无协作记录时的空态展示。

## Out of Scope（Spec 明确不做）
- 明细下钻与时间范围筛选（放后续迭代）。
- 跨仓库统计、权限体系改动、看板数据导出。
- 图表库引入（复用现有 KPI 卡 + 条形排行样式，不新增依赖）。

## 关键设计决策（Spec 已定 + 开发落地口径）
| 决策 | 选择 | 理由 |
|------|------|------|
| 聚合计算位置 | 后端聚合 API | 数据分散在 `issue` 与 `obs_llm_usage`/`agent_tasks`，后端一次聚合避免前端多请求 |
| 提需求/完成口径 | `issue` 单表（C3 权威）：`created_by` 空回退 `assignee`，按人 GROUP BY；完成 = `status='completed'` | `get_issue_stage` 已声明 issue 单表为权威；任务中心列表同源 |
| token 统计口径 | `obs_llm_usage`（`run_kind='task'`）→ `agent_tasks.issue_id` 回链 `issue` 创建人，按创建人 SUM | Spec「按用户需求驱动的任务关联的用量」；agent_tasks.user_id 多为 0 不可用，issue 归属链才是需求维度 |
| token 合计公式 | input+output+cache_read+cache_write（**不含 reasoning**） | 与 `src/observability/tokens.py` 的 `total` 同口径（reasoning 是 output 子集，加则重复计） |
| 协作者排除 | bot 用户名（`get_all_bot_usernames()`）与子任务行（`is_subtask_mode=1`）不计入 | bot 是执行方不是需求方；子任务归属父需求 |
| 展示形式 | KPI 汇总卡 + 协作者排行（表格/条形，参考附件图） | 与设计参考图一致，复用现有 `section()`/`_kpiRowHTML()` 助手 |

## 验收标准（Spec Acceptance Criteria）
- [ ] 运行驾驶舱「任务与日志」模块可见「能效看板」入口，界面风格与现有驾驶舱一致。
- [ ] 看板正确展示每个协作者的提需求数量、完成数量、消耗 token，数据与实际任务/事件记录一致，且仅统计当前仓库。
- [ ] 无协作记录时展示合理空态。
- [ ] 统计端点有测试覆盖（聚合正确性 + 空数据 + 鉴权 + 仓库隔离）；所有现有测试通过。

## 风险与缓解（Spec）
| 风险 | 缓解 |
|------|------|
| 用量记录与用户归属关联不完整（历史数据缺 created_by / 无用量行） | created_by 空回退 assignee；无用量按 0 展示，不阻塞页面 |
| 聚合查询在仓库协作人多/数据量大时变慢 | SQL 先按 task_id 聚合再 Python 归属，行数=任务数；协作者列表 TopN 截断（默认 50） |
| created_by 存量缺失 | 回退 assignee（webhook 兜底路径同样落 assignee） |

## 测试 seam（编码前声明）
- 后端 seam = 公开 HTTP 端点 `GET /api/dashboard/efficiency`（TestClient + 真实测试库），与 `tests/test_issue_410_dashboard_ops_auth.py` 同风格；数据输入 seam = `issue` / `obs_llm_usage` / `agent_tasks` 种子行（真实 DB 行为，不 mock 内部）。
- 前端 seam = `renderObservability` / `renderObsEfficiency` 公开渲染函数 + `observability.json` i18n 键（node 沙箱断言），与 `tests/js/test_anomaly_page_size.js` 同风格。
