---
name: domain-qa
version: 3.6.0
description: |
  面向 jiuwenswarm 领域模板仓库的领域 Swarm 二次开发验收（Grape QA，接管 coding C7）：7.1 静态自检；7.2 后端启动+WS 黑盒；
  7.3 前端编译+品牌冒烟；7.4 对话识别+代办冒烟。验收：扩展 async 可实例化、RPC 4 参契约、
  get_config、gateway 生效、页签插位、WS/REST CRUD 闭环、对话可识别可执行、交付项核对、Agent
  工具全量注册（24z，account 实坑）。finance 实坑：全域 RPC unknown method 查扩展
  加载失败/相对导入残留 → 一律绝对导入。断言 4g + 24aa 面板占位分配与布局排版契约——多面板
  须声明宿主容器占位（`flex:1 1 0%`+`width:100%`，grid 则 minmax），禁只写内部布局；环境就绪时
  浏览器打开领域页签做布局视觉自检（等宽/无堆叠/无横向溢出）。PASS 出结论，问题 A2A 打回 coding；
  每项至多 1 次；严禁 git merge/装依赖/后台常驻/为跑通改仓库；缺依赖即「待部署后冒烟」降级。
tools:
  - grape
  - bash
---
## 安全硬约束（必须遵守）

1. **CLI 优先**：凡 grape CLI 能做的走 CLI，禁止绕过 CLI 直连平台 API（curl/python httpx 等）。CLI 未装/版本过低 → **自动安装/升级**（官方源/签名包优先）→ 装好继续；仅当安装失败/环境不可用时才报障说明（不降级直连）。⚠️ 安装指引见 `references/grape-cli-installation.md`。
2. 调用仓库 API 只用 prompt 注入的 `$GRAPE_SESSION_TOKEN`，严禁从数据库/configs/日志/历史中寻找其他 token。
3. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据（`data/workspaces/` 项目知识文档除外）。
4. API 返回 403/无权限时：立即停止该操作并回复用户「该操作需要管理员/维护者权限，请手动配置后重试」，严禁绕行手段。
5. 401/凭据失效：任务级 token（tk_）短命，失效属正常机制——说明「凭据已失效」即可，不尝试替换/绕行。
6. token 不得写入任何文件/日志/评论。
7. **不执行业务代码本身**：只做静态检查、脚本自检与（可行时）网关合约冒烟。
8. **不搭环境、不越权（v3.2 新增，最高优先）**：验收只在**现成可用环境**上进行。**严禁**：
   - `git merge/pull/checkout/branch/commit/push` 等任何改变仓库状态的操作（QA 不搬运代码、不合并分支、不还原工作区）；
   - 安装/补齐任何依赖（`pydantic-core`、`aiohttp`、`uv pip install`、`npm install` 等一律禁止）；
   - 后台常驻进程（`nohup ... &`、`screen/tmux` 起服务）与反复启停调试（`start_services all` 后查端口、读配置、重启重试）；
   - 为让程序跑起来而改动落地仓库任何文件/配置。
9. **环境未就绪=验收结论，不是待办任务（v3.2 新增）**：若落地仓库缺依赖/缺配置/无法一次启动，这正是验收证据——
   动态项记 `FAIL/待部署后冒烟`、按 Q3 打回 coding 或交 sre，**绝不自己动手把它修到能跑**。

# 领域 Swarm 二次开发验收（domain-qa v3.6）

## 环境变量表（平台注入，按 qa Agent 类型）

| 变量 | 语义 | 用在哪 | 何时有 |
|------|------|--------|--------|
| `$GRAPE_SERVER_URL` / `$GRAPE_SESSION_TOKEN` | 服务端地址 / 会话 token（tk_） | grape CLI 自动读取 | 全部任务 |
| `$REPO_FULL_NAME` | 仓库全名（owner/repo） | A2A 交办 `repository`；`--owner`/`--repo` 拼装 | 全部任务 |
| `$REPO_OWNER` | 仓库属主 | CLI 用 `--owner` | 全部 |
| `$REPO_NAME` | 仓库名（派生：`${REPO_FULL_NAME#*/}`） | CLI 用 `--repo` | 全部 |
| `$ACCOUNT` | 个人账号（触发人 actor） | A2A payload `account` | 全部 |
| `$ISSUE_NUMBER` | Issue 编号（A2A 接入时注入） | `--number` / A2A payload | 全部任务 |
| `$DEVOPS_EVENT_TYPE` | 事件类型 | 场景路由判断 | 全部 |

派生变量：`REPO_NAME` = `${REPO_FULL_NAME#*/}`。敏感变量（token 类）**不 printenv、不落盘**。

## 前置检查门（自愈 → 重试 → STOP）

| 检查项 | 判定命令 | 通过标准 | 失败动作 |
|--------|---------|---------|---------|
| CLI 可用 | `grape --version` | ≥0.3.2 | **自愈**：按 `references/grape-cli-installation.md` 自动安装/升级（`timeout 300 pip install git+https://gitcode.com/grape-dev/grape-cli.git`）→ 装好再验证一次；仍不可用 → 输出「前置检查未通过：<原因>」并结束任务 |
| 契约刷新 | `grape refresh` | 拉取最新契约成功 | **重试** ≤2 次；仍失败 → 继续（回退内置快照） |
| 任务上下文 | `grape tasks current --json-out` + `grape issues detail` | 拿到 PR/交付物清单 | ISSUE/PR 缺失 → 用 `grape git-repo issues get` / `pulls list` 定位；定位不到 → STOP |
| 项目知识基线 + 法务先例 | prompt 内联 + 本地 `jiuwenswarm` 基线仓库（`extensions/legal_platform/` 等） | 返回基线摘要 | 「无基线」→ 降级到 references + README，**不阻塞** |
| 环境就绪（Q2-0，最高优先） | 只读 import 探针：`uv run python -c "import jiuwenswarm, <domain>_platform"` 或等价；服务端口 | 配置 | 依赖齐/无需 git 操作即含 PR 改动 / 端口未占 | 任一不满足 → **所有运行级冒烟整体降级**为静态核验 + 「待部署后冒烟」，**严禁**装依赖/起后台/改仓库 |
| 瞬时失败（网络/限流） | — | — | **重试**：自动重试 ≤3 次（退避 1s/2s/4s） |

> 本技能 QA 不搭环境、不装依赖、不改仓库——环境就绪判定是所有运行级项的唯一前置门槛（v3.2 增，SKILL.md Q2-0）。

## 定位

领域 Swarm 工厂流水线的**第四站**（承接 domain-coding 移交流水线的**全部运行级验收**）：

- **职责内**：按验收清单对「改造后的项目」执行静态 + 动态验收，输出每条 PASS/FAIL，判定
  **PASS → 评论输出验收通过结论并结束 / REVISE → A2A 打回 domain-coding 继续处理**。
