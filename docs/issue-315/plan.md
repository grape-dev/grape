# Plan: Issue #315 — 实现设计

## 链路
```
event → build_devops_agent_prompt
          └─ _inject_attachments_by_role(parts, repo_key, issue_id, agent_type)
               ├─ 既有：_attach_list(owner, repo, issue_num)  → 平台上传附件（OBS/本地）
               └─ 新增：_collect_issue_user_images(owner, repo, issue_num, texts)
                         ├─ extract_gitcode_user_image_urls(正文+评论)   [api_client.py]
                         ├─ create_client(repo_key).download_user_image() [api_client.py, Bearer]
                         └─ _attach_save(..., "user-image-{i}.{ext}", content) [issues_center.py]
```

## 1. src/vcs/api_client.py（纯函数 + helper）

### extract_gitcode_user_image_urls(text) -> list[str]
- 正则：`https://raw.gitcode.com/user-images/[^\s\)"'<>]+`
- 来源形态：`![alt](url)`、`<img src="url">`、裸 URL；右边界排除 `)]"'<>` 与空白。
- 去重保序（dict.fromkeys）。
- 纯函数，无 IO，可直接单测。

### VcsApiClient.download_user_image(url, max_size=20MB, timeout=15s) -> dict
- 仅 gitcode provider；URL 必须 `https://raw.gitcode.com/user-images/` 前缀（防 SSRF 到任意地址）。
- GET，头 `Authorization: Bearer {self.token}` + `Accept: image/*`。
- Content-Type 非 `image/*` → `{"error": "non-image content-type: ..."}`（已读 body 丢弃）。
- 流式读：累计 > max_size → `{"error": "image exceeds size limit (N bytes)"}` 并中断。
- HTTPError → `{"error": "HTTP 403"}` 等；URLError/timeout → `{"error": "timeout" / str(e)}`。
- 日志只记 URL + 错误 + 大小，绝不记 token / Authorization 头。
- 成功 → `{"content": bytes, "content_type": "image/png", "size": n}`。

## 2. src/api/issues_center.py（编排 + 存储）

### _collect_issue_user_images(owner, repo, issue_number, texts) -> dict
- `texts`：可迭代字符串（正文、各评论）。
- 提取 → 上限 `_USER_IMAGE_MAX_COUNT = 8`，超出部分记失败原因 `exceeds count limit`（降级说明，不静默）。
- 逐个下载（token 解析失败 → 该图失败 `no repo token`）。
- 成功 → 扩展名从 URL 末段取（默认 `.png`）→ 文件名 `user-image-{seq}.{ext}`（seq 从 1 递增，跳过已存在重名）→ `_attach_save(owner, repo, issue_number, filename, content)`。
- 返回 `{"ok": [{"filename", "url", "size"}], "failed": [{"url", "reason"}]}`；顶层 try/except，异常返回 `{"ok": [], "failed": [...], "error": str(e)}`。
- 不写 token，不抛异常到调用方。

## 3. src/prompt_builder.py（注入）

### _inject_attachments_by_role 扩展
- 既有 `_attach_list` 扫描后（v: images/files 分级前），新增调用：
  - `texts` 组装：正文（payload issue body/description）+ A2A context + 用户评论 note（事件 payload 内已有字段；不再额外拉 API——保持注入函数零网络调用的既有约束）。
  - `_collect_issue_user_images(...)` 结果缓存键 `(repo_key, issue_id)`，进程内 dict，防止重复采集。
- 注入格式（追加在既有 `## User Attachments` 语义下，独立小节）：
  - 成功（vod/coding）：`- user image user-image-1.png (from GitCode editor upload): {url}` + 复用各角色既有指引文案。
  - 成功（issue）：并入文件名清单。
  - 失败（所有角色）：`- ⚠️ user image fetch failed: {url_short} ({reason})`，原因用短码（HTTP 403 / timeout / non-image / size limit / count limit）。
- 失败说明对 vod/issue/coding 一律可见（无静默丢失）；qa/sre 维持不注入（与既有角色分级一致）。

## 4. tests/test_issue_user_images_315.py
- 识别：MD 图、HTML img、裸链、混合去重、非 user-images 域不匹配、右边界截断正确。
- 下载：mock urlopen → 200 image/png 成功返回 content；403 → error；text/html → non-image；超 max_size → size limit；超时 → timeout。
- 编排：monkeypatch create_client + _attach_save → 成功清单/失败清单/数量上限/异常不外抛。
- 注入：build prompt 各 agent_type → 成功 URL 出现；失败说明出现；qa 不出现；token 不出现在 prompt。
- conftest 强制 SQLite + 独立测试库（既有约定）。

## 安全要点
- token 仅进入请求头构造；日志/注入/返回值零 token。
- URL 白名单前缀防 SSRF；上限（8 张/20MB/15s）防资源滥用。
- 失败全部降级注入，不阻塞 prompt 组装主流程。
