# Tasks: Issue #1660

- [x] 全局定位 `REPO_OWNER/REPO_NAME/REPO_FULL_NAME` env 组装点
- [x] 新增 `derive_repo_identity` 单一派生入口（`src/repo_key.py`）
- [x] 事件入队 / 失败快照 / 事件重派 `build_task_env` 改走派生入口
- [x] direct 执行 `_execute_agent_inner` 由 `repository` 派生（不再用触发人账号）
- [x] autopilot / VOD 路径补齐仓库身份变量
- [x] chat/IM/Web 新增 `chat_repo_env` 并注入
- [x] 新增 `tests/test_issue_1660_repo_env_derivation.py` 多路径/异常输入单测
- [x] 回归相关既有测试
- [x] 技能 runbook 兜底校验转 grape-skills 仓单独建单跟进（交付说明注明）