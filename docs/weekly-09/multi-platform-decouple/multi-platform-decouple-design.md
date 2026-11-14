# 多平台支持 + OAuth 凭据解耦 — 设计文档（索引导航页）
> ⚠️ **本页为导航入口，正文唯一权威源在 `multi-platform-decouple/` 子目录——请勿在本页编辑内容**（防双源漂移）。
> 需求文档：multi-platform-decouple-requirements.md（同构索引页）。承接：grape-auth-audit v3.27 §5.6。
> 全局约束（跨分片生效，唯一权威在 `multi-platform-decouple/00-overview.md` 对应节 + 历史节选）：实现纪律/表设计铁律见各分片头部「继承」声明与 00 分片 D5。

## 阅读与结构说明

- **编号规则**：`00-06` 为设计域分片，`10-13` 为需求用例族分片（见 requirements 索引页）——两套编号区分子文档类型，非连续序号。
- **04/05 交错阅读顺序**：04 与 05 在 D53-64 段按时间线交错（历史写入顺序所致，每节完整移动、无主题中切）。连续阅读该段时按 **04(D53-62) → 05(D63) → 04(D64)** 顺序；两侧分片头部已互注。
- **05 拆分判据（下一拆分候选）**：05 当前 1029 行，内含两个自然切分面——清障方法论（D45-46/50-52/63）与清障执行+续审修复（D65-88）。**触发再拆条件：超过 ~1200 行或续审批次再增 3 节以上**，届时按该二分面拆分，避免长成第二个巨型文件。

## 分片路由

| 分片 | 域定义（回答什么问题） | D 节 |
|---|---|---|
| [00-overview.md](00-overview.md) | 目标形态与实施主线（D1-D5 + 实施顺序） | 5 节 | 来源 L1-L88, L3037-L3046 |
| [01-object-model.md](01-object-model.md) | 领域建模：凭据/身份对象模型、全域地图、双卡分离（D6-D11） | 6 节 | 来源 L89-L200 |
| [02-vcs-core.md](02-vcs-core.md) | vcs 基座与早期扫雷（D12-D25） | 14 节 | 来源 L201-L420 |
| [03-auth-channels.md](03-auth-channels.md) | 登录域与身份归属（D26-D40） | 15 节 | 来源 L421-L768 |
| [04-platform-rollout.md](04-platform-rollout.md) | 核心落地主线：实弹批/持久化 ORM/存储生命周期/方言注册（D41-44/D47-49/D53-62/D64；D53-64 段与 05 交错，见上方阅读说明） | 18 节 | 来源 L769-L913, L985-L1069, L1146-L1405, L1443-L1462 |
| [05-quality-infra.md](05-quality-infra.md) | 落地后清障与质量线：测试基建/枚举方法论/清障批次/续审修复（D45-46/D50-52/D63/D65-88；D63 与 04 交错，见上方阅读说明与拆分判据） | 30 节 | 来源 L914-L984, L1070-L1145, L1406-L1442, L1463-L2307 |
| [06-ledger.md](06-ledger.md) | 登记簿：挂账总清单 + 收官批（D89-D102） | 7 节 | 来源 L2308-L3036 |

## 批次方案稿（D90-D102 实施设计，本目录）

| 方案稿 | 域 |
|---|---|
| [d90-n2-repo-credential-contract-review.md](d90-n2-repo-credential-contract-review.md) | N1 凭据存储面治理契约评审 |
| [d90-n3-bot-token-encryption-review.md](d90-n3-bot-token-encryption-review.md) | N3 bot token 通道加密 |
| [d90-n4-audit-coverage-plan.md](d90-n4-audit-coverage-plan.md) | N4 审计覆盖方案 |
| [d90-n5-platform-config-review.md](d90-n5-platform-config-review.md) | N5 平台配置评审 |
| [d100-credential-type-schema-design.md](d100-credential-type-schema-design.md) | D100 凭据类型 Schema + sc 表 |
| [d101-task-token-snapshot-retirement-design.md](d101-task-token-snapshot-retirement-design.md)
| [d108-oauth-channel-config-decoupling-design.md](d108-oauth-channel-config-decoupling-design.md)
| [d109-platform-object-model-design.md](d109-platform-object-model-design.md) | D109 平台对象模型（四类配置 / 五张表 / 派生能力视图 / 用户级平台关系（状态实时计算 + 回填迁移）/ split mode / 扩展纪律与 checklist） | | D108 登录渠道配置解耦（oauth-providers.json 文件真源 + 与仓库托管配置分层 + 协议模式说明） | | D101 task 行凭据快照退役 |
| [rc-redesign-design.md](rc-redesign-design.md) | rc 表目标态重建（D99 前身） |
| [deploy-checklist-design.md](deploy-checklist-design.md) | D96 生产部署 Checklist 设计 |
| [deploy-checklist-mysql-backfill.md](deploy-checklist-mysql-backfill.md) | MySQL 首启回填观察（UC39） |
| [oauth-fill-contract-review.md](oauth-fill-contract-review.md) | OAuth 补全契约评审 |
| [cluster-config-convergence-requirements.md](cluster-config-convergence-requirements.md) | 集群配置收敛（独立立项） |

