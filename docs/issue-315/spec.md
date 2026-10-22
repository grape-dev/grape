# Spec: Issue #315 — Agent 访问 GitCode 编辑器上传截图（服务端鉴权下载 + 附件注入）

> 继承 Issue 评论已发布 Spec（Issue Agent，2026-10-13，评级 ready），基线分支更新为 功能分支。本文件为开发侧落地版，与 Spec 语义一致。

## 目标
平台组装 Agent 上下文时，对 Issue 正文/评论中经 GitCode 编辑器上传的图片（`raw.gitcode.com/user-images/...`）做服务端鉴权下载，并纳入现有附件注入体系（`## User Attachments`），使 Agent 能读取图片内容；拉取失败注入明确降级说明，不静默丢失、不阻塞主流程。

## 现状（VOD 已实测）
- 该类图片属 GitCode user-images 私有资源，匿名拉取 → HTTP 403 `{"message":"You have no access right to this project"}`；携带仓库 bot token（Authorization Bearer）→ HTTP 200 image/png。
- 平台附件体系（`issues_center._attach_save/_attach_list`，OBS 签名 URL 或 `/static/issue_attachments/`）已注入 `## User Attachments`，但编辑器上传图片不走平台上传接口，不在清单中。

## 方案
1. **识别**：`src/vcs/api_client.py` 新增 `extract_gitcode_user_image_urls(text)` —— 从 Markdown/裸链文本中提取 `raw.gitcode.com/user-images` 图片 URL（含 `![alt](url)`、`<img src="url">`、裸 URL；去重保序）。
2. **下载 helper**：`VcsApiClient.download_user_image(url, max_size, timeout)` —— GET 携带 `Authorization: Bearer {token}`，流式读、先校验 Content-Type 为 image/*、超大小/超时中断；返回 `{"content", "content_type", "size"}` 或 `{"error"}`；token 仅用于请求头，不进日志与返回值。
3. **下载落盘**：`src/api/issues_center.py` 新增 `_collect_issue_user_images(owner, repo, issue_number, texts)` —— 组合正文+评论文本 → 提取链接 → 逐个鉴权下载 → 复用 `_attach_save` 存储（文件名 `user-image-{i}.{ext}`）→ 汇总成功/失败清单；单 Issue 图片数上限、大小/超时上限、整体异常不外抛。
4. **注入**：`src/prompt_builder.py` `_inject_attachments_by_role` —— 在既有 OBS/本地附件扫描后追加 user-images 段：成功项与平台附件同级注入（vod: URL+分析指引；coding: URL+下载指引；issue: 文件名），失败项注入 `⚠️ 图片 X 拉取失败(原因)` 降级说明；同一 Issue 只采一次（按 repo+issue 缓存，TTL 一次流水线）。

## 范围
- In：正文+评论 user-images 图片识别、鉴权下载、附件注入、失败降级、单测。
- Out：第三方图床外链鉴权下载（仅降级说明）、图片内容理解（image_understanding 既有体系）、前端 UI。

## 验收标准
- [ ] 含编辑器上传图片的 Issue（#314 场景）触发 Agent 时，上下文 `## User Attachments` 含可访问图片
- [ ] 403/超时/非图片/超大时上下文含明确失败说明（含原因），无静默丢失
- [ ] 大小与超时上限生效，超大图不阻塞事件处理
- [ ] token 仅服务端使用，不进注入上下文与日志
- [ ] 现有测试全部通过；新增单测覆盖识别/下载成功/失败降级分支

## 影响模块
| 模块 | 改动 |
|------|------|
| src/vcs/api_client.py | 新增 user-images URL 提取纯函数 + VcsApiClient.download_user_image 带鉴权下载 helper |
| src/api/issues_center.py | 新增 _collect_issue_user_images：文本聚合→下载→_attach_save 存储→结果清单 |
| src/prompt_builder.py | _inject_attachments_by_role 追加 user-images 成功注入 + 失败降级说明 |
| tests/test_issue_user_images_315.py | 新增：识别/下载成功/403/非图片/超大/超时/注入分支单测 |

## 风险与对策
| 风险 | 对策 |
|------|------|
| bot token 对 user-images 权限失效 | 失败即降级说明+日志，不阻塞主流程 |
| 大图/多图拖慢事件处理 | 大小/超时/数量上限，超限降级 |
| 凭证泄漏 | token 仅构造请求头，不写入日志/注入上下文/返回值 |
