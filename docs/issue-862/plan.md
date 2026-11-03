# Plan: Issue #862 — 实施方案设计

## 改动文件清单

| 文件 | 改动 | 级别 |
|------|------|------|
| `src/api/routes/scenes.py` | 体验模板创建超时 30→45：新增模块级常量 `EXPERIENCE_TEMPLATE_CREATE_TIMEOUT = 45`，`experience_market_scene` 内 `asyncio.wait_for(..., timeout=EXPERIENCE_TEMPLATE_CREATE_TIMEOUT)`，注释更新（#862：全流程 ≈30s 压线撞闸，45s 覆盖实测分布） | P0 |
| `tests/test_issue_862_experience_timeout.py` | 新增：常量值断言 + 路由源使用常量断言（防回退） | 测试 |
| `docs/issue-862/*.md` | spec / plan / task 文档 | 文档 |

## 设计决策

- **为什么 45s**：实测全流程 ≈30s（push ~16s 占大头）+ 抖动余量 ~50%，45s 覆盖慢尾；同时远低于外层 nginx 300s 与前端 300s，不引入新的层级穿透。
- **为什么不动内层 git 超时**：clone 120s / push 180s 是操作级上限，与接口级闸门职责不同；接口闸门放宽即可。
- **为什么用命名常量而非魔数**：#357 P1-C 引入 30 时为内联字面量，导致后续调整需读源码定位；常量化使超时策略可检索、可测试锁定。
- **超时后行为不变**：仍抛 504 +「请重试」；假死线程由既有幂等路径兜底（重试命中「仓库已存在」秒过），异步化根治归 M2。

## 回滚

- 单点常量回滚：`EXPERIENCE_TEMPLATE_CREATE_TIMEOUT = 45 → 30`，无 schema/接口契约变更。

## 风险

- 45s 内接口占用一个事件循环等待位（wait_for + to_thread 不阻塞循环本身，风险与 30s 版本相同，仅时长拉长）。
- 极慢场景（>45s）仍会 504，但按实测分布（≈30s 压线）已消除主撞闸区间；根治走 M2 异步化。
