# Issue 附件双向打通方案

> 状态：待审批 | 2026-09-11

## 一、现状分析

### 1.1 GitCode webhook 里的附件长什么样

GitCode（Gitee 系）的 issue 附件机制：用户在 GitCode 网页上传附件后，平台自动在 issue body（description）里插入 markdown 链接：
- 图片：`![图片](https://cdn-img.gitcode.com/xxxxx.png)`
- 其他文件：`[文件名](https://cdn-files.gitcode.com/xxxxx.zip)`

webhook payload 的 `object_attributes.description` 里只有纯文本（含这些 markdown 链接），**没有独立的 attachments 字段**。生产库 events 表确认：所有 issue_opened 事件的 payload 里没有任何附件专用字段。

### 1.2 Agent 当前怎么处理 issue body

```
webhook → normalize_event() → payload 完整保留
→ prompt_builder.build_devops_agent_prompt()
→ 第445行: body = issue_data.get("body", description)[:500]  ← 截断到500字符
→ 拼入 prompt 文本，传给 agent
```

问题：
- body 被截断到 500 字符，附件 URL 可能被砍掉
- 即使 URL 在，agent 也只是看到一段 URL 字符串文本，不会去下载和分析
- issue webhook 流水线完全没有调用 image_understanding 模块

### 1.3 Chat 已有的图片/视频能力（可复用）

`src/agent/image_understanding.py` 有完整管线：
- 图片：RapidOCR（PP-OCRv4）提取文字 + PIL 提取视觉特征 → VLM（qwen2.5-vl-72b）生成自然语言描述
- 视频：ffmpeg 关键帧提取 → 逐帧 VLM 分析 → LLM 合成摘要

`chat.py` 的链路：前端 base64 → `_save_chat_images()` 存盘 → `_understand_image()` 分析 → 注入 prompt 为 `## 用户上传的图片分析\n{描述}`

### 1.4 Task Center 现状

- `tasks.js` 前端：完全没有附件上传 UI（无 file input、无拖拽区）
- `tasks.py` 后端：没有附件相关路由
- `uploaded_files` 表：已建表，0 行数据，目前只用于场景市场图标

### 1.5 GitCode API 限制

GitCode API v5 **没有公开的附件上传 endpoint**。skill 里的 API 参考文档只覆盖 issue/PR/comment/branch/commit 等，无文件上传。issue 创建 `_post_gitcode_issue()` 只发 `title`/`body`/`assignee` 三个字段（form-encoded）。

## 二、方案设计

### 2.1 总体架构

```
┌─────────────┐     webhook       ┌──────────────────┐
│  GitCode    │ ──────────────────►│  Grape Webhook   │
│  Issue 附件  │  body里有markdown  │  event_pipeline  │
│  (cdn-img)  │  图片/文件链接     │                  │
└─────────────┘                   └────────┬─────────┘
                                           │
                          ┌────────────────▼────────────────┐
                          │  附件提取器（新增）              │
                          │  1.正则提取 body 里的 URL        │
                          │  2.下载图片/文件到本地临时目录    │
                          │  3.图片→image_understanding分析  │
                          │  4.注入 prompt                   │
                          └─────────────────────────────────┘
                                           │
┌─────────────┐                   ┌────────▼─────────┐
│  Task Center │   上传附件        │  Grape API       │
│  前端        │ ─────────────────►│  /tasks/{id}/    │
│  (新增上传)  │  base64/form      │  attachments     │
└─────────────┘                   │  (新增路由)       │
      ▲                           └────────┬─────────┘
      │                                    │
      │  回显附件列表                       │ 存 uploaded_files 表
      │  + 图片预览                         │ + 落盘 data/issue_attachments/
      └────────────────────────────────────┘
                                           │
                          ┌────────────────▼────────────────┐
                          │  Issue 同步（新增）              │
                          │  创建/更新 GitCode issue 时      │
                          │  把 Grape 托管的附件 URL         │
                          │  以 markdown 链接拼入 issue body │
                          └─────────────────────────────────┘
```

### 2.2 方案A：完整双向（推荐）

两个方向都做：

