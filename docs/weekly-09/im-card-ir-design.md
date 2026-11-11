# 中立卡片模型（Card IR）设计 · weekly-09

> 状态：**方案待批准，未改代码**（2026-11-05）
> 关联：#1008 出站收敛为纯 Replier（已完成，本次设计的直接触发点）、#936 钉钉适配
> 代码基线：`feat/weekly-09-IM-mysql-ui` @ 2cb33eab9

---

## 1. 结论摘要

出站**信封**已抽象（`OutboundMessage` + `Capability` + `Replier` + `Sender.send()`），
纯文本/ markdown 是真正跨平台的。**卡片没有抽象**：`raw_card` 里装的是飞书 lark 卡片
JSON 原文，平台私有格式被当成公共语传递；除飞书外没人能渲染，也没人负责降级。

后果三层：

1. **钉钉静默丢内容**：钉钉声明了 `RICH_CARD`（`adapters/dingtalk/outbound.py:29`），
   但 `DingtalkSender.send()` 只有 text / markdown 两条分支，`kind="raw_card"` 时
   `text=""` → 实际发出 `send_text(cid, "")` 并返回 `ok=True`。已用假发送器实测复现。
2. **能力位语义失真**：`RICH_CARD` 的真实含义变成"我懂飞书卡片格式"，而不是"我能渲染卡片"。
   `Replier` 中"无 RICH_CARD → 压平文本"的降级分支因此被绕过。
3. **每加一个渠道就要再写一份转换**：业务侧 21 处调用点直接构造 lark 结构，
   卡片样式/交互的任何调整都等于改飞书协议。

本方案给出一个最小但完整的中立卡片 IR、渲染契约、分批迁移计划与验收标准。

---

## 2. 现状盘点（已核对代码）

### 2.1 卡片构造点
`src/im/handler_async.py`（1975 行）内：

| 位置 | 形态 |
|---|---|
| `_send_card_reply()` :149 | 唯一出站入口（→ `egress.send_raw_card`） |
| `_candidate_card()` :138 | 返回 markdown 字符串（不是卡片） |
| `_send_repo_card()` :158 | 选仓库卡（div + 动态 button 行 + 帮助按钮） |
| `_autopilot_state_card()` :395 | 派发流程（schema 2.0 + `select_static` 下拉） |
| `_autopilot_finding_list_card()` :531 | 问题列表卡 + 下拉 |
| `_send_bind_success_card()` :1032 | 绑定成功卡 |
| `_NO_REPO_CARD` / `_BIND_SUCCESS_CARD_TPL` 等常量 | 静态卡模板 |

调用点 **21 处**（`grep _send_card_reply(` / `send_raw_card(`),另有
`send_quick_cmd_card`（快捷命令卡）1 处。

### 2.2 用到的 lark 元素（频次）
`plain_text` 38、`lark_md` 31、`div` 31、`button` 15、`action` 9、`hr` 6、`select_static` 2。

### 2.3 按钮载荷形态
全部是飞书回调 value：`{"cmd": "/help"}`、`{"cmd": "/bind 1"}`、`{"cmd": "/chat"}`、
`{"cmd": "/finding-dispatch", "exec_id":…, "finding_id":…, "scene_id":…}`。
两种写法并存：`elements[].actions[]`（v1）与 `behaviors[].value`（v2）。

### 2.4 入站交互
只有飞书有卡片回调解析：`adapters/feishu/inbound.py:30 parse_card_action()`（`card.action.trigger`
→ `action_value` / `_reply_id` / `_action_token`）。**钉钉入站只处理 bot 消息**，没有任何
卡片回调通道。

### 2.5 已发现的附带缺陷（迁移时顺手清掉）
- `_BIND_SUCCESS_CARD_TPL` 在 `handler_async.py:1009` 与 `:1021` **重复定义**。
- `_flatten_card()`（`engine/egress.py:318`）只抽 `div.text`，按钮/下拉丢失；
  文档注释已承认"保内容不保交互"。
- 能力位无"能否回收动作"这一维，`RICH_CARD` 一维承担了渲染 + 交互两种语义。

---

## 3. 目标 / 非目标

