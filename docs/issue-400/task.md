# Task: Issue #400 — chat agent 引擎选择显示异常

| # | 任务 | 验收 | 状态 |
|---|------|------|------|
| 1 | engine_select.py：engine_unique_key / dedupe_engines + 候选收口去重 | 同身份候选只出现一次；resolve_local_engine/pool 名单不重复 | 待做 |
| 2 | chat.py：GET /config _backends 去重 + selectedEngine（runtime_id 权威） | 返回体含唯一 selectedEngine；并发个人+直连不重复 | 待做 |
| 3 | session_store.py：get_or_create_agent_session 支持 runtime_id 指针 | chat 会话 runtime_id == 执行引擎 id；旧调用行为不变 | 待做 |
| 4 | chat.js：单选渲染 + 顶栏引擎徽标 + 保存后刷新 | 同一时刻唯一 active；徽标显示实际引擎名 | 待做 |
| 5 | chat.css：徽标样式 | 视觉一致（app / apple 主题） | 待做 |
| 6 | 后端测试：去重/selectedEngine/指针 | 新增测试全过 + 既有相关测试回归通过 | 待做 |
| 7 | 前端测试：单选渲染 | 断言 active 唯一、精确匹配 | 待做 |
| 8 | 自审查 + 全量相关测试 + PR + Issue 评论 + A2A QA | PR 创建、评论、QA 交办完成 | 待做 |