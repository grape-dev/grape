# 领域 Swarm 验收清单（domain-qa v3.6）

> 配套 `skills/domain-qa/SKILL.md`
> v2.1：新增「五·四 前后端可用性」小节（后端扩展加载契约 + 运行时双配置 + 前端编译 + 导航滚轮）。
> v2.2：新增「五·五 任务对话可识别」小节（协调委派映射 + 技能触发场景/RPC 引用 + persona 枚举 + RPC 四处一致 + 对话识别冒烟）；
> v2.3：五·四新增 RPC handler 4 参契约 / get_config 配置读取 / 页签排序 / WS&REST 端到端闭环黑盒；前端操作闭环升级为完整增改删查。
> RPC 一致性检查由"三处"升级为"四处"（加技能 SKILL.md 引用）。
> v2.4：页签排序升级为**双路径检查**（静态数组 `domainFeatureNavItems` + 应用插件 `applicationPluginItems`，都必须在 chat 之后 skills 之前、禁止 append 末尾）；
> 新增「产品设计可用性」验收（表单校验/空态/错误态/loading/删除确认/分页）与「技能→我的技能」可见性验收（安装途径 + source=local）。
> v2.5：五·五升级为「任务对话可识别 + 可执行」——新增 `24b2` **可执行通道段**静态检查（REST 网关地址/方法/鉴权头/参数契约，
> 或已注册 Agent 领域工具）与 `24t` **对话代办闭环**验收（主对话下真实代办指令 → 真实 RPC 调用 → 录类落库行数 +1 可复核 → 页签可见；
> **禁止"只聊不做"**——模型只回文本不执行/零落库即 FAIL）。
> v2.6：五·四新增 `24p2` **WS payload 结构契约**验收（防"已落库但前端列表空白"finance 实坑）——
> WS 成功回包 payload 必须是业务数据本体（dispatch 返回 `{ok, data}` 中的 `data`），禁止整包下发 `{ok, data}` 双层包裹；
> `ws_smoke.py` 新增 `--expect-key` 断言业务字段、并内置双层包裹检测。
> v2.7：五·四新增 `24u` **扩展子类可实例化契约**验收（防"全域 RPC unknown method"finance 实坑）——
> 扩展子类 Must 完整实现基类抽象方法 `initialize`/`shutdown`，漏实现则扩展加载抛
> `TypeError: Can't instantiate abstract class ... with abstract methods initialize, shutdown`，
> 整个领域所有 RPC 未注册、前端全功能"数据加载失败"；识别到该问题 → REVISE 打回 domain-coding 补抽象方法实现。
> **v2.8（角色边界约束，配套 SKILL.md v3.2）**：新增「〇 环境就绪判定」作所有"可运行时"项的唯一前置门槛——
> 未就绪（缺依赖/缺配置/无法一次启动/端口占用）→ **不做任何环境搭建**（禁止 `git merge`、禁止安装依赖
> `pip/uv/npm install`、禁止 `nohup` 后台常驻、禁止反复启停调试），相关动态项整体标「待部署后冒烟」交 sre，
> 静态项照常全量执行；就绪的动态项**每项至多尝试 1 次、前台 + `timeout` 包裹**，失败一次即降级标注，不重试。
> **v2.9（CSS 主题变量契约，配套 SKILL.md v3.3 / coding v4.2，防"弹窗透明"gov 实坑）**：新增 `24v`——领域页面
> 组件样式**只准引用构建产物 `dist/assets/index-*.css` `:root` 真实定义的变量**，且每个 `var(--x)` 必须带
> 兜底色值；弹窗/浮层内容背景必须实色（白卡/表面变量+圆角+阴影+边框），禁止透明；各 Panel 必须自带 `index.css`
> 并在 tsx 顶部 import（禁止隐式依赖其他面板全局 css）。❌ `--color-bg`/`--color-text` 等未定义变量 → 弹窗透明判 FAIL。
> **v3.0（主对话身份 + Agent 工具注册 + 日期边界，配套 SKILL.md v3.3 / coding v4.2，防"问'你能做啥'答不出
> 领域功能、可执行通道形同虚设、date_to 查询必挂"gov 三实坑）**：新增 `24w`——主对话身份系统提示词领域化
> （`prompt_builder.py` `_identity_prompt`：领域品牌身份 + 「你能做什么」领域能力枚举 + 【强制·问候回复】，
> 禁止自称通用助手；**仅 extension hook / Agent persona 领域化不够**）；新增 `24x`——Agent 领域工具注册
> （`<domain>_gateway_tool.py` 用 `@tool` 定义后必须于 `interface_deep.py` `_get_tool_cards()` 注册
> `_register_shared_tool` + `tool_cards.append(card)`，受 `<domain>_gateway.enabled` 控制、日志含
> "tool registered"；**只定义不注册 = 模型工具链无此工具**）；新增 `24y`——后端通用 `_build_where` 日期
> 边界契约（`_to` 列名截取 `col[:-3]` 防 `no such column`；纯日期边界补全天时间戳防当天记录整批漏掉，
> `date_from=date_to=当天` 黑盒必须返回当天记录）。
> **v3.4（Agent 领域工具全量注册核对，配套 SKILL.md v3.4 / coding v4.3，防"页面/WS 全通但主对话代办只聊不做"
> account 实坑）**：新增 `24z`——全仓 `grep -l "gateway_tool" extensions/*_platform/*.py` 列出每个定义工具的域，
> 逐一验证 `interface_deep.py` `_get_tool_cards()` 有对应 `_register_shared_tool` 注册块；**进程内直调工具必须
> 无条件注册**（不依赖 `<domain>_gateway.enabled`，其调用不经 REST 端口）；凡"有工具文件无注册块"或"Spec 开放
> 对话代办但从未建 gateway_tool" 均判 FAIL。account 实坑：发票/凭证等页面齐全、WS RPC 全通，但工具未注册，
> 主对话"帮我登记一张发票"只回"好的已登记"、SQLite 行数不变（与 24t 双命中）。
> **v3.5（扩展模块导入契约，配套 SKILL.md v3.5 / coding v4.4，防"扩展整体加载失败 → 全域 unknown method"
> finance 实坑）**：改造 `24b` 为「**扩展模块导入契约-绝对导入**」——扩展内模块**必须绝对导入**
> （`from jiuwenswarm.extensions.<domain>_platform.<module> import ...`），禁止相对导入 `from .xxx import`
> （loader 以动态模块名 `jiuwenswarm.loaded_extension.<name>` 加载入口 `extension.py`，磁盘无 `jiuwenswarm.loaded_extension`
> 父包 → 入口相对导入报 `No module named 'jiuwenswarm.loaded_extension'` → 扩展整体加载失败、全域 RPC unknown method，
> finance 实坑：46 个 RPC 全丢）；静态自检脚本同步新增「扩展模块绝对导入」检查（入口 extension.py 含相对导入 → FAIL；
> 内部模块含相对导入 → WARN 提示统一改绝对导入）；`24u` 补充换根因分支②——日志报
> `No module named 'jiuwenswarm.loaded_extension'`（loader 动态模块名父包不存在）即入口相对导入残留，改绝对导入后复验；
> 运行冒烟须确认日志出现扩展加载成功（无该 No module 报错、注册 N 个 RPC），WS 黑盒全域 RPC `ok:true`。
> **面板占位分配与布局排版契约（24aa，防"多面板无法等宽平铺"finance 排版实坑）**：领域页面组件 css 必须
> **同时声明内部布局与外部容器占位**：多面板等宽平铺子面板须含 `flex: 1 1 0%` + `width: 100%`（宿主 flex）
> 或 `repeat(auto-fit, minmax(…, 1fr))`（宿主 grid），内容区 `flex:1 1 auto; min-height:0; overflow-y:auto`；
> 禁止只写 `display:flex; flex-direction:column; height:100%` 不管容器占位（finance 实坑：`.fin-panel` 缺两行
> → 记账凭证/资金池/资产负债/利润四面板 flex 容器中无法等宽平铺，补 `flex:1 1 0%` + `width:100%` 才恢复）；
> 环境就绪时浏览器逐一打开领域页签做**布局视觉自检**（等宽平铺/无堆叠/无横向溢出）；Spec 段 1 需含
> 「布局与排版规格」。

