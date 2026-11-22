# 需求：OAuth userinfo 端点与字段口径（华为渠道现场，2026-11-21）

> 来源：华为 OAuth 登录「换 code 成功、拉用户信息 500」现场（回调 `?oauth_error=user_info_failed`）。
> 结论：**主因是配置写错端点，不是代码能力缺失**；代码侧只补可观测性与昵称兜底。
> 实现：配置 3 字段 + `providers.py` 诊断/兜底（本单）。

## 1. 现场与结论

```
现象  00:33:35 GET /api/auth/callback/huawei?code=…     换 code 成功（华为已发 access_token）
      00:33:35 GET /?oauth_error=user_info_failed       拉用户信息这一步挂了
      日志   Get user info for huawei failed: 500 Server Error for url:
             …oauth-api.cloud.huawei.com/…user.info.get&access_token=***
实测   现配端点          → HTTP 500，text/html（华为错误页）
      正确端点（account） → HTTP 200，JSON（DUMMY token 回 {"error":"session timeout"}）
      另一候选 oauth-login → HTTP 404（端点不存在）
```

三处配置错，都在渠道卡上：

```
user_url    指向 oauth-api…user.info.get（错端点，必然 500）
name_field  nickName（返回体无此字段 → 昵称恒空，且不报错）
scope       openid（缺 profile，昵称/头像拿不到）
其余正确：auth_url / token_url / id_field=openID / avatar_field=headPictureURL /
          pkce_enabled=false / redirect_uri（AGC 侧已登记，否则换 code 不会成功）
```

## 2. 目标

```
G1  渠道卡指向华为正确的 userinfo 端点，登录能进站
G2  昵称不再静默为空：卡字段配错时回落候选字段并告警
G3  userinfo 失败可自诊：日志带状态码、打码端点、响应片段（不含 token）
G4  生效路径可解释：改文件后「同步到表 → 进程缓存」的时序写进运维口径
```

## 3. 需求条目

```
R1  huawei 渠道卡（configs/oauth-providers.json 与 .example 同步）：
      user_url   = https://account.cloud.huawei.com/rest.php
                   ?nsp_svc=GOpen.User.getInfo&nsp_fmt=JSON&getNickName=1&access_token={access_token}
      scope      = "openid profile email"
      name_field = "displayName"
R2  代码不新增平台分支：{access_token} 占位与 Bearer 互斥的能力已有（providers.py），
    本次只加固而非重写
R3  userinfo 非 2xx 必须留一条可检索的日志：provider、HTTP 状态、打码后的端点、
    响应片段（≤300 字符，去换行）；token 值不得进日志
R4  昵称口径：卡的 name_field 取不到时，按 displayName → nickName → name → login → userName
    依次回落，命中则告警「疑似字段名配错」；全不命中则告警响应键列表，仍返回空串（不阻断登录）
R5  头像口径：avatar_field 缺失/为空 → 空串，不报错、不阻断
R6  生效路径（运维口径，不做代码自动扫描）：
      configs/oauth-providers.json --启动期 migrate_oauth_providers--> grape_t_oauth_channels 表
      读取顺序 文件卡 → meta 逐键 → 表整卡覆盖 ⇒ 表是运行时真源
      进程内 provider 缓存 300s ⇒ 重启后仍可能短暂读到旧值（验收别在重启后立刻下结论）
R7  OAuth state 单次消费保持现状（SELECT 后 DELETE，且 expires > now）：
    同一 state 二次回调必 state_invalid，属独立议题，不在本单修
```

## 4. 验收标准

```
A1  两个配置文件 huawei.user_url 指向 account.cloud.huawei.com/rest.php 且带 {access_token}
A2  huawei.scope 为 "openid profile email"；/api/auth/login/huawei 的跳转 URL 带同样 scope
A3  huawei.name_field 为 displayName；响应含 displayName 时 external_name 非空
A4  卡 name_field 配错（响应只有 displayName）→ external_name 回落 displayName + 一条告警
A5  userinfo 返回 500 → get_user_info 返回 None，日志含 HTTP 500 与 account/oauth-api 端点（token 打码）
A6  avatar_field 指向不存在键 → external_avatar 空串，登录流程不抛错
A7  不新增表/列；不改 id_field / avatar_field / pkce / redirect_uri
A8  既有 oauth 相关用例全绿（本分支预存失败不新增）
```

## 5. 场景用例

```
S1  {access_token} 占位 → token 进查询参数且**不发** Authorization 头
S2  无占位 → 发 Authorization: Bearer
S3  name_field 命中 → external_name 取该字段
S4  name_field 未命中 → 回落候选字段 + 告警（不静默空名）
S5  userinfo 500 → 返回 None；日志含状态码、打码端点与响应片段，不含 token
S6  avatar_field 缺失 → 空串，不抛
S7  配置口径锁定：两个文件的 huawei 三字段 + build_auth_url 携带 scope
```

## 6. 非目标

```
· 不做启动期「扫描配置合法性」的自动治理（属运维口径，写进文档而非代码）
· 不改 OAuth App / AGC 侧登记
· 不动 state 单次消费语义（独立议题）
· 不引入华为专属分支（卡驱动仍是唯一入口）
```