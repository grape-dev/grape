# D107 — 代理层上游契约适配（v5 家族方言差异）设计分片

> 来源批次：2026-11-11 部署实弹验证（weekly-10 r3 集成树，feat/weekly-10-yaw-dev）。
> 索引导航：./multi-platform-decouple-design.md；用例 UC80/UC81（11-usecases-guards.md）；
> 需求 FR-28（multi-platform-decouple-requirements.md）；台账 06-ledger.md。
> ⚠ 红线：本文不含任何 IP/密码/token/密钥/真实内网地址（错误响应原文按字面义引用，无凭据形态）。

## 1. 背景与归因修正

部署实弹验证发现 gitee 仓 issues update 对 IK* 开头字符串 ident 恒 404
"project or enterprise"（form/JSON 双体、query 传 ident 与否均 404）。**该现象此前被
误判为"gitee 平台限制"（上游不认字符串 issue ident）**，据此放弃了 writeback 修复。

实测对照（同 token 同仓同 ident，仅换路径形态）：

| 路径形态 | 结果 |
|---|---|
| `PATCH /repos/{owner}/{repo}/issues/{number}`（repo 路径） | 恒 404 "project or enterprise" |
| `PATCH /repos/{owner}/issues/{number}`（官方 owner-path，body 带 repo） | 200 成功 |

**归因修正：不是平台限制，是集成路径错误。** gitee v5 官方 update 契约就是 owner-path
形态（v5 SDK 文档明载）；repo 路径形态是集成侧自造形态，上游按"owner=project 不存在"
解析才报 404。同案教训：**imjoey/ge PR !45**（gitee SDK 生态同样把 owner-path 契约
接错为 repo 路径、以平台限制结案后由社区更正）——外部先例证明该误判是系统性坑，
不独属于我们。

gitcode 同上游契约，但恰好对 repo 路径宽容侥幸工作——两平台统一收敛到官方契约
（不改行为面，只改路径形态），消除"一个平台侥幸=另一个平台 404"的隐性分叉。

## 2. 六项契约适配（实测实锤）

| # | 契约差异 | gitcode | gitee | github（通用基线） | 实测证据 |
|---|---|---|---|---|---|
| ① | issues update 路径 | owner-path（PATCH `/repos/{owner}/issues/{number}`，body 必带 repo） | 同左 | repo 路径 `PATCH /repos/{o}/{r}/issues/{n}` | gitee repo 路径对 IK* ident 恒 404 "project or enterprise"，owner-path 200 |
| ② | body.repo 形态 | owner/repo 全称与纯仓库名**均收** | **仅纯仓库名**（全称 404 "Not Found Project"） | n/a（路径含 repo 段） | gitee 发 `owner/repo` 全称 404，改纯 `repo` 200（同 token 同仓对照） |
| ③ | labels 色值 | **必须带 `#` 前缀**（"Color must be a valid color code"） | 宽容 | 必须不带 `#`（带 # 422，Issue #580） | gitcode 无 # 400 / github 有 # 422，方向相反 |
| ④ | milestones due_on | **只认 YYYY-MM-DD**（ISO 时间戳 400 提示 "correct to YYYY-MM…"） | 宽容 | ISO 时间戳 | gitcode 时间戳形态 400 |
| ⑤ | PR comment id | **32 位 hex** | **32 位 hex** | 数字 | int 声明对 hex 422；gitcode DELETE hex 直传 400 "Orchestration error"，须先解析为数字 note_id（复用 #623 解析器） |
| ⑥ | labels 创建 body 编码 | **仅 labels 端点**要 form（JSON 400 "Required request parameter 'name' is not present"，Spring form 形态）；其余写端点收 JSON | 收 JSON | 收 JSON | gitcode JSON labels 400 / JSON issue create、milestone 200 |

## 3. 机制设计：provider 契约开关（非枚举）

差异全部以**模块级契约开关**声明在 `src/vcs/providers/<name>.py`，代理层
（`src/api/git_repo_api.py`）以 `getattr(module_for(p), "<SWITCH>", False)` 派生消费：

- `LABEL_COLOR_PREFIX_HASH`（gitcode）：labels 色值补 `#`；
- `MILESTONE_DUE_DATE_ONLY`（gitcode）：due_on 截断为日期；
- `ISSUE_UPDATE_OWNER_PATH`（gitcode/gitee）：update 走官方 owner-path URL + body.repo
  必填（title 缺失回取回填，沿用 #640 已验证模式）。

**新平台接入时差异用同一组开关描述，代理层零平台枚举分支**——这正是多平台解耦
「配卡/模块声明差异、消费面通用」原则在传输契约维度的延续（对齐 D105-11 资源翻译
卡驱动的口径）。新差异类型（现三类开关描述不了的）才需要新开关 + 设计评审。

第 ⑥ 项（labels form 编码）不设开关：gitcode 模块 `request()` 内部对
`POST …/labels` 端点定向 form 编码（其余写端点走 `_req` JSON 通道，保持传输层
测试 monkeypatch 契约不变）。第 ② 项 gitee 纯仓库名随 owner-path 开关派生
（`repo if p == "gitee" else f"{owner}/{repo}"`，gitee 模块行为面差异，注释实锤在案）。
第 ⑤ 项是 id 形态兼容（#623 解析器复用），与开关族互补。

## 4. 交付与验证

- 代码 commits（本分支）：50b92de31（comment_id str 放宽）、770b063bf（hex→note_id
  delete 解析）、590aa8a22（owner-path 官方契约 + labels form 收窄 + _req 延迟导入）、
  568ef2f0b（gitee 纯仓库名）。
- 行为锁：`tests/test_proxy_contract_adaptations.py`（13 用例，传输层全 mock 零外网）：
  开关存在性 ×3、owner-path URL 拼接 ×3（gitcode/gitee/github）、label # 前缀 ×2、
  due_on 日期化 ×2、hex→note_id 解析 ×2（update/delete）+ 数字直传回兼容 ×1。
- 隔离跑法（测试铁律）：`OBS_LOG_DB=0` + `unshare -mr` + tmpfs `/tmp`，13 passed 全绿。

## 5. 风险与回滚

- owner-path 收敛对 gitcode 是**同契约换路径形态**（上游两种均收），行为面等价；
  gitee 是修复（404→200）。回滚=关开关（`ISSUE_UPDATE_OWNER_PATH=False` 即回 repo 路径）。
- gitee `repo` 全称→纯仓库名仅影响 owner-path 分支内 body 构造，GET/其余端点零涉及。
- 回归面：`test_git_repo_api` / `test_issue_640_milestone_contract` /
  `test_gitrepo_string_ident_contract` 家族全绿（见台账对应行）。
