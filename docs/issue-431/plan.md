# Plan: Issue #431

## 根因链
`update_chat_config` else 分支（无 app_id 时）无条件 `kwargs["im_app_id"]=""` 等三行 →
`set_chat_config` 合并语义中 `""` 也覆盖 → `_write_chat_config_row` INSERT OR REPLACE 整行落库 → IM 凭证清空。
同一 else 路径下 BotManager 的 `elif not feishu.get("app_id") and old_app_id` 也会静默摘除在跑 bot。

## 修复方案（调用侧，方案 B）
在 `update_chat_config` 中引入 `explicit_unbind = ("im" in body) and not feishu.get("app_id")`：
- `feishu.get("app_id")` 真值分支：保持现状（绑定/换绑校验 + 写值）。
- `explicit_unbind` 分支：保留三字段置空（显式解绑），`old_app_id` 供 purge/BotManager 使用。
- 其余（body 无 `im` 键）：只读 `old_app_id` 供对账，不写任何 `im_` 字段；BotManager 摘除分支改判
  `explicit_unbind and old_app_id`，避免无 im 保存摘 bot。

`_purge_chat_im_binding` / `_write_chat_config_row` / `set_chat_config` 本身不改。

## 其他入口排查结论
| 入口 | 结论 |
|------|------|
| `agents.py` `update_agent_config` | VOD 写 `devops_agent_session`，解绑需显式 `im_unbind=true`（Issue #294 模式），无静默清空 |
| `agents.py` `_unbind_vod_im` | 仅显式解绑触发 |
| `settings.py` | 无 `set_chat_config` / `chat_agent_session` 写路径 |
| `feishu_onboard.py` | `set_chat_config(user_id, im_app_id=app_id, ...)` 仅显式绑定，非空值 |
| `scenes.py` / `session_store.py` | 传 `im_app_id=None`，`set_chat_config` 合并时 None 跳过，不清空 |

## 历史数据恢复
新增 `scripts/diag/chat_config_im_restore.py`：从 `bak_chat_cfg_migrate_*` 备份表把
当前 `chat_agent_session` 行 im 三字段为空、备份表中非空的行恢复（默认 dry-run，`--apply` 写入）。

## 任务拆解
1. [x] 文档（spec/plan/task）
2. [x] chat.py 调用侧修复
3. [x] 回归测试：无 im 保存→IM 不变（DB + GET 回显 + bot 不摘）；显式解绑→清理正常（既有用例保绿）
4. [x] 修正既有 `test_put_config_clear_im`（其断言的是缺陷行为）
5. [x] 恢复脚本 + 在本地库恢复 user_id=1
