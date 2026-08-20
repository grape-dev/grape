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

Publish blog posts to the Blog System API at `http://123.249.19.227:18080`. FastAPI + MySQL, no authentication required. Supports articles, comments, messages, moods, labels, and users.

## When to Use

- User asks to publish / create / post a blog article
- User asks to update / edit / delete a blog post
- User asks to list articles, view detail, or check hot articles
- User asks to publish a mood (说说) or leave a message (留言)
- User asks to create labels or users for the blog system

Don't use for: non-blog content or when the API is not reachable.

## API Base URL

```
http://123.249.19.227:18080
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

### Pages (前端页面)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Blog home page (HTML) |
| GET | `/article/{id}` | Article detail page (HTML) |
| GET | `/docs` | Swagger API docs |
| GET | `/health` | Health check |

## Publish an Article

Step-by-step workflow:

1. Check API is reachable:
   ```bash
   curl -s http://123.249.19.227:18080/health
   ```
   Expect: `{"status":"ok","service":"blog-api","version":"1.0.0"}`

2. Get available labels:
   ```bash
   curl -s http://123.249.19.227:18080/api/lables
   ```

3. Publish article:
   ```bash
   curl -s -X POST http://123.249.19.227:18080/api/articles \
     -H "Content-Type: application/json" \
     -d '{
       "title": "文章标题",
       "content": "文章正文内容",
       "uid": 1,
       "lid": 1,
       "img": null,
       "heat": 0
     }'
   ```
   Response: `{"code":200,"message":"文章发布成功","data":{"id":N}}`

4. Verify: `curl -s http://123.249.19.227:18080/api/articles/{id}`

5. View in browser: `http://123.249.19.227:18080/article/{id}`

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
curl -s -X PUT http://123.249.19.227:18080/api/articles/{id} \
  -H "Content-Type: application/json" \
  -d '{"title": "新标题", "content": "新内容"}'
```

All fields optional: title, content, lid, img, heat.

## Delete / Restore

```bash
# Soft delete (default, can restore)
curl -s -X DELETE http://123.249.19.227:18080/api/articles/{id}

# Hard delete (permanent)
curl -s -X DELETE "http://123.249.19.227:18080/api/articles/{id}?soft=false"

# Restore soft-deleted article
curl -s -X POST http://123.249.19.227:18080/api/articles/{id}/restore
```

## Publish a Mood (说说)

```bash
curl -s -X POST http://123.249.19.227:18080/api/moods \
  -H "Content-Type: application/json" \
  -d '{"title":"今日心情","content":"今天完成了博客系统","src":"img/mood1.jpg"}'
```

## Post a Comment

```bash
curl -s -X POST http://123.249.19.227:18080/api/comments \
  -H "Content-Type: application/json" \
  -d '{"uid":1,"aid":1,"content":"很棒的文章！"}'
```

## Leave a Message (留言)

```bash
# Post message
curl -s -X POST http://123.249.19.227:18080/api/messages \
  -H "Content-Type: application/json" \
  -d '{"uid":1,"content":"你好，很高兴来到这个博客！"}'

# Reply to message
curl -s -X POST http://123.249.19.227:18080/api/messages/reply \
  -H "Content-Type: application/json" \
  -d '{"uid":1,"mid":1,"content":"谢谢支持！"}'
```

## Create a Label

```bash
curl -s -X POST http://123.249.19.227:18080/api/lables \
  -H "Content-Type: application/json" \
  -d '{"lname":"新标签"}'
```

## Create a User

```bash
curl -s -X POST http://123.249.19.227:18080/api/users \
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
   curl -s -X POST http://123.249.19.227:18080/api/articles \
     -H "Content-Type: application/json" -d @/tmp/article.json
   ```

4. **Deleting without confirming.** Soft delete is default and recoverable. Hard delete (soft=false) is permanent. Always confirm with user before hard delete.

5. **uid mismatch.** If the user hasn't created a blog user account, uid defaults to 1 (admin). Create a user first if authorship matters.

## Verification Checklist

- [ ] `GET /health` returns `{"status":"ok"}`
- [ ] `POST /api/articles` returns `{"code":200,"data":{"id":N}}`
- [ ] `GET /api/articles/{id}` returns the created article with correct title/content
- [ ] Article is visible on home page at `http://123.249.19.227:18080/`
- [ ] Article detail page at `http://123.249.19.227:18080/article/{id}` renders correctly
