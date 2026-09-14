# Issue 附件打通方案 v2

> 状态：待审批 | 2026-09-11

## 一、思路

附件在 issue body 里就是 markdown 链接。不在流水线创建时做任何预处理，零延迟。把链接丢进 prompt，agent 自己按需读取：

- **文本/代码/压缩包**：agent 用 bash 工具 curl 下载，read tool 读取
- **图片**：agent curl 调用 Grape 暴露的 `/api/analyze-image` 端点，VLM 分析后返回文字描述
- **网页链接**：agent 直接 curl 拿内容

Task Center 上传的附件落盘到 `data/issue_attachments/`，创建 issue 时 URL 拼进 body。不存 DB，文件系统就是真相。

## 二、实测数据

image_understanding 管线已在本机跑通（vlm-config.json 已配、rapidocr_onnxruntime + Pillow 已装）：

| 环节 | 耗时 |
|------|------|
| OCR | 0.54s |
| VLM 真视觉（qwen2.5-vl-72b） | 5.62s |
| **单张图端到端** | **~5s** |

VLM 耗时发生在 agent 执行期间，不阻塞流水线创建。agent 按需调用——不需要图片信息就不调，需要才花 5 秒。

## 三、改动清单

### 3.1 prompt_builder.py — body 截断放大（2处）

| 位置 | 现状 | 改为 |
|------|------|------|
| 第445行 `build_devops_agent_prompt` | `[:500]` | `[:4000]` |
| 第1027行 `build_task_chat_prompt` | `[:1000]` | `[:4000]` |

原因：附件以 markdown 链接嵌在 body 里，500 字符可能砍掉链接。生产数据确认 issue body 普遍很短，放大到 4000 不会让 prompt 膨胀。

### 3.2 prompt_builder.py — body 后追加附件读取指引

在 body 注入之后加一段（有附件链接时才生效）：

```python
# 检测 body 里有没有附件链接
import re
_has_img = re.search(r'!\[.*?\]\(https?://', body)
_has_file = re.search(r'\[.*?\]\(https?://', body)
if _has_img or _has_file:
    parts.append("""
## 附件处理指引
Issue body 中可能包含附件链接（图片、文件、网页 URL）。请按需读取：
- 图片链接（![...](url)）：用 bash 工具 curl 调用 {SERVER_URL}/api/analyze-image?url=<图片URL>，返回图片内容描述。
- 文本/代码/文档链接：用 bash 工具 curl 下载后用 read tool 读取。
- 网页链接：直接 curl 获取内容。
不相关附件可忽略，不要浪费时间下载所有附件。
""")
```

要点：
- `{SERVER_URL}` 在 worker 端会被替换为实际服务地址（已有占位符替换逻辑，同 `{{SERVER_URL}}`）
- 没有附件链接时这段不出现，零影响
- agent 自己判断是否需要读附件，不做强制

### 3.3 新增 `/api/analyze-image` 端点

在 `src/api/routes/chat.py` 或独立路由文件里加一个轻量端点：

```python
@router.get("/analyze-image")
async def analyze_image(url: str, repo_key: str = ""):
    """供 agent curl 调用：下载图片 → VLM 分析 → 返回文字描述。"""
    # 1. 域名白名单校验（防 SSRF）
    from urllib.parse import urlparse
    parsed = urlparse(url)
    allowed = ("cdn-img.gitcode.com", "cdn-files.gitcode.com")
    if parsed.hostname not in allowed:
        return {"error": "domain not allowed"}
    # 2. 下载图片到临时文件
    import tempfile, urllib.request
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    urllib.request.urlretrieve(url, tmp.name)
    # 3. 调 image_understanding 分析
    from src.agent.image_understanding import understand_image
    desc = understand_image(tmp.name)
    os.unlink(tmp.name)
    # 4. 返回描述
    return {"url": url, "description": desc or "(无法分析)"}
```