逐项检查 domain-coding 的实施成果。每项必填三态：
`✅ 已通过`（附佐证文件/命令）/ `⚠️ 警告`（不阻断但说明）/ `❌ 失败`（标原因，触发 REVISE）。

## 〇、环境就绪判定（v2.8 新增，所有"可运行时"项的前置门槛）

> 用只读命令一次判定（≤1min）。任一不满足 → 所有动态项降级为「待部署后冒烟」，静态项仍全量执行。
> **禁止**：`git merge/pull/checkout`；安装依赖（`pip/uv/npm install`）；`nohup` 后台起服务；反复启停调试；改仓库文件。

| # | 检查项 | 操作 | 判据 |
|---|--------|------|------|
| 0a | 工作区即验收代码 | `git status`/`git log`（只读） | 当前工作区已是 PR 改动后的代码，无需任何 git 操作 |
| 0b | 后端依赖已装 | `uv run python -c "import jiuwenswarm"`（或其等价） | import 直接成功，无需安装 |
| 0c | 前端依赖已装 | 检查 `frontend/node_modules` 存在 | 存在（不执行 `npm install`） |
| 0d | 端口可用 | `netstat`/替代只读探测 192xx/19000 | 目标端口未被占用 |

---

## 一、开源合规（硬性）

| # | 检查项 | 操作 | 判据 |
|---|--------|------|------|
| 1 | LICENSE 保留 | 检查仓库根目录 | LICENSE 存在且为 Apache-2.0 |
| 2 | 开源声明保留 | grep OPEN_SOURCE | 未删改 |