**目标**
- 业务侧只构造中立卡片对象，不再出现 `lark_md` / `tag` / `template` 等平台字段。
- 每个渠道一个 renderer，把中立卡渲染成本平台形态；都不支持时统一压平为文本，**内容不丢**。
- 能力位如实申报：渲染能力（`RICH_CARD`）与交互回收能力（新增 `CARD_ACTION`）分开。
- 无行为回退：飞书现有卡片外观与交互保持等价（含原地更新 token 语义）。

**非目标**
- 不做通用卡片设计器 / JSON DSL / 主题系统。
- 不追求钉钉与飞书交互完全对齐（钉钉机器人 actionCard 按钮只支持打开 URL，
  真回调需卡片实例 API + 回调订阅；本方案批 3/4 只做有限对齐，见 §8）。
- 不改动 `legacy` 出站模式的行为（保留 `legacy_send_card_reply` 原路径，仅飞书可用）。

---

## 4. Card IR（模块 `src/im/channel/card.py`，新增）

```python
class Tone(enum.Enum):      DEFAULT / SUCCESS / WARNING / DANGER / INFO   # 语义色,映射各平台配色
class ButtonStyle(enum.Enum): DEFAULT / PRIMARY / DANGER

@dataclass(frozen=True)
class Action:
    id: str                  # 语义动作标识，如 "bind", "chat.join", "task.open"
    label: str
    style: ButtonStyle = ButtonStyle.DEFAULT
    params: dict[str, Any] = {}      # 结构化参数,替代 /bind 1 这类命令串
    url: str = ""                    # 仅跳转型（钉钉 actionCard 唯一能承载的交互）

@dataclass(frozen=True)
class Block:
    kind: Literal["text", "fields", "divider", "select"]
    text: str = ""                   # markdown 通用语（kind=text）
    fields: list[tuple[str, str]] = []   # 键值对（kind=fields）
    options: list[Action] = []       # 下拉项（kind=select）——复用 Action 承载 value
    placeholder: str = ""

@dataclass(frozen=True)
class Card:
    title: str = ""
    tone: Tone = Tone.DEFAULT
    blocks: tuple[Block, ...] = ()
    actions: tuple[Action, ...] = ()      # 卡片底部按钮区
    page: tuple[int, int] | None = None   # (当前页, 总页数)，渲染为页脚
    hint: str = ""                         # 页脚提示，如"回复编号选择 · 0 取消"
    update_key: str = ""                   # 原地更新令牌（飞书 token / 后续钉钉 cardId）
    opaque: dict = field(default_factory=dict)  # 过渡期逃生舱：仅 fly 调试用，禁止业务依赖
```

设计取舍：
- **不做容器嵌套**：现有 21 处卡片最深两层（div / action），扁平 blocks + 底部 actions 足够，
  嵌套会立刻把渲染器写复杂。
- **交互语义化**：`Action.id + params` 而不是 `{"cmd": "/bind 1"}`。命令串由
  **上层（handler）在构造卡片时**决定，回收入站后映射回命令；跨平台差异（钉钉只能 URL）
  由渲染器决定丢弃/降级并计 degraded，不污染业务代码。
- **Tone 而非 template**：`red/blue/green` 是飞书配色名，`DANGER/WARNING/SUCCESS` 是语义。

---

## 5. 渲染契约

```python
class CardRenderer(Protocol):
    def capabilities(self) -> Capability: ...
    async def render(self, card: Card, route: str, *, reply_to: str = "") -> SendResult: ...
```

- `adapters/feishu/card_renderer.py`：IR → lark JSON（v1 `elements` 形态，保持现有外观；
  `select_static` 支持；`update_key` → token 原地更新）。
- `adapters/dingtalk/card_renderer.py`：IR → actionCard（`title` / `text`(markdown) /
  `btns`）；按钮若只有 `url` 则直出，若只有 `params` 则降级为文本提示并计
  `degraded("card_action_unsupported")`。
- `adapters/text_card_renderer.py`（共享）：IR → 纯文本/ markdown（替换现 `_flatten_card`），
  按钮渲染成"回复 /help 等命令"提示行。
- 选择规则（`HandlerEgress.send_card`）：
  `RICH_CARD` 有 → 本平台 renderer；无 → text renderer（计 degraded）。**不允许多条路径
  直接透传平台 JSON。**

