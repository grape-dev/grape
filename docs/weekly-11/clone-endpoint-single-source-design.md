# clone 端点单点派生（多平台兼容）设计方案

状态：设计稿（待评审）
关联：#3632（平台卡跨域契约）、#3580（空列投毒）、S7/G1（clone URL 拒绝静默回退）、D4/G2（按卡取 clone 鉴权形态）
分支：`feat/clone-endpoint-single-source`（基于 `arch-weekly-11@e707851bd`）

## 0. 结论先行

1. **要做**：把"clone 地址 + 鉴权形态 + 用户名"收口成一个纯函数面 `vcs/clone_endpoint.py`，四处 clone 入口（普通 worker / 云沙箱 executor / 沙箱上下文 / 接管下发）与两处凭据下发（CLI 一次券 / 沙箱 askpass）全部改为向它取参，删除 3 处硬编码 `https://gitcode.com/...` 与 2 套用户名常量。
2. **不做**（本轮）：不改 git 传输形态（不引入 bundle / HTTP 代理）、不动 `/api/git-repo` 的通道端点（checks/status 错平台是独立一步）、不新增平台卡 schema 键（`clone_username` 复用既有 `bot_username`，避免撞卡键集门禁）。
3. **收益**：新增平台 = 只加一张平台卡，clone 四处自动生效；沙箱/接管不再把 gitee/github/gitlab 仓当 gitcode 仓去 clone。
4. **行为变化点**（必须评审确认）：gitee 的 clone 用户名从 `""`/`x-access-token` 变为**令牌账号名**（解析失败才回落卡 `bot_username` → 仓库 owner）；沙箱与接管对非 gitcode 仓的 clone URL 从 `gitcode.com` 变为卡 host。前者是修复但需真机验证，后者是 bug 修复。

## 1. 现状事实（逐条可复验）

### 1.1 四个 clone 入口，地址来源不一致

| 入口 | 位置 | 地址来源 | 判定 |
|---|---|---|---|
| 普通 worker 引擎 | `src/domains/runtime/engine/repogit.py:535` `clone_url_for` | 平台卡 `host`（`_HOSTS` 优先） | 已卡驱动 |
| 云沙箱 executor | `src/domains/runtime/agents/executor.py:2128` | 硬编码 `https://gitcode.com/{owner/repo}.git` | 错平台 |
| 云沙箱上下文 | `src/domains/runtime/agents/sandbox/sandbox_context.py:81` | 同上硬编码 | 错平台 |
| 接管会话下发 | `src/domains/runtime/engine/takeover.py:1195` | 同上硬编码（`clone_url` 字段） | 错平台 |

### 1.2 两套用户名常量，都不满足 gitee 的 `basic_password`

```
src/domains/mgmt/routes/repos.py:3147   "username": "x-access-token"      # CLI 一次性券，恒常量
src/domains/runtime/agents/sandbox/backends/devbox.py:328   Username*) echo "bot"   # 沙箱 askpass
src/domains/runtime/engine/repogit.py:1927  _AUTH_USER_BY_SCHEME["basic_password"] = ""   # Basic ":<token>"
```

平台卡 `configs/platforms/gitee.json` 声明 `clone_auth=basic_password`，语义是"真实用户名 + 私人令牌当密码"。上述三种用户名都不是 gitee 账号名。

实测（本机探针，`/api/repos/{owner}/{repo}/git-credential`，假 token）：

```json
{"provider":"gitee","username":"x-access-token","password":"***","clone_url":"https://gitee.com/foo/bar.git","expires_in":600}
```

`clone_url` 正确，`username` 与卡声明不符。

### 1.3 传输面：**没有**服务端代 clone/代 push

```
grep -rn "upload-pack|receive-pack|info/refs" src/  → 0 命中
```
`git-credential` / `workspace` 只下发参数（一次性券 + 服务器本地路径），clone/fetch/push 全部在调用方本机执行、直连平台域名。

### 1.4 沙箱侧 clone 与 push 是两套凭据

`devbox.py:311-341` 在沙箱内 `git clone --depth 1 <repo_url>`（askpass 常量用户名 + `$GIT_BOT_TOKEN`）；push 由沙箱内的 grape-cli 走 `/api/repos` 凭据（`devbox.py:276 _configure_grape_cli`）。两条路不共享地址/用户名派生。

