# Git 仓库统一接口 Provider 中立化设计（/api/git-repo）

> 状态：设计方案（v1，2026-09-22）｜分支 weekly-04-lyz
> 关联：#213（grape-cli）、`help/api/10-git-repos.md`、`src/api/git_repo_api.py`、`src/vcs/api_client.py`（VcsApiClient）
> 触发：评审指出 pulls create 的 `inner_issue_nums`/`close_related_issue` 仍为 GitCode 专有字段

---

## 1. 背景与问题

### 1.1 现状：raw 透传 + 仅 1 处平台适配

`/api/git-repo` 的接入/认证层已 provider-aware（token `repo_key` 前缀 → gitcode/github base + token），
但**对外契约（入参/出参）仍是 GitCode 形态**：

- 大多数端点走 `_vcs_req(provider, ...)` **原样透传** body（`git_repo_api.py:382-389` pulls create）：
  `inner_issue_nums`、`close_related_issue` 等 GitCode 专有字段直接发给任意 provider → `github:` token 下被拒绝/无意义。
- **仅 `close_issue`** 走了 `VcsApiClient.close_issue`（#209）做平台差异适配
  （gitcode：先 GET title 再 PATCH `state=close`；github：PATCH `state=closed`）——适配层是既定方向，但覆盖 1 个操作。
- 文档 `10-git-repos.md` L10 明言「出参结构为 gitcode 平台实际结构；github/gitlab 未来映射」。

### 1.2 问题本质

对外 params/接口与具体 provider 绑定：
1. 技能/CLI 按 `gitcode-api-reference.md` 写死字段，GitHub 无法复用；
2. `_vcs_url`/`_vcs_req` 语义正确（provider 路由），但 body 未翻译——「接入中立、契约 GitCode」。

---

## 2. 目标

**终态 = 彻底中立（L3）**：
- `/api/git-repo` 对外**只暴露 provider 无关的 Git 资源契约**（路径、入参、出参统一），
  不暴露 `inner_issue_nums`/`close_related_issue`/`state=close`/`assignee_ids` 等平台专有细节。
- **核心设计物 = 接口参数映射表**：一张「中性参数 ↔ gitcode/github 实际参数」的静态表，
  服务端适配层**查表翻译**（入参中性→原生、出参原生→中性 DTO），新增 provider 只需加一行映射。
- 平台差异全部收敛到 `VcsApiClient` 适配层（完整双适配器：pulls/issues/labels/milestones/comments/files/merge…）。
- 兼容：`raw` 出参 + 旧字段别名，存量技能不回归；映射表作为文档与实现同源（`help/api/10-git-repos.md`）。

---

## 3. 两个必须正视的张力

| 张力 | 说明 |
|------|------|
| **token 与 provider 绑定** | provider 由 token `repo_key` 前缀决定。注入中立 ≠ token 能跨平台；语义中立，底层仍走 token 对应平台。 |
| **平台能力不对称** | GitCode「创建 PR 关联提交自动关 Issue」（`inner_issue_nums`/`close_related_issue`）GitHub 无；GitHub 用 PR body `Closes #N`。中性契约靠**语义化参数 + 映射**，直接删字段 = 丢能力。 |

---

## 4. 终态设计（彻底中立 L3）：中性契约 + 参数映射表 + 适配层

### 4.0 架构总览

```
技能/CLI → /api/git-repo（纯中性资源契约：路径/入参/出参 provider 无关）
                 ├─ inbound：中性参数 → 查询「参数映射表」→ provider 原生请求（gitcode/github/gitlab）
                 └─ outbound：provider 原生响应 → 中性 DTO（+ raw 兜底）
VcsApiClient = 完整多适配器（pulls/issues/labels/milestones/comments/files/merge…）
新增 provider 只补一行映射 + 原生调用函数，不改对外契约
```

### 4.1 中性契约（对外固定）

