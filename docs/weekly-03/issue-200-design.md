# Issue #200 设计方案：maybe_refresh_project_info 未传入 bot token 导致私有仓库基线知识生成降级

## 1. 问题概述

四个问题，同一根因（token 在 git fetch 路径缺失/不匹配）：

### 问题 1（Issue #200 主体）

- **位置**：`src/api/event_pipeline.py` 第 914 行
- **现象**：`maybe_refresh_project_info` 调用时只传 `repo_key`，未传 `token` → `ensure_repo_base` 的 `git fetch` 403 → bare mirror 建不起来 → `degraded=True`, `note="analysis worktree prepare failed"`
- **影响**：4 个私有仓库基线知识完全缺失（只有 `.metadata.json`，缺少 `project-graph.md` 和 `project-capabilities.md`）
- **受影响仓库**：signway/fluxgate、songspace/grape-admin、XOcto/grape-cli、Flycoder-web/hello-calculator
- **引入时间**：commit 97bebf9b（2026-08-11）

### 问题 2（附带修复）

- **位置**：`src/project_info/builder.py` 第 221-222 行
- **现象**：worktree `git fetch origin` 用的是 builder.py 自己的 `_run`（不注入 token header），私有仓库 fetch 403 静默失败
- **影响**：bare mirror 已存在时，worktree 代码不刷新，codegraph 探索旧代码，基线知识内容过时但 `degraded=False`
- **触发条件**：bare mirror 已存在 + 私有仓库 + 远程有新提交

### 问题 3（深度检查发现 — GitHub 仓库 token 类型不匹配）

- **位置**：`src/api/event_pipeline.py` 第 916 行（问题 1 修复引入）
- **现象**：直接用 `get_gitcode_token` 获取 token，未做 provider 分发。GitHub 仓库（`github:owner/repo`）会拿到 GitCode token，对 GitHub git fetch 和 REST API 均无效
- **影响**：GitHub 私有仓库的 project-info 刷新会失败。当前公开仓库不受影响，但将来接入 GitHub 私有仓库会出问题
- **对比**：`vod_handler_async.py` 第274-281行、`build_project_info` 第296-302行均有 provider 分发逻辑

### 问题 4（深度检查发现 — bare mirror 已存在时不刷新 refs）

- **位置**：`src/project_info/builder.py` 第 196 行
- **现象**：`if not os.path.isdir(base):` 条件导致 bare mirror 已存在时跳过 `ensure_repo_base`，不刷新 bare mirror 的 refs。worktree add 从过时的 refs checkout
- **影响**：bare mirror 的 refs 长期不刷新可能导致 worktree add 拿到错误分支，实际影响较小（worktree add 有 origin/main、origin/master 兜底，且 fetch 后 worktree 会更新）
- **修复方式**：去掉 `if not os.path.isdir(base)` 条件，每次都调 `ensure_repo_base`（内部幂等：已存在只 fetch）

## 2. 调用链路

```
event_pipeline.py run_pipeline()
  └─ 第914行: Thread(target=maybe_refresh_project_info, args=(repo_key, Repo(repo_key).bot_token))
      └─ builder.py maybe_refresh_project_info(repo_key, token)
          └─ build_project_info(repo_key, token, ...)
              ├─ Repo(repo_key).bot_token / .api_base  ← token 优先，空则兜底
              ├─ _ensure_analysis_worktree(repo_key, token)
              │    ├─ ensure_repo_base(repo_key, token)  ← 每次都刷新（修复问题4）
              │    │    └─ repogit.py _run_git([...], token=token)
              │    │         └─ if token: → 注入 http.extraheader
              │    └─ _run_git(["-C", work, "fetch", "origin", br], token=token)  ← 带 token（修复问题2）
              └─ if not work → degraded=True
```

## 3. 改动点

### 改动点 1：event_pipeline.py — 传入 token（通过 Repo 领域对象）

