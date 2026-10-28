# Spec: 一键部署闭环 — server_base_url Host 头自愈

> 背景：AI 代理一键部署 Grape 到平台托管环境（华为云 workspace / 容器）时，
> 常因启动提速关闭公网探测（`GRAPE_AUTO_DETECT_URL=false`），且自动探测本身
> 在此类环境拿不到平台签发域名（只能拿 NAT 出口 IP）。结果 meta 表空值，
> `get_server_base_url()` 各读取点兜底 `http://localhost:3000`，OAuth 回调 /
> A2A / 前端设备接入静默失败——服务启动全绿，用户到 GitCode 触发事件才暴露。

## Objective

补全 Issue #372 §14.2.2 已有的 Host 头自愈机制：webhook_url 之外，
server_base_url 也在用户首次访问时从请求 Host 头自动写对，实现
"一键部署 → 用户打开页面即自愈"，不依赖部署方配置任何地址。

## 机制

`src/api/main.py` 认证中间件（`request_id_and_log_middleware`）：

1. 请求带有效 JWT（`_uid` 非空）才触发 —— 防伪造 Host 头污染（配合 TRUSTED_HOSTS 白名单）。
2. 复用 `src/user/db.py:detect_webhook_from_host(host, x_forwarded_host, x_forwarded_proto)`
   推导地址。该函数已排除 localhost / 127.x / 0.0.0.0 / ::1，未配白名单时不读
   X-Forwarded-Host —— 安全约束全部继承，零新增信任面。
3. 写库条件（幂等，有效值不覆盖）：
   - webhook_url：仅当 meta 为空（既有行为，不变）
   - server_base_url：当 meta 为空 **或** 为本地无效值（http://localhost* / 127. / 0.0.0.0 / [::1]）—— 本地地址作为对外基址必然错误，覆盖零破坏

## 场景与用例清单

| # | 场景 | 前置状态 | 动作 | 预期 | 用例 |
|---|------|---------|------|------|------|
| S1 | 全新一键部署首访 | meta 空（探测被 AI 关闭） | 用户浏览器经平台域名登录，带 JWT 访问任意 API | webhook_url 与 server_base_url 一次性写为平台域名地址 | test_heals_when_server_base_url_empty（单测）+ test_s1_integration |
| S2 | localhost 残留自愈 | server_base_url=http://localhost:3000（旧错误值） | 同上 | server_base_url 被覆盖为平台域名；webhook_url 已有值不动 | test_heals_when_server_base_url_is_localhost + test_s2_integration |
| S3 | 有效值幂等 | server_base_url=已配置公网地址（用户显式配置 / server-config.json 同步） | 同上 | 不写库、不覆盖 | test_no_overwrite_when_server_base_url_valid + test_s3_integration |
| S4 | 本地开发直连 | 用户直接 http://127.0.0.1:3000 访问 | 登录后访问 | detect_webhook_from_host 返回空串，两个值都不写（本地开发不被自动发现污染） | test_local_host_not_written + test_s4_integration |
| S5 | 匿名请求不触发 | 任意状态 | 无 Authorization 头访问 | 中间件 `_uid` 守卫拦下，不触发任何推导 | test_s5_integration |
| S6 | 回归：webhook_url 分支 | meta 空 | 同 S1 | webhook_url 先写、格式含 /webhook/gitcode 后缀，行为与改动前一致 | 既有 tests/test_issue_372_webhook_url_detect.py 全量回归 |
| S7/S7b | 结构守卫 | — | 重构中间件 | S7：set/get_meta_value("server_base_url") 调用被源码断言守住；S7b：webhook_url 既有自愈分支保留（回归保护） | test_s7_structure_server_base_url_heal_present / test_s7_structure_webhook_heal_unchanged |

## 场景用例代码（集成用例，随 CI pytest 运行）

文件：`tests/test_oneclick_host_heal.py::TestScenarioIntegration`

用例代码形态：构建最小 FastAPI 应用，把真实中间件函数
`src.api.main.request_id_and_log_middleware` 注册上去，patch
`src.user.db.get_meta_value` / `set_meta_value`（内存 dict）与
`src.api.jwt_utils.verify_access_token`（固定返回 sub），用 TestClient
带 `Host: workspace.example.com`
+ `Authorization: Bearer x` 发请求，断言 mock 库里最终写入的值。不连真实
数据库、不依赖部署形态。

## Out of Scope

- 探测本身提速/并行化（治 AI 关探测的动机，独立改进）
- doctor.py 三态化与 /api/health 显式 server_base_url_valid 信号（部署验收闸门，独立改进）
- TRUSTED_HOSTS 白名单语义调整

## 扩展：一键部署引擎接入引导（bwrap/平台沙箱扫不到引擎）

背景：AI 一键部署 Grape 到平台沙箱（PATH 隔离）后，discover_backends 扫不到
任何引擎（opencode/codearts/hermes/codex/dsh/hwcloud/cursor 全 NOT FOUND），
引擎列表只剩默认 opencode 离线记录（ensure_default_runtime）。引擎不应装进
Grape 沙箱（量大、与平台"引擎独立环境"设计冲突）——正确路径是 Grape 既有的
远程 runtime 架构：装了引擎的机器跑 worker，凭 rta_ 凭据向主控注册。

已存在的底座（本特性零新机制，全部复用）：
- 凭据签发：POST /api/settings/runtime-credentials（settings.py，一次性明文）
- 安装包公开下发：GET /api/runtime/grape-worker.tar.gz（worker_package.py）
- 一键安装脚本：GET /api/runtime/install（Linux）/ install.ps1（Windows）
- 注册/心跳/认领协议：/api/runtime/register、/heartbeat、/runtimes/{id}/tasks/claim
- 任务分流：credential_id>0 → queue_remote，worker 认领自报

