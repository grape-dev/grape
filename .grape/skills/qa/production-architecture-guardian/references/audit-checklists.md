# 审计检查项清单（phases A–J，四段式）

> 每条检查项固定四段式：**检查内容 / 判定标准 / 所需证据 / 不通过时结论级别**（级别唯一取值域：`BLOCKED` / `Critical` / `Medium` / `Informational`，见 SKILL.md「四级判定体系」）。
> 判定落态：符合 / 不符合（须 `file:line` 或 `commit sha`）/ 不适用（写理由）/ 证据不足（`INSUFFICIENT DATA TO VERIFY` + 缺哪种测量）。
> **双向索引**：规则编号（A1…D2）与检查项编号的对应表见 `references/architecture-rules-baseline.md` §5；两表不得出现孤儿项。
> N1–N5 为生产就绪新增检查项（N1→B-2、N2→D-3、N3→G-3、N4→H-2、N5→F-3）。

## Phase A · Discovery（盘点）

| 编号 | 检查内容 | 判定标准 | 所需证据 | 结论级别 | 依据 |
|------|----------|----------|----------|----------|------|
| A-1 | 技术栈、运行环境、构建入口盘点 | 语言/框架/部署形态与仓库事实一致，无「文档写 A 代码是 B」 | `scripts/repo_inventory.py --json` 输出 + 关键 manifest `file:line` | Informational | `scripts/repo_inventory.py` |
| A-2 | 变更影响面盘点（目录聚合、组件边界、外部依赖面） | 影响面清单完整且能对上 `pulls files` 的实际变更文件 | 变更文件清单 + 目录聚合 + `commit sha` | Informational | `templates/finding.md` |
| A-3 | 代码目录落位与 `kernel` 依赖方向（规则 B3） | 新增文件落在 `src/api`（开放接口）/`src/domains`（业务领域）/`src/kernel`（公共能力）；**kernel 不 import domains** | 新增文件路径 + kernel→domains 的 import `file:line` | `BLOCKED` | 规则 `B3` |
| A-4 | 测试目录与 `src` 结构对齐（规则 B4） | 新增用例落在与 `src` 变更模块对应的 `tests/` 子目录 | 测试文件路径 + 对应 `src` 路径 | `BLOCKED` | 规则 `B4` |

## Phase B · Architecture Mapping（架构映射）

| 编号 | 检查内容 | 判定标准 | 所需证据 | 结论级别 | 依据 |
|------|----------|----------|----------|----------|------|
| B-1 | 新增开放 API 的 REST 风格一致性（规则 A1） | 资源命名/HTTP 方法/状态码与既有开放 API 风格一致，无 RPC 化动词端点 | 路由定义 `file:line` + 既有同类端点对照 | `Critical` | 规则 `A1` |
| B-2 | API 契约与向后兼容（规则 A14 + N1） | 未删除/改义既有开放接口的路径、必填参数、响应字段；新增能力有版本化或兼容层；公开契约变更有契约测试 | 变更前后的契约定义 `file:line`、契约测试用例、版本化说明 | `BLOCKED` | 规则 `A14`；`references/gate-review.md` §2 |
| B-3 | 大型特性模块化设计与接入（规则 A8） | 新特性按模块边界独立接入，而非在既有模块内堆叠 | 变更文件清单的模块分布 + 接入点 `file:line` | `Medium` | 规则 `A8` |
| B-4 | 新增开放 API 是否同步开放 CLI 能力（规则 A2） | 新端点有对应 `grape` 契约/命令开放，或给出不开放的明确理由 | 契约条目或命令缺失清单 | `Medium` | 规则 `A2` |
| B-5 | 用户文件上传与共享的存储抽象（规则 A5） | 用户文件读写经存储抽象（local/OBS 双后端），无绕过抽象直写本地盘 | 存储调用点 `file:line` + 后端实现 | `BLOCKED` | 规则 `A5` |
| B-6 | UI 国际化与双风格（规则 D1、D2） | 用户可见文案 key 化且有 zh/en 两份；UI 元素经主题令牌适配两套风格，无硬编码文案/单风格色值 | 前端变更行 `file:line` + 语言包与主题令牌对照 | `BLOCKED` | 规则 `D1`、`D2` |

## Phase C · Scalability（扩展性）