- 用 GET 方便 agent 直接 `curl {SERVER_URL}/api/analyze-image?url=xxx`
- 域名白名单防 SSRF
- 复用已验证的 image_understanding 三级管线（VLM → OCR+LLM → 纯OCR）
- 不需要认证——URL 参数本身不含敏感信息，分析结果也不含

### 3.4 tasks.py — Task Center 附件上传路由

新增上传端点，文件落盘到 `data/issue_attachments/{owner}/{repo}/{issue_number}/`：

```python
@router.post("/{task_id}/attachments")
async def upload_task_attachment(task_id: int, file: UploadFile = File(...),
                                 user: dict = Depends(get_current_user)):
    # 查 task → 拿 repo_key + issue_number
    # UUID 文件名保存到 data/issue_attachments/{owner}/{repo}/{issue_number}/{uuid}{ext}
    # 返回 {"url": "/static/issue_attachments/...", "filename": original_name}
```

- UUID 文件名（防猜测，同 chat_images 模式）
- 限制大小：图片 10MB、其他文件 50MB
- 不存 DB，文件系统就是真相

对应地，`create_task_via_issue` 创建 issue 时，把已上传附件 URL 拼进 body：

```python
# 在 _post_gitcode_issue 之前
import os
attach_dir = f"data/issue_attachments/{owner}/{repo}/{issue_number}"
attachments = []
if os.path.isdir(attach_dir):
    for f in os.listdir(attach_dir):
        ext = os.path.splitext(f)[1].lower()
        if ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
            attachments.append(f"![{f}](/static/issue_attachments/{owner}/{repo}/{issue_number}/{f})")
        else:
            attachments.append(f"[{f}](/static/issue_attachments/{owner}/{repo}/{issue_number}/{f})")
if attachments:
    description += "\n\n## 附件\n" + "\n".join(attachments)
```

### 3.5 tasks.js — 前端上传按钮

在任务详情弹窗 `_detailHtml` 底部加附件区域，复用 chat.js 的 file input 模式，但用 multipart/form-data POST 到 `/api/tasks/{task_id}/attachments`。展示已上传附件列表 + 删除按钮。

### 3.6 main.py — 静态文件服务

```python
app.mount("/static/issue_attachments", StaticFiles(directory="data/issue_attachments"), name="issue_attachments")
```

## 四、文件改动总表

| 文件 | 改动 | 量级 |
|------|------|------|
| `src/prompt_builder.py` | 截断 500→4000（2处）+ 附件指引（~10行） | 小 |
| `src/api/routes/chat.py` 或新路由 | `/api/analyze-image` 端点（~20行） | 小 |
| `src/api/routes/tasks.py` | 附件上传路由 + body 拼接（~40行） | 中 |
| `src/api/static/js/pages/tasks.js` | 上传按钮+附件列表（~60行） | 中 |
| `src/api/main.py` | 挂载静态目录（1行） | 极小 |

不新增独立模块文件，不改 DB schema，不碰 uploaded_files 表。

## 五、影响分析

**流水线延迟**：零。不在 event_pipeline 里做任何预处理，agent 启动不受附件影响。图片分析 5s/张发生在 agent 执行期间，且 agent 按需调用——不需要就不花时间。

**安全**：
- SSRF：`/api/analyze-image` 域名白名单（cdn-img.gitcode.com / cdn-files.gitcode.com）
- 路径穿越：UUID 文件名
- 文件大小：上传限 10MB/50MB

**历史数据**：无影响。已有 issue 没有附件指引，新功能只对新事件生效。

**远端 worker**：`/api/analyze-image` 是服务端端点，agent 通过 `{SERVER_URL}` 调用。worker 不需要装 rapidocr/VLM 依赖，VLM 在服务端跑。

**Grape 托管附件**：Task Center 上传的文件存在 Grape 服务器，URL 指向 `/static/issue_attachments/`。issue body 里这些 URL 在 GitCode 上可点击下载。服务器迁移需同步搬文件。
