# SRE Agent 个人机器专属逻辑 — 设计文档

> 状态：待主人审批（未写任何代码）
> 日期：2026-10-14
> 范围：场景创建（dashboard.js）+ 场景详情/编辑（dashboard.js）+ 引擎列表数据（dashboard.py）+ 添加设备面板改造（runtimes.js）+ 后端校验（scenes.py）

---

## 一、现状梳理（基于本地代码核实）

| 环节 | 现状 | 位置 |
|---|---|---|
| 引擎数据源 | `GET /api/dashboard/{owner}/{repo}` 返回 `agent_engines[agent_type]`，每个 agent 类型混查 global+user+repo+local 四种 scope | `dashboard.py:1187-1218` |
| 场景创建 | editNodes 默认全部 agent `enabled:true`，SRE 不特殊 | `dashboard.js:1570` |
| 引擎选择弹窗 | `_openEngineModalImpl` 按 scope 分组展示所有引擎，SRE 无过滤 | `dashboard.js:1340+` |
| 引擎校验 | `_validate_scene_engine_ids` 只校验 repo 归属 + tag 能力，不校验 scope/owner | `scenes.py:1438` |
| 添加设备 | 右侧滑出侧边栏 `openAddDevicePanel`，内含「生成凭据」+「AI Devspace」两个折叠项 | `runtimes.js:546` |
| AK/SK | `GET/PUT /api/devstage/config`（`ak_exists/sk_exists` 掩码回显），通用设置页有输入 UI | `devstage.py:78` / `settings.js:26` |
| 离线徽章 | `_engineBadgeHtml` 已支持「绑定失效 · 全部离线」红徽章 | `dashboard.js:918` |
| 任务派发 | `pick_engine`：plugin 模式空列表=广播，不按 agent 类型限 scope | `engine_select.py` |

---

## 二、可行性评估

**结论：可行，但有两处需要主人拍板的设计决策（见 §六），一处深水区风险（见 R3）。**

已具备的基础设施：
1. `agent_engines` 数据已带 `scope`/`status` 字段，前端过滤个人引擎零成本；
2. AK/SK 配置接口和 UI 已存在，可直接复用做弹窗内嵌输入；
3. AI Devspace 侧边栏是纯 DOM 动态构建（`openAddDevicePanel`），改成弹窗只需换容器样式和动画，内部两块折叠区逻辑不动；
4. 全部离线红徽章 `_engineBadgeHtml` 已存在，SRE 只需在它之上叠「点击重新唤醒」。

---

## 三、目标行为定义

### 3.1 SRE 引擎列表只显示个人级
- 场景创建/编辑的 SRE 引擎选择弹窗：只列出 `scope==='user'` 且属于当前用户的引擎。
- 后端 `agent_engines['sre']` 只返回 user scope（双保险，前端过滤只做展示兜底）。
- 其他 agent（vod/issue/coding/qa）行为完全不变。

### 3.2 场景创建时 SRE 默认置灰
- 新建场景 editNodes：sre 节点初始 `enabled:false`（其余 agent 维持现状）。
- 置灰样式复用现有 `opacity:.35;filter:grayscale(1)`。

### 3.3 唤醒逻辑（点击 SRE 的 ✓ 启用时触发）
```
唤醒 SRE
  → 拉取个人引擎列表（agent_engines.sre，scope=user）
  ├─ 有在线个人机器 → 自动全选所有属于该用户的在线个人机器
  │   （engine_runtime_ids 直接填入，引擎徽章即时显示台数）
  └─ 无（无个人机器或全部离线）→ 弹出「无个人机器」提示弹窗（见 3.4）
```
- 「自动全选」只选 `status==='online'` 的；离线的个人机器不自动选（选了也无法派发，符合失败不兜底哲学）。
- 用户之后仍可打开引擎弹窗手动增删，但列表里只有个人级。

### 3.4 无个人机器弹窗（两个入口）
```
┌────────────────────────────────────┐
│  ⚠ 没有可用的个人机器            │
│  SRE Agent 只能运行在个人级机器上， │
│  请先连接个人机器。                 │
│                                    │
│  [☁ 华为云 AK/SK 获得免费运行时]  │
│  [🔑 我有个人机器，直接托管]      │
└────────────────────────────────────┘
```
- 按钮 A（华为云）：先查 `GET /api/devstage/config`：
  - 已有 AK/SK → 跳过输入，直接进入「添加设备」弹窗（AI Devspace 区默认展开）；
  - 无 AK/SK → 先弹 AK/SK 输入弹窗（含「如何获取 AK/SK」引导链接），提交走现有 `PUT /api/devstage/config`（与通用设置完全同步，同一存储），成功后自动进入「添加设备」弹窗。
