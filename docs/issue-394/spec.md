# Spec: 仓库级与个人级 worker 设备 runtime 引擎「重置健康分」功能（Issue #394）

## Objective

为**仓库级（repo scope）和个人级（user scope）worker 设备**中的 runtime 引擎提供「重置健康分」能力：用户在管理界面点击重置后，引擎的 `agent_runtimes.health_reset_at` 被写入当前时间戳作为赦免点，健康分统计窗口从重置点重新开始，此前误降级/历史故障导致的低分被赦免，引擎可恢复参与调度。

- 用户：仓库成员（repo scope 设备）/ 个人设备 owner（user scope 设备）
- 成功标准：重置后该引擎健康分立即恢复（基于重置点之后的数据重算），调度逻辑不受影响

## 现状（已核实代码）

1. `src/runtime/health_scorer.py::_effective_window_start()` 已实现**消费侧**逻辑：读取 `agent_runtimes.health_reset_at`，重置点晚于 7 天前 → 统计窗口从重置点开始（交集语义，无需清理旧数据）。
2. `agent_runtimes` 表已有 `health_reset_at VARCHAR(64) DEFAULT ''` 列（db_setup.py 迁移已加）。
3. `GET /api/settings/agent-runtimes`（列表，含 health_score）、`GET /api/settings/agent-runtimes/{rid}`（详情）、`GET /api/runtime/admin/machine-health`（运维概览）已存在。
4. **写入口缺失**：代码注释提到「管理员通过 health-reset 接口写入」，但仓库中**不存在** health-reset 写接口，前端也无任何入口。

## 方案（复用现有可见性逻辑 + 新增写接口）

- 后端新增 `POST /api/settings/agent-runtimes/{rid}/health-reset`：
  - 权限复用 `get_agent_runtimes()` 的全量可见性过滤（credential scope + org 隔离 + repo 过滤），引擎不可见 → 404。
  - 仅 `scope=user / repo` 的引擎开放重置；`scope=global`（平台级）不开放 → 403，避免平台核心引擎被误重置。
  - 写入 `health_reset_at = now(UTC ISO)` → `invalidate_cache` → `force_refresh` 立即重算 → 返回 `{runtime_id, health_reset_at, health_score, ...}`。
  - 操作记录：`log.info`（operator / runtime_id / 重置点 / 新健康分），审计留痕，不泄露内部实现。
- `src/runtime/health_scorer.py` 新增公共函数 `reset_health(runtime_id) -> dict`，供路由调用。
- 前端 `src/api/static/js/pages/runtimes.js` 引擎详情弹窗：健康分行旁新增「重置健康分」按钮（二次确认弹窗 + 成功 toast + 刷新详情/列表）。
- i18n：`locales/{zh,en}/runtimes.json` 补 `action.reset_health`、确认/提示文案。

## Tech Stack

- Python FastAPI + SQLite（src.session_manager db/db_lock）
- 原生前端 JS（无框架，I18N 模块 + API 封装）
- pytest（后端测试）

## Commands

```bash
# 测试（相关用例）
python3 -m pytest tests/test_health_scorer.py -q
python3 -m pytest tests/test_settings_health_reset.py -q
# 全量回归（可后续执行）
python3 -m pytest tests/ -q --timeout 300
```

## Project Structure

- `src/runtime/health_scorer.py` — 健康分核心 + 新增 `reset_health()`
- `src/api/routes/settings.py` — 新增 health-reset 路由
- `src/api/static/js/pages/runtimes.js` — 引擎详情弹窗 UI + 交互
- `src/api/static/locales/{zh,en}/runtimes.json` — i18n 文案
- `tests/test_health_scorer.py` — 重置语义单测（新增类）
- `tests/test_settings_health_reset.py` — 接口权限/行为测试（新增）

## Code Style

与现有一致：路由函数带 `@router.post(...)` 完整 summary/description；DB 访问统一 `with _lock(): c = _db().cursor()`；模块内公共函数收集在「公共接口」区；前端沿用 `I18N.t('runtimes.action.xxx')` 与 `app.showToast`。

## Testing Strategy

- `reset_health()`：写入 health_reset_at、清缓存、force_refresh 重算（mock 断言）。
- `_effective_window_start()`：重置点晚于窗口 → 返回重置点；早于窗口 → 返回普通窗口；无重置 → 普通窗口（补齐原缺测试）。
- 接口测试：不可见引擎 404；global scope 403；user/repo scope 成功重置返回新健康分；未登录/无权限 401/403。

## Boundaries

- Always: TDD（先测试后实现）；原子提交；保持既有调度语义（is_healthy / filter_healthy_engines）不变
- Ask first: 不改 `agent_runtimes` 表结构（`health_reset_at` 列已存在）；不改 `_effective_window_start` 语义
- Never: 不开放 global 引擎重置；不写死分支名；不泄露 token/内部实现

## Success Criteria

1. 仓库级（repo scope）worker 设备引擎可触发健康分重置，重置后健康分按新窗口重算恢复；
2. 个人级（user scope）worker 设备引擎同样可触发；
3. 重置有权限控制（不可见 404 / global 403）与操作记录（log 审计）；
4. `tests/test_health_scorer.py` 全部通过，`filter_healthy/is_healthy` 行为无回归。

## Open Questions

- 无（需求经 VOD Agent 收敛 + 代码现状核实后已明确）。