## 二、领域扩展层（硬性）

| # | 检查项 | 操作 | 判据 |
|---|--------|------|------|
| 3 | 扩展声明 | 读 `extensions/<domain>_platform/extension.yaml` | `id: <domain>_platform` + `version` |
| 4 | 扩展入口 | grep `register_extensions` in `extension.py` | 命中且可 import 不报错 |
| 5 | 业务层 | `<domain>_backend.py` / `<domain>_gateway.py` | 领域函数存在 |
| 6 | RPC 注册 | grep `"<domain>.` in extension.py | 与路由表一一对应 |

## 三、配置与端口（硬性）

| # | 检查项 | 操作 | 判据 |
|---|--------|------|------|
| 7 | config 领域段 | grep `<domain>_gateway` in `config.example.yaml` | 命中 |
| 8 | 端口不冲突 | 对比端口表 | 领域网关端口 ≠ 5173/1793/18092/19000/19001，且不重复 |

## 四、Agent 与技能（硬性）

| # | 检查项 | 操作 | 判据 |
|---|--------|------|------|
| 9 | Agent 配置数 | 计数 `resources/agent/<domain>_agent_configs/*.json` | ≥ LEADER + N（spec 角色表） |
| 10 | Agent JSON 字段 | 抽查 1 个文件 | id/agent_id/name/persona/skills 齐全 |
| 11 | 技能数量 | 计数 `workspace/skills/` 下领域技能 md | ≥ spec 技能清单项数 |
| 11a | 技能安装途径（v2.4） | 部署指南/README 搜 `skills.install` | 每个领域技能有对应安装命令（安装后 source=local，进「技能→我的技能」）；无安装途径判 FAIL | 
| 12 | SKILL.md frontmatter | 抽查 | `---\nname/description/steps/output` 结构完整 |

## 五、框架守约（硬性）

| # | 检查项 | 操作 | 判据 |
|---|--------|------|------|
| 13 | 框架核心未大改 | 对比模板核心 diff | 领域能力只落在扩展/领域配置目录 |
| 14 | 品牌一致性 | grep 模板/先例品牌串残留（非本领域） | 无 EduSwarm/其他领域品牌串残留 |

## 五·二、领域前端功能模块（硬性，改造的核心验收）

| # | 检查项 | 操作 | 判据 |
|---|--------|------|------|
| 15 | 页面组件存在 | `frontend/src/components/<Domain><Feature>Panel/` | **每功能一个独立组件**（数量=Spec 功能数）；WorkbenchPanel 可选（仅补充入口，不替代独立组件） |
| 16 | API 封装与服务 | `services/<domain>Api.ts` | `import { webRequest } from './webClient'`；RPC 名与后端注册**四处一致**（extension.py / Api.ts / dispatch / 技能 SKILL.md 引用） |
| 17 | 状态管理 | `stores/<domain>Store.ts` | 领域状态 + loading/error |
| 18 | 导航 key 注册 | `SidebarNavKey` 含 `<domain>*` | 每个功能点一个，数量与 spec 功能清单一致 |
| 19 | SessionSidebar 挂载 | `components/SessionSidebar/index.tsx` | 导航 items 与 SidebarNavKey 一一对应 |
| 20 | App.tsx 渲染 | `src/App.tsx` | 每个新 key **独立渲染对应页面组件**（`return <Domain>XxxPanel/>`；禁止 mode 打包多功能为一个组件） |
| 21 | i18n 成对 | `i18n/locales/{zh,en}.json` | `nav.<domain>*` + 欢迎语 key，zh/en 均存在 |
| 21a | **导航滚轮** | `SessionSidebar/index.tsx` items 外包 `<div className="icon-rail-nav-list">`；`SessionSidebar.css` 含 `.icon-rail-nav-list` + `overflow-y: auto` | 页签超过可视高度可滚动、全部页签可见可点 |

## 五·三、界面品牌三件套（硬性，缺一 REVISE）

