# Spec: Issue #4169 场景分支端点上游错误映射

> 权威版本见 Issue #4169 评论「📋 Spec Document」（Issue Agent 出品）；本文为实现侧需求摘录。

## Objective
`GET/POST /api/scenes/branches/{owner}/{repo}` 不再把上游业务语义错误折叠成 502：
上游 404 → 404「仓库不存在或无权访问」；401/403 → 403 `TOKEN_PERMISSION_INSUFFICIENT`；
仅网络异常/超时/上游 5xx → 502，且 502 detail 不回吐上游原文（原文只落服务端日志）。

## 现状（arch-weekly-11-ljt 核对）
- `scenes.py` `list_repo_branches`：`except Exception → 502`，`detail.message = f"查询分支失败: {e}"`，
  `e` 由 `RuntimeError(f"{error} {detail}")` 中转拼成，含上游全文。
- `scenes.py` `create_repo_branch`：error-dict 分支只处理 409/422→409、403→维护者权限，
  **上游 404 落 502 并回吐 `result["error"]`**；异常分支同为 `502 f"创建分支失败: {e}"`。
- `repos.py` 已具备 `_upstream_status_code` / `_host_502_detail` 与 404/401/403 分流（#383/#395），但未覆盖 scenes。

## 上游错误两种报文形态（必须同时支持）
| 来源 | 形态 | 状态码位置 |
|------|------|-----------|
| `repos.py` 侧 RuntimeError（`_req`） | `GitCode API GET {url} failed (404): {body}` | 正则 `failed \((\d{3})\)` |
| `VcsApiClient._request` | `{"error": "HTTP 404", "status_code": 404, "detail": body}` | `status_code` 字段 / 正则 `HTTP (\d{3})` |
| 超时/网络 | `{"error": "timeout ..."}` | 无状态码 → 502 |

## Acceptance Criteria
- [ ] GET 上游 404 → 404，`detail.code=404`、message「仓库不存在或无权访问」，响应体不含 `Project not found`/上游 URL
- [ ] GET 上游 401/403 → 403，`error_code=TOKEN_PERMISSION_INSUFFICIENT`
- [ ] GET 超时/网络/上游 5xx → 502，detail 不含上游原文，`error_code` 形如 `UPSTREAM_*`
- [ ] POST 同四分支；409/422（分支已存在）仍 409
- [ ] 新增 `tests/mgmt/test_issue_4169_scenes_branches_error_map.py`
- [ ] 现有 `tests/mgmt/test_scenes_branches_cache.py` 全绿（`test_error_not_cached` 500→502 不变）
- [ ] 失败响应不入分支缓存