新增编排入口：GET /api/runtime/engine-onboarding（require_org_member）

| # | 场景 | 前置状态 | 预期 | 用例 |
|---|------|---------|------|------|
| E1 | 一键部署后首查（沙箱无引擎、无 worker） | discover 全空 + agent_runtimes 无 online | needs_worker=true，install_cmd 的 --server 为 Host 自愈后的公网地址 | test_e1 |
| E2 | 有 worker 在线 | agent_runtimes 有 online 行 | needs_worker=false，remote_engines 列出该引擎 | test_e2 |
| E2c | 仓库上下文（repo 参数，契约：规范带前缀形态） | 该 repo 的 repo-scope worker 计入可见性（对齐 dashboard 仓库级看板分层；engine_select._member_scope repo 档位合法）；同名仓库跨平台 worker 不泄漏；SQL 等值匹配（非多形态 IN） | test_e2c |
| E2d | 全局上下文（引导页/横幅） | repo-scope worker 不计入，仅 global + 本人 user-scope | test_e2c 内 r_global 断言 |
| E2e | 裸 owner/repo 形态传入 | 不做前缀猜测（查表猜前缀在双平台同名时命中不确定）→ 视为无仓库上下文走全局保守判定 | test_e2e_bare_repo_form_is_global |
| E2b | 他人 user-scope worker 在线 | 不计入本用户可见性（SQL 按 scope='global' OR user+本人 过滤，对齐 dashboard 惯例）→ 本用户 needs_worker 仍 true | test_e2b_scope_visibility |
| E3 | 本机装了引擎（裸机直装形态） | discover_backends 有 installed | needs_worker=false，local_engines 照实上报 | test_e3 |
| E4 | 鉴权 | 无有效登录态 | 401/403 | test_e4 |

### 引导页前端场景（时序 gating 修订：未绑定不提引擎，`_obEngineGuide` 已移除）

用户第一次进入页面（无平台绑定 → `_renderBindingGuide` 引导页）时，
异步拉取 engine-onboarding 渲染引擎接入提示：

| # | 场景 | 预期 | 用例 |
|---|------|------|------|
| O1 | 一键部署后首进引导页（needs_worker=true） | hero 下追加"接入 Agent 引擎"卡片：安装命令（_esc 转义）+ 复制按钮 + rta_ 凭据/token 配置提示 | test_engine_onboarding_guide.js O1 |
| O2 | 引擎可用（needs_worker=false）/ 视图已切走 | 不追加卡片（stale guard：window._currentView !== 'repos'；时序 gating 修订后卡片机制整体移除（原场景 O2-b 同随废弃）） | O2 |
| O3 | API 失败 | 静默，不影响平台绑定主流程 | O3 |
| O4 | 点击复制 | 命令写入剪贴板，按钮短暂显示"已复制" | O4 |
| O5 | 文案国际化 | locales zh/en 键对齐、app.js 实际引用、token hint 转义 | O5 |

### 时序引导链（最终版：token → 仓库 → 引擎）

用户链路每步只讲一件事，做完了才出现下一步；状态驱动（查实际状态不埋点）：

```
1. 未绑定平台        → 引导页：绑 GitCode/GitHub（OAuth/手动令牌双模式，
                       令牌直达链接；不提引擎）
2. 绑定成功          → 进仓库列表（不提引擎——先托管仓库）
3. 进入仓库之后      → 无可用引擎 → 顶部横幅"添加设备绑定引擎"（→ 引擎页）
4. 引擎页空态        → "添加设备"引导（openAddDevicePanel，凭据/安装命令
                       在侧边栏面板里，页面不堆命令文本）
终态：绑定+仓库+引擎 → 无任何引导
```

### 引擎不可用横幅（app.js `_checkEngineBanner`，B1-B10）

触发条件（最终版）：已绑定 + **已进入仓库上下文**（currentRepo 非空，
dashboard/仓库列表渲染时检查）+ needs_worker。绑定但未进仓库不弹（先托管
仓库）；引擎页内不弹（页内空态引导负责）；worker 上线自动消解；跳过按
用户名 localStorage 记忆。按钮直接跳引擎页（不弹命令弹窗）：

| # | 场景 | 预期 | 用例 |
|---|------|------|------|
| B1 | 已进入仓库（绑定+选中仓库）+ needs_worker | 顶部横幅"当前仓库还没有可用的执行引擎——添加设备绑定引擎后才能跑任务"+"添加设备绑定引擎"+"跳过" | B1 |
| B2 | 引擎可用（worker 已上线） | 横幅不出现（每次进工作台重查，自动消解） | B2 |
| B3 | 用户点击跳过 | localStorage 记忆（engine_banner_skipped_<user>），此后不再出现 | B3 |
| B4 | 仓库 dash 视图 | 横幅照常出现（renderDashboard 挂载点；引擎页除外） | B4 |
| B5 | API 失败 | 静默，不影响工作台 | B5 |
| B6 | 结构守卫 | 挂载点（仓库列表+dashboard）/currentRepo 门禁/onclick 导出/i18n 键存在 | B6 |
| B8 | 未绑定用户 | 无横幅（引导页专心配 token） | B8 |
| B9 | 绑定但未进入仓库 | 无横幅（先托管仓库——最终链路 token→仓库→引擎） | B9 |
| B10 | 已在引擎页 | 无横幅（页内空态引导负责，不叠加） | B10 |
| B7 | 已绑定但零托管仓库（空态） | 挂载点在 renderReposFromData 入口（managedRepos===0 分支 return 之前），空态页同样出现横幅 | B7 |


