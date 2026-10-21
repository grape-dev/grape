# 方案：托管仓库平台路由修复（GitHub 仓库被当 GitCode 托管 → 401/404）[weekly-08 分支]

> 状态：**已实施**（随 fix(repos) 提交）｜目标分支：weekly-08（部署分支 weekly-08-lyz）
> **敏感信息声明**：本文不含真实 IP、域名、账号、用户 ID、仓库名或 token（一律 `<xxx>` 占位）。
> 现场：环境 2（`<env_host>`，weekly-08-lyz）托管 GitHub 项目 `<owner>/<repo>` 失败：
> `GitCode API POST .../repos/<owner>/<repo>/hooks` →
> 第一轮 **401 Bad credentials**（身份层：旧承载账号 gitcode token 失效，已另行止血）；
> 第二轮 **404 Project not found**（身份层修复后仍走 GitCode 分支，但该仓库在 GitCode 不存在）。
>
> 本文只针对**平台路由缺陷**（把 GitHub 仓库当 GitCode 处理）；身份/凭据错位为环境数据问题已各自止血。

---

## 一、问题现象

- 用户在仓库页点击托管一个 **GitHub** 项目（例如 `<owner>/<repo>`）。
- 前端 `hostAndSelect` 仅 `POST /api/repos/host {full_name}`，**不携带 provider**。
- 后端 `host_repo` 自动判定 provider → 走 **GitCode 分支** → 调用
  `gitcode.com/api/v5/repos/{owner}/{repo}/hooks`：
  - gitcode token 失效 → **401**；
  - token 有效但仓库不在 GitCode（GitHub 仓库天然不在） → **404 Project not found**。
- 期望：GitHub 仓库由 **GitHub API** 托管（`api.github.com/.../hooks` + bot 协作者）。

## 二、根因分析

### 2.1 提供者（provider）判定链（`src/api/routes/repos.py` `host_repo`）

```python
# D44: 优先用调用方传入的 provider；未传时自动选择（向后兼容）
if provider:
    ...
else:
    provider, api_token = _get_user_provider_and_token(
        user["username"], user.get("login_provider", ""), user.get("id", 0))
```

`_get_user_provider_and_token` 核心：

```python
if login_provider == "gitcode":            # ← 当前登录平台（GitCode OAuth）
    gc = get_user_token_with_refresh(...)   # 只要有 gitcode token 就返回 gitcode
    if gc:
        return "gitcode", gc                # ← 短路，与仓库真实平台无关
...
gh = get_user_github_token_with_refresh(...)
if gh:
    return "github", gh
```

**缺陷**：provider 完全由「登录用户的 login_provider / 平台 token 是否存在」决定，
**从不考虑「被托管仓库实际属于哪个平台」**。

### 2.2 前端不传来源平台（`src/api/static/js/pages/repos.js`）

```js
function hostAndSelect(fullName, owner, name, repoKey, event) {
    API.post('/repos/host', {full_name: fullName}).then(...)
}
```
- `repoKey` 已含平台前缀（`github:`/`gitcode:`），但未用来传 provider。

### 2.3 为什么 401 之后变 404

- 401 层：待用 gitcode token 失效（环境身份错位侧）。
- 止血后 token 有效 → 同一缺陷继续走到 GitCode API → 仓库不在 GitCode → **404**。
- 两层都是「GitHub 仓库被按 GitCode 流程托管」的不同症状。

## 三、方案设计

### 3.1 前端：`hostAndSelect` 透传 provider（来源平台）

按 `repoKey` 前缀拼 `/repos/host?provider=github|gitcode`（body 仍为 `{full_name}`）。

### 3.2 后端：provider 判定改为「仓库来源为准」，禁止 login_provider 短路

| 优先级 | 来源 | 取法 |
|---|---|---|
| 1 | 调用方显式 `provider` | 直接用；GitHub 需 github token，缺失 → `TOKEN_NOT_BOUND`，**不回退 gitcode** |
| 2 | `full_name` / managed 记录前缀 | `github:`/`gitcode:` → 来源 |
| 3 | 无来源线索兜底 | 按平台 token 可用性（github 优先），不再按登录平台短路 |

新增 `_provider_from_hint(full_name)` / `_provider_from_token(user)`；`host_repo` 显式 > hint > token。

### 3.3 错误文案与状态码

| 场景 | 返回 |
|---|---|
| provider=github 无 github token | `400 TOKEN_NOT_BOUND`：绑定 GitHub 后重试 |
| gitcode webhook 401 | `502`：GitCode 登录已失效，请重新绑定/登录 |
| gitcode webhook 404 | `502`：仓库不存在于 GitCode，请确认仓库所属平台与托管账号平台一致 |

### 3.4 关键决策

| 决策 | 依据 |
|------|------|
| 来源平台优先（前端/前缀/managed），login 仅兜底 | 仓库平台是客观事实，登录平台是会话事实，可能不同 |
| GitHub 无 token 直接 400 不回退 | 避免「点 GitHub 却打 GitCode」静默错路由 |
| 保留 query `?provider=` 通道 | 后端签名已支持，既有调用方兼容 |

## 四、各模块改动

| 模块 | 改动 |
|------|------|
| `src/api/static/js/pages/repos.js` | `hostAndSelect` 按 `repoKey` 前缀拼 `?provider=` |
| `src/api/routes/repos.py` | `_provider_from_hint`/`_provider_from_token`；`host_repo` 判定顺序；`setup_webhook` 401/404 文案；前缀剥离 |
| `src/api/static/index.html` | repos.js `?v=` bump |
| `tests/` | 见 §六 |
| （已实施） | 根因相关历史提交含 `4bc7cf536`（归属子查询引入）等；本修复独立 |

## 五、影响面 / 风险

| 项 | 说明 |
|---|---|
| 既有调用方 | `create_from_template`（已带 provider）与 `?provider=` 兼容 |
| 行为变化 | 修正「GitHub 仓库被当 GitCode」错误路由；缺 github token → 明确绑定引导 |
| 安全 | 无新增权限面；bot 协作者逻辑不变 |

## 六、测试（测试先行，TDD）

### 6.1 后端 `tests/test_repos_host_provider.py`（7 项）

| ID | 场景 | 断言 |
|---|---|---|
| HT-01 | 显式 `?provider=github` | 只调 `setup_github_webhook`，不触 gitcode |
| HT-02 | `github:` 前缀、不传 provider | 定向 GitHub 分支 |
| HT-03 | managed 已登记 `github:` 前缀 | 定向 GitHub 分支 |
| HT-04 | provider=github 但无 github token | 400 `TOKEN_NOT_BOUND`；两个 setup 均不调用 |
| HT-05 | gitcode + 404 | 502，文案含「不存在于 GitCode」 |
| HT-06 | 用户 GitCode 登录 + `github:` 仓库 | 走 GitHub 分支（本 bug 核心场景） |
| HT-07 | gitcode + 401 | 502，文案含「登录已失效」 |

### 6.2 前端 `tests/js/test_repos_host_provider.js`（5 项）

provider 推导（github:/gitcode:）、URL 拼 `?provider=`、body 恒 `{full_name}`、`API.post(_url)`、
无前缀回退 `/repos/host`。

### 6.3 回归

repos/related 套件全绿；`repair-host`/remove 按 repo_key 前缀分流回归。

## 七、提交约定

- `fix(repos): route hosting by repo's real provider, not login platform`
- 前端改后按 `?v=` bump。
- docs：`docs/weekly-08/host-repo-provider-routing-fix-design.md`