## 逐节路由（D 节 → 分片）

<details><summary><code>00-overview.md</code>（5 节，点击展开）</summary>

- D1：平台配置卡——一条配置驱动四层
- D2：webhook 层——方言模板 + 配置覆盖
- D3：OAuth 与仓库凭据解耦（K1-K4 消除）
- D4：git 传输层去硬编码（G1-G4）
- D5：分批与零 DDL 纪律

</details>

<details><summary><code>01-object-model.md</code>（6 节，点击展开）</summary>

- D6：两类凭据对象模型——登录凭据 ≠ 仓库凭据
- D7：凭据/身份对象模型总览（全域地图）
- D8：全流程凭据触点地图（8 场景阶段）
- D9：D6 与既有设计的关系
- D10：双配置卡分离——登录渠道卡 ≠ 仓库平台卡
- D11：S12 场景对既有设计的反向约束

</details>

<details><summary><code>02-vcs-core.md</code>（14 节，点击展开）</summary>

- D12：webhook 验签与注册路由收口（W2/W3 补全 D2/D10）
- D13：VCS API 命令层配置化——命令注册表 + API 方言模板
- D14：webhook 路由动态注册 + oauth_fill 解绑级联 + 平台卡管理面（W4/E1/A1 收口）
- D15：K3 工厂 + 新平台 PAT 批次 0 存储（已拍板）
- D16：平台前缀候选集与 web 链接抽象（扫雷第二轮，A/B/C 类收口）
- D17：图片附件与 ZIP 下载基址按卡派生（扫雷第三轮）
- D18：管道内 Issue 读取下沉到统一命令层（S28）
- D19：分支命令与 bot 巡检接线（扫雷第五轮，S29）
- D20：白名单列卡化 + 建 Issue 统一命令（扫雷第六轮，S30/S31）
- D21：建 Issue 调用方接线 + labels 方言语义（扫雷第七轮，S32/S33）
- D22：实现基座合一（不留给批次 1 的尾巴）
- D23：审计 P1-3 首批分文件清扫（tasks/agents；S34/S35）
- D24：三族审计合并前修复（探活误删防护 + 影子函数 + 重复类块）
- D25：agent 注入与流水线 bot 身份前置修复（审计 #2/#3，原 S12 批次提前）

</details>

<details><summary><code>03-auth-channels.md</code>（15 节，点击展开）</summary>

- D26：登录渠道协议策略（华为云 SSO 接入方案，S38/S39）
- D27：身份键安全（S46/S47，外部评审吸收项）
- D28：登录路由策略分派接线（S48，外部评审吸收项）
- D29：评论投递族卡驱动（S49，外部评审吸收项）
- D30：外围第二批卡化——admin 面与路由解析点（S50）
- D31：身份优先归属 + PR 探测卡驱动（S51，M5 零危害优先解）
- D32：验签头透传 + 方言 overrides + PAT 泛型入口（S52，先测后改纪律）
- D33：归属加固（S53，先测后改）
- D34：归属链收口——建档写入 + 通知链（S54，先测后改）
- D35：建单链卡化（S55，先测后改）
- D36：查重策略下沉适配器（S56，先测后改）
- D37：token 解析二分族收口（S57，先测后改）
- D38：bot token 升级链卡化（S58，先测后改）
- D39：仓库平台绑定/解绑域收口（S59，先测后改，领域建模纠偏）
- D40：oauth_fill 根治——显式链接注册表 + 凭据撤销域服务（S60，先测后改）

</details>