**方向1：GitCode Issue 附件 → Agent 可读**
1. event_pipeline 收到 issue_opened/comment 事件后，从 description 里正则提取 markdown 图片/文件链接
2. 下载图片到临时目录
3. 调 image_understanding 分析图片（OCR + VLM）
4. 把分析结果注入 agent prompt（`## Issue 附件分析` section）
5. 非图片文件：把 URL 列表和文件名注入 prompt 供 agent 自行 curl 下载

**方向2：Task Center 附件 → GitCode Issue**
1. 前端在任务详情页加附件上传区（复用 chat 的图片上传 UI 模式）
2. 后端新增 `/tasks/{task_id}/attachments` 路由，接收文件，存 uploaded_files 表 + 落盘
3. 创建/更新 GitCode issue 时，把附件 URL 以 markdown 拼入 issue body
4. 因为 GitCode 没有附件上传 API，附件由 Grape 服务器托管，URL 指向 Grape 的 `/static/issue_attachments/`

### 2.3 方案B：只做方向1（最小改动）

只让 agent 能读到 GitCode issue 里的附件，不做 Task Center 上传。

改动范围小：只在 event_pipeline/prompt_builder 里加附件提取和下载逻辑。不涉及前端和 Task Center。

## 三、详细改动清单（方案A）

### 3.1 新增模块

**`src/agent/issue_attachment.py`（新文件）**
```
def extract_attachment_urls(description: str) -> dict:
    """从 markdown 文本里提取图片URL和文件URL
    返回: {"images": [url...], "files": [{"name":..., "url":...}]}
    """

def download_attachments(urls: list, dest_dir: str) -> list:
    """下载附件到本地临时目录，返回本地路径列表"""

def analyze_issue_attachments(description: str, repo_key: str, issue_id: str) -> str:
    """主入口：提取→下载→分析→返回 prompt 注入文本
    图片: 调 image_understanding.understand_image(path)
    返回: "## Issue 附件分析\n{分析结果}" 或空字符串
    """
```

### 3.2 修改 event_pipeline.py

在 `run_pipeline()` 里，issue_opened 事件处理时：
- 调 `analyze_issue_attachments()` 获取附件分析文本
- 把分析文本挂到 evt 对象上（如 `evt["_attachment_analysis"]`）

### 3.3 修改 prompt_builder.py

- 第445行：body 截断从 500 → 2000 字符（避免附件 URL 被砍）
- 在 issue body 之后、skill 之前，注入 `evt["_attachment_analysis"]`（如果有）
- 不改变现有 prompt 结构，只在有附件时追加一个 section

### 3.4 修改 tasks.py — 新增附件路由

```
POST /api/tasks/{task_id}/attachments  — 上传附件（multipart/form-data）
GET  /api/tasks/{task_id}/attachments  — 列出附件
DELETE /api/tasks/attachments/{file_id} — 删除附件
```

- 复用 uploaded_files 表，ref_table='task', ref_id=task_id
- 文件落盘到 `data/issue_attachments/{repo_key_safe}/{issue_number}/`
- 静态服务：`/static/issue_attachments/...`（nginx 或 FastAPI StaticFiles）

### 3.5 修改 tasks.py — issue 创建同步

在 `_post_gitcode_issue()` 和创建子任务的逻辑里：
- 查询该 task 关联的 uploaded_files
- 把每个附件 URL 拼成 markdown 链接追加到 issue body 末尾：
  ```
  ## 附件
  ![图片](https://grape-host/static/issue_attachments/xxx.png)
  [文件名](https://grape-host/static/issue_attachments/xxx.zip)
  ```

### 3.6 修改 tasks.js — 前端附件上传

任务详情弹窗（`_openBoardDetail`）里加：
- 图片上传按钮（复用 chat 的 file input + base64 转换模式）
- 附件列表展示（已上传的文件名 + 图片缩略图 + 删除按钮）
- 上传后调 `/api/tasks/{task_id}/attachments`

### 3.7 DB 变更

uploaded_files 表已存在，不需要新增表。只需要：
- 约定 ref_table='task' 时 ref_id=task_id
- 确保 file_key 存储路径相对于 `data/issue_attachments/`

## 四、影响分析

### 4.1 性能影响

