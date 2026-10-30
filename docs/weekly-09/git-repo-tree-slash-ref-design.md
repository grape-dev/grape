# git-repo tree 分支名含斜杠 404 修复设计（git-repo-tree-slash-ref-design.md）

> Status：plan-ready（待实施，TDD）
> Branch：`weekly-09`（目标部署分支 weekly-09-lyz）
> Related：`grape-skills#95`（Agent 自动反馈，source=agent）· `src/api/git_repo_api.py` get_repo_tree / _vcs_url · `src/vcs/providers/gitcode.py`
> Sensitive info: this doc contains no real IP, domain, password, account or token (all `<xxx>` placeholders)

---

## 〇、Problem / Observed behavior

对 `feat/issue-11` 等**含 `/` 的分支**执行 `grape git-repo tree --ref feat/issue-11` 返回 **404**；
而 `grape git-repo contents --ref feat/issue-11` 正常。

- 反馈仓库：`gitcode:Jason7602/hello-calculator-11`，任务 `t_98e67fdff8fa`（Agent qa / qa-cli），事件 2363（issue 12）
- 反馈原文建议：tree 命令对 ref 做 URL 编码

## 一、根因（已定位）

1. **CLI → grape 服务端**：ref 走 query 参数（`/api/git-repo/{owner}/{repo}/tree?ref=feat/issue-11`），正常到达服务端。
2. **服务端转发原生 API**：`git_repo_api.py:202` 把 ref 拼进**路径段**：
   ```python
   _vcs_url(owner, repo, f"/git/trees/{ref}", None, p)
   ```
3. **URL 组装不编码 subpath**：`git_repo_api.py:133-146` `_vcs_url` 对 owner/repo 做了
   `quote(..., safe='')`，但 subpath **原样拼接**，ref 里的 `/` 成为 GitCode URL 的路径分隔符：
   ```
   https://gitcode.com/api/v5/repos/<owner>/<repo>/git/trees/feat/issue-11
   ```
   被拆成 `trees/feat` + `issue-11` 两个段 → 资源不存在 → 404。
4. **对照组**：`contents` 把 ref 放 query 参数（`git_repo_api.py:258/265`），`urlencode`
   编码为 `ref=feat%2Fissue-11`，路径不被切开 → 正常。这是同 ref 在 tree 失败、contents 成功的原因。

## 二、目标语义

| 分面 | 语义 |
|------|------|
| 修复 tree | 含 `/` 的分支/引用调用 `git-repo tree` 成功返回树（单层与 GitCode 平台） |
| 平台对齐 | GitCode 用「先解析 sha → 再取树」绕开 ref 路径段；GitHub 保持既有 sha 解析语义（已天然免斜杠） |
| 行为不变 | 无 `/` 的 ref、空 ref（默认分支解析）、recursive 拒绝（GitCode）均保持现状 |
| 数据无损 | 不改响应体结构，只改 upstream URL 的构造方式 |
| 端到端 | CLI `--ref`、WebUI、Agent 内 `grape git-repo tree` 全部受益（改动在服务端一处） |

## 三、方案设计

### 3.1 核心：ref 含 `/` 时先解析为 tree/commit sha 再取树

`git_repo_api.py` `get_repo_tree` 单层分支（L198-203）：

- 若 `ref` 不含 `/` → 保持现状 `git/trees/{ref}` 直连（零回归、少一次请求）；
- 若 `ref` 含 `/` → 走新增 `_ref_to_tree_sha(provider, owner, repo, ref, user)`：
  - **GitCode**：用 `git/commits/{ref}`（same 语义 `GET /git/commits/{sha_or_ref}`）解析 commit → 取 `commit.tree.sha`
    （与 `_github_tree_sha` 同思路；GitCode headers/JSON 结构对齐 GitLab 兼容 API，需实测取 tree sha 字段形态）；
    取不到 sha 时**保序降级**：仍尝试 `git/trees/{ref}` 原样（维持现状行为，不静默变 404 文案）；
  - **GitHub**：复用既有 `_github_tree_sha`（已解析 → 无斜杠 sha，天然修复 `f"/commits/{ref}"` 裸插路径段的问题）；
- 解析成功后统一 `git/trees/{sha}`（sha 无 `/`，彻底免 URL 编码歧义）。

> **为何不用 `quote(ref)` 直改**：含 `/` 的值经路径段编码为 `feat%2Fissue-11` 后，部分网关/代理会
> 在到达应用前解码回 `/` 再切分，编码失效（不可控）。「先解析 sha → 再取树」与 GitHub 既有
> `_github_tree_sha` 兜底一致性最高，且 sha 不含 `/`，从根上消除路径段切分风险。

