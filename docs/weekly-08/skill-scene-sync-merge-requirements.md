# skill 同步与场景同步合并为串行定时任务

> **分支**：`feat/skill-scene-sync-merge`
> **基线**：`weekly-08`（已 rebase 至 head c100ddd3；纯新增文档，合入无冲突）
> **状态**：零实现需求提案，设计文档确认后方可实施
> **编号说明**：原内部编号 R13/P29/P30 与 weekly-08 已有编号冲突（weekly-08 已占用 R1-R30、P01-P44），2026-10-22 全库扫描后重新分配为 **R31 / P45 / P46**。

## 一、总览

### 1.0 铁律（不可违背的设计约束）

> **同步链路严禁破坏生产数据。** 定时同步任务的写操作只允许"幂等新增/更新"（INSERT OR IGNORE / 条件 UPDATE）；**任何删除、停用、覆盖用户或管理员数据的语义一律禁止出现在同步链路中**——发现脏数据只做只读检测+报告，处置权归管理台管理员。本提案的一切设计决策如与此冲突，以本条为准。任何后续提案同样受本条约束：宁可留下可见的残留（带告警日志），不可引入不可控的自动化破坏路径（一次瞬时故障被放大为生产事故）。2026-10-22 评审确立：早期版本的"对账删除/自动停用"设计即因违反本铁律被否决并重写为检测+报告。

### 1.1 背景

两个独立的 asyncio 定时任务（skill 同步 + scene 同步），各自 30 分钟周期，各自独立启动。scene 同步的 skill 引用预检依赖 skill 同步的 DB 同步完成，但两个 worker 并发执行无顺序保证，首次启动时场景可能全部被跳过。

同时发现 scene_sync.py 存在临时目录泄漏 bug（`tmp_dir` 未赋值，finally 清理永远不执行）。

### 1.2 需求范围

| 编号 | 需求 | 类型 |
|------|------|------|
| R31 | skill 同步与场景同步合并为串行定时任务，消除依赖竞态 + 避免重复下载 | 架构优化 |
| P45 | 修复 scene_sync.py sync_official_scenes 临时目录泄漏 bug | Bug 修复 |
| P46 | 场景/skill 同步补 stale 只读检测（含完成守卫，零 DB 写，铁律 1.0），统一 skill 预检口径，补 scenes/ 早退兜底（见 §六） | Bug 修复 |

### 1.3 不在范围

- 登录错误提示优化（独立分支 `feat/login-error-prompt-optimization`）
- 全屏 z-index 修复（独立分支 `feat/fullscreen-zindex-fix`）
- 一键部署脚本（独立分支 `feat/one-click-deploy-script`）

### 1.4 实测数据（2026-10-22 核验）

以下数字为对当前 head 的实测结果，替代早期估算：

- grape-skills 仓库 ZIP（main 分支）实测 **1.97 MB**（768 个条目，匿名 302→OBS 下载成功，与代码 R25.6 注释一致：token 非硬前提）。
- 临时目录泄漏量：**每周期 1 个 ~2MB 目录，48 周期/天 ≈ 96 MB/天**（早期文档按 50MB ZIP 估算的 2.4GB/天夸大约 25 倍，予以更正）。
- 活体证据：部署实例 /tmp 现存 6 个残留的 `scene_sync_zip_*` 目录（其一 5.1MB），佐证 P45 真实发生。
- 附带发现：skill_sync.py 现有清理 `tmp_dir = repo_root` 只删除 mkdtemp 内的仓库子目录，**空的 `scene_sync_zip_XXXX` 父目录每周期残留 1 个**——本设计的 `os.path.dirname(repo_root)` 清理形式顺带修复此问题。
- "首次启动场景 100% 被跳过"为代码结构推断（两个 worker 同刻启动、scene 侧处理轻量先到查表），未实测复现；即使发生，30 分钟后下一周期自愈。

---

## 二、R31：skill 同步与场景同步合并

### 2.1 当前架构

```
main.py:207  start_skill_sync_scheduler()  → _skill_sync_worker (每 1800s)
main.py:214  start_scene_sync_scheduler()   → _scene_sync_worker  (每 1800s)
```

两个 worker 各自调用同一个 `_download_repo_zip` 函数（定义在 `scene_sync.py:202`，被 `skill_sync.py:221` import）下载同一个仓库 ZIP。由于两个 worker 是独立的 asyncio task，各自调用一次该函数，导致同一个 ZIP 被下载两次。

### 2.2 依赖竞态

scene 同步步骤 4-7 直接依赖 skill 同步步骤 4（skill 表 DB 同步完成）：

```
scene_sync._check_skills_exist()       (scene_sync.py:154)
  → SELECT skill_name FROM skill WHERE skill_name IN (...)
  → 依赖 skill 表已有记录
  → 依赖 skill_sync._sync_skills_to_db() 已执行  (skill_sync.py:64)
```

### 2.3 优化方案

将场景同步合并到 skill 同步的同一个定时任务中，按依赖顺序串行执行：

```
_skill_sync_worker (每 30 分钟)
  ├── sync_official_skills()      ← skill 同步（远程下载 + DB + OBS）
  │     ↓ 完成后
  └── sync_official_scenes()      ← scene 同步（复用已下载的 ZIP + skill 表已就绪）
```

### 2.4 P45：scene_sync.py 临时目录泄漏

