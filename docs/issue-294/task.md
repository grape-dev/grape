# 任务拆解 — Issue #294：IM 渠道配置支持解绑

| # | 任务 | 落点 | 依赖 | 状态 |
|---|------|------|------|------|
| T1 | i18n 解绑文案键（zh/en） | `locales/{zh,en}/chat.json`、`locales/{zh,en}/agents.json` | — | ✅ |
| T2 | Chat 前端解绑入口（按钮 + confirm + 刷新） | `src/api/static/js/pages/im_channel.js` | T1 | ✅ |
| T3 | Chat 后端解绑清理（注册表 / 会话 / 映射对账） | `src/api/routes/chat.py` | T1 | ✅ |
| T4 | VOD 后端显式解绑（`im_unbind` + 三字段置空 + 清理） | `src/api/routes/agents.py` | T1 | ✅ |
| T5 | VOD 前端解绑入口（已绑定卡片 + 解绑按钮） | `src/api/static/js/pages/agents.js` | T1, T4 | ✅ |
| T6 | 后端测试：Chat 解绑清理 / 绑-解-重绑循环 | `tests/test_chat_routes.py` | T2, T3 | ✅ |
| T7 | 后端测试：VOD 解绑清理 / 引擎技能保留 / 普通保存不误清 | `tests/test_agents_routes.py` | T4, T5 | ✅ |
| T8 | 前端回归测试：入口渲染 / 载荷 / i18n 键 | `tests/js/test_issue_294_im_unbind.js` | T1–T5 | ✅ |
| T9 | 自审查（5 轴）+ 全量回归 | — | T1–T8 | ✅ |

## 验收对照

- T2/T3 → 解绑入口可用、缓存与 DB 状态回到「未接入」
- T3/T4 → 注册表 / 渠道会话 / BotManager / `unified_user_mapping` 无残留
- T6/T7 → 绑定 → 解绑 → 再绑定循环、普通保存不误清
- T1 → zh/en 双语文案齐全
- T8/T9 → 新增用例覆盖 + 现有用例回归

## 验证命令

```bash
# 后端
/opt/grape/.venv/bin/python -m pytest tests/test_chat_routes.py tests/test_agents_routes.py -q
# 前端
node tests/js/test_issue_294_im_unbind.js
```