## 2. 根因

1. **平台知识只驱动了"字段翻译"，没驱动"端点/身份"**：`translate_inbound` 已卡驱动，但 host / 鉴权形态 / 用户名 / 通道端点仍是调用点各自的常量。
2. **没有"clone 三要素"这个抽象**：每个入口自己拼 `https://<host>/<owner>/<repo>.git`，于是硬编码了三份；用户名同理。
3. **`clone_auth` 只描述"用什么方式"，不描述"以谁的身份"**：卡里 `bot_username` 是身份真源，但 clone 链没向它要。
4. **门禁覆盖不到**：`test_guard_no_new_platform_branches` 只禁 gitee 字面量分支；`test_no_new_binary_platform_enumerations` 只认"同行两个平台字面量"；`test_no_new_single_platform_provider_compares` 只认变量名 `provider`。硬编码 `f"https://gitcode.com/{...}"` 没有平台名比较，三道全不命中。

## 3. 设计

### 3.1 分层（职责边界）

```
L0 平台卡      唯一真源：host / api_base / clone_auth / bot_username / webhook_dialect / 端点模板
               宿存 mgmt 域 → worker 经 GET /api/runtime/platform-cards 取（#3632 已通）
L1 方言层      providers/<p>.py / card_module：字段翻译 + 能力位；不决定"去哪"
L2 适配点      vcs/clone_endpoint.py：clone_url / clone_auth / clone_username / clone_params
               vcs 域内、纯函数、无 IO、不碰令牌
L3 消费面      repogit / executor / sandbox_context / takeover / repos.git-credential / devbox askpass
               只调 L2，不再持有 host 或用户名常量
```

### 3.2 新增模块 `src/domains/runtime/vcs/clone_endpoint.py`

```python
CloneEndpointError(provider, detail, code="platform_not_registered")

clone_host(provider) -> str                 # 卡 host；无卡/空 host → fail-fast，不猜域名
clone_auth(provider) -> str                 # 卡 clone_auth，缺省 x_access_token
clone_username(provider, repo_key="") -> str
clone_url(repo_key) -> str                  # https://<card.host>/<owner>/<repo>.git
clone_params(repo_key) -> dict              # provider/owner/repo/clone_url/clone_auth/username
```

`clone_username` 派生顺序（关键设计点；2026-11-21 修正为两层）：

```
1) clone_auth = basic_password  → 有令牌：问平台「这枚令牌是谁的」→ 令牌账号名（token_owner）
                                  无令牌 / 解析失败：卡 bot_username → 仓库 owner
2) clone_auth = x_access_token  → "x-access-token"（gitcode/github 现形态）
3) clone_auth = oauth2          → "oauth2"（gitlab 现形态）
```

为什么 `basic_password` 不能给常量：该形态语义是"真实用户名 + 令牌当密码"，占位用户名在 gitee 上不是账号，凭据必然被拒。

为什么"按卡派生"后来又改了一次：卡上的 `bot_username` 是人工填的——填成组织名、填成 grape 账号名、令牌轮换后没同步，症状都是 403，而令牌本身有效。同一症状三种成因，运维只能猜。真实账号名只有一个权威来源：平台按令牌回答（`verify_token` → `login`）。所以第一层改为**令牌身份解析**（`src/domains/runtime/vcs/clone_identity.py`，`84ac4a1d3`）：

```
username_for_token(provider, token, repo_key) -> (username, source)
  只对 basic_password 出网；按令牌指纹（sha256 前缀）缓存，TTL 默认 3600s，
  同 key 并发用 per-key 锁收敛（single-flight）；失败不缓存
  source: token_owner / card_bot_username / owner_fallback / scheme_const / missing
```

解析失败**不抛**：回落第 1) 条的后两层，`source` 记日志——"谁给的用户名"必须可观测，否则又回到"改了没用"的排查。卡 `bot_username` 由此降级为**回落值**；保存 bot token 时平台侧还会按令牌回填它（`mgmt/admin/repo_platform_routes.py`）。

### 3.3 消费点改造（一次性对齐）