**文件**：src/storage/scene_sync.py:308, 316, 508
**问题**：`tmp_dir` 初始化为 `None`（L308），下载成功后赋值给 `repo_root`（L316），但从未执行 `tmp_dir = repo_root`。`finally` 块 `if tmp_dir:` 永远为 false，清理永远不执行。
**对比**：skill_sync.py:238 正确执行了 `tmp_dir = repo_root`（但其清理形式只删子目录、残留空父目录，见 1.4）。
**修复**：下载成功后 `tmp_dir = repo_root`；caller 提供 repo_root 时由 caller 负责清理（含父目录）。另：现有泄漏目录无兜底清扫（进程 kill -9 / 断电后残留目录永不清理），建议 upload_cleanup 增加 ">24h 的 `scene_sync_zip_*` 泄漏模式" 扫描规则（可与 P45 同批实施；范围仅限 sync 模块的泄漏模式，不含其他模块如 executor 的 hermes-iso-* 临时目录——那属于各自模块的清理职责）。

### 2.5 影响分析

| 维度 | 当前 | 优化后 |
|------|------|--------|
| 定时任务数 | 2 个独立 worker | 1 个 worker |
| 仓库下载 | 每周期 2 次（2×~2MB） | 每周期 1 次 |
| 依赖顺序 | 不保证（并发竞态） | 保证（串行执行） |
| 首启场景跳过 | 可能全跳过，30min 后自愈 | 0%（串行保证） |
| 分布式锁 | 2 个 | 正常路径 1 个 |
| 临时目录泄漏 | scene_sync 泄漏 ~96MB/天 + skill_sync 空父目录残留 | 全部修复 |

### 2.6 验证清单

- [ ] 服务首次启动 → skill 同步完成后自动触发场景同步 → 场景市场有数据
- [ ] 新 skill + 新场景推送 → 同一个周期内 skill 先入库 → 场景同步成功
- [ ] 仓库 ZIP 只下载一次（日志中只有一个 "download repo zip" 记录）
- [ ] 临时目录（含 mkdtemp 父目录）在同步完成后被清理
- [ ] 连续运行 3 个同步周期，/tmp 下无泄漏的临时目录、无残留空父目录
- [ ] `sync_official_scenes(repo_root=None)` 独立调用正常（自行下载 + 清理）
- [ ] skill 同步成功但场景同步失败 → 不影响下一个周期
- [ ] 异常路径下临时目录不泄漏（try/finally 保证）
- [ ] P46：仓库删除场景后，stale 检测报告出现在日志（warning + stats），DB 零写变更（UC9-1，铁律 1.0）
- [ ] P46：scenes/ 早退路径执行兜底初始化（UC9-2）
- [ ] P46：预检与 ref 重建口径一致，无 skill_id=0 组合（UC9-3）
- [ ] P46：遍历中途异常时检测跳过本轮、DB 零写变更（UC9-4）
- [ ] P46.4：json 移除 skill 后 stale 检测报告、守卫跳过、管理员停用后 ref 不指向废弃行（UC11-1/11-2/11-3）
- [ ] 测试回归：test_scene_sync.py 38 用例全绿、skill 侧测试不触发未 mock 的 scene 逻辑（UC10-1/10-2）
- [ ] P45 伴生：kill -9 后残留的 scene_sync_zip_* 目录由 upload_cleanup 兜底清扫
- [ ] 网络重试：瞬态故障 3 次退避内自愈（UC12-1）、持续故障有限退出不放大（UC12-2）、4xx 快速失败（UC12-3）、连续 3 轮失败升级 ERROR（UC12-4）
- [ ] 滚动升级：窗口期锁试探避让零并发（UC13-1）、终态试探恒成功零开销（UC13-2）、无锁序环（UC13-3）、试探失效时唯一约束兜底（UC13-4）
- [ ] 锁管理：长周期自动续锁且锁被抢即中止（UC14-1）、试探锁续期（UC14-2）、周期 >900s 提前 ERROR 告警（UC14-3）、崩溃后锁 TTL 自动失效（UC14-4）、正常释放语义不变（UC14-5）
- [ ] 多平台源：GitHub 全链路（UC16-1）、gitcode 不变（UC16-2）、未知 host 回退（UC16-3）、解析防回归（UC16-4）、GitHub+token 匿名（UC16-5 P0）、测试环境解耦（UC16-6 P1）
- [ ] 异步与收敛：调度层不阻塞启动/请求（UC15-1）、执行层阻塞现状已披露未变差（UC15-2）、连续多周期后各存储位无超线性增长（UC15-3）、管理员手工收缩路径可用（UC15-4）

---

## 三、场景与场景用例

以下场景（Scenario）描述触发条件与环境，场景用例（Use Case）给出可执行的验证步骤与预期结果，供实施后逐条验收。

### 场景 S1：服务首次部署（双表为空）

**UC1-1 首启场景市场有数据**
- 前置：全新部署，skill 表与 scene_market 表均为空
- 步骤：启动服务，等待首个同步周期完成
- 预期：日志顺序为 skill DB sync → scene `_check_skills_exist`；scene_market 表有官方场景记录；前端场景市场页面非空

**UC1-2 首启 ZIP 只下载一次**
- 前置：同 UC1-1
- 步骤：grep 服务日志统计 "download repo zip" 出现次数
- 预期：每周期恰好 1 次（此前为 2 次）

### 场景 S2：官方仓库新增 skill + 场景

