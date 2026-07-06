# GitCode Webhook 事件类型与 Payload 格式

## 目录

1. [通用 Payload 结构](#通用结构)
2. [Push 事件](#push-事件)
3. [Pull Request 事件](#pull-request-事件)
4. [Issue 事件](#issue-事件)
5. [Issue 评论事件](#issue-评论事件)
6. [PR 评论事件](#pr-评论事件)
7. [Tag 推送事件](#tag-推送事件)
8. [Release 事件](#release-事件)
9. [成员事件](#成员事件)
10. [Webhook 验证](#webhook-签名验证)

---

## 通用结构

所有 Webhook 请求均为 `POST`，`Content-Type: application/json`。

### 通用 HTTP Headers

| Header | 说明 |
|--------|------|
| `X-GitCode-Event` | 事件类型名称 |
| `X-GitCode-Token` | Webhook 配置时设置的 Secret（如已配置） |
| `X-GitCode-Signature` | HMAC-SHA256 签名（如已配置） |
| `Content-Type` | `application/json` |

### 通用 Payload 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `object_kind` | string | 事件类型（与 X-GitCode-Event 对应） |
| `repository` | object | 仓库信息 |
| `user` / `sender` | object | 触发者信息 |

---

## Push 事件

**X-GitCode-Event**: `push`

```json
{
  "object_kind": "push",
  "ref": "refs/heads/main",
  "before": "abc123...",
  "after": "def456...",
  "repository": {
    "id": 123,
    "name": "my-repo",
    "full_name": "owner/my-repo",
    "html_url": "https://gitcode.com/owner/my-repo",
    "default_branch": "main"
  },
  "user_name": "developer",
  "user": {
    "id": 1,
    "username": "developer",
    "name": "Developer Name"
  },
  "commits": [
    {
      "id": "def456...",
      "message": "feat: add new feature",
      "timestamp": "2024-01-15T10:30:00+08:00",
      "author": {
        "name": "Developer",
        "email": "dev@example.com"
      },
      "added": ["src/new_file.go"],
      "modified": ["src/existing_file.go"],
      "removed": []
    }
  ],
  "total_commits_count": 1
}
```

---

## Pull Request 事件

**X-GitCode-Event**: `pull_request`

```json
{
  "object_kind": "pull_request",
  "action": "open",
  "number": 42,
  "pull_request": {
    "id": 100,
    "number": 42,
    "title": "Add new feature",
    "body": "PR description in markdown",
    "state": "open",
    "html_url": "https://gitcode.com/owner/repo/pulls/42",
    "source_branch": "feature-branch",
    "target_branch": "main",
    "source": {
      "name": "owner/repo",
      "branch": "feature-branch"
    },
    "target": {
      "name": "owner/repo",
      "branch": "main"
    },
    "author": {
      "id": 1,
      "username": "developer"
    },
    "created_at": "2024-01-15T10:00:00+08:00",
    "updated_at": "2024-01-15T10:00:00+08:00",
    "merge_commit_sha": null,
    "merged": false
  },
  "repository": {
    "id": 123,
    "full_name": "owner/repo"
  },
  "sender": {
    "id": 1,
    "username": "developer"
  }
}
```

**action 取值**：
- `open` — PR 创建
- `update` — PR 更新（新提交推送）
- `close` — PR 关闭（未合并）
- `merge` — PR 合并
- `review` — 审查提交

---

## Issue 事件

**X-GitCode-Event**: `issues`

```json
{
  "object_kind": "issues",
  "action": "open",
  "issue": {
    "id": 200,
    "number": 15,
    "title": "Bug report",
    "body": "Issue description",
    "state": "open",
    "html_url": "https://gitcode.com/owner/repo/issues/15",
    "author": {
      "id": 1,
      "username": "reporter"
    },
    "assignees": [],
    "labels": [
      {"id": 1, "name": "bug", "color": "ff0000"}
    ],
    "created_at": "2024-01-15T10:00:00+08:00",
    "updated_at": "2024-01-15T10:00:00+08:00"
  },
  "repository": {
    "full_name": "owner/repo"
  },
  "sender": {
    "username": "reporter"
  }
}
```

**action 取值**：`open`, `close`, `reopen`, `update`, `assign`, `label`

---

## Issue 评论事件

**X-GitCode-Event**: `issue_comment`

```json
{
  "object_kind": "issue_comment",
  "action": "created",
  "issue": {
    "number": 15,
    "title": "Bug report"
  },
  "comment": {
    "id": 300,
    "body": "Comment content in markdown",
    "author": {
      "username": "commenter"
    },
    "created_at": "2024-01-15T11:00:00+08:00"
  },
  "repository": {
    "full_name": "owner/repo"
  }
}
```

---

## PR 评论事件

**X-GitCode-Event**: `pull_request_comment`

```json
{
  "object_kind": "pull_request_comment",
  "action": "created",
  "pull_request": {
    "number": 42,
    "title": "Add new feature"
  },
  "comment": {
    "id": 400,
    "body": "Code review comment",
    "author": {
      "username": "reviewer"
    },
    "path": "src/main.go",
    "position": 10,
    "commit_id": "abc123"
  },
  "repository": {
    "full_name": "owner/repo"
  }
}
```

---

## Tag 推送事件

**X-GitCode-Event**: `tag_push`

```json
{
  "object_kind": "tag_push",
  "ref": "refs/tags/v1.0.0",
  "before": "0000000000000000000000000000000000000000",
  "after": "abc123...",
  "repository": {
    "full_name": "owner/repo"
  },
  "user_name": "developer"
}
```

---

## Release 事件

**X-GitCode-Event**: `release`

```json
{
  "object_kind": "release",
  "action": "published",
  "release": {
    "id": 500,
    "tag_name": "v1.0.0",
    "name": "Release v1.0.0",
    "body": "Release notes",
    "draft": false,
    "prerelease": false,
    "html_url": "https://gitcode.com/owner/repo/releases/v1.0.0",
    "author": {
      "username": "developer"
    }
  },
  "repository": {
    "full_name": "owner/repo"
  }
}
```

---

## 成员事件

**X-GitCode-Event**: `member`

```json
{
  "object_kind": "member",
  "action": "added",
  "member": {
    "id": 2,
    "username": "new-member"
  },
  "repository": {
    "full_name": "owner/repo"
  }
}
```

---

## Webhook 签名验证

如果创建 Webhook 时配置了 Secret，GitCode 会在每个请求中附带签名：

### Token 方式（简单）

Header `X-GitCode-Token` 的值等于配置的 Secret。服务端直接比对即可。

### HMAC-SHA256 签名方式

1. 用 Secret 作为 key，对整个 request body 计算 HMAC-SHA256
2. 签名放在 `X-GitCode-Signature` Header 中，格式为 `sha256=<hex>`
3. 服务端验证：

```go
import (
    "crypto/hmac"
    "crypto/sha256"
    "encoding/hex"
)

func verifySignature(body []byte, signature string, secret string) bool {
    mac := hmac.New(sha256.New, []byte(secret))
    mac.Write(body)
    expected := "sha256=" + hex.EncodeToString(mac.Sum(nil))
    return hmac.Equal([]byte(signature), []byte(expected))
}
```

### 安全建议

- 始终配置 Secret，不要使用空 Secret
- 验证签名后再处理事件，防止伪造请求
- 使用 HTTPS 的 Webhook URL
