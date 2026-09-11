# git-repo 支持仓库目录树/文件列表——平台侧实现方案

> 需求来源：grape-cli 仓库 `docs/grape-server-requirement-gitrepo-tree-list.md`
> 目标仓库：grape 主仓库（`src/api/git_repo_api.py` / OpenAPI 契约）
> 优先级：P1（QA/开发类 agent 普遍需要；减少结构探测的无效调用与 token 浪费）
> 日期：2026-09-11
> **实现状态**：✅ 已实施（新增 `gitrepo_contents_root` 根路由 + contents 目录语义；tree 独立方案见
> `issue-git-repo-tree-endpoint-design.md`）

## 一、问题背景

QA Agent 审查 PR 时需枚举仓库结构（README / AGENTS.md / CI 配置 / spec / test 等）。
当前 `git-repo contents --path <file>` 只支持取单文件 base64，**没有一次拿目录树/列表的能力**，
agent 只能"猜常见路径逐一探测"：

- 实测会话（`ses_f702d3661ffe7BRftbNoXx7wnx`）对 AGENTS/CLAUDE/.github/spec/test/package.json
  等**同一套路径探测 3 轮**，不存在的路径各吃一次 404，大量无效调用与 token 浪费。

## 二、现状核实（已实际调用 GitCode 平台 + 读源码）

### 2.1 现有端点（`src/api/git_repo_api.py:223-227`）

```python
@router.get("/{owner}/{repo}/contents/{path:path}", operation_id="gitrepo_contents", summary="文件内容（base64）")
def get_file_content(owner, repo, path, ref="", user=..., p=...):
    return _gitcode(p, "GET", _vcs_url(owner, repo, "/contents/" + path, {"ref": ref}, p),
                    _repo_token(owner, repo, user, p))
```

- `_gitcode` 直接透传平台响应（无任何适配/二次加工）。
- `_vcs_url` 过滤空 params；空 path → `/contents/`（根）。

### 2.2 GitCode 平台 `/contents/` 实际响应（grape 仓库实测）

| 请求 | 状态 | 响应 | 语义 |
|------|------|------|------|
| `GET /contents/`（根，尾斜杠） | **200** | **JSON 数组**（根目录条目：`.opencode`, `configs`, ...） | 目录 → 列表 |
| `GET /contents`（无 path） | **200** | **JSON 数组**（同样根目录列表） | 目录 → 列表 |
| `GET /contents/configs`（目录） | **200** | **JSON 数组**（该目录条目：`admin.env.example`, ...） | 目录 → 列表 |
| `GET /contents/README.md`（文件） | **200** | **JSON 对象**（`{type:file, encoding:base64, size, content}`） | 文件 → 单对象 |

**平台语义**（与 GitHub 一致）：`/contents/<path>` 对**目录返回数组**、对**文件返回单对象**。

### 2.3 核心判断

**现有 `get_file_content` 已是纯透传，天然支持目录/根列表**——服务端无需任何平台适配层改动。
只要 `path=""` 能命中 `contents/{path:path}` 路由（FastAPI `{path:path}` 允许空串），
根目录请求 `/contents/` 就会返回目录条目数组。

## 三、实现方案（选项 A：复用现有 contents 端点）

> 选选项 A（改现有端点语义），不用选项 B（新增 /tree 端点）：GitCode 平台 contents 已满足
> "目录数组 + 文件单对象"，无递归树 API 依赖；改动最小、零平台适配。

### 3.1 路由空 path 处理（确认/加固）

`contents/{path:path}` 对 `path=""`：
- 期望：请求 `/contents/` → GitCode 返回根目录数组。
- 若 FastAPI 对空 `path:path` 有匹配歧义，改为显式双路由或 `path` 默认 `""` + 特判：
  ```python
  # 方式一（现路由即可，空串通常可匹配）
  @router.get("/{owner}/{repo}/contents/{path:path}", ...)
  def get_file_content(owner, repo, path="", ref="", ...):
  ```
  （`path` 默认 `""`，路由透传 `/contents/` 或 `/contents/{path}`）

### 3.2 契约/说明语义更新（不改透传逻辑）

- `operation_id="gitrepo_contents"` 的 `summary` 从"文件内容（base64）"扩为
  **"文件内容 / 目录列表（path 空=根；目录返回数组，文件返回 base64）"**。
- 出参即平台原始结构：
  - 目录/根 → `[{type: dir|file, name, path, sha, url, html_url, ...}]`
  - 文件 → `{type, encoding, size, name, path, content(base64)}`

### 3.3 兼容性

- `contents --path <file>` 单文件 base64 行为**不回归**（透传不变，文件仍返回对象）。
- `--path ""` / `--path <dir>` 新语义可用。

## 四、改动与测试清单

| # | 项 | 说明 | 优先级 |
|---|----|------|--------|
| 1 | `git_repo_api.py` `get_file_content` | `path` 默认 `""`；确认根路径路由命中 | 必须 |
| 2 | `git_repo_api.py` summary | 扩为"文件内容 / 目录列表" | 必须 |
| 3 | `tests/test_git_repo_api.py` | 补 `--path ""`（根）断言返回数组 + `type` 字段；目录返回数组；文件 base64 不回归 | 必须 |
| 4 | `help/api/10-git-repos.md` | contents 段说明目录/根语义（同步 help） | 必须 |
| 5 | OpenAPI 契约 | `gitrepo_contents` summary/描述更新（FastAPI 自动生成） | 自动 |
| 6 | CLI 侧 | `contents --help` 双向化（列目录 / 取文件） | 独立 |

## 五、验收标准

- [ ] `grape git-repo contents --path "" --json-out` → 返回根目录条目列表（含 `type: file/dir`）
- [ ] `--path <dir>` → 返回该目录条目列表
- [ ] `--path <file>` → 仍返回 base64 内容（不回归）
- [ ] 契约 `openapi.json` 的 `gitrepo_contents` summary 含"目录/文件"语义
- [ ] `help/api/10-git-repos.md` contents 段含目录/根语义说明
- [ ] 现有 `test_contents_branches_milestones_labels` 不回归

## 六、影响评估

| 项 | 说明 |
|----|------|
| Agent 体验 | QA/开发 agent 一步拿目录树，省多轮探测；内容/CI/spec/test 现状一目了然 |
| 帮助联动 | 契约 summary 更新 → CLI 自动生成命令 + 帮助示例双向化 |
| 兼容性 | 仅扩目录语义，文件内容行为不变 |
| 工作量 | 小：路由默认值 + summary + 测试 + help 文档 |

## 七、关键代码位置

| 位置 | 说明 |
|------|------|
| `src/api/git_repo_api.py:223-227` | `get_file_content`（path 默认空 + summary 更新） |
| `src/api/git_repo_api.py` | `_gitcode` / `_vcs_url`（纯透传，无需改） |
| `src/api/git_repo_api.py:26-27` | `_GITCODE_API_BASE` / `_GITHUB_API_BASE`（`/contents/` 语义） |
| `tests/test_git_repo_api.py:156` | `test_contents_branches_milestones_labels`（补根/目录用例） |
| `help/api/10-git-repos.md` | contents 段说明（同步刷新） |