**UC2-1 同周期内新 skill 先入库、新场景同步成功**
- 前置：服务正常运行，仓库新增 skill X 及引用 X 的场景 Y
- 步骤：推送变更，等待下一个同步周期
- 预期：同一周期内 skill X 入库后场景 Y 同步成功，`scene_skill_ref.skill_id` 指向 skill X 的主键；日志无 "引用了不存在的技能: X"

### 场景 S3：skill 同步下载失败（降级路径）

**UC3-1 scene 同步回退独立下载**
- 前置：模拟 skill sync 下载失败（如断网或仓库 5xx），`repo_root=None`
- 步骤：触发同步周期
- 预期：scene 同步仍以 `repo_root=None` 自行下载执行；若也失败则 `_init_featured_scenes_if_empty()` 兜底；无异常外抛

### 场景 S4：场景同步自身失败（故障隔离）

**UC4-1 scene 失败不影响 skill 与下周期**
- 前置：注入 scene 同步异常（如 scenes/ 结构非法）
- 步骤：触发两个连续周期
- 预期：第 1 周期 skill 同步结果正常返回，仅 scene 部分记 exception 日志；第 2 周期行为不受影响；临时目录仍被清理（try/finally）

### 场景 S5：standalone worker 移除（已决策：方案 A）

> **已决策（2026-10-22，负责人确认）**：skill 与场景定时任务合一——只保留合并后的单一 `_skill_sync_worker`。`_scene_sync_worker`、`start_scene_sync_scheduler`、`stop_scene_sync_scheduler` 及 `SCENE_SYNC_INTERVAL` 死配置一并删除。`sync_official_scenes(repo_root=None)` 的独立调用能力保留（测试与未来 admin 触发端点仍可复用，届时十分钟可补）。以下用例为删除验收。
>
> **能力溯源（git 全史检索，4338 commits）**：该 worker 体系由 issue #11（2026-09-04，commit 94cb9cf3d）一次性引入——scene_sync.py 含调度器三件套 + interval 配置 + main.py 挂载 + 42 个测试。两个关键事实支撑删除决策：
> 1. **"管理员手动触发"自引入起就不存在**：全史 `-S` 检索确认 start/stop 调度器从 2026-09-04 至今仅被 main.py lifespan 一处调用，从未有第二调用方——这是 issue #11 文档设想但从未落地的能力，删除不消灭任何被使用过的功能。
> 2. **issue #11 的提交说明承诺了"delisting"（下架）但实现只有 INSERT/UPDATE**——P46.1 的"无删除机制"是原始交付的缺口，不是后来退化；同日即有 scene_skill_ref 指向 inactive skill 的修复，预检口径问题（P46.2）也是同期遗留。
>
> 后续 6 周 4 轮修复均在这套框架上打补丁（ZIP 下载方式改造引入了 P45 泄漏、admin 字段覆盖修复、分布式锁加固），调度结构无人动过——双 worker 并存 + 双锁即源于最后一轮锁加固。

**UC5-1 删除后无残留引用**
- 前置：合并实现完成
- 步骤：全仓 grep `_scene_sync_worker\|start_scene_sync_scheduler\|stop_scene_sync_scheduler\|SCENE_SYNC_INTERVAL`
- 预期：除 git 历史外零命中；启动日志只有 skill sync scheduler 一条；shutdown 只停 skill worker（内含 scene 同步，UC8-1）

**UC5-2 独立调用能力保留**
- 前置：删除后
- 步骤：测试中直接 `await sync_official_scenes(repo_root=None)`
- 预期：正常下载、同步、自行清理临时目录（standalone 语义完整，供测试与未来 admin 端点复用）

### 场景 S6：多 worker 部署（uvicorn --workers N）

**UC6-1 单一 worker 执行合并同步**
- 步骤：以 `--workers 2` 启动，触发周期
- 预期：`skill_sync` 分布式锁保证仅一个 worker 执行完整 merged 流程（skill+scene）；总时长 ~45s << 锁 TTL 1800s，无锁过期并发

### 场景 S7：异常退出与清理（P45 验收）

**UC7-1 正常周期零泄漏**
- 步骤：连续运行 3 个周期
- 预期：/tmp 下 `scene_sync_zip_*` 目录（含空父目录）数量不增长

**UC7-2 异常路径零泄漏**
- 前置：在 scene 处理中途注入 RuntimeError，甚至让 logger.exception 抛 IOError
- 步骤：触发周期
- 预期：try/finally 保证临时目录仍被删除（对比 P45 修复前：任何路径都不清理）

### 场景 S8：优雅关闭

**UC8-1 停止服务时合并流程随 skill worker 一同取消**
- 步骤：同步运行中执行 shutdown
- 预期：`stop_skill_sync_scheduler` 取消的 task 同时终止内嵌 scene 同步；`stop_scene_sync_scheduler` 保留为 no-op 安全调用

### 场景 S9：官方仓库删除场景（P46 检测+报告）

**UC9-1 已删场景可被管理员发现（检测+报告，铁律 1.0）**
- 前置：DB 存在仓库已删除的场景（审计实测：`api-skill-dev`、`skill-dev` 残留且 is_active=1，拖 10 条孤儿 scene_skill_ref）
- 步骤：推送删除变更，等待下一个同步周期
- 预期：同步日志出现 warning："stale official scene: api-skill-dev, skill-dev (removed from repo)"，stats 含 `stale_official=2`；**DB 无任何写变更**（is_active/行数/ref 数完全不变）；下架由管理员在管理台手工执行后，下一轮检测不再报告

