# 钉钉互动卡片（批 5）：卡片按钮可点 · 设计备忘

> 状态：**待批准**（2026-11-05）。上游 `docs/weekly-09/im-card-ir-design.md` §8 把"钉钉动作回收"
> 列为批 5，本文件是它的可执行版：范围、模板契约、分批步骤、降级与验收。
> 落地前所有接口细节以 [钉钉开放平台 · 互动卡片] 官方文档为准，本文提到的 URL 属**待验证**，批 5.1 用真机 curl 逐条核对后再写进代码。

---

## 0. 一句话

钉钉机器人 `actionCard` 的按钮**只支持 URL 跳转**，带参数的动作只能靠"互动卡片"（卡片实例 API +
回调）。批 5 = 给钉钉渠道补上 `Capability.CARD_ACTION`：Card IR 的 `Action` 变成**真按钮**，
点击后回调进引擎，复用现有 `/bind N`、`/join N` 的选择逻辑。

---

## 1. 现状与边界（已核实）

| 项 | 现状 | 结论 |
|---|---|---|
| 卡片渲染 | `DingtalkCardRenderer` → `actionCard`（标题/正文/URL 按钮） | ✅ 不用重写，批 5 只**新增**一种输出模式 |
| 换行 | `hard_breaks()` 补两空格硬断行（`542c4546f`） | ✅ 已修 |
| 动作按钮 | 无 URL → 降级成 `**1**：回复 /bind 1` 文本 + `card_action_unsupported` | ← 批 5 要替换成真按钮 |
| 能力位 | `DINGTALK_CAPABILITIES = TEXT \| RICH_CARD`（无 `CARD_ACTION`） | ← 批 5 置位 |
| Stream 长连接 | 已有（`connections/open` + WS 循环），订阅 `CALLBACK:<bot topic>` + `EVENT:*` | ✅ 可复用，只需+1 个订阅项 |
| 卡片回调通道 | 未接 | ← 批 5 主工作量 |
| 原地更新 | `update_card_by_token` 降级"追加一条" | 批 5.3 可借卡片实例 update 真正实现 |

钉钉互动卡片回调两种模式（官方）：
- **STREAM**（推荐给我们）：复用 TCP 长连接，零公网/零域名，topic 固定 `/v1.0/card/instances/callback`；
  建实例时 `callbackType="STREAM"`
- HTTP：需公网回调地址 + 验签 → **不选**

回调报文：`type=actionCallback`、`outTrackId`、`userId`、`content`（**JSON 字符串**，坑与机器人
帧的 `data` 同款）→ 解析后 `cardPrivateData.actionIds` + `cardPrivateData.params`。

---

## 2. 前置条件（主人在钉钉侧做，代码无法替代）

1. 开发者后台 → 应用 → 开通「**互动卡片实例写权限**」（企业内部应用）
2. 卡片平台（开发者后台 → 开放能力 → 卡片平台）**新建并发布**模板（见 §3 清单）
3. 把 **cardTemplateId** 填进引擎安装配置：`channel_installation.config.dingtalk_cards =
   {"select": "<id>", "result": "<id>"}`（前端 IM 渠道页后续加输入框；批 5.1 可先手改 DB）
4. 确认该应用**当前没有第二个 Stream 客户端**在用同一 clientId（官方明确：一个 clientId 同时
   只能有一个 Stream 服务，否则回调互抢）

> 没有 1~3 时，引擎**自动回落**到今天的行为（文字卡 + 回数字），不影响可用性。

---

## 3. 模板清单（要你在卡片平台搭的）

## 3.0 最终设计（2026-11-05 修订，**覆盖下面 3.1–3.3 初稿**）

**只建 1 个通用模板 `im_card_v1`**，不按业务分模板：