<details><summary><code>04-platform-rollout.md</code>（18 节，点击展开）</summary>

- D41：gitee issue 写路径方言收口（S61，先测后改，实弹观测驱动）
- D42：repos.py 托管主路径收口（S62，先测后改，用户领域纠偏）
- D43：凭据/列名族卡化——admin 面 + settings + bot 健康 + 路径 B 查询（S63，先测后改）
- D44：展示/IM/读侧族收口（S65/S66，先测后改）
- D47：gitee/gitlab 实弹批次——建单链/回执链/托管形态 10 缺陷收口（S70/S71/S72，先测后改，服务级实弹+官方文档驱动）
- D48：同型家族收口——S70/S64 缺陷形状的全仓扫描（S73，先测后改）
- D49：剩余挂账收口——附件同步卡化 / 落库响亮化 / 登录渠道管理面（S74，先测后改）
- D53：持久化骨架——身份/凭据/仓库域 ORM 化（S78，先测后改，业界参考评审驱动）
- D54：身份域 ORM 切换——双路径对账与三缺口落定（S79，先测后改）
- D55：凭据域单表+双写——grape_t_repo_credentials 收口（S80，先测后改）
- D56：仓库域查询族接 Repository（S81，先测后改）
- D57：仓库域单列助手直切（S81b，用户拍板：不加双路径开关）
- D58：双写移除——grape_t_repo_credentials 唯一权威（S82，先测后改，用户拍板方案 B）
- D59：平台卡文件外置——能力聚合点补完（S83，先测后改，评审论证采纳）
- D60：评审收口三件——直切准入制度化 / ORM 开关退役计划 / provider 派生统一（S84，评审采纳）
- D61：S82 读者收口补漏 + 全仓派生清零（S85，先测后改，批次 2 首批）
- D62：健康扫描去映射表 + 事件族 fixture 修复 + 阶段路由死锁修复（S86，先测后改，批次 2 续）
- D64：bot 身份链数据驱动 + github 怪癖能力位（S88，先测后改，批次 2 续）

</details>

<details><summary><code>05-quality-infra.md</code>（30 节，点击展开）</summary>

- D45：project-info 后台刷新单飞——段错误专项·线程风暴治理（S67，先测后改，faulthandler 驱动）
- D46：混跑污染专项——模块缓存/DB 绑定污染源收口（S68，先测后改，第三/四轮全量对账驱动）
- D50：全量枚举收口——方法论升级与 11 组确认洞（S75，先测后改）
- D51：族B/族A 终收口——第三轮枚举与五族清零（S76，先测后改）
- D52：IM 域终残留——绑定 provider 派生（S77，先测后改）
- D63：二分全量枚举分类台账 + 真二分收口第一批（S87，先测后改，批次 2 续）
- D65：树 API 家族卡位 + OAuth email 补抓卡位（S89，先测后改，批次 2 续）
- D66：模板 push 目标 host 卡派生 + 源平台 tarball 分派表（S90，先测后改，批次 2 续）
- D67：GRAPE_PERSISTENCE_ORM 开关退役 + engine 重绑跟随修复（S91，先测后改，批次 2 收口）
- D68：um 平台归属列运行时停写（S92，先测后改，um 退役第一步落地）
- D69：observability 可见性候选集统一 + health 聚合动态化（S94，先测后改，"待人工"桶首批）
- D71：D69"待人工"余量批次——登记站点卡/注册表派生收口 + 合法冻结分类（S96，先测后改）
- D72：confirm-done 关单链多平台重构——仓库能力抽象（S97，先测后改，用户拍板"只要仓库能力抽象"）
- D73：澄清评论发送统一——关单链同族收口（S98，先测后改）
- D74：um 退役批次 3——IM 通道目录 channel_directory（S99，先测后改，用户拍板方案 B）
- D75：D16/S25"占位符半截工程"全场景收口（S100，先测后改，外部扫描报告逐条实测核验后采纳）
- D76：repo_key_in_params 原子产物——候选集占位符/参数同源构造（S101，先测后改，外部评审采纳）
- D76 替换清单批次 1（S102，随批提交）：dashboard 10 处 + alerts 1 处迁入原子产物
- D77：扫描报告 P2 收口——知识读侧候选集 + 前端二分残留（S103，先测后改）
- D79：S69 测试基建批次 1-3——死用例清理 + 孤儿 inode 根因修复 + 两个真产品缺陷（S105/S106，先测后改）
- D80：G2 方言翻译模块补齐——gitee/gitlab 注册进 provider 注册表（S107，先测后改，架构红线贯通）
- D81：G3 收口——镜像语义全平台化完成（S108，收尾清障）
- D82：G5 测试基建批次 4——clarify/create/209 族清零（S109，先测后改）
- D78：parse_repo_key 内联回潮全仓扫描收口（S104，先测后改，D58 登记挂账清账）
- D83：审计修复批次——P0 app.js 截断 / P1 持久化依赖 / gitee 凭据脱敏 / F5+R4 死代码 / 测试基建环境无关性（S111，先审后改，第八轮续审驱动）
- D84：worker 包 import 闭包缺 src/vcs——存量 worker 升级即启动失败（S112，第九轮续审深度演练发现）
- D85：worker chat 用户 token 注入链修复——SQLAlchemy + deps.py 入包（S113，D84 挂账项收口）
- D86：测试迁移债清零 + host_repo 网络错放行回归修复（S114，30 净失败→0，独立分支）
- D87：git_repo_api 6 净失败归因修正 + 清零（S115，续审用户拍板"分支引起的就修"）
- D88：卡驱动漏网二元分叉收口（S115 续批：A2A fallback + templates 白名单 + admin verify-token）