```
repogit.clone_url_for          → 委托 clone_endpoint.clone_url，异常转译回
                                 PlatformCardMissingError（agent 按该类型阻断任务，类型一变会退回
                                 "一条 WARNING 就放行"的旧链）
repogit._host_for_provider     → 卡优先，_HOSTS 降为无卡兜底（现为 _HOSTS 优先：卡里配的内网镜像 host 不生效）
repogit._basic_auth            → 新增 provider 参数，用户名问 clone_username；provider 为空沿用旧表
                                 （本地 git 调用点零行为变化）
repogit.ensure_repo_base       → 传 provider 给 _run_git / _run_git_retry（唯一 clone/fetch 上游点）
executor.py:2128               → clone_url(repo_key)（REPO_FULL_NAME 即带前缀 repo_key）
sandbox_context.py:81          → clone_url(repo_full_name or task.repo_key)
takeover.py:1195               → clone_url(repo_key)
repos.py:3147                  → username = clone_username(p, rk)
devbox._clone_repo             → askpass 脚本 Username 读 $GIT_BOT_USERNAME，configure_repo 传入
                                 clone_username（cloud_base + devbox + cubesandbox 签名加可选参数）
```

### 3.4 异常与失败语义（保持既有口径）

- 无卡 → `CloneEndpointError(code=platform_not_registered)`；repogit 边界转译为 `PlatformCardMissingError`，agent 侧继续显式阻断（#3632 语义不变）。
- 取卡通道不可用（主控读表失败 / 超时 / 5xx）→ `code=platform_card_contract_unavailable`，与「平台未注册」**分码**；repogit 把 `code` 透传给 `PlatformCardMissingError`。默认超时 10s，仅传输类失败重试 1 次（详见 `git-credential-token-plane-revision.md` §9）。
- 派生失败绝**不**回退 `gitcode.com`：静默回退会把 gitee 仓伪装成 gitcode 仓，是本次要消灭的形态。
- 用户名派生失败（basic_password 且 owner 解析不出）→ 返回空串 + WARNING，不阻断 git（让上游 401 带原文暴露），避免本地把网络/权限问题伪装成配置错误。

## 4. 验收标准（可直接写成用例）

```python
# A. 四平台 + 无前缀地址派生
assert clone_url("gitcode:o/r") == "https://gitcode.com/o/r.git"
assert clone_url("github:o/r")  == "https://github.com/o/r.git"
assert clone_url("gitee:o/r")   == "https://gitee.com/o/r.git"
assert clone_url("gitlab:o/r")  == "https://gitlab.com/o/r.git"
assert clone_url("o/r")         == "https://gitcode.com/o/r.git"   # 无前缀默认平台不变

# B. 用户名按卡派生（不再常量）
assert clone_params("gitcode:o/r")["username"] == "x-access-token"
assert clone_params("gitlab:o/r")["username"]  == "oauth2"
# gitee：卡 bot_username 为空 → owner；配了 bot_username → 取卡值
assert clone_params("gitee:o/r")["username"] == "o"
monkeypatch(卡.bot_username="gitee-bot"); assert clone_params("gitee:o/r")["username"] == "gitee-bot"

# B2. gitee 有令牌：令牌账号名优先于卡值（source=token_owner）；解析失败回落卡值/owner
assert username_for_token("gitee", "<token>", "gitee:o/r")[1] == "token_owner"
assert username_for_token("gitee", "", "gitee:o/r")[1] in ("card_bot_username", "owner_fallback")
# 其余形态（x_access_token / oauth2）是常量用户名，绝不出网

# C. 无卡 fail-fast，不静默 gitcode
pytest.raises(CloneEndpointError) / pytest.raises(PlatformCardMissingError) 各测一次

# D. 宿主一致性（跨入口同源）——四个入口同一 repo_key 得到同一 URL
repogit.clone_url_for(k) == executor_url(k) == sandbox_context_url(k) \
    == takeover_bundle(k)["workspace"]["clone_url"] == git_credential(k)["clone_url"]

# E. 凭据不回显：clone_params 结构里不含 token/password
```

门禁用例（第二阶段随实现一起提交）：参数化 `4 平台 × clone 入口` 断言"实际上游 host == 卡 host"，把"跨平台代打"变成红项。

## 5. 改动清单与规模

