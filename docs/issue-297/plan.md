# Plan: Issue #297

## 改动设计（TDD：先写测试再实现）

### 1. nginx-grape.conf（部署层放行）
server 块增加 `client_max_body_size 50m;`（对齐前端 9×5MB 上限 + base64 开销）。
uvicorn/FastAPI 无请求体上限（已核对 start_server.py / Dockerfile），无需改动。

### 2. src/api/static/js/api.js — handleResp 非 ok 降级
新增 `_safeErrJson(r)`：content-type 含 `text/html` 直接返回 null；否则 `r.json().catch(() => null)`。
handleResp 三个非 ok 分支（401+retry、401 无 retry、通用）改用 `_safeErrJson`：
- err 为 null → 通用分支 message 回退 `I18N.t('common.request_failed', {status: r.status})`，`e.status = r.status`
- 401 分支 err 为 null → 走既有 session_expired / handle401 路径
杜绝 `Unexpected token '<'` 裸异常进入用户提示。

### 3. src/api/static/js/pages/chat.js — doSend 错误分支 + 超时
- catch 分支：`renderMarkdown(e.message || I18N.t('common.request_failed', {status: e.status || ''}))`，不再拼接 `request_failed`（无参）+ 裸 message 双重渲染。
- 多图/视频 submit 请求体大、上传耗时：`API.post('/chat/submit', …, 120000)`，避免默认 30s 超时误报。

### 4. 后端 chat.py
`/chat/submit` 多图链路核对结论：`_save_chat_images`（JSON 数组解析 + PIL 压缩落盘）、`_build_image_markers`、`_understand_image`（多图分段描述）、`add_message`（submit 即落用户消息 + task_id/generate_status=1）、agent_fn finally 落库（generate_status=2）均已支持多图，无代码缺陷——丢失根因是请求被 nginx 413 拒绝。仅补测试覆盖，不改实现。

## 测试计划
- `tests/test_chat_multi_image_297.py`：
  - `_save_chat_images` 多图（2 张 data URL JSON 数组）→ 2 个 URL + 文件落盘
  - `/chat/submit` 多图 → 用户消息含 2 个 `[chat_img:]` marker + agent 回复落库（generate_status=2），`GET /chat/messages/{cid}` 可恢复（模拟 F5）
- `tests/js/test_issue_297_multi_image_error_and_persist.js`（node 静态走查，同仓库既有模式）：
  - api.js 含 `_safeErrJson` 降级 + `{status: r.status}` 回退
  - chat.js doSend catch 传 `{status: e.status}`、无旧拼接模式、多图超时 120000
  - nginx-grape.conf 含 `client_max_body_size 50m`
  - zh/en `request_failed` 均含 `{{status}}`

## 提交拆分（原子）
1. `feat: #297 nginx client_max_body_size 50m（多图 base64 请求体放行）`
2. `feat: #297 前端 handleResp 非 JSON 错误降级 + doSend 错误提示 status 渲染`
3. `test: #297 多图 submit 持久化与前端降级测试`