| 编号 | 检查内容 | 判定标准 | 所需证据 | 结论级别 | 依据 |
|------|----------|----------|----------|----------|------|
| C-1 | 新增接口吞吐目标（规则 A3） | 说明是否满足不低于 50tps 的业务目标；**数值来源=明确业务要求（Issue #3166 正文 A3）**，不得写成实测或行业标准 | 压测报告 / 设计说明（含来源标注）；缺则 `INSUFFICIENT DATA TO VERIFY` | `Critical` | 规则 `A3`；`references/numerical-evidence-gate.md` |
| C-2 | 新增接口时延目标（规则 A4） | 说明是否满足时延 <200ms 的业务目标；来源同 C-1，不得把 `200ms` 说成实测值 | 同上（时延口径与测量点定义） | `Critical` | 规则 `A4`；`references/numerical-evidence-gate.md` |
| C-3 | 扩展性假设与头部热点拆分 | 扩展性结论标注假设（数据量/并发量/增长曲线），热点有拆分或降级路径 | 假设清单 + 代码/配置 `file:line` | `Medium` | `templates/finding.md` |
| C-4 | 容量与瓶颈举证 | 容量结论不超出证据等级（`plausible` 不得表述为 `load-tested`） | 验证方式标签 + 指标出处 | Informational | 硬规则 2（SKILL.md） |

## Phase D · Security（安全）

| 编号 | 检查内容 | 判定标准 | 所需证据 | 结论级别 | 依据 |
|------|----------|----------|----------|----------|------|
| D-1 | 新增特性鉴权机制（规则 A9） | 走平台权限机制（组织/角色校验），无自建鉴权旁路 | 鉴权调用点 `file:line` + 权限入口对照 | `BLOCKED` | 规则 `A9` |
| D-2 | 运行态不得调用管理态（规则 A10） | 运行态（runtime/insight）代码无对管理态（mgmt/admin）服务或管理态接口的直接调用 | 调用点 `file:line` | `BLOCKED` | 规则 `A10` |
| D-3 | 多租户/组织隔离与越权路径（N2） | 数据/配置/资源访问均带租户（org_id）约束；列表、详情、导出、批处理四类路径均有隔离校验；无 IDOR/横向越权 | 查询与鉴权 `file:line` + 越权路径推演记录 | `BLOCKED` | N2；`references/gate-review.md` §2 |
| D-4 | 隐私数据加密存储（规则 C1） | 不可逆场景用 PBKDF2，可逆场景用 AES-GCM-256；无明文落库 | 加解密实现 `file:line` + 落库字段清单 | `Medium` | 规则 `C1` |
| D-5 | 外部接口日志与审计日志（规则 C2、C3） | 外部调用有请求日志；非查询类操作（创建/更新/删除）落审计日志，含操作者与对象 | 调用点与日志写入 `file:line` | `Critical` | 规则 `C2`、`C3` |

## Phase E · Performance（性能）

| 编号 | 检查内容 | 判定标准 | 所需证据 | 结论级别 | 依据 |
|------|----------|----------|----------|----------|------|
| E-1 | 数据库访问与查询次数 | 无 N+1 查询、无循环内查询、无全表扫描（无索引支撑）；查询次数与数据量关系可说明 | SQL/ORM 调用 `file:line` + 执行计划或索引定义 | `Critical` | `references/gate-review.md` §2 |
| E-2 | 缓存策略与失效路径 | 缓存键包含租户维度，有失效/穿透/雪崩防护，TTL 有出处 | 缓存调用 `file:line` + TTL 来源标注 | `Critical` | `references/numerical-evidence-gate.md` |
| E-3 | 异步任务与队列背压 | 有批大小/并发/重试预算的来源，失败可观测，无无界队列 | 队列配置 `file:line` + 阈值出处 | `Critical` | `references/numerical-evidence-gate.md` |

## Phase F · Reliability（可靠性）

| 编号 | 检查内容 | 判定标准 | 所需证据 | 结论级别 | 依据 |
|------|----------|----------|----------|----------|------|
| F-1 | 失败模式与缓解 | 关键依赖失败有超时/重试/熔断/降级中的明确策略；阻断级失败模式必须有缓解方案 | 策略配置 `file:line` + 故障场景推演 | `Critical` | `templates/finding.md` |
| F-2 | 幂等与重试安全 | 写操作具备幂等键或去重机制；重试不产生重复副作用 | 幂等实现 `file:line` | `Critical` | `references/gate-review.md` §2 |
| F-3 | 备份与恢复演练 RPO/RTO 举证（N5） | 有恢复演练记录，RPO/RTO 为实测值并标注出处；仅有设计值即证据不足 | 演练记录 + 指标出处（含时间与范围） | `BLOCKED` | N5；`references/numerical-evidence-gate.md` |
| F-4 | 单点与故障域 | 新增组件不引入未声明的单点；多副本/故障转移假设写明 | 部署拓扑 + 配置 `file:line` | Informational | `references/gate-review.md` §2 |

## Phase G · Data（数据）

