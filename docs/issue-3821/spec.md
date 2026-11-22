# Issue #3821 需求文档（spec）

> 来源：Issue #3821 + Issue Agent Spec 评论（comment id=202860097）。
> 开发基线：`arch-weekly-11-ljt`。

## 目标

让「评论删除/更新」在 API 侧真正可用：

1. issues 评论补齐 `PATCH/DELETE /api/git-repo/{owner}/{repo}/issues/{number}/comments/{comment_id}`（当前恒 405）。
2. pulls 评论 update/delete 按**创建同源通道**选路：合入前 `/pulls/{n}/comments` 创建的走 pull note 通道；#754 合入后回退 `/issues/{n}/comments` 创建的走 issue note 通道，消除 400 `Note type is not correct`。
3. 统一 id 口径：数字 note_id 与 create/list 返回的十六进制 id 均可，解析失败返回可读 4xx（不再是无说明 400/422）。

## 范围

### In Scope

- `src/api/git_repo_api.py`：新增 issues 评论 PATCH/DELETE 两个 operation。
- 泛化 `_resolve_pull_comment_note_id` 为按 scope（issues/pulls）解析的通用解析器。
- pulls update/delete 通道判定与 note-type 400 回退。
- `contracts/openapi_baseline.json`（端点数 390→392）与 `contracts/required_operations.json`（新增两条 CLI 契约）刷新。
- `help/api/10-git-repos.md` 文档端点块与 CLI 契约说明。
- 新增 `tests/api/test_issue_3821_comment_update_delete.py`。

### Out of Scope

- 评论正文业务语义校验（#2225 已有断言）不改。
- 不新增评论列表分页/搜索。
- 不修改 grape-cli 仓库代码；CLI 命令由服务端 OpenAPI 契约自动生成。

## 验收标准

- [ ] issues 评论 create→update→delete 闭环，数字 note_id 与十六进制 id 两种入参均可用。
- [ ] pulls 评论在「合入前 /pulls 通道创建」与「已合入 #754 回退 /issues 通道创建」两条路径下 update/delete 均成功，无 `Note type is not correct`。
- [ ] 两种 id 形态各至少一条自动化用例。
- [ ] 非法/不可解析 id 返回可读 4xx（含原因）。
- [ ] 现有测试全绿，契约完整性门禁与文档同步测试通过。

## 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 通道判定引入额外上游请求 | 中 | 数字 id 直传零额外请求；仅 hex 做解析，仅 note-type 400 才回退 |
| 新 operation 改变契约端点数 | 中 | 受控 `--update-baseline --reason` 重定基 |
| CLI 与 API 发布不同步 | 低 | 先上线 API，契约由 OpenAPI 自动生成 |