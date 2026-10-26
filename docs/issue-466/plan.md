# Issue #466 — 方案设计与任务计划

> 关联：`spec.md`（需求文档）| 分支 `feat/issue-466` ← base `weekly-XX`

## 1. 改动清单

### 1.1 断点一：`src/api/routes/repos.py::_repo_provider`

现状（repos.py:2244）：

```python
def _repo_provider(user: dict = Depends(get_current_user_or_api_key)) -> str:
    from src.repo_key import parse_repo_key
    p = parse_repo_key(user.get("repo_key") or "")[0] or "gitcode"
    if p not in _SUPPORTED_PROVIDERS:
        raise HTTPException(403, ...)
    return p
```

改为接收 `request.url.path`，从路径的目标仓段解析前缀：

```python
_REPO_KEY_PREFIXES = ("gitcode:", "github:")

def _provider_from_path(path: str) -> str:
    """从 /api/repos/{owner}/{repo}/... 路径解析目标仓前缀 provider；无前缀返回空。"""
    for seg in [s for s in path.split("/") if s]:
        if seg.lower().startswith(_REPO_KEY_PREFIXES):
            return seg.split(":", 1)[0].lower()
    return ""

def _repo_provider(request: Request, user: dict = Depends(get_current_user_or_api_key)) -> str:
    from src.repo_key import parse_repo_key
    p = _provider_from_path(request.url.path) \
        or parse_repo_key(user.get("repo_key") or "")[0] or "gitcode"
    if p not in _SUPPORTED_PROVIDERS:
        raise HTTPException(403, ...)
    return p
```

要点：
- 只认 `gitcode:` / `github:` 前缀（小写归一），其余前缀落到回退分支按会话主仓解析
  （主仓也未知 provider 时维持既有 403 防御）。
- `{owner}` path 段本身可能含 `:`（FastAPI 默认允许），带前缀时端点内 `owner` 值为
  `gitcode:OPCTeam`，`_repo_platform_token` 的 `to_repo_key(f"{owner}/{repo}")` 幂等返回
  原值（repo_key.py:14-16 `":" in owner_repo` 直接返回），无需额外剥前缀——
  `get_gitcode_token(repo_key="gitcode:OPCTeam/HDBlogService")` 命中正确仓库行。

### 1.2 断点二：`src/runtime/repogit.py`

新增 env 读取辅助 + 两处 token 解析改造：

```python
_GRAPE_LINKED_REPO_TOKENS_ENV = "GRAPE_LINKED_REPO_TOKENS"

def linked_repo_token_from_env(repo_key: str, env: dict | None = None) -> str:
    """从 env(GRAPE_LINKED_REPO_TOKENS JSON) 解析关联仓 token；无/失败返回空。"""
    import json as _json
    _env = env if env is not None else os.environ
    raw = (_env or {}).get(_GRAPE_LINKED_REPO_TOKENS_ENV, "") or ""
    if not raw:
        return ""
    try:
        m = _json.loads(raw)
        return str(m.get(repo_key, "")) if isinstance(m, dict) else ""
    except Exception:
        return ""

def _linked_repo_token(repo_key: str, env: dict | None = None) -> str:
    """关联仓 token 解析：服务端 env 分发优先 → 本地 Repo.bot_token 回退。"""
    tok = linked_repo_token_from_env(repo_key, env)
    if tok:
        return tok
    try:
        from src.repo import Repo
        return Repo(repo_key).bot_token
    except Exception as e:
        log.warning(f"linked repo token resolve failed for {repo_key}: {e}")
        return ""
```

- `ensure_linked_repo_worktrees(linked_repos, agent_type, issue_num, token="", env=None)`
  与 `ensure_repo_base_multi(linked_repos, token="", env=None)`：新增 `env` 参数（默认 None →
  读 `os.environ`，兼容本地直调），token 获取改走 `_linked_repo_token(rk, env)`。
- skip 日志保留（两者皆无 token 时），行为对齐 Spec「维持降级语义」。

### 1.3 服务端 env 注入：`src/api/event_pipeline.py`

在所有注入 `GRAPE_LINKED_REPOS` 的三处（`build_task_env` L3092、`_enqueue_pipeline_task`
L2830、direct 执行路径 L3671）追加 token 分发。抽公共函数：