**UC9-2 scenes/ 结构异常时的兜底（早退路径修复）**
- 前置：模拟仓库 scenes/ 目录缺失或更名
- 步骤：触发同步周期
- 预期：退出前调用 `_init_featured_scenes_if_empty()`，新装实例不出现全空白市场（当前代码 L324 直接 return，绕过兜底）

**UC9-3 预检与 ref 重建口径一致**
- 前置：场景引用仅 user-scope 存在的技能，或已停用（is_active=0）的技能
- 步骤：触发同步周期
- 预期：`_check_skills_exist` 与 `_rebuild_scene_skill_ref` 采用同一过滤口径（is_active + scope），不再出现"预检通过但 skill_id=0 写入"的组合

**UC9-4 检测的完整性前提（守卫仅护报告准确性）**
- 前置：同步周期中某场景处理抛异常（DB 忙 / icon 上传失败），遍历中途失败
- 步骤：注入异常后触发同步周期，随后检查日志与 DB
- 预期：本轮 stale 检测**跳过**（不出具可能误报的名单），下一完整周期补上；**DB 零写变更**——最坏后果是"本轮没有报告"，不存在任何数据伤害路径（铁律 1.0）

### 场景 S10：测试与回归（实施配套）

**UC10-1 既有 scene 测试套不受破坏**
- 前置：tests/test_scene_sync.py 现有 38 个用例、6 处直接调用 `sync_official_scenes()`
- 步骤：签名加 `repo_root=None` 后全量跑该文件
- 预期：全部通过（默认参数向后兼容，standalone 模式行为不变）

**UC10-2 skill 侧测试不意外触发 scene 逻辑**
- 前置：skill_sync 串行调用 sync_official_scenes 后，skill 侧测试路径会连带执行 scene 逻辑
- 步骤：跑 skill 相关测试（含 test_skill_sync_obs.py）
- 预期：scene 调用被 mock 或短路，不因 DB/OBS mock 缺失而挂；新增测试显式区分 standalone 下载 / caller 注入两种模式

### 场景 S11：官方仓库移除 skill（P46.4 检测+报告）

**UC11-1 已删 skill 可被管理员发现（检测+报告，铁律 1.0）**
- 前置：skill-description.json 移除某 official skill（DB 行残留，is_active=1）
- 步骤：推送变更，等待下一个同步周期
- 预期：同步日志出现 warning："stale official skill: <name> (removed from repo json)"，stats 含 `stale_skill=N`；**DB 无任何写变更**；停用由管理员手工执行（is_active=0 或 admin 操作），停用后引用它的场景在下一轮场景同步中预检判缺失（P46.2 口径统一后），不再解析到废弃 skill_id

**UC11-2 skill 检测的完整性前提**
- 前置：skill-description.json 解析失败或 DB 写入中途异常
- 步骤：注入异常后触发同步周期
- 预期：stale 检测跳过本轮（不出具可能误报的名单）；**DB 零写变更**；临时目录仍清理（whole-body finally）

**UC11-3 skill 停用后 scene_skill_ref 的连带一致性**
- 前置：管理员已手工停用 skill Y（is_active=0），场景 X 引用 Y（scene_skill_ref.skill_id 已指向 Y）
- 步骤：触发下一轮场景同步
- 预期：`_rebuild_scene_skill_ref` 重建时 Y 落入 user/repo 兜底查询或 skill_id=0（带 warning），引用链不指向 is_active=0 的 official 行；运行中 pipeline（devops_pipeline_scene 经 market_scene_id 引用场景）不受场景/技能停用影响

### 场景 S12：网络异常与重试（§8.7）

**UC12-1 瞬态网络故障轮内自愈**
- 前置：模拟下载首次 httpx.TransportError（连接重置），第二次成功
- 步骤：触发同步周期
- 预期：第 1 次失败 → 5s 退避 → 第 2 次成功，周期正常完成；日志含 attempt=1 warning；临时目录正常清理

**UC12-2 持续网络故障不放大**
- 前置：模拟下载持续 5xx
- 步骤：触发同步周期
- 预期：3 次尝试（5/15/45s 退避）后 ERROR 日志 + 降级（scene 走 featured 兜底）；总耗时约 +65s，锁 TTL 1800s 内安全退出；下轮周期整体重试（content_hash 增量只补失败部分）

**UC12-3 4xx 快速失败不重试**
- 前置：token 失效返回 401
- 步骤：触发同步周期
- 预期：不重试（非网络类），直接降级 + 日志指明认证失败——重试坏 token 只会产生噪音

**UC12-4 连续失败升级告警**
- 前置：连续 3 个周期整体失败
- 步骤：检查日志级别
- 预期：第 3 轮出现 ERROR 级"skill sync: 3 consecutive failed cycles"（监控可按关键字告警）；单轮失败保持 WARNING 不误报

### 场景 S13：滚动升级双跑窗口（§8.8 锁试探避让）

**UC13-1 窗口期避让：旧 scene worker 在跑 → 新 worker 跳过场景步骤**
- 前置：滚动部署中，旧实例的 `_scene_sync_worker` 持有 `scene_sync` 锁正在同步
- 步骤：新实例合并 worker 完成 skill 步骤后非阻塞试探 `scene_sync` 锁
- 预期：试探失败 → 仅跳过本轮场景步骤（记 warning），skill 同步结果正常返回、临时目录仍被清理；任一时刻不存在两个进程并发写 scene_market；下轮旧实例已下线 → 试探成功 → 场景同步恢复