| # | 文件 | 内容 | 净行数 |
|---|---|---|---|
| 1 | `src/domains/runtime/vcs/clone_endpoint.py`（新） | 五个纯函数 + 异常 | +150 |
| 2 | `src/domains/runtime/engine/repogit.py` | 委托 + provider 透传 + _HOSTS 降级 | ±35 |
| 3 | `src/domains/runtime/agents/executor.py` | 地址 + 传入 username | ±6 |
| 4 | `src/domains/runtime/agents/sandbox/sandbox_context.py` | 地址 | ±4 |
| 5 | `src/domains/runtime/engine/takeover.py` | 地址 | ±3 |
| 6 | `src/domains/mgmt/routes/repos.py` | git-credential username | ±2 |
| 7 | `sandbox/backends/{cloud_base,devbox,cubesandbox}.py` | configure_repo 加 username + askpass 读 env | ±20 |
| 8 | `tests/runtime/test_clone_endpoint_multiplatform.py`（新） | 验收 A~E | +120 |

合计约 8 文件、净 +340 行（其中测试 120）。

## 6. 风险与回滚

| 风险 | 说明 | 处置 |
|---|---|---|
| `_HOSTS` 优先级改变 | 若某部署在卡里配了内网镜像 host，此前被 `_HOSTS` 覆盖、现在生效 | 视为修复，部署说明里点明；回滚=恢复 `_HOSTS` 优先 |
| gitee 用户名变化 | 由 `""`/`x-access-token` 变为 owner 或卡 `bot_username` | 真机首跑验证；不符则卡里填 `bot_username`，代码不动 |
| 沙箱 URL 变化 | 非 gitcode 仓由 `gitcode.com` 变正确 host | 修复；沙箱 clone 仍直连上游（部署属性，见 §7） |
| gitcode/github/gitlab 零变化 | host 与首用户名与现状一致 | 用 A/B 断言锁死（用例 B 前三行） |

回滚粒度：单 commit 回滚即可（新模块无调用方即死代码）。

## 7. 边界（不做）

1. **不做传输代理**：worker 直连上游是部署事实；要"只连主控"是新增通道（bundle / HTTP 代理），与多平台兼容正交。
2. **不动 `/api/git-repo` 通道端点**：checks/status 对 gitee/gitlab 打到 gitcode、gitlab 评论未映 `/notes`，属"通道能力位"第二阶段。
3. **不新增卡键**：本轮 `clone_username` 复用既有 `bot_username`；若将来需要独立键，走卡 schema 变更 + 键集门禁同步。
4. **不改沙箱 clone 深度/策略**（`--depth 1` 保持），不改 push 路径（grape-cli）。

## 8. 落地顺序

```
步骤 1  新增 clone_endpoint.py + 用例 A/B/C（红→绿：改前 gitee username 不是 owner/卡值）
步骤 2  repogit 委托 + 异常转译，跑既有 repogit 相关单测（每文件独立跑）
步骤 3  executor / sandbox_context / takeover / repos 四处改址 + 用例 D（宿主机一致性）
步骤 4  沙箱 configure_repo 传 username + askpass 读 env
步骤 5  （第二阶段，另开）通道能力位：check_runs_path / status_path / comments_path
步骤 6  （第二阶段）棘轮扩面 + host 守恒参数化用例
```

每步独立可验、可单独回滚；步骤 1~3 是"多平台 clone 成立"的最小闭环。

## 9. 第二刀实现记录（同一分支）

### 9.1 通道能力位表

```
位                          语义                                          声明方
CHECK_RUNS_NATIVE           原生 GET /commits/{ref}/check-runs             github / gitee
CHECK_RUNS_VIA_ACTIONS      gitcode Actions v8 适配器（api.gitcode.com）    gitcode
CHECK_RUNS_RESOLVE_SHA      check-runs 的 ref 需先解析成 sha（预留）          （本分支未启用）
STATUS_NATIVE               原生 GET /commits/{ref}/status                 github / gitcode
STATUS_RESOLVE_SHA          status 的 ref 需先解析成 sha                   gitcode
PR_ISSUE_LINK_NATIVE        PR↔Issue 有原生关联存储（可解除）                gitcode
PR_COMMENT_FLAT_PATH        评论 id 走扁平 /pulls/comments/{id}             gitcode/gitee/github（默认 True）
```

缺位 = 显式 404/幂等 no-op，**没有"回退到 gitcode"这条路**；`_gitcode_checks_for_sha`
只在 `CHECK_RUNS_VIA_ACTIONS` 为真时可达，函数内部仍走 gitcode API v8（Actions 是
gitcode 专有通道，不做跨平台抽象）。

