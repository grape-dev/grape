# Plan: Issue #364 — 实施方案设计

## 改动文件清单

| 文件 | 改动 | P级 |
|------|------|-----|
| `src/api/routes/settings.py` | 新增 `GET /agent-runtimes/{rid}`（operation_id `settings_agent_runtime_detail`），内部复用 `get_agent_runtimes` 全量可见性过滤+字段派生，按 id 精确返回，未命中/不可见 404 | P1 |
| `src/api/static/js/pages/runtimes.js` | 模块级 `_rtEngineIndex`（id→engine）；`renderRuntimes` 后重建索引（Engines 缓存 ∪ devices[].runtimes，设备来源补齐 worker/scope/owner 字段并标记 `_partial`）；`openEngineDetail` 重构为多源查找 + 异步回退（loading 弹窗 + 失败 toast）+ `_partial` 后台补全 | P1 |
| `help/api/13-settings.md` | 补 `GET /api/settings/agent-runtimes/{rid}` 文档块（契约-文档一致性测试要求） | P1 |
| `tests/test_api_routes.py` | 新增详情端点测试：可见引擎 200（字段完整性）、不存在/不可见 id 404、user scope 他人引擎 404 | 测试 |

## 关键设计决策
- 详情端点不复刻 100 行可见性/派生逻辑，直接调用 `get_agent_runtimes(user, page_size=0)` 后按 id 过滤 —— 可见性口径（credential scope + org 隔离 + repo 过滤 + 已删除用户/吊销凭据过滤）与列表 100% 一致，零逻辑漂移。
- 前端设备来源的索引条目为部分字段（缺 health_score 等），同步渲染保证即时响应，同时后台异步拉详情端点整弹窗替换补全（满足"弹窗含健康分"验收）。
- 失败 toast 复用现有词条 `common.load_failed`，不新增 i18n。

## 回滚
单 commit 纯增量改动，revert 即回滚；无 DB schema 变更、无配置项。