```python
def _inject_linked_repo_tokens(env: dict, linked_repos_json: str) -> None:
    """Issue #466: 服务端解析各关联仓 bot token 随 env 分发（worker 端本地无库）。"""
    import json as _json
    from src.token_service import get_repo_bot_token
    if not linked_repos_json:
        return
    try:
        linked = _json.loads(linked_repos_json)
    except Exception:
        return
    tokens = {}
    for lr in (linked or []):
        rk = (lr or {}).get("repo_key") or ""
        if not rk:
            continue
        try:
            tok = get_repo_bot_token(rk)
        except Exception:
            tok = ""
        if tok:
            tokens[rk] = tok
    if tokens:
        env["GRAPE_LINKED_REPO_TOKENS"] = _json.dumps(tokens, ensure_ascii=False)
```

调用点紧跟 `env["GRAPE_LINKED_REPOS"] = ...` 之后（direct 路径在 env_extra 组装处、
`ensure_linked_repo_worktrees(..., env=env_extra)` 调用处同步传 env）。
`src/runtime/agent.py` worker 端 L1798 调用处传 `env=env`（任务 env 已含
GRAPE_LINKED_REPO_TOKENS——claim 时随 env_extra 一起取到）。

### 1.4 token 解析入口选择

`get_repo_bot_token`（token_service）按 repo_key 前缀分流（token_resolver.create_provider_from_repo_key），
与 `Repo.bot_token` 同一链路（repo.py:43）。服务端进程内可用（MySQL/本地库均在）。

## 2. 兼容性分析

| 场景 | 行为 | 结论 |
|------|------|------|
| 同平台（gitcode 主仓 + gitcode 关联仓），路径无前缀 | 回退会话主仓 → gitcode（同现状） | 不回归 |
| 跨平台，路径带前缀 | 按前缀解析 → 正确平台 | 修复 |
| 跨平台，路径无前缀（老 CLI） | 回退会话主仓 → 仍是主仓 provider（维持现状错误，不恶化） | 兼容保留 |
| worker 无 env token（本地模式） | 回退 `Repo(rk).bot_token`（本地库可解析时可用） | 兼容保留 |
| env 有 token 但本地也有 | env 优先（服务端分发为权威，与 GRAPE_WORK_BRANCH 注入模式一致） | 预期 |
| `gitlab:` 前缀 | 不在 `_REPO_KEY_PREFIXES` → 回退主仓；主仓未知 → 403 | 防御保持 |

## 3. TDD 计划（红→绿分提交）

1. **红 T1**：`tests/test_git_repo_api.py` 新增 `TestRepoCredentialProviderByPath`
   - 跨平台：user repo_key=`github:o/hello`，请求 `/api/repos/gitcode:x/y/git-credential` →
     mock `get_gitcode_token` 被调用、`get_github_token` 未被调用、provider=gitcode。
   - 反向：user repo_key=`gitcode:o/hello`，请求 `github:` 前缀 → provider=github。
   - 回退：无前缀路径 → provider 按会话主仓（现状断言保留）。
   - 未知前缀 `gitlab:` → 回退主仓（不新增 403 路径；403 断言用主仓 gitlab 场景保留）。
2. **绿 T2**：实现断点一。
3. **红 T3**：`tests/test_repogit.py` 新增
   - env 优先：`env={"GRAPE_LINKED_REPO_TOKENS": '{"gitcode:o/r": "tok_env"}'}` +
     mock `Repo.bot_token` 返回不同值 → `ensure_issue_worktree` 收到 tok_env。
   - 回退：env 无该仓键 → 收到本地 bot_token。
   - 双无 → skip（worktrees 列表不含该仓，维持 warning 日志）。
   - `ensure_repo_base_multi` 同口径断言。
4. **绿 T4**：实现断点二 + 服务端注入（repogit.py / event_pipeline.py / agent.py 传 env）。
5. **T5**：全量回归 pytest。

## 4. 风险

- env 传 token 增加暴露面：与 GRAPE_SESSION_TOKEN / repo_gitcode_token 同通道同生命周期，无新增持久化。
- agent_tasks.env_extra 落库（JSON）：repo_gitcode_token 本就落该表同列，模式一致，非新增面。
- CLI 老版本不带前缀：维持现状回退，不阻断；新版 CLI 按目标仓带前缀即可用。