### 9.2 gitlab 子路径

`PATH_MAP` 此前只映首段（`/pulls→/merge_requests`），尾段方言差异（评论是 `notes`、
MR 变更是 `changes`）没人接，导致 `VcsApiClient`（agent 侧）对、透传路由错。本刀给
provider 模块加可选钩子 `rewrite_path(subpath)`，由 `_vcs_url` 在拼接前调用：

```
/issues/{n}/comments            → /issues/{n}/notes
/merge_requests/{n}/comments    → /merge_requests/{n}/notes
/merge_requests/{n}/comments/{id} → /merge_requests/{n}/notes/{id}
/merge_requests/{n}/files       → /merge_requests/{n}/changes
```

### 9.3 沙箱 token 键

注入侧（`cloud_worker`）与读取侧（`executor` / `sandbox_context`）过去都写死
`BOT_GITCODE_TOKEN`：gitee 仓库在沙箱里 bot token 恒空 → `configure_repo` 因
`if repo_full_name and bot_token` 整段跳过 → 沙箱内没有仓库（比"clone 错平台"更早断）。
现在键名由卡 `token_env_name` 派生（`BOT_GITEE_TOKEN` …），并保留 `BOT_GITCODE_TOKEN`
兼容读/写（升级期不断链）。bwrap 白名单与场景 env 保留名按 `BOT_/USER_ + _TOKEN`
命名约定放行，不再逐平台枚举。

### 9.4 第二刀验证

```
tests/api/test_channel_capability_bits.py            15 passed
  · gitee checks → https://gitee.com/api/v5/repos/foo/bar/commits/main/check-runs
  · gitlab checks/statuses → 404 且 0 上游调用（不得打 gitcode）
  · gitcode checks → api.gitcode.com/api/v8/…actions/runs（不变）
  · github checks/statuses → api.github.com（不变）
  · gitee unlink → 幂等 no-op + 能力位理由；gitcode unlink 仍走 GET/PATCH
  · gitlab issue/MR 评论与 MR files 路径 → notes / changes
tests/runtime/test_clone_endpoint_multiplatform.py   20 passed（含 token 键派生与兼容读）
```

### 9.5 已知边界

- `tests/test_issue_2035_checks_statuses.py`（gitcode/github 的 checks/status 行为）本机
  因缺 `sqlalchemy` 无法运行；静态核对结论：该文件只断言 `code` 字段与 gitcode/github
  路径，不受本刀影响（未新增 gitee/gitlab 断言）。
- `tests/runtime/test_multi_platform_decouple_step1.py` 的 K1/K4 结构守卫、`test_agent_env_and_bot_identity.py`
  的 S37 源码断言、`test_issue_573_*` 的 4 条 DB 用例在基线上同样红（已按同用例在
  e707851bd 上复跑对照），属存量/环境债，本分支零新增。

### 9.6 真机验证（2026-11，只读）

对私有仓做 `git ls-remote`，三种用户名形态的结果：令牌账号名 `rc=0`；仓库 owner 在
**个人仓**（与账号同名）`rc=0`、在**组织仓** `rc=128 The token username invalid`；
`x-access-token` 与空用户名一律 `rc=128`。

因此用户名派生收敛为：卡 `bot_username`（= 令牌账号名）→ owner 兜底（仅个人仓可用，
打 WARNING 提示配卡）→ 无卡才 fail-fast。**组织仓必须配 `bot_username`**，代码不猜账号名。

真机同时暴露并修复了一个派生缺陷：`_basic_auth` 只传 provider 时拿不到 repo_key，
`basic_password` 退化为空用户名（连个人仓都失败）。现在用户名在 `ensure_repo_base`
一次派生后显式下传，`_basic_auth` 只用不再猜。

平台模块与能力位的只读验证：`/repos/{o}/{r}`、`/issues`、`/pulls`、`/branches`、
`/contents` 均 200；`/commits/{ref}/check-runs` 200（`CHECK_RUNS_NATIVE` 依据）；
`/commits/{ref}/status` 404（`STATUS_NATIVE=false` 依据）。

### 9.7 从库到派生的复验（dev 库，2026-11）

读 `grape_t_repo_platforms` 的真实行构造平台卡（字段名与 `_PLATFORM_CARD_COLUMNS` 对齐），
再让 `clone_params` 对 5 个托管仓派生：