| # | 检查项 | 操作 | 判据 |
|---|--------|------|------|
| 22 | 左上角 logo 领域化 | `SessionSidebar/index.tsx` 顶部 import + `public/<domain>-ai-logo.svg` | logo 引用已替换为领域图标（如法务=天平），文件存在可加载 |
| 23 | 任务欢迎语领域化 | i18n `welcomeHeading/welcomeBubbleText/welcomeSuggestions` | 已替换为领域文案（zh/en 成对） |
| 24 | 「能做啥」=平台+领域 | Agent persona / 领域技能数据 | 回答含平台能力 + 领域能力清单 |

## 五·四、前后端可用性（v2.1 新增，硬性，缺一击 REVISE）

> 原则：**静态文件存在 ≠ 可用**。以下任一缺证据不得判该域 PASS。

| # | 检查项 | 操作 | 判据 |
|---|--------|------|------|
| 24a | 后端扩展入口契约 | `extension.py` 搜 `register_extensions` | 必须为 **`async def register_extensions`**（loader 以 `await` 调用；写 `def` 扩展不会真正注册，功能不可用） |
| 24b | **扩展模块导入契约-绝对导入（v3.5 新增，防"扩展整体加载失败→全域 RPC unknown method"finance 实坑）** | `grep -n "from \." jiuwenswarm/extensions/<domain>_platform/`（应无输出）；`extension.py` 头部应为 `from jiuwenswarm.extensions.<domain>_platform.<module> import ...` | **❌ 相对导入 `from .<domain>_backend import ...` 在 loader 动态模块名 `jiuwenswarm.loaded_extension.<name>` 下报 `No module named 'jiuwenswarm.loaded_extension'`**（磁盘无此父包）→ 整个扩展加载被跳过、该域全部 `<domain>.*` RPC 未注册、前端全功能 `unknown method`（finance 实坑：46 个 RPC 全丢）。扩展内所有模块（extension.py + `<domain>_backend.py` 等）必须**绝对导入** `from jiuwenswarm.extensions.<domain>_platform.<module> import ...`；命中 → **REVISE 打回 domain-coding 改绝对导入**，复验重跑①-插冒烟无 `No module named` 报错 |
| 24c | **运行时双配置** | 同时检查 `resources/config.yaml` 与 `config.example.yaml` | 两份都有 `<domain>_gateway:` **且** `enabled: true`；只写 example 默认启动后 REST 网关不加载、`/api/<domain>/health` 不可用 |
| 24d | REST 健康检查 | 仅环境就绪（门槛=〇节）：`timeout 60 curl /api/<domain>/health`；未就绪静态确认路由存在 | 返回 `{"success": true, ...}`（就绪时）；未就绪标「待部署后冒烟」 |
| 24e | 前端可编译 | 仅环境就绪（门槛=〇节，`node_modules` 已存在）：`cd jiuwenswarm/channels/web/frontend && timeout 300 npm run build`（或 `tsc --noEmit`）；**失败不重试、不 `npm install`** | 编译/类型检查无错误（未就绪至少确认无 TS 硬伤，标「待部署后冒烟」） |
| 24f | 导航滚轮可用 | `SessionSidebar/css`：`.icon-rail-nav-list` + `overflow-y: auto`，items 外包该 div | 页签超过可视高度时可滚动、底部页签可达可点 |
| 24g | 闭环可操作（仅环境就绪） | 复用①-插服务：登录 → 每个页签点进 → 完成一次「增/查/落库」 | 全页签可点、每功能页面可用；未就绪标注「待部署后冒烟」 |
| 24m | **RPC handler 契约（v2.3）** | `extension.py` 搜 `register_method` | handler **必须 `async def (ws, req_id, params, session_id)`** 且内部自行 `await channel.send_response(ws, req_id, ok=..., payload=...)`；❌ `lambda p:` / 同步返回 dict → `TypeError`、前端永远无响应 |
| 24n | **配置经 get_config() 读取（v2.3）** | `extension.py` 搜 `get_config` | 扩展读取自身配置段必须 `from jiuwenswarm.common.config import get_config` 再取 `<domain>_gateway`；❌ 依赖 `registry.config`（`.config` 恒为 `{}`）→ 网关永不启动 |
| 24o | **页签排序-双路径（v2.4）** | `SessionSidebar/index.tsx` 搜 `domainFeatureNavItems`、`applicationPluginItems`、`visibleMainNavItems`、`mainNavItems` | **两条贡献路径都必须插在 `chat` 之后、`skills` 之前**：（1）静态数组 `domainFeatureNavItems` 紧跟 `chat` 后，**各页签顺序与 Spec 功能清单一致**；（2）应用插件动态页签 `applicationPluginItems` 在 `visibleMainNavItems` 组装时**同样拆出 chat/skills 插位**（按 `position` 排序后插入 chat 与 skills 之间）；**❌ 禁止 `[...mainNavItems, ...applicationPluginItems]` append 末尾**（gov 实坑，只会导致所有领域页签排到"更新"等平台页签之后，判 FAIL） |
| 24p | **端到端闭环黑盒（v2.3，仅环境就绪硬验，门槛=〇节）** | 复用①-插已启动服务（单次前台 `timeout 300`，**禁止后台常驻**）→ asyncio 脚本连 Gateway WS `ws://127.0.0.1:19000/ws` 发 `{"method":"<domain>.<entity>.<action>","params":{...},"request_id":N}`；REST 走 `/api/<domain>/*` | 对**每个功能**跑通 `create→list(新行可见)→get(详情)→update→list(最新)→delete→list(移除)` 且回包 `ok:true`；任一功能调不通即 REVISE，**只看 dispatch 静态存在不算通过**。可先复用现成黑盒脚本 `references/scripts/ws_smoke.py`（1 写 + 1 读抽查，**读抽查传 `--expect-key items` 或列表字段**，接管 coding C7.2）再用 24t/前端页签补齐全量；**未就绪/启动失败 → 该项标「待部署后冒烟」，不重试、不装依赖、不调启动** |
| 24p2 | **WS payload 结构契约（v2.6 新增，防"已落库但列表空白"实坑）** | 查 `extension.py` 成功回包写法 + `ws_smoke.py` 读抽查传 `--expect-key <业务字段>` | **WS 成功回包 `payload` 必须是业务数据本体**（`<domain>_backend` 的 `dispatch_<domain>_rpc` 返回 `{ok, data}`，须解出 `data` 作为 payload 下发，与 REST 网关 `result.get("data")` 同口径）；**❌ 禁止 `payload=result or {}` 整包下发 `{ok,data}` 双层包裹**——前端 `webRequest` 的 resolve 值恰是 payload、组件直接取 `res.items`，整包会导致数据库已落库但页面显示"暂无资产"（finance 实坑）。`ws_smoke.py` 已内置双层包裹检测 + `--expect-key` 业务字段断言 |
| 24q | 前端功能操作闭环（v2.3，仅环境就绪） | 复用①-插服务，每个功能页：新增表单→列表立即出现新行；编辑→刷新最新值；删除→行消失；关键字查询过滤 | 全部闭环可操作且重启服务后数据仍在（SQLite 持久化）；页面有但"点了没反应"即 REVISE。**若弹窗提示"登记成功"但列表仍"暂无资产"**——先查 24p2：后端多半整包下发 `{ok,data}` 双层包裹（数据库已落库、前端取不到业务字段），非前端改动可解决，须回后端 `payload=result.get('data')`；**未就绪 → 标「待部署后冒烟」交 sre** |
| 24r | **产品设计可用性（v2.4）** | 逐功能核对 spec 段 1 产品设计与实现对照 | 表格/新增编辑表单：**必填与校验生效**（空值/超长/唯一性被拦截并红字提示）；**空态**（无数据文案+引导）；**loading**（拉取/提交中按钮禁用或 spin）；**删除二次确认**；**错误态**（`ok:false` 红字提示，不静默）；**分页**（>page_size 可分页浏览）。gov 式"能看不能用的壳子"（无校验/无空态/无 loading/失败不提示）判 FAIL |
| 24s | **技能→我的技能可见性（v2.4）** | `快速部署指南.md`/README 是否有 `skills.install <领域技能名>` 安装说明；运行时（仅环境就绪）在 UI「技能→我的技能」搜索 | 每个领域技能都**有安装到"我的技能"的途径**；安装后 `skills.list` 中 `source='local'`，「我的技能」可搜到可启用；❌ 只写内置目录 `resources/agent/workspace/skills/` 而无安装说明/入口（UI 全不可见）判 FAIL；未就绪只查静态安装途径 |
| 24u | **扩展子类可实例化契约（v2.7 新增，防"全域 RPC unknown method"finance 实坑）** | `extension.py` 中 `class <Domain>PlatformExtension(ApplicationPluginExtension)` 是否**完整实现基类两个抽象方法** `async def initialize(self, config)` 与 `async def shutdown(self)`；运行冒烟时抓日志中 `Can't instantiate abstract class` | **漏实现 → 实例化处抛 `TypeError: Can't instantiate abstract class ... with abstract methods initialize, shutdown` → 插件从未注册 → 整个领域所有 `<domain>.*` RPC 全部 `unknown method`、前端所有功能页同时"数据加载失败请稍后重试"**（finance 实坑）；**v3.5 补另一根因**：日志报 `No module named 'jiuwenswarm.loaded_extension'` → 扩展内残留相对导入 `from .xxx`（loader 动态模块名父包不存在，见 24b）→ 扩展同样整体加载失败、全域 RPC unknown method。症状判别：**同一领域全部 RPC 同时挂** = 扩展没加载成功，非逐个方法命名问题；按序排查日志：① abstract-method `TypeError`（漏实现抽象方法）→ ② `No module named 'jiuwenswarm.loaded_extension'`（相对导入残留）→ 再静态核子类是否实现 `initialize`/`shutdown` 与扩展模块是否绝对导入（对照先例 `video_duplex`/`ManifestApplicationPlugin`）。命中根因① → **REVISE 打回 domain-coding，修改点明确：给扩展子类补 `async def initialize` / `async def shutdown`（可仅打日志）**；命中根因② → **REVISE 打回 domain-coding，修改点明确：扩展内模块一律改绝对导入 `from jiuwenswarm.extensions.<domain>_platform.<module> import ...`**；复检重跑①-插冒烟，确认日志"扩展初始化完成/注册 N 个 RPC 方法"且 WS 抽查 `ok:true` |
| 24v | **CSS 主题变量契约（v2.9 新增，防"弹窗透明"gov 实坑，纯静态不受环境就绪影响）** | 对每个领域页面组件 `frontend/src/components/<Domain><Feature>Panel/index.css` 静态核验：① `grep -rn "var(--"` 列出的变量逐一与**构建产物 `dist/assets/index-*.css` 的 `:root`**（运行时真相，非 src）比对是否为**真实定义**；② 每个 `var(--x)` 是否带**兜底色值**（`var(--color-surface-elevated, #fff)` 形）；③ 弹窗/浮层内容类（`.gov-modal__content` 等）背景是否**实色**（白卡/表面变量 `--color-surface-elevated` + 圆角 + 阴影 + 1px 边框），禁止透明或复用页面磨砂背景；④ 各 Panel 组件 tsx 顶部是否 `import './index.css'`（禁止隐式依赖其他面板全局 css） | **❌ 引用 `--color-bg` / `--color-text` / `--color-accent` 等"直觉命名但未定义"变量且无兜底值 → `var()` 解析失败、背景透明、弹窗整块透明不可用**（gov 实坑：9 个占位变量全未定义）。抓到时 → **REVISE 打回 domain-coding**：仅保留主题真实变量（`--color-surface-*`/`--color-text-primary`/`--color-action-primary`/`--color-border-default`）+ 全部带兜底；弹窗内容实底渲染；Panel 自 import css；改完需重新 `npm run build`（前端服务读 dist） |
| 24w | **主对话身份注入（v3.0 新增，防"问'你能做啥'答不出领域功能"gov 实坑，纯静态不受环境就绪影响）** | `agents/harness/common/prompt/prompt_builder.py` 搜 `_identity_prompt` | zh/en 身份段均已领域化：「# 身份」为 `<Brand>` 领域身份（"你是由 `<平台名>` 提供能力的 `<领域>` AI 助手"）；「## 你是谁」明确"不是通用聊天机器人"；「## 你能做什么」**逐条枚举领域能力**（与左侧页签/`<domain>.*` RPC 同口径，含引导例句）；含「**【强制·问候回复】**」段（用户发你好/你是谁/你能做什么时必须输出领域自我介绍+能力概览，不得省略、不得自称通用助手）。❌ 仍为通用 JiuwenSwarm 身份 / 缺强制问候段 → 主对话问"你能做啥"不回领域功能，判 FAIL（**仅 extension hook / Agent persona 领域化不通过**——主对话身份唯一入口是 `_identity_prompt`，gov 实坑） |
| 24x | **Agent 领域工具注册（v3.0 新增，防"可执行通道段形同虚设"gov 实坑，纯静态不受环境就绪影响）** | 若存在 `extensions/<domain>_platform/<domain>_gateway_tool.py`，则 `server/runtime/agent_adapter/interface_deep.py` 搜 `<domain>_gateway_call` | 工具文件用 `@tool` 装饰器定义后，`interface_deep.py` `_get_tool_cards()` 必须含注册块：`self._register_shared_tool(<domain>_gateway_call)` + `tool_cards.append(<domain>_gateway_call.card)`，受 `config_base.get("<domain>_gateway", {}).get("enabled")` 控制，注册路径打 `<Domain>Swarm <domain>_gateway_call tool registered` 日志（运行级可复核）。❌ 只定义未注册 / 注册未受 enabled 控制 → 模型工具链无此工具，技能「可执行通道段」全部落空，判 FAIL |
| 24z | **Agent 领域工具全量注册核对（v3.4 新增，防"页面/WS 全通但主对话代办只聊不做"account 实坑，纯静态不受环境就绪影响）** | `grep -l "gateway_tool" extensions/*_platform/*.py` 全仓列出每个用 `@tool` 定义工具的领域；再在 `server/runtime/agent_adapter/interface_deep.py` 搜 `_register_shared_tool` | 每个 `*_gateway_tool.py` 都必须在 `interface_deep.py` `_get_tool_cards()` 有对应注册块（import → `_register_shared_tool(...)` → `tool_cards.append(...card)` → 日志）。**进程内直调工具必须无条件注册**（try/except 包裹、不依赖 `<domain>_gateway.enabled`——进程内调用不经 REST 端口，enabled 缺省/关闭不应让工具从模型工具链消失）。❌ 有工具文件无注册块 / Spec 开放了对话代办但该域从未建 gateway_tool → 判 FAIL（account 实坑：发票/凭证等页面齐全、WS RPC 全通，但工具未注册，主对话"帮我登记一张发票"只回"好的已登记"、SQLite 行数不变，24t 同命中） |
| 24y | **后端 where 日期边界契约（v3.0 新增，防"date_to 查询必挂/漏整天"gov 实坑；静态必查，黑盒仅环境就绪）** | `<domain>_backend.py` 搜 `_build_where` 与日期边界处理（`_normalize_date_bound` 或等价逻辑）；仅环境就绪时对代表功能传 `date_from`/`date_to` 同一天跑黑盒 | 静态：① `_to` 后缀列名截取必须 `col[:-3]`（删 `"_to"` 3 字符）；❌ `col[:-2]` → 生成 `created_at_` → `no such column`，**所有 `date_to` 查询必挂**（`_from` 为 `col[:-5]` 删 5 字符无此问题）；② 纯日期 `YYYY-MM-DD` 值必须补全天边界（`_from` + ` 00:00:00`、`_to` + ` 23:59:59`），带时间值原样——否则字符串比较 `'2026-09-21 10:07:26' <= '2026-09-21'` 为 False，**当天记录整批漏掉**。仅环境就绪黑盒：`{"date_from":"2026-09-21","date_to":"2026-09-21"}` 必须返回**当天全部记录**（gov 实测 petition.search 应返 2 条）；返回空/报错 → REVISE 打回 domain-coding |
| 24aa | **面板占位分配与布局排版契约（防"多面板无法等宽平铺"finance 排版实坑，静态必查）** | 对每个领域页面组件 `frontend/src/components/<Domain><Feature>Panel/index.css` 静态核验（对照 Spec 段 1「布局与排版规格」）：① 多面板/多卡片同屏平铺页面（工作台/并列报表/概览等），子面板/卡片样式**必须含宿主容器占位分配**——宿主 flex 则含 `flex: 1 1 0%` + `width: 100%`、宿主 grid 则含 `repeat(auto-fit, minmax(…, 1fr))`；② 面板内容区 `flex:1 1 auto; min-height:0; overflow-y:auto`；③ 禁止 `overflow-x:hidden` 遮盖式解决横向溢出 | **❌ 只写 `display:flex; flex-direction:column; height:100%`（仅内部布局）而无外部占位 → 多面板按内容宽度排列、不等宽/堆叠/溢出**（finance 实坑：`.fin-panel` 缺 `flex:1 1 0%` + `width:100%`，记账凭证/资金池/资产负债/利润四面板 flex 容器中排布错乱，补两行才平铺）→ **REVISE 打回 domain-coding**：面板/卡片补容器占位（`flex:1 1 0%` + `width:100%` 或 grid 平铺）并重新 `npm run build` 复验。仅环境就绪加做**布局视觉自检**：浏览器逐一打开领域页签——多面板等宽平铺（宽度一致/间距均匀/不堆叠）、页面无横向滚动条、内容超长仅列表区滚动；发现问题 → REVISE。未就绪标「待部署后冒烟（交 sre 侧完成）」 |