**UC13-2 终态零开销：全量新代码下试探恒成功**
- 前置：所有实例均已运行新代码（无旧 scene worker）
- 步骤：观察任意周期的锁试探
- 预期：`scene_sync` 锁永远空闲，非阻塞试探瞬间成功，行为与单锁设计完全一致；每周期仅多一次无竞争的锁获取/释放

**UC13-3 无死锁**
- 前置：任意时刻
- 步骤：审查锁获取顺序
- 预期：skill_sync 先取、scene_sync 试探非阻塞不等待、旧 worker 不取 skill_sync——不存在锁序环；试探锁 TTL 300s，持有者崩溃自过期

**UC13-4 纵深防御仍在（试探被绕过也无害）**
- 前置：假设试探逻辑异常失效，两个进程并发写 scene_market
- 步骤：检查并发 INSERT/UPDATE 行为
- 预期：唯一约束 uk_name_scope_user_repo 拒绝重复插入（输方记 failed），UPDATE 按 content_hash 幂等——无数据损坏，仅浪费

### 场景 S14：长周期与系统异常下的锁管理（§6.3 自动续锁 + 到期失效）

**UC14-1 长周期自动续锁**
- 前置：仓库规模增长或网络重试叠加，单轮周期接近/超过 skill_sync 锁 TTL 1800s
- 步骤：观察场景步骤入口与长循环内的 renew_lock 调用
- 预期：沿用 skill_sync.py:280 现成模式——场景步骤入口续一次、长循环内按条目续；锁全程保持持有，周期任意长不失效；续锁失败（锁被抢）→ 记 ERROR 并中止本轮剩余步骤，绝不持失效锁继续写（铁律 1.0）

**UC14-2 试探锁同样续期**
- 前置：场景步骤（含 icon 上传）超过 scene_sync 试探锁 TTL 300s（约 4 分钟）
- 步骤：观察场景步骤内的续锁
- 预期：试探锁按同模式续期；步骤完成即释放

**UC14-3 周期超时提前告警**
- 前置：某轮周期总时长超过 TTL/2（900s）
- 步骤：检查日志
- 预期：出现 ERROR 级"skill sync: cycle exceeded 900s"（监控关键字可告警）——锁过期风险在发生前可观测，提示调 TTL 或排查卡死，而非事后静默并发

**UC14-4 持有者崩溃锁自动失效**
- 前置：worker 进程在持锁期间崩溃 / kill -9 / 断电（finally 释放来不及执行）
- 步骤：等待 TTL 后检查锁状态
- 预期：distributed_locks.expires_at 到期 → 其他 worker 以原子条件 UPDATE 抢占（WHERE expires_at < now），无人工干预；过期行被定期清理。此能力平台已具备（覆盖所有锁），本提案零改动

**UC14-5 正常路径释放不受影响**
- 前置：周期正常完成或异常退出
- 步骤：检查锁释放
- 预期：run_with_distributed_lock 的 finally 释放照常执行（包括 CancelledError 路径）；续锁逻辑不改变正常释放语义

### 场景 S15：异步模型与存储收敛（§6.6/§8.9）

**UC15-1 调度层全异步不阻塞启动与请求**
- 前置：服务启动
- 步骤：观察 lifespan 与 worker 启动
- 预期：`asyncio.create_task` 拉起 worker 即返回（不像 D32 迁移那样 yield 前等待）；worker 循环 await/sleep 全异步；网络下载 httpx.AsyncClient 真异步；锁操作经 asyncio.to_thread 卸载线程池

**UC15-2 执行层阻塞现状披露（pre-existing，未变差）**
- 前置：同步周期执行中
- 步骤：观察事件循环卡顿
- 预期：文件 IO（shutil.copytree/rmtree、open/read、os.listdir）在 async 函数体同步执行，每周期累计 ~1-2 秒事件循环卡顿（当前 2MB 仓库量级）；此为现状行为（原双 worker 相同），串行化后总量不变、频次减半；OBS hash 计算已用 run_in_executor（既有正确实践）；仓库显著增长时文件 IO 迁移 to_thread（观察项，不阻塞本提案）

**UC15-3 存储收敛：无超线性增长**
- 前置：长期运行（数月、仓库多次更新）
- 步骤：审计各存储位体积随时间变化
- 预期：
  - /tmp 临时目录：当周期即删（P45 修复后含父目录），崩溃残留由 >24h 清扫兜底——不累积
  - 本地 skills/ 目录：hash 门控 skip（内容未变不拷）；变更时先删旧目录再 copytree——按内容收敛不叠版本
  - OBS：固定 key 原子覆盖（v2.4 去版本化）+ hash 门控——同名单对象，对象数不随周期增长
  - DB skill/scene 表：固定行 UPDATE——行数 = 官方仓库条目数
  - 唯一增长源 = 官方仓库自身长大（实测 ZIP 1.97MB/解压 4.2MB/最大单技能 1.25MB），任何同步方案等比例跟随，hash 增量只传变化部分

**UC15-4 仓库删除 skill 后本地目录收缩（管理员手工）**
- 前置：P46.4 检测报告 stale skill，管理员确认停用
- 步骤：管理员删除 DB 行并清理本地 skills/ 对应目录
- 预期：本地目录与 DB 一致收缩；stale 检测下轮不再报告（同步链路仍零删除，收缩是管理台动作——铁律 1.0）