```
clone_url   全部 https://gitee.com/<owner>/<repo>.git（按卡 host，不再 gitcode.com）
username    全部 = 卡 bot_username（<token-login>）
clone_auth  basic_password
认证        用该用户名 + 令牌只读 ls-remote → 5/5 rc=0
```

即"卡 → 派生 → 认证"三段在真实库数据上闭环。

凭据段随后也补齐：dev 环境 `credential_keys` 为空 → 密钥由运行进程 `JWT_SECRET` 派生
（kid=`derived`）。按同一规则在服务端生成密文（89 字符）写入 5 个托管仓的 bot 行
（含原本缺行的 `micro-svc`），服务端解密 5 条 → 5/5 明文哈希一致。
⇒ 四段（卡 → 派生 → 加密写入 → 解密读取）全部实测通过，无需服务端改配置。

### 9.8 部署与运行时观察（3 库 4 进程）

**部署要点**

- 四进程都要重启才吃到新代码：`mgmt → event → insight → worker`（worker 最后，它取卡依赖 mgmt）；
- 切换部署目录时 `.grape/skills/**` 是运行期物化产物、与仓库同名文件冲突，`git checkout`
  会 Aborting；做法是先打包 `.grape` 备份再 `checkout -f`，验证完可用备份恢复；
- 健康检查用端口：`8081/8082/8083/8084` 逐个 `curl /health`，另加一条
  「部署 venv 能 import 到 `vcs.clone_endpoint`」作为代码真的生效的判据（端口 200 只说明进程活着）。

**运行时观察（与本次改动相关的两条日志）**

```
取卡成功      platform cards from <source>: ['gitee']
派生/回落     clone_endpoint: <provider> 的平台卡未声明 bot_username … 回退为仓库 owner
```

**认领闸门与引擎健康（不属于本分支，但排障会撞上）**

`claim_by_tag` 的闸门是 `engine/service.py::_has_verified_executor`：

```
probe_status == spawn_failed                    → 拒绝
credential_id == 0（本机直连）                  → installed && healthy
其余                                           → worker_version 非空 或 probe_status 非 pending/failed
```

本次 dev 环境撞到的正是第一种形态的变体：hermes 引擎 `healthy=0`（ACP 二次探活失败），
而 worker 的 rid 列表里包含它 → 每轮一条 skip warning；同机其它引擎 healthy=1 且同样在
认领循环里。结论：**日志里那条 warning 不等于整台 worker 不能干活**，要看有没有 queued 任务
与健康 rid 的 claim 记录。

**边界**：引擎探活（hermes ACP）与 runtime 健康属运维面，不在本设计范围内；本设计只保证
「卡 → 派生 → 凭据 → worktree」这条链在 3 库 4 进程拓扑下成立。

### 9.9 新增平台接入（纯配卡，无需 Python）

本设计把 clone 三要素全部收口到平台卡，因此**新增平台 = 加一张卡**。最小卡（以示例平台
`gitea` 为例，字段值随平台替换）：

```json
{
  "name": "Gitea",
  "dialect": "github",
  "host": "gitea.example.com",
  "api_base": "https://gitea.example.com/api/v1",
  "api_auth": "token_header",
  "webhook_path": "/webhook/gitea",
  "webhook_verify": "hmac",
  "webhook_dialect": "github_web",
  "clone_auth": "basic_password",
  "bot_username": "<令牌所属账号名>",
  "token_env": "BOT_GITEA_TOKEN",
  "issue_write_form": "repo_path",
  "branch_create_form": "form",
  "pr_head_filter": "owner_ref"
}
```

必填/关键字段与影响面：

| 字段 | 作用 | 缺省/约束 |
|---|---|---|
| `host` | clone/web 域名（`clone_url` 直接用它） | 必填；无卡即 fail-fast |
| `clone_auth` | 鉴权形态 → 用户名派生 | 缺省 `x_access_token` |
| `bot_username` | `basic_password` 形态的用户名（令牌账号名） | 空则回退仓库 owner + WARNING |
| `token_env` | bot token 的环境变量名 | 缺省 `BOT_<PROVIDER>_TOKEN` |
| `dialect` | 字段翻译方言（`github` / `gitlab`） | 必填枚举 |
| `webhook_path` / `webhook_verify` / `webhook_dialect` | 入站回调形态 | `webhook_path` 必须 `/webhook/<name>` |