- 按钮 B（直接托管）：进入「凭据生成」弹窗（复用现有 `toggleAddDeviceCred` 的凭据生成 UI，搬到弹窗容器）。
- 关闭弹窗 = 放弃唤醒，SRE 保持置灰。

### 3.5 添加设备改为弹窗
- `openAddDevicePanel` 从右侧滑入侧边栏（520px）改为居中模态弹窗（宽 ~560px、圆角 18px、毛玻璃遮罩，iOS 风格）。
- 内部「生成凭据」「AI Devspace」两个折叠区逻辑原样保留。
- 侧边栏函数保留为兼容入口（其他页面如有调用不动），或统一替换（待确认调用点，初步检索仅 runtimes.js 自身 + 添加设备按钮）。

### 3.6 已建场景的离线检查
- 场景详情/编辑页渲染时：若 SRE 节点 `enabled && engine_runtime_ids.length > 0` 且这些引擎全部 `status!=='online'`：
  - 引擎徽章显示「机器全部离线」（复用现有红徽章文案 `dashboard.engine_offline_all`）；
  - 打开该场景详情页（或点击徽章）时重新触发 §3.3 唤醒逻辑：有在线个人机器 → 重新自动全选并提示；无 → 弹 §3.4 弹窗。
- SRE 节点空引擎列表：显示「未绑定引擎」灰徽章（Q2 裁定：SRE 空 ≠ 广播）；点击同样触发唤醒逻辑。

### 3.7 决策补录（2026-10-14 主人已拍板）

| 决策点 | 主人裁定 | 落地含义 |
|---|---|---|
| Q1 自动全选后反选 | **允许** | 自动全选只是初始值，用户可在引擎弹窗内自由增删（仍限个人级） |
| Q2 SRE 启用但引擎为空 | **为空 ≠ 广播，= 彻底为空** | SRE 节点语义改变：空列表不再回退广播，而是「无引擎，无法派发」。前端：空列表显示「未绑定引擎」灰徽章（非绿色广播徽章）；后端：sre 节点 enabled 且空列表 → 400 拒绝保存；`pick_engine` 对 sre 空列表返回 engine_unavailable，不广播 |
| Q3 派发层约束 | **要与前端一致，否则多此一举** | 本期必须改 `engine_select.py`：SRE 任务派发只允许 user scope 引擎，空则任务失败（不兜底）。详见 §4 后端改动 |
| Q4 添加设备弹窗化范围 | **仅 SRE 流程单独弹窗化，不改设备页侧边栏** | 新增独立的 SRE 引导弹窗（复用凭据生成 + AI Devspace 的内容逻辑，但以弹窗容器呈现）；`openAddDevicePanel` 原样保留，设备页入口不动 |

---

## 四、改动清单（按文件）

### 后端
| 文件 | 改动 | 规模预估 |
|---|---|---|
| `routes/dashboard.py` | `agent_engines['sre']` 仅查 `query_engines(repo_key,"user",agent_type="sre",username=uname)`，去掉 global/repo/local 三路 | ~5 行改动 |
| `routes/scenes.py` | `_validate_scene_engine_ids` 增加：tag 为 sre 时，引擎 scope 必须为 user 且 owner_id==当前用户，否则 400；且 sre 节点 enabled 时 engine_runtime_ids 非空，否则 400 | ~20 行 |
| `runtime/engine_select.py` | **派发层约束（Q3 裁定）**：① `pick_engine`（plugin 模式）sre 节点 pool 只保留 user scope 引擎，空池 → engine_unavailable，不广播；② legacy 路径 sre 仅从 user scope 候选取，无 → None | ~30 行 |
| `routes/settings.py` | （agents 配置页）`get_agent_runtimes` 无需改——SRE 在 agents.js 前端过滤 scope==='user' | 0 行 |

### 前端
| 文件 | 改动 | 规模预估 |
|---|---|---|
| `pages/dashboard.js` | ① 新建场景 sre 默认 `enabled:false`；② `toggleAgentInScene('sre')` 拦截→唤醒流程；③ `_openEngineModalImpl` sre 时只显示 user 引擎组；④ 新增无个人机器弹窗 + AK/SK 输入弹窗 + 唤醒重触发；⑤ 详情页全离线检测；⑥ `_engineBadgeHtml` 对 sre 空列表显示「未绑定引擎」灰徽章（Q2 裁定） | ~280 行（新增为主） |
| `pages/runtimes.js` | **不改侧边栏（Q4 裁定）**：抽出凭据生成 / AI Devspace 两块内容为可复用渲染函数，供新 SRE 弹窗调用；`openAddDevicePanel` 原样保留 | ~40 行（抽取重构，不改行为） |
| `pages/agents.js` | SRE agent 的引擎下拉过滤 `r.scope==='user'` | ~3 行 |
| `index.html` | dashboard.js / runtimes.js 版本号 +1（防缓存） | 2 行 |

