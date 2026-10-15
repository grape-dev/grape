# Task: Issue #342

- [x] T1 根因定位：轮询回调全量 renderEventDetail → innerHTML 整页重写
- [x] T2 片段构建函数抽取（_evXxxText 系列）+ 稳定 id 容器
- [x] T3 `_evFingerprint` 指纹跳过 + `_patchEventDetail` 定向更新
- [x] T4 终态停止轮询 + 最终结论完整渲染一次
- [x] T5 app.js 离开 event 视图显式清理定时器
- [x] T6 回归测试 tests/js/test_issue_342_event_poll_partial.js（6 组断言）
- [x] T7 自审查（5 轴）+ 语法检查 node --check 通过