接入分两种深度：

```
① 只做 clone（克隆/推送）：上面这张卡即可，clone 链路零代码改动
   验收：tests/runtime/test_new_platform_card_only_onboarding.py
        （用例造一张新卡，断言 clone_url / clone_auth / username / token env 全部来自卡，
          且 module_for(新平台) 为 None 也不影响 clone）
② 还要代理 API（issue/PR/评论/checks）：卡里补 `resources.rules`（字段翻译）
   或写 providers/<p>.py 模块；通道能力位（CHECK_RUNS_NATIVE / STATUS_NATIVE /
   PR_ISSUE_LINK_NATIVE / PR_COMMENT_FLAT_PATH）按平台实际能力声明，缺位即显式 404/幂等 no-op
```

部署注意：3 库 4 进程下 worker 读的是**表行**（经 `GET /api/runtime/platform-cards`），
所以新平台卡要么写进 `grape_t_repo_platforms`，要么走 meta `vcs_providers_config`；
只放到部署目录的 `configs/platforms/` 里，worker 引擎读不到（详见运维 checklist §3）。

**本轮发现的实现缺陷（已修）**：`bot_token_env_name` 曾把 `PlatformCard.token_env_name`
当属性用（它其实是方法），导致「卡没有 `token_env` 字段」的平台在派生 token 键时抛
`AttributeError`（被上层 try 吞成"退回 BOT_GITCODE_TOKEN"）。上表用例把这一点锁住了。

### 9.10.1 修正：`session_key` 的唯一性不是契约（2026-11-21，原实现已还原）

§9.10 里描述过一版「无条件 upsert + 迁移建 uniq_session_key」的实现。**那一步是错的，已撤销。**

```
错在哪
  起点是 <db_mgmt> 上有手工 UNIQUE(session_key) → 裸 INSERT 第二次 1062 → 上层吞成
  warning → 快照有 token、子进程没有（#990 症状）。当时的修法是改成
  INSERT ... ON CONFLICT(session_key) DO UPDATE，并为了让它在无索引的库上不退化，
  又补了一条迁移：去重 + CREATE UNIQUE INDEX uniq_session_key（同时把 TEXT 改 VARCHAR(191)）。
  也就是「先把语义改成单行，再为了支撑它去改库结构」——倒果为因。

原设计是什么（代码证据）
  建表     只有 id INTEGER PRIMARY KEY AUTOINCREMENT，session_key 无 UNIQUE
  索引     ix_sat_kind_key(kind, session_key) —— NON_UNIQUE=1
  原语     session_token_set_expiry / _shrink_active / _delete_by_session 全是
           UPDATE/DELETE ... WHERE session_key=? （批量语义）
  读侧     只有 session_token_by_sha，没有「按 session_key 取一枚」
  实测     <db_mgmt> 至今没有 uniq_session_key；<db_alt> 的那个是这条迁移加的
  文档     session-token-idempotent-issue.md:97 自己写着「全仓 grep uniq_session_key=0 命中
           → 该索引是部署/迁移侧手工加的」

还原后的形态
  session_token_insert / session_token_rotate
      裸 INSERT 优先：无唯一索引的库 → 多行并存（各枚各自有效）
      撞唯一键（SQLite UNIQUE constraint / MySQL 1062）→ 就地 UPDATE 该行，返回其 id
      全程不用 UPDATE 的 rowcount 判存在性
  迁移 202611210004
      只保留 MySQL 列宽对齐（TEXT → VARCHAR(191)，因为部署侧可能的 uniq_session_key 要定长列）
      去重与建索引都已删除
  已跑过该迁移的部署
      保留现状即可（代码把索引当「可能存在的物理约束」兼容）；要还原原设计需人工
      ALTER TABLE session_api_tokens DROP INDEX uniq_session_key

对 chat 的影响
  conv 级 key 在原设计下完全合法（同一会话多条消息各持一枚）；唯一性一旦成立才变成
  「后签顶前签」。所以 §9.11 的消息级 key 不是绕开唯一键的补丁，而是原设计的形状。
  在部署侧有 uniq_session_key 的库上它仍然必要（物理上无法多行）。


### 9.10 会话凭据的写入与分发契约（相邻缺陷，两刀修完）

派发链路上「任务会话 token」与平台卡无关，但同一批真跑验证里连续暴露了两层写入问题，
按同一设计口径收口。

**契约一：写入先 INSERT，只有数据库真的回「重复」才降级 UPDATE**

```
写入面   session_api_tokens（mgmt 域真源）
主路径   裸 INSERT —— 同一 session_key 允许多枚并存（原设计：建表只有 id 主键、
         ix_sat_kind_key(kind, session_key) 非唯一、批量原语全按 key 操作）
