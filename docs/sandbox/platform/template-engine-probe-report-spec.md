# 模板引擎探测上报 spec（自包含版）

> 本文档自包含：无需阅读其他 todo 文档即可实施评审。前置裁定结论已复制进正文。
> 实施记录：2026-11-03 探测批次 + 2026-11-04 §2.5.1 实施修正（三轮实测驱动）。
> 母本：docs 仓 todo/2026-11-02-template-engine-probe-report-spec.md（含完整修订史）。

## 0. 需求

worker 对 Provider 模板自动探测沙箱内已预装的 agent 引擎（类型+版本），上报主控。**用户裁定（2026-11-02 第二轮）：worker 一键安装时即自动全量/分批扫描模板引擎并上报，detected 行直接参与调度（用户无需前端勾选）；用户手动勾选仍允许，作为覆盖/收敛手段**。

## 1. 与既有设计的关系

| 既有设计 | 本 spec 的差异 |
|---|---|
| 绑定验真（10-30 spec 方案 B，未实施）：bind-engine 时**主控直连**建箱探测 | 探测方换 **worker**（真实网络路径——egress/网段差异只有 worker 测得出，对齐 manifest 分层执行裁定"模板检查委托在线 worker"）；且探测**先于**绑定（填表助手）而非绑定后验 |
| `sandbox_provider_report` 模板补报（cloud_worker.py:1212，已实施） | 同一上报通道 payload 加 engines 字段——搭现有车 |
| `_init_sandbox` 预装检测（engine_strategies.py:119，每次建箱在跑） | 结果回传 binding status——执行时验真的免费副产品 |

## 2. 方案（四件套，全链路平台无关）

### 2.1 manifest 能力位（P2 契约）

`MANIFEST.capabilities` 加 `probe_engines`。前端/主控据此判断该平台是否支持探测（与 list_templates 同款语义）——不支持的平台探测入口不出现，零特判。

### 2.2 adapter 探测契约（USP 接口）

```python
def probe_engines(self, config: dict, template_id: str) -> list[dict] | None:
    """返回统一格式 [{"engine": "hermes", "version": "0.19.0", "source": "envd|shell"}]；
    不支持返回 None（同 list_templates 语义）。"""
```

探测分层，尽量不建箱：
- **第一层（零成本）**：读平台元数据——envd 系（E2B/CubeSandbox）的 Template 模型自带 envd_version，镜像 labels/预装清单可读则直接读，**不建箱**
- **第二层（低成本，仅按需）**：建箱（~1s）→ `run_cmd("<binary> --version")` 扫 **ENGINE_REGISTRY 白名单**（现有 5 引擎，非任意命令扫描——安全边界）→ kill
- 探测实现收在 adapter 内部（cube 走 SDK 元数据、devbox 走 shell 扫描）——核心不知道平台细节

### 2.3 worker 上报链（复用现有通道，零新协议）

| 时机 | 通道 | payload 变化 |
|---|---|---|
| 启动模板补报 | `sandbox_provider_report`（cloud_worker.py:1212） | 每模板加 `engines` 列表 |
| 建箱后实测 | `_init_sandbox` 预装检测结果 | 回传 binding status（verified / 未预装留痕） |
| 绑定弹窗打开时 | 主控委托在线 worker 探测（复用检查模板的委托指令通道，manifest 分层执行 §3.3） | 按需二选一：元数据层（免费）/建箱层 |

服务端 UPSERT 进 `engine_template_binding`。

### 2.4 binding status 语义分层（2026-11-02 第二轮裁定后）

```
用户 WebUI 勾选绑定          → status='manual'    （人工权威，覆盖 detected 行）
worker 探测上报（自动发现）   → status='detected'  enabled=1（参与调度，默认可执行）
```

- **detected = 可执行**（用户裁定）：探测结果直接驱动引擎行展开与任务派发。理由：探测是 worker 真实建箱/读元数据的实测结果，非猜测——与用户手动勾选同等的可信度来源
- **manual = 覆盖**：用户勾选仍允许且优先级更高——同一 (provider_id, engine_type) 已有 manual 行时，探测上报不降级不改写（UPSERT 冲突规则：manual 恒赢）；用户也可勾掉（解绑）一个 detected 行 = 显式否决探测
- 用户解绑 detected 行后再探测到同引擎 → 重新 UPSERT 为 detected（用户若想永久否决某引擎，解绑后 platform 详情页提供"忽略此引擎"操作——极简做法：解绑即否决，重扫不再自动恢复，需再探测需手动触发"重新扫描"）

