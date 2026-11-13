# `im_card_v1` — 钉钉互动卡片通用模板（批 5）

**一个模板顶全套**：选仓库 / 选会话 / 选任务 / 结果提示都复用它，靠正文 + 按钮编号表达，
不按业务再分模板（模板是钉钉侧外部依赖，越少越好）。

## 怎么导入（3 步）

1. 打开卡片平台：<https://open-dev.dingtalk.com/fe/card>
2. 新建卡片模板（业务类型选「机器人」）→ 右上角 `更多` → `导入`
3. 选本目录的 `im_card_v1.json` → 导入后**发布** → 复制 `cardTemplateId`

> 文件由钉钉官方示例 `open-dingtalk/dingtalk-card-examples` 的 `examples/helloworld/helloworld.json`
> 改写而来（保留其 `StdCard` 外壳与组件 schema，替换变量/子组件/按钮），
> **尚未在卡片平台实际导入验证过**。导入若报错，把报错原文给我，我按报错改。

## 模板契约（代码侧约定，改动需同步）

变量（`cardParamMap`，值一律字符串）：

| 变量 | 类型 | 用途 |
|---|---|---|
| `title` | string | 卡片标题（StdCard `${title}`） |
| `body_md` | markdown | 正文（列表 / 结果文案，引擎已做硬断行） |

按钮（固定 6 个，回传参数固定写在模板里）：

| 按钮 | 回传 params |
|---|---|
| `1` `2` `3` `4` `5` | `{"action":"select","num":1..5}` |
| `取消` | `{"action":"cancel"}` |

**按钮 params 是固定值，带不了会话 id** → 点击回调里的 `outTrackId` 反查
`channel_outbound_card`（批 5.2 投放时落库）拿到 chat_id，再翻译成命令：
`num` → 纯数字（走引擎既有 pending 选择分支）、`cancel` → `/cancel`。

## 限制

- 一页最多 5 个选项（模板按钮数固定）。仓库/会话超过 5 个时：先按最近 5 个出，
  剩余仍可用「回复数字」在文字路径选（降级路径保留 20 条/页）
- 结果卡复用同一模板：正文写结果，按钮隐藏或直接不带（按钮点击无效也无副作用）