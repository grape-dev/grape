# Plan: Issue #1748

## 设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 注入时机 | prompt 组装期（事件构建期） | 与 Issue #315 user-images 注入同层，服务端持有凭证 |
| 附件读取 | 服务端鉴权下载 → `_attach_save` 转存 → 注入平台附件 URL | 复用 OBS 签名/本地静态通道，Agent 不再 401 |
| 视频分析 | 复用 `understand_video_from_bytes` | 不重做分析能力，仅接线 |
| 失败路径 | 注入 `附件不可读 + reason` | 满足「不静默缺失」验收 |
| 去重 | 进程内 TTL 缓存（repo+issue+评论文本指纹） | 流水线多 Agent 不重复下载/转写 |

## 实现
1. `src/vcs/api_client.py`
   - `extract_gitcode_user_attachment_urls(text)`：识别 `gitcode.com/user-attachments/...`（Markdown / HTML / 裸链），去重保序。
   - `_download_user_attachment(url, token, ...)`：白名单前缀防 SSRF；接受 image/video/octet-stream；大小/超时降级。
   - `VcsApiClient.download_user_attachment`。
2. `src/api/issues_center.py`
   - `_collect_issue_media_attachments(...)`：批量提取 → 带凭证下载 → 按图片/视频分类转存为平台附件，返回 `ok/failed`。
3. `src/prompt_builder.py`
   - `_collect_comment_media_for_injection(...)`：仅评论事件（或 A2A 上下文含附件链接）触发；图片走 `understand_image_from_bytes`，视频走 `understand_video_from_bytes`；成功注入分析段落 + 读取通道，失败注入原因。
   - 接入 `_inject_attachments_by_role`，纯文本评论无附件时零注入。
4. `tests/test_issue_1748_comment_media.py`：提取/下载/采集/注入/失败/缓存/纯文本回归。