### 3.2 影响面收敛：其他裸插 ref 的路径段一并处理

| 位置 | 现状 | 处理 |
|------|------|------|
| `git_repo_api.py:202` 单层 tree | `git/trees/{ref}` 裸拼 | 走 3.1 解析 sha；无 `/` 直连保现状 |
| `git_repo_api.py:212` recursive tree | `git/trees/{_tree_sha}`（已解析） | 保持（sha 无斜杠） |
| `git_repo_api.py:236` `_github_tree_sha` | `commits/{ref}` 裸拼 | 复用 3.1：ref 含 `/` 时先解析（GitHub 平台 blame/commit 行为不变） |

其余 `src/api/git_repo_api.py` 端点（branches/commits/contents/issues/labels 等）ref 均走 query
参数或非 ref 路径段，**不在本次修复范围**（contents 已验证正常）。

### 3.3 GitCode tree api 形态说明

- 平台透传走 `_vcs_url` + `_PROVIDER_MODULES[provider].request`（`src/vcs/providers/gitcode.py:7-9` → `_req`），
  URL 构造与 tokens 解析沿用现有链路，无新增网络依赖。
- `git/commits/{ref}` 对含 `/` 的 ref 同样有裸插路径段问题 ⇒ **必须先把 ref 编码为 query
  或确认 GitCode commits 接受 `%2F`**。备选：GitCode `git/commits?ref_name={ref}`（query 参数形态，
  与 branches/contents 同风格，实测优先用 query 形态，避免二次踩坑）。

## 四、关键实现要点

- 单点收敛：新增 `_ref_to_tree_sha(provider, owner, repo, ref, user) -> str`，单层与
  `_github_tree_sha` 共用同一套「取 commit → 取 tree.sha」语义；
- 降级不破：解析失败（网络/JSON 形态不符）→ `log.warning` + 返回原 `ref`（行为等同现状，
  至少不打回 404 装配成 502 的二次混淆）；
- 不改变响应：`get_repo_tree` 的返回体结构、`_vcs_req` 的错误映射均不动；
- 日志：解析成功打 `gitrepo_tree: ref <ref> -> tree <sha>（含 /，先解析）`，便于审计。

## 五、影响面 / 风险

| 项 | 说明 |
|----|------|
| 含 `/` ref 的 tree | 成功后返回正确树（此前 404） |
| 无 `/` ref / 默认分支 | 直连路径不变，零回归（保持测试绿） |
| 平台行为 | GitCode 单层修复；GitHub 递归/树 sha 语义与现状一致 |
| 网络成本 | 仅在含 `/` 的 ref 增加一次 commits 解析请求（低频场景，可接受） |
| 解析失败 | 保序降级原样透传（与现状一致，不新增失败面） |

## 六、测试（测试先行，TDD）

落地文件 `tests/test_git_repo_api.py`（扩展既有 tree 用例组，或新增 `tests/test_git_repo_tree_slash_ref.py`）：

| ID | 场景 | 断言 |
|----|------|------|
| QA-01 | GitCode + ref=`feat/issue-11` 单层 tree | 先调 commits 解析 → 再调 `git/trees/{sha}`；sha 无 `/`；返回 200 |
| QA-02 | GitCode + ref=`main`（无斜杠） | 保持直连 `git/trees/main`，不多一次请求（零回归） |
| QA-03 | GitCode + ref 空 | 沿用 `_default_ref` 解析默认分支，行为不变 |
| QA-04 | commits 解析失败（mock 500/坏结构） | 降级返回原 ref，仍调用 `git/trees/{ref}`（不 502） |
| QA-05 | GitCode + recursive | 保持 400「暂不支持递归」拒绝（不回归） |
| QA-06 | GitHub + ref 含 `/` | 走 `_github_tree_sha` 解析 sha 后取树（commit tree.sha 无斜杠） |
| QA-07 | GitHub + ref 无 `/` | 行为与现状一致（`commits/{ref}` 形态先行） |
| QA-08 | URL 形态断言 | 所有 upstream URL 不含裸 `feat/issue-11` 路径段；query 均 urlencode |

## References

- 反馈来源：`grape-skills#95`（Agent 自动反馈 source=agent）
- `src/api/git_repo_api.py`：`_vcs_url`(133) / `_vcs_req`(149) / `get_repo_tree`(184) / `_default_ref`(216) / `_github_tree_sha`(233)
- `src/vcs/providers/gitcode.py`：`request`(7) 透传链路
- `src/user/repo_manager.py`：`_req`(12) 原生 API 调用
- `tests/test_git_repo_api.py`：tree 既有用例组（L321-348）