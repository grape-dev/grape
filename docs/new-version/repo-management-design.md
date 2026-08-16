# DevOps Bot 仓库托管管理 — 设计文档

## 概述

为 DevOps Bot 增加仓库托管管理功能。用户（或管理员）可通过面板调用 GitCode API 获取当前 token 下拥有管理员权限的仓库列表，选择后将 DevOps Bot 的 webhook URL 自动注册到该仓库上（监听全部事件），使仓库纳入 Bot 的自动化管理范围。

## 新增数据库表

### managed_repos 表（`sessions.db`）

```sql
CREATE TABLE IF NOT EXISTS managed_repos (
    user_id INTEGER NOT NULL,
    repo_owner TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    webhook_id INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, repo_owner, repo_name)
);
```

| 字段 | 说明 |
|------|------|
| `user_id` | 关联 `users.id` |
| `repo_owner` | 仓库所有者 |
| `repo_name` | 仓库名称 |
| `full_name` | `owner/repo` |
| `webhook_id` | GitCode webhook ID（用于删除） |

## GitCode API

| 接口 | 用途 |
|------|------|
| `GET /api/v5/user/repos?type=all&per_page=100&page={n}` | 列出用户仓库，翻页，过滤 `permissions.admin=true` |
| `POST /api/v5/repos/{owner}/{repo}/hooks` | 创建 webhook，URL=`http://203.0.113.2:8080/webhook/gitcode`，密码=`gitcode_webhook_token`，全开事件 |
| `DELETE /api/v5/repos/{owner}/{repo}/hooks/{id}` | 删除 webhook |

## 模块

- **`src/user/repo_manager.py`** — `fetch_admin_repos()` / `setup_webhook()` / `remove_webhook()`
- **`src/user/db.py`** — `managed_repos` 表 CRUD
- **`src/admin/server.py`** — UI + 路由

## 路由

| 路径 | 用途 |
|------|------|
| POST `/user/repo-sync` | 用户面板：加载可托管仓库（调用 GitCode API） |
| POST `/user/repo-add` | 用户面板：添加托管（创建 webhook + 存 DB） |
| POST `/user/repo-remove` | 用户面板：移除托管（删除 webhook + 删 DB） |
| POST `/admin/repo-sync` | 管理员面板：同上 |
| POST `/admin/repo-add` | 管理员面板：同上 |
| POST `/admin/repo-remove` | 管理员面板：同上 |

## 安全

- 删除托管时同时删除 GitCode 侧 webhook
- API token 仅服务端使用
