# Issue #3999 — 方案设计

## 1. 分层与落点

| 层 | 文件 | 改动 |
|----|------|------|
| kernel 指标 | 新增 `src/kernel/gsb/issue_card_metrics.py` | 定义 `grape_gsb_issue_card_invalid_ident_total{provider}` 的「可安装收集器 + 无副作用 observe」 |
| 指标注册 | `src/domains/insight/observability/__init__.py` | 启动时 `install(registry)`，与 `comment_metrics` / `cascade_converge_metrics` 同口径 |
| stage 读取 | `src/api/session_store.py` | `get_issue_stage` 的失败出口：invalid-ident 计数 + provider/回退 warning |
| 测试 | `tests/runtime/test_issue_3999_stage_ident_observability.py` | 计数器、warning、回退、数字路径零回归 |

kernel 不依赖 domains（`check-arch` R1 HARD）：metrics 模块只定义收集器，registry 由 insight 域注入，
未安装时 `observe` 为 no-op，worker 不暴露 `/metrics` 也不影响主流程。

## 2. provider 标签归一

复用 `src.kernel.repo_key.parse_repo_key(repo_key)[0]` 取 provider，再收敛到固定枚举
`gitcode|gitee|github|gitlab|other`，避免自建实例名（如 `gitlab-corp`）抬高标签基数。

## 3. `get_issue_stage` 失败出口

```
_num = _issue_num_or_ident(issue_id)
if _num:                       # 可归一：数字 / IK* / autopilot-*
    provider hop:
      ok -> return card
      except -> warning(repo_key, issue_id, ident) + 回退
    *_any 直读:
      ok -> return row
      except -> warning(repo_key, issue_id, ident)   # 原为 pass
else:                          # 不可归一：pr_* / 空 / 垃圾
    if issue_id 非空: observe_invalid_ident(repo_key) + debug
```

- `autopilot-*` / `IK*` 仍走 provider（`card_ident_number` 已放行），行为不变；
- `pr_*` 仍不触达 provider（#4065 契约），仅新增计数与可辨识日志；
- 数字单号路径 `_num` 为 `int`，分支与失败出口逻辑不变。

## 4. 风险与缓解

| 风险 | 缓解 |
|------|------|
| warning 过多污染日志 | 仅在 provider/回退**失败**时 warning；成功路径零新增日志 |
| provider 标签基数 | 固定枚举，未知实例归 `other` |
| 指标未安装 | `observe` 对空 `_INSTALLS` 直接返回（no-op） |
| 静默 except 棘轮 | 本次删除一处 `except: pass`（计数下降，方向正确），不新增静默吞噬点 |