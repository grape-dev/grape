---
name: blog-publish
description: "Use when publishing blog posts via the Blog System API."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [blog, publishing, api, content]
    related_skills: []
---

# Blog Publish

## Overview

Publish blog posts to the Blog System API. FastAPI + MySQL, no authentication required. Supports articles, comments, messages, moods, labels, and users.

## Configuration

Before using this skill, the target server address must be determined. Replace `{BLOG_HOST}` in all commands below with the actual deployment address in the format `IP:PORT` (e.g., `192.168.1.100:18080`).

The `{BLOG_HOST}` value is resolved in this priority order:

1. **Project Knowledge** — check `project-info/member/blog-config.yaml` (or similar config file) for `blog.host` field.
2. **Current context** — the user provides it or the current conversation context already contains it; use it directly as `{BLOG_HOST}`.
3. **Workspace files** — search the project's knowledge directory at `/opt/grape-workspaces/{owner}/{repo}/project-info/` for a file containing the blog system deployment address.
4. **Ask user** — if none of the above found, ask the user for the target server address before proceeding.

## When to Use

- User asks to publish / create / post a blog article
- User asks to update / edit / delete a blog post
- User asks to list articles, view detail, or check hot articles
- User asks to publish a mood (说说) or leave a message (留言)
- User asks to create labels or users for the blog system

Don't use for: non-blog content or when the API is not reachable.

## API Base URL

```
http://{BLOG_HOST}
```

All endpoints below are relative to this base. No auth header needed.

## Endpoints

### Articles (文章)

| Method | Path | Body | Description |
|--------|------|------|-------------|
| GET | `/api/articles?page=1&size=10&lid=0&keyword=` | — | List articles (paginated, filter by label/keyword) |
| GET | `/api/articles/{id}` | — | Get article detail (increments heat) |
| POST | `/api/articles` | `{title, content, uid, lid, img, heat}` | Publish new article |
| PUT | `/api/articles/{id}` | `{title?, content?, lid?, img?, heat?}` | Update article |
| DELETE | `/api/articles/{id}?soft=true` | — | Delete (soft default, soft=false for hard) |
| POST | `/api/articles/{id}/restore` | — | Restore soft-deleted article |
| GET | `/api/articles/heat/top?limit=5` | — | Top hot articles |

### Labels (标签)

| Method | Path | Body | Description |
|--------|------|------|-------------|
| GET | `/api/lables` | — | List all labels |
| POST | `/api/lables` | `{lname}` | Create label |

### Comments (评论)

| Method | Path | Body | Description |
|--------|------|------|-------------|
| GET | `/api/comments/{aid}` | — | List comments for article |
| POST | `/api/comments` | `{uid, aid, content}` | Add comment |
| DELETE | `/api/comments/{id}` | — | Delete comment (soft) |

### Messages (留言)

| Method | Path | Body | Description |
|--------|------|------|-------------|
| GET | `/api/messages` | — | List messages with replies |
| POST | `/api/messages` | `{uid, content}` | Post message |
| POST | `/api/messages/reply` | `{uid, mid, content}` | Reply to message |
| DELETE | `/api/messages/{id}` | — | Delete message (soft) |

### Moods (说说)

| Method | Path | Body | Description |
|--------|------|------|-------------|
| GET | `/api/moods` | — | List moods |
| POST | `/api/moods` | `{title, content, src}` | Publish mood |
| DELETE | `/api/moods/{id}` | — | Delete mood |

### Users (用户)

| Method | Path | Body | Description |
|--------|------|------|-------------|
| GET | `/api/users` | — | List users |
| POST | `/api/users` | `{uname, phone, pwd, email, img}` | Create user |

### Upload (上传文件)

| Method | Path | Body | Description |
|--------|------|------|-------------|
| POST | `/api/upload` | `multipart/form-data: file=@文件` | Upload image/video, returns `{url: "/uploads/xxx.png"}` |

### Pages (前端页面)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Blog home page (HTML) |
| GET | `/article/{id}` | Article detail page (HTML) |
| GET | `/docs` | Swagger API docs |
| GET | `/health` | Health check |

## Publish an Article

Step-by-step workflow:

### 1. Check API is reachable

