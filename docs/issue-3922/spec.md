# Spec: Issue #3922 非法分支名创建分支返回 502 且丢失上游原因

> 权威版本见 Issue #3922 评论「📋 Spec Document」（Issue Agent 出品，评级 ready）；本文为实现侧需求摘录。

## Objective
`POST /api/scenes/branches/{owner}/{repo}`（`src/domains/mgmt/routes/scenes.py: create_repo_branch`）：

1. 非法 ref 名在**服务端前置拒绝**，返回 **400 + 可操作原因**，不触达上游；
2. 上游 **4xx 逐档透传**（400/401/403/404/409/422），仅 **5xx / 传输异常** 映射 502；
3. 用户可见 message 经 `sanitize_upstream_error()` 脱敏（剥离内网 URL / token），原文只落服务端日志。

## 现状（基线 arch-weekly-11-ljt 核对）
- `create_repo_branch()` 对 `branch_name` **只判空**（"分支名不能为空"），无 ref 规则校验 —— 非法输入直接打到上游。
- 错误分支走 `_vcs_error_map.map_vcs_error()`（Issue #4169 引入）：404→404、401/403→403、
  `conflict_on=(409,422)`/`conflict_markers`→409、**其余一律 502** —— 上游 400（用户输入非法）落 502，
  且 `VcsApiClient` 错误 dict 里的 `detail`（说明原因）被丢弃。
- `map_vcs_error` 在 `tests/mgmt/test_issue_4169_scenes_branches_error_map.py` 有强契约：
  `map_vcs_error({"status_code": 409})` 无 `conflict_on` 时必须 502（读路径语义），因此**不能**在
  `map_vcs_error` 内直接放开 4xx 透传（会破坏既有用例与读路径语义）。

## 期望行为（验收口径）
| 输入 / 上游响应 | 期望 |
|---|---|
| `-x`、`refs/heads/x`、`refs/remotes/x`、`a b`、`a..b`、`x.lock`、`a~b`、`a:b`、`a?b`、`a*b`、`a[b`、`a\b`、`a"b`、`a'b`、`a//b`、`@{x}`、`@`、控制字符、超长 | **400** + 违反项文案，**不调用上游** |
| 上游 400 | **400** 透传，message 含可读原因，不含内网 URL/token |
| 上游 401 / 403 | **403** `TOKEN_PERMISSION_INSUFFICIENT`（既有语义不变） |
| 上游 404 | **404**「仓库不存在或无权访问」（既有语义不变） |
| 上游 409 / 422 / 报文含 already exists | **409**「分支已存在」（既有语义不变） |
| 上游 5xx / 超时 / 无码网络异常 | **502**（既有语义不变） |
| 合法分支名（`main`、`feat/issue-3922`、`release/v1.2.3`） | 正常创建（行为不变） |

## Scope
### In
- 新增 ref 规则校验工具（git `check-ref-format` + GitCode 约束，服务端单一来源）。
- `create_repo_branch()` 前置校验接入（判空之后、取 token / 调上游之前）。
- 新增单点映射工具 `map_upstream_error(status_code, detail)`（4xx 透传 / 5xx→502 / message 脱敏）。
- `create_repo_branch()` 错误分支切到该单点工具。
- 新增单测覆盖三类路径（非法名、上游 4xx、上游 5xx）。

### Out
- 存量 ~60 处 `status_code=502` 全量重构（仅提供单点工具，不批量替换）。
- 前端 UI / i18n 改造（服务端返回中文可读 message 即可）。
- #3737 日志明文 token 脱敏（不同缺陷）。

## 关键设计决策
| 决策 | 选择 | 理由 |
|---|---|---|
| 校验位置 | 服务端前置（判空之后、取 token 之前） | 前端可绕过；放在取 token 前可确保「非法名不触发任何上游/凭据调用」 |
| 校验工具位置 | `src/kernel/git_ref.py`（无 FastAPI 依赖的纯函数） | 规则是 git 域公共知识，后续前端/其它端点可复用同一份规则 |
| 4xx 透传落点 | 新增 `map_upstream_error()`，**不改** `map_vcs_error()` 的读路径语义 | #4169 契约用例要求 `map_vcs_error` 对无 `conflict_on` 的 409 仍→502；两条口径必须并存 |
| 400 的 message | 上游 `detail` 抽出可读 message → `sanitize_upstream_error()` → 空则回退通用文案 | 透传「可操作原因」同时不泄露内网 URL/token |
| 冲突判定顺序 | 先 `conflict_markers` / `conflict_on`，再 4xx 透传 | 上游常以 400 + "already exists" 表达冲突（既有用例已覆盖） |

## Risks & Mitigations
| Risk | Impact | Mitigation |
|---|---|---|
| 本地规则与 GitCode 实际约束不一致导致误拒 | Med | 规则严格对齐 `git check-ref-format`；合法形态（`feat/x`、`release/v1.2.3`）全部进正例用例 |
| 透传 detail 泄露内网信息 | High | 强制走 `sanitize_upstream_error()`，并额外脱敏 `token=/access_token=` 形态 |
| 影响正常创建路径 | Med | 校验仅在非法输入短路；合法输入仍走原 VcsApiClient 调用 |
| 破坏 #4169 既有用例 | Med | `map_vcs_error` 不动；POST 侧改为语义等价的 `map_upstream_error` 并回归全量用例 |