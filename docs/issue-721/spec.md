# Spec: Issue #721 — codearts-deliver-upload runbook clone URL 模板修正

> 来源：用户反馈后台 id 488 | Spec 评级 ready（评论 id=197703282）| 目标分支 weekly-08-mys

## Objective
修正 codearts-deliver-upload runbook 的 clone URL 拼接模板，使其与实际注入的 `$REPO_FULL_NAME`（格式：`owner/repo` 全名，不含 host 前缀）一致；标注各变量来源与格式（自文档化）；补充 clone 失败排查指引；并与同族 #584（BOT_USERNAME）/ #595（PR_NUMBER）做注入一致性核对。

## 现状核实（本轮 coding 实测）
- runbook 实体位于 **GTDTeam/CodeArts** 仓 `.grape/skills/codearts-deliver-upload/`（`references/deliver-upload-runbook.md` Step 5，存在于 `lidongsheng-CodeArts` / `feat/issue-288` / `feat/case-eval-workflow` 分支，dev 分支无此文件）。
- 模板现状：`grape git clone "https://gitcode.com/$REPO_FULL_NAME.git" "$repo_dir" 2>/dev/null || grape git -C "$repo_dir" pull`，存在三处脆弱点：
  1. 未声明 `$REPO_FULL_NAME` 格式契约，若注入值带 provider 前缀（`gitcode:owner/repo`）或为空，拼出 `https://gitcode.com/gitcode:xxx/yyy.git` / `https://gitcode.com/.git` 错误地址；
  2. `2>/dev/null` 吞掉 clone 错误详情；
  3. `|| grape git -C "$repo_dir" pull` 在 clone 失败（目录不存在）时给出误导性报错，掩盖根因。
- 注入侧（本仓 grape）：`REPO_FULL_NAME` 在 `_execute_agent_inner` / `_enqueue_pipeline_task` / `_create_failed_task`（`src/api/event_pipeline.py`）均为 `evt.get("repository", ...)` 直通值，无统一归一兜底；同族 #584 / #595 均为「runbook 依赖的环境变量与实际注入值不匹配」家族。

## Scope
- In：Grape 仓注入侧 REPO_FULL_NAME 归一兜底 + `help/usage/agent-environment-variables.md` 变量格式自文档化 + clone 失败排查指引 + 测试；CodeArts 仓 runbook 模板修正与排查指引（双目录同步）。
- Out：注入侧语义重构、其他 runbook 排查（后续按需建 Issue）。

## 方案
- 注入侧（grape，防御性归一）：env_extra 构建处 `REPO_FULL_NAME = from_repo_key(repository) or repository`——剥 provider 前缀、幂等；与 #94（REPO_OWNER/REPO_NAME 剥前缀）口径一致。
- 消费侧（CodeArts runbook）：模板改为「变量校验 + 归一 + clone」，显式声明变量格式，失败时报错可见并给出排查指引；同步 `Maturity/skill/` 镜像副本（逐字节一致，C10 用例）。
- 文档：`help/usage/agent-environment-variables.md` 增加 REPO_FULL_NAME 格式契约、runbook clone URL 规范模板与 clone 失败排查指引（含 #584/#595 一致性核对结论）。

## Acceptance Criteria
- [x] clone URL 模板与注入值格式对齐（runbook 侧归一 + 注入侧归一双保险）
- [x] runbook / 文档明确标注 clone URL 各段变量来源与格式
- [x] 附 clone 失败排查指引（变量值校验命令 + 常见错误模式）
- [x] 与 #584 / #595 注入一致性核对完成（结论写入帮助文档与 PR）
- [x] 新增测试覆盖归一逻辑，现有测试通过
