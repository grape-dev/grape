# git-repo 增加仓库目录树（tree）获取接口——平台侧独立方案（选项 B）

> 需求关联：`docs/grape-server-requirement-gitrepo-tree-list.md`（与选项 A「contents 目录列表」并列的独立方案）
> 目标仓库：grape 主仓库（`src/api/git_repo_api.py` / OpenAPI 契约）
> 状态：**✅ 已实施**（`gitrepo_tree` 单层透传 + GitCode recursive 明确拒绝 + GitHub 递归支持；见 `src/api/git_repo_api.py`）
> 日期：2026-09-11

---

## 一、需求定位

在 `git-repo` 能力中增加**仓库目录树（tree）获取接口**，作为「contents 单文件/目录列表」之外的
**整树/子树一次性获取**能力选项。目标用户：QA/开发类 agent 需要"一眼看到仓库全部文件与层级"。

## 二、与选项 A 的关系（并列独立方案）

| 维度 | 选项 A（contents 目录列表） | 选项 B（tree 端点，本文） |
|------|---------------------------|--------------------------|
| 能力 | 单层目录列表（根/任意 dir） | 整树 / 单层树（含 type/sha） |
| 请求数 | 1 次 | 1 次（若 recursive 可用）或 N 次（自建递归） |
| 现状验证 | GitCode contents 对目录返回数组，透传即可 | **GitCode Tree API 部分支持（见下）** |
| 定位 | 满足"枚举结构"最常见诉求 | "整树导出/深层视角"的增强 |

> 两者可并存：A 先落地（零成本），B 作为单层树增强，递归树需平台支持。

## 三、GitCode 平台 Tree API 实测（2026-09-11，grape 仓库）

### 3.1 实测矩阵

| 请求 | 状态 | 返回 | 结论 |
|------|------|------|------|
| `GET /git/trees/main`（单层） | 200 | 20 条 = **9 tree + 11 blob**（type/name/path/mode/sha） | ✅ **单层树可用，结构标准** |
| `GET /git/trees/main?recursive=1` | 200 | 20 条**全 tree、无 blob** | ⚠️ **recursive 语义异常**：丢失文件，非完整递归树 |
| `GET /git/trees/main?recursive=true` | 400 | Bad Request | ⚠️ 非标准参数被拒 |
| `GET /git/trees/HEAD` | 200 | 同 main（单层） | ✅ 支持 HEAD/分支引用 |
| `GET /trees/main`（无 git 前缀） | 404 | Not Found | ❌ 必须 `git/trees/{ref}` |
| `GET /git/refs/heads/main` | 404 | Not Found | ❌ refs 端点不存在 |

### 3.2 关键结论

1. **单层树可用**：`git/trees/{ref}` 返回标准 tree（tree+blob 混合，含 type/name/path/mode/sha），
   可作为"指定分支/层级的目录树"获取接口。
2. **递归树不可用**：GitCode 的 `recursive` 实现与 GitHub 不兼容——`recursive=1` 返回的树
   **丢失全部 blob 文件**（仅剩 tree 目录），无法直接用于"一次性拿整仓库"。
3. **无 refs/对象端点**：无法按标准 Git Data 流程解析 commit→tree sha 链。

## 四、方案设计（选项 B：`GET /tree`）

### 4.1 接口定义

```
GET /api/repos/{owner}/{repo}/tree
    query: ref（分支/引用，默认 "" → 自动解析 main/默认分支）
           depth(可选)："single"（单层，默认） | "recursive"（递归，平台支持时）

返回（200）：
  单层：{ "ref": "main", "tree": [ {type, name, path, mode, sha}, ... ] }
  递归：{ "ref": "main", "tree": [ ... 全路径条目 ... ], "truncated": bool }
```

- 认证：`get_current_user_or_api_key` 三通道，沿用 `_repo_token`。
- 白名单：进契约（`custom_openapi` 正常导出），CLI `grape git-repo tree` 可用。

### 4.2 实现要点（GitCode 递归残缺的应对）

