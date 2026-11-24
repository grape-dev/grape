# Issue #3756 任务拆解

> 目标合入分支：`arch-weekly-11-ljt` | 2026-11-22

| # | 切片 | 交付物 | 验证 |
|---|------|--------|------|
| 1 | 文档 | `docs/issue-3756/{spec,plan,task}.md` | 方案评审 |
| 2 | 红：渲染层降级用例 | `tests/api/test_openapi.py` 新增 registry 缺失用例 | 先红（当前抛 RuntimeError） |
| 3 | 红：打包/预检用例 | `tests/api/test_release_pack_openapi_filter.py` 新增 contracts 断言；`tests/api/test_deploy_preflight.py` 新增缺登记表 exit 1 用例 | 先红 |
| 4 | 绿：渲染层降级 | `src/api/openapi.py` | `pytest tests/api/test_openapi.py -k degraded` 转绿 |
| 5 | 绿：打包期断言 + contracts 入包 | `scripts/release_pack.py` | `pytest tests/api/test_release_pack_openapi_filter.py` 转绿 |
| 6 | 绿：预检契约项 | `scripts/deploy_preflight.py` | `pytest tests/api/test_deploy_preflight.py` 转绿 |
| 7 | 回归 | — | `pytest tests/api/test_openapi.py tests/api/test_release_pack_openapi_filter.py tests/api/test_deploy_preflight.py tests/contracts/test_contract_deprecation.py` 全绿 |
| 8 | 自审查 | 5 轴 + pre-pr 增量检查 | `origin/arch-weekly-11-ljt...HEAD` |
| 9 | 建 PR + Issue 评论 + A2A QA | PR / 评论 | `grape tasks complete` |

## 原子提交

1. `docs(#3756): 补充规格与方案设计`
2. `test(#3756): 先写登记表缺失降级/打包/预检失败用例`（红）
3. `fix(#3756): 打包纳入 contracts 并新增登记表降级与预检`（绿）

## 复审整改（PR #1968 审查 R1/R2）

| # | 切片 | 交付物 | 验证 |
|---|------|--------|------|
| R1 | 必需文件清单单一来源 | 新增 `src/domains/contracts/registry_files.py`；`release_pack.py` / `deploy_preflight.py` 共同引用 | 打包/预检清单一致用例；缺 `openapi_baseline.json` / `required_operations.json` → 预检 exit 1 |
| R2 | 降级只表达「不可用」 | `deprecation.RegistryUnavailable`；`openapi.py` 仅捕获它 | 缺失 → 降级；损坏 / 漂移 → `RuntimeError` fail-fast |
| Doc | 部署顺序 | `deploy/README.md` 写明「解包 → 预检 → 起服务」与 `--contracts-dir` | 文档核对 |