| 资源 | 中性入参 | 中性出参 DTO（摘要） |
|------|----------|----------------------|
| issues list/get | `state=open/closed/all` | `Issue{id,number,title,body,state(open/closed),labels,assignees,html_url,created_at,milestone}` |
| issues create/update | `{title, body, assignee?/assignees?, labels?, state?, milestone?}` | 同上 |
| pulls create | `{title, head, base, body?, related_issue_nums?: list[int], auto_close_issues?: bool}` | `PR{id,number,title,head,base,state,body,merged,mergeable,user,html_url,created_at}` |
| pulls list/get/update/merge | `{title?,body?,labels?}` / merge 无参或 `{merge_method?}` | 同上 / `{merged, sha}` |
| comments（issues/pulls） | `{body}`（行内评论见 §4.3 能力差异） | `{id,body,path?,line?,user,created_at}` |
| repo/branches/tree/contents/commits/labels/milestones/files | 已中性 | 中性 DTO（labels/milestones 并入 L1 归一） |

### 4.2 接口参数映射表（核心设计物，查表式翻译）

> 声明式 `PARAM_MAP`（代码 + `help/api/10-git-repos.md` 同源）。inbound：中性 → 原生；outbound：原生 → 中性 DTO。

| # | 中性参数 | gitcode 实际（`api.gitcode.com/api/v5`） | github 实际（`api.github.com`） |
|---|----------|------------------------------------------|----------------------------------|
| 1 | issues.create `{title,body,assignee?}` | `{repo?,title,body,assignee?}`（`repo` 由 path 补，去掉客户端冗余） | `{title,body,assignees?}` |
| 2 | issues.update `labels` | 字符串/数组 | `labels`（名数组） |
| 3 | issues.update `assignees` | `assignee_ids`（数字 id 数组） | `assignees`（用户名数组） |
| 4 | issues.update `state=close` | `{state:"close", repo, title}`（需先 GET 取 title） | `{state:"closed"}` |
| 5 | issues.query `state`（open/closed/all） | `opened/closed/all` | `open/closed/all` |
| 6 | issues.close（端点） | GET title → PATCH `state=close`（复用 `VcsApiClient.close_issue`） | PATCH `state=closed` |
| 7 | pulls.create `title/head/base/body` | 同 | 同 |
| 8 | pulls.create `related_issue_nums: [5,6]` | `inner_issue_nums="o/r#5,#6"` | 无原生 → body 追加 `Closes #5 #6` |
| 9 | pulls.create `auto_close_issues` | `close_related_issue=true` | body 尾部 `Fixes #N`（关键词见 §8 待决） |
| 10 | pulls.comments.create `{body}` | 行内评论（`path/line` 可选） | review comment 需 `path/line/commit_id`（行内能力差异） |
| 11 | pulls.comments resolve | `discussion_id` | review thread「mark resolved」（无 discussion_id） |
| 12 | pulls.merge | 默认 merge（行为差异） | 需 `sha`+`merge_method` |
| 13 | tree `depth=recursive` | 不支持（返回 400） | 支持 `recursive=1`（能力交集外，文档标注） |
| 14 | 出参 state | `opened/closed` → DTO `open/closed` | `open/closed` → DTO `open/closed` |

> **翻译机制**：inbound 在路由进 `_vcs_req`/`VcsApiClient` 前按 PARAM_MAP 逐字段翻译；outbound 按 DTO 定义抽字段 + `raw` 保留原始响应。旧字段（`inner_issue_nums` 等）在 Pydantic model 上保留为 alias，翻译前归一到中性字段——向后兼容。

### 4.2.1 映射表形态（怎么存在）

**结论：单一真相 + 按 provider 分区。** `PARAM_MAP` 仍是唯一执行真相，但**每个 provider 维护自己的 map 模块**，
`__init__` 汇总成注册表；未来加 gitlab/gitee **新增一个 provider 文件即可**，不改既有 provider、不改中性契约。

