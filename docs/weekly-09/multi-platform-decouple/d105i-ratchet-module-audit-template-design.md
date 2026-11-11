# D105-14 设计方案：平台硬编码棘轮扩维 + 模块回退审计 + 卡规则模板插值

状态：已实施（2026-11-09；行为锁 12 用例全绿：棘轮 4+审计 3+模板 8，家族回归 89 绿）
前置依赖：基于 3161a423d（D105-12/13 之后）；批次号避让已占用的 D105-12/13
相关批次：D105-10/11（配卡即接入验收+资源翻译）、D105-3（硬编码豁免清单）
业界对照：Hephaestus ADR 0015（厂商成本持平）、Chronicle Declarative captures
（触发/输出解耦）、agent-contracts（$append/$prepend/$replace 操作符表）、
@bonniernews/json-templates（strict 缺失策略）——采纳决策见 §5

## 0. 背景与动机

外部评审对照业界实践提出收紧项，经代码核验采纳三项：

1. **单平台字面量比较分叉无守卫**：现有 test_platform_whitelist_ratchet
   只覆盖"同行恰好两个平台字面量"的二元枚举形态；`provider == "github"`
   单平台能力分叉不在扫描面。存量实锤：repo_manager.py 34 处 gitcode
   硬编码、runtime/service.py 平台名分叉——大部分属 D105-3 豁免在册
   （已知记账），缺的是**防新增的自动拦截**。
2. **模块回退零观测**：module_for() 落 _MODULES 回退时无任何记录，
   "哪些平台依赖代码模块"散落在 PR 讨论里，不可追踪。
3. **transform 回调是纯 JSON 卡的唯一硬墙**：拼接类语义（Closes #N）
   目前必须写 providers 模块；模板插值（纯 str.replace，无表达式求值）
   可安全收窄该半径。

## 1. 改动点 A：棘轮扩维——单平台字面量比较守卫

**文件**：tests/test_platform_whitelist_ratchet.py（扩展，不新建）

```python
_COMPARE_PATTERN = re.compile(
    r'''\bprovider\s*(?:==|!=)\s*["'](gitcode|github|gitee|gitlab)["']'''
    r'''|["'](gitcode|github|gitee|gitlab)["']\s*(?:==|!=)\s*\bprovider''')
```

- 只拦 `provider == "<平台>"` 比较形态。**扫描面诚实声明**：仅覆盖直接
  比较；字典映射/动态分派/三平台以上枚举不在本模式（后者由既有二元
  ratchet 覆盖）
- 不扫裸平台词（URL 拼接/变量名误伤不可接受，且属 D71 冻结存量域）
- BASELINE 豁免清单：现存命中行逐个审阅冻结，格式复用现有
  (路径, 行内容) → 计数机制；棘轮语义不变（基线豁免、新增必红）
- 预期增量成本≈零：卡驱动接入不产生 `provider == "<p>"` 行

**范围外登记**：repo_manager.py 34 处存量 gitcode 治理不属本批
（存量委托域重构，另立批次 D105-13 之后的独立设计）；本批只保证不新增。

## 2. 改动点 B：模块回退审计埋点

**文件**：src/vcs/providers/__init__.py（module_for 单函数内）

形态复用 _audit_source 既有写法（token_resolver.py L35，不发明新模式）：

```python
_fallback_reported: set[str] = set()   # 进程内去重：每 provider 仅首触发记一条

def module_for(provider: str):
    if provider in _MODULES:
        _report_module_fallback(provider, caller)
        return _MODULES[provider]
    ...
```

```python
def _report_module_fallback(provider: str, executor_context: str) -> None:
    if provider in _fallback_reported:
        return
    _fallback_reported.add(provider)
    try:
        from src.persistence.credential_usage_log import log_usage
        log_usage("", "", credential_kind="integration_path",
                  executor_context=executor_context,
                  operation=f"module_fallback_{provider}", repo_key=f"{provider}:")
    except Exception:
        pass  # 审计失败不阻断（同 _audit_source 形态）
```

**设计决策**：
- **每 provider 进程内去重一次**——存量四平台每次 API 调用都命中
  _MODULES，不去重会灌满 usage_log 有界队列（10000 洪峰丢最旧）挤掉
  凭据审计；去重后 usage_log 查 `operation LIKE 'module_fallback_%'`
  即"哪些平台存在代码级依赖"完整清单
- **executor_context 带调用点**（_is_supported_provider / _vcs_url /
  _vcs_req 等，由调用方传入）——"为什么走模块"可追；不做"触发原因"
  （原因只有"平台在 _MODULES 在册"一个事实，附因是伪需求）
