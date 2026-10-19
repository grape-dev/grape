# 4. IM 机器人接入

Grape 通过 IM（飞书）机器人向用户推送通知、接收指令。自建部署需要完成「扫码建应用 → 绑定 → 验证」三步。

## 4.1 飞书扫码配置（app_id / app_secret）

**在哪配**：无需手工在飞书开放平台建应用——使用控制台的**飞书扫码接入**流程（基于 lark-oapi `aregister_app`）：用户扫码创建飞书应用，自动获取 `app_id` / `app_secret` 并绑定到当前用户。

**配置示例**（扫码完成后系统自动写入，无需手工填写）：

```
应用凭证（自动获取，勿手工修改）：
  app_id:     cli_xxxxxxxx（系统生成）
  app_secret: <system-generated-secret>
```

- 一个 `app_id` **全局唯一**：已被 VOD Agent 使用的凭证不能同时绑定 Chat Agent（强约束校验）。
- 首次绑定后，机器人以该应用身份建立长连接接收消息。

**如何验证**：扫码接入页显示任务状态 `status=success` 且回显 `app_id`；随后在飞书中给机器人发一条消息，Grape 控制台对应会话出现消息记录。

## 4.2 lease 多实例机制（一句话版）

多实例部署时，**每个飞书机器人只会与「owner 实例」建立连接**：集群通过一致性哈希环 + 租约（lease）决定哪个实例持有某 `app_id`；非 owner 实例不连接，lease 过期后自动切换（优雅让渡）。因此看到「只有一台实例连着某个 bot」是**预期行为**，不是故障。

## 4.3 如何验证机器人在线（#365 检查清单收敛）

**验证 1：日志关键字（必须命中）**

在持有 lease 的实例上查服务日志，必须出现：

```
[lease] bot <bot-name> connected after acquiring lease
```

- 出现该行 = 该实例拿到 lease 并成功建立 IM 长连接。
- **没有该行却「配置都正确」** → 依次排查：本实例是否是 owner（哈希环）、DB 中该 `app_id` 的 `lease_owner` / `lease_expires_at` 是否过期未清理、应用凭证是否被改动。

**验证 2：test-notify（端到端通道验证）**

调用测试告警接口，向**你自己的 IM 身份**发送一条测试消息（经你自己的 chat bot 投递）：

```
POST /api/obs/alerts/test-notify
```

- 返回 `ok: true` 且飞书收到消息 → 通知通道端到端可用。
- 返回 `reason=no_open_id`（无 IM 身份）/ `reason=no_bot`（无可用 bot）/ `reason=send_failed`（发送失败）→ 按对应缺失项补配置。

> 💡 **提示**
>
> 排障顺序建议：先看 `connected after acquiring lease` 日志确认连接在位，再用 `test-notify` 验证投递通道，最后才去怀疑应用凭证本身。