- **接管说明（v3.0）**：原 `domain-coding` 的 C7 验收（7.1 静态自检 / 7.2 后端 1 次启动 + `/health` +
  WS 黑盒 / 7.3 前端编译 + 品牌冒烟 / 7.4 对话识别 + 对话代办冒烟 / 7.5 超时降级）已整体移交本技能执行；
  coding 不再预跑运行级冒烟，只负责交付完整代码与 `scripts/validate_domain_project.py` 副本。
- **职责外**：不实施新改动（domain-coding）、不部署运行。

## 评论前缀规范

所有 QA Agent 评论 body 一律以 `[Domain QA Agent]` 开头。

## 处理流程

### 场景 A：接管 A2A（coding 交办验收 / 打回修改复检）

#### Q1 读取流水线信息（必须先做）
- 读取 payload：`pr_url`、`domain`、`产品品牌名`、`直接检查范围（改了什么）`、交付物清单
  （coding 不再产出运行级自检结果，只保证 `scripts/validate_domain_project.py` 静态可用；运行级验收由本技能全量执行）。
- 提取并明确 **Reference version**：下载/读取 PR，commit 后单独目录，
  **<reference version=j1>保存 PR commit 内容</reference version>**。
- 按 Reference version 读取 PR 改动与文档：<reference version=j1>README、部署指南、架构图、经验分享</reference version>。
- 依据法务先例（`extensions/legal_platform/`、前端 `LegalWorkbenchPanel/legalApi.ts/frontendPlatform.ts/SessionSidebar/legal-ai-logo.svg`），
  理解领域项目应长什么样，再验收。
- **配套命令**：
  ```bash
  # 读取 Issue 当前阶段 + pipeline（防重复 A2A 检查）
  grape issues detail --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out
  grape issues pipeline --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out
  ```

#### Q2 执行验收四条
详见 `references/qa-checklist.md`，按模板输出**每条 PASS/FAIL + 证据（命令/文件/路径/片段）**。

**Q2-0 环境就绪判定（v3.2 新增，所有"可运行时"项的前置门槛）**
在开始任何运行级动作前，用**只读命令**一次性判定环境是否就绪（≤1min，不打洞排查）：
- ✅ 就绪判据（全部满足才算）：落地仓库当前工作区已是待验收代码（无需任何 git 操作即包含 PR 改动）；
  运行环境自带依赖（`uv run python -c "import jiuwenswarm, <domain>_platform"` 或等价 import 探针直接成功，无需安装）；
  服务可用一条命令前台启动；端口未被占用。
- ❌ 任一不满足 → **立即停止全部运行级冒烟**，将所有"可运行时"项（①-插启动、前端编译、对话代办、端到端闭环）
  整体降级为静态核验 + 备注「待部署后冒烟（环境未就绪）」；其中缺依赖/启动失败这一点**本身就是验收证据**，
  记入 REVISE 清单（若代码层面有证据则打回 coding，纯环境问题交 sre）。**禁止为了通过冒烟而装依赖/改仓库。**

**① 自检与静态检查（必跑，不通过直接 REVISE）**
- 将 `references/scripts/validate_domain_project.py`（**权威版自本技能发布**，coding 侧为同一份副本）复制到落地仓库 `scripts/` 后，在其根目录运行
  `python scripts/validate_domain_project.py <repo> --domain <domain>`，核对退出码（应 0）；
- ⚠️ 若自检脚本缺失/不可运行 → 直接 REVISE 回 coding（交付物不完整）。
- 对照 coding 的「16 处改动路径速查（含可用性增强 + 任务对话可识别）」逐项静态确认：后端扩展层文件、前端**每功能独立页面组件**
  `<Domain><Feature>Panel/`、`<domain>Api.ts`、`<domain>Store.ts`、`SidebarNavKey`、`SessionSidebar` 顶部 logo 引用、
  `App.tsx` 每 key 独立渲染、i18n `nav.<domain>*` + 欢迎语（zh/en 成对）、`public/<domain>-ai-logo.svg`。
- ⚠️ **前后端可用性静态确认（新增验收维度）**：
  - 后端可用前提：`extension.py` 的 `register_extensions` 为 **`async def`**（loader 以 await 调用，写成 def 会导致扩展注册不上）；
    **扩展内模块必须绝对导入（v3.5 finance 实坑）**：检查 `extensions/<domain>_platform/*.py` 无
    `from .xxx import` 相对导入残留（`grep -n "from \." extensions/<domain>_platform/` 应为空）——
    扩展模块引用应写 `from jiuwenswarm.extensions.<domain>_platform.<module> import ...`；❌ 相对导入在 loader
    动态模块名 `jiuwenswarm.loaded_extension.<name>` 下报 `No module named 'jiuwenswarm.loaded_extension'`
    （磁盘无此父包）→ 整个扩展加载失败、全域 `<domain>.*` RPC unknown method（finance 实坑，46 个 RPC 全丢）；
  - 配置双份：`<domain>_gateway` 段**同时存在于** `resources/config.yaml`（运行时生效）与
    `resources/config/config.example.yaml`，且 `enabled: true`——只写 example 默认启动后 REST 网关不加载、后端功能不可用；
  - 前端滚轮：`SessionSidebar/index.tsx` 导航 items 外包 `<div className="icon-rail-nav-list">`，
    `SessionSidebar.css` 含 `.icon-rail-nav-list` + `overflow-y: auto`——页签过多时可滚动、全部可达。
