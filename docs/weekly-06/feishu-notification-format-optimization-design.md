# 方案：飞书通知卡格式优化 —— 类型差异化 + 结构化布局 + 时间戳

> 状态：已实施（2026-10-11，分支 `feat/feishu-notify-format`，提交 `719a940a6`；迭代二同分支）｜目标分支：weekly-06
> 迭代二（评审修订）：「任务通知」类型更名为「信息知会」；字段区新增「需求标题」，Issue 编号改为 VCS 公开页链接。见 §2.1、§2.2。
> 动机：三条静态飞书通知卡（信息知会 / 需求待澄清 / 需求待审核）均为「纯文本段落 + 统一蓝色模板」，
> 类型不可一眼区分、信息堆叠为长段落、无时间戳、引导文案与快捷按钮语义重复。
> 本次将通知卡从「纯文本段落」升级为「按类型差异化 + 结构化字段 + 底部时间戳」的卡片布局。
> 本文档同时列出**后续可扩展方向**（§六），作为下一轮优化的输入。

---

## 一、现状与问题

### 1.1 改造前卡片结构（全为蓝色，雷同）

三条通知均走 `send_card_to_user`（feishu_async.py），出参 JSON 结构：

```json
{
  "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "📋 你有新的任务\n\n仓库: owner/repo\nIssue: #7\n\n{描述}\n\n请在任务中心查看并处理。"}}],
  "header": {"title": {"tag": "plain_text", "content": "任务通知"}, "template": "blue"}
}
```

| 卡片 | 函数（issues_center.py） | 原 title | 模板色 |
|------|--------------------------|----------|--------|
| 任务通知（迭代二改名「信息知会」） | `_run_notify`:798 | `任务通知` | blue（固定） |
| 需求待澄清 | `_run_awaiting_notify`:841 | `需求待澄清` | blue（固定） |
| 需求待审核 | `_run_pending_review_notify`:880 | `需求待审核` | blue（固定） |

### 1.2 问题点

1. **视觉无区分**：三张卡同色同构，通知流里无法一眼识别类型 / 紧急度（待审核 ≠ 任务分配）。
2. **信息是纯文本行**：`仓库: xxx` / `Issue: #7` 仅换行堆叠，长描述（澄清问题 / 审核点）会把整卡拉成大段文本，移动端可读性差。
3. **无时间戳**：卡片不携带发送时间，跨日/跨天回溯通知时机困难。
4. **引导文案冗余**：尾部「请在任务中心查看并处理」等与卡片内的「💬 进入任务会话」按钮（`_build_notify_actions`，weekly-05 已加）语义重复。

### 1.3 约束

- 仅改**飞书卡片表达层**；两条「静态模板 agent 消息」写入会话的文案、`tasks.py`/`issues.py` 触发点签名、`_build_notify_actions` 按钮机制均**保持不变**。
- `send_card_to_user` 现有调用（`chat.py:320` 的 `[来自Web]` 转发卡等）参数兼容。

---

## 二、本次优化方案

### 2.1 按类型差异化模板色 + emoji 标题

> 迭代二修订：「任务通知」类型更名为「信息知会」——该卡承载的是任务分配/换人等静态信息告知，命名「信息知会」避免被误读为仅限任务分配场景。

```python
_IM_NOTIFY_TEMPLATES = {"task": "blue", "awaiting": "orange", "pending_review": "red"}
_IM_NOTIFY_TITLES    = {"task": "📋 信息知会", "awaiting": "❓ 需求待澄清", "pending_review": "🔎 需求待审核"}
```

| 类型 | 模板色 | 头部标题 |
|------|--------|----------|
| 信息知会 | blue | 📋 信息知会 |
| 需求待澄清 | orange | ❓ 需求待澄清（需用户动作，警示色） |
| 需求待审核 | red | 🔎 需求待审核（待拍板，最高优先级） |

### 2.2 结构化字段（`field` 键值对）：仓库 / Issue（链接）/ 需求标题

> 迭代二修订：新增「需求标题」字段（复用 `_get_issue_title`，events 表零 API 查询）；Issue 编号改为 `[#N](url)` 跳转 VCS 公开页。

`_build_notify_fields(repo_key, issue_number)`（issues_center.py:745）→ 追加 `{"tag":"div","fields":[...]}`：

```python
[
  {"is_short": True,  "text": {"tag": "lark_md", "content": "**📦 仓库**\nowner/repo"}},
  {"is_short": True,  "text": {"tag": "lark_md", "content": "**🎯 Issue**\n[#7](https://gitcode.com/owner/repo/issues/7)"}},
  {"is_short": False, "text": {"tag": "lark_md", "content": "**📌 需求标题**\n<events 表最近真实标题>"}},
]
```

- 仓库 / Issue 两个短字段一行对齐；需求标题为全宽字段（长文本不截断换行）。
- Issue 链接 URL 规则与 `_build_notify_actions` 一致：`repo_key.startswith("github:")` → GitHub，否则 GitCode。
- 需求标题为空 / 无编号（`issue_number=0`）时回退「—」，不弹链接。
- 飞书端按 `field` 渲染为对齐键值对，移动端不再依赖肉眼对齐。

### 2.3 底部时间戳（`note` 灰字注释）

`_notify_note()` → `{"tag":"note","elements":[{"tag":"plain_text","content":"发送时间：2026-10-11 14:30"}]}`，位于卡片最底部。

### 2.4 精简冗余文案

- 信息知会正文：`你有一条新任务待处理。\n\n{task_description}`（空描述时仅首行）。
- 待澄清正文：`Agent 需要你补充以下信息：\n\n{questions}`。
- 待审核正文：`请审核确认以下交付审核点：\n\n{review_points}\n\n参考: {references}`。
- **删除** 尾部「请在任务中心… / 请审核后回复…」引导句（与已存在按钮语义重复）。