| 形态 | 是否采用 | 理由 |
|------|:---:|------|
| 每 provider 一个 Python map 模块 `src/vcs/providers/{provider}.py`（复用既有 `src/vcs/`，不新增顶层目录） | ✅ 主 | 加 provider=加文件；低侵入、隔离、符合「新增 provider 只写适配器」；数据 + transform 一体 |
| 一个宽表横向加列 | ❌ | 一行塞多平台、改公共代码、merge 冲突面大 |
| JSON/YAML 数据文件 | ⚠️ 生成物 | 表达力不足（拼接/追加是代码），只作导出/审查 |
| 文档 `10-git-repos.md` | ✅ 同步 | 从各 provider map 生成/校验一致 → 「文档与实现同源」 |

结构示意：

```text
src/vcs/
  api_client.py     # VcsApiClient（close_issue 等业务适配，可逐步委托 providers）
  providers/
    __init__.py     # PARAM_MAP = {"gitcode": gitcode.MAP, "github": github.MAP, "gitlab": gitlab.MAP}
                    # get_map(provider) 未注册 → 显式报「provider 未支持」
    base.py         # 中性契约：字段枚举 / DTO schema / transform 约定（全球唯一）
    gitcode.py      # MAP = {("issues","create"): {"assignee":"assignee", "state":{"values":{"open":"opened"}}},
                    #        ("pulls","create"): {"related_issue_nums":{"native":"inner_issue_nums",
                    #            "transform": _gc_inner_nums}, ...}}
    github.py       # 同形：{"state":{"values":{"open":"open","closed":"closed"}}}；related_issue_nums 走 body “Closes #N”
    gitlab.py       # （未来）只加此文件
```

- **inbound**：`get_map(provider)[(res,op)]` → 逐中性字段「原生字段名 / 值映射 / transform」；无映射原样透传。
- **outbound**：`DTO_MAP` 同样按 provider 分区（原生 → 中性 DTO 字段）+ `raw` 保留。
- **新增 gitlab/gitee**：加 `param_map/{provider}.py` + 原生调用适配函数；**`/api/git-repo` 签名、中性契约、既有 provider map 零改动**。

### 4.2.2 transport 下沉：`git_repo_api.py` 加 provider 零改动

路由层不再持有 provider 分支——**每个 provider 模块自曝 API_BASE / request / token 解析**，
`git_repo_api.py` 只读注册表（`_MODULES`）：

```python
# src/vcs/providers/gitlab.py（未来，只加此文件）
API_BASE = "https://gitlab.com/api/v4"
def request(method, url, token, body=None): ...      # 原生调用
def bot_token(repo_key): ...                          # 仓库级 token
def user_token(username, uid): ...                    # 用户级 token
MAP = {...}
translate / query_state
```

`git_repo_api.py` 四个触点全部改读注册表：

| 触点 | 改法 |
|------|------|
| `_SUPPORTED_PROVIDERS` | `= set(_MODULES)`（自动） |
| `_vcs_url` base | `_MODULES[p].API_BASE` |
| `_vcs_req` 分发 | `_MODULES[p].request(method, url, token, body)` |
| `_repo_token` | bot → `_MODULES[p].bot_token(rk)`；user → `_MODULES[p].user_token(username, uid)` |

→ **加 gitlab/gitee = 新增 `vcs/providers/{provider}.py` + `__init__` 注册，`git_repo_api.py` 与既有 provider 零改动**（完成「新增 provider 只写适配器」闭环）。

### 4.3 受影响接口清单（映射表覆盖提示）