| 编号 | 检查内容 | 判定标准 | 所需证据 | 结论级别 | 依据 |
|------|----------|----------|----------|----------|------|
| G-1 | 新增表命名规则（规则 A6） | 主控系统 `grape_t_xxx`、运营管理台 `grape_admin_t_xxx`，`xxx` 用英文字母表示用途 | DDL / 迁移脚本 `file:line` | `Critical` | 规则 `A6` |
| G-2 | 大表分区与归档（规则 A7） | 预估大表（消息/日志/事件类）有分区键或归档策略，且与查询模式匹配 | DDL `file:line` + 表设计说明 | `Medium` | 规则 `A7` |
| G-3 | 数据迁移可回滚（N3） | 迁移脚本具备双向策略（up/down 或等价回滚脚本），并有回滚演练证据；不可逆操作为 `BLOCKED` | 迁移脚本 `file:line` + 回滚演练记录 | `BLOCKED` | N3；`templates/gate-decision.md` |
| G-4 | 域数据库边界（规则 A11） | 各域只访问自身库；跨域数据经内部 API（gsb 总线或 lib public 契约），运行态访问管理态优先 lib 契约 | 数据源配置与 SQL `file:line` + 跨域调用点 | `BLOCKED` | 规则 `A11` |
| G-5 | 主键与唯一键非自增（规则 A12） | 新增对象主键/唯一键不使用自增 id（`AUTO_INCREMENT`/`AUTOINCREMENT`/隐式 `rowid`），使用雪花 UUID 口径 | DDL `file:line` | `BLOCKED` | 规则 `A12` |
| G-6 | SQL 双模兼容（规则 B1） | 新增 SQL 同时适配 sqlite 与 mysql，无单边方言 | SQL `file:line` + 方言清单 | `BLOCKED` | 规则 `B1` |

## Phase H · Operations（运维）

| 编号 | 检查内容 | 判定标准 | 所需证据 | 结论级别 | 依据 |
|------|----------|----------|----------|----------|------|
| H-1 | 可观测接入（规则 A13） | Metrics（Prometheus）/Logs（Loki）/Traces（Tempo）/Profile（Pyroscope）四项齐备，缺项逐项列出 | 接入配置或初始化代码 `file:line` | `BLOCKED` | 规则 `A13` |
| H-2 | 告警阈值出处（N4） | 每个告警阈值标注主源/需求出处（含指标口径与观测窗口），无出处阈值不得定稿 | 告警规则定义 `file:line` + 出处标注 | `Critical` | N4；`references/numerical-evidence-gate.md` |
| H-3 | 部署与配置变更影响 | 新增配置项有默认值、有出处、可回退；部署顺序与依赖写明 | 配置定义 `file:line` + 部署说明 | `Critical` | `references/numerical-evidence-gate.md` |
| H-4 | 数值证据门核对（规则 B2） | `magic_number_audit.py --json` 的 `unbacked_numbers[]` 逐条结案：举证、白名单登记（四类出处）或转 `<undecided>` + 待决策清单；无出处不得给数字 | 脚本 JSON 输出 + 登记表 + 报告未举证清单 | `Critical` | 规则 `B2`；`references/numerical-evidence-gate.md` |
| H-5 | 运行手册、密钥与开关治理 | 新增运维动作有操作步骤；密钥不入代码/配置明文；特性开关有默认态与关闭路径 | 文档/配置 `file:line` | Informational | `references/gate-review.md` §2 |

## Phase I · Supply Chain（供应链）

| 编号 | 检查内容 | 判定标准 | 所需证据 | 结论级别 | 依据 |
|------|----------|----------|----------|----------|------|
| I-1 | 新增依赖与固定版本 | 新增依赖有用途说明与固定版本（锁文件或精确版本），无浮动范围进生产 | `scripts/dependency_inventory.py --json` 输出 + manifest `file:line` | `Medium` | `scripts/dependency_inventory.py` |
| I-2 | 依赖来源与许可 | 依赖来源可追溯（官方/镜像），许可证与项目策略不冲突 | 来源清单 + 许可证标识 | Informational | `scripts/dependency_inventory.py` |
| I-3 | 构建可复现 | 构建步骤可重复，无对本机环境的隐式依赖 | 构建脚本 `file:line` | Informational | `scripts/repo_inventory.py` |

## Phase J · Verification Plan（验证计划）

| 编号 | 检查内容 | 判定标准 | 所需证据 | 结论级别 | 依据 |
|------|----------|----------|----------|----------|------|
| J-1 | 验证方式与发现类型匹配 | 每条发现给出与类型匹配的验证方式（安全/查询/扩展性/并发/队列/迁移/恢复），不写「已修复」无据结论 | 验证方式表（`references/architecture-review-runbook.md` §5） | `Critical` | `references/architecture-review-runbook.md` |
| J-2 | 压测与故障演练计划 | 有可复现的压测/演练计划（范围、口径、判据），与 C-1/C-2/F-1 结论对应 | 计划文档 + 指标口径 | `Critical` | `references/numerical-evidence-gate.md` |
| J-3 | 待决策清单与证据不足登记 | 所有 `INSUFFICIENT DATA TO VERIFY` 项进待决策清单，写明缺哪种测量与责任人 | 报告「待决策清单」小节 | Informational | `templates/audit-report.md` |