### 场景 S16：多平台仓库源（开源就绪，§7.6）

**UC16-1 GitHub 开源地址全链路同步**
- 前置：GRAPESKILLS_REPO_URL 配置为 https://github.com/grape-dev/grape-skills（或 scene-sync.json 同值）
- 步骤：触发同步周期
- 预期：下载走 codeload.github.com（/zip/refs/heads/ 路径），匿名成功；ZIP 顶层目录/内容与 gitcode 一致（66 skill / 13 scene）；场景与 skill 同步全链路正常；stale 检测与铁律行为不变（已 scratch DB 端到端实测）

**UC16-2 gitcode 地址行为不变**
- 前置：不配置 repo_url（走代码兜底默认值 gitcode）
- 步骤：触发同步周期
- 预期：下载仍走 raw.gitcode.com（/archive/ 路径），行为与合入前完全一致——开源支持是加法不是替换

**UC16-3 未知 host 回退**
- 前置：repo_url 配置为未识别的 host
- 步骤：触发同步周期
- 预期：回退 gitcode 语义（历史行为），不因配置错误崩溃；下载失败走网络重试 → 降级链

**UC16-4 恶意/错误 URL 防回归**
- 前置：代码曾因硬编码字符串手术产生 raw.gitcode.com/https://github.com/... 垃圾 URL（BadZipFile）
- 步骤：单元测试断言 _parse_repo_host_path 对 github/gitcode/未知 host 三类输入的解析正确性
- 预期：未来新增 host 必须经 _parse_repo_host_path，禁止字符串替换式解析（测试防回归）

**UC16-5 token 与下载源匹配（第六复审 P0 修复）**
- 前置：GitHub repo_url + 生产标配 GRAPE_DEV_TOKEN（GitCode PAT）——GitHub codeload 对任何 Authorization Bearer header 一律返回 404（实测：匿名 200 / 带 PAT 404）
- 步骤：配置 GitHub 源且有 token 的环境触发同步周期
- 预期：GitHub 下载**始终匿名**（headers={} 即使 token 存在），下载 200 成功、场景/技能同步正常；token 只附加到 gitcode 端点（私仓认证行为不变）——已端到端实测（token + GitHub URL → 200，13 场景 + 66 skill 完整）；两个回归测试锁定 per-host header 行为

**UC16-6 测试环境解耦（第七复审 P1 适配）**
- 前置：测试宿主机配置了 GitHub 源（环境变量或本地 scene-sync.json）——原 test_auth_header 未固定下载源，走全局 _DL_BASE 解析
- 步骤：GRAPESKILLS_REPO_URL=github... 环境下跑 test_scene_sync.py 全量
- 预期：test_auth_header 显式 patch _DL_BASE 为 gitcode 后通过（token 行为本属 gitcode 语义，测试与宿主环境解耦）；GitHub 匿名行为由 test_github_download_sends_no_auth_header 独立覆盖——两个测试各司其职，任何环境组合下全绿（已注入 GitHub 环境变量实测 84/84）

---

## 四、改动文件

| 文件 | 改动内容 |
|------|---------|
| src/storage/skill_sync.py | sync_official_skills 完成后串行调用 sync_official_scenes，复用已下载 ZIP；whole-body try/finally 清理（含父目录）；skill stale 只读检测（P46.4，铁律 1.0：无删除/停用语义） |
| src/storage/scene_sync.py | sync_official_scenes 接收 repo_root 参数，可选跳过下载；修复 tmp_dir 泄漏（P45，清理形式统一为 os.path.dirname）；scene stale 只读检测 + 预检口径统一 + 早退兜底（P46，铁律 1.0） |
| src/api/main.py | 移除独立的 start_scene_sync_scheduler 调用；standalone worker 及死配置按已决策方案 A 一并删除（S5） |
| src/storage/scene_sync.py | （S5 决策 A）删除 _scene_sync_worker / start/stop_scene_sync_scheduler / SCENE_SYNC_INTERVAL；保留 sync_official_scenes(repo_root=None) 独立调用能力 |
| src/storage/upload_cleanup.py | （P45 伴生）增加 /tmp scene_sync_zip_* 泄漏模式兜底清扫（>24h），设计见设计文档 §12.4 |
| src/storage/skill_sync.py, scene_sync.py | 网络类异常（下载/OBS）轮内重试：3 次指数退避 5/15/45s，仅网络类错误重试、4xx 快速失败、DB 不轮内重试；连续 3 轮失败升级 ERROR 告警（设计文档 §8.7） |
| src/storage/skill_sync.py, scene_sync.py, src/runtime/task_lock.py | 锁自动续期：外层 skill_sync 锁场景步骤入口 + 长循环内 renew_lock（沿用 skill_sync.py:280 模式），锁被抢即中止；周期 >TTL/2 记 ERROR；到期失效能力已具备零改动（设计文档 §6.3、§8.8） |

---

## 五、设计文档

详细设计见同目录下 `r31-sync-merge-design.md`（§12 为 P46 实施设计：stale 只读检测含完成守卫、口径统一、早退兜底、/tmp 清扫及测试计划；铁律 1.0：同步链路零删除/停用写操作）。

---

## 六、P46：深度审计新增问题（2026-10-22 实库核验）

### 6.1 P46.1 场景市场无删除机制（中，实库已复现；修复策略：检测+报告，清理权在管理台）