```bash
curl -s http://{BLOG_HOST}/health
```
Expect: `{"status":"ok","service":"blog-api","version":"1.0.0"}`

### 2. Get available labels

```bash
curl -s http://{BLOG_HOST}/api/lables
```

### 3. Upload media files to blog server

Media files (images, cover, video) are stored locally in the blog output directory. They must be uploaded to the blog server first, and relative paths in the HTML must be replaced with the upload URLs.

Determine `OUTPUT_DIR` from the A2A context (coding agent handoff provides the product path). If not provided, look in:
```
/opt/grape-workspaces/{owner}/{repo}/issues/{issue_number}/coding/blog_output/
```

```python
import os, json, subprocess, glob, re

BLOG_HOST = "{BLOG_HOST}"  # Resolved from project knowledge or context
OUTPUT_DIR = "/opt/grape-workspaces/..."  # From A2A context
UPLOAD_URL = f"http://{BLOG_HOST}/api/upload"

# Step 3a: Upload all media files
upload_map = {}  # rel_path -> upload_url
for ext in ["*.png", "*.jpg", "*.jpeg", "*.gif", "*.mp4"]:
    for fp in glob.glob(os.path.join(OUTPUT_DIR, "**", ext), recursive=True):
        rel = os.path.relpath(fp, OUTPUT_DIR)
        print(f"Uploading: {rel}...")
        r = subprocess.run(["curl", "-s", "-X", "POST", UPLOAD_URL,
                           "-F", f"file=@{fp}"], capture_output=True, text=True)
        d = json.loads(r.stdout)
        if d.get("code") == 200:
            upload_map[rel] = d["data"]["url"]
            print(f"  -> {d['data']['url']}")
        else:
            print(f"  FAILED: {r.stdout}")

# Step 3b: Replace relative paths in index.html with upload URLs
html_path = os.path.join(OUTPUT_DIR, "index.html")
with open(html_path, "r", encoding="utf-8") as f:
    html = f.read()

for rel_path, upload_url in sorted(upload_map.items(), key=lambda x: -len(x[0])):
    html = html.replace(f'src="{rel_path}"', f'src="{upload_url}"')
    html = html.replace(f'poster="{rel_path}"', f'poster="{upload_url}"')

with open(html_path, "w", encoding="utf-8") as f:
    f.write(html)

print("HTML updated with upload URLs")
```

**Important**: After this step, all `src="intro.mp4"`, `poster="cover.png"`, `src="images/theme_1.png"` etc. are replaced with `/uploads/xxx.xxx` paths served by the blog server.

### 4. Extract title and cover image URL from the updated HTML

```python
import re
with open(os.path.join(OUTPUT_DIR, "index.html"), "r") as f:
    html = f.read()

# Extract title (<h1>)
m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.DOTALL)
BLOG_TITLE = m.group(1).strip() if m else "无标题"

# Extract cover image URL (first poster or first img)
BLOG_IMG = ""
m = re.search(r'poster="(/uploads/[^"]+)"', html)
if m:
    BLOG_IMG = m.group(1)
else:
    m = re.search(r'<img[^>]+src="(/uploads/[^"]+)"', html)
    if m:
        BLOG_IMG = m.group(1)

print(f"Title: {BLOG_TITLE}")
print(f"Cover: {BLOG_IMG}")
```

### 5. Choose label ID

From the label list obtained in step 2, pick the matching label ID for the article topic. Common labels:
| ID | Name |
|----|------|
| 1 | 技术 |
| 2 | 生活 |
| 3 | 随笔 |
| 4 | 教程 |

If the topic is food/美食/product introduction and no matching label exists, create one with `POST /api/lables {"lname": "美食"}`.

### 6. Publish article to blog API

```python
import json
article = {
    "title": BLOG_TITLE,
    "content": html,       # Full HTML with uploaded URLs
    "uid": 1,
    "lid": LABEL_ID,       # From step 5
    "img": BLOG_IMG,
    "heat": 0
}

with open("/tmp/blog_article.json", "w", encoding="utf-8") as f:
    json.dump(article, f, ensure_ascii=False)
```

