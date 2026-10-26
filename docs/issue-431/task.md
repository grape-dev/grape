# Task: Issue #431

- [x] T1 文档：spec.md / plan.md / task.md
- [x] T2 修复 `src/api/routes/chat.py` `update_chat_config`：无 `im` 键不写 im_ 字段；BotManager 摘除仅显式解绑触发
- [x] T3 排查 `agents.py` / `settings.py` / `feishu_onboard.py` / `scenes.py` / `session_store.py`（结论见 plan.md，均无需改动）
- [x] T4 回归测试 `tests/test_chat_config_im_preserve.py`；更新 `tests/test_chat_routes.py::test_put_config_clear_im`
- [x] T5 恢复脚本 `scripts/diag/chat_config_im_restore.py` + user_id=1 恢复
- [x] T6 全量相关测试跑绿 → 自审查 → PR