scene_sync 只做 INSERT/UPDATE（content_hash 不变则 skip），全代码库没有任何 `DELETE FROM scene_market WHERE source='official'`。仓库删除场景后 DB 行永久残留并继续展示。

实库证据（MySQL grape 库）：仓库现有 13 个场景，DB 有 15 个 official 场景——`api-skill-dev`、`skill-dev` 已从仓库删除但仍 is_active=1，并拖 10 条孤儿 scene_skill_ref。skill 侧无此问题（66/66 全量一致）。

**修复方向（第三版，2026-10-22 评审修正）**：同步链路**不做任何自动删除/停用**。同步周期末尾以仓库场景集为准做**只读检测**：DB 中 source='official' 但仓库已不存在的行 → 记 warning 日志 + 汇总 stats（`stale_official=N`），供管理员发现；**下架/清理由管理台管理员手工执行**（admin 场景列表或 SQL）。理由：同步任务拥有删除语义会把瞬时故障放大为展示层事故（部分遍历 → 误判 stale → 误下架），即使加完成守卫也只是降低概率而非消除路径；只读检测的最坏情况是"报告不准"，零危害。残留可见性由检测日志保证，不会重新退化为"无人知晓的暗数据"。验收见 UC9-1（检测报告）/UC9-4（检测的完整性前提，仅影响报告准确性）。

**完成守卫（保留，仅护报告准确性）**：scene 遍历是逐目录 try/except 的，中途异常时"仓库场景集"不完整——此时跳过本轮检测（避免报告里出现误报的 stale 名单），下轮补上。守卫失败的最坏后果从"误下架全市场"降级为"本轮没有 stale 报告"。

### 6.2 P46.2 预检与 ref 重建口径不一致（低，模式缺陷未触发）

`_check_skills_exist`（L160）查 skill 表不过滤 scope/is_active——user-scope 技能也算"存在"；`_rebuild_scene_skill_ref`（L247-257）要求 is_active=1 且 scope ∈ {official, repo}。组合后果：预检通过但 skill_id=0 写入（当前实库 0 行，属未触发隐患）。

**修复方向**：两处统一为同一查询口径（is_active + scope 白名单）。

### 6.3 P46.3 scenes/ 早退路径绕过兜底（低，pre-existing）

下载成功但 scenes/ 目录缺失时 L324 直接 return，`_init_featured_scenes_if_empty()` 不执行——新装实例得到全空白市场。

**修复方向**：return 前调用兜底初始化（与下载失败路径行为一致）。

### 6.4 P46.4 skill 侧同源对账缺失（中，机制级，第三复审发现；修复策略同 6.1：检测+报告）

`_sync_skills_to_db(data)` 只遍历 skill-description.json 条目做 INSERT/UPDATE，无任何 DELETE/停用逻辑——skill 从仓库 json 移除后，skill 表 official 行永久残留（与 P46.1 场景侧完全同源；当前实库 66/66 恰好一致故未暴露，但机制缺陷相同）。连带危害：残留 skill 仍 is_active=1，会继续通过 `_check_skills_exist` 预检和 `_rebuild_scene_skill_ref` 的 official 优先查询，让场景解析到已废弃技能。

**修复方向（第三版，与 6.1 同策略）**：同步侧**只读检测**——DB 中 scope='official' 但 json 已不存在的 skill → 记 warning + stats（`stale_skill=N`）；**停用由管理员手工执行**。json 解析失败/写入中断时跳过本轮检测（避免误报名单）。验收见 UC11-1/11-2/11-3。

### 6.5 审计排除项（避免后续误报）

- `datetime('now')` MySQL 不兼容——mysql_compat.py L289 已翻译为 CAST(NOW() AS CHAR)，实库同步正常，排除。
- zip-slip 路径穿越——CPython zipfile extractall 自带 curdir/pardir 过滤，来源为官方仓库；用户上传路径另有大小上限，排除。
- 同 repo 重复下载家族——全仓只有 scene_sync._download_repo_zip 一个下载点，排除。
- OBS 同步临时文件——打包在内存 BytesIO，无临时目录，排除。
- INSERT OR IGNORE 静默丢错——scene_market 主 INSERT 为普通 INSERT；scene_skill_ref 丢行无害，排除。

### 6.6 观察项（暂不立项）

- 锁 TTL 硬编码 1800 与可配置 interval 脱钩：配更小 interval 时实际频率被锁钳制且无告警（当前部署用默认 1800，无影响）。
- icon 本地兜底写入源码树 `src/api/static/img/scenes`（代码目录污染、多实例不共享），仅 storage 未配置时触发。
- rmtree 在 async 上下文同步执行会短暂阻塞事件循环（~2MB 量级可接受）；仓库显著长大后应换 `asyncio.to_thread`，实施时在代码注释里显式声明此取舍。

## 七、场景用例 → 实现与测试映射（2026-10-22 实施后补）