- ⚠️ **任务对话可识别 + 可执行七查（新增验收维度，缺一 REVISE）**——统计每个 Spec 功能是否都能被主对话模型识别并**真实发起**调用：
  - (a) **委派映射**：`workspace/skills/*-swarm-coordination/SKILL.md` 含「委派映射」表，**每个功能一行**（触发关键词→专员 id）；
  - (b) **技能 RPC**：每个功能技能 SKILL.md 有「适用场景」+「调用 `<domain>.<x>.<y>`（参数）」；
  - (b2) **可执行通道段（v2.5 新增）**：每个功能技能 SKILL.md 含「可执行通道段」——REST 网关地址/方法/
    **鉴权头（`X-API-Key: <api_key>`，api_key 来自 config `<domain>_gateway` 段，禁止硬编码）**/参数 JSON 契约，
    或已注册的 Agent 直接可调领域工具（`<domain>_gateway_tool.py`）——模型拿到文档即可写出真实 curl/webRequest 调用；
  - (c) **persona 枚举**：LEADER/worker persona「能做啥」逐条枚举领域功能；
  - (d) **RPC 四处一致**：技能引用的 RPC 名 = `extension.py` 注册名 = `<domain>Api.ts` = `dispatch_<domain>_rpc` 分支；
  - (e) **主对话身份注入（v3.3 新增，防"问你能做啥答不出领域"gov 实坑）**：`agents/harness/common/prompt/prompt_builder.py`
    `_identity_prompt()`（zh/en）已领域化——「# 身份」为 `<Brand>`/`<平台名>` 领域身份，「## 你能做什么」**逐条枚举
    领域能力**（与左侧页签/RPC 同口径），含「【强制·问候回复】」段（问候时完整输出领域自我介绍+能力概览，
    **不得自称通用助手**）；❌ 仍是通用 JiuwenSwarm 身份 / 无强制问候段 → 问"你能做啥"不回领域功能，判 REVISE；
  - (f) **Agent 领域工具注册（v3.3 新增）**：若提供 `<domain>_gateway_tool.py`，则 `interface_deep.py` `_get_tool_cards()`
    必须含注册块（`_register_shared_tool(<domain>_gateway_call)` + `tool_cards.append(card)`，受 `<domain>_gateway.enabled`
    控制），启动日志含 `"<Domain>Swarm <domain>_gateway_call tool registered"`；❌ 只定义未注册 → 模型工具链无此工具，
    技能"可执行通道段"全部落空，判 REVISE；
  - (f2) **Agent 领域工具全量注册核对（v3.4 新增，account 实坑：页面/WS 全通但主对话代办只聊不做）**：不只看本域——
    `grep -l "gateway_tool" extensions/*_platform/*.py` 全仓列出**每一个**用 `@tool` 定义工具的领域，逐一在
    `interface_deep.py` `_get_tool_cards()` 找对应 `_register_shared_tool` 注册块（import → 注册 → append card）：
    ① 存在 `*_gateway_tool.py` 但无注册块 / ② 该域从未提供 gateway_tool（Spec 开放了对话代办）→ 都判 REVISE。
    **进程内直调工具必须是无条件注册**（try/except 包裹、不依赖 `<domain>_gateway.enabled`，因进程内调用不经
    REST 端口）——account 实坑：发票/凭证等页面齐全、WS RPC 全通，但工具未注册，主对话"帮我登记一张发票"
    只回复"好的已登记"、SQLite 行数不变（24t/24z 双命中）；
  - 冒烟（仅环境就绪，门槛=Q2-0）：主对话对模型下达每个功能的指令，确认识别→**真实 RPC（看日志/落库，非只看回复文本）**→落库→前端对应页签出现新记录。另发问候语（"你好/你能做什么"）确认识别身份：回复含领域品牌名 + 领域能力概览，非通用助手口径。
**①-插 后端运行冒烟（接管 coding C7.2，仅环境就绪且服务仅前台启动 1 次）**
- **前置门槛 = Q2-0 环境就绪判定**；未就绪 → 跳过本项，直接把"后端运行/WS 黑盒/端到端"标「待部署后冒烟（环境未就绪）」，
  **禁止** `git merge`/装依赖/起后台服务/调启动（VOD→Issue→Coding 流水线的落库代码，sre 侧在部署环境一并验证）。
- 就绪后启动服务 **仅 1 次且必须前台 + 超时包裹**：`timeout 300 python -m jiuwenswarm.start_services --foreground`
  （或等价单命令），确认 `ExtensionLoader` 加载无报错、**扩展模块绝对导入可正常加载（v3.5 finance 实坑：若日志报
  `No module named 'jiuwenswarm.loaded_extension'` → 扩展内残留相对导入 `from .xxx`，整个扩展加载被跳过、
  该域全部 RPC 未注册 → 判 REVISE 打回 coding 改绝对导入）**、`register_extensions` 被 await；
  **❌ 禁止 `nohup ... &` 后台常驻**；启动失败/超时 → 记一次证据，按 7.5 降级，**不再重试**；
- REST 舱门：`curl http://127.0.0.1:<port>/api/<domain>/health` 返回 `success`；
- **WS 黑盒抽查（现成脚本，禁止手写）**：将 `references/scripts/ws_smoke.py` 复制到落地仓库 `scripts/` 后运行
  `python scripts/ws_smoke.py --method <domain>.write.xxx --params '{...}' --method <domain>.read.yyy --params '{}' --expect-key <列表字段>`，
  对**任一代表功能**抽 **1 条写 RPC + 1 条读 RPC**（如 `case.create` + `case.list`），断言收到 `ok:true` 回包；
  读 RPC 的 `--expect-key` 断言 payload **直接包含**业务字段（如 `items`/`records`/`total`），阻断
  **"已落库但列表空白"实坑**——WS payload 若整包下发 `{ok,data}` 双层（后端 `payload=result or {}`），
  前端 `res.items` 取空、页面显示"暂无资产"（finance 实坑）；`ws_smoke.py` 还会自动检测双层包裹并判 FAIL；
  脚本已按 webClient 真协议（`{type:req,id,method,params}` + res 回包）发帧并断言，不需要再写/调试任何代码；
- 只看 `dispatch` 静态存在不算通过，**必须真的拿到回包**（写 RPC 落库可在读 RPC 返回新行时一并印证）；
  若出现"ok:true 但 payload 无业务字段/双层包裹"→ REVISE 打回 coding（成功回包须 `payload=result.get('data')`）；
- **⚠️ "全域 unknown method"症状判定（v3.1 新增，最高优先；v3.5 补充第二个根因）**：若 **同一领域的所有**
  `<domain>.*` RPC 全部回 `unknown method` / 前端所有功能页同时报"数据加载失败请稍后重试"——**这不是逐个方法
  注册/命名问题，而是整个扩展加载失败了**。按序排查三个根因：
  ① 宿主进程/启动日志中 `ExtensionLoader` 是否有
  `TypeError: Can't instantiate abstract class <Domain>PlatformExtension with abstract methods initialize, shutdown`（漏实现抽象方法）；
  ② 宿主进程/启动日志中扩展加载处是否报 **`No module named 'jiuwenswarm.loaded_extension'`**（v3.5 finance 实坑：
  loader 以 `importlib.util.spec_from_file_location("jiuwenswarm.loaded_extension.<name>", entry)` 动态加载
  `extension.py`，磁盘上**不存在 `jiuwenswarm.loaded_extension` 父包**；若 `extension.py` 或其内部模块残留相对导入
  `from .xxx import` → 解析父包失败 → 整个扩展加载被跳过、全部 RPC 未注册。此时 `grep -n "from \." jiuwenswarm/extensions/<domain>_platform/`
  必命中的相对导入行即证据。对照先例 `video_duplex`/`finance_backend` 已改用绝对导入
  `from jiuwenswarm.extensions.<domain>_platform.<module> import ...`）；
  ③ 静态核 `extension.py`：扩展子类 `class <Domain>PlatformExtension(ApplicationPluginExtension)` 是否
  **完整实现基类抽象方法 `async def initialize(self, config)` 和 `async def shutdown(self)`**（`BaseExtension`
  是两个 `@abstractmethod`；漏实现时 Python 在 `FinancePlatformExtension()` 实例化处直接抛 TypeError，
  插件从未被注册 → 全部 RPC 不存在。**finance 实坑，已修复**，对照先例 `video_duplex`/`ManifestApplicationPlugin`）；
  命中 ①② 中任一（加载异常/导入解析失败）→ **REVISE 打回 domain-coding**：① 的修改点=补 `async def initialize` /
  `async def shutdown`（可仅打日志）；② 的修改点=将扩展内所有相对导入改为绝对导入
  `from jiuwenswarm.extensions.<domain>_platform.<module> import ...`（包括 extension.py 与 `<domain>_backend.py` 等
  内部模块的 `from .xxx`）；复验时重走①-插冒烟确认日志出现"扩展初始化完成/注册 N 个 RPC 方法"且 WS 回包 `ok:true`；
  若因环境未就绪（如缺依赖起不来）而无法冒烟 → 该项同样标「待部署后冒烟」，静态证据照记；