### 2.5 一键安装时的全量探测（用户裁定新增，核心交付）

worker 一键安装完成 → 首次启动（_amain）即执行**模板引擎全量扫描**：

```
启动 → 拉 provider-configs → 逐 Provider:
  ├─ 扫模板清单（现状, cloud_worker.py:1212）
  ├─ 元数据层探测（免费）: adapter 读平台模板元数据（envd 系带 envd_version）
  ├─ 建箱层探测（分批）: 逐模板建箱 → ENGINE_REGISTRY 白名单 --version 扫描 → kill
  └─ 上报: sandbox_provider_report payload 含 templates + engines
           → 服务端 UPSERT binding status='detected' enabled=1
```

**分批策略（防平台资源冲击）**：建箱探测按 Provider 的 max_sandboxes 流控串行/低并发（信号量限 1-2），批间隔可配（默认 0 即全量串行扫完再注册，批大时可分批边扫边上报——先到的 engines 先落库先参与调度）。每箱即建即杀（幂等，与验真同款），超时 60s。

**时序保证**：探测先于（或伴随）首次 register——worker 注册引擎行的展开数据源 = binding（含 detected）。一键安装完 → 主控 WebUI 打开平台详情页 → 引擎已上线（而非"等用户勾选再上线"）。首次注册前探测未完成的模板，后续 refresh 循环（60s）增量补齐（added 引擎注册链路 94aa2f0fa 已验证）。

**重复安装语义**：重装/重启 worker = 再探测再上报（幂等 UPSERT 刷 version，不增行不降级 manual）。

### 2.5.1 实施修正（2026-11-04，三轮实测驱动）

**①注册先行（推翻原「探测先于 register」设计）**：
- 原设计"探测先于（或伴随）首次 register"在实测中出事：探测（每箱 20-40s × N 模板）把
  `cloud_worker registered` 日志拖到 34s 后，**安装器 30s 注册验证窗口**判超时回滚健康新版
  （.17 实例 27 实证：54:49 还在正常建箱探测就被回滚，用户多次重装触发）。
- 修正时序：`拉模板 → register → recover_orphans → Provider 上报+探测（注册后）`。
  worker 秒级上线（与普通 worker 同语义：本地发现秒级、健康探测后置），安装窗口不再被探测吃掉。
  安装器侧 240s 沙箱窗口（489bbff8d）作双保险保留。
- "首次注册前探测未完成的模板 refresh 增量补齐"语义保留——现在全部模板的 binding 都走
  探测后上报（注册后数分钟），refresh 循环仍兜底引擎行展开。

**②失败对账 + 重试循环（修复 binding 永缺缺口）**：
- 实测缺口：启动探测批**只跑一次**；10min 健康线程只测本机引擎（沙箱 worker 是 no-op，
  引擎在远端沙箱）——130597 资源不足失败的模板**永远没有 binding**（.17 实例 22 实证：
  全天仅启动一次探测批，4 模板 130597 后永缺）。
- 对账设计：`_report_providers_once()` 返回失败计数 = Σ(每 Provider:
  扫描到的模板数 − 探测返回有引擎的模板数 + 上报异常×1)。三类未完成形态都计入：
  建箱失败（130597）/探测无引擎/批量截断。
- 重试循环：`_probe_retry_loop` 后台任务，有失败即每 120s 重跑整批（指数退避 600s 封顶），
  全成功自然退出。幂等性=三元组 UPSERT（见 2.4）+ 模板登记幂等——重跑零副作用。
- 已知代价：**真无引擎的模板永远差 1**（每次重试都重探一遍，幂等多花一轮建箱）——
  可接受（探测即建即杀，单轮成本 ~30s）；"确认无引擎"负缓存已实现（见④ probe-state.json）。

**③批量上限 PROBE_TEMPLATES_MAX（默认 8，env 可配）**：
- 防模板爆炸串行探测数小时（每箱 20-40s，8 个 ≈ 3-5min）。超出截断+告警，截断数
  经②对账计入失败 → 重试循环下轮续探（分批推进语义）。