</details>

<details><summary><code>06-ledger.md</code>（7 节，点击展开）</summary>

- D89：双卡体系治理方向修正——领域分离 + 基础设施薄共享（S116，2026-11-07）
- D95：N5 收官补验——场景链用例 + 缓存版本纪律收口 + migrate 合并修复（UC38，2026-11-08）
- D94：OAuth 未配置 + 用户 PAT 场景——github PAT 流完好性守卫（S121，2026-11-07）
- D93：agent 全链路凭据流——双链收敛守卫（S120，2026-11-07）
- D92：仓库凭据语义收口——三类凭据正名 + cred_type 隔离守卫 + 文件 fallback 告警（S119，2026-11-07）
- D91：仓库凭据策略链收口——路径分档守卫 + GlobalBot 缺失告警（S118，2026-11-07）
- D90：oauth_fill 落库消费端——跨域契约先行的待闭环登记（2026-11-07）
- D96：生产部署 Checklist——MySQL 首启回填观察三件套（UC39，2026-11-08）
- D97：全域续审——死 seam 测试债同根因清零（2026-11-08）
- D98：rc 双权威分裂修复——手动设置 token 补写 rc（UC40，2026-11-08）
- D99：rc 表目标态重建——id 主键 + cred_type 归一（UC41，2026-11-08）
- D100：凭据类型 Schema 驱动 + grape_t_system_credentials 建能力 + upc 退役 + OAuth 快照清退（UC42/UC42B，2026-11-08，方案稿 d100-credential-type-schema-design.md）
- D101：task 行凭据快照退役（claim 现取制）+ session 死列清理（策略链四改三）（2026-11-08，方案稿 d101-task-token-snapshot-retirement-design.md）
- D102：Gitee 实弹缺陷批——前端二分残留清查 ×8 + generic 适配器 owner 兜底（2026-11-08）
- D102-B/C：登录页平台按钮固定序+溢出折叠+hwcloud logo / 纯登录渠道象限（S12b 镜像）+可读用户名派生（2026-11-08）
- D102-D/E/F/G：providers 缓存 TTL 重载 / OIDC 引擎兜底（_tokens 复用+id_token 解码）/ 前端 host 派生通用化（issue 链接二分收口）/ A2A 字符串 ident 合法化（2026-11-08，集群配置收敛需求独立立项在册）
- D102-H/I/J：设计文档归拢（结构守卫）/ patrol ident 漏网第 16 处 / 全局同族扫尾（2026-11-08）
- D102-K/L：设置页双轴分离——引导页数据源分离+双路径并列（OAuth 未配置提示 PAT 显式回退）/ 第三方账号绑定与仓库凭据分域（方案稿 d102l-settings-split-design.md，业界参照在册）（2026-11-08）
- D102-M：双范式并存收口——来源徽章（凭据 tab 三态）+ fetch_user_token 命中来源审计（四层模型对账：存储分离✓/解析 Manual>OAuth 维持 D10/验证句柄化归 P8）（2026-11-08）

</details>