- 完成后立即停服，**本阶段不再重启**（后续前端页签可点/对话冒烟复用本次环境或按 ③ 的 WS/REST 黑盒全闭环执行）。

**② 领域前端功能冒烟（核心：界面可见可点可查、页签多时可滚）**
- 静态断言（无需运行时，逐条给证据）：
  - 断言 1：`SidebarNavKey` 注册了全部 `<domain>*` 页签 key，数量与 Spec 功能模块清单一致（**每功能一入口**）；
  - 断言 2：`SessionSidebar` 导航 items 与 `SidebarNavKey` 一一对应（左侧导航**多入口直挂**，全部可见可点）；
  - 断言 3：每个 `<domain>*` 页签在 `App.tsx` 中命中**一个独立渲染入口**（`return <Domain>XxxPanel/>`，
    禁止共享 `<Domain>WorkbenchPanel` + mode 参数打包多个功能；聚合首页不得替代各功能入口）；
  - 断言 4：无损坏/悬挂引用（页面组件文件存在且每功能独立、`<domain>Api.ts` 的 RPC 名与后端注册一致）；
  - **断言 4a（可滑轮）**：`SessionSidebar/index.tsx` 导航 items 外包 `<div className="icon-rail-nav-list">`，
    `SessionSidebar.css` 含 `.icon-rail-nav-list` + `overflow-y: auto` + flex 容器（页签超过可视高度可滚动）。
  - **断言 4b（任务对话可识别静态证据）**：协调技能 `*-swarm-coordination/SKILL.md` 含「委派映射」表且每个
    Spec 功能有一行（触发词→专员）；每个功能技能 SKILL.md 有「适用场景」+「调用 `<domain>.<x>.<y>`（参数）」；
    Agent persona「能做啥」枚举领域功能；技能引用的 RPC 与 `extension.py`/`<domain>Api.ts`/`dispatch_<domain>_rpc` 四处一致。
  - **断言 4e（主对话身份注入静态证据，v3.3）**：`prompt_builder.py` `_identity_prompt()` 含领域品牌身份 +
    「你能做什么」领域能力逐条枚举 + 「【强制·问候回复】」段（问"你是谁/你能做什么"必须输出领域回顾，禁止自称通用助手）。
  - **断言 4f（Agent 领域工具注册静态证据，v3.3）**：`interface_deep.py` `_get_tool_cards()` 存在
    `<domain>_gateway_call` 的 `_register_shared_tool` + `tool_cards.append(card)` 注册块（受
    `config_base.get("<domain>_gateway", {}).get("enabled")` 控制）；只定义工具文件但无注册块 → 判 REVISE。
  - **断言 4c（可执行通道段静态证据，v2.5）**：每个功能技能 SKILL.md 含「可执行通道段」——写清 REST 网关地址
    （`/api/<domain>/<action>`、端口 192xx）+ 方法 + **鉴权头（`X-API-Key: <api_key>`，api_key 来自 config
    `<domain>_gateway` 段，禁止硬编码）** + 参数 JSON 契约，或已注册的 Agent 直接可调领域工具（`<domain>_gateway_tool.py`）；
    只有"适用场景+调用 RPC"而无「可执行通道段」→ 对话**只聊不做**，判 REVISE。
  - **断言 4d（CSS 主题变量契约，v3.3 新增，gov 实坑：弹窗透明）**：对每个领域页面组件
    `frontend/src/components/<Domain><Feature>Panel/index.css`：
    ① `grep -n "var(--" *.css` 列出的变量，逐一核对是否在**构建产物 `frontend/dist/assets/index-*.css`
    `:root` 中真实定义**（打包运行的是 dist 而非 src——src 里没有、dist `:root` 里也没有的一律视为
    **未定义**）；② 每个 `var(--x)` 是否都带**兜底色值**（`var(--x, 兜底)`）；③ 弹窗/浮层内容
    （`.gov-modal__content` 等）背景是否**实色**（白卡/表面变量 + 圆角 + 阴影 + 边框，而非透明/复用
    页面磨砂背景）；④ 各 Panel 组件是否在 tsx 顶部 `import './index.css'`（**禁止隐式依赖其他面板
    全局 css 生效**）。❌ 出现未定义变量无兜底 / 弹窗背景透明 / 面板未自 import css → 判 REVISE。
  - **断言 4g（面板占位分配与布局排版契约，finance 排版实坑：`.fin-panel` 缺容器占位 →
    多面板无法等宽平铺）**：对每个领域页面组件 `<Domain><Feature>Panel/index.css`：
    ① 凡页面含**多面板/多卡片同屏平铺**（工作台、并列报表、概览类等，Spec 段 1「布局与排版规格」标注
    平铺/网格的），子面板/卡片样式**必须声明在宿主容器中的占位分配**——宿主 flex 则含
    `flex: 1 1 0%` + `width: 100%`、宿主 grid 则含 `repeat(auto-fit, minmax(…, 1fr))`；
    ❌ 只写 `display:flex; flex-direction:column; height:100%`（只有内部布局）而无外部占位 →
    **多面板按内容宽排列、不等宽/堆叠/溢出**（finance 实坑：记账凭证/资金池/资产负债/利润四面板
    在 flex 容器中排布错乱，补两行才平铺）；② 面板内容区须 `flex:1 1 auto; min-height:0; overflow-y:auto`
    （超高内容区域内滚，不撑破页面）；③ 禁止用 `overflow-x:hidden` 遮盖式解决横向溢出（不解决根因）。
    ❌ 命中 → 判 REVISE 打回 domain-coding（24aa 同判据，静态可验不依赖环境）。
- **界面品牌冒烟**：
  - 断言 5：左上角 logo 已从默认品牌替换为 `/<domain>-ai-logo.svg`（领域图标，如法务=天平）；
  - 断言 6：任务欢迎语 i18n 已领域化（`welcomeHeading/welcomeBubbleText/welcomeSuggestions`，zh/en 成对）；
  - 断言 7：「能做啥」回答 = 平台能力 + 领域能力（persona 或技能数据中有领域能力清单）。
