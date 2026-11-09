# Issue #734 方案设计

## 方案概述

服务端 git-repo 代理层在读路径补齐「ref 不存在 → 404」判定：tree 增加 ref 预校验；
contents 维持 #549 上游错误码透传，仅加回归锁定。

## 实现路径

1. `get_repo_tree`（`src/api/git_repo_api.py`）单层路径：
   - ref 空 → 沿用 `_default_ref` 解析默认分支；
   - GitHub + 无斜杠 ref → 保持直连 `git/trees/{ref}`（上游对未知 ref 已 404，零回归）；
   - GitCode + 完整 40-hex sha ref → 直连 `git/trees/{sha}`（sha 非分支名，commits 预校验反成回归）；
   - 其余（GitCode 分支/标签名，含斜杠与不含斜杠）→ 统一经 `/commits?sha={ref}&per_page=1`
     校验存在性并解析 tree sha（复用 `_ref_to_tree_sha`），解析不到 → HTTP 404 +
     `{"code": 404, "message": "ref not found: <ref>"}`（与 `_vcs_req` 透传包络同构）。
2. `_ref_to_tree_sha`（GitCode 分支）：
   - 解析失败（空列表/坏结构）由「保序降级返回原 ref」改为返回 `None`（调用方归一 404）；
   - `HTTPException`（含上游 404/403 透传）不再吞掉，直接上抛；
   - GitHub 路径不变（`_github_tree_sha` 失败兜底返回原 ref，由上游 404 兜底）。
3. 测试：
   - `tests/test_git_repo_tree_slash_ref.py`：不存在 ref（空 commits / 上游 404 / 坏结构，
     含斜杠与不含斜杠）均断言 404；新增 sha ref 直连零回归断言；
   - `tests/test_git_repo_api.py`：contents 深路径/根目录/不存在分支 404 回归矩阵。

## 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|---|---|---|---|
| contents 不存在路径返回形态 | 404 透传 vs 空对象带 error | 维持 404 透传 | #549 已实现且有测试覆盖 |
| tree 不存在 ref 判定位置 | 服务端预校验 vs 仅文档 | 服务端预校验 | 上游 `git/trees` 对未知 ref 返回 200 空树 |
| sha 形态 ref | 也预校验 vs 直连 | 直连 | commits?sha= 对 tree sha 会 404，预校验反成回归 |

## 风险与缓解

- 非斜杠分支名 ref 增加一次上游请求（Spec 已接受，Low）。
- 依赖「空树」判断的降级逻辑：vod-runbook 已将降级条件修正为「404 或空树」；发布说明标注行为变更。