> 原则：**功能"前端有页签" ≠ "任务对话能识别" ≠ "识别了就能真执行"**。用户在主对话下达功能指令
> （"生成一个财务报告""帮我查一下安全生产政策""帮我录入这条政策"）时，
> 模型必须能经委派映射识别归属 → 按技能 SKILL.md 调用对应 `<domain>.<x>.<y>` RPC（**真实发起**）→ 落库 → 前端页签可见。
> 以下五件套每功能必查（对照 Spec 功能清单逐功能过），另加 `24t` **对话代办闭环**防"只聊不做"。

| # | 检查项 | 操作 | 判据 |
|---|--------|------|------|
| 24h | 协调技能委派映射 | `workspace/skills/*-swarm-coordination/SKILL.md` 搜「委派映射」 | 存在委派映射表，且**每个 Spec 功能至少一行**（功能触发关键词 → 对应专员 id）；LEADER 据此识别任务归属 |
| 24i | 功能技能触发场景 + RPC | 每个功能技能 SKILL.md | 「适用场景/触发词」（如"用户要生成报表"）+「调用 `<domain>.<x>.<y>`（参数）」明确写出；仅描述业务不写 RPC 判失败 |
| 24i2 | **可执行通道段（v2.5 新增）** | 每个功能技能 SKILL.md 搜「可执行通道」 | SKILL.md 含**「可执行通道段」**——写清 REST 网关地址（`/api/<domain>/<action>`、端口 192xx）+ 方法 + **鉴权头（`X-API-Key: <api_key>`，api_key 来自 config `<domain>_gateway` 段，禁止硬编码）** + 参数 JSON 契约（与 `<domain>.<x>.<y>` params 同名同型）；或已注册的 Agent 直接可调领域工具（`<domain>_gateway_tool.py`）。只有"触发场景+RPC"而无「可执行通道段」→ 对话**只聊不做**，判 FAIL |
| 24j | Agent persona 枚举功能 | `resources/agent/<domain>_agent_configs/*.json` 的 persona |「能做啥」逐条列出领域功能（生成财务报告/资产台账/…），与 `team.list`/`skills.list` 返回同口径 |
| 24k | RPC 四处一致（含技能引用） | 技能 SKILL.md 中出现的每个 `<domain>.<x>.<y>` | 与 `extension.py` 注册名 / `<domain>Api.ts` / `dispatch_<domain>_rpc` 分支同名存在——模型按技能描述调用不 404 |
| 24l | 对话识别冒烟（仅环境就绪，门槛=〇节） | 主对话对每个 Spec 功能下达指令（主对话模型可达才做） | 模型识别功能归属→**真实调用** RPC（看调用日志/落库，非只看回复文本）→SQLite 落库→前端对应页签出现新记录；任一功能识别不到即 REVISE；未就绪标「待部署后冒烟」，静态核「可执行通道段」后交 sre |
| 24t | **对话代办闭环（v2.5 新增，仅环境就绪时硬验，防"只聊不做"）** | 门槛=〇节；未就绪 → 该项标「待部署后冒烟」（不因模型环境不可用打回 coding）。就绪后主对话下达**真实代办指令**（查类与录类各 1）：如"帮我查一下安全生产相关政策""帮我录入这条政策：标题=…文号=…正文=…" | 录类：确认真实 `import` 调用并**落库**——对话前后对 `SQLite` 执行 `SELECT count(*)`（或经 `<domain>.xxx.list` 复核）行数 **+1** 且新行内容与入参一致、前端「政策法规库」页签可见；查类：确认真实 `search` 调用并返回**可验证结果**（条目/总数）。仅回复"已查询/已录入"但**零调用、零落库数据变化**（行数不变、无新内容）即 REVISE——技能缺可执行通道段，回 coding 补后在本步复验 |