### 首次进入路由（app.js `_restoreNavAfterLogin`，G1-G5）

实测复现：未绑定平台的新用户带 hash 进入（注册页跳转/OAuth 回调携带 hash）时，
原逻辑 hash 优先级最高直接 nav()，绕过绑定引导页——用户首次进入看不到
GitCode/GitHub 绑定提示，只剩密码补全提示条。修正：未绑定且无当前仓库 →
一律引导页（hash 丢弃）；已绑定/有仓库用户 hash 恢复语义不变。

| # | 场景 | 预期 | 用例 |
|---|------|------|------|
| G1 | 未绑定 + 任意 hash 进入 | 绑定引导页（GitCode/GitHub 卡片 + 引擎接入卡片） | guideTests G1/G1b |
| G2 | 已绑定 + hash（如 #settings） | 正常恢复路由（无回归） | G2 |
| G3 | 未绑定但有当前仓库 + hash | hash 恢复（原语义保留） | G3 |
| G4 | 无 hash 未绑定 | 引导页（原有行为不变） | G4 |
| G5 | 结构守卫 | 修复分支（bindings===0 && !currentRepo → 引导页）存在 | G5 |

场景用例代码：与 hash 分支同规格的纯函数提取 + app.js 源码结构守卫，
`node tests/js/test_engine_onboarding_guide.js` guideTests 段。

### 引擎页接入指引（runtimes.js，Q1-Q9 —— 简化定稿，替代原 R 系列）

> 2026-10-28 简化重构（用户定稿）：原分阶段聚光引导（全屏遮罩/箭头气泡/状态机/
> 按用户名持久跳过键/五条标记写入路径，约 400 行）整体删除。理由：需求最小满足是
> "进仓库无引擎 → 指引去配置"；聚光机制是需求外的表演层，且其状态复杂度产生了
> 三次实测状态错乱（跳过键与服务端状态脱节、判定误报、无痕失效），复杂度藏进了
> 维护税而非用户价值。同批回归简约：横幅单句文案（两级认知文案取消）、凭据面板
> intro 说明取消（用户原话"一点不简约，你乱加"）。

简化后形态（三层非打断提示，全部退场靠真实状态）：

```
1. 仓库页横幅（唯一主动提示）：已绑定+已进仓库+needs_worker → 单句提示+按钮
2. 按钮脉冲：引擎页 needs_worker=true → "添加设备"按钮呼吸光圈（不遮罩不挡内容）
3. 直达：横幅按钮跳转 → 引擎页渲染完 → 添加面板自动展开（省一次寻找点击）
```

| # | 场景 | 预期 | 用例 |
|---|------|------|------|
| Q1 | 聚光机制已删 | 无 _rtSpotlight/rtGuideMask/rtGuideTip/rt_guide_done 键/箭头 keyframes；脉冲保留且仅 box-shadow；reduced-motion 停用 | Q1-Q1f |
| Q2 | 引擎页按钮脉冲 | needs_worker 门控（非列表为空）；会话级去重（_rtPulseDone，点开面板不再加回）；仅 engines 标签；无持久记忆 | Q2-Q2d |
| Q3 | 横幅跳转直达 | _gotoEnginePage 设 _rtOpenPanelOnEnter → 渲染完成消费标记自动开面板；跳转前先撤横幅 | Q3-Q3c |
| Q4 | 侧栏/任意路径进引擎页 | nav('runtimes') 统一退场在场横幅（实测复现：侧栏进入横幅残留已修；与横幅按钮跳转行为一致；语义差异：跳转=用户响应记录跳过键，自入=不记录） | Q4 |
| Q5 | 密码条排队可见 | 安全横幅抑制引擎引导时，密码条尾部挂"稍后还有 1 条引擎提示"chip（纯文本无交互）；密码条消失 chip 自动带走；100ms 后补位检查接上引擎横幅 | Q5-Q5c |
| Q6 | i18n 对齐 | 横幅单句（≤50 字）；排队 chip zh/en；两级次句键/面板 intro 键/聚光 spot_* 死键均已删 | Q6-Q6g |
| Q7 | 旧测试清理 | test_engine_onboarding_guide.js / test_guide_visual.js / test_engine_cognitive_layer.js 删除（对象不复存在） | Q7-Q7c |
| Q8 | 安装命令 base_url | 面板命令经 health.server_base_url + grape_server_base 缓存解析（上轮修复守卫仍在，family 防复发） | Q8 |
| Q9 | 符号残留 | app.js/runtimes.js 无已删引导符号（_rtGuideStage/_maybeShowWorkerGuide/_rtGuideMarkDone） | Q9-Q9b |

场景用例代码（tests/js/test_engine_guide_simplified.js，源码结构守卫 + i18n
对齐 + 文件存在性，`node tests/js/test_engine_guide_simplified.js` 运行，
30 项断言）：

```js
// Q1: 聚光机制已删（防复活守卫）
ok(!rtSrc.includes('function _rtSpotlight'), 'Q1: _rtSpotlight 函数已删');
ok(!rtSrc.includes('rtGuideMask') && !rtSrc.includes('rtGuideTip'), 'Q1b: 遮罩/气泡元素已删');
ok(!rtSrc.includes('rt_guide_done_'), 'Q1c: 聚光持久跳过键已删（无跨设备状态残留）');
// Q2: 简化版指引（needs_worker 门控的按钮脉冲，会话级）
const fn = rtSrc.slice(rtSrc.indexOf('window._rtMaybePulseAddBtn'),
                       rtSrc.indexOf('function _addDeviceBtnHtml'));
ok(fn.includes("d.needs_worker") && fn.includes("rt-guide-pulse"), 'Q2: needs_worker 触发按钮脉冲');
ok(fn.includes("_rtPulseDone"), 'Q2b: 会话级去重（点开面板后不再加回）');
ok(!fn.includes('localStorage'), 'Q2d: 无持久记忆（会话变量，刷新即重置）');
// Q4: 侧栏进引擎页横幅退场
const navFn = appSrc.slice(appSrc.indexOf('function nav(view, param)'),
                           appSrc.indexOf('function nav(view, param)') + 800);
ok(navFn.includes("if (view === 'runtimes') _dismissEngineBanner()"), 'Q4: nav 统一退场');
// Q5: 排队 chip
ok(fn5.includes('engineBannerQueued'), 'Q5: 抑制分支挂排队 chip');
```

