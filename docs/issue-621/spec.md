# Spec: Issue #621 — issues update `state` 字段契约修正（reopen/close）

> 来源：Issue #621 评论已发布的 Spec Document（Issue Agent，评级 ready）。本文件为仓库内索引副本。
> 目标分支：weekly-08-mys（场景注入基线）。

## 目标
修正服务端 openapi 契约中 `PATCH /api/git-repo/{owner}/{repo}/issues/{number}` 请求体 `state` 字段：
description 明确仅接受 `reopen`/`close`（对齐 GitHub `state_event`），schema 增加 enum，请求示例补充 `{"state":"reopen"}`。

## 范围
- In：`src/api/git_repo_api.py` IssueUpdateBody.state 契约（description/enum/examples + 模型级请求示例）；`help/api/10-git-repos.md` 契约说明与示例同步。
- Out：后端运行时行为（仍为自由 str 透传，非法值由后端 400 拒绝）；grape-cli 仓改动；issue-runbook.md（位于 grape-skills 仓，需后续在该仓同步）。

## 方案
- `state: str = Field(..., json_schema_extra={"enum": ["reopen","close"]}, examples=["reopen"])`——仅 schema 层收紧，不改运行时校验。
- `IssueUpdateBody.model_config.json_schema_extra.examples = [{"state":"reopen"}]` 提供请求示例。

## 验收
- openapi.json 中 `state` 带 enum `["reopen","close"]` 且 description 说明约束（已加测试 `tests/test_issue_621_state_contract.py`）。
- 请求示例包含 `{"state":"reopen"}`。
- 现有行为不变（测试断言字段仍为自由 str）。
