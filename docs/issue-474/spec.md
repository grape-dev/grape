# Spec: Issue #474 — 个人设置「凭据」接口明文回显云账号 SK（机密泄露面）

> 来源：Issue #474 + Issue Agent Spec 评论| 目标分支：weekly-09 | 优先级：P1

## 需求
`GET /api/settings/credentials` 响应体 `huawei_cloud` 块当前同时返回
`ak_exists`/`sk_exists` 以及明文 `ak`/`sk`（取库直出，未遮蔽）。SK 等同云账号操作密钥，
任意已登录用户可无审计读取原值，泄露面覆盖浏览器缓存/截图/代理与网关日志/第三方插件。

期望：凭据视图与平台「机密只回存在性」口径统一——`huawei_cloud` 块仅保留
`ak_exists`/`sk_exists`，删除明文 `ak`/`sk` 键；前端凭据页改为掩码占位交互，
不再依赖明文回显。

## 现状（缺陷）
`src/api/routes/settings.py` `get_credentials`（settings_credentials_get）：
```python
"huawei_cloud": {
    "ak_exists": bool(hw_cred.get("ak")),
    "sk_exists": bool(hw_cred.get("sk")),
    "ak": hw_cred.get("ak", ""),   # ← 明文泄露点
    "sk": hw_cred.get("sk", ""),   # ← 明文泄露点
},
```
同响应 `codearts` 块仅返回 `ak_exists`/`sk_exists`（无明文）——同视图内口径已不一致，
`huawei_cloud` 为唯一明文泄露点；devstage `/config` 亦为「仅 *_exists」口径。
`PUT /credentials` 写路径（`_is_placeholder` 兜底）不受影响。

## 范围
- In：
  1. `src/api/routes/settings.py` 凭据视图删除 `huawei_cloud.ak/sk` 两个明文字段，仅留
     `ak_exists`/`sk_exists`；同步更新路由 `description`（标注 credentials are never
     returned in plaintext）。
  2. `src/api/static/js/pages/account.js` 凭据 tab：输入框改 `********` 掩码占位
     （`*_exists` 为 true 时），移除 👁 明文切换与真实值记录，保留「未改动不回传」与
     「清空显式清除」语义。
  3. `src/api/static/js/pages/scene-market.js`（新增识别的前端明文消费方）：华为云场景
     apply 弹窗当前用 `hw.ak`/`hw.sk` 自动填入 `HUAWEICLOUD_SDK_AK/SK`，接口收敛后改为
     空行 + 提示（不再承诺自动填入明文）。
  4. 新增 `tests/test_issue474_credentials_no_plaintext.py`：断言响应无明文密钥键、
     `*_exists` 语义正确、codearts 块不回退。
- Out：一次性显隐接口（二次认证+审计+当次返回）待产品确认另立 Issue；gitcode/github
  token 状态块、运行时凭据 `list_runtime_credentials` 已符合口径，不动；生产部署验证归 sre。

## 验收
- [ ] `GET /api/settings/credentials` 响应 `huawei_cloud` 块仅含 `ak_exists`/`sk_exists`，
      不含 `ak`/`sk` 键（有值、无值两种状态均断言）
- [ ] `codearts` 块不回退（仍仅存在性、有值可正确反映）
- [ ] 前端凭据页：已有凭据显示 `********` 掩码，未配置显示空框；输入新值可保存；
      清空可显式清除；未改动不回传
- [ ] 华为云场景 apply 弹窗不再回显明文，提示语与行为一致
- [ ] 新增单测通过 + 现有测试套件（settings/credentials 相关）不回归
---

# 追加：方案A —— 按需取回明文接口（2026-10-29 用户确认）

> 用户对「移除 👁 明文切换按钮」的回复：「这个功能我不想删掉，你看下该怎么办」→
> QA 给出方案A（保留 👁，改走独立按需取回明文接口），用户确认「方案A」。

## 需求（在 PR #673 既有收敛之上追加）

- `GET /api/settings/credentials` 维持「仅回 *_exists」不变（不恢复明文回显）。
- 新增独立接口 `POST /api/settings/credentials/reveal`：
  - 请求：`{provider: "huawei_cloud"|"codearts", field: "ak"|"sk", password: "<当前 Grape 密码>"}`
  - 二次确认：重新输入当前 Grape 密码（错误/缺失 → 400；未设置密码 → 400 + need_set_password）
  - 服务端从库中取原值、当次返回一次：`{provider, field, value}`，响应 `Cache-Control: no-store`
  - 审计日志：`credential_reveal`（user_id/ip/detail.provider/detail.field，不落明文值）
  - 高频取回告警：窗口（10 分钟）内取回次数 ≥ 10 → 429 + 服务端告警日志
- 前端凭据页保留 👁 按钮（已配置时才显示）：点击弹二次确认框 → 调 reveal 接口 →
  明文临时写入输入框展示（type=text，仅本次内存态）；再次点击恢复掩码 `********`；
  不落任何前端缓存（localStorage/sessionStorage/变量持久化均不存明文）。
- 单测覆盖：二次确认失败、正确取回当次返回、列表接口仍无明文（回归）、审计日志、
  高频告警 429、未设置密码拒绝、未知 provider/field 拒绝。

## 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 明文来源 | 列表接口回显 / 独立接口 | 独立接口 | 列表接口仍只回存在性，泄露面不恢复 |
| 二次确认方式 | 重新输入密码 / 仅登录态 | 重新输入密码 | 对齐「二次认证」期望，防止已登录会话被冒用 |
| 明文返回策略 | 每次成功即返回 / 一次性令牌 | 每次成功即返回（no-store） | 服务端无状态、实现简单；配合审计+高频告警兜底 |
| 审计粒度 | 仅记 provider / provider+field+ip | provider+field+ip | 满足「谁/何时/取了哪个凭据」 |
| 高频告警 | 仅打日志 / 计数限流+告警 | 计数限流+告警日志 | 既能阻断滥用，也有服务端告警线索 |
