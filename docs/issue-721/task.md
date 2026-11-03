# Task: Issue #721

- [x] T1 定位 runbook 实体（GTDTeam/CodeArts .grape/skills/codearts-deliver-upload/references/deliver-upload-runbook.md L103）与三处脆弱点
- [x] T2 grape 注入侧：三处 env_extra 归一（from_repo_key）
- [x] T3 grape 文档：agent-environment-variables.md 格式契约 + clone URL 规范模板 + 排查指引
- [x] T4 grape 测试：test_issue_721_repo_full_name_env.py（RED→GREEN）
- [x] T5 CodeArts runbook：模板修正 + 变量格式自文档化 + clone 失败排查指引；SKILL.md 补格式
- [x] T6 CodeArts 双目录同步（diff -r 逐字节一致）
- [x] T7 #584/#595 注入一致性核对（help 文档总表 + 测试断言覆盖）
- [x] T8 自审查（5 轴 + pre-pr-release-check 增量检查）→ PR①/PR② → 评论 → A2A QA → tasks complete