| 环节 | 影响 | 量级 |
|------|------|------|
| 图片下载 | 每张图 HTTP GET | 0.5-3s/张（取决于 CDN 速度） |
| OCR | RapidOCR 首次加载 ~2s，后续 ~0.5s/张 | 可接受 |
| VLM 调用 | qwen2.5-vl-72b API 调用 | 2-5s/张（外部 API 延迟） |
| 非图片文件 | 不下载，只传 URL | 无影响 |

**关键风险**：如果一个 issue 带多张图（比如截图说明 bug），附件分析总耗时可能 10-30s，会拉长 webhook → agent 启动的延迟。

**缓解**：附件分析在 pipeline 里异步执行，不阻塞 agent 启动。如果分析还没完成就先让 agent 跑，分析完成后通过 A2A 追加注入。但这会增加复杂度。更简单的方案是同步等待但有超时（比如最多等 15s，超时就只传 URL 不传分析）。

### 4.2 安全影响

1. **下载外部 URL**：需要限制只下载 `cdn-img.gitcode.com` / `cdn-files.gitcode.com` 域名的 URL，防止 SSRF（用户在 issue body 里塞一个内网 URL 骗 Grape 去访问）
2. **文件存储**：上传的附件存到 `data/issue_attachments/`，需要确保路径不能被 `../` 穿越
3. **静态文件服务**：`/static/issue_attachments/` 暴露到公网，附件 URL 可被猜测。可以用不可猜的 UUID 文件名（类似 chat_images 的做法）
4. **文件大小限制**：上传需要限制大小（比如图片 10MB、其他文件 50MB），防止磁盘撑爆

### 4.3 对现有流程的影响

1. **prompt 长度**：附件分析文本会增加 prompt 长度。一张图的 VLM 描述约 200-500 字，多张图可能让 prompt 膨胀。需要限制最多分析 N 张图（建议 5 张）
2. **body 截断改动**：500→2000 字符会影响所有 issue 的 prompt。issue body 普遍不长（生产数据确认大多是一句话），影响可忽略
3. **webhook 延迟**：如果同步做附件分析，issue_opened 事件的 agent 启动会延迟。需要评估是否可接受
4. **远端 worker**：附件分析在 server 端做（event_pipeline 里），分析文本注入 prompt 后随任务下发到 worker。worker 不需要改

### 4.4 历史数据

- 已有的 issue 没有附件分析 → 不影响，它们本来就没有附件
- uploaded_files 表 0 行 → 没有历史数据需要迁移
- 新功能只对新事件生效，不回溯

### 4.5 GitCode 平台限制

1. **Task Center 上传的附件**只能由 Grape 托管（GitCode 没有附件上传 API）。这意味着：
   - issue body 里的附件 URL 指向 Grape 服务器
   - 如果 Grape 服务器挂了或迁移了，这些链接会失效
   - 需要在 issue body 里注明「附件由 Grape 平台托管」
2. **GitCode 网页上传的附件** URL 是 GitCode CDN 的，Grape 下载时可能需要认证（取决于 GitCode 附件是否公开）。需要实测。

## 五、文件改动清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/agent/issue_attachment.py` | 新增 | 附件提取/下载/分析主模块 |
| `src/api/event_pipeline.py` | 修改 | run_pipeline 里调附件分析 |
| `src/prompt_builder.py` | 修改 | body 截断 500→2000 + 注入附件分析 |
| `src/api/routes/tasks.py` | 修改 | 新增附件 CRUD 路由 + issue body 拼接 |
| `src/api/routes/chat.py` | 参考 | 不改，复用 image_understanding 调用模式 |
| `src/api/static/js/pages/tasks.js` | 修改 | 任务详情加附件上传/展示 |
| `src/api/db_setup.py` | 不改 | uploaded_files 表已存在 |
| `src/api/main.py` | 可能改 | 挂载 issue_attachments 静态文件路由 |

## 六、待确认问题

1. **附件分析同步还是异步**：同步会延迟 agent 启动（最多 15s），异步增加复杂度。倾向同步+超时降级。
2. **非图片文件怎么处理**：只传 URL 列表给 agent 让它自己 curl？还是也下载到工作目录？
3. **Grape 托管附件的 URL**：用公网 IP 还是域名？nginx 需要加 location block？
4. **方案A还是方案B**：先只做 GitCode→Agent 方向（最小改动），还是一步到位双向？
