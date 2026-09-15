# OpenAPI 契约注入 `x-token-scope`（评审 + 落地方案）

> 来源需求：`grape-cli/docs/grape-server-requirement-x-token-scope.md`（CLI 侧提交）
> 目标仓库：grape 主仓库（`src/api/openapi.py` / 契约生成）
> 分支：`weekly-03-lyz`；状态：**已实施**；日期：2026-09-15

---

## 一、评审结论

**方向可行**：在对外安全 operation 注入 `x-token-scope`（仅新增 `x-` 扩展字段），为 CLI/SDK
按 token 类型过滤命令提供契约级事实源——与已实施的 token-scope 分类（
`docs/weekly-03/token-scope-classification-design.md` §3.3）对齐，且不破坏现有消费方。

**但需求 §三「按依赖链函数名自动推导」有一处关键错误**：`_repo_token`（以及 repos.py 的
`_repo_platform_token`）**不是 FastAPI 依赖**，而是各端点**体内调用**的普通函数；
该类端点的契约认证依赖实为 `get_current_user_or_api_key`。若按需求原实现遍历 `dependant`，
git-repo/git-credential/workspace 会被标成 `dual`，**永远推导不出 `session_only`**。
需以「依赖链推导 + session_only 覆盖表」修正（§三.2）。

---

## 二、现状核实

### 2.1 三种认证的真实依赖形态

| 需求分类 | 源码形态 | contract 依赖链中可见? | 端点数 |
|----------|----------|:---:|:---:|
| `user_only`（`require_no_session_token`） | **真 FastAPI 依赖**，内嵌 `Depends(get_current_user_or_api_key)` | ✅ 可见 | 156（D 批全量） |
| `session_only`（git-repo / git-credential / workspace） | 认证用 `get_current_user_or_api_key`；**体内**调 `_repo_token`/`_repo_platform_token` 判 `auth_type != "session_token"` → 403 | ❌ 不可见 | 23（git-repo 21 + repos 2） |
| `dual`（`get_current_user_or_api_key`） | 真 FastAPI 依赖 | ✅ 可见 | 其余对外端点 |
| a2a 软认证（`get_user_or_none`） | 真 FastAPI 依赖 | ✅ 可见 | 1（`a2a_handoff`） |

关键证据：
- `git_repo_api.py:78` `def _repo_token(owner, repo, user, provider)` —— 普通函数。
- `git_repo_api.py:178` 端点 `user: dict = Depends(get_current_user_or_api_key)`。
- `repos.py:1145` `def _repo_platform_token(...)` —— 普通函数；
  `repos.py:1208/1226` git-credential/workspace 端点 `Depends(get_current_user_or_api_key)`。

### 2.2 契约落盘链路

`custom_openapi(app)` → `main.py` 启动时 `write_openapi_contract` 落盘两份：
`/static/openapi.json` + `help/api/openapi.json`。**改动 custom_openapi 后两份自动带新字段**，无需额外处理。

---

## 三、修正后的实现方案

### 3.1 scope 判定顺序（前缀匹配，先特异性后通用）

对每个对外安全路由（沿用 `route_is_public` 过滤），遍历 `dependant` 依赖链（已有 `_iter_deps`）：

```python
# 1) session_only：依赖链命中白名单守卫（未来若守卫化）或路径/operation_id 命中覆盖表 → session_only
# 2) user_only  ：依赖链命中 require_no_session_token         → user_only  （156 端点）
# 3) dual       ：依赖链命中 get_current_user_or_api_key /
#                 get_user_or_none（a2a 软认证，三通道或 None）→ dual
# 4) 其余不标   ：无对外认证依赖（不应进契约，已由 route_is_public 排除）
```

### 3.2 session_only 的判定：覆盖表（唯一手维护点，静态且稳定）

由于 session 强制发生在端点体内、依赖链不可见，采用 **operation_id / 路径前缀覆盖表**：

```python
_SESSION_ONLY = {
    # git-repo 传输层（git_repo_api.py，21 个 operation）
    "gitrepo_repo", "gitrepo_branches", "gitrepo_commits", "gitrepo_contents",
    "gitrepo_contents_root", "gitrepo_tree", "gitrepo_labels_list", "gitrepo_labels_create",
    "gitrepo_milestones", "gitrepo_issues_list", "gitrepo_issues_create",
    "gitrepo_issues_get", "gitrepo_issues_update", "gitrepo_issues_comments_list",
    "gitrepo_issues_comments_create", "gitrepo_pulls_list", "gitrepo_pulls_create",
    "gitrepo_pulls_get", "gitrepo_pulls_update", "gitrepo_pulls_merge",
    "gitrepo_pulls_comments_list", "gitrepo_pulls_comments_create", "gitrepo_pulls_files",
    # CLI 本地 git 凭据/工作区（repos.py）
    "repos_git_credential", "repos_workspace",
}
```

判定优先级：`session_only`（覆盖表）→ `user_only`（依赖链 `require_no_session_token`）→ `dual`。
覆盖表用注释标明「即使改认证依赖形态也保持标注」，并接入契约测试（§五）防漂移。

> 备选（不推荐本轮做）：把 `_repo_token`/`_repo_platform_token` 改造成真 `Depends` 守卫，
> 依赖链即可见 → 免覆盖表。改动面大（23 端点签名 + 体内调用点），且守卫名与「授权辅助函数」
> 混用会引入额外耦合；覆盖表 + 测试已足够消灭漂移。

### 3.3 写入与导出（已实现，`src/api/openapi.py` `token_scope()`）

