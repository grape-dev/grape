# Issue #684 Spec：服务端 JSON 控制字符统一转义 + milestones state 口径收口

来源：Issue #684 评论 197570999（issue agent 深度可开发性分析，评级 ready）。本仓分工为服务端侧；CLI 客户端侧（grape-cli#8/#9、grape-skills#213）平行推进，不重复实现。

## 目标
1. grape 服务端所有 `--json-out` 端点共用的 JSON 序列化统一出口保证：响应体为合法 JSON，字符串值中的原始控制字符（U+0000–U+001F、U+007F）与孤立代理项（lone surrogate）经序列化后，下游 `json.loads` 无预处理直接解析成功。
2. `git-repo milestones` 端点：`--state` 过滤合法值（open/closed/all）与返回条目 `state` 字段命名口径对齐——服务端将返回 `state` 归一化为 `open`/`closed`（active→open 映射在端点描述与 API 文档注明）。

## 范围
- `src/api/json_safe.py`（新增）：统一安全 JSON 序列化出口（SafeJSONResponse）。
- `src/api/main.py`：注册 `default_response_class=SafeJSONResponse`（全端点共用路径，非逐端点补丁）。
- `src/api/git_repo_api.py`：milestones list/create/update 返回 `state` 字段归一化（active→open），端点 description 注明口径。
- `docs/openapi/10-git-repos.md`：milestones 章节补充 state 口径说明。
- `tests/`：覆盖测试。

## 非目标
- CLI 客户端输出侧转义 / active→open 客户端归一化（grape-cli#8/#9）。
- skills 文档降级（grape-skills#213）。
- milestones create 能力缺口（#668）。
- 历史提交评审 / 全历史扫描。

## 验收标准
- [ ] 构造含 `\x01`–`\x1f`/原始换行/孤立代理项的字符串字段，`--json-out` 读端点输出可 `json.loads` 直接解析（测试覆盖 app 级 git-repo 端点 + 响应类单元）。
- [ ] 序列化出口为统一实现（default_response_class 单点），含覆盖测试。
- [ ] milestones 返回 `state` 归一化为 open/closed，端点描述与文档注明 active→open 映射；`--state` 过滤值仍为 open/closed/all。