- credential_kind="integration_path" 与凭据审计域隔离
- 零行为变更：审计失败静默，返回值与调用方不变

## 3. 改动点 C：卡规则模板插值（第四形态）

**目标**：拼接类语义从"必须写模块"降为纯配置。纯 str.replace，
**无表达式求值**（与 JSONata 的本质区别；安全面等价现有 values 映射）。

### 规则形态（终稿 v3——触发/输出显式解耦）

```json
"rules": {
  "issues.create": {
    "body": {"mode": "append", "trigger": "number",
             "template": "{body}\n\nCloses #{number}"}
  }
}
```

| 维度 | 语义 | 校验 |
|---|---|---|
| trigger | 触发锚=该中性字段非空；缺省=key 字段 | 构建期：trigger ∈ 该 (resource,op) 中性字段集（github 缺省规则∪卡显式字段的键集），错字段红 |
| mode | replace / append / prepend 显式三操作符，缺省 replace | 构建期枚举校验 |
| template | 输出模板，{占位符} 按中性字段名替换（append 引原值即 `{body}` 形态，不设专用锚值语法） | 构建期：占位符全部 ∈ 上述中性字段集（拼写错如 {numbr} 构建期红，兑现"正确性前移"）；且含 ≥1 占位符（防误用为纯字面量） |
| 占位符缺失 | **strict：运行时缺数据即抛错留痕**，不静默保留字面量 | 见下 |
| 互斥 | 同一 field 内 template 与 values/drop 互斥；**跨字段** template+values 自由组合 | 同 field 级校验 |

**strict 默认的理由**（业界默认偏 warn+keep，本仓反选）：
字面量 `Closes #{number}` 写进远端 issue 是"看似成功实则无效"的静默
错误输出，下游是人读的 issue 区，服务端告警无人看——keep 部分就是
静默错误面，与本仓"把静默行为全部显性化"的 D105 家族驱动力冲突。
构建期已校验占位符合法，运行时缺数据=数据形态异常，抛错留痕优于
静默写入。**不设 strict_template 开关**：两个默认值=三态未知行为，
守卫与排障劣化；grape 卡是内部部署资产非通用库，统一默认是更好接口。
真有"缺失可容忍"场景等真实案例出现再评估（届时才知道容忍语义是什么）。

**行为边界（零回归）**：存量四平台卡无 resources 段全走模块路径；
test_s3 的 test_dual_track_transform_fields_stay_module_only 反向锁不受
影响——transform 回调仍是模块专属，模板只让**不需要回调的场景**
（业界案例中约 80% 的"看似要写代码"）不写模块；剩余复杂语义归模块
escape hatch，该边界健康。

**三处实现**：
1. src/vcs/platform_card.py _build_resources：第四形态校验（报错三要素：
   卡 ID+字段路径+期望格式，沿用 D105-11 口径）
2. src/vcs/providers/base.py apply_rules：识别 template 形态——先判
   trigger 锚非空（缺省 key 字段），按 mode 拼 template（replace 覆盖 /
   append 后缀 / prepend 前缀），占位符取值自整个 neutral dict，缺失抛错
3. translate_inbound 卡路径：零改动（字段级合并天然流经 apply_rules）

## 4. 验证方案

1. 棘轮 RED 复验：临时注入 `if provider == "gitee":` → 红 → 撤销 → 绿
   （XPRESS 手法，注入不进 commit）
2. 模块审计行为锁：(a) 卡在册平台走 module_for 落
   module_fallback_<p> 一条 (b) 二次调用不重复 (c) 纯配卡平台不落
3. 模板 8 用例：append 输出 `Closes #12` 正确 / trigger 错字段构建红 /
   mode 缺省 replace 等价 / 占位符拼写构建红 / 跨字段 template+values
   组合 / 运行时缺占位符抛错 / prepend 顺序 / 同 field 互斥红
4. 家族回归：test_platform_whitelist_ratchet +
   test_platform_onboarding_acceptance + test_s3_card_driven_resources +
   test_platform_card_matrix + test_d102j_global_sweep +
   test_s126_credential_usage_log（新 credential_kind 不破坏既有记录）全绿

## 5. 明确不做（决策留档）

