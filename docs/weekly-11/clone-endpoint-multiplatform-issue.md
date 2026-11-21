# 需求：clone 端点单点派生（多平台兼容）

状态：需求稿（待评审）
分支：`feat/clone-endpoint-single-source`（基于 `arch-weekly-11@e707851bd`）
设计稿：`docs/weekly-11/clone-endpoint-single-source-design.md`
关联：#3632、#3580、S7/G1、D4/G2、D105

## 1. 背景与现象

worker 的 clone 由调用方（worker 本机 / 云沙箱）执行，grape 只下发"地址 + 令牌"。地址与用户名的派生分散在六处，其中三处写死 `gitcode.com`：

- 非 gitcode 平台（gitee / github / gitlab）的仓，在**云沙箱**与**接管会话**链路上会被当成 gitcode 上的同名仓去 clone，失败信息与"仓库不存在/无权限"不可区分；
- gitee 的平台卡声明 `clone_auth=basic_password`（真实用户名 + 私人令牌当密码），而 CLI 一次性券恒返回 `x-access-token`、沙箱 askpass 恒答 `bot`、repogit 的 `basic_password` 映射为空串——三者都不是 gitee 账号名。

现象在"gitee 仓库在 worker 上拿不到克隆地址"（#3632）修复后仍然存在：**#3632 只修好了普通 worker 引擎取卡 → 拼 URL 这一段**，沙箱与接管两条链、以及用户名这一维度没有覆盖。

## 2. 现状链路（逐跳标注知识来源）

```
入口 A 普通 worker 引擎
  agent.py:3345 ensure_issue_worktree(token=_get_task_token(task))      [来源: DB 任务行/bot token]
  → repogit.py:613 ensure_repo_base
      repo_base_dir  = <workspace>/<repo>/codebase                     [来源: 部署 workspace 根]
      clone_url_for  → parse_repo_key + 平台卡 host                     [来源: 平台卡]
      scheme         → 平台卡 clone_auth                               [来源: 平台卡]
      git fetch 鉴权 → -c http.extraheader=Basic(base64(user:token))    [user 来源: repogit.py:1927 常量表]
  → repogit.py:1379 git worktree add

入口 B 云沙箱 executor
  executor.py:2128   repo_url = https://gitcode.com/{owner/repo}.git   [来源: 代码硬编码]
  → sandbox.configure_repo(repo_url, bot_token, work_branch)           [来源: proc_env BOT_GITCODE_TOKEN]
  → devbox.py:311-341 写 /tmp/_git_askpass.sh（Username→"bot"）       [来源: 代码常量]
       git clone --depth 1 -b <branch> <repo_url> /repo
  push 由沙箱内 grape-cli 走 /api/repos 凭据                          [来源: mgmt 凭据表]
  → 与 clone 不共享地址/用户名派生（双轨）

入口 C 沙箱上下文
  sandbox_context.py:81 repo_url = https://gitcode.com/{repo}.git      [来源: 代码硬编码]

入口 D 接管会话下发
  takeover.py:1195 workspace.clone_url = https://gitcode.com/{repo}.git [来源: 代码硬编码]

入口 E CLI 一次性券
  mgmt/routes/repos.py:3133 → {"username":"x-access-token","clone_url":"https://<card.host>/..."}
                                                                        [username 来源: 代码常量]

入口 F 沙箱 askpass
  devbox.py:328 Username*) echo "bot"                                  [来源: 代码常量]
```

关键事实：`grep -rn "upload-pack|receive-pack|info/refs" src/` = 0 命中——**服务端不做 git 传输代理**，只发参数。

## 3. 已验证缺陷表

| # | 缺陷 | 证据 | 定档 |
|---|---|---|---|
| D1 | 沙箱 executor 的 clone host 写死 gitcode.com | `executor.py:2128`；探针：gitee 仓拼出 `https://gitcode.com/foo/bar.git` | **已实测**（假 token 探针捕获） |
| D2 | 沙箱上下文同样写死 | `sandbox_context.py:81` | 代码核对 |
| D3 | 接管下发同样写死 | `takeover.py:1195` | 代码核对 |
| D4 | CLI 一次性券用户名恒 x-access-token，与 gitee 卡 clone_auth=basic_password 不符 | `repos.py:3133`；探针返回体 `{"provider":"gitee","username":"x-access-token","clone_url":"https://gitee.com/foo/bar.git"}` | **已实测**（clone_url 对、username 不符） |
| D5 | 沙箱 askpass 用户名恒 `bot` | `devbox.py:328` | 代码核对 |
| D6 | repogit 把 basic_password 映射为空用户名 | `repogit.py:1927` `_AUTH_USER_BY_SCHEME` | 代码核对 |
| D7 | `_HOSTS` 常量优先于平台卡，卡里配置的 host 不生效 | `repogit.py:18` 与 `_host_for_provider` 顺序 | 代码核对 |
| D8 | gitee 拒收占位/空用户名；组织仓用 owner 也被拒 | 真机（私有仓 `git ls-remote`）：`x-access-token` / 空用户名 → rc=128 `The token username invalid`；令牌账号名 → rc=0；组织仓 `owner` → rc=128 | **已实测** |

