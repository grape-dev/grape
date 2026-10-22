# Tasks: Issue #315

- [x] T1 `src/vcs/api_client.py`：`extract_gitcode_user_image_urls(text)` 提取函数 + `VcsApiClient.download_user_image()` 鉴权下载 helper（Bearer、Content-Type 校验、大小 20MB/超时 15s/数量 8 上限、URL 白名单防 SSRF、token 不落日志）
- [x] T2 `src/api/issues_center.py`：`_collect_issue_user_images(owner, repo, issue_number, texts)` —— 正文+评论聚合提取 → 鉴权下载 → `_attach_save` 落盘 → 成功/失败清单；数量/大小/超时上限；顶层异常不外抛
- [x] T3 `src/prompt_builder.py`：`_inject_attachments_by_role` 注入 user-images 成功项（vod/coding/issue 分级）+ 失败降级说明（含原因短码，无静默丢失）；(repo_key, issue) 进程内缓存 TTL 600s 防重复采集
- [x] T4 `tests/test_issue_user_images_315.py`：31 用例——识别（MD/HTML/裸链/去重/边界/非目标域）、下载（成功/Authorization 头/403/非图片/超大/超时/白名单）、编排（成功/混合失败/数量上限/无 token/save 失败降级/异常不外抛）、注入（vod/coding/issue/qa 分级、texts 透传、缓存、token 不进 prompt、build_devops_agent_prompt 全链路）
- [x] T5 回归：新增测试 31 passed；相关既有测试（prompt_builder/issues_center/attach/api_client 涉及的 45 个测试文件）回归
- [x] T6 提交 feat/issue-315（8f765dfa3）→ 推送 → 建 PR #571（base=功能分支）→ Issue 评论告知 → A2A QA（）
