# 设计：OAuth userinfo 端点与字段口径（华为渠道，2026-11-21）

> 需求：`oauth-userinfo-endpoint-requirements.md`｜场景用例：`tests/mgmt/test_oauth_userinfo_scenarios.py`（S1–S7）
> 结论一句话：**代码能力早就够，错在渠道卡的三处值；代码只补诊断与昵称兜底。**

## 1. 现状事实（逐条可复验）

```
配置   configs/oauth-providers.json（md5 0b1c2620…，example 同步）
       user_url = https://oauth-api.cloud.huawei.com/rest.php?nsp_fmt=JSON&nsp_svc=
                  com.huawei.developer.openapi.user.info.get&access_token={access_token}
       scope = "openid"   name_field = "nickName"
       id_field = openID  avatar_field = headPictureURL  pkce_enabled = false

代码   providers.py:705-711   _token_in_url = "{access_token}" in user_url
                              命中 → 替换为 urlencode(token)，且不再发 Authorization: Bearer
       providers.py:715/717   按卡的 name_field / avatar_field 取值
       providers.py:521-522   scope 参与授权 URL（改卡上的 scope 即改登录跳转）
       providers.py:152       _CACHE_TTL = 300（进程内缓存）

库     grape_t_oauth_channels 的 huawei 行与被改前的文件同值（表是运行时最后一层）

生效   文件卡 --启动期 migrate_oauth_providers --> _sync_providers_from_config
      （mgmt/user/db.py:3689）**写表、不写 meta**（meta 写入已在 034c36aed 删除）
      _load_providers（providers.py:200-260）顺序：文件卡 → meta 逐键 → **表整卡覆盖**
```

现场证据（另两台节点）：换 code 成功的同秒回调 `?oauth_error=user_info_failed`，
日志 `Get user info for huawei failed: 500 Server Error for url: …user.info.get&access_token=…`；
同 URL 直连 500（text/html），正确端点回 200 JSON（DUMMY token → `{"error":"session timeout"}`），
`oauth-login…/v3/userinfo` 回 404。本机 arch11 日志里这类失败 0 条 —— 现场不在本机。

## 2. 根因

```
① user_url 指向 oauth-api…user.info.get：该端点对 GET 回 500 HTML，换 code 成功后必然挂在
   拉用户信息这一步；与凭据无关（DUMMY token 也能拿到 500，说明请求根本没到业务层）
② name_field=nickName 与返回体不一致 → external_name 恒空，且**不报错**（最难发现）
③ scope=openid 缺 profile → 昵称/头像不可得
```

## 3. 设计

### 3.1 配置（三字段，不做代码分支）

```
"user_url":   "https://account.cloud.huawei.com/rest.php?nsp_svc=GOpen.User.getInfo
               &nsp_fmt=JSON&getNickName=1&access_token={access_token}"
"scope":      "openid profile email"
"name_field": "displayName"
其余（auth_url / token_url / id_field=openID / avatar_field=headPictureURL / pkce=false /
      redirect_uri）不动
```

### 3.2 代码加固（providers.py）

```
:650 _mask_token_in_url(url)       日志里把 access_token 值打码，端点仍可定位
:660 _NAME_FALLBACK_FIELDS         昵称兜底候选：displayName → nickName → name → login → userName
:721-727 userinfo 非 2xx           不再只靠 raise_for_status 的异常字符串：
                                   log.warning("Get user info for {p} failed: HTTP {code} url={打码} body={≤300}")
                                   返回值仍是 None（回调照旧落 oauth_error=user_info_failed）
:751-765 昵称取不到时             先按候选字段回落（命中 → WARNING「疑似字段名配错」）；
                                   全不命中 → WARNING 打印响应键列表（不含值），external_name 仍空串
```

为什么不改 `{access_token}` / Bearer 逻辑：它已经是「占位命中就不发 Authorization」，
与华为 GOpen.User.getInfo 的要求一致（发 Bearer 会变成 invalid session），改动无收益。

### 3.3 生效路径与运维口径

