# Tasks: Issue #297

- [x] T1 基线确认：worktree feat/issue-297 基于 origin/local-liufei
- [x] T2 根因实证：nginx-grape.conf 无 client_max_body_size（默认 1m）；api.js 非 ok 分支无条件 r.json()；chat.js doSend catch 未传 {status}
- [x] T3 编写测试（RED）：test_chat_multi_image_297.py + test_issue_297_multi_image_error_and_persist.js
- [x] T4 nginx-grape.conf 增加 client_max_body_size 50m
- [x] T5 api.js _safeErrJson 降级
- [x] T6 chat.js doSend catch {status} 渲染 + 多图超时 120s
- [x] T7 运行测试（GREEN）+ 既有测试回归
- [x] T8 自审查（5 轴）
- [x] T9 建 PR（base=local-liufei）+ Issue 评论 + A2A QA
