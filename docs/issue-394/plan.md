# Implementation Plan: runtime 引擎重置健康分（Issue #394）

## Overview

补全健康分体系的**写入口**：新增 `POST /api/settings/agent-runtimes/{rid}/health-reset`（复用现有可见性过滤，仅开放 repo/user scope），写入 `health_reset_at` 赦免点并即时重算；前端引擎详情弹窗增加「重置健康分」按钮；补齐测试。

## Architecture Decisions

1. **写点落在 `agent_runtimes.health_reset_at`**：消费侧 `_effective_window_start()` 已实现（Issue-P2 赦免语义），写入口只需写入时间戳 + 清缓存 + force refresh，无需改动调度逻辑。
2. **权限复用 `get_agent_runtimes()` 全量过滤**：与详情端点同口径（credential scope + org 隔离 + repo 过滤），不可见统一 404；再按需求范围限制 `scope ∈ {user, repo}`，`global` 403。
3. **操作记录用 `log.info` 审计**：与现有 health_scorer 日志风格一致，不新增表（避免 schema 变更）。
4. **公共函数 `health_scorer.reset_health(runtime_id)`**：路由薄封装，核心逻辑留在 health_scorer 层便于单测。

## Task List

### Phase 1: 核心逻辑（health_scorer）
- [ ] Task 1: `health_scorer.reset_health(runtime_id)` 写入 health_reset_at + invalidate_cache + force_refresh 重算，返回新健康分；并补 `_effective_window_start` 重置语义测试。

### Phase 2: 管理接口（settings.py）
- [ ] Task 2: 新增 `POST /agent-runtimes/{rid}/health-reset`：可见性过滤 + scope 限制 + 调用 reset_health + log 审计。
- [ ] Task 3: 接口测试文件 `tests/test_settings_health_reset.py`（404 / 403 global / 成功 / 401）。

### Phase 3: 前端入口
- [ ] Task 4: `runtimes.js` 引擎详情弹窗「重置健康分」按钮 + 确认交互 + toast + 刷新；`runtimes.json`（zh/en）补文案。

### Checkpoint: Complete
- [ ] `python3 -m pytest tests/test_health_scorer.py tests/test_settings_health_reset.py -q` 全绿
- [ ] `_effective_window_start` / `reset_health` 单测覆盖四类窗口场景
- [ ] 既有 `is_healthy / filter_healthy / filter_or_fallback` 行为无回归

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| `health_reset_at` 列缺失（老库未迁移） | Med | 写入前 `INSERT OR REPLACE`/UPDATE 前先 SELECT 校验，异常回退并 log.warning |
| global 引擎被误重置影响平台调度 | High | scope 限制：仅 user/repo 可重置，global 返回 403 |
| 重置后窗口语义与预期不符 | Med | 复用已实现 `_effective_window_start`，测试覆盖早于/晚于窗口场景 |

## Open Questions

- 无。