- **技能可见性冒烟（v2.4）**：领域技能写在内置目录 `resources/agent/workspace/skills/`（`source='builtin'`）
  但未安装时**不会**出现在 UI「技能 → 我的技能」（前端 `computeMySkills` 排除未安装内置技能）。
  验收：① 部署指南/README 有 `skills.install <领域技能名>` 安装说明；②（仅环境就绪）执行后前端
  「技能 → 我的技能」可搜到该技能、可启用，`skills.list` 返回 `source='local'`——只写内置目录而无安装
  途径（UI 全不可见）判 REVISE。
- **可用性冒烟（静态项之外必须落实；前置门槛 = Q2-0 环境就绪判定）**：
  - 就绪时跑一次 `cd jiuwenswarm/channels/web/frontend && timeout 300 npm run build`（或 `tsc --noEmit`）编译通过
    → 前端可打包可部署；**未就绪/失败均不重试**（不 `npm install`、不补环境），记证据；
  - 不可运行时：需 coding 在文档/PR 注明前端的编译可用性；QA 至少确认无 TypeScript 报错级硬伤，该项标「待部署后冒烟」。
- **页签排序断言-双路径（无论可否运行时都必验证）**：领域页签可能走两条路径，**两条都必须落在
  `chat` 之后、`skills` 之前，且各领域页签顺序与 Spec 功能清单一致**：
  ① 静态数组 `domainFeatureNavItems` 紧跟 `chat` 后、`skills` 前，逐项顺序 = Spec 功能清单顺序；
  ② 应用插件动态页签 `applicationPluginItems` 在 `visibleMainNavItems` 组装处**同样拆出 chat/skills 插位**
  （按 `position` 排序后插在 chat 与 skills 之间）。**❌ 禁止 `[...mainNavItems, ...applicationPluginItems]`
  append 末尾**——gov 实坑：领域页签排到"更新"等平台页签之后、用户要滚到底才找得到，判 REVISE。
- 可运行时端到端闭环（**硬性，前置门槛 = Q2-0 环境就绪判定，就绪才启动**）：复用①-插已启动的服务
  （或单次前台 `timeout 300` 启动）完成后端就位，登录后左侧导航出现全部领域页签，顺序为
  「任务 → 领域页签 → 技能…」（判页签排序）；**对每个功能页完成完整操作闭环**
  「新增（表单提交→列表立即出现新行）→ 编辑（刷新显示最新值）→ 删除（行消失）→ 查询过滤」，
  并核对数据确实落库（重启服务后仍在）；`/api/<domain>/health` 返回 success。任一功能页"点了没反应"
  或"只显示但操作无效"即 REVISE。**未就绪/执行失败 → 不重试、不补环境**，该项在文档中标注「待部署后冒烟（交 sre 侧完成）」。
- **布局视觉自检（仅环境就绪时运行，判据=断言 4g）**：登录后浏览器打开**全部领域页签**，
  逐一确认：多面板页面**等宽平铺**（宽度一致、间距均匀、不堆叠不重叠）、页面**无横向滚动条**、内容超长时
  仅列表/表格区滚动、无整体竖向拉伸或错位。发现任一面板堆叠/不等宽/横向溢出/排版错乱 → 判 REVISE 打回
  domain-coding（引用 24aa/断言 4g：补面板容器占位 `flex:1 1 0%`/`width:100%` 或 grid 平铺并重新 build）。**未就绪 → 标「待部署后冒烟（交 sre 侧完成）」**。
- **24t 对话代办闭环（v2.5 新增，仅环境就绪时运行，防"只聊不做"）**：前置门槛 = Q2-0；
  **未就绪 → 该项整体标「待部署后冒烟」，仅做「可执行通道段」静态核验后交 sre**（主对话模型环境不可用不是
  coding 缺陷，不因冒烟失败打回）。就绪后在主对话向模型下达**真实代办指令**，断言
  **发生了真实 RPC 调用且数据落库可复核**，而不是模型只回复文本：
  - **查类**（如"帮我查一下安全生产相关的政策/法规"）→ 断言产生 `gov.policy.search` 实际请求并返回**可验证结果**
    （条目/总数字段），前端「政策法规库」页签可见检索结果；
  - **录类**（如"帮我录入这条政策：标题=…、文号=…、正文=…"）→ 断言产生 `gov.policy.import` 实际调用并**落库**
    （执行 `SELECT count(*) FROM <domain>_<table>`（或经 `<domain>.xxx.list` 复核）前后行数 **+1**，且新行内容与入参一致）；
  - **数据变化复核兜底**：对话前后对比 SQLite 库行数/内容（文档 `resources/` 或数据目录可查），行数不变且无新内容 → 模型"只聊不做" → 判 REVISE；
  - **怎么知道模型说了什么**：读取主对话会话记录/任务流日志，确认模型**提到了执行动作且数据确实变化**；
    仅"回复已查询/已录入但无任何调用与数据变化"即为 FAIL（技能缺可执行通道段，回 coding 补后在本步复验）。

**③ 领域扩展能力与代码质量**
- 关键字段抽查：扩展层注册方法名 / `<domain>Api.ts` RPC 名 / `dispatch_<domain>_rpc` 分支**三处一致**。
- **后端可用性抽查（新增，缺一击 REVISE）**：
  - 断言 8a：`extension.py` 中 `register_extensions` 为 **`async def`**（loader 以 await 调用，同步 def 扩展不会真正注册）；
  - **断言 8a3（扩展子类可实例化，v3.1 新增，缺一击 REVISE）**：`extension.py` 中
    `class <Domain>PlatformExtension(ApplicationPluginExtension)` 必须**完整实现基类两个抽象方法**
    `async def initialize(self, config)` 与 `async def shutdown(self)`（`BaseExtension` 的 @abstractmethod）。
    **漏实现 → 实例化 `FinancePlatformExtension()` 处直接抛
    `TypeError: Can't instantiate abstract class ... with abstract methods initialize, shutdown` →
    插件从未注册 → 整个领域全部 RPC 报 `unknown method`、前端所有功能页"数据加载失败"（finance 实坑，
    静态自检脚本已能拦截）；与 8a2 同样属"静态存在≠可用"类别，自检脚本 `check_extension_abstract_methods` 强制；
  - 断言 8a2：**RPC handler 契约为 `async def h(ws, req_id, params, session_id)` 且内部调用
    `send_response(ws, req_id, ok=..., payload=...)`**（`register_method` 包装为 4 参调用并等待自行回包；
    写成 `lambda p:` 或同步返回 dict → `TypeError`/前端无响应）；
  - 断言 8b：扩展内模块引用**绝对导入** `from jiuwenswarm.extensions.<domain>_platform.<module> import ...`
    且 `dispatch_<domain>_rpc` 存在（v3.5 finance 实坑：❌ 相对导入 `from .xxx import` 在 loader 动态模块名
    `jiuwenswarm.loaded_extension.<name>` 下报 `No module named 'jiuwenswarm.loaded_extension'` →
    扩展整体加载失败、该域全部 RPC unknown method；`grep -n "from \." extensions/<domain>_platform/` 应无输出）；
  - 断言 8b2：**扩展读取自身配置段必须走 `from jiuwenswarm.common.config import get_config`**——框架注入的
    `registry.config.config` 恒为 `{}`，从它判断 `<domain>_gateway.enabled` 会永远不启动网关（查 extension.py
    是否有 `get_config()` 调用，而非依赖构造函数 config 参数）；
  - 断言 8c：`<domain>_gateway` 配置段**同时存在于运行时生效的 `resources/config.yaml` 与 `config.example.yaml`** 且 `enabled: true`（只写 example 默认启动 REST 网关不加载）。
  - **断言 8d（任务对话可识别-RPC 四处一致）**：领域技能 SKILL.md 中引用的每个 `<domain>.<x>.<y>` 均能在
    `extension.py` 注册名 / `<domain>Api.ts` / `dispatch_<domain>_rpc` 中找到同名分支——模型按技能描述调用 RPC
    不会 404（只查三处同样会被"技能文档写错方法名"漏过）。
  - **断言 8e（任务对话可识别-冒烟，仅环境就绪，门槛=Q2-0）**：主对话对每个 Spec 功能下达指令（"生成一个财务报告"等），
    模型能识别功能归属→调用 RPC→SQLite 落库→前端对应页签出现新记录；任一功能识别不到即 REVISE；
    **未就绪 → 标「待部署后冒烟」交 sre，不做对话环境搭建。**
