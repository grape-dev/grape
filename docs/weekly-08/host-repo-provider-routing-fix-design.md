# 方案：托管仓库平台路由修复（GitHub 仓库被当 GitCode 托管 → 401/404）[weekly-08 分支]

> 状态：**待实施**｜目标分支：weekly-08（部署分支 weekly-08-lyz）｜关联：REPOS-4 平台分流
> 现场：环境 2（120.46.222.221，weekly-08-lyz）托管 GitHub 项目 `Jason7602/mygateway` 失败：
> `GitCode API POST .../repos/Jason7602/mygateway/hooks` →
> 第一轮 **401 Bad credentials**（身份层：旧 id7 失效 gitcode token，已另行止血）；
> 第二轮 **404 Project not found: Jason7602/mygateway**（身份层已修复后，仍走 GitCode 分支，但该仓库在 GitCode 不存在）。
>
> 本文档只针对**平台路由缺陷**（把 GitHub 仓库当 GitCode 处理）；身份/凭据错位（old user_accounts vs new grape_accounts）为环境数据问题，已在生产/环境2 分别止血，不在本文范围。

---

## 一、问题现象

- 用户在仓库页点击托管一个 **GitHub** 项目（如 `Jason7602/mygateway`、`Jason7602/jason-skills-test`）。
- 前端 `hostAndSelect` 仅 `POST /api/repos/host {full_name}`，**不携带 provider**。
- 后端 `host_repo` 自动判定 provider → 走 **GitCode 分支** → 调用
  `gitcode.com/api/v5/repos/{owner}/{repo}/hooks`：
  - 若 gitcode token 失效 → **401**；
  - 若 token 有效但仓库不在 GitCode（GitHub 仓库天然不在） → **404 Project not found**。
- 期望：GitHub 仓库应由 **GitHub API** 托管（`api.github.com/repos/.../hooks` + bot 协作者），并按 GitHub 的分支处理。

## 二、根因分析

### 2.1 提供者（provider）判定链（`src/api/routes/repos.py:766 host_repo`）

```python
# D44: 优先用调用方传入的 provider；未传时自动选择（向后兼容）
if provider:
    if provider == "github":
        api_token = get_user_github_token_with_refresh(...)
    else:
        api_token = get_user_token_with_refresh(...)
else:
    provider, api_token = _get_user_provider_and_token(
        user["username"], user.get("login_provider", ""), user.get("id", 0))
```

`_get_user_provider_and_token`（`repos.py:35`）核心：

```python
if login_provider == "gitcode":            # ← 当前登录平台（GitCode OAuth）
    gc = get_user_token_with_refresh(...)   # 只要有 gitcode token 就返回 gitcode
    if gc:
        return "gitcode", gc                # ← 短路，与仓库真实平台无关
elif login_provider == "github": ...
gh = get_user_github_token_with_refresh(...) # 只有上方都没命中才看 github
if gh:
    return "github", gh
return "gitcode", get_user_token_with_refresh(...)   # 兜底 gitcode
```

**缺陷**：provider 完全由「登录用户的 login_provider / 平台 token 是否存在」决定，
**从不考虑「被托管仓库实际属于哪个平台」**。

### 2.2 前端不传来源平台（`src/api/static/js/pages/repos.js:504-506`）

```js
function hostAndSelect(fullName, owner, name, repoKey, event) {
    API.post('/repos/host', {full_name: fullName}).then(...)
}
```

- `repoKey` 参数**已经包含平台前缀**（`github:...` / `gitcode:...`），但没有被用来传 provider。
- 于是后端只能靠登录平台猜 → 只要用户是 GitCode 登录，**托管任何 GitHub 仓库都会被当 GitCode 处理**。

### 2.3 为什么 401 之后变 404

- 401 层：使用的 gitcode token 失效（环境身份错位，`user_credentials(id7)` 陈旧凭据被 `_get_gitcode_credential_by_gitcode_id` 跨用户匹配命中）。
- 止血（删除 id7 凭据/Key）后 token 恢复有效 → 同一缺陷继续走到 GitCode API → 仓库不在 GitCode → **404**。
- 两层都是「GitHub 仓库被按 GitCode 流程托管」的不同症状，本文修路由层。

---

## 三、方案设计

### 3.1 前端：`hostAndSelect` 透传 provider（来源平台）

从 `repoKey` 前缀推导并随请求传递：

```js
function hostAndSelect(fullName, owner, name, repoKey, event) {
    var _provider = (String(repoKey || '').indexOf('github:') === 0) ? 'github'
                  : (String(repoKey || '').indexOf('gitcode:') === 0) ? 'gitcode' : '';
    API.post('/repos/host' + (_provider ? '?provider=' + _provider : ''), {full_name: fullName})
      .then(...)
}
```

- 后端 `host_repo` 已支持 query 参数 `provider`（FastAPI 自动绑定），无需改签名。

### 3.2 后端：provider 判定改为「仓库来源为准」，禁止 login_provider 短路

**判定原则（按优先级）**：