### 判定 SQL（engine-onboarding，X1-X8 —— 真实 SQLite 回归，2026-10-28 修复）

> 生产实测发现的列名 bug：判定 SQL 误用 runtime_credentials 的列名
> （device_name/owner_name）查 agent_runtimes —— SQLite 抛 no such column 被
> except 吞掉，remote_engines 恒空，有在线 worker 的部署被误报
> needs_worker=true（用户有引擎却被告知"没有"）。此前 mock 单测自建带同样
> 错误列名的假表——测试与实现共享同一错误假设，绿灯无意义。修复：真实列
> （host_name/display_name/owner_id）+ JOIN runtime_credentials（repo-scope
> 归属按凭据 owner_name，LOWER 双边匹配）+ org 隔离（对齐 dashboard/settings
> 引擎列表惯例）。配套回归测试改在 conftest 真实测试库（真实 db_setup DDL）
> 上插行验证——列名再漂移立即红。

| # | 场景 | 预期 | 用例 |
|---|------|------|------|
| X1 | 无任何在线引擎 | needs_worker=true（正向：提示该出现） | test_no_engines_needs_worker_true |
| X2 | 有 online global 引擎 | needs_worker=false + remote 列出（**反向用例：本次生产 bug 完全体**） | test_online_global_engine_no_misreport |
| X3 | 本人 user-scope（owner_id=uid 字符串） | 计入，免提示 | test_own_user_scope_engine_counts |
| X4 | 本人 repo-scope（owner_id=仓库全名，凭据 owner_name） | JOIN 命中免提示；他人 repo worker 不计入 | test_own_repo_worker_via_credentials_join |
| X5 | 他人 user-scope | 不计入本人 | test_others_private_engine_not_shared |
| X6 | org 隔离 | 他组织 global 引擎对本组织不可见（不计数+不泄露列表） | test_org_isolation |
| X7 | 离线引擎 | status!=online 不计入 | test_offline_engine_does_not_count |
| X8 | 用户名大小写 | 凭据 owner_name='Alice' vs 查询 'alice' → LOWER 匹配命中 | test_username_join_case_insensitive |

场景用例代码（tests/test_engine_onboarding_sql.py，真实 SQLite 上的行为
断言）：

```python
def test_online_global_engine_no_misreport(self, real_db):
    """反向用例（本次生产 bug 的完全体）：有 online global 引擎
    → needs_worker 必须为 false——列名错误时此查询抛异常被吞、误报 true。"""
    _seed(real_db, [("opencode", "global", "", 0, "", 0, "online")])
    r = _call()
    assert r["needs_worker"] is False, "有在线引擎仍误报 needs_worker（列名/JOIN 错误）"
    assert len(r["remote_engines"]) == 1
    assert r["remote_engines"][0]["device_name"] == "opencode-host"

def test_username_join_case_insensitive(self, real_db):
    """凭据 owner_name='Alice'，查询用户名 'alice' → LOWER 匹配仍命中。"""
    ...
    r = runtime_engine_onboarding(repo="gitcode:owner/r", user={"username": "alice", "id": 55, "org_id": 0})
    assert r["needs_worker"] is False, "凭据 owner_name 大小写差异不应漏命中（误报）"
```

### 令牌创建直达链接（account.js `_accountManualBindForm`，T1-T5）

手动绑定令牌的帮助文案（"1. 前往 gitcode.com → 设置…"）要求用户自己去平台
找设置入口，不直观。改为超链接一键直达令牌创建页（GitHub/GitCode 都支持）：

| # | 场景 | 预期 | 用例 |
|---|------|------|------|
| T1 | GitHub 手动绑定表单 | 顶部"前往令牌创建页面"按钮 → `github.com/settings/tokens/new?scopes=repo&description=Grape%20DevOps`（官方支持 scopes 预勾选 repo 权限 + description 预填） | test_token_create_deeplink.js T1 |
| T2 | GitCode 手动绑定表单 | 同上 → `gitcode.com/setting/token-classic/create`（官方帮助文档路径：个人设置→访问令牌(经典)→新建；首版 settings/personal-access-tokens/new 是 SPA 兜底 200 的误判，已改） | T2 |
| T3 | 帮助文案第一步链接化 | 仅第一步"前往 xxx"变为链接；`target=_blank` + `rel="noopener noreferrer"`；其余步骤纯文本 | T3 |
| T4 | 链接安全性 | 直达 URL 为静态常量（无用户可控注入面），域名标签 _esc 转义 | T4 |
| T5 | i18n | `app.account.open_token_page` zh/en 对齐；4 组帮助文案第一步格式可被链接化正则命中 | T5 |

场景用例代码：与 `_accountManualBindForm` 同规格提取构建函数（读真实
locales json），对生成的 HTML 串做断言；并含 account.js 源码结构守卫
（直达 URL 常量与 i18n 调用存在，防重构丢失）。运行：
`node tests/js/test_token_create_deeplink.js`。



