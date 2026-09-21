# Git 仓库接口 provider 判定设计：由 token 派生，移除调用方参数

> 关联：`docs/weekly-02/issue-45-git-repos-api-design.md`、`issue-45-api-token-design.md`、`docs/openapi/10-git-repos.md`
> 目标：解决 `/api/git-repo`（及 `/api/repos` git-credential/workspace）provider 双信息源错配
> 分支：`weekly-02-lyz`
> 状态：**已实施（2026-09-12）**
> 日期：2026-09-12

---

## 一、问题背景

当前 `/api/git-repo/{owner}/{repo}/...` 与 `/api/repos/{owner}/{repo}/git-credential|workspace`
的 provider（gitcode / github）判定来源是**调用方请求参数**：

```python
# src/api/git_repo_api.py:63-68（repos.py 的 _repo_provider 同构）
def _provider(header: str = Header("", alias="X-Git-Provider"),
              provider: str = Query("", description=".../缺省 gitcode")) -> str:
    p = (header or "").strip() or (provider or "").strip() or "gitcode"
    if p not in _SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=403, ...)
    return p
```

**但同一请求的认证 token 已携带权威 provider**：

- 会话级临时 token（`tk_`）签发时 `repo_key` 即带前缀（`gitcode:owner/repo` / `github:owner/repo`）：
  - 事件流程：`repo_key = f"{source}:{repository}"`（`src/api/event_pipeline.py:733`）
  - webhook：显式 `gitcode:` / `github:` 前缀（`src/api/routes/webhook.py:125,183`）
- 认证 user dict 已把 `repo_key` 还给调用方（`src/api/deps.py:96`）
- `repo_key` 规范本身就是「跨平台全局唯一标识的单一入口」（`src/repo_key.py:4`）

于是存在**双信息源**：token 的 repo_key 前缀 + 请求参数 provider。

## 二、错配场景与后果

| # | 场景 | 后果 |
|---|------|------|
| ① | 技能/调用方未传 `X-Git-Provider`/`provider`（最常见）→ 缺省 `gitcode` | github 仓库会话被当 gitcode 处理，`_repo_token` 取错平台 token |
| ② | 传了但值 ≠ token 的 provider | 平台错配，**无一致性校验**，不报警，只拿到另一平台的 401/404 |
| ③ | 同一 `owner/repo` 同时存在于 gitcode 与 github（两个不同仓库） | 只有传对 provider 才命中目标仓库；而 token 已确定是哪个，传参反而引入不确定 |

**根因**：provider 有两个来源且未做强一致约束，调用方可绕过 token 语义指定平台。

## 三、方案：provider 由 token 派生，移除调用方参数

### 3.1 判定规则（单一信息源）

```
provider = parse_repo_key(user["repo_key"])[0]   # repo_key 前缀即权威
```

- `user["auth_type"] == "session_token"`：`user["repo_key"]` 必带前缀 → provider 由此派生。
- **不再接受 / 忽略** `X-Git-Provider` 与 `provider` query。
- 保留白名单校验：派生值不在 `{"gitcode", "github"}` → 403（不应发生，防御性）。

### 3.2 改造点（代码层面，本次不实施，仅记录）

| 文件 | 改动 |
|------|------|
| `src/api/git_repo_api.py` | `_provider` 删除 `Header`/`Query` 依赖，改为从 `user["repo_key"]` 派生；所有接口签名中传 `user` 处同步 |
| `src/api/routes/repos.py` | `_repo_provider` 同构改造（git-credential/workspace） |
| 两处 `_repo_token` / `_repo_platform_token` | 接受的 `provider` 实参改为由 `user["repo_key"]` 派生，不再依赖 router 参数 |

### 3.3 兼容性考虑

- **session_token 场景（当前唯一放行场景）**：`_repo_token` 本就 `auth_type != session_token` 返回 403
  （`git_repo_api.py:81-82`），所以 git-repo 接口**只有 token 认证路径**，移除参数无兼容负担。
- **API Key / JWT 场景**：user dict 无 `repo_key`（`deps.py:100-120`），且本身被 git-repo 拒绝。
  若未来要开放 API Key 访问 git-repo，才需要显式 provider 参数——届时再评估，不在本次范围。
- **文档**：`docs/openapi/10-git-repos.md`、`02-repos.md`、`issue-45-git-repos-api-design.md`
  的 provider 说明改为「由 token 绑定的 repo_key 前缀判定」。

### 3.4 不改动的部分

- `session_api_tokens` **不加 provider 列**：repo_key 前缀已表达（见 issue-45-api-token-design 评审）。
- 平台 token 解析逻辑（chat→user / devops→bot）不变，仅 provider 来源统一。

---

## 四、验收

1. github 仓库事件产生的会话 token，调用 `/api/git-repo/{o}/{r}/branches` 不带 provider 参数 → 走 GitHub token。
2. gitcode 仓库会话 → 走 GitCode token。
3. 传 `X-Git-Provider` / `provider` 与 token 不一致 → **以 token 为准**（或直接忽略参数），不再错配。
4. `test_git_repo_api.py` / `test_openapi.py` 契约更新后全绿。

---

## 四·A、测试用例设计