- 变量 2 个：`title`（string）、`body_md`（markdown）
- 固定按钮 6 个：`1`–`5`（回传 `{"action":"select","num":N}`）+ `取消`（`{"action":"cancel"}`）
- 选仓库/会话/任务/结果卡全部复用它：正文不同、按钮相同
- 按钮 params 带不了 chat_id → 回调按 `outTrackId` 反查 `channel_outbound_card` 拿会话
- 可直接导入的模板 JSON：`docs/weekly-09/templates/im_card_v1.json`（改自钉钉官方示例，**未经导入验证**）
- 代价：一页最多 5 项；超出走"回数字"降级路径（文字卡仍是 20 条/页）

其它已按官方 SDK 源码校正的口径（`dingtalk_stream/card_replier.py`）：
- 一步接口 `POST /v1.0/card/instances/createAndDeliver`（不必 create+deliver 两次）
- 单聊 `openSpaceId=dtv1.card//IM_ROBOT.<staffStaffId>` + `imRobotOpenDeliverModel{spaceType}`
- 群 `openSpaceId=dtv1.card//IM_GROUP.<conversationId>` + `imGroupOpenDeliverModel{robotCode}`
- 更新 `PUT /v1.0/card/instances`（+ `cardUpdateOptions.updateCardDataByKey`）
- `cardParamMap` 值必须是字符串（非字符串 json.dumps）

### 3.1 `im_select_v1` — 选择卡（初稿，已被 3.0 取代）

变量（`cardParamMap`，全部 String）：

| 变量名 | 示例 | 说明 |
|---|---|---|
| `title` | `📁 选择仓库` | 卡片标题 |
| `body_md` | `**共 3 个仓库**\n1. XOcto/deepseek-harness` | 列表正文（markdown，引擎已做硬断行） |
| `page_text` | `第 1/1 页` | 页码行 |
| `hint` | `点按钮选择 · 或直接回复数字` | 底部提示 |

交互组件（固定按钮，**回传参数走 cardPrivateData.params**）：

| 按钮 | 回传 params |
|---|---|
| `1` `2` `3` `4` `5` | `{"action":"select","num":1..5}` |
| `下一组 ▸` | `{"action":"page","delta":1}` |
| `取消` | `{"action":"cancel"}` |

> ⚠️ **按钮数量必须在模板里写死**。这是本设计最大的取舍：选仓库每页从 20 条降到 **5 条**
> （仅钉钉互动卡路径；纯文本降级路径仍 20 条），换取"按钮可点"。若卡片平台实测支持
> **动态列表/循环组件**，批 5.2 再升级成"整页 N 个按钮"，本轮不赌。

### 3.2 `im_result_v1` — 结果卡（绑定成功/加入对话/进入任务/校验失败）

| 变量名 | 示例 |
|---|---|
| `title` | `✅ 绑定成功` |
| `body_md` | `**✅ 已绑定 `XOcto/deepseek-harness`**` |
| `tone` | `success` / `danger` / `neutral`（模板内 3 个样式分支，或做成图片/色条变量） |

交互组件：`选择会话`（`{"action":"chat_list"}`）、`继续提问`（`{"action":"dismiss"}`）。
无按钮场景（提示类）复用同模板，只填正文。

### 3.3 `im_stream_v1`（可选，批 5.4）

Agent 回答的流式进度卡：`title` + `body_md` + `status`。先不排期。

---

## 4. Card IR → 模板映射约定（代码侧）

- 渲染器新增模式：`DingtalkCardRenderer(mode="interactive"|"action_card"|"text")`；
  `interactive` 只在**能力位有 CARD_ACTION 且安装配了对应模板 ID**时启用
- IR 映射（不改业务层）：
  - `Card.title` / `text_block` 文本 → `title` / `body_md`
  - `Action(id="bind", label="1", params={"num":1})` → 按钮 1，回传
    `{"action":"select","num":1}`（`id` 决定按钮位，`params` 透传）
  - `Action(url=...)` → 模板里的 URL 按钮（仍走原路径）
  - `page` / `hint` → 同名变量
- **按钮槽位是模板决定的**：IR 里第 i 个可交互 Action 映射到第 i 个按钮槽；超出槽位数的
  Action **保留文本提示行**（内容不丢，计 `card_action_slot_overflow`）
- `outTrackId` 由引擎生成并落库（见 §5 第五项），供"点击后原地更新"与幂等重放

