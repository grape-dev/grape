# Plan — Issue #582

## 方案
创建 `scripts/atom_selfcheck.py`（基线未物化，见 spec.md 现状），实现：
- `PATH_ENV_KEYS = {"GRAPE_SRC_DIR"}` 白名单；
- `anchor_env(extra_env)`：白名单相对值拼仓根；非白名单键经 `os.path.exists(仓根/value)` 探测兜底；URL/绝对路径/非字符串透传；
- `run_case(name, command, extra_env, ...)`：merge `os.environ` + `anchor_env` 结果后 `subprocess.run`，返回结构化结果（passed 据实于 returncode）；
- CLI：`--env KEY=VALUE ... -- CMD` 便于本地复现。

## 决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 锚定判定 | 白名单优先 + 探测兜底 | Spec 方案 C |
| 白名单初始成员 | 仅 GRAPE_SRC_DIR | 最小化，按需扩展 |
| 文件创建 vs 修改 | 创建（基线无此文件） | 仓与全历史均未物化，按契约从源头实现 |

## 步骤
1. spec/plan/task 文档落仓（docs/issue-582/）。
2. scripts/atom_selfcheck.py 实现 + docstring 契约标注。
3. tests/atomic/test_issue_582_env_anchoring.py 回归用例（RED→GREEN）。
4. 自审查（5 轴 + pre-pr 增量检查）→ 提交 → PR base=weekly-08-mys。