### 3.1 真机验证记录（2026-11，只读）

用真实 gitee 私人令牌对**私有仓**做 `git ls-remote`（只读，不写远端数据）：

```
用户名形态                 个人仓（owner == 令牌账号）   组织仓（owner != 令牌账号）
令牌账号名                 rc=0, 5 refs ✅               rc=0, 4 refs ✅
仓库 owner                 rc=0, 5 refs（同名偶然可用）   rc=128 The token username invalid ❌
x-access-token             rc=128 The token username invalid ❌   rc=128 ❌
（空用户名）               rc=128 ❌                      rc=128 ❌
```

结论（修复口径据此确定）：

1. gitee 的 `basic_password` 用户名**只能是令牌所属账号名**；占位名/空名必被拒。
2. `owner` 兜底只在"个人仓且 owner 与令牌账号同名"时成立，**组织仓/他人仓必然失败** ——
   这就是为什么平台卡必须有 `bot_username`（卡是唯一真源，不要在代码里猜）。
3. 修复前 `repogit._basic_auth` 在只传 provider 时派生不到 repo_key，用户名退化成空串，
   连个人仓都会失败（真机已复现并修复：用户名一次派生后显式下传）。

同一轮只读验证（平台模块与能力位依据）：

```
GET /repos/{o}/{r}                       200（query_token 鉴权可用）
GET /repos/{o}/{r}/issues                200 list[5]
GET /repos/{o}/{r}/pulls                 200 list[5]
GET /repos/{o}/{r}/branches              200
GET /repos/{o}/{r}/contents/             200
GET /repos/{o}/{r}/commits/{ref}/check-runs  200（check_runs/total_count）→ CHECK_RUNS_NATIVE 成立
GET /repos/{o}/{r}/commits/{ref}/status      404 → STATUS_NATIVE=false 成立
```

验证只用了只读端点；未向任何真实仓库写入（未建 issue / 未发评论 / 未 push）。

### 3.2 dev 库复验（2026-11，只读 + 一处纯文本列写入）

从 dev 库 `grape_t_repo_platforms` 读 gitee 行（明文列）→ `clone_endpoint.clone_params`
派生 → 真实 gitee 认证，三段证据分开记：

```
A 库里的卡   host=gitee.com | dialect=github | clone_auth=basic_password
             bot_username=<token-login>（本轮补写；原值为空）
B 派生       5 个托管仓：clone_url = https://gitee.com/<owner>/<repo>.git
             username = <token-login>   clone_auth = basic_password
C 认证       用派生出的用户名 + 令牌做只读 ls-remote → 5/5 rc=0
```

凭据段（同一轮已闭环）：

- 凭据列是 `{kid}:{nonce}:{ciphertext}`（AES-GCM）。dev 环境 `credential_keys` 为空，
  密钥由运行进程的 `JWT_SECRET` 派生（kid=`derived`，与库内既有 gitcode 行同 kid）；
- 按同一派生规则在 dev 服务端生成密文（89 字符）→ 写入 5 个托管仓的 bot 行
  （`micro-svc` 原本缺行，一并补齐；webhook 密钥属 gitee 侧配置，未臆造）；
- 服务端用同一密钥解密 5 条 → **5/5 plain_len=32、sha256 与写入明文一致**。

⇒ "卡（用户名/地址）→ 派生 → 加密凭据写入 → 服务端解密"四段在 dev 环境全部实测通过。

组织仓对照（同一轮）：`owner` 形态 rc=128 `The token username invalid`，
`<token-login>` 形态 rc=0 —— 用户名的取值口径由此坐实。

### 3.3 3 库 4 进程部署验证（本分支实操）

按架构规范在 **3 库 4 进程**部署上验证（mgmt / event / worker / insight 四进程，
三域库分离），不使用单库 edge worker。