| 场景/用例 | 实现位置 | 自动化测试 | 部署后手工验收 |
|---|---|---|---|
| S1/UC1-1,1-2 首启有数据、单次下载 | skill_sync 串行调用 + repo_root 复用 | TestMergedPath::test_repo_root_mode_skips_download | 部署测试环境观察首个周期日志 |
| S2/UC2-1 新 skill+场景同周期 | 串行顺序保证（先 DB 后 scene） | 既有 test_full_sync_new_scene | 推送变更观察下周期 |
| S3/UC3-1 skill 下载失败降级 | repo_root=None 回退 standalone 下载 | test_zip_download_failure_skips（既有） | — |
| S4/UC4-1 scene 失败隔离 | 步骤 4 独立 try/except + whole-body finally | TestTempDirCleanup | — |
| S5/UC5-1,5-2 worker 删除 | 决策 A 删除 + 兼容提示 | TestStandaloneWorkerRemoval、TestTempDirCleanup::test_provided_repo_root_not_cleaned_by_scene | grep 复核（测试已断言） |
| S6/UC6-1 多 worker 单执行 | run_with_distributed_lock(skill_sync) 既有 | — | --workers 2 部署验证 |
| S7/UC7-1,7-2 清理零泄漏 | P45 + dirname 统一 + upload_cleanup 清扫 | TestTempDirCleanup::test_standalone_cleanup_removes_parent | 连续 3 周期查 /tmp |
| S8/UC8-1 优雅关闭 | stop_skill_sync_scheduler 取消内含 scene | — | shutdown 观察 |
| S9/UC9-1,9-4 stale 检测+守卫 | _detect_stale_official_scenes | TestDetectStaleOfficialScenes（3 用例，含零写断言） | 日志观察 stale_official 计数 |
| S9/UC9-2 早退兜底 | _init_featured_scenes_if_empty() | test_no_scenes_dir_skips（既有）+ 清理测试路径 | — |
| S9/UC9-3 口径统一 | _query_active_skill_names | TestPrecheckScopeUnification | — |
| S11/UC11-1,11-2 skill stale | _detect_stale_official_skills | TestDetectStaleOfficialSkills | 日志观察 stale_skill |
| S11/UC11-3 停用后 ref 一致 | 重建查询既有 is_active 过滤 | — | 管理员停用后观察 |
| S12/UC12-1,2,3 网络重试 | _download_repo_zip_with_retry | TestNetworkRetry（3 用例） | — |
| S12/UC12-4 连续失败升级 | worker _consecutive_failures 计数 | —（需跨周期，部署验收） | 连续 3 轮失败观察 ERROR |
| S13/UC13-1~4 锁试探避让 | 步骤 4 内非阻塞 acquire scene_sync | —（需双实例，部署验收） | 滚动部署窗口观察 |
| S14/UC14-1~5 锁续期/到期/告警 | 步骤 4 renew_lock（to_thread）+ >900s ERROR + 既有 expires_at | TestLockStepCodeQuality（to_thread 一致性 + 死变量回归防护，第五复审补） | 长周期场景观察 |
| S15/UC15-1~4 异步与收敛 | 调度结构 + 既有收敛机制 | — | 各存储位审计 |
| S16/UC16-1~6 多平台仓库源 | _parse_repo_host_path + 双路径下载 + per-host token 匹配 | TestMultiPlatformRepoSource（7 用例：解析 4 + header 行为 2 + URL 形态 1） | GitHub 全链路（已实测，含 token 组合） |
| S10/UC10-1,10-2 测试回归 | — | 84 用例全绿（实测） | — |

自动化覆盖：36/42 用例（其余为时长类/多实例类/部署级验收项，需测试环境执行）。

### 6.7 实施配套要求（第二/三复审 2026-10-22 补；第四复审同步文档残留）

- 测试迁移评估：test_scene_sync.py 38 用例 / 6 处直接调用需验证默认参数兼容；skill 侧测试需 mock 或短路串行 scene 调用（UC10-1/10-2）。**已落地**：2 个 _load_config 用例迁移，72 用例全绿（38 scene + 21 skill OBS + 13 新增）。
- standalone worker 去留：**已决策（2026-10-22，方案 A）并已实施**——skill 与场景定时任务合一，standalone worker 及死配置已删除（UC5-1 测试 grep 断言零残留）；设计文档 §4.4/§7.1/§7.2/§7.3/§9 影响表/§10.3 验证清单/§8.5（标注 superseded）均已同步决策 A，无残留矛盾。
- 设计文档旧伪代码 supersede 标注：§3.1/§3.3 的窄 try/finally 与裸 step-5 清理已被 §3.5 取代、§4.2 的 rmtree(tmp_dir) 已被 §4.5 取代——实现已按 §3.5/§4.5 落地。
- 止损优先级：实现已合入本分支（feat 提交），待部署测试环境后线上泄漏与 stale 展示即消除；MR 描述应点明。
- 合入流程（CONTRIBUTING §5/§6）：实施前建关联 issue（R31/P45/P46 摘入），MR 按模板带关联 Issue/改动内容/测试情况。**待办**：issue 与 MR 尚未创建。

### 6.8 第五复审（2026-10-22，实现审计）修复记录

- scene_step_done 死变量（复审 4.1）：已删除。
- renew_lock 未包 to_thread（复审 4.2）：已改为 `await asyncio.to_thread(_rn, "skill_sync", 1800)`，与同步骤 acquire/release 风格统一；新增 TestLockStepCodeQuality 双测试防回归（to_thread 一致性断言 + AST 死变量扫描）。
- 复审 4.3（_rebuild 口径"不彻底"）：维持现状——行为等价，设计文档 §12.2 明确保留 official-first 优先查询、仅统一存在性语义。
- 复审 4.4（python3.9 不支持 str | None）：基线已在用 PEP 604 语法，项目运行时为 3.14，非本分支引入，不处理。
- 复审 4.5（test_defaults 失败）：其快照环境的本地 scene-sync.json 问题，当前分支实测通过（无本地配置文件时走 gitcode 默认值），非分支缺陷。
