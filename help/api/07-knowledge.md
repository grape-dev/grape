# Knowledge 知识接口（/api/knowledge）

> 对外公开，认证：`get_current_user_or_api_key`。
> scope（x-token-scope）：读接口 `list/stats/detail/content/context` = **dual**；写操作
> （entry/archive/refs/delete/update）= **user_only**（`grape_` API Key / JWT；`tk_` → 403）。

## GET /api/knowledge/content — 读取知识内容

| Query | `repo_key`、`scope`（project/issue/issue_agent）、`issue_num`（可选）、`agent_type`（可选）、`filename`（可选） |
| 出参 | `{filename, content}` 或 `[{filename, content}]` |
| 返回 | 200；403 越权；404 |

---

## GET /api/knowledge/context — 获取知识上下文

| Query | `repo_key`、`scope`、`issue_num`（可选）、`agent_type`（可选） |
| 出参 | `{knowledge.md: "...", project_info: {...}}`（合并上下文） |
| 返回 | 200 |