```python
# --- 修改前 ---
try:
    from src.project_info.builder import maybe_refresh_project_info
    threading.Thread(target=maybe_refresh_project_info, args=(repo_key,), daemon=True).start()
except Exception as e:
    log.warning(f"Pipeline: project-info refresh trigger failed: {e}")

# --- 修改后 ---
try:
    from src.project_info.builder import maybe_refresh_project_info
    from src.repo import Repo
    _pi_token = Repo(repo_key).bot_token
    threading.Thread(target=maybe_refresh_project_info, args=(repo_key, _pi_token), daemon=True).start()
except Exception as e:
    log.warning(f"Pipeline: project-info refresh trigger failed: {e}")
```

设计说明：
- `Repo(repo_key).bot_token` 按 provider 自动分发（github → get_github_token，其他 → get_gitcode_token）
- 在主线程同步调用（查 DB，无网络开销），获取后传给子线程
- 避免子线程内 DB 锁竞争

### 改动点 2：builder.py _ensure_analysis_worktree — bare mirror 每次刷新

```python
# --- 修改前 ---
    if not os.path.isdir(base):
        try:
            from src.runtime.repogit import ensure_repo_base
            base = ensure_repo_base(repo_key, token or "")
        except Exception as e:
            log.warning(f"project-info ensure_repo_base failed {repo_key}: {e}")

# --- 修改后 ---
    # Issue #200: 每次都刷新 bare mirror refs（ensure_repo_base 内部幂等：已存在只 fetch）
    try:
        from src.runtime.repogit import ensure_repo_base
        base = ensure_repo_base(repo_key, token or "")
    except Exception as e:
        log.warning(f"project-info ensure_repo_base failed {repo_key}: {e}")
```

设计说明：
- 去掉 `if not os.path.isdir(base)` 条件
- `ensure_repo_base` 内部幂等：bare mirror 已存在时只执行 `git fetch --prune`，不重建
- 保证 bare mirror refs 每次刷新到最新，worktree add 能 checkout 到最新代码

### 改动点 3：builder.py _ensure_analysis_worktree — worktree fetch 带 token + 失败日志

```python
# --- 修改前 ---
        # 刷新 main/master 到最新
        for br in ("origin/main", "origin/master"):
            _run(["git", "-C", work, "fetch", "origin", br], timeout=300)

# --- 修改后 ---
        # 刷新 main/master 到最新（私有仓库需 token 认证；Issue #200）
        from src.runtime.repogit import _run_git
        for br in ("origin/main", "origin/master"):
            _r = _run_git(["-C", work, "fetch", "origin", br], token=token, timeout=300)
            if _r and _r.returncode != 0:
                log.warning(f"project-info worktree fetch {br} failed {repo_key}: {(_r.stderr or '')[:200]}")
```

设计说明：
- 复用 `repogit._run_git`（已实现 token → `http.extraheader` 注入）
- 新增失败日志：fetch 失败时 `log.warning`，但不 return None（worktree 已有代码，codegraph 探索旧代码总比完全不生成好）

### 改动点 4：builder.py build_project_info / should_refresh — 通过 Repo 获取 token 和 api_base

```python
# --- 修改后 ---
from src.repo import Repo
_repo = Repo(repo_key)
tok = token or _repo.bot_token
_api_base = _repo.api_base
```

## 4. Repo 领域对象抽象

### 4.1 背景

修复过程中发现，"按 provider 分发获取仓库 bot token" 的代码模式重复出现在 12 个调用点，分布在 6 个文件中。每次都是同样的 4-6 行 if/else 分发逻辑。抽象为统一的仓库领域对象，消除重复，便于后续扩展。

### 4.2 设计模式

领域对象模式（Domain Object Pattern）：以仓库为维度建一个 Repo 类，封装仓库维度的行为和数据。

选择理由：
- 仓库是一个明确的领域概念，有多个行为（获取 token、获取 API 地址、获取 clone URL 等）挂在它身上
- 比简单工厂函数更符合面向对象设计：行为和数据绑定在领域对象上
- 比策略模式更轻量：不需要多态对象和继承体系，只是一个领域对象的方法分发
- 放在独立模块 src/repo.py，不污染 repo_key.py（纯标识转换工具）的职责

已知局限（后续优化方向）：
- Repo 对象无缓存，每次 `Repo(repo_key)` 新建实例，每次访问 `.bot_token` 都查 DB
- 当前场景下与简单工厂函数价值相当，未发挥领域对象的状态持有优势
- 后续可演进为请求级单例（token 查一次 DB 缓存在实例上），但需考虑生命周期管理
- repo.py 位置后续可考虑移到 src/vcs/ 下，与 VcsApiClient/VcsBackend 对齐