```
部署   部署目录切到本分支 commit（原分支 c1fd1854e → 197e15379），
       force checkout 原因：运行期物化的 .grape/skills 与仓库同名文件冲突，
       切换前已把 .grape 打包备份
进程   mgmt / event / insight / worker 逐个 restart
健康   8081 / 8082 / 8083 / 8084 → 全部 200
代码   部署 venv 内 import 到 src/domains/runtime/vcs/clone_endpoint.py（新模块在四进程可见）
日志   除既有飞书 IM 通道 "too many connections" 外，无 import/启动错误
```

**真实 worktree 实证**（用部署代码 + 库里的卡 + 解密出的凭据）：

```
ensure_issue_worktree("gitee:<owner>/<repo>", "coding", 1)  →
  <workspace>/gitee/<owner>/<repo>/issues/1/coding
  .git            存在（worktree 形态，file）
  remote.origin   https://gitee.com/<owner>/<repo>.git      （不是 gitcode.com）
  HEAD            feat/issue-1，两条真实提交
  RESULT          PASS
```

这与 #3632 的症状（目录空、无 `.git`、任务假完成）正好相反。

**任务派发受阻点（与本分支无关）**：

```
worker 认领时每轮打印：claim_by_tag: runtime=23403 has no verified executor
  (probe=failed, worker_version=-), skip claiming
判据（engine/service.py::_has_verified_executor）：credential_id=0 的本机直连 runtime
  只看 installed 与 healthy；该 runtime 是 hermes 引擎，ACP 二次探活失败 → healthy=0
同机其它引擎（codex / opencode 等）healthy=1、probe=ok，且都在 tag 绑定与认领循环内
库内 running=0 / queued=0，所以没有任何 claim 成功的日志——只有被拒的那个 rid 在刷
```

即：**clone 链路已就绪，派发被 dev 的某个引擎健康状态挡住**，属环境运维，不进本需求范围。

### 3.4 相邻缺陷（同批验证暴露）：会话凭据缺失与签发幂等

在 dev 上真跑任务时暴露一条独立缺陷，属于「任务能拿到凭据」这一层，与 clone 多变平台无关但同批修掉：

```
现象   agent 自述「任务级会话凭据未注入」；子进程 environ 无 GRAPE_SESSION_TOKEN，
       而 agent_tasks.env_extra 快照里却有；issues detail / tasks complete / A2A 补发全部 Missing token
根因①  同一 session_key 在入队与派发各签发一次，第二次撞部署库的 UNIQUE(session_key) → 1062，
       异常被吞成 warning「task continues」→ 快照有、进程无（主线的派发路径已改为复用快照，
       落后部署仍会再签一次）
根因②  session_token_rotate / session_token_insert 非幂等：只缩短旧行 expires_at 后裸 INSERT
根因③  任务 env 的 GRAPE_SERVER_URL 取到公网基址，执行节点只能走内网 → 派生 agent 指向不可达地址
修复    原语改幂等 upsert（同 key 就地覆盖、行数恒 1）；签发失败写 agent_tasks.errors 诊断；
        基址改内网
验收    tests/runtime/test_session_token_idempotent.py 四例；1062 计数归零；进程 environ 可读到 token
详见    docs/weekly-11/session-token-idempotent-issue.md
```

## 4. 历史成因（时间线）

| 时间 | 提交 | 引入的内容 |
|---|---|---|
| 2026-09-08 | `ae87c3d35` M3 统一 Git 仓库接口 | `git-credential` 返回 `username: x-access-token`（当时只有 gitcode） |
| 2026-09-26 | `9ada7bb25` BUG-4 sandbox 改用 GIT_ASKPASS | askpass 用户名写死 `bot` |
| 2026-09-26 | `de8078920` sandbox git clone 修复 | executor 拼 `https://gitcode.com/{repo}.git` |
| 2026-09-29 | `4dce9a908` 仓库信息内聚到 SandboxContext | 第二处同样的硬编码 |
| 2026-11-03 | `256d5e586` D4 平台卡驱动 git 传输层 | `_AUTH_USER_BY_SCHEME["basic_password"] = ""`（卡驱动只到 scheme，未到用户名） |
| 2026-11-12 | `d82b2a90b` takeover 提升为一等资源 | 第三处同样的硬编码 |
| 2026-11-15 | `a59e5e91b` 平台卡/登录渠道/机器人身份解耦 | gitee 才正式进卡（gitee.json: clone_auth=basic_password、bot_username 为空） |

归因：clone 的地址与用户名在"单平台（gitcode）时代"写死；gitee 在 2026-11-15 进卡时，这六处没有一处被复查。#3632（2026-11-20）修的是"引擎取卡"，恰好绕开了这六处——所以同一症状会以"沙箱/接管上 clone 错平台"的形式再演一遍。