| # | 端点 | gitcode 绑定点 | 映射条目 |
|---|------|----------------|:---:|
| 1 | POST `/issues` | body 双形态（issue-advance `{repo,...}`） | §4.2-#1 |
| 2 | PATCH `/issues/{number}` | `state="close"`、`assignee_ids` | §4.2-#3/#4 |
| 3 | POST `/pulls` | `inner_issue_nums`/`close_related_issue` | §4.2-#8/#9 |
| 4 | POST `/pulls/{number}/comments` | 行内评论仅 `{body}` | §4.2-#10 |
| 5 | PUT `/pulls/{number}/comments/{discussion_id}` | GitHub 无 discussion_id | §4.2-#11 |
| 6 | PUT `/pulls/{number}/merge` | merge 语义差异 | §4.2-#12 |
| 7 | 全量读接口出参 | gitcode 原样结构 | DTO+raw（#14） |
| 8 | GET `/tree` `depth=recursive` | 仅 GitHub | #13（能力标注） |

> labels/milestones 语义相近（name/color/state/title），差异（颜色格式、due_on）低危，并入中性契约。

### 4.4 实施阶段（以 L3 为终态，逐阶段落地）

| 阶段 | 范围 | 产出 |
|------|------|------|
| **P1** | 参数映射表落库/落文档 + pulls create / issues create·update 的 inbound 翻译（gitcode/github 双写） | PARAM_MAP(P1 子集) + 双 provider 用例 |
| **P2** | 读出接口出参归一中性 DTO（`raw` 兜底）+ 剩余 inbound（comments/merge） | 完整 PARAM_MAP + neutral DTO |
| **P3** | `VcsApiClient` 完整多适配器，`/api/git-repo` 全部走适配层；`gitcode-api-reference.md` 退役 → `grape-git-contract.md`；CLI/技能同步 | 彻底中立终态 |

---

## 5. 改动点清单（按 P1→P3 落地）

| 文件 | 改动 |
|------|------|
| `src/api/git_repo_api.py` | model 全中性（`related_issue_nums`/`auto_close_issues`/`state=open·closed`/`assignees`…），旧字段保留 alias；四触点（SUPPORTED/base/`_vcs_req`/token）改读 `vcs.providers._MODULES` 注册表（§4.2.2），不再含 provider 分支 |
| `src/vcs/api_client.py` | 完整多适配器：pulls/issues/labels/milestones/comments/files/merge；`close_issue` 并入；inbound/outbound 查表翻译 |
| `src/vcs/providers/`（新，复用 `src/vcs/`） | 每 provider 模块自曝 `API_BASE`/`request`/`bot_token`/`user_token`/`MAP`/translate（base 中性契约 + `__init__` 注册表）；`VcsApiClient` 可逐步委托 |
| `help/api/10-git-repos.md` | 中性契约 + §4.2 映射表（文档与代码同源）；移除 gitcode 专有字段 |
| `gitcode-api-reference.md` | 退役 → `grape-git-contract.md`（P3） |
| grape-cli（独立） | 命令参数切中性（`--related-issue-nums` 等） |
| tests | 双 provider 用例（gitcode/github 各 1 组）+ 映射表覆盖度断言 + 旧别名兼容 |

## 6. 验收标准（终态）

| # | 验收项 |
|---|--------|
| 1 | `/api/git-repo` 对外参数/响应 **provider 无关**（契约内无 `inner_issue_nums`/`state=close`/`assignee_ids` 等） |
| 2 | PARAM_MAP 覆盖 §4.2 全部条目；inbound/outbound 各双 provider 用例通过 |
| 3 | `gitcode:` → `inner_issue_nums`/`close_related_issue`/`state=close`；`github:` → `Closes #N`/`state=closed`/`closed` |
| 4 | 旧字段作为 alias 仍可调用（向后兼容），存量 gitcode 技能/CLI 不回归 |
| 5 | 出参 `raw` 保留平台原样（排障/兼容兜底）；新增 provider 仅加映射行 |
| 6 | 文档（10-git-repos.md + grape-git-contract.md）与 PARAM_MAP 同源 |

### 6.1 测试用例设计

> 载体：`tests/test_git_provider.py`（PARAM_MAP 单测）+ `tests/test_git_repo_api.py`（端点级，mock `_vcs_req`/平台响应）。

