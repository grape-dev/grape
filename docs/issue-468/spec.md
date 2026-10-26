# Spec: Issue #468 — SRE deploy_url 自动登记双路径统一

> 完整 Spec 见 Issue #468 评论（comment id=196026176）；定位分析：docs/weekly-08/deploy-url-not-registered-analysis.md。

## 目标
- 远程 worker `complete_task()` 路径执行 SRE 后，`managed_repos.deploy_url` 自动更新（互联网小球显示新地址）。
- 修复旧正则 `https?://[\d\w.\-]+(?::\d+)?(?:/)?` 截断 URL 资源路径的缺陷。

## 范围
- `src/api/event_pipeline.py`：抽公共函数 `register_sre_deploy_url(repo_key, result_text) -> str`，替换 `_execute_agent_inner` 内 L4085-4109 内联登记块。
- `src/runtime/service.py`：`complete_task()` post-completion 钩子清单补第 6 项（sre 且有 result_text → 延迟导入调用，try/except 防护）。
- 新增 pytest 单测 `tests/test_issue_468_sre_deploy_url_register.py`。

## Out of Scope
- 前端小球展示链路、手动 `PUT /repos/deploy-url` 语义、`pending_review` 覆盖核查、双主部署与回填 SQL（sre 环节执行）。

## 验收标准
1. 远程/直连两条 SRE 收尾路径均自动登记 deploy_url，日志出现 `SRE deploy_url auto-registered: ... for <repo_key>`。
2. 完整 URL（含资源路径）不截断；非法/超长（>512）URL 拒绝；SRE 失败（非 completed）不触发。
3. 幂等：重复登记同一 URL 无副作用。