## 5. 方案分档

### P1 单点派生（建议本轮做）

新增 `vcs/clone_endpoint.py`（clone_host / clone_auth / clone_username / clone_url / clone_params），六个消费点改为向其取参。

- 文件数：8（含 1 新模块 + 1 测试文件）
- 净代码行：约 +220 实现 + +120 测试
- 风险点：四个入口的地址/用户名会变（非 gitcode 平台变正确；gitcode/github/gitlab 保持原值，用断言锁死）；`_HOSTS` 优先级反转
- 代价：需要一次全链路回归（沙箱 + 接管各跑一条真任务）
- 不做的后果：沙箱与接管上 gitee/github/gitlab 永久 clone 不到；CLI 券对 gitee 不可用

### P2 通道能力位（第二阶段，另开）

checks/status 通道、gitlab 的 `/notes`、`/changes`、PR↔Issue 关联解除等"读哪个端点"的差异进卡（`check_runs_path` / `status_path` / `comments_path` / `mr_files_path`），删除 `if p == "github" else gitcode` 三处。

- 文件数：5（4 provider + 1 棘轮用例 + 1 探针用例）
- 净代码行：约 +90 实现 + +120 用例
- 风险点：gitee 的 checks 从"打 gitcode"改为直连 gitee（gitee 有原生 `/commits/{ref}/check-runs`）；gitlab 的 statuses 从"打 gitcode"改为显式 404
- 不做的后果：QA 归因（mergeable_state=unstable）对 gitee/gitlab 永远读不到，且可能读到 gitcode 上同名仓的数据

### P3 传输通道（可选，属新能力）

"worker 只连主控"需要 bundle 或 HTTP 代理（主控代 clone/代 push）。文件数与行数按选型另估；沙箱形态的 clone 目前在沙箱内直连上游，`--depth 1` 单分支，push 另走 grape-cli，双轨本身也需要一并改造。

- 代价：2~3 周量级 + 主控 mirror 磁盘/刷新/限流
- 不做也可：直连形态下的多平台兼容由 P1 成立

## 6. 改动量与风险

| 档 | 文件数 | 净行（实现/测试） | 行为变化面 | 需回归 |
|---|---|---|---|---|
| P1 | 8 | +220 / +120 | 沙箱/接管地址；gitee 用户名；`_HOSTS` 优先级 | 沙箱真任务 1 条、接管 1 条、普通 worker 1 条 |
| P2 | 6 | +90 / +120 | gitee checks 通道、gitlab 评论/文件路径、gitee/gitlab statuses 显式不支持 | 透传面 URL 矩阵（探针） |
| P3 | 未估 | 未估 | 新增传输通道 | 独立设计稿 |

## 7. 能否根治

**能根治（P1）**：
1. "地址来源不唯一"——六处各自拼地址，收口为一个纯函数面后物理上只有一个来源；
2. "用户名与卡声明不符"——用户名按 `clone_auth` 派生，`basic_password` 从卡 `bot_username`/仓库归属取，不再有常量。

**不能根治（物理/部署约束）**：
1. 各平台语义差异（gitee 建 milestone 必填 due_on、评论 id 为 hex、PR head 要裸分支名）——只能靠能力位声明，不能被统一抽象抹平，属 P2 范围；
2. "worker 不出网到上游"——clone 在调用方本机执行是当前设计的物理事实，要改必须新增传输通道（P3），与多平台兼容正交；
3. gitee 用户名到底取 owner 还是卡值，取决于部署里令牌归属账号——代码只能派生到"卡优先、owner 回退"，最终正确性靠部署配置（P1 里以 WARNING 暴露）。

## 8. 验收标准（可直接写成用例）

```python
# A 地址派生
clone_url("gitcode:o/r") == "https://gitcode.com/o/r.git"
clone_url("github:o/r")  == "https://github.com/o/r.git"
clone_url("gitee:o/r")   == "https://gitee.com/o/r.git"
clone_url("gitlab:o/r")  == "https://gitlab.com/o/r.git"
clone_url("o/r")         == "https://gitcode.com/o/r.git"

# B 用户名派生
clone_params("gitcode:o/r")["username"] == "x-access-token"
clone_params("gitlab:o/r")["username"]  == "oauth2"
clone_params("gitee:o/r")["username"]   == "o"          # 卡 bot_username 空 → owner
卡.bot_username = "gitee-bot" → clone_params("gitee:o/r")["username"] == "gitee-bot"

# C 无卡 fail-fast
clone_url("unknown:o/r") 抛 CloneEndpointError(code="platform_not_registered")
repogit.clone_url_for 同场景抛 PlatformCardMissingError（异常类型保持不变）

# D 宿主一致性（六入口同源）
{repogit.clone_url_for, executor, sandbox_context, takeover, git_credential} 对同一 repo_key
产出同一 clone_url

# E 凭据不回显
clone_params 的键集合 == {provider, owner, repo, clone_url, clone_auth, username}
```