降级     数据库拒绝该行（SQLite UNIQUE constraint / MySQL 1062）→ 就地 UPDATE 该行
         仅发生在「部署侧确实存在 uniq_session_key」的库上
行数     无唯一索引的库：多行并存，各枚各自有效；有索引的库：1 行（降级覆盖）
返回     session_token_insert 落库后 SELECT id 返回行 id（契约不变）
禁用     ✗ UPDATE ... ; if rowcount > 0 → 命中 else INSERT
         原因：MySQL 的 rowcount 是「实际改变行数」，同值重写为 0 → 误判 → INSERT → 1062；
               SQLite 的 rowcount 是「匹配行数」，同值也为 1 → 单测永远绿，掩盖线上缺陷
```

**契约二：列宽对齐，不声明唯一键**

```
DDL      session_key VARCHAR(191) NOT NULL（原 TEXT：MySQL 不允许 TEXT 上建唯一索引）
迁移     202611210004 session-token-key-column-width（kind=safe, domains=("mgmt",)）
           ① MySQL: MODIFY session_key VARCHAR(191) NOT NULL
           ② 仅此一步 —— 不去重、不建 uniq_session_key（早期版本做过这两步，已撤销）
为什么   部署侧可能有手工建的 uniq_session_key（实测 <db_mgmt> 建于 2026-11-18、
         <db_alt>），而 MySQL 不允许 TEXT 列建唯一索引 → 列宽必须对齐，
         否则该索引形态与写入路径冲突。但「唯一性」不是本仓的设计主张（见 §9.10.1）。
```

**契约三：派发期复用快照，不制造未落库 token**

```
入口      service.py::reuse_or_issue_session_token(_safe)
委托      session_token_for_execution(task_id, session_key_fallback=…)
            · 快照有效 → 复用，不轮换（不产生 401 窗口）
            · 快照失效/作用域不匹配 → 同事务作废旧 token + 签发 + 回写 agent_tasks.env_extra
            · 有任务行但同步失败 → 返回空串（绝不另签未回写快照的 token）
注入      成功：env["GRAPE_SESSION_TOKEN"], env["GRAPE_CLI_MODE"]="WORKER"
          失败：env 不注入 + SESSION_TOKEN_ISSUE_FAILED / SESSION_TOKEN_CONTEXT_MISSING
               写 agent_tasks.errors + log.error（#2790 可见性口径不变）
调用点    event_pipeline.py（派发前）、direct_executor.py（执行前）
反例      每次派发都 issue_session_token 新签一枚 → 覆盖入队快照 → 快照 stale →
          只能靠 sync_task_session_token 事后收敛，中间 401（#990 症状）
```

**基址（部署侧，代码分支不含改动）**

```
任务 env 的 GRAPE_SERVER_URL 必须对执行节点可达：
  ① meta.server_base_url（runtime 域库）—— 本机部署改为内网地址
  ② 或四进程 env 显式设 RUNTIME_SERVER_URL（agent.py 的本机覆盖读它；当前四进程 0/4 命中）
webhook 回调走独立键 webhook_url，不受 server_base_url 影响
```

**验收用例**

```
tests/runtime/test_session_token_idempotent.py        7 例（含同值重写 ×2、源码级 rowcount 禁令、
                                                             ON CONFLICT→ON DUPLICATE KEY 方言翻译锁）
tests/runtime/test_session_token_reuse_snapshot.py    5 例（复用不新签 / 空快照不注入 / 上下文缺失 /
                                                             同步异常 / safe 封装）
真库红绿对照（<db_mgmt>）：旧逻辑同值重写 → 1062；新实现同值 insert×2 + rotate×2 → 1 行
```

完整记录见 `docs/weekly-11/session-token-idempotent-issue.md`。