- 安全：无敏感信息提交（token/password 不可见）、无 `data/`、`configs/` 读取。
- 代码质量：目录命名、文件结构符合模板先例，无重复/冗余/坏味。
- 断言 9：SQLite 落库函数存在（建表 + 写入 + 查询），写操作有异常处理。

**④ 交付验收**
- 断言 10：Agent 配置文件存在（LEADER + N worker，与 Spec 角色表数量一致）、persona 「能做啥」完整；
- 断言 11：领域技能目录存在、SKILL.md frontmatter 完整、skill_name 唯一；
- 断言 12：文档四件套齐备且 README 品牌化（含 `<Domain>Swarm` 名称与 logo）；
- 断言 13：端口规划与 Spec 一致（网关 192xx，避开保留端口）；
- 断言 14：LICENSE 与开源声明原样保留（Apache-2.0 合规）。

#### Q3 判定 PASS / REVISE 并输出
- 全部 PASS → 评论输出「PASS（领域 Swarm 验收通过）+ 验收摘要」，验收结束（不发 A2A）。
- 任一 FAIL → 评论输出「REVISE（验收未通过）+ 具体修改点列表 + 复查入口（文件/路径/判据）」→
  **A2A 打回 domain-coding 继续处理**（修复后按场景 A 复检；涉及运行级/对话级的项，复检时重跑对应冒烟，不省略）。
- ⚠️ **重复 A2A 检查**：若 pipeline 已含 `coding:a2a_event`，不重复 A2A，只评论。
- **执行命令**：
```bash
# 防重复 + 发结论评论
grape issues pipeline --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" \
  --json '{"body":"[Domain QA Agent] {PASS/REVISE}（{验收/打回摘要}）\n{证据/REVISE 项列表}"}'
# REVISE：打回 coding
grape a2a handoff --json '{"agent":"coding","payload":{"repository":"$REPO_FULL_NAME","account":"$ACCOUNT","issue_number":'$ISSUE_NUMBER',"pr_url":"<pr_url>","domain":"<domain>","verdict":"REVISE","revise_items":[{...}],"allowed_agents":["coding","qa"],"context":"QA 验收未通过，请按 REVISE 项修复后复检"}}'
# 失败 → 重试 ≤3 次 → 仍失败：评论说明「A2A 服务不可达，待恢复后重试」+ next_step 人工介入
```

### 场景 B：修改请求（coding 修复后复检）
- 读取 domain-coding 修复后的复检请求：核对 REVISE 项是否全部修复，按 Q2 重跑对应项（运行级项**仅环境就绪时**重跑冒烟，否则仍按「待部署后冒烟」降级）并更新判定。
- **执行命令**：
```bash
# B0 读 coding 打回评论
grape git-repo issues comments list --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out
# B1 按 Q2 重跑对应项（运行级项仅环境就绪时），并按 Q3 输出 PASS/REVISE
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" \
  --json '{"body":"[Domain QA Agent] {PASS/REVISE}（复检）\n{核对 REVISE 项的 PASS/FAIL 列表 + 证据}"}'
# B2 防重复 + 交办（同 Q3）
grape issues pipeline --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out
# 已含 coding:a2a_event → 不重复，只发评论；否则按 Q3 verdict 选 a2a handoff
```

## 验收判据速查（VOD→Issue 高保真）
对照 <spec> 逐项高保真核对：<spec> 功能模块规格每项的
「左侧导航 key / 页面组件 / 后端 RPC / 落库表」四要素与 Spec 一致；品牌三件套（logo/欢迎语/能做啥）已落地；
**可用性三关**（后端可加载 `async def register_extensions` + 扩展子类**可实例化**（完整实现抽象方法
`initialize`/`shutdown`，漏实现 → `TypeError: Can't instantiate abstract class` → 全域 RPC `unknown method`，
finance 实坑）+ RPC handler 4 参契约
`async (ws,req_id,params,session_id)` 自行 send_response + **WS 成功回包 `payload` 必须是业务数据本体
`result.get('data')`，禁止整包下发 `{ok,data}` 双层（否则前端 `res.items` 取空、页面"暂无资产"——
finance 实坑，ws_smoke `--expect-key` 可拦截）** + 配置经 `get_config()` 读取 + 双配置
`config.yaml`/`example` 均 enabled、前端可编译 + 导航滚轮 `.icon-rail-nav-list`）缺一不可；
**页签排序**（领域页签 `domainFeatureNavItems` 紧贴 `chat` 之后、skills 之前，禁止 append 末尾）、
**端到端闭环**（环境就绪则每功能 WS/REST 黑盒 create→list→get→update→delete 全链路 `ok:true` +
前端页签操作闭环；未就绪标「待部署后冒烟」）为硬性必验；
**任务对话可识别 + 可执行七件套**（协调委派映射覆盖每功能 + 技能 SKILL.md 触发场景与 `<domain>.<x>.<y>` 调用 +
**技能「可执行通道段」（REST 网关地址/方法/鉴权头/参数契约，或已注册 Agent 领域工具）** +
persona 枚举功能 + RPC 四处一致 + **主对话身份注入（`prompt_builder.py` `_identity_prompt` 领域化 + 强制问候回复）** +
**Agent 领域工具注册（`interface_deep.py` `_get_tool_cards` 注册块，v3.3）**）同样缺一不可；
**24t 对话代办闭环**（主对话下真实代办指令→真实 RPC 调用→录类落库行数 +1 可复核→页签可见，
禁止"只聊不做"）为硬性必验。

