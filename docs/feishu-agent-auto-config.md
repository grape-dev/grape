# 飞书智能体扫码自动配置 — 设计文档

## 概述

通过飞书扫码创建智能体（AI Agent），自动获取 `app_id` 和 `app_secret`，实现飞书机器人与 DevOps Bot 的一键建连，无需管理员手动在飞书开发者后台创建应用并复制凭据。

## 背景

当前用户需要在飞书开放平台手动创建应用、获取 `app_id`/`app_secret`、配置事件订阅、再填入 admin 面板，步骤繁琐且容易出错。阿里云 OpenClaw 提供的「扫码配置」极大简化了此流程。

## 阿里云方案参考

### 用户视角的流程

```
1. 在 OpenClaw 控制台点击「扫码配置」
2. 页面生成一个二维码（等待 30s-2min）
3. 用户用飞书扫描二维码
4. 飞书内打开「创建智能体」页面
5. 用户填写智能体名称等信息，点击创建
6. 创建成功后，OpenClaw 自动获取到 app_id + app_secret
7. 飞书机器人自动完成连接
```

### 技术推测

阿里云利用了飞书开放平台的以下能力：

1. **智能体创建 Deep Link** — 生成一个指向飞书内智能体创建页面的 URL，通过二维码分发
2. **智能体创建回调** — 飞书在用户创建智能体后，通过 webhook 将 `app_id` 和 `app_secret` 推送给平台方
3. **ISV 平台身份** — 阿里云在飞书注册了平台应用，作为智能体创建的代理方

## DevOps Bot 实现方案

### 整体架构

```
┌─ 浏览器 ─┐       ┌─ DevOps Bot ─┐       ┌─ 飞书开放平台 ─┐
│          │       │              │       │               │
│ 点击配置  │ ───→ │ 生成 QR code │       │               │
│ 扫码     │ ←─── │ 展示二维码   │       │               │
│ (飞书)   │ ──────────────→ 创建智能体页面  │
│ 填写创建 │       │              │       │               │
│          │       │ ←──────────── webhook 推送凭据 ── │
│          │       │ 存储凭据     │       │               │
│          │       │ 建立 WS 连接 │       │               │
│          │ ←─── │ 配置完成     │       │               │
└──────────┘       └──────────────┘       └───────────────┘
```

### 所需飞书开放平台能力

| 能力 | 说明 | 状态 |
|------|------|------|
| ISV 应用注册 | DevOps Bot 需在飞书注册一个 ISV 应用作为平台身份 | 待调研开通 |
| 智能体创建回调 URL | 配置一个 webhook 地址，飞书在智能体创建后推送凭据 | 待飞书审核 |
| Deep Link 生成 | 生成 `feishu://` 或 `https://` 格式的智能体创建链接 | 待确认接口 |

### 组件变更

#### 1. 新增 API 端点

```
GET /api/feishu/agent/qrcode
  → 生成智能体创建二维码
  → 返回二维码图片或 token
  → 认证: Admin Token

GET /api/feishu/agent/callback
  → 飞书回调端点（在飞书开放平台配置的 webhook URL）
  → 接收 app_id + app_secret
  → 认证: 飞书签名验证

GET /api/feishu/agent/status?token=xxx
  → 轮询查询配置状态
  → 返回: pending / success / failed
```

#### 2. 数据库变更

`user_accounts` 表已有字段（无需新增）：
- `feishu_app_id`
- `feishu_app_secret`

新增一个过程状态表或字段：

```sql
ALTER TABLE user_accounts ADD COLUMN feishu_agent_status TEXT DEFAULT '';
-- 值: 'pending' | 'connected' | 'failed'
```

或新增独立表：

```sql
CREATE TABLE IF NOT EXISTS feishu_agent_setup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT UNIQUE NOT NULL,          -- 轮询 token
    user_id INTEGER NOT NULL,
    app_id TEXT DEFAULT '',
    app_secret TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',       -- pending | connected | failed
    created_at TEXT DEFAULT (datetime('now'))
);
```

#### 3. Admin 面板 UI 变更

在飞书分组增加「扫码配置」按钮：

```
┌─ 飞书 ──────────────────────────┐
│  App ID    [________________]   │
│  App Secret [________________]   │
│  [扫码自动配置]  ← 新增按钮      │
│  点击后用飞书扫描下方二维码...    │
│  ┌──────────────────────┐       │
│  │      QR Code         │       │
│  │     ██ █  ██ ██      │       │
│  │     █ ███ █ ██ █     │       │
│  │     ██ █ ██ █  ██    │       │
│  └──────────────────────┘       │
│  状态: ⏳ 等待配置...            │
└─────────────────────────────────┘
```

### 服务端流程

```
POST /api/feishu/agent/qrcode
  1. 生成唯一 token
  2. 存入 feishu_agent_setup (status=pending)
  3. 调飞书 API 获取创建智能体的 deep link URL
  4. 将 deep link 转为二维码
  5. 返回二维码 image + token

飞书回调 → GET /api/feishu/agent/callback
  1. 验签（飞书 HMAC 签名）
  2. 解析 app_id, app_secret
  3. 根据回调中的 token 找到对应记录
  4. 存储到 user_accounts (feishu_app_id, feishu_app_secret)
  5. 建立飞书 WS 连接
  6. 更新状态为 connected

前端轮询 → GET /api/feishu/agent/status?token=xxx
  1. 查询 feishu_agent_setup.status
  2. 返回状态
  3. 前端根据状态更新 UI
```

## 未解决问题

| 问题 | 说明 |
|------|------|
| ISV 应用注册流程 | 需要在飞书开放平台注册为服务商，审批周期不确定 |
| 智能体创建 Deep Link 格式 | 不确定飞书对外提供的 URL scheme 是什么，需要从飞书 SDK 或文档确认 |
| 回调签名验证 | 飞书 webhook 回调的签名方式有待确认 |
| 是否需要付费 | 飞书 ISV / 智能体能力可能涉及商务合作 |
| 多租户支持 | 同一个 DevOps Bot 实例接入多个飞书租户的策略 |

## 后续行动

1. 在飞书开放平台注册 ISV 开发者账号
2. 调研「智能体创建回调」API 的具体参数和接入方式
3. 调研飞书是否提供「代客户创建应用」的 OAuth scope
4. 实现原型验证流程可行性
