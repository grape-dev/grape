# Plan: Issue #721

## 改动一：src/api/event_pipeline.py — REPO_FULL_NAME 注入归一（防御性）
- 三处 env_extra 构建点（`_create_failed_task` L514、`_enqueue_pipeline_task` L3265、`_execute_agent_inner` L4232）：`"REPO_FULL_NAME": from_repo_key(repository) or repository`。
- 幂等：无前缀输入原样返回，行为不变；带前缀输入剥前缀，与 REPO_OWNER/REPO_NAME（#94 split_owner_repo）口径一致。

## 改动二：help/usage/agent-environment-variables.md — 格式自文档化 + 排查指引
- REPO_FULL_NAME 行标注格式契约：`owner/repo`，无 host、无 provider 前缀（`gitcode:`）、无 `.git`。
- 新增「runbook clone URL 模板（规范写法）」与「clone 失败排查指引」小节：变量校验命令 + 常见错误模式表（前缀泄漏/空值/裸 repo 名/错 host）+ #584/#595 一致性核对结论。

## 改动三：tests/test_issue_721_repo_full_name_env.py — 测试
- from_repo_key 幂等/剥前缀；带前缀 repository 注入后 REPO_FULL_NAME 无前缀；clone URL 拼接断言（正常/前缀/空值）。

## 改动四（GTDTeam/CodeArts，feat/issue-721-clone-url，base=lidongsheng-CodeArts）
- `references/deliver-upload-runbook.md` Step 5：变量格式声明 + 归一 + clone 重写（去掉 2>/dev/null 与误导性 `|| pull`）+ 错误排查表补充 clone 失败排查指引；SKILL.md 环境变量表补格式。
- 双目录同步：`.grape/skills/codearts-deliver-upload/` 与 `Maturity/skill/codearts-deliver-upload/` 逐字节一致（diff -r 校验）。

## 验证
- `python3 -m pytest tests/test_issue_721_repo_full_name_env.py tests/test_agent_env_parts.py -q`（grape）
- CodeArts：`diff -r` 双目录一致；runbook 模板 shell 片段本地干跑（bash -n / 变量夹具）。

## 交付
- PR①（grape，base weekly-08-mys，head feat/issue-721）：改动一/二/三。
- PR②（CodeArts，base lidongsheng-CodeArts，head feat/issue-721-clone-url）：改动四。
- Issue #721 评论（两个 PR + 一致性核对结论）→ A2A QA → tasks complete。