## 操作边界

| 行为 | 态度 |
|------|------|
| 读验收清单/文献与 PR 改动 | ✅ **可以做** |
| 运行 Python 自检脚本 / 静态检查 | ✅ **必须做** |
| Q2-0 环境就绪判定（只读 import/版本/端口探测） | ✅ **必须做**（≤1min） |
| 后端启动 + `/health` + WS 黑盒（`ws_smoke.py` 现成脚本）单次冒烟 | ✅ **可以**（**仅环境就绪**；前台 + `timeout` 包裹，单次尝试，失败即降级标注） |
| 前端 `npm run build` / `tsc --noEmit` 可用性核对 | ✅ **可以**（仅环境就绪；单次尝试，不 `npm install`） |
| 对话识别/代办冒烟（24l/24t） | ✅ **可以**（仅环境就绪，主对话模型可达） |
| `git merge/pull/checkout/branch` / 恢复/切换分支 | ❌ **禁止**（不搬运代码、不改仓库状态） |
| 安装/补齐依赖（`uv pip install`/`pip install`/`npm install`/`uv sync`） | ❌ **禁止**（缺依赖 = 验收证据「待部署后冒烟」，不自己修） |
| 后台常驻进程（`nohup ... &`）/ 反复启停调试（查端口→读配置→重启重试） | ❌ **禁止**（一次性前台 timeout 启动，失败记一次证据后停止） |
| 为验证修改落地仓库文件/配置 | ❌ **禁止**（只复制自检/冒烟脚本进 `scripts/`，其余一律只读） |
| 修改 PR / 合并 / 部署 | ❌ **禁止** |
| 回复 Token/密码 | ❌ **禁止** |

## 硬约束

1. **验收对象=改后的领域项目**，不是领域技能本身（防止验收跑偏到"查技能定义"）。
2. **自检脚本必跑**：先跑自检（脚本来自本技能 `references/scripts/`，权威版），后逐项读证据；脚本失败项直接 REVISE。
3. **证据必须落地**：无证据该项不判 PASS。
4. **验收四条缺一不可**：静态/冒烟/扩展/交付；少一条不输出 PASS。
5. **Q3 判定必须二选一**：PASS 或 REVISE，不得"部分通过"。
6. **A2A / CLI 失败降级**：`grape a2a handoff` 非 2xx 或超时（CLI 退出码 ≥400 即 HTTP 透传）→ 重试至多 3 次；仍失败 → 评论注明，标记人工介入。
   其他 CLI 命令（`issues detail` / `pipeline` / `comments create` 等）出错时**先看完整 stderr**（去掉 `| tail` 截断，`2>&1` 保留 stderr）→ 不反复盲试同一命令；命令不识别时先 `grape refresh` 再重试。
7. **一条流水线只验收一次**：确认 payload 无 `qa:a2a_event` 才执行验收。
8. **可用性必验**：静态文件存在 ≠ 可用。后端扩展加载（`async def register_extensions` + **扩展模块绝对导入**
   （v3.5 finance 实坑，❌ 相对导入 `from .xxx` 报 `No module named 'jiuwenswarm.loaded_extension'` →
   扩展整体加载失败、全域 unknown method，见 24b）+
   **扩展子类可实例化**——`class <Domain>PlatformExtension(ApplicationPluginExtension)` 必须完整实现基类
   抽象方法 `async initialize`/`async shutdown`，漏实现 → `TypeError: Can't instantiate abstract class`
   → 插件未注册、全域 `unknown method`（finance 实坑；自检脚本 `check_extension_abstract_methods` 强制拦截））、
   **RPC handler 4 参契约**（`async def h(ws, req_id, params, session_id)` + 自行 `send_response`，
   非 lambda/同步返回）、**WS 成功回包 `payload` 契约**（必须是业务数据本体 `result.get('data')`，
   **禁止 `payload=result or {}` 整包下发 `{ok,data}` 双层**——前端 `webRequest` resolve 值即 payload、组件取
   `res.items`，整包导致数据库已落库但页面"暂无资产"，finance 实坑；在 `extension.py` 静态核 + ws_smoke `--expect-key`
   黑盒拦截）、**配置经 `get_config()` 读取**（非依赖 registry.config 空 `{}`）、
   双配置 `resources/config.yaml`/`config.example.yaml` 均 `<domain>_gateway.enabled=true`、
   前端可编译（`npm run build`/`tsc --noEmit`）、导航滚轮（`.icon-rail-nav-list` + `overflow-y: auto`）——
   任一缺证据不得判该域 PASS；环境未就绪/不可运行时至少确认静态前提并标注「待部署后冒烟」。
   新增 **页签排序断言**：`domainFeatureNavItems` 位于 `chat` 之后、`skills` 之前，**禁止 append 导航末尾**。
   运行级动作**必须在 Q2-0 环境就绪判定通过后**才允许尝试，且每项**最多 1 次、前台 + `timeout`**；无论就绪与否
   **都严禁 `git merge`/装依赖/后台常驻/为跑通改仓库**（越界即停止，转入静态核验 + 降级标注路径）。
9. **任务对话可识别 + 可执行必验**：功能"页面有页签"≠"对话可识别"、"识别了"≠"真的执行"。每个 Spec 功能必须
   五件套齐备——(a) 协调技能委派映射覆盖该功能触发词；(b) 功能技能 SKILL.md 有「触发场景 + 调用 `<domain>.<x>.<y>`」；
   (b2) **功能技能 SKILL.md 有「可执行通道段」**（REST 网关地址/方法/**鉴权头（api_key 来自 config，禁止硬编码）**/
   参数 JSON 契约，或已注册的 Agent 领域工具——模型拿到文档即能真实发起调用）；(c) persona 枚举领域功能；
   (d) 技能引用 RPC 与 `extension.py`/`Api.ts`/`dispatch` **四处一致**。
   任一功能缺项，或冒烟时模型无法识别→**真实**调 RPC→落库→页签可见，即 REVISE。
   必验 **24t 对话代办闭环**：主对话下达查/录类真实指令，看**落库数据变化/SQLite 行数**（非只看回复文本）——
   录类代办后行数应 +1 且内容可复核，查类应有实际请求与可验证结果；"回复已查询/已录入但零调用零落库"即 REVISE
   （account 实坑：发票/凭证页面在、WS RPC 通，但主对话"帮我登记发票"只回"好的已登记"、行数不变 → 工具未注册）。
   必验 **Agent 领域工具全量注册（24z，v3.4）**：`grep -l "gateway_tool" extensions/*_platform/*.py` 列出每个域，
   `interface_deep.py` `_get_tool_cards()` 必须有对应 `_register_shared_tool` 注册块（进程内工具无条件注册、
   不依赖 enabled），缺 1 → REVISE。
