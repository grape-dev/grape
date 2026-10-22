# Plan: Issue #411 — scenes market 分页参数校验

## 方案选择
| 决策 | 选择 | 理由 |
|------|------|------|
| 校验层级 | FastAPI `Query(ge=1)`（路由签名层） | 与现有 FastAPI 风格一致；非法值在 Pydantic 校验阶段返回 422，不进入业务/SQL |
| 非法值响应码 | 422 | FastAPI 默认语义；Acceptance 允许 400/422 |
| 异常脱敏 | 全局兜底（已有）+ 本链路无需路由 catch | `list_scene_market` 无 catch → DB 异常直抛全局 handler（已脱敏）|
| 公共分页依赖抽象 | 不做（Out of Scope，#412 统一收敛） | 本单仅单点修复 |

## 实现步骤（依赖序）
1. **RED**：新增 `tests/test_issue_411_scenes_market_page_validation.py`
   - 复现用例：`page_size=-1`、`page=0`、`page_size=0`、`page=-1`、非整数 → 修复前 500 / 修复后 422
   - 回归用例：合法分页 200；`featured_only=1&limit=5` 精选 200；全局异常脱敏（global_exception_handler 单测）
   - 修复前跑测试确认失败（RED）
2. **GREEN**：`src/api/routes/scenes.py`
   - 顶部导入 `Query`
   - `page: int = Query(default=1, ge=1)`、`page_size: int = Query(default=20, ge=1)`
3. **验证**：跑新测试 + 相关回归（test_scene_skill_market.py / test_issue_395_bugfixes.py）
4. **Refactor/自审查**：确认 OpenAPI 契约不改破坏、无其他端点受影响

## 风险与缓解
| 风险 | 缓解 |
|------|------|
| 前端传 `page=0` 依赖旧行为 | 无：前端分页正常从 1 开始；422 是正确语义 |
| Query 默认值语义变化 | `default=1` / `default=20` 与原 `int = 1/20` 一致，仅新增 ge 约束 |
| 全局异常脱敏被绕过 | 已核实本链路无 catch 绕过；3 处 `detail=str(e)` 不在此链路 |

## 验证命令
```bash
python3 -m pytest tests/test_issue_411_scenes_market_page_validation.py -v
python3 -m pytest tests/test_scene_skill_market.py -v
python3 -m pytest tests/test_issue_395_bugfixes.py -v
```