能力位调整（`channel/capability.py`）：
- `RICH_CARD` 语义收紧为"能渲染 Card IR"；
- 新增 `CARD_ACTION = 1 << 8`："能回收卡片动作回调"（飞书 true，钉钉 false 直至批 4）；
- 既有 `send_raw_card(card: dict)` 保留为 `deprecated`，仅在 `legacy` 模式与飞书下使用，
  迁移完成后删除。

---

## 6. 迁移分批（每批独立可回滚，开关 `GRAPE_IM_CARD_IR=off|on`）

| 批 | 内容 | 预估 |
|---|---|---|
| 1 | `channel/card.py` IR + `Capability.CARD_ACTION` + text renderer（搬 `_flatten_card`）+ 契约测试 | +220 / -40 |
| 2 | feishu card_renderer + `HandlerEgress.send_card()` + 双轨开关；**不改业务调用点**（开关 on 后仍走旧 raw_card） | +140 / -20 |
| 3 | 迁移 `handler_async.py` 21 处调用点为 IR（按功能簇分 3 小批：仓库绑定类 / 会话任务类 / autopilot 派发类），顺手删重复模板 | 净 -120 左右（JSON 字面量变构造调用） |
| 4 | dingtalk actionCard renderer + 去掉钉钉 RICH_CARD 谎报 → 改由 renderer 申报；空文本护栏（`send_text("")` → failed，不再假 ok） | +160 / -10 |
| 4.5 | 卡片生命周期落库：`channel_outbound_card` 表 + `Card.update_key` 持久化（参照 multica，见 §9.3）；飞书原地更新可跨重启 | +120（含迁移脚本） |
| 5 | 钉钉动作回收（可选，需卡片实例 API + 回调订阅）：inbound 增卡片回调解析 + `CARD_ACTION` 置位 | +180（风险最高，单独评估） |

批 3 前建议先跑一轮"卡片快照"基线：把 21 处调用构造出的旧 JSON 存成 fixture，
迁移后断言 IR 渲染回等价 JSON（字段序无关、忽略 `tag` 之外的默认值），保证外观零漂移。

---

## 7. 验收标准

1. **契约测试**：同一批 IR fixture × 3 个 renderer，断言
   (a) 飞书渲染结果与迁移前 JSON 等价；(b) 钉钉渲染结果含标题/markdown/按钮，且无交互参数时
   不出空按钮；(c) 文本渲染结果包含卡片全部可见文本（`_flatten_card` 的"保内容"承诺升级为
   测试断言，按钮以命令提示保留）。
2. **不丢内容的硬断言**：`send_card` 三条路径任一情况下发出内容为空 → 计 `failed` 并告警
   （新增守卫 `if not text and not rendered: failed`）。
3. **能力位断言**：`SUPPORTED_CHANNELS` 的 caps 表 —— 钉钉在批 4 前 `RICH_CARD` 保持不变，
   但 `card_renderer` 缺失必须显式降级，不能有"声明支持但无实现"的组合（新增启动自检：
   声明 `RICH_CARD` 就必须提供 renderer，否则启动即失败）。
4. **功能验证**（mock 驱动，真实函数）：`/repo`、`/chat`、`/task`、`/help`、
   autopilot 派发 5 条链路各跑一次，断言 renderer 收到的 IR 字段完整、回收入站动作能映射回命令。
5. **回归**：`pytest tests/im` 全绿（当前 229），新增测试 ≥ 25 条；`test_handler_no_direct_send`
   扩展为"禁止业务层出现平台字段名"的静态检查（`lark_md` / `template` / `behaviors`）。
6. **指标**：`flap_by_cause` 之外新增 `degraded_by_reason`（`card_text_fallback` /
   `card_action_unsupported` / `card_empty_guard`），上线后可从 `/health/realtime` 观察。

---

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| 飞书外观漂移（用户已验收的卡片变样） | 批 3 的 JSON 等价断言 + 保留 `legacy` 开关 |
| 迁移面积大（21 处、1975 行文件） | 按功能簇拆 3 小批，每批单独 commit + tag，跑相关测试 |
| 钉钉交互期待落空（按钮点了没反应） | 批 4 明确定位：**只做展示 + URL 跳转**；带 params 的按钮降级为文本提示并计 degraded；批 5 才谈回调 |
| `select_static` 在钉钉无等价物 | 渲染为"回复编号"文本列表（现有 `_candidate_card` 已是这个形态，可直接复用） |
| 双轨期维护成本 | 开关默认 off，上线验证后一次翻转；双轨只保留到批 5 结束 |