---

## 5. 分批步骤（每批可独立上线，默认开关 off）

开关：`GRAPE_DINGTALK_CARD_MODE=action_card|interactive`（默认 `action_card`=今天行为）；
另加 `data/im_dingtalk_card_mode` 文件式热切换（沿用 `im_engine_mode` 的做法）。

| 批 | 内容 | 估行 | 验收 |
|---|---|---|---|
| **5.1 传输与能力** | `adapters/dingtalk/cards.py`：create / deliver / update 卡片实例（accessToken 复用现有缓存）；Stream 订阅 `CALLBACK:/v1.0/card/instances/callback`；`DINGTALK_CAPABILITIES \|= CARD_ACTION`；建卡失败→回落 actionCard | ~180 | 真机：一条手工建实例能投放，能收到回调帧并打日志 |
| **5.2 选择卡** | renderer `interactive` 模式 + 模板变量映射；`_send_repo_card` / `_send_chat_card` / `_send_task_card` 走新路径；分页 5/页 | ~140 | 真机点 `1` 真绑上仓库，`下一组` 翻页正确，`取消` 生效 |
| **5.3 结果卡与原地更新** | 结果卡模板；`update_card_by_token` → 卡片实例 update（点击后"已选中"态原地变） | ~100 | 点按钮后原卡变"已绑定"样式，不追加新消息 |
| **5.4 加固** | 降级链路演练（删模板 ID/关权限/超时 2s 内响应）；`degraded_by_reason` 新增 3 类；回归 + 文档 | ~80 | 关权限后按钮自动退回文本+数字 |

回调时限：官方要求 **2 秒内**响应，且"回调中不要调更新接口" → 选择逻辑走 `to_thread`/异步任务，
先回 ack 再处理，处理完用 update 接口改卡。

---

## 6. 降级矩阵（必须有，验收 5.4 逐条演练）

| 触发条件 | 行为 | 计数 |
|---|---|---|
| 安装未配 `dingtalk_cards` | 回落 actionCard 文字 + 回数字 | 无（默认路径） |
| 权限缺失 / 建实例 4xx | 回落 actionCard，日志 WARN 一次（每 app 每天） | `card_instance_create_failed` |
| 回调超时/异常 | 回复"处理失败，请重发 /repo" | `card_callback_error` |
| Action 超出模板按钮槽 | 该 Action 保留文本提示 | `card_action_slot_overflow` |
| 点的是别人/过期卡 | 幂等忽略（`outTrackId` 不在库内） | `card_callback_stale` |

---

## 7. 风险

1. **模板是外部依赖**：模板改版 / 换企业 / 换应用 → 变量不一致。对策：模板名+版本号约定
   （`im_select_v1`），启动自检时若模板缺变量则关闭 interactive 模式并告警
2. **按钮槽位固定**：列表只能一页 5 条（见 §3.1 取舍）
3. **一个 clientId 只能一个 Stream**：本地 + 线上同时跑会互抢回调 → 部署文档写死"开发用独立 clientId"
4. 卡片实例幂等键 `outTrackId` 必须在库（`channel_outbound_card` 新表或 `channel_inbound_message`
   旁挂），否则"点击后改卡"和重放都会乱

---

## 8. 请求决策（等主人拍板）

1. §3.1 的 **5 条/页 + 固定按钮** 是否接受？（另一条路：先在卡片平台试"动态列表组件"，能成再上
   整页按钮，但要你先试模板）
2. 批 5 是否**单独立分支**（`feat/im-dingtalk-interactive-card`）做，不混进当前 `feat/weekly-09-IM-mysql-ui`？
   （建议：是，先推现有 7 个提交再开）
3. `outTrackId` 落库：新表 `channel_outbound_card` vs 复用 `channel_inbound_message` 旁挂列？
   （建议：新表，字段 out_track_id / installation_id / chat_id / card_template_id / payload_hash）

> 2026-11-05 已拍板：5 条/页 + 固定按钮；单开分支 `feat/im-dingtalk-interactive-card`；
> outTrackId 走新表 `channel_outbound_card`。