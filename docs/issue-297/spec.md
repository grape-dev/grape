# Spec: Issue #297 — Chat 多图发送错误 + F5 消息丢失

> 来源：Issue #297 Spec（Issue 评论 id=193254200，issue agent 产出，评级 ready）。本文件为开发侧存档副本。

## 需求
1. 修复 Chat 页面上传 ≥2 张图片发送后返回 `请求失败 ({{status}})Unexpected token '<'` 的错误。
2. 修复 F5 刷新后图片消息与返回消息丢失（根因同 1：请求被拒，从未落库）。

## 根因链
两张 ~5MB 图片 base64 后请求体 ≈13MB+ → 超过 nginx 默认 `client_max_body_size 1m` → nginx 直接返回 HTML 413 错误页（不转发后端）→ 前端 `handleResp` 非 ok 分支无条件 `r.json()` → 抛 `Unexpected token '<'`；同时 `chat.js doSend` catch 分支 `I18N.t('common.request_failed')` 未传 `{status}` → `{{status}}` 占位符未渲染。请求未到达后端 → 无任何消息落库 → F5 后全部丢失。

## In Scope
- `nginx-grape.conf`：`client_max_body_size 50m`
- `src/api/static/js/api.js`：`handleResp` 非 ok 分支 HTML/非 JSON 降级
- `src/api/static/js/pages/chat.js`：`doSend` 错误分支传 `{status}`；多图请求加长超时
- `src/api/routes/chat.py`：多图 submit 持久化链路（已支持，补测试覆盖）
- i18n `locales/{zh,en}/common.json`：`request_failed` 占位符核对（文案已正确，无需改）

## Out of Scope
视频链路重构、Chat UI 改版、图片理解模型升级、IM 侧推送。

## 验收标准
- 两张图片（纯图/图+文字）发送正常返回 AI 响应
- 错误场景提示为 `请求失败 (413)` 形式（{{status}} 被实际状态码替换）
- F5 刷新后图片消息与返回消息保留
- 单图/纯文字无回归；现有测试通过 + 新增测试覆盖