**④本地负缓存 probe-state.json（2026-11-04 用户设计，消掉②的已知代价）**：
- 文件：`/var/lib/grape/cloud-worker/workers/<key>/probe-state.json`（与 whoami.json/
  provider-configs.json 同族，实例隔离）：`{"templates": {"tpl-xxx": {"engines": [...],
  "probed_at": iso, "ok": true}}}`。
- 语义：ok=true 模板后续批次直接跳过建箱（日志打命中数），**但已知引擎结果照常并入上报**
  ——服务端 binding 不因跳过而缺。失败永不缓存（重试循环必须重探）。
- 强制全量重探：删该文件即可（模板镜像装了新引擎后）。
- 缓存写失败非致命（告警继续探测）。

**⑤探测过程日志（防用户以为卡死）**：
- `[engine-probe] start: N templates, ~20-40s each` / 逐箱 `[k/N] creating sandbox for tpl-xxx ...`
  / 结果行带耗时 `(Ns)` / 收尾 `done: K/N templates with engines`。

**探测白名单**：仅 ENGINE_REGISTRY 已注册引擎（hermes/opencode/codex/codearts/hwcloud）——刻意边界，不扫任意二进制。UHP 开放接入后引擎清单声明式化，白名单随之扩大。

## 3. 解耦度检验（USP 六 gap 对照）

| Gap | 新耦合？ |
|---|---|
| G1 执行热路径 | 否——探测走 run_cmd/SDK 元数据契约，不碰 PTY 收编面 |
| G2 平台散布 | 否——探测实现全在 adapter（envd 元数据 vs shell 扫描的平台差异自吸收） |
| G3 worker 打包 | 否——探测依赖 ENGINE_REGISTRY（核心，平台无关）+ adapter 懒加载 |
| G4 能力发现 | probe_engines 即 manifest 能力位（与 supports_pty 同款） |
| G5 契约测试 | 契约套加参数化用例：声明 probe_engines 的 adapter 必须返回统一格式 |

结论：核心只新增三个平台无关概念（adapter 可探测 / worker 可上报 / binding 有 status），USP 形态成立。

## 4. 防死循环的解释（第二轮裁定后已不构成风险，保留为设计依据）

"死循环"指**调度链的自举悖论**，位置在 claim_by_tag 派任务机制：

```
引擎派任务的依据是 agent_runtimes 表的引擎行
引擎行的来源是 engine_template_binding 展开（cloud_worker _expand_templates）
binding 的来源（若全自动）= worker 探测结果
worker 探测引擎需要建箱跑命令
建箱发生在任务执行时（cloud_worker 只有领到任务才建箱）
任务派给引擎行 ← 回到第一行
```

链条断点：**全新模板零 binding → 零引擎行 → claim_by_tag 永远不派任务给它 → worker 永远不会为它建箱 → 探测永远不发生 → binding 永远不会出现**。系统对这个模板整体沉默，无人打破僵局。

这正是 2026-10-27 修过的「空模板死锁」同构问题（一键安装 worker 空模板 exit 2 循环，0c18a4c21）——当时结论就是引擎绑定必须有人工/事件先落第一行。第二轮裁定后 detected 直接参与调度——死循环之所以解除，是因为**探测行为本身发生在任务派发链之外**（worker 启动时主动建箱，不依赖"领到任务才建箱"）。原死循环的断点"worker 不会无缘无故为一个模板建箱"被一键安装全量扫描打破：探测是安装流程的主动步骤，不是任务执行的副产品。用户勾选降级为覆盖手段（manual 恒赢 / 解绑即否决），调度正确性由"探测=真实建箱实测"的可信度背书。

auto_trust_detected 开关即承认这个权衡：放开=用户信任探测自动落数行（探测是 worker 建箱行为，有真实网络路径背书，不是纯猜测），但换来误绑风险。

## 5. 风险与边界

