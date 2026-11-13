# Task 拆解：Issue #1916

| # | 任务 | 产物 | 状态 |
|---|------|------|------|
| 1 | 上下文拼装纯函数 + 测试（红） | `tests/js/test_obs_feedback_entry_1916.js` S1 | done |
| 2 | 上下文拼装实现（绿） | `observability.js` `_obsFeedbackPageUrl` | done |
| 3 | 面板渲染/校验/防重 + 测试（红） | 同上测试 S2-S6 | done |
| 4 | 面板 + 提交实现（绿） | `observability.js` `openObsFeedback` 等 | done |
| 5 | 入口渲染 + 窄屏图标 + i18n（zh/en）+ 打点 | `observability.js` + locales + index.html 版本号 | done |
| 6 | 全量回归：node tests/js 定向用例 + 既有 observability 用例 | 测试输出 | done |

说明：任务 1-5 在一次编码会话内按 TDD 顺序完成；每步先跑测试确认红/绿再进入下一步。