### 不改的部分（明确划界）
- 设备页「添加设备」侧边栏及其入口（Q4 裁定：SRE 流程单独弹窗化）；
- AI Devspace 创建环境的后端接口（`devstage.py` 全部复用）；
- 其他四种 agent 的一切行为（含空列表=广播语义，仅 SRE 改变）。

---

## 五、交互时序（关键路径）

```
用户点 SRE 的 ✓
  → 前端已有 agent_engines（页面加载时随 dashboard 数据返回，无需额外请求）
  → filter(scope=user && online)
  ├─ 命中 → node.engine_runtime_ids = 全部命中 ID，卡片徽章刷新
  └─ 空 → 弹无机器弹窗
       ├─ 点华为云 → GET /api/devstage/config
       │    ├─ ak_exists → openAddDeviceModal(devspace 展开)
       │    └─ 否 → AK/SK 输入弹窗 → PUT /api/devstage/config
       │              → openAddDeviceModal(devspace 展开)
       └─ 点直接托管 → openAddDeviceModal(凭据区展开)
            （用户在真实机器上装好 worker、注册成功后关闭弹窗）
       → 弹窗关闭时重新拉 agent_engines（调 /api/dashboard 刷新或局部
         /settings/agent-runtimes）→ 再走一遍唤醒判断
```

保存场景时后端 `_validate_scene_engine_ids` 兜底：SRE 节点若混入非个人引擎 → 400 拒绝（防 API 直调绕过前端）。

---

## 六、决策点（2026-10-14 主人已全部拍板，见 §3.7 补录）

1. **Q1**：允许反选 ✔
2. **Q2**：SRE 空列表 ≠ 广播，= 彻底为空（无引擎，无法派发）✔
3. **Q3**：派发层（`engine_select.py`）必须同步约束，与前端一致 ✔
4. **Q4**：添加设备弹窗化仅限 SRE 流程，设备页侧边栏不动 ✔

---

## 七、风险与对策

| # | 风险 | 对策 |
|---|---|---|
| R1 | `agent_engines` 是页面级缓存，弹窗流程中装好机器后数据过期 | 弹窗关闭时强制重拉（局部接口），不依赖旧缓存 |
| R2 | AK/SK 输入弹窗与通用设置同步：掩码 `********` 语义（未修改）需复用 settings.js 的提交逻辑 | 抽公共提交函数，勿复制粘贴两份 |
| R3 | ~~派发层未约束~~ | 已按 Q3 裁定纳入本期：`pick_engine` 强制 SRE 只派 user scope，空则 engine_unavailable（失败不兜底）。需重点回归聊天主链路的 SRE 触发路径 |
| R4 | 个人机器全在但全离线时，唤醒弹窗会误判"无个人机器" | 弹窗文案区分两种情况：「无个人机器」vs「个人机器全部离线」（后者按钮 B 更合理，提示用户开机） |
| R5 | dashboard.js 已 2000+ 行，再塞 250 行 | 新逻辑集中放在文件尾部独立区块 `── SRE 个人机器唤醒逻辑 ──`，便于回滚 |

## 八、测试要点（改动相关文件，不跑全量）
1. 新建场景：SRE 默认置灰；启用→自动全选；无机器→弹窗两条路径；
2. 已有 AK/SK 时华为云路径跳过输入直接进 Devspace 弹窗；
3. 保存含 SRE 场景 → 后端校验通过；API 直调塞全局引擎 → 400；sre enabled 且空列表 → 400；
4. **派发层（Q3）**：触发 SRE 任务 → 只入队 user scope 引擎；个人引擎全离线 → engine_unavailable 失败，不广播、不落到全局引擎；
5. 详情页全离线 → 红徽章 + 重开触发唤醒；SRE 空列表 → 灰徽章「未绑定引擎」；
6. 回归：vod/issue/coding/qa 的引擎选择、空列表=广播语义不受影响；设备页「添加设备」侧边栏原样可用（Q4）；
7. 回归：聊天主链路 `pick_engine` 非 SRE agent 派发路径无副作用。

## 九、预估
后端 ~55 行（dashboard.py + scenes.py + engine_select.py），前端 ~320 行，共 6 个文件。一次会话可完成主体，弹窗细节（间距/圆角/毛玻璃参数）预计 1-2 轮 UI 迭代 nya~