1. **探测白名单边界**：只扫 ENGINE_REGISTRY 已注册引擎——非注册引擎测不出（刻意：不扫任意二进制，安全+误报控制）。UHP 开放接入后引擎清单声明式化，白名单随之扩大
2. **建箱成本**：第一层元数据免费；第二层建箱仅"新模板上报/绑定弹窗打开"时触发，不做周期建箱巡检
3. **版本字段**：仅供展示/筛选，不进调度决策（调度只看 engine_type + tags，避免版本比较进热路径）
4. **devbox**：非 envd 系无元数据层，只能建箱 shell 扫描——adapter 自选层级，manifest 如实声明
5. **上报幂等**：同 (provider_id, engine_type, template_id) UPSERT，重探测刷 version 不增行
6. **探测口径 ≠ 执行口径（2026-11-04 实测增补，升级探测深度）**：建箱层 `--version` 只证"二进制在"，不证执行链路——hermes 需 ACP 依赖（`hermes acp --check`）+ provider/模型链路；裸 hermes 模板会照样上报 detected 进调度，任务派发后才在引擎起动时炸。**当前 s2(hermes-git-acp) 模板实测无恙**（hermes v0.19.0 + `hermes acp --check OK`，ACP 依赖在 /opt/hermes-venv 自带），但这是"模板恰好造得全"，不是探测口径保证的。

   **修复（用户裁定 2026-11-04）：建箱层探测升一档——`--version` 通过后追加深检命令**，按引擎注册深度命令表（挂在 ENGINE_REGISTRY/EnginePlugin，声明式非硬编码 if-else）：

   | engine | 深检命令 | 验证面 | 增量成本 |
   |---|---|---|---|
   | hermes | `hermes acp --check` | ACP 协议依赖完整（对齐本机探活的第一步，不含模型推理） | ~2s |
   | opencode | 现版 `opencode run` 类轻量真跑（对齐本机 _PROBE_COMMANDS 口径） | 可执行+协议面 | ~3s |
   | codearts/codex/hwcloud | 暂保持 `--version`（无廉价深检命令，后续按需加） | 二进制在 | 0 |

   深检失败 → 该引擎**不上报**（宁缺勿假）+ worker 日志记 `probe deep-check failed: <err>`；不降级上报"detected 但不可用"——执行口径的失败就该挡在调度外。
   与 §2.4 的关系：detected=可执行的裁定**要求**这一档——深检是"可执行"承诺的最小诚实实现。总成本：每模板每引擎 +2~3s（即建即杀箱内追加一条命令），低于再建一箱。

## 6. 决策点

| # | 问题 | 建议 |
|---|---|---|
| D1 | ~~auto_trust_detected 默认值~~ | 已废——第二轮裁定 detected 直接 enabled=1，无需开关 |
| D2 | 探测触发时机 | 一键安装首启（全量：元数据层+建箱层，用户裁定）/ worker 后续重启（元数据层顺路 + 建箱层可选）/ 绑定弹窗按需（刷新单模板）/ 建箱后实测回传（免费）。不做周期巡检 |
| D3 | 与 10-30 spec 方案 B（绑定验真）合并实施 or 分开 | 建议合并：探测（worker 侧前置）+ 验真（binding 落库后 worker 复核）共用 adapter.probe_engines() 与委托指令通道，一份契约两个消费时机 |
| D4 | detected 行的生命周期 | 模板重扫 engines 变化时刷新；模板删除随 binding 级联；不保留过期 detected（防建议项腐化） |

## 7. TDD 顺序

1. RED：FakeAdapter 实现 probe_engines 返回统一格式 → 契约测试过；不支持的 adapter 返回 None → 能力位缺失自动跳过
2. GREEN：adapter 契约方法 + manifest 能力位
3. RED：sandbox_provider_report payload 带 engines → 服务端 binding UPSERT status='detected' enabled=0
4. GREEN：上报端点 + binding 列（status, engine_version——_ensure_column 幂等迁移）
5. 前端：绑定弹窗建议项徽标（探测到 X vX ✓，可点击确认转 manual）+ :title 全列
6. E2E：.17 worker 启动补报带探测 → WebUI 显示 detected 建议项 → 勾选转 manual → 引擎 60s 内注册上线

## 8. 验收

1. pytest 全量基线不变；新增探测契约/上报 UPSERT/binding status 用例
2. .17 实测：hermes-git-acp 模板探测上报显示 hermes（detected），勾选后 60s 注册上线，任务真实跑通
3. 不支持平台（无 probe_engines 能力位）前端无探测入口，行为零变化
4. **深检验收（2026-11-04 增补）**：裸 hermes（无 ACP 依赖）模板探测**不上报** hermes；`hermes acp --check OK` 模板正常上报 detected——detected 行的实际可执行率 100%（不再有"探测通过、执行即炸"行）