- **单层（default）**：透传 `GET /git/trees/{ref}`，直接返回平台 tree 数组（结构已标准，零二次加工）。
- **递归（recursive）**：因 GitCode 平台 `recursive` 返回破碎树，
  **本期不做递归**（或降级为"仅单层"，避免多步请求抵消减少探测的意义）。
  - 契约中 `depth` 仅暴露 `single`；`recursive` 标记 `planning`（待平台修复）。
- **ref 解析**：`ref=""` 时用 `GET /branches` 拿默认分支（实测可用）或调用方传入；
  也可支持 `ref=<sha>` 直引。

> 注：GitHub provider 走 `api.github.com` 标准的 `git/trees/{sha}?recursive=1`（GitHub 支持递归），
> 与 GitCode 行为分开适配（`_vcs_url` / provider 分支）。

### 4.3 约束

- 认证：同现有 git repo 接口；仅会话级令牌 `_repo_token`。
- 兼容：不新增对 `contents` 的影响；`contents` 目录列表能力（选项 A）独立存在。
- provider：GitCode / GitHub 分别适配（GitHub 递归完整，GitCode 单层）。

## 五、改动与测试清单（实施时参考）

| # | 项 | 说明 |
|---|----|------|
| 1 | `git_repo_api.py` 新增 `GET /{owner}/{repo}/tree` | 单层透传；recursive 仅 GitHub；ref 解析 |
| 2 | `_vcs_url` | 支持 `/git/trees/{ref}` 路径拼接（GitCode） |
| 3 | `tests/test_git_repo_api.py` | 单层 tree 数组断言；GitHub recursive 断言；ref="" 默认分支 |
| 4 | `help/api/10-git-repos.md` | tree 段说明（单层/recursive 平台差异） |
| 5 | OpenAPI 契约 | `gitrepo_tree` 自动导出（FastAPI 自动） |
| 6 | CLI 侧 | `grape git-repo tree --ref main` 命令生成（从契约） |

## 六、验收标准（实施时参考）

- [ ] `grape git-repo tree --ref main` → 返回单层 tree（tree+blob，含 type/path/sha）
- [ ] `ref=""` → 自动解析默认分支
- [ ] GitHub provider `--ref main --recursive` → 返回完整递归树
- [ ] GitCode provider `recursive` → 明确提示"暂不支持递归"（或单层降级），不产生破碎树
- [ ] 契约/帮助导出；现有 contents 不回归

## 七、影响评估

| 项 | 说明 |
|----|------|
| Agent 体验 | 单层树可替代多轮路径探测；递归树待平台支持后可达"整树一览" |
| 平台适配 | **GitCode 递归残缺是不确定点**——单层确定可用，递归需等平台修复或自建聚合 |
| 工作量 | 中：单层透传小；递归（若坚持）大（N 次 `/git/trees/{sha}` 聚合） |
| 兼容性 | 新增端点，不影响 contents/issues 等既有能力 |

## 八、结论

- **单层 tree 接口**：可行、低成本（透传 GitCode `git/trees/{ref}`），建议实施时作为
  `gitrepo_tree` 端点落地。
- **递归 tree 接口**：GitCode 平台当前不支持（递归返回破碎树），**不建议本期实施递归**；
  仅 GitHub 侧可做 `recursive=1`（平台原声支持）。
- 与选项 A（contents 目录列表）**互不冲突**：A 覆盖"单层列表"最常见诉求，B 覆盖"单层树（含 sha，
  可继续下钻子树）"，两者都用单层，避免触碰 GitCode 递归残缺。

## 九、关键代码位置（参考）

| 位置 | 说明 |
|------|------|
| `src/api/git_repo_api.py` | 新增 `GET /{owner}/{repo}/tree`（operation_id=`gitrepo_tree`） |
| `src/api/git_repo_api.py:148` | `_vcs_url`（拼 `/git/trees/{ref}`，GitCode/GitHub 分支） |
| `src/api/git_repo_api.py:26-27` | `_GITCODE_API_BASE` / `_GITHUB_API_BASE` |
| `tests/test_git_repo_api.py` | 新增 tree 用例 |
| `help/api/10-git-repos.md` | tree 段说明 |