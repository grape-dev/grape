# D105e：平台接入验收机制（「配卡即接入」最小验收用例集）

> 扫雷⑦机制建议落地。 companion 测试：tests/test_platform_onboarding_acceptance.py（UC65）。
> 来源：multi-platform-decouple 批 B3 收口——把"新增一个仓库托管平台"的最小接入面
> 固化为可重复的进程内断言，防止接入链路回归（某一面悄悄退回硬编码/兜底）。

## 一、机制定义

「配卡即接入」= 写一张 `configs/platforms/<name>.json`（或 admin 运行时 meta 覆盖）
即完成平台接入，覆盖以下四面；**资源 API 面除外**（见 §三门槛）。

| # | 面 | 断言载体 | 机制点 |
|---|----|----------|--------|
| ① | 卡注册面 | build_platform_card 成功 + list_vcs_platforms 含 foo | Builder 即校验（S18），注册=目录扫描+缓存失效 |
| ② | webhook 面 | _generic_backend_for 非 None + list_webhook_providers 含 foo | 方言策略（GenericBackend）+ 动态路由，零代码 |
| ③ | 资源 API 面 | translate_inbound('foo',...) **现状必抛 ValueError** | 显性化门槛：需写 providers/<name>.py 并注册 _MODULES，纯配卡不覆盖 |
| ④ | 工厂面 | create_provider('foo').provider_name=='foo'，不落 GitCodeProvider 兜底 | D22 工厂卡驱动分派 |
| ⑤ | 建仓面 | create_repo 返回非 error dict（_http 进程内 mock） | 卡决定 api_base/api_auth/form 形态 |
| ⑥ | 凭据面 | credential_registry lookup('foo_pat') 非 None | 动态 PAT 规则：<p>_pat 且 <p> 卡在册 |
| ⑦ | bot 身份面 | resolve_bot_username('foo')=='foo_bot'（卡显式值） | 卡 bot_username，空=无 bot 协作能力降级 |

另加反向锁 ⑧：无卡平台不接入（get_platform_card / _generic_backend_for 均 None）——
「配卡即接入」的另一面是「无卡即不接入」，防"处处兜底成 gitcode"的假接入。

## 二、测试手法（可复用的注册手法）

平台卡注册是 `_load_cards()` 惰性扫描 `_CONFIGS_DIR`（模块级常量）+ meta 覆盖链，
**不是 import 时硬绑定**。测试用手法的最小形态：

```python
(tmp_path / "foo.json").write_text(json.dumps(FOO_CARD_RAW))
monkeypatch.setattr(platform_card, "_CONFIGS_DIR", str(tmp_path))
invalidate_platform_card_cache()   # 用例结束再 invalidate 一次，恢复内置默认
```

全程进程内、零外网；⑤ 的 HTTP 层以 monkeypatch GenericVcsProvider._http 替身，
同时断言 method/path/form 编码形态由卡派生（query_token → form）。

## 三、门槛说明（③ 为什么是预期失败）

`translate_inbound` 走 `providers/<name>.py` 模块表（git-repo-provider-neutral-
interface-design.md §4.2）：issue/PR 写路径的方言翻译按"每 provider 一模块"组织，
配卡只自动覆盖 webhook/工厂/凭据/bot 四面。新平台若需要资源写 API，必须写
providers/<name>.py 并注册 `_MODULES`——该门槛被断言 ③ **锁为预期失败**：
- 现状：`pytest.raises(ValueError, match="foo")` 全绿（门槛显性化，禁止假绿）；
- 未来：若实现卡驱动的资源翻译或补 providers/foo.py，把该断言改为正向等价断言即改绿。

## 四、验收与边界

- 验收：tests/test_platform_onboarding_acceptance.py 全绿（8 用例，无 skip）；
  相邻平台族回归（test_platform_card / test_git_provider / test_s107 / test_s88 /
  test_vcs_dialect_backend / test_platform_whitelist_ratchet）不受缓存失效污染。
- 边界：本机制只锁"配卡能接入什么"，不验证真实平台连通性（那属 D104 实弹矩阵族）；
  OAuth 登录渠道卡正交（D10），不在本用例集范围。
