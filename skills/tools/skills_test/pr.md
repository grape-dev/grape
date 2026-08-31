# PR 自动设置合并人方案（复用参考）

> 本文档供其他 agent/机器复用"评测后自动设置 PR 合并人"功能。
> 方案基于 GitCode 公开 API 实测验证。

## 一、功能概述

评测完成后，自动把 PR 的 assignee（合并人）设置为 `HugLovesWine`。

```
评测完成 → 设置 PR assignee（HugLovesWine）→ [合入] → 创建评测报告 ISSUE → ...
```

## 二、核心 API（已实测）

### 设置 PR 合并人

```
POST https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/assignees
```

请求头：
```
PRIVATE-TOKEN: <GITCODE_TOKEN>
Content-Type: application/json
```

请求体：
```json
{"assignees": "HugLovesWine"}
```

> **关键坑**：`assignees` 是**字符串**，不是数组。用数组 `["HugLovesWine"]`
> 会返回 `PARAMETER_ERROR`（error_code 1000）。

成功返回：
```json
{"assignees_number": 1}
```

### 取消 PR 合并人（如需）

```
DELETE https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/assignees?assignees=HugLovesWine
```

## 三、触发时机

在评测后的 PR 治理编排阶段调用。每个 assignee 动作独立 try/except，失败不阻断后续。

## 三点五、合并人配置项（可改）

合并人 login 通过环境变量配置，统一默认 `g30074593`：

| 位置 | 配置方式 |
|------|---------|
| `skill-auto-test-pr/scripts/run-pipeline.sh` | `GITCODE_PR_ASSIGNEE`（默认 `g30074593`） |
| `deploy-skills-dev.sh` | `export GITCODE_PR_ASSIGNEE="${GITCODE_PR_ASSIGNEE:-g30074593}"` |
| `fetch-pr-skill/scripts/eval/pr_orchestrator.py` | `ASSIGNEE_LOGIN = os.getenv("GITCODE_PR_ASSIGNEE", "g30074593")` |

> **关键坑（实测）**：合并人**必须是目标仓库 Owner/Maintainer 级别**（access_level ≥ 40）。
> 设为 Developer 级成员（如 HugLovesWine 等）会返回
> `The user must have matching operation privileges. Please check the following users: X`（HTTP 400）。
> 且 PR **作者不能设自己**（`Merge request author cannot operate their own merge request`）。

## 三点六、合并人（与审查人不同字段，内网 API）

GitCode 里**审查人**与**合并人是两个独立字段**：

| 角色 | 接口 | 标识 |
|------|------|------|
| 审查人 | `POST /api/v5/repos/{o}/{r}/pulls/{n}/assignees`，body `{"assignees":"<login>"}` | login 字符串 |
| 合并人 | `PUT https://web-api.gitcode.com/issuepr/api/v1/projects/{repoId}/isource/merge_requests/{iid}`，body `{"repoId":<repoId>,"iid":<n>,"assignee_ids":"<数字id>"}` | **数字用户 id** |

- 需要 `GITCODE_ACCESS_TOKEN`（Bearer JWT）+ 浏览器 headers（`X-App-Channel: gitcode-fe`、Origin、Referer、UA）
- `repoId` = v5 `GET /repos/{o}/{r}` 的 `id`（数字）
- 合并人数字 id 配置：`GITCODE_PR_ASSIGNEE_ID`（默认 `11027048` = g30074593）
- **幂等坑**：重复设置同一合并人时接口返回 `assignees_number: 0`、`assignees: null`，需回读 GET 该 MR 确认，不能据此误判失败（`assign_merger` 已处理）
- 实现：`fetch-pr-skill/scripts/eval/gitcode_merger.py::assign_merger()`

## 四、环境变量

只需 **1 个 token**：

| 变量 | 说明 |
|------|------|
| `GITCODE_TOKEN` | GitCode 个人访问令牌（PRIVATE-TOKEN 形式，如 `ssxQv5hpA-...`） |

> 注：给 PR 设置合并人只用公开 v5 API，**不需要** JWT / 刷新 token。
> 若需同时给评测报告 **ISSUE** 设置负责人，才需要额外的
> `GITCODE_ACCESS_TOKEN`（Bearer JWT）+ `GITCODE_REFRESH_TOKEN`（刷新），
> 且要走内部 API `web-api.gitcode.com`（见文末扩展）。

## 五、参考实现（Python，urllib.request）

```python
import json
import urllib.request

GITCODE_API_BASE = "https://api.gitcode.com/api/v5"
ASSIGNEE_LOGIN = "HugLovesWine"


def parse_pr_url(pr_url: str):
    """解析 PR URL，返回 {"owner","repo","pr_number"}；失败返回 None。"""
    import re
    m = re.match(
        r"^https://gitcode\.com/([^/]+)/([^/]+)/(?:pull|merge_requests)/(\d+)$",
        pr_url,
    )
    if not m:
        return None
    return {"owner": m.group(1), "repo": m.group(2), "pr_number": m.group(3)}


def _http_post(url: str, headers: dict, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def assign_pr(pr_url: str, token: str, assignee_login: str = ASSIGNEE_LOGIN) -> dict:
    """设置 PR 合并人。返回 {"assigned": bool, "assignees_number": int}。"""
    parsed = parse_pr_url(pr_url)
    if not parsed:
        raise ValueError(f"cannot parse PR URL: {pr_url}")
    url = (
        f"{GITCODE_API_BASE}/repos/{parsed['owner']}/{parsed['repo']}"
        f"/pulls/{parsed['pr_number']}/assignees"
    )
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
    body = {"assignees": assignee_login}  # 字符串，非数组！
    data = _http_post(url, headers, body)
    n = data.get("assignees_number", 0)
    return {"assigned": n > 0, "assignees_number": n}
```

## 六、验证清单

- [ ] `assign_pr` 请求体为 `{"assignees":"HugLovesWine"}`（字符串）
- [ ] 成功返回 `assignees_number: 1`
- [ ] 用 `GET /api/v5/repos/{owner}/{repo}/pulls/{number}` 确认 assignees 含 HugLovesWine
- [ ] 数组形式的请求体应报 PARAMETER_ERROR（验证坑点）

## 七、扩展：同时设置 ISSUE 负责人（可选）

若还需给评测报告 ISSUE 设置负责人（= PR 关联 issue 创建人），需额外：
- 环境变量：`GITCODE_ACCESS_TOKEN`（Bearer JWT，约 36 天）+ `GITCODE_REFRESH_TOKEN`（约 24 天）
- 查用户数字 id：`GET https://web-api.gitcode.com/issuepr/api/v1/issue/{projectId}/issues/assignee_candidates?keyword=<login>` → `content[].id`（数字）
- 设置 ISSUE assignee：`PUT https://web-api.gitcode.com/issuepr/api/v1/issue/{projectId}/issues/{iid}`，body `{"assignee_ids":[数字id]}`
- 需带浏览器 headers（`X-App-Channel: gitcode-fe`、`Origin`、`Referer`、UA）绕过 WAF
- 刷新：`POST https://web-api.gitcode.com/uc/api/v1/user/token/refresh?refresh_token=<REFRESH>`（query 参数）+ `Authorization: Bearer <旧ACCESS>`

（详细内部 API 方案见同目录 fetch-pr-skill 历史实现。）