### 4.3 实现：src/repo.py

```python
"""仓库领域对象：封装仓库维度的行为和数据。

与 repo_key.py 的区别：
- repo_key.py：纯标识转换工具（to_repo_key / parse_repo_key / split_owner_repo）
- repo.py：领域对象，封装业务行为（bot_token / api_base / clone_url）
"""

from src.repo_key import parse_repo_key

_HOSTS = {"gitcode": "gitcode.com", "github": "github.com"}
_API_BASES = {"gitcode": "https://gitcode.com/api/v5", "github": "https://api.github.com"}


class Repo:
    """仓库领域对象。"""

    def __init__(self, repo_key: str):
        self.repo_key = repo_key
        self.provider, self.owner, self.name = parse_repo_key(repo_key)

    @property
    def api_base(self) -> str:
        """返回该仓库对应平台的 API 基础 URL。"""
        return _API_BASES.get(self.provider, _API_BASES["gitcode"])

    @property
    def clone_url(self) -> str:
        """返回仓库的 git clone URL。"""
        host = _HOSTS.get(self.provider, _HOSTS["gitcode"])
        return f"https://{host}/{self.owner}/{self.name}.git"

    @property
    def bot_token(self) -> str:
        """按 provider 分发获取仓库 bot token（含三级兜底）。

        github → get_github_token（managed_repos.bot_github_token → user_accounts → 全局）
        其他   → get_gitcode_token（devops_agent_session → managed_repos.bot_gitcode_token → 全局）
        """
        if self.provider == "github":
            from src.user.db import get_github_token
            return get_github_token(repo_key=self.repo_key) or ""
        else:
            from src.user.db import get_gitcode_token
            return get_gitcode_token(repo_key=self.repo_key) or ""
```

设计说明：
- `__init__` 解析 repo_key，存储 provider/owner/name，避免重复解析
- `bot_token` 用 @property，调用方 `Repo(repo_key).bot_token`，语义清晰
- 延迟 import（get_github_token/get_gitcode_token 在 property 内部 import），不引入模块级 DB 依赖
- `api_base` 和 `clone_url` 是本次附带抽象的属性，因为 builder.py 的两个调用点同时取 token 和 api_base
- `_HOSTS` 和 `_API_BASES` 从 repogit.py 对齐，后续可合并到 repo.py 作为单一数据源
- get_gitcode_token/get_github_token 底层实现完全不变，三级兜底逻辑原封不动

### 4.4 替换 12 处重复的 if/else provider 分发

| 文件 | 处数 | 说明 |
|------|------|------|
| event_pipeline.py | 2 | issue_opened 刷新 + workdir 清理 |
| builder.py | 2 | build_project_info + should_refresh（同时取 api_base） |
| vod_handler_async.py | 1 | VOD 执行 token 兜底 |
| onboarding.py | 2 | onboarding 任务创建 + 重试（清理死代码 parse_repo_key） |
| events.py | 2 | 对话执行（清理 dead import get_gitcode_token） |
| chat.py | 3 | Issue 评论同步 + token 兜底 |

### 4.5 不替换的调用点

| 文件 | 处 | 原因 |
|------|-----|------|
| repos.py | ~20 | API 请求分发（_github_req / _req），不是 token 获取 |
| tasks.py | 1 | issue 创建分发（_post_github_issue / _post_gitcode_issue） |
| scenes.py | 2 | API 请求分发 |
| vcs/api_client.py | ~7 | VCS 抽象层本身的 if/else，是合理的多态分发 |
| 其他 | 若干 | API 请求分发，不属于 Repo.bot_token 抽象范围 |

## 5. 不改动的部分