## 9. 边界（不做）

1. 不做传输代理（P3）；不改沙箱 clone 深度与 push 路径；
2. 不动 `/api/git-repo` 的通道端点（P2 另开）；
3. 不新增平台卡 schema 键（复用既有 `bot_username`）；
4. 不改平台卡键集与漂移门禁的既有断言（`PUBLIC_PRIMITIVES` / `CONTRACT_METHODS` 不动）。

## 10. 关联

- #3632：平台卡跨域契约（本需求的前置；已合入 arch-weekly-11）
- #3580：空列投毒（表卡 → 卡 raw 的省略语义，与本需求无直接改动面）
- S7/G1：clone URL 拒绝静默回退（本需求保持 fail-fast，不回退 gitcode.com）
- D4/G2：按平台卡取 clone 鉴权形态（本需求把"用户名"补齐到同一能力位）
- D105：平台卡驱动（本需求是其"端点/身份"维度的补完）

## 11. 实现状态（滚动）

### 第一刀：clone 端点单点派生（已完成）

- `src/domains/runtime/vcs/clone_endpoint.py`（clone_host / clone_auth / clone_username / clone_url / clone_params / CloneEndpointError）
- 六个消费点收口：repogit、executor、sandbox_context、takeover、git-credential、沙箱 askpass
- `_HOSTS` 降为无卡兜底；异常语义保持 `PlatformCardMissingError`
- 用例 `tests/runtime/test_clone_endpoint_multiplatform.py`（A/B/C/D/E 全覆盖）

### 第二刀：通道能力位 + 沙箱 token 键（已完成）

| 项 | 内容 | 位置 |
|---|---|---|
| checks 通道 | `CHECK_RUNS_NATIVE` / `CHECK_RUNS_VIA_ACTIONS`，无位即 404 `checks_unavailable` | `git_repo_api.list_check_runs` |
| gitee checks | 走**原生** `/commits/{ref}/check-runs`（平台 swagger 实测存在），不再打 gitcode Actions | providers/gitee.py |
| statuses 通道 | `STATUS_NATIVE` / `STATUS_RESOLVE_SHA`，无位即 404 `statuses_unavailable` 且**零上游调用** | `git_repo_api.list_commit_statuses` |
| PR↔Issue 关联 | `PR_ISSUE_LINK_NATIVE`（gitcode=true，其余 false → 幂等 no-op），判定不再比较平台名 | `git_repo_api.unlink_pull_issue` |
| gitlab 子路径 | 模块 `rewrite_path()` 钩子：`/comments→/notes`、`/files→/changes`、评论带 MR 号 | providers/gitlab.py + `_vcs_url` |
| 评论路径形态 | `PR_COMMENT_FLAT_PATH`（gitlab=false → 带 MR 号） | update/delete pull comment |
| sha 解析 | `_commit_sha_or_404(provider, …)` 参数化（原 gitcode 硬编码） | `git_repo_api` |
| 沙箱 token 键 | `bot_token_env_name` / `bot_token_from_env`：BOT_GITEE_TOKEN 等按卡派生；cloud_worker 注入、executor/sandbox_context 读取同源 | clone_endpoint + 三处消费 |
| bwrap 白名单 | `BOT_*_TOKEN` / `USER_*_TOKEN` 命名约定放行（不再枚举平台键） | backends/bwrap.py |
| 场景 env 保留名 | `BOT_` / `USER_` 前缀进入保留命名空间 | kernel/scene_custom_env.py |

验证（每次 pytest 单文件）：新增 `tests/api/test_channel_capability_bits.py` 15 passed；
`test_clone_endpoint_multiplatform.py` 20 passed；回归见 commit message。

### 仍未做

- P3 传输通道（bundle / HTTP 代理）——属新能力，另开设计。
- gitee 的 clone 真机 401 验证（需真实令牌）。
- 棘轮扩面（变量名 `p`/`in`/`not in`/else 兜底形态）——存量 7 处需先登记基线，未在本分支动。