---

## 9. 参考实现对比：multica（Go）

本设计**并非**照抄 multica；初版是从 grape 代码基线现推的。事后对照 multica 现行实现
（`server/internal/integrations/`），结论是：multica 做了"卡片相关抽象"，但**不是跨平台卡片 IR**，
它选的是"每平台内各自渲染 + 能力位诚实申报"。逐项对照：

| 维度 | multica 现状 | 本方案 |
|---|---|---|
| 能力位 | `channel/capability.go` 声明式位掩码，注释明确"本包不含降级逻辑，调用方自己读位决定" | 同思路，保留；额外新增 `CARD_ACTION` 区分"能回收动作" |
| 卡片模型 | 无中立 IR。卡片 JSON 在各平台包内生成 | 中立 `Card`/`Block`/`Action` |
| 飞书 | `lark/outbound.go`：`Renderer` 接口 `Render(RenderInput) → CardRender{JSON}`，可插拔模板；`RenderInput` 是**生命周期快照**（thinking / running / final / error），不是业务卡片 | 通用 Card IR + feishu renderer；业务卡（选仓库/派发）在它那儿同样没有抽象 |
| 卡片生命周期 | **落库**：`channel_outbound_card_message`（status pending/streaming/final/error、last_patched_at、channel_card_message_id），支持跨进程 padding/patch | 仅 `update_key` 内存透传（grape 的 `_action_token` 来自回调事件，不落库，重启后无法再 patch） |
| 钉钉 | `caps = TEXT \| ATTACHMENT`，**不申报 RICH_CARD**；出站只有 text/markdown；`dingtalk/inbound_card.go` 仅把"用户引用的卡片消息"拍平成文本读入，不是发卡 | 批 4 去掉 RICH_CARD 谎报 + actionCard 渲染（比 multica 多做一步） |
| Slack | `caps = TEXT \| THREAD_REPLY`，有 Block Kit 能力但不做卡片 | 不在本期范围 |
| 降级消费方 | 全仓 `CapRichCard` 只出现在 lark 声明处 + 单测，**没有任何调用方真的按位降级** | 本方案把降级做成唯一路径（`send_card` 选择器），并有测试断言 |

可吸收的三点（已并入本方案）：

1. **能力位诚实申报**（§5 批 4）：multica 钉钉不申报 RICH_CARD，所以不存在"声明了但发空消息"这种
   事故；这反过来印证 §2.1 的判断——grape 的钉钉问题根源是这个位。
2. **Renderer 以构造注入、可替换**：本方案 `CardRenderer` 协议按同样方式装配
   （`sender = Channel(card_renderer=…)`），模板可 A/B 而不用改装配层。
3. **卡片生命周期落库**（新增批 4.5，见 §6）：multica 把 `card_message_id + status + last_patched_at`
   存表，飞书原地更新才能跨进程/跨重启。grape 现在 `_action_token` 只在回调事件里活着，
   重启即失效。建议新增 `channel_outbound_card` 表（installation_id / chat_id / message_id /
   update_token / status / last_patched_at），与 §4 的 `Card.update_key` 对应。

不照搬的地方：
- multica 的 `RenderInput` 是**产品生命周期**快照（thinking/running/final），适合"一条任务卡被反复
  patch"的场景；grape 的卡片多是**一次性交互卡**（选仓库/派发/帮助）。两者语义不同，不要合并成一个包。
- multica 没有中立卡 IR，所以它的 Slack/企微将来做卡片时仍要各写一遍；本方案正是为了避开这个。

---

## 10. 开放问题（需主人拍板）

1. 批 3 是否允许**图片/附件类卡片**一并迁移（当前附件走独立两跳通道，与卡片无关）？建议不动。
2. 钉钉的交互目标：只做"像卡片"（展示对齐，按钮降级文本），还是最终要按钮可点回调（批 5，
   需钉钉卡片实例 API + 应用回调配置，涉及外部配置与联调）？
3. `opaque` 逃生舱是否保留？建议只保留一批迁移期，批 5 结束删除，避免变成新的私有格式入口。
4. 批 4.5（卡片生命周期落库）是否纳入本期？纳入意味着本期多一个 DB 迁移；不纳入则飞书卡片
   原地更新在服务重启后失效（现状即如此，非新引入）。