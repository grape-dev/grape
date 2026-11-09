# Spec: Issue #731 — pulls files 返回 diff 字段结构不一致

> 摘自 Issue #731 评论（Issue Agent 发布，评级 ready）；以 #731 为开发基准（#732 同根跟踪）。

## 目标
统一 `grape git-repo pulls files --json-out` 返回文件对象中 diff 字段位置，消除顶层 `.diff` 与 `.patch.diff` 并存。

## 范围
- In：服务端代理层 `list_pull_files` 出参归一化至 `.patch.diff`；一致性断言（异常 log.warning）；契约文档标注字段路径；新增测试。
- Out：不改 CLI 客户端；不统一为 GitHub 风格顶层 `.diff`；不改 patch 元信息语义。

## 根因
`src/api/git_repo_api.py` `list_pull_files` 纯透传 `_vcs_req(...)`，无响应归一化；GitCode 平台对部分调用链路返回顶层 `.diff`。

## 验收
- 所有文件对象 diff 一律位于 `.patch.diff` 且非空
- 顶层 `.diff` 混入场景被归一化并有测试覆盖
- patch 元信息（added_lines/removed_lines/too_large 等）语义不变
- 现有测试通过 + 新增测试覆盖