| 项 | 处置 | 理由 |
|---|---|---|
| 显式 SPI 核心层 | 不做 | 单域多平台非多域集成产品，SCM 消费面已被 test_s3 10 用例锁定；若未来出现第二个大集成域再立项=新设计评审非补账 |
| JSONata 表达式映射 | 不做 | 用户可控输入进求值上下文=安全审计面；静态 schema 校验失效（test_s3 全部 guard 用例重写）；模板插值覆盖 80% 场景，剩余归模块 |
| strict_template 开关 | 不做 | 见 §3 strict 理由 |
| repo_manager 34 处存量治理 | 独立批次 | 存量委托域重构与本防线不同域；棘轮扩维后治理完成度可用 BASELINE 清零度量 |

## 6. 改动量

| 文件 | 变更 |
|---|---|
| tests/test_platform_whitelist_ratchet.py | +1 模式 +BASELINE 若干行 |
| src/vcs/providers/__init__.py | +去重 set +~12 行函数 +调用行 |
| src/vcs/platform_card.py | _build_resources 第四形态校验 ~40 行 |
| src/vcs/providers/base.py | apply_rules template 分支 ~30 行 |
| tests/test_module_fallback_audit.py | 新建 3 用例 |
| tests/test_s3_card_driven_resources.py | +模板用例 8 个 |
| docs + 台账 | 本文件 + 06-ledger D105-14 行 + UC73 入账 |

合计 ~250 行，零 DDL、零前端、零行为变更（新增路径均校验先行）。

## 7. 实施顺序与守卫随行

A→B→C；每步新守卫先临时 RED 复验再放行；bump 不涉及（无前端缓存版本）；
提交前实跑全部家族守卫（silent_except ratchet 若被本批触碰需同步基线——
预检：B 的 try/except pass 是**静默 except 形态**，将被 D105-13 新 ratchet
扫描命中，需在 EXEMPT_FILES 登记豁免理由"审计失败不阻断，同 _audit_source
存量形态豁免"——此点实施时验证）。

## 8. 评审差距收口（D105-16 续，2026-11-09 外部五差距分析采纳 3.5/5）

采纳：差距一（capabilities 显式化，含差距二剩余的覆盖一致性校验）、
差距三（pat_prefix，挂账至下次接真实平台作为卡字段验证项）、
差距五（通用链职责边界文档化，本节）。
不采纳照单：差距二"占位符来源校验缺失"（实查已在 D105-14 落地：
platform_card.py 占位符∈中性字段集∪_COMMON_TRIGGER_FIELDS 构建期校验）；
差距四（回退熔断）**前提错位**——存量四平台走 _MODULES 是设计主路径而非
逃逸，按回退次数熔断会误熔核心平台；熔断对象应为平台 API 调用失败率
（挂起，与告警体系同批设计）。

### 8.1 capabilities 显式声明（差距一/二）

- 卡可选 `capabilities` 数组（`"<resource>.<op>"` ∪ 基础设施能力
  `webhook.verify`/`repo.host` 等，枚举 `_VALID_CAPABILITIES`，未知值构建期红）
- **覆盖一致性**（差距二剩余）：声明的能力必须有承载——卡 resources.rules
  对应键，或 provider 在 `_MODULES`（代码模块承接）；皆无 = 构建期红
  （"零代码成立"从真机验证前移为构建期可判定条件）
- 查询：`card_capabilities(provider)`（None=未声明不限制；tuple=白名单）
- 消费侧 fail-closed：`require_capability(provider, cap)` 对声明白名单外
  能力显式拒绝（含声明集三要素）；未声明卡零影响（存量四平台不受限）
- 已有行为锁：tests/test_capabilities_declaration.py 8 用例

### 8.2 通用链职责边界（差距五：不做什么清单）

`base.apply_rules` / `translate_inbound` / `providers/__init__.module_for`
（下称"通用链"）**永不承载**以下职责，能力位扩展与插件模块必须在边界外实现：

| 禁止事项 | 归属 |
|---|---|
| 凭据验证 / token 获取与刷新 | token_resolver / deps（供给门面）|
| 重试与退避决策 | 调用方策略层（oauth_401_retry 链）|
| 业务权限判断（谁能操作哪个仓） | routes 层 require_* 依赖族 |
| 存储层操作（managed_repos/user_repos 写） | repos 路由与仓储层 |
| org/成员可见性过滤 | org 域（resolve_org_context 族）|
| 审计之外的副作用（通知/触发流水线） | event_pipeline |

约束的执行方式：结构性守卫（test_s3 双轨等价 + transform-只归模块反向锁）
+ ratchet 家族（比较形态/静默 except）已把"平台特有分支渗透通用链"的
主要形态纳入扫描；本清单作为评审时的人工核对基准，与守卫互补。