10. **端到端闭环必验（仅环境就绪时，硬性）**：通过 Q2-0 环境就绪判定后，对**每个功能页**必须跑完整操作闭环
    「新增（表单提交→列表立即出现新行）→ 编辑（刷新显示最新值）→ 删除（行消失）→ 查询过滤」，
    并配套 WS/REST 黑盒 `create→list→get→update→delete` 全链路 `ok:true`；任一功能"页面有但操作无效、
    点了没反应、数据不持久"即 REVISE，不得以"静态存在"充数。**环境未就绪 → 该项标注「待部署后冒烟」交 sre，
    不因无法启动而自行搭环境或反复重试。**
11. **验收超时降级（7.5，硬性）**：验收全程（Q1→Q3）预算 ≤15min。任一动态项超时/失败（服务启动/WS 黑盒/
    前端编译/对话冒烟）→ **立即停止该项冒烟，至多尝试 1 次**：以已通过项为准，在 REVISE 清单将
    「待测项」明确标注（如"对话冒烟待建模响应环境"/"运行环境未就绪，待部署后冒烟"），A2A 打回 domain-coding 补齐
    或交 domain-sre 部署侧验证；**禁止装依赖、禁止后台起服务、禁止无休止补验拖延全局超时**。
12. **角色边界（行为红线）**：QA = 「审」，不是「修」也不是「搭」。出现以下任一行为即视为越界，立即停止并回到
    静态核验路径：`git merge/pull/checkout` 改仓库；`uv pip install`/`pip install`/`npm install` 装依赖；
    `nohup`/`screen`/`tmux` 后台起服务；`start_services all` 后逐一查端口/读配置/重启调试；修改落地仓库文件。
    这些一律是**禁止动作**；一旦需要它们才能继续 → 该动态项判「待部署后冒烟」，交 sre，静态证据照记不误。

## 输出格式与任务收尾（必做）

业务动作完成后，先输出人类可读摘要（verdict/各组通过项/证据），再调用：

```bash
# PASS：不发 A2A，验收结束
grape tasks complete --json '{
  "result_text":  "QA 验收完毕（结论：PASS/REVISE）",
  "actions":      ["读取 PR + Spec", "Q2-0 环境就绪: {就绪/未就绪}", "Q2 四条验收: 静态/{N} / 冒烟/{M} / 扩展/{K} / 交付/{L}", "结论评论: {已发}", "A2A: {coding/无}"],
  "conclusion":   "{PASS — 验收通过，结束 / REVISE — 打回 coding（{n} 项）}",
  "artifacts":    ["评论: {PASS/REVISE 摘要}", "A2A handoff: {agent=coding, status=ok/降级（仅 REVISE 时）}", "evidence: {self_check_exit, health, ws_smoke, ...}"],
  "issue_summary":"{Domain}Swarm 验收（verdict={PASS/REVISE}，{通过数}/{总项数}）",
  "next_step":    "a2a_coding（仅 REVISE 时）"
}'
# REVISE：打回 coding——需手动再调 grape a2a handoff（见 Q3 命令块）
```

始终输出验收摘要 JSON（人类可读）：

```json
{
  "skill": "domain-qa",
  "summary": {
    "domain": "legal",
    "verdict": "PASS",
    "static_checks_passed": 16,
    "frontend_smoke_passed": 8,
    "brand_checks_passed": ["logo", "welcome", "howto"],
    "usability_checks_passed": [
      "后端async扩展入口", "扩展模块绝对导入", "双配置gateway-enabled",
      "前端编译通过", "导航滚轮容器overflow-y"
    ],
    "conversation_recognizable": {
      "delegation_mapping_ok": true,
      "skills_document_rpc_ok": true,
      "skills_have_executable_channel": true,
      "persona_enumerates_features_ok": true,
      "rpc_names_consistent_4_places": true,
      "identity_prompt_localized_ok": true,
      "agent_tool_registered_ok": true,
      "chat_smoke_features": ["生成财务报告->report.generate->落库可见", "登记资产->asset.create->资产页签可见", "你好/你能做什么->领域品牌名+能力概览"]
    },
    "conversation_delegation_closure": {
      "query_real_call": "帮我查安全生产政策->policy.search实际请求+返回可验证结果",
      "create_real_persist": "帮我录入政策->policy.import落库, SELECT count(*)前后行数+1, 新行内容可复核",
      "no_chat_only_fake": true
    },
    "ext_checks_passed": ["RPC四处一致(含技能引用)", "SQLite落库"],
    "delivery_checks_passed": ["agent配置", "技能", "文档", "端口", "LICENSE"],
    "evidence": {"self_check_exit": 0, "health": "200 OK", "ws_smoke_1write_1read": "ok:true", "nav_count": 7, "frontend_build": "passed", "nav_scroll": "overflow-y auto"},
    "ct7_inherited": true
  },
  "next_step": "验收结束，无下游交办。"
}
```

⚠️ **结构化字段必填**（结尾 = 下一节点的输入）：`actions[]`/`conclusion`/`artifacts[]`/`issue_summary` must 真实填充——只传 `result_text` 会丢结构化归档。

⚠️ **结束信号**：`grape tasks complete` 后立即结束任务。A2A 交办成功 ≠ 任务结束（仍需 complete）。

## 参考

| 文件 | 用途 | 先读 |
|------|------|------|
| `references/qa-checklist.md` | 验收四条完整清单与模板 | ⭐ 最先 |
| `references/qa-runbook.md` | **执行走查表：场景 A/B 全程命令逐步照抄 + 错误排查速查** | ⭐ 最先 |
| `references/grape-cli-installation.md` | grape CLI 安装/升级指引（前置门自愈用） | CLI 未装时 |
| `references/scripts/validate_domain_project.py` | 自检脚本，**权威版**，coding 侧为同一份副本 | 自检时 |
| `references/scripts/ws_smoke.py` | **WS 黑盒冒烟现成脚本**，接管 coding C7.2 的直接复用，禁止手写 WS 脚本 | 后端冒烟时 |
| `references/grape-cli-reference.md` | grape CLI 命令参考（含 QA 用到的子集） | 命令不确定时 |
| 需求/规格基线 | `../domain-vod/references/domain-requirement-guide.md`、`../domain-issue/references/domain-spec-guide.md` | 验收时 |
| 实施对照 | `../domain-coding/SKILL.md`（16 处改动速查 + 交付物清单；coding 不做运行级验收，复检确认交付物完整即可） | 复检时 |
| 前端嵌入先例 | `../domain-coding/references/domain-frontend-integration-guide.md` | 前端项验证时 |

> ⚠️ **任务结束**：完成 `grape tasks complete` 后立即结束任务，不要继续思考或等待输入。