```
1) 改文件（两节点同改；备份 0600）—— 文件是渠道定义的导入源
2) 重启四进程 —— 启动期 _sync_providers_from_config 把文件同步进 grape_t_oauth_channels
3) 进程内 provider 缓存 300s：重启后热路径可能短暂读旧值，验收别在重启后 1 秒下结论
4) 反向坑：直接改表可即时生效（表是最后一层），但下次启动会被文件覆盖回去；
   改 meta 同理（且在表就绪时被表覆盖）
```

### 3.4 不变量

```
① token 永不进日志（打码在 _mask_token_in_url）
② 不做启动期配置自动扫描/自动纠正（运维口径写文档，代码不做治理）
③ 不新增平台分支，不改 id_field / avatar_field / pkce / redirect_uri
④ 昵称缺失不阻断登录（返回空串 + 告警），失败只体现在 name 为空而不是抛错
⑤ state 单次消费语义不变（auth_guards.py:114-133 SELECT → DELETE，expires > now）
```

## 4. 场景用例表（S1–S7）

| # | 场景 | 期望 | 用例 |
|---|---|---|---|
| S1 | user_url 带 `{access_token}` | token 进查询参数（urlencode），**不发** Authorization | `test_s1_token_in_url_skips_bearer` |
| S2 | user_url 无占位 | 发 `Authorization: Bearer` | `test_s2_bearer_when_no_placeholder` |
| S3 | name_field 命中 | external_name = 该字段值 | `test_s3_name_field_hit` |
| S4 | name_field 未命中（响应只有 displayName） | 回落 displayName + 一条「疑似配错」告警 | `test_s4_name_field_miss_falls_back` |
| S5 | userinfo 500 | 返回 None；日志 HTTP 500 + 打码端点 + 片段，不含 token | `test_s5_userinfo_500_is_diagnosable` |
| S6 | avatar_field 不存在 | external_avatar 空串，不抛 | `test_s6_missing_avatar_is_empty` |
| S7 | 配置口径 | 两个文件 huawei 三字段正确；build_auth_url 带 scope | `test_s7_channel_cards_and_scope` |

## 5. 边界与遗留

```
· displayName 字段名仍需一次真实 token 的返回体确认（DUMMY 探测只到 "session timeout"，
  看不到字段名）；配错时 S4 的兜底与告警会把它暴露出来，而不是悄悄空名
· state_invalid（重复回调）属独立议题：state 单次消费导致二次回调必失败，
  需查 oauth_states 的 expires 与前端弹窗是否重复触发，不在本单
· 本机 arch11 的 GitHub/华为 OAuth 渠道需重启才会读到新表值；前端无关
```

## 6. 验证（实跑）

```
场景用例 tests/mgmt/test_oauth_userinfo_scenarios.py     7 passed（部署 venv）
红相：同一文件跑在父提交 297481b1c 上                    3 failed / 4 passed
     失败正是本次新增行为：S4 昵称回落、S5 诊断日志、S7 配置口径；
     通过的四条（{access_token}、Bearer 分支、name 命中、头像缺失）是既有能力，不因改动而变
回归（部署 venv）：
  tests/mgmt/test_s127_oauth_401_retry.py                7 passed
  tests/mgmt/test_issue_3563_reauth_401_split.py        19 passed
  tests/mgmt/test_issue_3563_github_token_refresh.py     3 passed
  tests/mgmt/test_github_oauth_scope.py                  1 passed
  tests/mgmt/test_d109_credential_migration.py           7 passed
  tests/mgmt/test_d109_platform_order_data_driven.py     4 passed
  tests/mgmt/test_issue3054_p1_credentials.py           20 passed
```

## 7. 上线步骤（部署侧，需运维执行）

```
1) 两节点同步 configs/oauth-providers.json（备份 0600）
2) 重启四进程 —— 启动期把文件同步进 grape_t_oauth_channels
3) 等过 300s 缓存窗口再验收（或确认四个进程都重启过）
4) 验收：/api/auth/login/huawei 的 scope=openid+profile+email、
        user_url 指向 account.cloud.huawei.com；点一次登录，回调进站而不是
        ?oauth_error=user_info_failed
5) 若昵称仍为空，日志会给出「疑似字段名配错 / 响应键列表」，据此再定 name_field
```