场景用例代码（tests/test_engine_onboarding.py，两层形态）：
- mock 层（E1/E2/E2b/E3）：patch get_server_base_url / discover_backends /
  db cursor（description 驱动列名取值），直接调用路由函数断言响应字段；
  E4 鉴权以路由签名依赖检查实现（require_org_member 挂参）+ 源码结构守卫
- 执行层（E2c/E2d）：真实 SQLite 临时库建 agent_runtimes/managed_repos 表
  验证 SQL 等值匹配语义（mock 无法验证 SQL 本身），含跨平台同名仓库
  worker 不泄漏断言；E2e 为 repo 参数契约纯函数用例


## Acceptance Criteria

- S1-S7 用例全部通过；既有 Issue #372 webhook 检测用例全量回归通过
- 改动不引入新配置项、不改启动行为、不改任何读取点兜底逻辑
- 用户视角闭环：一键部署（探测关/不关均可）→ 登录后第一个请求 →
  webhook_url + server_base_url 全部写对 → GitCode 回调 / OAuth / A2A / 前端接入可用

## 注册密码强度四条件（register.html + password.py 同源黑名单，P1-P5）

实测复现：test1234 前端三条件（≥8位/字母+数字/不等于用户名邮箱）全绿，提交被
"Password is too weak" 拦截。根因：后端 check_password_strength 有第四条规则——
99 词弱密码黑名单（大小写不敏感）——前端既不显示也不预检，用户被看不见的规则
拦截且文案不说原因。

修复：前端强度面板增第四条"非常见弱密码"；RULES.regPassword 提交前预检；
黑名单由 scripts/gen-pwd-blocklist.py 从后端 _WEAK_PASSWORDS 同源生成
（src/api/static/js/pwd-blocklist.js）；i18n zh/en 补全新文案（原英文 too weak
同时修正为可读的"过于常见"表述）。

| # | 场景 | 预期 | 用例 |
|---|------|------|------|
| P1 | 黑名单密码（test1234/Abc12345，任意大小写） | 前三条件 MET、第四条 NOT-MET（红） | test_password_blocklist.js P1 |
| P2 | 正常密码（Xk9mzqR7 等） | 四条件全 MET，放行 | P2 |
| P3 | 提交时前端预检 | RULES.regPassword 在 blur/submit 即拦黑名单（不等后端 400），错误文案为明确中文/英文 | P3（结构守卫：预检代码/req 挂钩/脚本引入/UI 行） |
| P4 | 前后端同源 | blocklist 文件可解析、含 test1234、规模合理；生成脚本存在 | P4 |
| P5 | i18n | pwd_req_blocklist / err_password_blocklist / error.PASSWORD_WEAK zh-en 对齐 | P5 |

场景用例代码：与 register.html reqs 同规格的四条件纯函数 + 前端 blocklist 文件
解析 + register.html/password.py 源码结构守卫。运行：
`node tests/js/test_password_blocklist.js`。端到端浏览器验证已在提交
06731200f 记录（Test1234 → 第四条 NOT-MET + 明确拒绝文案；Xk9mzqR7 → 放行）。

### 复核 #3 修复：全密码入口接入 + 统一谓词（P6-P8，2026-10-28 审查 follow-up）

审查发现弱密码黑名单仅接入注册页，改密（/auth/change-password）/设密
（/auth/set-password）/忘记密码重置（/auth/forgot-password/reset）三个入口
后端均有 check_password_strength（含黑名单），前端却不预检——"前端全绿、
后端 400"的不一致在这三个入口同样存在。同时收敛判断逻辑：生成器产出
window.isWeakPassword(p) 唯一谓词（trim + 大小写归一），四个消费点统一调用，
消除各处 PWD_BLOCKLIST.has 散落拷贝（数据单源 + 判断单点）。

