# Issue #3999 — get_issue_stage GSB provider hop 非数字单号 422 的观测补齐

> 开发基线：`arch-weekly-11-ljt`
> 关联：#4065（PR #2063 已消除 422 成因）、#3739

## 1. 背景

生产 `get_issue_stage()` 对 `autopilot-*` / `pr_*` / Gitee `IK*` 三类非数字 ident 走 GSB provider
hop，`/internal/issues/card` 恒 422（基线 561 次/小时），异常被 `except Exception` 吞成一条 debug
日志后静默回落到 legacy `*_any` 直读。

`#4065`（PR #2063）已把根因消除：

- `/internal/issues/card` 的 `issue_number` 由 `int` 改为 `str`，入口先 `normalize_card_ident`；
- `_issue_num_or_ident` 收敛到内核 `card_ident_number`，`pr_*` 伪 id 直接返回 `None`，不再触达卡片端点。

即“422 成因”和“三类 ident 取 stage”已解决，但 Issue #3999 验收里的**观测项**（验收 3/4）仍未落地：

- 没有 `grape_gsb_issue_card_invalid_ident_total{provider}` 计数器；
- provider 失败仍只有 `log.debug`，不带 ident / `repo_key`；
- `*_any` 回退读取失败仍是 `except Exception: pass`，失败不可见。

## 2. 目标

1. 新增 `grape_gsb_issue_card_invalid_ident_total{provider}` 计数器：ident 无法归一为卡片主键时计数，
   `provider` 限枚举（`gitcode|gitee|github|gitlab|other`）控制标签基数。
2. provider hop 失败由 `debug` 提升为 `warning`，日志携带 `repo_key`、`issue_id` 与归一后的 `ident`。
3. `issue_card_row` 回退直读失败由静默 `pass` 改为 `warning`（携带 `repo_key` / `issue_id` / `ident`），
   不新增任何静默 `except`。
4. 数字单号路径零回归：`_num` 为 `int` 时行为与失败出口计数逻辑不变。

## 3. 非目标

- 不重复实现 #4065 的 ident 归一 / 卡片端点契约（已在基线）；
- 不改 autopilot 调度、`get_issue_stage` 的 DB 读取语义；
- 不新增告警规则（本单只补计数与日志可见性）。

## 4. 验收标准

- [ ] `autopilot-*` / `IK*` 归一成功路径行为不变（provider 正常查询卡片）；
- [ ] `pr_*` 等不可归一 ident 不触达 provider，`invalid_ident_total{provider}` 递增；
- [ ] provider 抛异常时打 `warning`（含 `repo_key` 与 `ident`）并回退到 `*_any`；
- [ ] `*_any` 回退读取异常时打 `warning`（含 `repo_key` 与 `ident`），不再静默；
- [ ] 数字单号路径无回归，既有 `get_issue_stage` 用例保持绿。

## 5. 命令

```bash
python3 -m pytest tests/runtime/test_issue_3999_stage_ident_observability.py -q
python3 -m pytest tests/api/test_issue_4065_issue_card_ident.py tests/runtime/test_issue_state.py -q
make lint
python3 scripts/check-arch.py
```