| 优先级 | 来源 | 取法 |
|---|---|---|
| 1 | 调用方显式 `provider`（前端从 repoKey 推导） | 直接用；GitHub 需 github token，缺失 → `TOKEN_NOT_BOUND`，**不回退 gitcode** |
| 2 | `body.full_name` / managed 记录的前缀 | `github:`/`gitcode:` 前缀即来源 |
| 3 | 用户仓库列表归属 | 在 `managed_repos` 中已登记的平台 |
| 4 | 兜底：两个平台 token 都取到才自动选（不再用 login_provider 单一短路） | github token 存在 → github；否则 gitcode（并给出可读提示） |

具体改动（`host_repo` 内 provider 解析段）：

```python
# 旧：provider, api_token = _get_user_provider_and_token(...)
# 新：来源优先
_repo_provider_hint = _provider_from_hint(body.full_name, user, db)   # 前缀 / managed / 用户列表
if provider:                       # 显式（前端已带）
    pass
elif _repo_provider_hint:
    provider = _repo_provider_hint
else:                              # 无任何来源线索才自动选
    provider, _ = _get_user_provider_and_token(...)
# GitHub 必须拿到 github token；拿不到 → 明确 400 TOKEN_NOT_BOUND，不回退 gitcode
if provider == "github":
    api_token = get_user_github_token_with_refresh(...)
    if not api_token:
        raise HTTPException(400, {"code":400,"error_code":"TOKEN_NOT_BOUND",
                                  "message":"GitHub API Token 未配置，请绑定 GitHub 后重试"})
else:
    api_token = get_user_token_with_refresh(...)
```

- `_provider_from_hint`：`body.full_name` 带 `github:`/`gitcode:` 前缀 → 直接取；否则查
  `managed_repos`/`user_repos` 中该 full_name 的 `repo_key` 前缀。
- **保留** `_get_user_provider_and_token` 但仅用于「无来源线索」时的兜底；同时去掉
  `login_provider` 单平台短路（避免“登录平台≠仓库平台”时误判），改为「哪个平台有可用 token 才选哪个」。

### 3.3 错误文案与状态码（便于排障）

| 场景 | 返回 |
|---|---|
| 显式/provider 判定 = github 但无 github token | `400 TOKEN_NOT_BOUND`：绑定 GitHub 后重试 |
| 判定 = gitcode 但仓库不存在于该 owner | 区分 「token 有效但仓库 404」→ `仓库 {owner}/{repo} 不存在于 GitCode/GitHub`；维护 token 失效话术（`GitCode/GitHub 登录已失效`），不再笼统「托管失败」 |
| 创建 webhook 401/403 | 沿用平台 token 失效/权限提示（`checks` 已有 401 识别） |

### 3.4 关键决策

| 决策 | 依据 |
|------|------|
| 来源平台优先（前端/前缀/managed），login 仅兜底 | 仓库平台是客观事实，登录平台是会话事实，两者可能不同（用户 GitCode 登录但托管 GitHub 仓库） |
| GitHub 无 token 直接 400 不回退 | 避免「点 GitHub 却打 GitCode」的静默错路由（本次事故根因） |
| 保留 query `?provider=` 通道 | 后端签名已支持、create_from_template 等既有调用方继续兼容 |
| 前端从 `repoKey` 推导 | repoKey 已带平台前缀（`github:`/`gitcode:`），无需额外接口 |

---

## 四、各模块改动

| 模块 | 改动 |
|------|------|
| `src/api/static/js/pages/repos.js` | `hostAndSelect`：按 `repoKey` 前缀拼 `?provider=...` 传给 `/repos/host` |
| `src/api/routes/repos.py` | `host_repo` provider 解析：显式 > 仓库来源提示 > 兜底；新增 `_provider_from_hint`；GitHub 无 token 短路 400；去 `login_provider` 单平台短路 |
| `src/api/routes/repos.py` | 错误响应拆分：`TOKEN_NOT_BOUND` / 仓库不存在于该平台 / token 失效文案 |
| `tests/` | 见 §六 |

**未改动**：`_post_*`/`setup_webhook`/`setup_github_webhook` 本体、`require_*` 鉴权、数据库结构。

---

## 五、影响面 / 风险

| 项 | 说明 |
|---|---|
| 既有调用方 | `create_from_template`（已带 provider）与 `?provider=` 通道不受影响 |
| 仅后端修复不改前端 | 老前端仍不传 provider → 走「仓库来源提示」兜底；`mygateway` 若已 managed 为 github: 前缀也能正确定向 |
| 行为变化 | 修正「GitHub 仓库被当 GitCode」的错误路由；缺失 github token 时明确返回绑定引导而非尝试打 gitcode |
| 安全 | 无新增权限面；GitHub bot 协作者逻辑不变 |

---

## 六、测试用例（测试先行，TDD）

> 用例先于实现编写并运行（红灯），实现后转绿；后端用 `fastapi TestClient`（对齐 `tests/test_api_routes.py`、`tests/test_issues_api.py` 风格，`patch` 目标为 `src.user.repo_manager.setup_webhook/setup_github_webhook` 等），前端用 node+vm 源码断言（对齐 `tests/js/*`）。

