# Spec: Issue #364 — Workers 设备卡片"承载引擎"部分引擎点击不弹详情

> 基准分支：`功能分支` | 评级：P1 bug | Spec 源：Issue #364 Spec Document 评论

## Objective
修复 Agent 运行时页面 Workers 视图设备卡片"承载引擎"区域部分引擎点击无响应的缺陷：`openEngineDetail` 仅在 Engines 分页缓存（`__runtimeData.runtimes`）中查找，未命中即静默 `return`。

## 根因
- Workers 视图数据来自 `/settings/runtime-devices`（设备维度分页），设备内 `runtimes[].id` 与 Engines 列表 `/settings/agent-runtimes`（引擎维度分页，仅第一页 20 条 + scope/org 过滤）集合天然不一致；
- `openEngineDetail(runtimeId)`（runtimes.js）查找失败后无任何反馈 → 点击无响应。

## Scope
- In：`openEngineDetail` 多数据源查找（Engines 缓存 ∪ 设备数据索引），未命中回退 `GET /settings/agent-runtimes/{rid}` 异步拉详情（loading 态 + 失败 toast）；后端新增单引擎详情端点（复用列表可见性校验）；端点 API 测试。
- Out：Engines 视图交互、设备卡片其余区域、健康分计算、i18n 新词条。

## 验收标准
1. Workers 视图每个设备的所有承载引擎点击均弹详情（含本机直连、不在 Engines 第一页的引擎）；
2. Engines 视图弹窗行为不回归；弹窗含名称/类型/版本/健康状态/健康分/worker/IP/scope/owner；
3. 不可见引擎点击有 toast 反馈；新端点对无权限用户返回 404；
4. 现有测试全过 + 新增端点测试通过。