| 位置 | 原因 |
|------|------|
| `get_gitcode_token` / `get_github_token`（user/db.py） | 底层实现，保持不变，Repo 内部调用 |
| `maybe_refresh_project_info` 函数签名和内部逻辑 | 已支持 token 参数，只是调用方没传 |
| `_run` 函数（builder.py 第 182 行） | 其他调用点仍在使用（worktree add、codegraph init/explore/files/callees） |
| `ensure_repo_base` 函数内部逻辑 | 已幂等：已存在只 fetch，不重建 |
| `repo_key.py` 所有函数 | 纯标识转换工具，职责不变 |
| `repogit.py` 的 `_HOSTS` | 暂不合并到 repo.py，避免 repogit.py 改动扩大范围；后续可统一 |

## 6. 影响分析

### 公开仓库（20 个，GitCode）

- `Repo(repo_key).bot_token` 可能返回全局 bot token 或空
- `_run_git` 中 `if token:` 为 True 时注入 header，公开仓库也能正常 fetch（多带 header 无害）
- **无回归**

### 私有仓库（4 个，GitCode）

- `Repo(repo_key).bot_token` 返回 bot token
- `ensure_repo_base` 的 git fetch 携带认证 → 成功
- bare mirror refs 每次刷新到最新
- worktree fetch 携带认证 → 成功
- 基线生成完整 3 条 → `degraded=False`

### GitHub 仓库

- `Repo(repo_key).bot_token` 返回正确的 GitHub token（provider 分发）
- 不会误传 GitCode token 给 GitHub API

### 已降级的 4 个仓库

- `should_refresh` 检测到 `degraded=True` 且 codegraph 可用 → 返回 True
- 下次 `issue_opened` 触发时自动重新生成
- 重新生成后 `degraded=False`

## 7. 单元测试

### 新增 tests/test_repo.py（12 个测试）

- TestRepoInit: gitcode/github/no-prefix/empty repo_key 解析
- TestRepoApiBase: gitcode/github/unknown provider fallback
- TestRepoCloneUrl: gitcode/github
- TestRepoBotToken: github 调 get_github_token、gitcode 调 get_gitcode_token、无前缀默认 gitcode、空/None token 返回空字符串

### 新增 tests/test_project_info_builder.py（8 个测试，3 个新测试类）

- TestEnsureRepoBaseAlwaysRefresh: bare mirror 已存在时也调 ensure_repo_base、token 正确传递
- TestWorktreeFetchWithToken: fetch 使用 _run_git、fetch 失败记日志但不中断
- TestBuildProjectInfoTokenPassing: 显式 token 优先于兜底、空 token 走 Repo 兜底

## 8. 验收标准

- [x] event_pipeline.py 中 `maybe_refresh_project_info` 调用传入 token 参数
- [x] token 获取通过 Repo 领域对象做 provider 分发
- [x] builder.py 中 bare mirror 每次都调 ensure_repo_base 刷新 refs
- [x] builder.py 中 worktree fetch 使用 `_run_git` 并传入 token
- [x] builder.py 中 worktree fetch 失败有日志
- [x] 新增 src/repo.py，Repo 类含 bot_token/api_base/clone_url
- [x] 12 处调用点全部替换为 Repo 调用
- [x] 清理 onboarding.py 死代码 parse_repo_key
- [x] 清理 events.py dead import get_gitcode_token
- [x] 新增 20 个单元测试（test_repo.py 12 个 + test_project_info_builder.py 8 个）
- [x] 语法检查通过
- [x] 单元测试通过（28 passed）
- [ ] 私有仓库触发 VOD 后生成完整 3 条 baseline（需测试环境验证）
- [ ] 公开仓库行为不受影响（需测试环境验证）
- [ ] 已降级的 4 个仓库重新生成后 `degraded=False`（需测试环境验证）

## 9. 验证方式

1. 语法检查：7 个文件 ast.parse 通过
2. 单元测试：`python3 -m pytest tests/test_repo.py tests/test_project_info_builder.py -v` → 28 passed
3. grep 确认无残留：`grep -rn 'if _provider.*==.*"github"' src/ --include='*.py'` 仅剩 repos.py/tasks.py/scenes.py 等非 token 分发
4. grep 确认新用法：`grep -rn 'Repo(.*).bot_token' src/ --include='*.py'` 共 12 处
5. 测试环境验证：触发 issue_opened，确认 degraded=False，3 条基线完整

## 10. 提交记录

基线：grape-dev/weekly-03-hc c811f722（!250 merge multitask）
分支：feat/issue-200-bot-token-baseline
