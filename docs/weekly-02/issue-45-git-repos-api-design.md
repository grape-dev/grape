# Issue #45 设计文档：Git 仓库统一接口（/api/repos）与方案③ token 不可感知

> Issue：https://gitcode.com/grape-dev/grape/issues/45
> 类型：Feature | P1 | 状态：spec-ready
> 分支：`weekly-02`
> 日期：2026-09-07
> 范围：技能使用到的 GitCode API 包装为 grape 统一 Git 仓库接口；git clone/push 的 token 不可感知。

## 关联文档

- API 认证：`docs/weekly-02/issue-45-openapi-api-design.md`
- CLI：`docs/weekly-02/issue-45-cli-design.md`
- 技能使用面：`docs/weekly-02/issue-45-skills-api-scan.md`
- 接口参考（按接口）：`docs/openapi/10-git-repos.md`

## 一、目标

- 技能只调 `{{SERVER_URL}}/api/repos/...` + 一个 grape token（API Key）；GitCode token 由 grape 保管。
- **多平台**：接口中立于 gitcode/github/gitlab，`X-Git-Provider`（或 `provider` query）区分后端。
- **方案③**：git clone/push 由 `grape git` 本地执行，token 只在 CLI 进程内获取，agent 不接触平台 token。

## 二、Git 仓库统一接口

### 2.1 前缀与 provider

```
BASE = {{SERVER_URL}}/api/repos
X-Git-Provider: gitcode | github | gitlab   （缺省 gitcode）
或 ?provider=github
```

- `gitcode`：现状 `api.gitcode.com/api/v5`（复用 `repo_manager._req/_req_query`）
- `github` / `gitlab`：未来接入

### 2.2 白名单资源端点（技能实际使用，14 类）

见 `docs/openapi/10-git-repos.md` 完整入参/出参/返回（仓库信息、分支、contents、commits、labels、milestones、issues、issues/comments、pulls、pulls/comments、pulls/files）。

核心：
```
GET|POST  /{owner}/{repo}/issues                  # 列/建 Issue
GET|PATCH /{owner}/{repo}/issues/{n}
GET|POST  /{owner}/{repo}/issues/{n}/comments
GET|POST  /{owner}/{repo}/pulls
GET|PATCH /{owner}/{repo}/pulls/{n}
GET|POST  /{owner}/{repo}/pulls/{n}/comments
GET       /{owner}/{repo}/pulls/{n}/files
GET       /{owner}/{repo}/branches /commits /labels /milestones /contents/*
```

### 2.3 代码归属

- 新增 `src/api/git_repo_api.py`：`dispatch(user_or_key, provider, method, path, params, repo_key)` → 按 provider 解析平台 token → 调平台 client → 返回。
- provider 判定：`X-Git-Provider` 头优先，其次 `provider` query，缺省 gitcode。

## 三、Git 传输层（clone/push）与方案③

### 3.1 clone 是非 REST 的 git 协议

clone/push 走 git Smart HTTP（不经过 `/api/repos` REST 出参）。**两套独立认证**：

| 场景 | 认证 |
|------|------|
| REST `/api/repos/*` | grape api_key（Bearer 三通道） |
| git 协议 clone/push | **对应平台 token**（gitcode bot token / GitHub PAT / GitLab PAT） |

git 协议不认 grape api_key → **clone 必须用平台 token**。

### 3.2 方案③（选定）：grape CLI 本地执行 git，token 进程内获取

```
agent → grape git <任意 git 原生命令> → 本地 git（CLI 内经 /git-credential 取平台 token）
```

- `grape git` 是 **git 原生命令透传**（clone/fetch/checkout/commit/push/log/diff/status 全支持）。
- CLI 持有 grape api_key，通过 `GET /api/repos/{o}/{r}/git-credential` 取一次性平台 token，在 CLI 进程内（credential helper）注入 git。
- **token 不进 agent env、不落磁盘、不进 git 命令行**；进程结束即释放。
- 服务端不实际跑 git（只提供 token/工作区查询）。

**服务端接口（CLI 内部调用）**：

| 能力 | API |
|------|-----|
| 取一次性 git token | `GET /api/repos/{o}/{r}/git-credential`（HTTPS，一次性失效，可审计） |
| 工作区路径 | `GET /api/repos/{o}/{r}/workspace` |

**现状风险点（需配合收敛）**：
- `BOT_GITCODE_TOKEN` env 注入 agent 可见 → 不再注入；
- askpass 脚本落 agent 工作区 → 废弃；
- `git -c http.extraheader=` 命令行暴露 base64 → 改服务端 `GIT_CONFIG` env 瞬时注入（方案③下由 CLI 进程内 credential helper，天然规避）。

## 四、与 CLI 的关系

`grape` 提供两组 Git 能力：
- **`grape git-repo <...>`**：对应 `/api/repos/*` **REST 资源**操作（issue/pulls/comments/contents 等，`--provider` 区分后端）——见 `issue-45-cli-design.md`。
- **`grape git <任意原生 git 命令>`**：**本地 git 传输**（clone/commit/push，方案③ token 隔离）——本节核心。

## 五、改动清单（Git 仓库接口部分）

| # | 模块 | 改动 | 优先级 |
|---|------|------|--------|
| 1 | 新增 `src/api/git_repo_api.py` | `/api/repos/*` 白名单资源端点 + provider 分派 | P0 |
| 2 | 新增 `/git-credential`、`/workspace` | CLI 内部取 token/工作区 | P0 |
| 3 | `cli/grape_cli/commands/gitrepo.py` | `/api/repos/*` REST 命令（issue/pulls/comments/branches/contents/commits/labels）+ `--provider` | P1 |
| 4 | `cli/grape_cli/commands/git.py` | git 原生命令透传 + credential helper（方案③） | P1 |
| 5 | 技能迁移 | `gitcode-api-reference.md`：`api.gitcode.com` → `{{SERVER_URL}}/api/repos/...`；`$BOT_GITCODE_TOKEN` → `$GRAPE_TOKEN` | P1 |
| 6 | `tests/` | provider 分派 + 白名单 + credential + CLI gitrepo/git | P1 |

## 六、验收标准

- [ ] 技能通过 `{{SERVER_URL}}/api/repos/...` + 单一 grape token 完成 GitCode 操作
- [ ] `grape git <任意命令>` 本地执行，平台 token agent 不可感知（零暴露面）
- [ ] provider 头/query 区分三方平台（gitcode 现状，github/gitlab 预留）
- [ ] 白名单限定 14 类资源端点，无限制透传被拒

## 七、风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 透传面过宽 | 高 | 白名单 + 写操作需授权 API Key |
| token 经 API 传输泄露 | 中 | HTTPS + 一次性 + 可审计 |
| 技能迁移遗漏 | 中 | §scan 文档逐技能清单核对 |