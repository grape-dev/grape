# Spec: Issue #648 — issue 附件展示链路打通（API 字段 + WebUI 附件区 + GitCode 同步过渡）

> 基线分支：weekly-08-mys（场景注入，Spec 评论已复核确认；原文 Spec 见 Issue #648 评论 2026-11-02 10:55）

## Objective
打通 issue 附件读取展示链路：
1. API 层：`issues_detail`（GET /api/issues/{owner}/{repo}/{number}）与统一列表 `issues_list`（GET /api/issues/{owner}/{repo}）响应增加 `attachments` 字段（`filename/url/size/is_image`），复用 `src/api/issues_center.py` 既有 `_attach_list`，不新建存储。
2. WebUI：issue 详情页附件区渲染（图片预览、其余下载），数据优先取详情/列表卡片 `attachments` 字段，缺省回退既有 `/issues/{issue_id}/attachments` 端点。
3. GitCode 同步过渡：抽取「## 附件」markdown 拼装为可复用函数；附件上传（post-creation，issue_number 目录）后向 VCS issue 同步一条「## 附件」链接清单评论（best-effort，失败不影响上传）。

## Scope
- In：上述三部分 + 单测。
- Out：GitCode 原生附件上传对接、上传/存储链路改造、PR 侧附件、附件管理。

## Acceptance Criteria
- [x] detail 与统一列表响应含 `attachments`（无附件为 `[]`）。
- [x] WebUI 详情页附件区：图片缩略图预览 + 下载链接；无附件显示「暂无附件」。
- [x] 上传后同步评论含「## 附件」链接清单（GitCode）。
- [x] 新增单测覆盖：markdown 拼装、_issue_attachments 聚合（含异常/空降级）、detail/list 端点字段、上传同步评论触发。