> 被测对象：`git_repo_api._provider`（/api/git-repo）与 `repos._repo_provider`（/api/repos git-credential/workspace）的 token 派生逻辑。
> Mock 方式沿用 `tests/test_git_repo_api.py`：`app.dependency_overrides[get_current_user_or_api_key]` 注入不同 user dict，断言实际请求落到的平台 token 来源与 URL host。

### 基线 User dict 结构（对照 `deps.py:89-98`）

| auth_type | repo_key | agent_type | 期望 provider |
|-----------|----------|------------|---------------|
| session_token | `gitcode:o/hello` | vod | gitcode |
| session_token | `github:o/hello` | vod | github |

---

### TC-1 provider 由 token 派生（github 仓库）

```python
def test_provider_derived_from_token_github(_client, gitcode_calls):
    # user["repo_key"] = github:o/hello → 透传走 GitHub API
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "git_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": f"github:{OWNER}/{REPO}"}
    client = _client
    r = client.get(f"/api/git-repo/{OWNER}/{REPO}/branches")
    assert r.status_code == 200
    _u = gitcode_calls[-1]["url"]
    assert "api.github.com" in _u            # 平台 host = GitHub
    assert gitcode_calls[-1]["token"] == BOT  # 走仓库 bot token
```

### TC-2 provider 由 token 派生（gitcode 仓库）

```python
def test_provider_derived_from_token_gitcode(_client, gitcode_calls):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "git_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": f"gitcode:{OWNER}/{REPO}"}
    client = _client
    r = client.get(f"/api/git-repo/{OWNER}/{REPO}/branches")
    assert r.status_code == 200
    assert "api.gitcode.com" in gitcode_calls[-1]["url"]
```

### TC-3 忽略调用方传的 provider 参数（以 token 为准）

```python
def test_provider_param_ignored_when_token_authoritative(_client, gitcode_calls):
    # token=github，调用方误传 X-Git-Provider: gitcode → 仍走 GitHub
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "t", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": f"github:{OWNER}/{REPO}"}
    client = _client
    r = client.get(f"/api/git-repo/{OWNER}/{REPO}/branches",
                   headers={"X-Git-Provider": "gitcode"})
    assert r.status_code == 200
    _u = gitcode_calls[-1]["url"]
    assert "api.github.com" in _u and "api.gitcode.com" not in _u
```

### TC-4 git-credential / workspace 同理派生（/api/repos 平台接口）

```python
def test_repos_credential_provider_from_token(_client):
    # 复用 TC-1 同 user（github repo_key）→ git-credential provider=github
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "t", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": f"github:{OWNER}/{REPO}"}
    client = _client
    cred = client.get(f"/api/repos/{OWNER}/{REPO}/git-credential").json()
    assert cred["provider"] == "github"
    assert cred["clone_url"].startswith("https://github.com/")
    ws = client.get(f"/api/repos/{OWNER}/{REPO}/workspace").json()
    assert ws["clone_url"].startswith("https://github.com/")
```

### TC-5 未知 provider 防御（白名单校验仍生效）

```python
def test_unsupported_provider_in_token_rejected(_client):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "t", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": f"gitlab:{OWNER}/{REPO}"}
    client = _client
    r = client.get(f"/api/git-repo/{OWNER}/{REPO}/branches")
    assert r.status_code == 403
    assert "未接入" in (r.json().get("detail", {}).get("message", ""))
```

### 契约测试同步（test_openapi.py）

- 端点 operationId / path **不变**（仅 provider 判定逻辑改），`EXPECTED_PUBLIC` 无需增删。
- 若实施选择「移除参数」：确认 OpenAPI schema 中 `X-Git-Provider` header 与 `provider` query 不再出现在 git-repo 各端点（参数级断言需新测或删旧 api-route 用例）。

---

## 五、影响面与风险

| 项 | 影响 | 缓解 |
|----|------|------|
| 调用方已传 provider 参数 | 若行为改为「忽略」，旧调用方无感；若改为「不一致报 400」，需文档明示 | 建议忽略（向后兼容、行为收敛） |
| 测试 | `test_git_repo_api.py` 有 provider 头/query 用例，需改为 token 派生断言 | 同步更新 |
| 文档 | 10-git-repos / 02-repos / issue-45-* 多处 provider 描述 | 全量同步 |

---

## 六、实施记录（2026-09-12，已完成）

- `git_repo_api.py`：`_provider` 改为从 `user["repo_key"]` 前缀派生（`parse_repo_key`），删除 `Header`/`Query` 依赖；头部注释更新。
- `src/api/routes/repos.py`：`_repo_provider` 同构改造（git-credential/workspace）；清理 `Header`/`Query` import。
- `tests/test_git_repo_api.py`：新增 `github_calls` fixture；重写 `test_provider_header_and_query`（缺省 gitcode + 参数忽略）；新增 TC-1~TC-5（gitcode/github 派生、参数忽略、git-credential/workspace 派生、gitlab 403）。
- 运行时契约验证：`/api/git-repo` 16 端点 + `/api/repos` git-credential/workspace **均无 `X-Git-Provider`/`provider` 参数**。
- 文档：`10-git-repos.md`（后端区分段 + 仓库信息入参）、`02-repos.md`（语义句 + 两端点 Provider 项）、`issue-45-git-repos-api-design.md` / `issue-45-cli-design.md` 顶部加修订批注。
- 测试：`test_git_repo_api` 20 passed + `test_openapi`/`test_help_directory` 51 passed。