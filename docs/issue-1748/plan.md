# Plan: Issue #1748

## 设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 注入时机 | prompt 组装期（事件构建期） | 与 Issue #315 user-images 注入同层，服务端持有凭证 |
| 附件读取 | 服务端鉴权下载 → `_attach_save` 转存 → 注入平台附件 URL | 复用 OBS 签名/本地静态通道，Agent 不再 401 |
| 视频分析 | 复用 `understand_video_from_bytes` | 不重做分析能力，仅接线 |
| 失败路径 | 注入 `附件不可读 + reason`；采集顶层异常同样注入原因且不缓存 | 满足「不静默缺失」验收 |
| 范围门控 | 仅 `issue_comment` 事件正文触发（不读 A2A 上下文） | 贴合 Spec L4，避免任意事件扩面与缓存指纹漂移 |
| 附件边界 | 媒体扩展名白名单 + UUID 文件名；非媒体预过滤不下载 | 防公开静态目录存储型 XSS、跨评论覆盖与越界采集 |
| 去重 | 进程内 TTL 缓存（repo+issue+评论文本指纹），仅缓存成功/降级结果 | 流水线多 Agent 不重复下载/转写 |

## 实现
1. `src/vcs/api_client.py`
   - `extract_gitcode_user_attachment_urls(text)`：识别 `gitcode.com/user-attachments/...`（Markdown / HTML / 裸链），去重保序。
   - `_download_authed(...)`：`_download_user_image` / `_download_user_attachment` 共用鉴权下载（白名单前缀防 SSRF、大小/超时降级）。
   - `attachment_kind` / `_attachment_ext` / `is_media_attachment_url`：统一媒体扩展名白名单（图片/视频），非白名单扩展名不透传。
   - `VcsApiClient.download_user_attachment`。
2. `src/api/issues_center.py`
   - `_collect_authed_attachments(...)`：`_collect_issue_user_images` 与 `_collect_issue_media_attachments` 共用的参数化采集编排。
   - `_collect_issue_media_attachments(...)`：仅媒体（image/video）落盘；UUID 命名；非媒体预过滤/Content-Type 复查后忽略，返回 `ok/failed/error`。
   - `_ATTACH_IMAGE_EXTS` 与下载侧对齐（纳入 `.bmp`）。
3. `src/prompt_builder.py`
   - `_collect_comment_media_for_injection(...)`：仅 `issue_comment` 触发；图片走 `understand_image_from_bytes`，视频走 `understand_video_from_bytes`；成功注入分析段落 + 读取通道（所有角色），失败/采集异常注入原因。
   - 分析摘要限长 `_COMMENT_MEDIA_ANALYSIS_MAX_CHARS`；异常结果不缓存。
   - 接入 `_inject_attachments_by_role`，纯文本评论无附件时零注入。
4. `tests/test_issue_1748_comment_media.py`：提取/下载/采集/注入/失败/缓存/白名单/UUID/异常传播/纯文本回归。