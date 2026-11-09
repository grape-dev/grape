# Plan: Issue #648 实现方案

## 改动点

| 文件 | 改动 |
|------|------|
| `src/api/issues_center.py` | ① 新增 `_attachments_markdown_block(attachments)`：拼「## 附件」链接清单（图片 `![]()`，其余 `[]()`）；② 建单路径（原 :1420 内联拼装）改用该函数；③ 新增 `sync_issue_attachment_comment(repo_key, issue_number, attachment)`：向 VCS issue 发「## 附件」评论（VcsApiClient + get_gitcode_token，best-effort） |
| `src/api/routes/issues.py` | ① 新增 `_issue_attachments(repo_keys, issue_number)` 辅助：按 repo_key 顺序调 `_attach_list(owner, rname, str(number))`，异常/空降级 `[]`；② `get_issue_detail` 返回追加 `attachments`；③ `list_repo_issues` 富化循环为每张 issue 卡片加 `attachments`；④ `upload_issue_attachment`（post-creation 路径）保存后调 `sync_issue_attachment_comment` |
| `src/api/static/js/pages/issues.js` | `_loadTaskAttachments` 拆出 `_renderTaskAttachments`；卡片自带 `attachments` 数组时直接渲染（免二次请求），否则回退既有端点；上传/删除成功后强制走端点刷新 |
| `tests/test_issue_648_attachments_display.py` | 后端单测（markdown 拼装 / 聚合降级 / detail & list 端点字段 / 上传同步评论） |
| `tests/js/test_issue_648_attachments_inline.js` | 前端单测（内联渲染优先 + 回退 + 空态） |

## 设计决策
- attachments 来源 = `_attach_list(owner, rname, str(issue_number))`（已建单附件目录；pending 为建单前临时目录，不入 issue 详情，避免跨 issue 污染）。
- 同步评论按「单次上传单条评论」追加，只列本次上传文件，幂等性由上传动作天然保证。
- 同步失败仅日志告警，不影响上传响应（过渡方案，不阻塞主流程）。

## 任务拆解（task.md 见同目录）
1. issues_center：markdown 函数 + 建单路径复用 + 同步评论函数（含单测）
2. routes/issues：_issue_attachments + detail/list 字段（含单测）
3. routes/issues：上传端点接同步评论（含单测）
4. 前端 issues.js 渲染改造（含 JS 单测）
5. 回归：相关既有测试