```bash
curl -s -X POST http://{BLOG_HOST}/api/articles \
  -H "Content-Type: application/json" \
  -d @/tmp/blog_article.json
```
Expect: `{"code":200,"message":"文章发布成功","data":{"id":N}}`

### 7. Verify

```bash
curl -s http://{BLOG_HOST}/api/articles/{id}
```

### 8. View in browser

```bash
echo "http://{BLOG_HOST}/article/{id}"
```

The article page should now load images and video from the blog server's `/uploads/` directory.

### Article Fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| title | string | Yes | — | Article title |
| content | string | Yes | — | Article body (plain text) |
| uid | int | No | 1 | Author user ID |
| lid | int | No | 1 | Label ID (1=技术, 2=生活, 3=随笔, 4=教程) |
| img | string | No | null | Cover image path |
| heat | int | No | 0 | Initial heat value |

## Update an Article

```bash
curl -s -X PUT http://{BLOG_HOST}/api/articles/{id} \
  -H "Content-Type: application/json" \
  -d '{"title": "新标题", "content": "新内容"}'
```

All fields optional: title, content, lid, img, heat.

## Delete / Restore

```bash
# Soft delete (default, can restore)
curl -s -X DELETE http://{BLOG_HOST}/api/articles/{id}

# Hard delete (permanent)
curl -s -X DELETE "http://{BLOG_HOST}/api/articles/{id}?soft=false"

# Restore soft-deleted article
curl -s -X POST http://{BLOG_HOST}/api/articles/{id}/restore
```

## Publish a Mood (说说)

```bash
curl -s -X POST http://{BLOG_HOST}/api/moods \
  -H "Content-Type: application/json" \
  -d '{"title":"今日心情","content":"今天完成了博客系统","src":"img/mood1.jpg"}'
```

## Post a Comment

```bash
curl -s -X POST http://{BLOG_HOST}/api/comments \
  -H "Content-Type: application/json" \
  -d '{"uid":1,"aid":1,"content":"很棒的文章！"}'
```

## Leave a Message (留言)

```bash
# Post message
curl -s -X POST http://{BLOG_HOST}/api/messages \
  -H "Content-Type: application/json" \
  -d '{"uid":1,"content":"你好，很高兴来到这个博客！"}'

# Reply to message
curl -s -X POST http://{BLOG_HOST}/api/messages/reply \
  -H "Content-Type: application/json" \
  -d '{"uid":1,"mid":1,"content":"谢谢支持！"}'
```

## Create a Label

```bash
curl -s -X POST http://{BLOG_HOST}/api/lables \
  -H "Content-Type: application/json" \
  -d '{"lname":"新标签"}'
```

## Create a User

```bash
curl -s -X POST http://{BLOG_HOST}/api/users \
  -H "Content-Type: application/json" \
  -d '{"uname":"作者名","phone":"13800000000","pwd":"123456","email":"author@example.com"}'
```

## Common Pitfalls

1. **API not reachable.** Always check `/health` first. The API runs on port 18080 — security group must open TCP 18080.

2. **Wrong label ID.** Labels are not hardcoded — query `/api/lables` first to get current IDs. New labels can be created via POST.

3. **Content with special characters.** Use proper JSON escaping. For long content with quotes/newlines, use a temp file:
   ```bash
   cat > /tmp/article.json << 'EOF'
   {"title":"标题","content":"包含\"引号\"的内容","uid":1,"lid":1}
   EOF
   curl -s -X POST http://{BLOG_HOST}/api/articles \
     -H "Content-Type: application/json" -d @/tmp/article.json
   ```

4. **Deleting without confirming.** Soft delete is default and recoverable. Hard delete (soft=false) is permanent. Always confirm with user before hard delete.

5. **uid mismatch.** If the user hasn't created a blog user account, uid defaults to 1 (admin). Create a user first if authorship matters.

## Verification Checklist

- [ ] `GET /health` returns `{"status":"ok"}`
- [ ] `POST /api/articles` returns `{"code":200,"data":{"id":N}}`
- [ ] `GET /api/articles/{id}` returns the created article with correct title/content
- [ ] Article is visible on home page at `http://{BLOG_HOST}/`
- [ ] Article detail page at `http://{BLOG_HOST}/article/{id}` renders correctly