## 六、文档与自检（软性 + 硬性结论）

| # | 检查项 | 操作 | 判据 |
|---|--------|------|------|
| 25 | README 品牌化 | 读根 README 标题 | 含 `<Domain>Swarm`/品牌名 |
| 26 | 文档四件套 | 快速部署/架构图/经验分享 | 推荐存在（缺失记警告） |
| 27 | **自检脚本实跑** | 自本技能 `references/scripts/`（**权威版**，coding 交付副本为同一份）复制到落地仓库 `scripts/` 后，运行 `python scripts/validate_domain_project.py <repo> --domain <domain>` | 退出码 0（硬性，纯静态不依赖网络/服务，**不受环境就绪门槛影响，始终执行**）；脚本缺失/不可运行 → REVISE 回 coding 补齐 |

## 七、输出模板（验收评论）

```markdown
[Domain QA Agent] 验收完成 —— 结果：PASS / REVISE

### 验收清单
| # | 检查项 | 结果 | 佐证 |
|---|--------|------|------|
| 1 | LICENSE 保留 | ✅ 已通过 | 仓库根 LICENSE（Apache-2.0） |
| ... | ... | ... | ... |

### 关键佐证
- 自检脚本退出码：0 / 非 0（附输出行）

### 结论
- PASS：验收通过，结束（不发 A2A）。
- REVISE：❌ 项清单 + 复查入口（文件/路径/判据）。
```