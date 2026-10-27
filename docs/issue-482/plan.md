# Plan: Issue #482

## 方案
1. `src/api/event_pipeline.py`：
   - 从 `register_sre_deploy_url` 抽出 URL 提取校验为共享内部函数 `_extract_deploy_url(result_text) -> str`（结构化 artifacts/issue_summary 优先 + 完整 URL 正则回落 + http(s)/≤512 校验），`register_sre_deploy_url` 改为调用它后写 `managed_repos`（行为不变）。
   - 新增 `register_scene_deploy_url(repo_key, issue_id, result_text) -> str`：
     - `get_issue_scene(repo_key, issue_id)` 取 issue 绑定 scene_id（Issue 级快照，空=无绑定 → 跳过返回 ""）。
     - `_extract_deploy_url` 提取校验（空 → 跳过）。
     - `UPDATE devops_pipeline_scene SET deploy_url=? WHERE scene_id=? AND repo_key=? AND is_active=1`（rowcount=0 不写库）；成功打日志 `scene deploy_url auto-registered: {url} for scene {scene_id}`；异常捕获记 warning，不影响主流程。
2. 直连路径：`_execute_agent_inner` 收尾（现 `register_sre_deploy_url` 调用处）并列调用 `register_scene_deploy_url(repo_key, issue_id, result_text)`，独立 try/except。
3. 远程路径：`src/runtime/service.py` `complete_task()` 钩子 #6 旁并列同调用（`_repo_key` / `_issue_id`），独立 try/except。
4. 测试 `tests/test_issue_482_scene_deploy_url_register.py`：仿 #468 测试（独立测试库 + 种子）+ 双路径调用点 source 检查。

## 测试 seam
- 公开函数 `register_scene_deploy_url`（DB 行为）；
- `complete_task()` 远程钩子端到端；
- 双路径源码含调用点（防遗漏回归，仿 test_issue_session 的 source 检查模式）。

## 边界
- exit_code≠0：钩子外层 `if exit_code == 0` 门（沿用 #468 结构）不触发。
- 登记失败：内部捕获，收尾主流程不受影响。