### 6.1 后端 `tests/test_repos_host_provider.py`

| ID | 场景（输入） | 断言（期望） |
|---|---|---|
| HT-01 | `POST /api/repos/host?provider=github`，`{full_name:"Jason7602/mygateway"}`，用户有 github token | 200 `{ok:true}`；`setup_github_webhook` 被调、`setup_webhook`（gitcode）**未被调**；`managed_repos.repo_key` 以 `github:` 开头 |
| HT-02 | 不传 provider，`{full_name:"github:Jason7602/mygateway"}` | `_provider_from_hint` 解析 `github` → 走 GitHub 分支（断 HT-01 断言） |
| HT-03 | 不传 provider，`{full_name:"Jason7602/mygateway"}`，且 `managed_repos` 已登记 `repo_key='github:Jason7602/mygateway'` | 按 managed 前缀定向 github → GitHub 分支 |
| HT-04 | 判定 `github` 但该用户**无** github token（mock 返回空） | 400；`detail.error_code == "TOKEN_NOT_BOUND"`；message 含「绑定 GitHub」；`setup_webhook`/`setup_github_webhook` **均未被调** |
| HT-05 | 判定 `gitcode`（有 token），mock `setup_webhook` 抛 `RuntimeError("... failed (404): Project not found...")` | 502；message 含「不存在于 GitCode/Jason7602/mygateway」；不含「Bad credentials」 |
| HT-06 | 用户 `login_provider="gitcode"`（GitCode 登录）+ 托管 `github:` 仓库，不传 provider | **不再走 gitcode**：按仓库来源（HT-02/03 逻辑）进 GitHub 分支 |
| HT-07 | 判定 `gitcode` 且 token 401（mock `get_user_token_with_refresh` 返回失效 token，`setup_webhook` 抛 401） | 502；文案含「GitCode 登录已失效，请重新绑定/登录」 |
| HT-08 | `create_from_template`/既有调用方显式 `provider=github` 或 `provider=gitcode` | 行为与改造前一致（回归：不回归依赖显式 provider 的调用） |
| HT-09 | 显式 `?provider=github` + 仓库实际无 github token | 400 `TOKEN_NOT_BOUND`（显式优先，不回退 gitcode） |
| HT-10 | 无来源线索、无任何平台 token | 400，message 明确「未绑定平台 token」；不静默选平台 |

断言模板（HT-01 示例，`tests/test_repos_host_provider.py`）：

```python
def test_host_github_via_explicit_provider(client, monkeypatch):
    calls = {"gh": 0, "gc": 0}
    def f_gh(*a, **k): calls["gh"] += 1; return 101
    def f_gc(*a, **k): calls["gc"] += 1; return 202
    monkeypatch.setattr("src.user.repo_manager.setup_github_webhook", f_gh)
    monkeypatch.setattr("src.user.repo_manager.setup_webhook", f_gc)
    r = client.post("/api/repos/host?provider=github",
                    json={"full_name": "Jason7602/mygateway"}, headers=_gh_headers)
    assert r.status_code == 200
    assert calls["gh"] == 1 and calls["gc"] == 0        # 未触 gitcode
```

### 6.2 前端 `tests/js/test_repos_host_provider.js`（源级/沙箱）

| ID | 场景 | 断言 |
|---|---|---|
| FS-01 | 源码含 provider 推导 | `hostAndSelect` 体内含 `_provider = (String(repoKey||'').indexOf('github:')===0) ? 'github' ...` 片段 |
| FS-02 | `repoKey="github:Jason7602/mygateway"` | 调用 URL 为 `/repos/host?provider=github` |
| FS-03 | `repoKey="gitcode:Jason7602/grape"` | 调用 URL 为 `/repos/host?provider=gitcode` |
| FS-04 | `repoKey=""`（无前缀） | URL 保持 `/repos/host`（不带 provider，兼容） |
| FS-05 | body 恒为 `{full_name}`（不因加 provider 改变） | `API.post` 第二参 `{full_name: fullName}` |

### 6.3 回归

| 项 | 断言 |
|---|---|
| 现有相关套件 | `tests/test_api_routes.py`、`tests/test_issues_api.py`、`tests/js/*`（repos 相关）全绿 |
| 既有手动路径 | `create_from_template` 显式 provider 不受影响；`repair-host`/`/repos/remove`（按 repo_key 前缀分流）回归 |
| 全量新增失败 | 施行后新增失败 = 0（仅既有已知失败集不变） |

---

## 七、相关文档 / 提交约定

- 现场记录：`temp/events-*` 不相关；`DEPLOYMENT-REMOTE2.md §8`（身份错位背景）。
- 提交（Conventional Commits，`git commit -s`）：
  `fix(repos): route hosting by repo's real provider (github) instead of login platform`
- 前端改动记得 bump `index.html` 中 repos.js 的 `?v=`。