```python
def token_scope(route) -> str | None:
    names = {getattr(d.call, "__name__", "") for d in _iter_deps(route.dependant)}
    op_id = getattr(route, "operation_id", "") or ""
    path = getattr(route, "path", "") or ""
    if op_id in _SESSION_ONLY_OPERATIONS or path.startswith(_SESSION_ONLY_PATH_PREFIXES):
        return "session_only"
    if "require_no_session_token" in names:
        return "user_only"
    if names & _PUBLIC_AUTH_DEPS:
        return "dual"
    return None
```

`custom_openapi` 遍历每个公开 op 时写入 `op["x-token-scope"]`。
`security` 三通道数组不变（兼容现有消费方）；仅新增 `x-` 扩展字段。

### 3.4 后续新增 API 的强制机制（L1/L2/L3）

**L1 operation_id 精确集合（既有）**：`test_openapi` 断言 `op_ids == EXPECTED_PUBLIC` 且
`seen == EXPECTED_PUBLIC.values()`。新增/改名/删公开端点 → 集合不等 → 测试必挂 → 强制同步刷新
operation_id 与契约测试。

**L2 scope 必填存在性（新增）**：`test_public_ops_have_x_token_scope` 断言每个公开 op 的
`x-token-scope ∈ {user_only, session_only, dual}`，绝不遗漏标注。

**L3 双向一致性守卫（新增）**：`test_x_token_scope_consistency`：
- 覆盖表/前缀规则命中的 operation 必须在契约中且被标 `session_only`（防删/改残留）；
- 契约中所有 `session_only` 必须命中规则（防新增 session 强制端点漏登记被误标 dual）；
- 抽查 user_only / dual / session_only 实例（skills/logout/dashboard / tasks-mine/a2a / git-repo/git-credential/workspace）。

> 新增 API 流程：定守卫 → 不公开用 `get_current_user`（不进契约，无 scope）；
> D 类管理用 `require_no_session_token`（自动 user_only）；开放读用 `get_current_user_or_api_key`（自动 dual）；
> git 平台传输/凭据进 `/api/git-repo/` 前缀（自动 session_only）；跑 test_openapi 补 EXPECTION 即闭环。

### 3.5 各验收样例的预期结果（对照需求 §四，均已实现并抽查通过）

| 目标端点 | 机制 | 预期 `x-token-scope` |
|----------|------|:---:|
| `GET /api/skills` | 依赖链 `require_no_session_token` | `user_only` |
| `POST /api/skills/upload` | 依赖链 `require_no_session_token` | `user_only` |
| `GET /api/git-repo/{owner}/{repo}/branches` | 前缀 `/api/git-repo/` | `session_only` |
| `GET /api/repos/{owner}/{repo}/git-credential` | 覆盖表 `repos_git_credential` | `session_only` |
| `GET /api/repos/{owner}/{repo}/workspace` | 覆盖表 `repos_workspace` | `session_only` |
| `GET /api/tasks/mine` | 依赖链 `get_current_user_or_api_key` | `dual` |
| `GET /api/issues/{owner}/{repo}/{number}/pipeline` | 依赖链 `get_current_user_or_api_key` | `dual` |
| `POST /api/a2a` | 依赖链 `get_user_or_none` | `dual` |

---

## 四、影响评估

| 项 | 说明 |
|----|------|
| 兼容性 | 仅新增 `x-token-scope`（OpenAPI 扩展位），security/结构不变；`test_openapi` operation_id 集合断言不受影响 |
| 落盘 | `/static/openapi.json` + `help/api/openapi.json` 启动时由 custom_openapi 自动带新字段 |
| 维护 | 156 处 user_only + 其余 dual = 自动推导零维护；session_only 覆盖表 25 项静态、有契约测试兜底 |
| CLI 侧 | 读契约 `x-token-scope` 优先，缺省回落静态 `token-scope.json`（消除漂移） |
| 安全 | 标注不新增/不削弱鉴权；真实安全仍由守卫保证（session_only 端点体内 403 不变） |

---

## 五、验收（已完成 ✅）

1. ✅ `openapi.json` 抽查：`/api/skills`=user_only、`/api/skills/upload`=user_only、
   `/api/git-repo/{owner}/{repo}/branches`=session_only、`/api/tasks/mine`=dual、`/api/a2a`=dual
   （契约 215 op 分布：user_only 171 / session_only 25 / dual 19）。
2. ✅ `tests/test_openapi.py`：新增 `test_public_ops_have_x_token_scope`（L2）与
   `test_x_token_scope_consistency`（L3）8 项全过；`op_ids == EXPECTED_PUBLIC` 集合断言不回归。
3. ✅ `/static/openapi.json` 与 `help/api/openapi.json` 均含新字段，既有字段不回归。
4. ✅ 端到端：`/api/git-repo/.../branches` 契约标 session_only，冒烟 session→502（provider 解析）、
   固守 **400** api_key→**403**（与标注一致）；`test_token_scope_guard` 不回归。

### 实施记录（2026-09-15）
- `src/api/openapi.py`：新增 `_SESSION_ONLY_OPERATIONS`（25 op）+ `_SESSION_ONLY_PATH_PREFIXES`
  （`/api/git-repo/`）+ `token_scope()`；`custom_openapi` 逐 op 写入 `x-token-scope`。
- `tests/test_openapi.py`：新增 L2 存在性断言 + L3 双向一致性/抽样断言。
- 契约落盘两份 JSON 已重刷；回归 test_openapi + token_scope_guard + TestAuth 全绿（42 passed）。

> CLI 侧（待 grape-cli）：读契约 `x-token-scope` 优先，缺省回落静态 `token-scope.json`。