| # | 场景 | 预期 | 用例 |
|---|------|------|------|
| P6 | 谓词导出与语义 | 生成物含 isWeakPassword；TEST1234/带空白 admin123 命中，正常密码与空串不命中 | test_password_blocklist.js P6-P6c（eval 沙箱语义实测） |
| P7 | 全消费面接入 | 改密 doChangePassword / 设密 doSetPassword / 忘记密码 NEWPWD_RULE / 注册 RULES 均走谓词；index.html 与 forgot.html 引入 blocklist 脚本（v=2） | P7-P7e |
| P8 | 无散落拷贝 | 四消费点源码无 PWD_BLOCKLIST.has( 直呼（判断逻辑不重复） | P8 |

场景用例代码（P6-P8 摘要）：

```js
// P6c: 谓词语义实测（eval 沙箱）
globalThis.window = globalThis;
const r = eval(blSrc + '; (function(){ return [isWeakPassword("TEST1234"),
  isWeakPassword("  admin123  "), isWeakPassword("xK9#mQ2$vL"),
  isWeakPassword("")]; })()');
assert.deepStrictEqual(r, [true, true, false, false]);

// P7: 三个新入口走谓词（与注册同源）
assert.ok(doChangeFn.includes('isWeakPassword(np)'));   // app.js doChangePassword
assert.ok(doSetFn.includes('isWeakPassword(pwd)'));      // app.js doSetPassword
assert.ok(forgotSrc.includes('isWeakPassword(v)'));      // forgot.html NEWPWD_RULE

// P8: 判断逻辑无散落拷贝
for (const src of [regSrc, forgotSrc, appSrc, accountJs])
  assert.ok(!/PWD_BLOCKLIST\.has\(/.test(src));
```

后端核对（前置成立）：grape_auth.py 四端点（register:336 / forgot-reset:503 /
change-password:537 / set-password:612 附近）均调 auth_password.check_password_strength，
内含 _WEAK_PASSWORDS 黑名单——前端预检是同源前移，非新规则。

### 复核问题清单处置（2026-10-28 深度审查，7 项）

| # | 严重度 | 处置 |
|---|--------|------|
| 1 | 中 | Host 头投毒（未配 TRUSTED_HOSTS 时已登录用户可持久污染 server_base_url）——继承 webhook_url 既有模式，**部署侧要求**：公网生产必须配 TRUSTED_HOSTS 白名单 + 反代正确传 X-Forwarded-Host/Proto（本 ECS nginx 已配）。库内不改动（门禁语义属 C2 契约，动门禁需删 C2c 用例另评） |
| 2 | 中 | **已修**：dashboard.js 补位调 banner 但 ?v=100 未 bump → 101（runtimes.js 89 / app.js 195 同批 bump） |
| 3 | 低 | **已修**：见上 P6-P8（全入口 + 谓词收敛） |
| 4 | 低-中 | **撤回**（审查误报）：/api/health 实测返回 server_base_url 字段（curl 验证 keys 含 server_base_url/configured/warning），runtimes.js 三处消费真实有效；且 /health 无需鉴权、渲染前可用，改调 require_org_member 的 engine-onboarding 反而引入登录依赖 |
| 5 | 低 | **已修**：_fromGuide 死变量删除（标记消费在渲染路径，注释同步） |
| 6 | 低 | **撤回**（核对无实据）：runtimes.js 与需求文档中无注释 URL 与代码不一致的实际样本 |
| 7 | 低 | **已修**：_is_local_base 补 https://localhost / https://127. / https://0.0.0.0 / https://[::1]（反代终结 TLS 后写回的本地值同样可自愈） |

### 二轮深扫 B/C/D 处置 + 全流程端到端验证（2026-10-28）

二轮家族扫描（意图标记/版本号断言/长度谓词三个维度横向全库），处置三项 + 一项待决：

| # | 家族 | 处置 |
|---|------|------|
| B | 测试锁死 `?v=` 具体值（T10/T19/U9-3/T9 i18n-refresh——任何合法 bump 都误红） | **已修**：改锁存在性/两页一致性；i18n_repos_local_refresh 因此从长期红转 11/11 绿（其红的根因正是 v=20261020 写死） |
| C | 密码长度 `8` 硬编码 9 处散落（admin×3/auth 评分×2/forgot·register 规则与 blur×4） | **已修**：生成器产出 `PWD_MIN_LEN=8` + `isPwdTooShort` 谓词（与后端 check_password_strength 下限同源，后端调整重跑生成器即全端同步）；P10f 无散落守卫另揪出 2 处首轮漏掉的 blur 内联一并收敛；admin.html 引入 blocklist |
| D | window 意图标记 4 个逐一审计 | `_eventNavFromTask`/`_rtPulseProbing` 闭环 ✅、`_emailConfigured` 是缓存 ✅、**`_isCreateMode` 真泄漏已修**（原只在返回/成功/失败三路径复位，侧栏离开残留 → 下次 dash 渲染智能体卡片按创建模式跳过 skill 名；nav() 与 `_dashboardEditing` 同位统一复位） |
| A | admin.js 改密弱口令预检缺一行（后端 admin /change-password 有 check_password_strength，前端只查长度） | ⏳ 条件已铺（admin.html 已引入 blocklist），预检一行待加 |

#### 全流程端到端验证（真实数据，2026-10-28，服务 127.0.0.1:3000）

真实链路：专用账号（密码登录）+ 真实托管仓库（managed_repos/user_repos）+ 真实 gitcode
绑定（external_identities）+ 引擎为空（needs_worker 真实判定）+ 解绑走后端真实
need_confirm 二次确认。10/10 通过（测试数据已清理）：

| # | 场景 | 实测结果 |
|---|------|---------|
| F1 | 进有仓库无引擎的工作台 | 横幅出现，文案/按钮正确 |
| F2 | 进用户设置 | **横幅消失**（原始缺陷场景）+ body padding 复位 |
| F3 | 返回工作台 | 横幅恢复（非跳过键误判） |
| F4 | 点"添加设备"跳转 | 引擎页 + 面板自动展开 + 安装命令可见 |
| F5 | 跳转后中途改道（任务页）再进引擎页 | 面板不自动弹（`_rtOpenPanelOnEnter` 作废；首次 True 为脚本时序噪音——旧面板 DOM 未移除完，干净重测 False） |
| F6 | 解绑唯一平台（真实 409 need_confirm → 受影响仓库列表 → confirm=true） | **留在 account 页**、currentRepo 清理、bindings=[]、toast 含受影响仓库数 |
| F7 | 解绑后主动返回工作台 | 落绑定引导页（语义合理路径） |
| F8 | 改密填弱口令 password123 | 前端拦截 0 API 请求；正常密码到达后端成功改密 |
| F9 | 谓词与 i18n | isPwdTooShort/isWeakPassword 语义正确；PWD_MIN_LEN=8；i18n 键返回中文（无裸键） |
| F10 | 后端数据一致性 | 解绑后 user_repos 被服务端 D42 真清空 |

非缺陷澄清：点"添加设备"跳转后横幅不再出现 = 跳过键设计语义（用户响应过引导
即记忆），清键后横幅即恢复，补位链路完好。

#### B/C/D 场景用例（P10-P11，tests/js/test_password_blocklist.js）

| # | 场景 | 预期 | 用例 |
|---|------|------|------|
| P10 | 长度谓词产出与语义 | 生成物含 PWD_MIN_LEN=8 + isPwdTooShort；短/空/null 拦截、8 位放行 | P10-P10c（eval 沙箱语义） |
| P10d-g | 消费面 | admin.js 三处、auth.js 评分阈值、forgot/register 规则走谓词；admin/forgot/register/index 四页无 `length<8/>=8` 散落；admin.html 引入 blocklist | P10d-P10g |
| P11 | 意图标记复位 | nav() 统一复位 `_isCreateMode`（防创建模式幽灵） | P11 |

场景用例代码（P10-P11 摘要）：

```js
// P10c: 长度谓词语义（eval 沙箱）
const r10 = eval(blSrc + '\n; (function(){ return [isPwdTooShort("abc123"),
  isPwdTooShort("abcdefg1"), isPwdTooShort(""), isPwdTooShort(null)]; })()');
assert.deepStrictEqual(r10, [true, false, true, true]);

// P10f: 无散落硬编码守卫（揪出过 2 处漏网 blur 内联）
for (const [name, src] of [['admin', adminSrc], ['forgot', forgotSrc], ['register', regSrc]])
  assert.ok(!/length\s*[<>]=?\s*8\b/.test(src));

// P11: nav 离开统一复位创建模式标记
assert.ok(appSrc.includes('window._isCreateMode = false;'));
```

## 引导链视觉与动效（多轮实测评审迭代，U1-U6 / V1-V3）

引导链功能就位后，经真实浏览器多轮走查评审，视觉/动效/时序经历七轮迭代：
高亮光圈 → 箭头气泡指引 → 苹果风格化 → PPT 级联入场 → 单一节奏线统一 →
跨环节视觉语言统一 → 绑定成功反馈。最终形态（对应代码 app.css v92）：

```
入场（单一节奏线，0.12s 恒定步进，0.6s expo-out）：
  0.05s logo → 0.17s 标题 → 0.29s 副标题 → 0.38/0.50/0.62s 卡片（浮入+缩放）
  → 0.86s 底部提示 → 1.35s 指引气泡（淡入）→ 1.85s 箭头弹跳
指引（用户注意力聚焦）：卡片呼吸光圈（纯光效，无 transform——避免与 hover 冲突）
  + 渐变底色 + 箭头+气泡文案"点击下方卡片绑定"
交互：hover 苹果缓动上浮（-2px+1.5% 缩放）；点击绑定 → 光效静默
成功：绑定的卡片原地变绿 ✓ 确认态（角标弹出动画）→ 1.6s 后进仓库列表
跨环节统一：引擎横幅品牌渐变+slide-down；引擎页引导脉冲同款光圈（定稿：
静态引导卡已删，视觉统一由工具栏"添加设备"按钮的呼吸光圈延续，keyframes
仅 box-shadow——与引导页 U3 同纪律）
```

| # | 场景 | 预期 | 用例 |
|---|------|------|------|
| U1 | 级联入场节奏 | 单一节奏线：各元素 0.12s 恒定步进、统一 0.6s 时长与位移；卡片是级联步骤（容器仅占位，无双层动画断层） | test_guide_visual.js U1 |
| U2 | 指引晚于卡片 | 气泡 1.35s 淡入（最后一张卡片 1.22s 落定后），弹跳 1.85s 起 | U2 |
| U3 | 光圈不与 hover 冲突 | 脉冲 keyframes 无 transform（纯 box-shadow/brightness）；hover transition 独占 transform | U3 |
| U4 | 点击绑定后卡片不消失 | .bound 停脉冲但保留 opacity:1；ob-shown JS 兜底（1.5s）锁定入场终态（防 forwards 被抹） | U4 |
| U5 | prefers-reduced-motion | 全部元素直接可见、无动画 | U5 |
| U6 | 跨环节视觉统一 | 引擎横幅品牌渐变（非警示黄）+ slide-down 入场；引擎页引导脉冲同款光圈（工具栏"添加设备"按钮呼吸光圈，keyframes 仅 box-shadow；静态引导卡 CSS 已清理无死选择器） | U6 |
| V1 | OAuth 绑定成功反馈 | 绑定卡片绿 ✓ 角标（弹出动画）+ 绿描边；1.6s 后自动 loadRepos | test_guide_visual.js V1 |
| V2 | 成功态时序桥接 | 回调处理早于引导页渲染时经 sessionStorage pending 标记，渲染完成后应用成功态 | V2 |
| V3 | 取消绑定 | 弹窗关闭后卡片可见、指引气泡隐藏（ob-bound-done 仅在真实成功/点击时加） | V3 |

场景用例代码（tests/js/test_guide_visual.js）：读 app.css/register.html/app.js
源码做结构断言（keyframes 无 transform、延迟值、类名/兜底存在性），
`node tests/js/test_guide_visual.js` 运行；动效终值以真实浏览器实测为准
（无头环境后台 tab 节流会暂停 CSS 动画，不能作为动效判定环境——实测记录
见提交 b620de18a/d45ce83ea）。

## 部署契约（一键部署环境差异清单，全链路扫描产出）

Host 自愈解决的是"代码侧可自愈"的地址问题；以下差异在代码侧无法自愈或
不应自愈，属于部署方/平台侧必须知晓的契约。引导页/横幅后续迭代可消费
本清单增强提示。

| # | 项 | 现状 | 契约要求 |
|---|-----|------|---------|
| C1 | OAuth App 回调地址 | redirect_uri 注册在 GitCode/GitCode 平台侧，换域名后平台直接拒绝回调，代码侧 Host fallback 无法参与 | 部署方需在平台 OAuth App 设置中同步改 redirect_uri（Host 自愈不覆盖此项） |
| C2/C2b/C2c | TRUSTED_HOSTS | C2：未设白名单时不读 X-Forwarded-Host（R11.1 门禁）；C2b：设白名单后 forwarded-host 参与且须命中；C2c（实测发现，现状固化）：X-Forwarded-Proto 不在门禁内——伪造仅能翻转推导协议 http↔https，不能改域名。不将 proto 纳入门禁的理由：未配白名单的合法反代会丢失 https 探测（存量破坏路径） | 公网生产部署建议设置 TRUSTED_HOSTS 白名单；如将来门禁 proto 需删 C2c 用例并同步本契约 |
| C3/C3b | 管理员初始密码 | C3：默认 admin123（未设 GRAPE_ADMIN_PASSWORD）；C3b：环境变量优先于默认值 | 部署契约强制：首次登录立即改密（前端 admin_pwd_initial 提示已存在） |
| C4/C4b | 邮件服务 | C4：无 email.json 时 send-code 返回 EMAIL_NOT_CONFIGURED 并引导直接账号密码注册（Issue #372 R7 设计降级，注册不受影响，email_verified=0）；C4b：verify 路径直建账号（email 允许为空）。受影响的仅是邮箱验证码注册与找回/重置密码 | 需要邮箱验证注册或密码找回的部署需配置 email.json（SMTP）或 Resend API；dev 环境可用 GRAPE_DEV_VERIFICATION_CODE 固定码 |
| C5/C5b | worker 安装命令 server | C5：install-worker.sh 默认 grape.example.com（占位假域名，手抄命令需自查）；C5b：UI 生成命令 --server 显式代入（闭环项，结构守卫防丢失） | 手抄命令需自查；UI 命令已闭环 |

C 系列场景用例（tests/test_deploy_contract.py，C1 平台侧配置无库内断言点，仅契约交付）：

| # | 场景 | 用例 |
|---|------|------|
| C2/C2b/C2c | X-Forwarded-Host 门禁 / 白名单参与+命中 / proto 门禁缺口现状固化 | test_c2_no_forwarded_host_without_whitelist / test_c2b_whitelist_enables_forwarded_host / test_c2c_known_proto_gap |
| C3/C3b | 默认密码 admin123 与 GRAPE_ADMIN_PASSWORD 优先序 | test_c3_default_password_admin123 / test_c3b_env_overrides_default |
| C4/C4b | send-code 降级 EMAIL_NOT_CONFIGURED + 引导直接注册 / verify 直建账号 | test_c4_send_code_degrades_gracefully / test_c4b_verify_creates_account_without_email |
| C5/C5b | 脚本占位 server 仍在 / UI 命令 --server 显式代入 | test_c5_placeholder_server_in_script / test_c5b_ui_command_injects_real_server |

已修复（同次扫描产出）：
- app.js 缓存版本号 bump（v185→v186）：前端引擎引导卡片/横幅对存量浏览器可见
- requirements.txt 补 requests>=2.31：providers.py / email_service.py / repo_manager.py
  直接 import 但未声明依赖，一键部署 pip install 后启动即 ImportError

## 覆盖汇总（程序化对账，场景 ID ↔ 测试锚点双向审计）

对账方法：正则提取文档全部场景编号（含合并单元格展开），逐一在测试文件中
定位锚点（pytest 函数名 / docstring 编号 / 断言标签），双向差异人工复核。

| 系列 | 场景数 | 处置 | 用例载体 |
|------|--------|------|---------|
| S1-S6, S7/S7b | 8 | ✅ tested | tests/test_oneclick_host_heal.py（单测 + TestClient 集成，函数名即场景号） |
| E1-E4（含 E2b-E2e） | 8 | ✅ tested | tests/test_engine_onboarding.py（mock 层 + 真实 SQLite 执行层） |
| O1-O5 | 5 | ✅ tested | tests/js/test_engine_onboarding_guide.js（onboarding 引导链形态断言） |
| B1-B10 | 10 | ✅ tested | 同上（bannerTests） |
| G1-G5 | 5 | ✅ tested | 同上（guideTests，hash 路由纯函数提取 + 结构守卫） |
| Q1-Q9（简化定稿：横幅+脉冲+直达+退场+排队 chip） | 30 项断言 | ✅ tested | tests/js/test_engine_guide_simplified.js（源码结构守卫 + i18n 对齐；2026-10-28 简化重构，替代原 R/U/V 系列） |
| X1-X8（判定 SQL 真实 SQLite 回归） | 8 | ✅ tested | tests/test_engine_onboarding_sql.py（列名锁定 + 反向用例 + org 隔离 + 大小写；2026-10-28 列名 bug 修复配套） |
| T1-T5 | 5 | ✅ tested | tests/js/test_token_create_deeplink.js |
| P1-P5 | 5 | ✅ tested | tests/js/test_password_blocklist.js（四条件纯函数 + 同源 blocklist） |
| C1 | 1 | ⏳ 平台侧契约（OAuth App redirect_uri 在平台侧配置，无库内断言点，文档交付） | — |
| C2-C5b（含 C2b/C2c/C3b/C4b/C5b） | 9 | ✅ tested | tests/test_deploy_contract.py（C2c 为实测发现后现状固化用例） |
| P6-P11（#3 全入口 + C/D 谓词与标记收敛） | 12 | ✅ tested | tests/js/test_password_blocklist.js（P1-P11 单文件，eval 语义 + 消费面 + 无散落守卫） |
| F1-F10（全流程端到端，真实数据） | 10 | ✅ 实测通过 | 浏览器真实链路验证（2026-10-28，表见上；含真实 need_confirm 二次确认）——非 CI 用例，记录性交付 |

合计 103 个场景：91 有随 CI 运行的用例锚点（含 P6-P11 新增 12 锚点），1 个为纯平台侧契约（C1），10 个 F 系列为真实数据端到端实测记录（非 CI 载体，文档交付）。