| # | 用例 | provider | 前置 | 断言 |
|---|------|:---:|------|------|
| T1 | pulls create → gitcode 载荷 | gitcode | token=`gitcode:o/r`；body `{title,head,base,related_issue_nums:[5],auto_close_issues:true}` | v5/pulls payload 含 `inner_issue_nums="o/r#5"`、`close_related_issue:true`；不含中性字段 |
| T2 | pulls create → github 载荷 | github | 同 T1 | payload 含 `title/head/base`，body 追加 `Closes #5`；**无** `inner_issue_nums`/`close_related_issue` |
| T3 | 旧字段别名兼容 | gitcode | body 用 `inner_issue_nums:"o/r#5"`/`close_related_issue:true` | 归一到中性后生效，载荷同 T1 |
| T4 | issues create body 剥 repo | gitcode | body `{repo:...,title,body}` | v5/issues payload 无 `repo`（由 path 补） |
| T5 | issues PATCH state 归一 | github | `{state:"close"}` | payload `state:"closed"` |
| T6 | issues query state 归一 | gitcode | `state=open` | 请求 v5 `state=opened` |
| T7 | assignees 转换 | 双 | `{assignees:["bob"]}` | gitcode→`assignee_ids`；github→`assignees` |
| T8 | outbound DTO + raw | 双 | mock 响应 `{state:"opened",...}` / `{state:"open",...}` | DTO.state==`open`；`raw` 含原对象 |
| T9 | 映射表覆盖度 | 双 | 遍历 `PARAM_MAP` 全条目 | 每条 inbound+outbound 有 ≥1 通过用例 |
| T10 | 新增 provider 零改动路由层 | gitlab（模拟） | 加 `vcs/providers/gitlab.py`（API_BASE/request/token/MAP）+ 注册 | `/api/git-repo` 端点/模型/`git_repo_api.py` 零改动，仅注册表新增 |

## 7. 风险与兼容

| 风险 | 等级 | 缓解 |
|------|------|------|
| 技能/CLI 依赖 gitcode 字段 | Med | 旧字段别名保留；技能同步为渐进 |
| github 无「关联关闭」等价能力 | Low | `auto_close_issues` github 映射=body `Closes/Fixes #N`（语义近似，非原子） |
| 响应归一化破坏存量解析 | Med（P2） | `raw` 字段兜底 + 旧形状保留，P2 单独评审 |
| provider 判定与 token 绑定造成「中立名不副实」 | Low | 文档明确「语义中立、平台由 token 决定」 |

## 8. 回退方案

- P1/P2/P3 独立提交可单独回退：**旧字段 alias + `_vcs_req` 直通路径保留**，回退后存量行为不变。
- PARAM_MAP 为纯增量翻译层，未收敛的操作继续走原透传（渐进替换，不一次性断）。

---

## 9. 待办/未决

- [ ] GitHub `Closes`（默认 master）与 `Fixes` 语义选定（对齐 GitHub 关键字）——§4.2-#8/#9。
- [ ] 行内评论 / review-thread resolve（§4.2-#10/#11）是否纳入 P1 中性契约，还是按「能力差异」标注留 P2。
- [ ] merge 参数（sha/merge_method）中性化形态（无参 vs `merge_method?`）。
- [ ] 出参 DTO 字段清单（Issue/PR/…）与 `raw` 保留策略最终确认。
- [ ] PARAM_MAP 按 provider 分区已定（§4.2.1）；「文档同源」生成方式待定（手写 vs 从代码导出）。
- [ ] transport 下沉（§4.2.2）已定：provider 模块自曝 API_BASE/request/bot_token/user_token；`git_repo_api.py` 读注册表零分支。
- [ ] gitlab/gitee 首例 provider 的适配函数清单（作为「只加一个文件」的新增范式验证）。
- [ ] `gitcode-api-reference.md` → `grape-git-contract.md` 的迁移列表（技能侧改动清单）。