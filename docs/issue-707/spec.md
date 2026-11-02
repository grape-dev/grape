# Spec: Issue #707 — issues update 400 报错文案字段名与契约字段不一致（state_event vs state）

> 基于 Issue Agent 已发布 Spec（本 Issue 评论，2026-11-02）落地开发；开发基线 weekly-08-mys。

## Objective
消除 `grape git-repo issues update` 传非法 `state` 值时 400 报错文案中的 `state_event` 误导，使报错口径与契约字段 `state` 及合法取值（reopen/close）对齐；`state=open` 等常用中性词形直接翻译为上游合法值，不再落入上游 400。

## Scope
- `src/vcs/providers/gitcode.py` `_UPDATE_STATE` 映射修正/扩充：`open`/`opened`→`reopen`（原 `open`→`open` 为上游非法值，即根因），`closed`/`close`→`close`，`reopen`→`reopen`。
- `src/vcs/providers/github.py` `_UPDATE_STATE` 对齐中性集合：`reopen`→`open`、`opened`→`open`、`close`→`closed`。
- `src/api/git_repo_api.py` `update_issue`：`state` 非空时本地前置校验（合法中性集合 open/opened/reopen/closed/close，统一小写归一），非法值本地 400，文案形如 `'state': input must in [reopen, close] (aliases: open, opened, closed)`，无 `state_event` 字样。
- `IssueUpdateBody.state` description 补充别名说明（enum 保持 `["reopen","close"]` 口径不变）。
- 新增测试：非法值本地 400 文案 / 合法中性值翻译正确（RED→GREEN）。

## Out of Scope
- 不改契约字段名、不改 enum、不动 grape-cli、不处理 #703。

## Acceptance Criteria
- `state=invalidvalue` → 本地 400，文案字段名为 `state`、列出合法取值，无 `state_event`。
- `state=open/opened` → 翻译为 `reopen` 发往上游；`state=closed/close/reopen` → 翻译正确。
- 契约 description / --help / 报错三方口径一致；现有测试全通过。