### 2.5 最终卡片结构

```json
{
  "header": {"title": {"tag": "plain_text", "content": "❓ 需求待澄清"}, "template": "orange"},
  "elements": [
    {"tag": "div",  "text": {"tag": "lark_md", "content": "Agent 需要你补充以下信息：\n\n{questions}"}},
    {"tag": "div",  "fields": [{"is_short": true,  "text": {"tag": "lark_md", "content": "**📦 仓库**\nowner/repo"}},
                               {"is_short": true,  "text": {"tag": "lark_md", "content": "**🎯 Issue**\n[#7](https://gitcode.com/owner/repo/issues/7)"}},
                               {"is_short": false, "text": {"tag": "lark_md", "content": "**📌 需求标题**\n<标题>"}}]},
    {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "💬 进入任务会话"}, "value": {"cmd": "/task-open", ...}},
                                  {"tag": "button", "text": {"tag": "plain_text", "content": "查看 Issue ↗"}, "url": "..."}]},
    {"tag": "note", "elements": [{"tag": "plain_text", "content": "发送时间：2026-10-11 14:30"}]}
  ]
}
```

---

## 三、各模块改动

| 模块 | 改动 |
|------|------|
| `src/im/feishu_async.py` `send_card_to_user`:544 | 新增 `template="blue"` / `fields=None` / `note=None` 参数；fields 追加 div、note 追加底部注释、header 用传入 template。默认值 → 与改造前完全一致 |
| `src/api/issues_center.py` | 新增 `_IM_NOTIFY_TEMPLATES` / `_IM_NOTIFY_TITLES`（:733-742，task 标题为「📋 信息知会」）、`_build_notify_fields`（:745，仓库/链接 Issue/需求标题）、`_notify_note`（:763）；三条 `_run_*_notify` 传 `title/template/fields/note` 并精简正文 |

**未改动**：会话静态模板文案、`tasks.py:275` / `issues.py:908/988` 触发点、`_build_notify_actions` 按钮机制、`send_card_to_chat`、`to_lark_elements`。

---

## 四、影响面 / 兼容性 / 安全边界

| 项 | 说明 |
|----|------|
| 参数兼容 | `send_card_to_user` 新参数全部带默认值；`test_chat_routes.py` / `test_cluster_im_lease.py` 等存量调用零改动 |
| 行为收敛 | 通知仍不触发 ChatAgent / 不跑 LLM；仅卡片表达层变化，发送对象（P2P 本人 bot）与 `_IM_NOTIFY_COUNTERS` 计数不变 |
| 内容安全 | 正文 / fields / note 均经 `to_lark_elements(text, 2000)` 截断与 `<at>` 清洗；时间戳无用户输入 |
| 测试兜底 | 新增以下用例全部通过 |

---

## 五、验证

| 用例 | 断言 | 位置 |
|------|------|------|
| `test_send_card_to_user_template_fields_note` | 出参 JSON：header.template=orange、title=❓ 需求待澄清、fields div 存在、note 为末元素 | tests/test_feishu_async.py |
| `test_send_card_to_user_with_actions` / `_no_actions` | actions 参数行为与改造前一致 | tests/test_feishu_async.py |
| `test_build_fields` | fields 3 项：仓库、`[#7](gitcode 链接)`、需求标题（全宽） | tests/test_feishu_navigation_commands.py |
| `test_build_fields_github_issue_link` | GitHub 前缀仓库 Issue 链接指向 github.com | tests/test_feishu_navigation_commands.py |
| `test_build_fields_no_issue` | 无编号：Issue 字段回退「—」且无链接 | tests/test_feishu_navigation_commands.py |
| `test_notify_note_has_timestamp` | 时间戳格式 | tests/test_feishu_navigation_commands.py |
| 全量回归 | 基线 359 fail/2312 pass → 本次 358 fail/2317 pass，**branch-only 失败=0**（存量失败均为环境性：test_tasks 鉴权种子、测试文件 mojibake） | pytest tests/ |

---

## 六、后续可扩展方向（作为下一轮优化的输入）

> 迭代二已落地原 §六.1（需求标题字段 + Issue 链接），以下为本轮之外剩余方向，按性价比与依赖排序：

1. **按钮「查看 Issue ↗」与字段内 Issue 链接语义重复**：字段区已可点开 Issue 页，可评估移除按钮或将按钮升级为更有价值的回环操作。
2. **正文状态区**：字段区之下加 `**状态**: pending_review → ✅ 待你审核`，让用户进会话前即知当前阶段。
3. **按钮扩展**：「✅ 标记完成 / ❌ 打回」等一键回环按钮（复用 `value.cmd` + `_card_value` 机制，weekly-05 链路已打通），需先落一版回调处理，安全边界同 §1.3。
4. **通知偏好打通**：`repo_im_group_binding.notify_enabled`（settings.py 已暴露）目前未接入飞书卡片发送分支，可做按用户/仓库的通知开关。
5. **发送失败重试 / 降级**：`send_card_to_user` 失败时是否降级 `send_text_to_user`（纯文本兜底），需评估重复通知噪声。
6. **通知合并 / 限流**：同 issue 高频阶段流转时去重（如 1 分钟窗口内同 issue 只发一张最新态卡片），并支持卡片 `update_message` 原位刷新。
7. **Web ↔ 飞书入口统一**：`_build_notify_actions` 的「进入任务会话」与 Web 任务中心「去对话」语义对齐，文档补充两侧入口说明。