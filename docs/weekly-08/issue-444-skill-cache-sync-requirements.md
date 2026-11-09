# 同名技能上传更新后跨进程缓存不一致（需重启生效）修复

> **Issue**：[#444](https://gitcode.com/grape-dev/grape/issues/444)
> **分支**：`feat/issue-444-skill-cache-sync`（基于 weekly-08 head 595cde23f，已 rebase 同步）
> **设计文档**：`issue-444-skill-cache-sync-design.md`（同目录）
> **状态**：已实施（P47-P49 代码 + 测试 82/82 通过，见 §六测试映射）
> **类型**：Bug 修复

## 一、总览

### 1.1 背景

技能详情读取链路 `skill_md_content()`（`src/storage/__init__.py`）走两级缓存：

1. **内存 LRU 缓存** `_mem_cache`（`skill_name -> (content, size, content_hash)`）——模块级 dict，**进程私有**；
2. **磁盘 zip 缓存**——共享文件，`zip_cache_put` 用 `os.replace` 原子覆盖同 key，此层无问题。

上传接口（`src/api/routes/skills.py` `upload_skill`，~L726-744）末尾调用了 `mem_cache_invalidate(skill_name)` + `zip_cache_put(object_key, zip_bytes)`，但只能清掉**接收到上传请求的那个进程**的内存缓存。多进程部署（双主：`uvicorn :3000` main 与 `:8080` webhook，或双机双主）下，另一进程的旧 SKILL.md 内容原地不动，直到重启清空 `_mem_cache` 才生效。

Issue #254 引入的 content_hash 读时校验（内存命中后与 DB 比对 hash，不一致则失效回源）理论上可跨进程纠偏，但该防线在 user/repo 技能上传链路**四处同时失效**（实测核验，见 §1.4）。

### 1.2 需求范围

| 编号 | 需求 | 类型 |
|------|------|------|
| P47 | 上传**与转换**链路写入 content_hash：`upsert_skill_obs` 的 UPDATE/INSERT 补写 content_hash 列；`zip_cache_put` 传入该 hash（覆盖 upload_skill 与 convert_skill 两条写路径——convert 现状同病：`zip_cache_put(dst_key, src_zip)` 无 hash、`upsert_skill_obs` 无 hash，转换产生的技能同样需重启才生效）——使另一进程读时校验可感知变更并自动回源 | Bug 修复 |
| P48 | `_verify_skill_cache_hash` 校验查询带正确 scope（`get_skill_content_hash` 默认 `scope='official'`，user/repo 技能查不到行，校验恒降级通过） | Bug 修复 |
| P49 | 内存缓存 key 去同名串键：`_mem_cache` 仅按 `skill_name` 索引，user/repo/official 同名技能互相污染缓存；key 改 object_key（或 scope+name）口径后，upload 与 convert 两处的 `mem_cache_invalidate(skill_name)` 调用需同步改为新口径，否则转换后失效不生效 | Bug 修复（叠加发现，随本单一并修复） |

### 1.3 不在范围

- **不引入**跨进程缓存广播 / Redis / 消息总线等主动失效机制——现有"读时校验"机制修通即可，避免为修 bug 新增运维面与故障面
- 磁盘 zip 缓存层（已正确：同 key 原子覆盖）
- worker 投递路径 `get_skill_zip_bytes`（走磁盘缓存，能拿到新包，不受影响）
- 官方技能同步链路 `skill_sync.py`（其 hash 写入与校验已正确工作，仅保证不回归）
- 删除/转换技能的缓存失效链路（`invalidate_skill_caches` 已有，仅保证不回归）

### 1.4 根因实测核验（基于 weekly-08 head 910b38baf）

四处失效点（读时校验防线形同虚设）：

1. `upsert_skill_obs()`（`src/user/db.py:1243`）的 UPDATE/INSERT **不写 content_hash 列**——DB 里 user/repo 技能的 hash 永远为空/旧值。`update_skill_content_hash` 仅被官方技能同步（`src/storage/skill_sync.py:198`）调用，上传路径无人调用。
2. 上传时 `zip_cache_put(object_key, zip_bytes)`（skills.py:743）未传 hash 参数 → 缓存条目 hash 为空串。
3. `_verify_skill_cache_hash()`（`src/storage/__init__.py:471`）对空 hash 直接 `return True`（向后兼容降级），DB 侧 db_hash 为空也 `return True`——**两层空对空，永远判定"一致"**。
4. `get_skill_content_hash(skill_name)` 默认 `scope='official'`（`src/user/db.py:1126`）——即使写入了 hash，user/repo 技能的校验查询也查不到行，照样降级返回 True。

叠加问题：`_mem_cache` 仅按 `skill_name` 索引（无 scope 维度），user/repo/official 同名技能互相串键。

---

## 二、修复方案（最小改动、零破坏）

### 2.1 上传链路（P47）

上传时计算规范化 zip 的 content hash（复用 `_compute_skill_hash` 口径或按 zip 字节 sha256，实施时定一种并与读侧一致），并贯通三处：

- `upsert_skill_obs(...)` 增加 `content_hash` 参数，UPDATE/INSERT 语句补写 `content_hash` 列；
- `zip_cache_put(object_key, zip_bytes, content_hash)`；
- `mem_cache_put` 的写入路径在回源后已带 hash（`skill_md_content` 回源分支），无需改。

效果：另一进程内存/磁盘缓存命中后，`_verify_skill_cache_hash` 比对 DB 新 hash 不一致 → 自动失效回源 OBS → 返回新内容。**无需重启。**

### 2.2 校验查询带 scope（P48）

`_verify_skill_cache_hash` 需要按正确的 (skill_name, scope) 查 DB。scope 可从 `parse_skill_key(object_key)` 解析（读侧 `skill_md_content` 已持有 object_key）。`get_skill_content_hash` 保持函数签名兼容，读侧调用时显式传 scope。

### 2.3 内存缓存 key 去 scope 串键（P49）

`_mem_cache` 的 key 由 `skill_name` 改为 `object_key`（或 `scope+name` 复合键），读写两侧同步；`mem_cache_invalidate` 上传路径按同口径失效。磁盘 zip 缓存本身已按 object_key 索引，无此问题。

### 2.4 性能不退化约束

hash 一致时必须仍走内存命中快路径（不退化为每次回源 OBS/每次查 DB 的降级读）；DB hash 查询仅在缓存命中时发生一次（现状即如此，保持）。

### 2.5 兼容与降级语义（保持不变）

- 缓存无 hash（存量旧数据）→ 不校验、返回 True（向后兼容）；
- DB 查询失败/技能不存在 → 不校验、返回 True（降级不阻塞读）；
- DB 记录 hash 为空（official 未同步、旧记录）→ 不校验、返回 True。

即：修复只让"有 hash 可比对"的场景真正比对，不改变任何降级行为，零新增故障面。

---

## 三、场景与场景用例

以下 11 个场景（Scenario）描述触发条件与环境，场景用例（Use Case）给出可执行的验证步骤与预期结果，供实施后逐条验收。S1/S2/S5 为核心修复场景，S3/S4 为兼容与回归场景，S6 为家族扫描记录项的观察口径（不修，仅确认自愈边界），S7 为存储模式 × 部署形态矩阵与任务执行链路（worker 端）的全链路核验口径，S8 为 backlog 项 F1-F3 的边界行为确认，S9 为 skill 域"读时门 + 读时重签"既有正确设计的防回归守卫，S10 为并发与 worker 上报链路的第五轮核验口径，S11 为 direct 端技能物化的第六轮核验口径。

### 场景 S1：双进程部署，同名技能覆盖更新（核心修复场景）

环境：同机双进程（`uvicorn :3000` main + `:8080` webhook）或双机双主；OBS 存储已启用；用户 U 已登录。

**UC1-1 另一进程不重启即返回新内容（修复主目标）**
- 步骤：经进程 A `POST /api/skills/upload` 上传 `my-skill` v1（scope=user）→ 经 A 和 B 各读取一次详情（两侧 `_mem_cache` 均有 v1）→ 再经 A 上传同名 `my-skill` v2 → 经 B 读取详情。
- 预期：B 返回 v2 内容（hash 校验不一致 → 失效回源），且 B 进程未重启。

**UC1-2 上传进程自身缓存即时失效（现状保持）**
- 步骤：UC1-1 之后立即经 A 读取详情。
- 预期：A 返回 v2（`mem_cache_invalidate` 直接失效路径，不依赖 hash 校验）。

**UC1-3 仓库技能（scope=repo）同样修复**
- 步骤：以 scope=repo、repo=`<owner>/<repo>` 重复 UC1-1。
- 预期：同 UC1-1（P47/P48 覆盖 user 与 repo 两类 scope）。

**UC1-4 hash 一致时快路径不退化**
- 步骤：UC1-1 之后再次经 B 读取详情（v2 已缓存，DB hash 一致）→ 观察日志/埋点回源次数。
- 预期：返回 v2 且不触发 OBS 回源（内存命中 + hash 校验通过，性能与修复前一致）。

### 场景 S2：同名不同 scope 技能互不串缓存（P49）

环境：同 S1；存在同名技能：official `my-skill`（官方同步入库）与 user `my-skill`（用户 U 上传）。

**UC2-1 同名 user/official 不互相污染**
- 步骤：读取 official `my-skill` 详情 → 读取 U 的 user `my-skill` 详情 → 再读 official `my-skill`。
- 预期：三次各自返回正确 scope 的内容，内存缓存按 object_key（或 scope+name）隔离，无串键。

**UC2-2 同名 user/repo 不互相污染**
- 步骤：同名技能分别上传 scope=user 与 scope=repo → 交替读取两者详情。
- 预期：内容各自正确，互不覆盖缓存。

### 场景 S3：存量与降级兼容（零回归）

环境：升级部署后，磁盘 zip 缓存与 DB 中存在无 hash 的旧数据（修复前写入）。

**UC3-1 存量无 hash 缓存仍可读（向后兼容）**
- 步骤：直接读取一个修复前已缓存的技能详情（缓存条目无 hash、DB 记录无 hash）。
- 预期：正常返回内容（不校验、不报错、不强制回源）。

**UC3-2 DB 查询异常降级不阻塞读**
- 步骤：模拟 `get_skill_content_hash` 抛异常（如 DB 锁）→ 读取已缓存技能详情。
- 预期：返回缓存内容并记录 warning，请求不失败。

### 场景 S4：既有链路回归（保证不破坏）

**UC4-1 官方技能同步不回归**
- 步骤：触发一次官方 skill 同步（`sync_official_skills`）→ 读取被更新技能的详情。
- 预期：官方同步的 hash 写入/校验行为与现状一致。

**UC4-2 删除技能缓存失效不回归**
- 步骤：删除一个 user 技能 → 读取该技能详情。
- 预期：`invalidate_skill_caches` 链路正常，详情返回 404，缓存（含按新 key 口径的内存缓存）被清除。

**UC4-3 worker 投递拿到新包**
- 步骤：UC1-1 之后，经 worker 投递链路获取该技能 zip（`get_skill_zip_bytes`）。
- 预期：拿到 v2 zip（磁盘缓存同 key 覆盖，现状已正确，验证不回归）。

### 场景 S5：转换（convert）链路跨进程缓存一致（P47/P49 覆盖，核心修复场景）

环境：同 S1；用户 U 拥有 user 技能 `my-skill` v1（已通过 A、B 两进程各读取过一次详情）；目标仓库 `<owner>/<repo>` 由 U 托管。修复前 convert 链路与上传同病：`zip_cache_put(dst_key, src_zip)` 无 hash、`upsert_skill_obs` 不写 hash，转换出的技能覆盖更新后另一进程同样读旧内容。

**UC5-1 转换产物在另一进程可正确读取**
- 步骤：经进程 A 调用 `POST /api/skills/convert`（user → repo）→ 经进程 B 读取目标 repo 技能详情。
- 预期：B 读到转换复制后的内容（convert 产物 DB 记录含 content_hash；若缓存 hash 为空/不一致则回源，不依赖重启）。

**UC5-2 转换产物的磁盘缓存带 hash**
- 步骤：UC5-1 之后检查磁盘 zip 缓存 index 中 dst_key 条目。
- 预期：`hash` 字段非空且与 DB `content_hash` 一致（P47 覆盖 convert 的 `zip_cache_put(dst_key, src_zip, hash)`）。

**UC5-3 转换产物被覆盖更新后另一进程免重启**
- 步骤：UC5-1 之后经 A 对目标 repo 技能做同名覆盖上传 v2 → 经 B 读取详情。
- 预期：B 返回 v2（与 UC1-1 同口径，验证 convert 产物与直传技能行为完全一致）。

**UC5-4 转换后源/目标内存缓存按新 key 口径失效**
- 步骤：UC5-1 之后经 A 读取源 user 技能与目标 repo 技能详情。
- 预期：convert 内的 `mem_cache_invalidate` 已改为 object_key（或 scope+name）口径，源 scope 内容不受污染、目标 scope 不残留旧值（P49 连带点）。

### 场景 S6：家族扫描记录项的观察口径（不修，仅确认自愈边界）

以下用例不改代码，用于实施验收时确认"记录不修"项的实际陈旧窗口与扫描结论一致，防止误判为未修复的 #444 同类问题。

**UC6-1 OAuth provider 配置跨进程 5 分钟内自愈**
- 步骤：admin 修改 OAuth provider 配置 → 立即经另一进程读取登录页 provider 列表 → 等待 >300s 再读。
- 预期：立即读取为旧配置（TTL 内，预期行为）；>300s 后自动新配置（`_PROVIDERS_CACHE` TTL 自愈，非 #444 同病）。

**UC6-2 用户禁用 5 秒内跨进程生效**
- 步骤：admin 禁用用户 U → 立即经另一进程以 U 的 token 发请求。
- 预期：最长 5 秒后 401（`_user_status_cache` TTL=5s，`invalidate_user_cache` 只清当前进程、TTL 兜底）。

**UC6-3 子技能端点对 user/repo 技能的已知 404 行为**
- 步骤：读取一个 user 技能（OBS 存储、含 references/ 子技能）的 `GET /{skill_id}/sub` 与 `GET /package-sub/{pkg_id}/{sub_name}`。
- 预期：返回 404（已知功能缺口，§4.4，建议另立 issue；本单不修，验收时确认行为未因 P47-P49 改动而变化）。

### 场景 S7：存储模式 × 部署形态矩阵 + 任务执行链路（全链路核验口径）

环境：矩阵维度——存储模式（OBS / local（`SKILL_STORAGE=local|auto` 无 OBS）/ storage=None obs-only 未配置）× 部署形态（单机双进程 / 集群双机）。任务执行链路（worker 物化）单独核验。以下用例不改代码（除 UC7-3 复用 P47-P49 修复），用于验收时确认各形态行为边界与核验结论一致。

**UC7-1 OBS + 单机双进程（S1 同机形态，P47-P49 修复适用）**
- 步骤：S1 全部用例在单机双进程（:3000/:8080）部署下执行。
- 预期：同 S1。磁盘缓存同机共享 + DB hash 校验跨进程生效。

**UC7-2 OBS + 集群双机（S1 跨机形态，P47-P49 修复适用）**
- 步骤：S1 全部用例在双机部署（A 机上传、B 机读详情）下执行。
- 预期：同 S1。B 机磁盘缓存 miss → 回源 OBS 拿新包；内存缓存命中后 hash 校验（P48 带 scope）失效回源。修复前 B 机需重启才拿到新内容——本用例即 Issue #444 原始报障形态。

**UC7-3 local + 单机双进程（P47-P49 修复同样适用）**
- 步骤：`SKILL_STORAGE=local`（OBS 未配置）单机双进程，重复 UC1-1。
- 预期：另一进程不重启读到新内容。local 后端权威存储落共享磁盘（`SKILL_STORAGE_DIR`，tmp+replace 原子覆盖），zip_cache/DB hash 机制与后端无关，P47-P49 修复对 local 模式同样生效。

**UC7-4 local + 集群双机（已知不支持形态，记录边界）**
- 步骤：`SKILL_STORAGE=local` 双机部署，A 机上传 → B 机读详情 / B 机调任务 skill-zip 代拉。
- 预期（已知边界，非本单修复）：LocalStorage 明确"仅单机语义，集群应 `SKILL_STORAGE=obs` 或共享盘"（local.py docstring）。B 机行为：详情 `content` **静默返回空串**（`skill_md_content` 回源 miss → None，无告警）；任务 skill-zip 代拉 502；convert 跨机 copy 失败。其中"详情静默空串"建议另立 issue 改为显式错误（503+指引），本单不改。
- 观察：storage=None（obs-only 未配置 OBS）时上传/详情/转换均显式 503，边界清晰——local+集群是唯一静默失败形态。

**UC7-5 任务执行链路 claim 现签（核验无陈旧，防回归）**
- 步骤：OBS 模式下创建引用技能 `my-skill` v1 的任务（不立即执行）→ 同名覆盖上传 v2 → worker claim 并执行该任务 → 检查工位 `.skills/my-skill/SKILL.md` 内容。
- 预期：为 v2。`skill_obs_url` 在 worker **claim 时现签**（runtime.py:468，TTL 默认 1800s，非入队时快照）→ worker 直连 OBS 现拉最新对象。此设计是任务链路无陈旧的根因，**后续改动严禁把 URL 签名提前到入队时刻**（否则引入与 #444 同族的陈旧问题）。

**UC7-6 队列中任务的 skill_files 快照语义（语义记录）**
- 步骤：UC7-5 场景中若 worker 直连 OBS 与服务端代拉均失败（①②双降级）→ 任务回退 ③ `skill_files` 文本物化。
- 预期：③ 兜底内容为**任务入队时刻**打包的快照（可能为 v1）——快照语义可辩护（入队时版本），且仅在 ①② 双失败时触达。记录语义，不改。

### 场景 S8：backlog 项 F1-F3 的边界行为确认（不修，验收时防误判）

以下用例不改代码，验收时确认 backlog 项的实际行为与记录一致，避免误判为 P47-P49 修复不完整。

**UC8-1（F1）local + 集群详情静默空串**
- 步骤：`SKILL_STORAGE=local` 双机部署，A 机上传 `my-skill` → B 机读详情。
- 预期：`content` 为空串、HTTP 200（已知静默失败，F1 另立 issue 改显式 503；本单 P47-P49 不改变此行为）。

**UC8-2（F2）子技能端点 OBS 技能 404**
- 步骤：同 UC6-3（并入 F2 口径统一确认）。

**UC8-3（F3）多 owner 同名技能 hash 查询无串扰**
- 步骤：两个不同用户各上传同名 user 技能 `my-skill`（内容不同）→ 经另一进程分别读两者详情。
- 预期：各自返回正确内容。此用例同时是 P49（object_key 口径）+ F3（`get_skill_content_hash` 查询收紧）的联合验收——若实施采用按 id/object_key 查询则天然无歧义；若仍按 (name, scope) 查询，本用例暴露 LIMIT 1 不确定行问题，必须收紧。

### 场景 S9：skill 域"读时门 + 读时重签"设计确认（防回归守卫）

环境：同 S1。以下用例守的是 P47-P49 实施中最容易顺手改坏的两处既有正确设计，与 #444 修复路线同构。

**UC9-1 删除后另一进程立即 404（DB 行是访问门）**
- 步骤：经进程 A 读取 user 技能 `my-skill` 详情（B 进程内存缓存已有内容）→ 经 A 删除该技能 → 立即经进程 B 读取详情。
- 预期：B 返回 404。删除先删 DB 行，B 的详情端点读缓存前先查 DB（行不存在 → 404），缓存残留不可达——**实施时不得把 DB 行查询挪到缓存读取之后**，否则删除会退化为"另一进程仍返回旧内容"的 #444 同族问题（与 UC4-2 删除回归互为补充：UC4-2 验缓存失效，本用例验访问门顺序）。

**UC9-2 详情页 zip_url 每次动态签发（读时重签）**
- 步骤：读取技能详情两次，间隔 >300s（超过签名 TTL）→ 对比两次返回的 `zip_url`。
- 预期：每次均为新签发的有效 URL（详情端点读取时 `storage.temporary_url` 现签，DB/缓存不持久化签名 URL）。

### 场景 S10：并发与 worker 上报链路（第五轮核验口径）

环境：同 S1，OBS 模式为主。以下用例不改代码，验收时确认并发边界与 worker 技能上报链路与核验结论一致。

**UC10-1 并发上传同名不服务旧内容**
- 步骤：模拟两个客户端并发上传同名技能 `my-skill`（A 发 v1、B 发 v2，请求交错）→ 等两者都完成后经另一进程读取详情 → 与 OBS 对象实际内容比对。
- 预期：详情内容与 OBS 上的实际对象一致（后写者胜）；若 DB hash 与 OBS 内容因交错而错位，读侧 hash 校验触发一次额外回源后自愈——**任何交错组合都不会读到既非 v1 也非 v2 之外的旧缓存内容**（修复前此场景另一进程可能长期吐先入缓存者）。

**UC10-2 worker 引擎技能列表跨实例一致**
- 步骤：worker W1 本机技能目录新增一个 skill → 触发 `POST /runtimes/{rid}/refresh-skills`（或等 W1 下次 claim 空闲上报）→ 经服务端另一进程读取引擎详情/引擎列表。
- 预期：两进程均返回更新后的 local_skills 列表（DB 直查无缓存）；W1 忙碌执行任务时 scan 指令延迟到下次空闲 claim（有界延迟，预期行为）。

**UC10-3 场景引用技能预检一致**
- 步骤：删除一个被场景引用的 user 技能 → 触发一次场景同步 → 查看场景引用预检/stale 检测日志。
- 预期：skill 表 DB 直查保证预检跨进程一致；场景侧 stale 表现归 R31/P46 既有设计（检测+报告，零 DB 写），本单不改。

**UC12-7 物化链路读时校验（第九轮收尾，P-A）**
- 背景：get_skill_zip_bytes（任务物化三入口 runtime/service.py:780、event_pipeline.py:3218、runtime.py:609 取包）原为"磁盘缓存命中即返回"零校验——与详情页同族的 #444 病，双主部署下节点 2 的任务物化仍解压旧 zip。
- 步骤：B 进程 get_skill_zip_bytes 读 v1 填缓存 → A 进程覆盖上传 v2 → B 不重启再取。
- 预期：返回 v2 字节（套用同款 _verify_skill_cache_hash：sid 锚点/旧格式降级/DB 异常降级，与详情页完全一致）；回源后缓存条目写 (hash, sid)，后续命中走锚点快路径；降级语义零回归（无 hash 条目/DB 异常 → 不校验直接返回）。

**UC12-8 upload 409 存储回滚（第九轮收尾，P-B）**
- 背景：upload 写序 save_bytes → upsert，并发同名 409 时存储已是新字节而 DB hash 停留旧值（hash 指纹失真，一次性回源后收敛）。
- 步骤：owner-a 已上传 → owner-b 并发上传同 key，upsert 失败 409。
- 预期：save 前留存的旧字节被恢复（存储与 DB 行回到一致）；DB 行（owner-a 的行与 hash）未动。对齐 convert 409 分支既有回滚语义。

**UC12-9 upload 409 存储回滚（路由级，第十轮 P2-2）**
- 背景：第九轮 UC12-8 用例为路由逻辑手工复刻，路由函数演进时测试不报警；本轮改为按路由 storage 分支逐行同序驱动（skills.py:746-764：read 旧 → save 新 → upsert 失败 → 恢复旧），断言存储恢复旧字节、DB 行未动。
- 预期：409 后存储对象 = 旧字节；owner-a 行与 hash 完好。

**UC12-10 convert 路由核心序列（路由级，第十轮 P2-2）**
- 背景：test_t4 系列此前只测 helper，本轮按 convert 路由 skills.py:927-940 实际顺序驱动：copy_object → 源缓存读 hash → 目标行写入 → dst 缓存锚定新行 id。
- 预期：另一进程读 convert 产物命中正确内容；源被覆盖后产物不随源自变化（复制语义，行隔离）。

**UC12-11 upload 路由核心序列（路由级，第十轮 P2-2）**
- 步骤：按上传路由 skills.py:746-764 逐行同序：新格式键（provider 段）→ save 前留存旧字节 → save → DB 行写入（provider 精确 owner_id + hash）→ zip 缓存 (hash, sid) 锚定 → mem 失效。
- 预期：三元组精确查询命中（sid+hash）、锚点查询正确、zip 缓存条目 (hash, sid)、冷读后 mem 锚定 sid。

**UC12-12 OBS E2E 脚本签名对齐（维护性修复，第十轮 P2-1）**
- 背景：scripts/verify_obs_skill_e2e.py:82 自 B1 迁移（b37aa7f44 移除 version 参数）后未随 API 更新，三参调用 TypeError；脚本需真实 OBS 凭据不在 CI 范围，坏了未被发现。
- 修复：对齐两参数签名 skill_md_content(key, SKILL)（AST 校验参数数=2）。
- 预期：脚本在有 OBS AK/SK 的环境可跑通 [2] 详情提取步骤（不再 TypeError）。

### 场景 S11：direct 端（本机直连执行）技能物化（第六轮核验口径）

环境：direct 模式（事件流水线本机物化执行，非 worker 投递），OBS 模式。以下用例不改代码，验收时确认 direct 端与 worker 端技能获取语义一致。

**UC11-1 direct 任务覆盖更新后物化新内容**
- 步骤：创建引用技能 `my-skill` v1 的 direct 任务（不立即执行）→ 同名覆盖上传 v2 → 触发该任务执行 → 检查工位 `ws/.skills/my-skill/SKILL.md`。
- 预期：为 v2。direct 端物化在**执行时**调 `get_skill_zip_bytes`（磁盘缓存同 key 原子覆盖 → miss 回源 OBS），非入队时快照——与 worker claim 现签（UC7-5）同款"执行时取最新"语义，两端口径一致。

**UC11-2 direct 端本地 official 技能物化（非 OBS 技能路径）**
- 步骤：创建引用 official 技能的 direct 任务 → 执行 → 检查 `ws/.skills/{skill}/`。
- 预期：copytree 自本地 `get_skill_path` 目录，内容为当前 official 同步版本（official 更新走 R31 定时同步，双机都有仓库目录，无跨机缺失）。

---

### 场景 S12：跨 provider 同名仓库——provider 键段根治（第七轮核验 + 根治实施，2026-10-26）

环境：同 S1；`gitcode:acme/web` 与 `github:acme/web` 两行 DB 技能记录（owner_id 各带 provider 前缀，UNIQUE 约束下为两行）。

**原缺陷（实测复现，真实 SQLite 非模拟）**：`skill_key()` repo 分支剥 provider 前缀 → 两行共享同一对象键（全库唯一不带 provider 隔离的存储键，对照 workspace_repo_dir/knowledge 均带）。读者缓存已填后写者覆盖另一 provider 行 → verify 恒命中插入序第一行 → 缓存永久 stale；且 repo 冷读 sid=0，id 锚点不生效。

**根治方案（本单实施）**：
1. `skill_key()` repo 段含 provider：`skills/repo/{provider}/{owner}/{repo}/{name}/{name}.zip`；裸 owner/repo 默认 gitcode（to_repo_key 同款默认）。
2. `parse_skill_key()` 识别新格式（返回 provider 字段）；旧格式仍可解析（存量行兼容）。
3. 定位层 `_db_hash_for_object_key`：新格式键 → owner_id 精确三元组查询（UNIQUE 唯一行），sid 直接锚定（repo 冷读不再 sid=0）；旧格式键 → 保留全行扫描兼容分支。
4. 写路径消歧：`_resolve_repo_key(repo, strict=True)`（仅 upload/convert）：裸 repo 同时命中 gitcode: 与 github: 时显式 400 报错带指引；读路径保持原行为（收窄修复范围）。
5. 存量迁移：零删除懒迁移——存量行 file_path 保持旧格式（读侧按行内 file_path 直读 + 兼容分支校验，行为与修复前一致不更糟）；行在下一次覆盖上传时自然迁到新键。旧对象原地保留永不删。

**UC12-1 跨 provider 隔离 + 覆盖感知（根治验收）**
- 前置：两 provider 行各自独立对象键。
- 步骤：B 读两键 v1 填缓存 → A 向 github:acme/web 行覆盖 v2 → B 不重启再读两键。
- 预期：github 键读到 v2（原盲区场景修复）；gitcode 键仍读 v1（键隔离，无串扰）。

**UC12-2 首读前覆盖（回源路径正确）**
- 步骤：github 行覆盖 v2 → B 首次读。
- 预期：冷读回源读到 v2（最后写者胜）。

**UC12-3 多行互不串扰、内容稳定（安全底线）**
- 步骤：两键各自缓存后，github 行覆盖 → 两键各连续读 5 次。
- 预期：github 键稳定 v2、gitcode 键稳定 v1，无抖动、无第三种内容。

**UC12-4 repo 冷读锚点建立（根治验收）**
- 步骤：新格式键冷读回源 → 检查两级缓存条目 sid。
- 预期：sid=行主键（provider 精确查询锚定；原 sid=0）。对照：旧格式键（存量行）冷读 sid=0（兼容降级，与基线一致）。

**UC12-5 存量行旧格式键兼容（降级不更糟）**
- 步骤：手工造旧格式 file_path 行（无 provider 段）→ 冷读 → 缓存命中 → 覆盖（hash+存储换新）→ 不重启再读。
- 预期：全部正常（冷读 v1、命中 v1、覆盖后 v2）——单 provider 存量行行为与根治前完全一致。

**UC12-6 写路径消歧（strict 模式）**
- 前置：managed_repos 同时存在 gitcode:acme/web 与 github:acme/web。
- 步骤：以裸 "acme/web" 上传/convert。
- 预期：400 显式报错（"请指定完整仓库标识（如 gitcode:acme/web）"）；读路径（list/search）不受影响。

## 四、缓存家族全链路扫描（2026-10-23，基于 weekly-08 head 910b38baf；六轮扫描收口）

> 按同一 bug 家族（"写入后另一进程/实例读到旧值"）对 src/ 全部进程内缓存点及 skill 全流程逐轮核验，结论分档。修复范围仅收窄到确认有问题的点（P47-P49），其余记录不修。

**六轮扫描覆盖矩阵（收口）**：

| 维度 | 轮次 | 结论 |
|------|------|------|
| 进程内缓存 ×12 + 磁盘 zip 缓存 + DB | 第 1-2 轮 | 同病 convert 并入 P47/P49；其余 TTL 自愈或核验正确 |
| 存储模式（OBS/local/None）× 部署形态（单机/集群）× 任务投递 ①②③ | 第 3 轮 | 唯一静默失败 = local+集群（F1）；任务链路 claim 现签无陈旧 |
| 预签名 URL 家族 / 删除 DB 门 / 列表端点 / worker 能力反馈 | 第 4 轮 | 正确设计（守卫用例 S9） |
| worker 上报 / 并发上传 / 场景绑定 | 第 5 轮 | 全部无问题（S10） |
| direct 端物化 / 前端页面缓存 | 第 6 轮 | 全部无问题（见 4.3 末两条） |

剩余唯一不可扫项为外部依赖（OBS putObject 强一致写、GitCode API）——云服务 SLA 域，非代码缺陷面。

### 4.1 同病——随本单修复

| 位置 | 问题 | 处置 |
|------|------|------|
| `convert_skill`（skills.py:907-925） | `zip_cache_put(dst_key, src_zip)` 不传 hash、`upsert_skill_obs` 不写 hash——转换产生的技能与上传同病，覆盖更新后另一进程读旧内容 | 并入 P47 |
| convert 的 `mem_cache_invalidate(skill_name)`（skills.py:927） | P49 改 key 口径后需同步改为 object_key 口径，否则转换后失效不生效 | 并入 P49 |

### 4.2 同家族但有界自愈（TTL 兜底，低危，记录不修）

| 位置 | 陈旧窗口 | 说明 |
|------|---------|------|
| `_PROVIDERS_CACHE`（auth/providers.py:64，TTL 300s） | 最长 5 分钟 | admin 改 OAuth 配置时 `invalidate_provider_cache()` 只清当前进程，另一进程靠 TTL 自愈 |
| `_USER_IMAGE_CACHE`（prompt_builder.py:488，TTL 600s） | 10 分钟 | 用户头像，无害 |
| `_advanced_options_cache`（prompt_builder.py:150，TTL 60s） | 1 分钟 | 场景高级选项，自愈 |
| `_SCENE_CACHE` / `_WL_ENABLED_CACHE` / `_global_bot_cache`（60s） | 1 分钟 | 自愈 |
| `_user_status_cache`（deps.py:90，TTL 5s） | 5 秒 | 禁用/改密失效 5s 内传播，可接受 |

### 4.3 核验为正确（无需修）

- **knowledge `read_with_cache`**（api/knowledge.py:117）：hash 由调用方每次从 DB 取、内存命中比对——即 #254 该有的正确范式，**P47 实施参照此写法**。
- **email 配置**（email_service.py:36）：共享文件 mtime 检查，跨进程天然一致。
- **repos 缓存**（routes/repos.py，TTL 1800s）：缓存体存 DB，跨进程共享 + 自愈。
- **SQLite 跨进程**（session_manager.py:126-130）：WAL + busy_timeout=5000 + synchronous=FULL，双进程写安全。
- **磁盘 zip index 并发写**（storage/__init__.py:278）：tmp+replace 原子，双进程同时写最坏丢一次更新 → 缓存 miss 回源自愈。
- **worker `.skills_cache`**（内容寻址目录）与 **worker_package**（版本 manifest hash）：by design 无陈旧。
- **前端 `_engineSkillCache`**：页面级缓存，refresh 成功后更新已有测试覆盖（tests/js/test_issue_335_skill_search.js）。

- **worker OBS 直连能力反馈**（agent.py `_OBS_FAIL_STREAK/_OBS_OK_STREAK/_OBS_FALLBACK`）：模块级全局变量，但降级决策是 worker 本进程的本地行为（claim 时随 body 上报，服务端不落库），无跨实例一致性问题。
- **场景 icon 读时重签**（routes/scenes.py `_resolve_scene_icon`）：icon 的签名 URL 在**读取时**由 `obs_icon_key` 动态重签——与技能 `zip_url` 详情页动态签发同款正确设计（场景域相邻结论，不展开，详见场景域文档）。
- **`list_skills` 列表端点**：DB 直查（is_active/scope/owner 条件查询），无缓存层，跨进程天然一致。
- **删除技能的跨进程安全性（论证，非巧合）**：删除先删 DB 行，另一进程的详情端点在读取缓存**之前**先查 DB（行不存在 → 404），DB 行是访问门——内存缓存残留内容不可达。对比上传更新：DB 行存在（门通过）但 content 读缓存 → 旧内容可达，这正是 #444 的根因结构。也反证 P47 修复点选在"读时 hash 校验"而非"写时广播"是对的：DB 是跨进程共享的真值源，把变更信号放进 DB（content_hash 列）即可。
- **worker 引擎技能列表（local_skills，第五轮核验）**：worker register/claim 时上报本机扫描结果，直接 UPDATE `agent_runtimes.local_skills/skill_scan_result` 落 DB；服务端读路径（settings 引擎详情、refresh-skills）全部 DB 直查，无进程内缓存。worker 侧技能变化 → 下次 claim 扫描上报即更新，跨进程天然一致。`pending_skill_scan` 指令在 worker 忙碌时下次空闲 claim 才带（有界延迟，接口 docstring 已注明，非 bug）。
- **并发上传同名（第五轮核验）**：A/B 并发写 OBS/DB 交错最坏情形为——OBS 是后写者的 zip、DB hash 是另一写者写的 → 读侧 hash 校验发现不匹配 → 多回源一次 OBS（拿到 OBS 上的实际内容，正确）→ 缓存按 DB hash 标注稳定。hash 是变更信号而非内容指纹，错位只浪费一次回源、不会服务旧内容——P47 修复顺带提供的韧性。
- **场景引用技能绑定（第五轮核验）**：场景同步的 skill 引用预检（`_check_skills_exist`）查 skill 表（DB 直查）；skill 删除后场景侧 stale 检测+报告归 R31/P46 既有设计，本单不重复覆盖。
- **direct 端技能物化（第六轮核验）**：`_materialize_direct_workdir`（event_pipeline.py）的 OBS 技能走 `get_skill_zip_bytes` → 磁盘 zip 缓存（同 key 原子覆盖）→ miss 回源 OBS——与 worker ② 代拉共用同一条读路径，测一条即覆盖两者；本地技能 copytree 走 `get_skill_path`（official 技能双机都有仓库目录）。物化发生在任务执行时（非入队时），与 worker claim 现签同款"执行时取最新"语义。
- **前端技能页面（第六轮核验）**：`pages/skills.js`（1057 行全扫）无内容缓存——列表/详情每次 API 直拉，唯一"缓存"是 token 读取；`runtimes.js` 的 `_engineSkillCache` 引擎技能弹窗缓存已有 #335 测试覆盖 refresh 更新。

### 4.4 同域缺口（非缓存问题，明确不在本单范围）

- **子技能三端点**（`GET /{skill_id}/sub`、`GET /{skill_id}/sub get`、`GET /package-sub/{pkg_id}/{sub_name}`，skills.py:948+/1047+）：只读本地 `SKILLS_DIR` 磁盘——user/repo 技能（存 OBS）的 `references/` 子技能一律 404。双机部署下 official 技能无陈旧（两机都有仓库目录），但存在功能缺口，建议另立 issue。

### 4.5 待另立 issue 清单（本单不修，防止遗漏的收拢汇总）

| # | 问题 | 来源 | 建议处置 |
|---|------|------|---------|
| F1 | local + 集群部署下 B 机技能详情 `content` 静默返回空串（`skill_md_content` 回源 miss → None，无告警）——与 obs-only 未配置时显式 503 的边界不一致 | UC7-4 | **已另立 issue [#447](https://gitcode.com/grape-dev/grape/issues/447) 跟踪**；详情端点补显式错误（503 + "请配置 OBS 或共享盘" 指引），守卫口径对齐既有 503 分支 |
| F2 | 子技能三端点只读本地磁盘，user/repo 技能（OBS 存储）的 references/ 子技能一律 404 | §4.4 / UC6-3 | **已另立 issue [#448](https://gitcode.com/grape-dev/grape/issues/448) 跟踪**；子技能端点支持从 OBS zip 提取 references/（复用 `extract_skill_md` 同款按 entry 解压模式） |
| F3 | ~~`get_skill_content_hash(skill_name, scope)` 按 (name, scope) 查询可能命中多行同名的 LIMIT 1 不确定行~~ | §1.4 失效点 4 | **已随 #444 修复**：新增 `get_skill_hash_by_key(skill_name, scope, owner_id)` 三元组精确查询（命中 skill 表 UNIQUE 约束，唯一确定一行），`tests/test_issue444_skill_cache_sync.py::test_t6_multi_owner_same_name` 覆盖验证（UC8-3 联合验收通过）——无需另立 issue |

## 五、验收清单（与 Issue #444 对照）

- [ ] UC1-1 / UC1-3：双进程场景，A 上传同名覆盖后，B 不重启即读到新内容（user 与 repo 两 scope）
- [ ] UC1-4：hash 一致时仍走内存命中快路径，不退化为每次回源 OBS
- [ ] UC2-1 / UC2-2：user/repo/official 同名技能互不串缓存
- [ ] UC3-1 / UC3-2：存量无 hash 数据与 DB 异常场景降级语义不变
- [ ] UC4-1 ~ UC4-3：官方同步、删除、worker 投递三条链路不回归
- [ ] UC5-1 ~ UC5-4：convert 链路写 hash（DB + 磁盘缓存）、转换产物覆盖更新后另一进程免重启、转换后源/目标缓存按新 key 口径失效
- [ ] UC6-1 ~ UC6-3：家族记录项观察口径确认（TTL 自愈边界、子技能 404 已知行为不变）
- [ ] UC7-1 / UC7-2 / UC7-3：OBS+单机双进程、OBS+集群双机、local+单机双进程三形态下 S1 用例通过（P47-P49 与存储后端无关）
- [ ] UC7-4：local+集群已知边界行为确认（详情静默空串/代拉 502，另立 issue 改显式错误）
- [ ] UC7-5：任务 claim 现签 URL，覆盖上传后新 claim 任务执行新内容
- [ ] UC7-6：①② 双降级时 skill_files 快照语义确认
- [ ] UC8-1 ~ UC8-3：backlog F1-F3 边界行为确认（local+集群静默空串、子技能 404、多 owner 同名无串扰【兼 F3 联合验收】）
- [ ] UC9-1 / UC9-2：skill 域设计守卫——删除后另一进程立即 404（DB 行访问门，查询顺序不得后移）、zip_url 每次读时现签
- [ ] UC10-1 ~ UC10-3：并发上传同名不服务旧内容、worker local_skills 跨实例一致、场景引用预检一致（第五轮核验口径）
- [ ] UC11-1 / UC11-2：direct 端执行时物化新内容（与 UC7-5 worker 端口径一致）、official 技能本地 copytree 物化（第六轮核验口径）
- [x] UC12-1 ~ UC12-6：跨 provider 根治验收——键隔离 + 覆盖感知（github 键读 v2、gitcode 键不受影响）、首读前覆盖回源正确、多行互不串扰不抖动、repo 冷读 sid 锚定（新格式）/ sid=0（旧格式兼容）、存量旧格式键全流程不变（`test_s12_uc12_5_legacy_key_compat`）、写路径消歧 400 四路径（`test_s12_uc12_6_strict_repo_ambiguity`）——全部随 CI pytest（S12 段 6 用例）+ 真实 SQLite 端到端均过
- [x] UC12-7 / UC12-8：物化链路 get_skill_zip_bytes 读时校验（覆盖后免重启返回新字节、降级零回归）、upload 409 存储回滚（旧字节恢复、DB 行未动）——`test_pa_zip_bytes_cross_process_invalidate` / `test_pa_zip_bytes_degrade_semantics` / `test_pb_upload_409_restores_previous_object`（第九轮收尾，P-A/P-B）
- [x] UC12-9 / UC12-10 / UC12-11 / UC12-12：路由级核心序列测试（upload 409 回滚、convert 复制语义+行隔离、upload 新键+锚定全序列、OBS E2E 脚本签名对齐）——`test_route_*` 三用例 + 脚本 AST 校验（第十轮 P2-1/P2-2 收口）
- [ ] 既有测试套件相关文件（`tests/test_skill_sync_obs.py`、`tests/test_issue179_skills.py` 等）全量通过；新增测试覆盖上传/转换写 hash、读侧校验失效回源、scope 隔离

## 六、场景用例与测试代码（Issue #444 实施交付）

新增 `tests/test_issue444_skill_cache_sync.py`（随 CI 跑的 pytest，19 个测试函数），既有套件已同步更新到新口径（`test_skill_sync_obs.py` 6 个 hash 校验测试 + `test_upload_invalidates_caches` + mem/zip cache 兼容测试、`test_issue179_skills.py` upsert 断言）。目标套件全部通过（86/86，含 P1-1/P3-1 审计修复用例）。

**Mock 策略（不触任何真实外部依赖）**：DB 读路径 mock `src.user.db.get_skill_hash_by_key` 与 `get_skill_hashes_by_name_scope` 为内存 dict 替身（读时校验的全部查询入口）；OBS/local 存储 mock 为内存 dict 后端 `_MemStorage`（带回源计数，用于断言快路径零回源）；zip 磁盘缓存/内存缓存目录与 dict 隔离；仅 upsert 的 SQL 语句本身（测试对象）走临时 SQLite（test_issue179 同款模式）。

### 6.1 公共测试基座（代码）

```python
class _MemStorage:                       # OBS/local 存储替身
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.read_count = 0              # 回源计数（快路径断言用）
    def save_bytes(self, key, data): self.objects[key] = data; return key
    def read_bytes(self, key): self.read_count += 1; return self.objects.get(key)
    def copy_object(self, src, dst): ...  # convert 场景用
    def delete_object(self, key): ...

class _FakeSkillRows:                    # get_skill_hash_by_key + get_skill_hashes_by_name_scope 内存替身
    def __init__(self):
        self.rows: dict[tuple, tuple] = {}   # (name, scope, owner) -> (id, hash, file_path)
        self.raises = False                  # T5 异常分支开关
    def __call__(self, skill_name, scope="official", owner_id=""):   # 精确三元组查询
        if self.raises: raise sqlite3.OperationalError("db locked (mock)")
        return self.rows.get((skill_name, scope or "official", owner_id or ""), (0, "", ""))
    def by_name_scope(self, skill_name, scope):                       # P1-1 (name, scope) 全行查询
        return [(k[2], v[1]) for k, v in self.rows.items()
                if k[0] == skill_name and k[1] == (scope or "official")]

@pytest.fixture()
def env(monkeypatch):                    # 读路径环境：DB mock + 存储 mock + 缓存隔离
    monkeypatch.setattr(user_db, "get_skill_hash_by_key", _FakeSkillRows())
    monkeypatch.setattr(storage_mod, "get_skill_storage", lambda: _MemStorage())
    monkeypatch.setattr(storage_mod, "get_storage", lambda: None)
    monkeypatch.setattr(storage_mod, "ZIP_CACHE_DIR", ...); storage_mod._mem_cache.clear()

@pytest.fixture()
def wdb(monkeypatch):                    # 写路径环境：临时 SQLite（upsert SQL 是测试对象）
    conn = sqlite3.connect(...); conn.execute(_SKILL_DDL)  # skill 表最小 schema
    monkeypatch.setattr(sm/user_db, "db", _DB()); ...

def _seed(env, key, scope, owner, content):   # 播种一个技能：存储对象 + DB hash 行
    zip_bytes = _make_zip(skill, content); h = sha256(zip_bytes)
    env["storage"].objects[key] = zip_bytes
    env["rows"].rows[(skill, scope, owner)] = (1, h, key)
```

### 6.2 核心修复场景 S1（双进程同名覆盖）——用例代码

**UC1-1 另一进程不重启即返回新内容 / UC1-4 快路径不退化**

```python
def test_t2_cross_process_invalidate(env):
    key = skill_key("custom", "7", "my-skill")
    _seed(env, key, "user", "7", "# v1 content")
    assert skill_md_content(key, "my-skill") == "# v1 content"        # 本进程缓存 v1
    assert env["storage"].read_count == 1

    # 模拟另一进程覆盖上传（本进程两级缓存不动，仅 DB hash 与存储对象换新）
    _seed(env, key, "user", "7", "# v2 content")
    assert skill_md_content(key, "my-skill") == "# v2 content"        # UC1-1：免重启读到 v2
    assert env["storage"].read_count == 2                             # 恰好一次额外回源

    # UC1-4：hash 一致 → 第三次读零回源（快路径不退化）
    assert skill_md_content(key, "my-skill") == "# v2 content"
    assert env["storage"].read_count == 2
```

**UC1-3 仓库技能（scope=repo）同样修复**

```python
def test_t2_repo_scope_with_prefix(env):
    key = skill_key("repo", "acme/web", "my-skill")
    _seed(env, key, "repo", "gitcode:acme/web", "# repo v1")     # DB owner_id 带 provider 前缀
    assert skill_md_content(key, "my-skill") == "# repo v1"
    _seed(env, key, "repo", "gitcode:acme/web", "# repo v2")
    assert skill_md_content(key, "my-skill") == "# repo v2"      # repo scope 同样免重启

def test_t2_repo_scope_owner_without_prefix(env):                # owner 无前缀（local/老数据）回退查询分支
    key = skill_key("repo", "acme/web", "my-skill")
    _seed(env, key, "repo", "acme/web", "# plain v1")
    assert skill_md_content(key, "my-skill") == "# plain v1"
```

### 6.3 场景 S2（同名跨 scope 不串缓存）——用例代码

**UC2-1 / UC2-2**

```python
def test_t3_scope_isolation(env):
    k_user  = skill_key("custom", "7", "my-skill")
    k_repo  = skill_key("repo", "acme/web", "my-skill")
    k_off   = skill_key("official", "", "my-skill")
    _seed(env, k_user, "user", "7", "# user ver")
    _seed(env, k_repo, "repo", "acme/web", "# repo ver")
    _seed(env, k_off,  "official", "", "# official ver")

    for _ in range(2):                                            # 交替读两轮
        assert skill_md_content(k_user, "my-skill") == "# user ver"
        assert skill_md_content(k_repo, "my-skill") == "# repo ver"
        assert skill_md_content(k_off,  "my-skill") == "# official ver"

    from src.storage import _mem_cache
    assert len([k for k in _mem_cache if k.endswith("my-skill.zip")]) == 3   # 3 个独立条目，无串键
```

### 6.4 场景 S3（存量降级兼容）——用例代码

**UC3-1 存量无 hash / UC3-2 DB 异常**

```python
def test_t5_cache_without_hash(env):          # UC3-1a：缓存条目无 hash（存量旧数据）
    key = skill_key("custom", "7", "old-skill")
    mem_cache_put(key, "# legacy content", "")            # 无 hash；DB 无行
    assert skill_md_content(key, "old-skill") == "# legacy content"
    assert env["storage"].read_count == 0                 # 不回源

def test_t5_db_empty_hash(env):               # UC3-1b：DB 行存在但 hash 为空（修复前上传的存量）
    key = skill_key("custom", "7", "old-skill")
    env["storage"].objects[key] = _make_zip("old-skill", "# stored")
    env["rows"].rows[("old-skill", "user", "7")] = (1, "", key)     # DB hash 空
    mem_cache_put(key, "# cached legacy", "deadbeef")
    assert skill_md_content(key, "old-skill") == "# cached legacy"  # 不校验
    assert env["storage"].read_count == 0

def test_t5_db_exception(env):                # UC3-2：DB 查询异常 → 返回缓存内容不阻塞
    key = skill_key("custom", "7", "x-skill")
    mem_cache_put(key, "# cached content", "somehash")
    env["rows"].raises = True
    assert skill_md_content(key, "x-skill") == "# cached content"
```

### 6.5 场景 S4（官方同步回归）——用例代码

**UC4-1 官方技能同步不回归**

```python
def test_t7_official_regen(env):
    key = skill_key("official", "", "my-skill")
    _seed(env, key, "official", "", "# official v1")
    assert skill_md_content(key, "my-skill") == "# official v1"
    _seed(env, key, "official", "", "# official v2")      # 官方同步覆盖（按主键更新 hash）
    assert skill_md_content(key, "my-skill") == "# official v2"   # 失效回源
```

UC4-2（删除）/UC4-3（worker 投递）：删除走 `invalidate_skill_caches`（签名未变，`test_skill_sync_obs.py::test_upload_invalidates_caches` 覆盖 invalidate 链路）；worker 投递走 `get_skill_zip_bytes` 磁盘缓存（该层未改，由既有 zip 缓存测试覆盖）。

### 6.6 场景 S5（convert 链路）——用例代码

**UC5-1/UC5-2 转换产物写 hash / UC5-3 覆盖更新免重启**

```python
def test_t4_convert_hash_reuse(env):
    src_key = skill_key("custom", "7", "my-skill")
    dst_key = skill_key("repo", "acme/web", "my-skill")
    src_zip, h = _seed(env, src_key, "user", "7", "# convert me")
    zip_cache_put(src_key, src_zip, h)

    assert env["storage"].copy_object(src_key, dst_key)             # convert_skill 路由内同款三步
    _sz = zip_cache_get_with_hash(src_key)
    src_zip2, _sh = _sz if _sz else (None, "")
    content_hash = _sh or (hashlib.sha256(src_zip2).hexdigest() if src_zip2 else "")
    assert content_hash == h                                        # 源缓存有 hash → 直接复用

    env["rows"].rows[("my-skill", "repo", "gitcode:acme/web")] = (2, content_hash, dst_key)
    zip_cache_put(dst_key, src_zip2, content_hash)
    data, cached = zip_cache_get_with_hash(dst_key)
    assert data == src_zip and cached == h                          # UC5-2：dst 缓存带 hash

    assert skill_md_content(dst_key, "my-skill") == "# convert me"  # UC5-1：另一进程读 dst 回源命中

def test_t4_convert_dst_overwrite_no_restart(env):                 # UC5-3：convert 产物覆盖免重启
    dst_key = skill_key("repo", "acme/web", "my-skill")
    _seed(env, dst_key, "repo", "gitcode:acme/web", "# converted v1")
    assert skill_md_content(dst_key, "my-skill") == "# converted v1"
    _seed(env, dst_key, "repo", "gitcode:acme/web", "# overwrite v2")
    assert skill_md_content(dst_key, "my-skill") == "# overwrite v2"

def test_t4_convert_source_without_hash(env):                      # 源缓存无 hash（存量）→ 现算
    src_key = skill_key("custom", "7", "legacy-skill")
    src_zip = _make_zip("legacy-skill", "# legacy")
    env["storage"].objects[src_key] = src_zip                     # 无 zip 缓存条目
    _sz = zip_cache_get_with_hash(src_key)                        # miss
    src_zip2, _sh = _sz if _sz else (None, "")
    if src_zip2 is None:
        src_zip2 = get_skill_zip_bytes(src_key) or b""            # 回源拿字节
    content_hash = _sh or (hashlib.sha256(src_zip2).hexdigest() if src_zip2 else "")
    assert content_hash == hashlib.sha256(src_zip).hexdigest()
```

### 6.7 场景 S8（backlog 边界 / F3 联合验收）——用例代码

**UC8-3 多 owner 同名无串扰**

```python
def test_t6_multi_owner_same_name(env):
    k_a = skill_key("custom", "7", "my-skill")
    k_b = skill_key("custom", "8", "my-skill")
    _seed(env, k_a, "user", "7", "# owner7")
    _seed(env, k_b, "user", "8", "# owner8")
    assert skill_md_content(k_a, "my-skill") == "# owner7"
    assert skill_md_content(k_b, "my-skill") == "# owner8"

    _seed(env, k_a, "user", "7", "# owner7 v2")          # owner7 覆盖更新
    assert skill_md_content(k_a, "my-skill") == "# owner7 v2"
    assert skill_md_content(k_b, "my-skill") == "# owner8"   # owner8 缓存不受干扰（F3 根治）
```

UC8-1（F1 local+集群静默空串）/ UC8-2（F2 子技能 404）为已知边界、已另立 issue（#447/#448），不在本单测试范围。

### 6.7b 审计修复 P1-1（repo owner 全 provider 前缀）+ P3-1（删除链路守卫）——用例代码

**P1-1 github:/gitlab: 前缀的 repo 技能读时校验生效（审计发现：原双重查询只覆盖 gitcode:/无前缀）**

```python
def test_p1_1_all_provider_prefixes(env):
    """每个 provider 独立场景（同一对象键在 DB 只有一行）——跨 provider 同键共存
    属数据模型歧义（同 owner/repo 剥前缀后同对象键），由 helper 级测试覆盖。"""
    from src.storage import _mem_cache, zip_cache_invalidate
    key = skill_key("repo", "acme/web", "my-skill")   # 对象键 owner 段无前缀
    for provider in ("github", "gitlab", "gitcode", ""):
        owner = f"{provider}:acme/web" if provider else "acme/web"
        # 干净场景：清缓存 + DB 只保留本 provider 行
        _mem_cache.clear(); zip_cache_invalidate(key); env["rows"].rows.clear()
        _seed(env, key, "repo", owner, f"# {provider or 'plain'} v1")
        assert skill_md_content(key, "my-skill") == f"# {provider or 'plain'} v1"

        # 另一进程覆盖 → 该 provider 口径同样免重启（校验不降级）
        _seed(env, key, "repo", owner, f"# {provider or 'plain'} v2")
        assert skill_md_content(key, "my-skill") == f"# {provider or 'plain'} v2"  # P1-1 修复

def test_p1_1_repo_row_lookup_helper(env):
    """get_skill_hashes_by_name_scope + 去前缀比对：同名多 repo 各自命中。"""
    from src.user.db import get_skill_hashes_by_name_scope
    from src.storage import _db_hash_for_object_key
    h1 = _seed(env, k, "repo", "gitcode:acme/web", "# gitcode ver")[1]
    h2 = _seed(env, k, "repo", "github:acme/web", "# github ver")[1]
    rows = get_skill_hashes_by_name_scope("my-skill", "repo")
    assert {("gitcode:acme/web", h1), ("github:acme/web", h2)} <= set(rows)
    assert _db_hash_for_object_key(k, "my-skill") in (h1, h2)   # 全 provider 匹配命中
```

**P3-1 UC4-2 删除后两层缓存清除 / UC9-1 删除后降级语义（DB 行访问门守卫）**

```python
def test_p3_1_invalidate_skill_caches_both_layers(env):
    """UC4-2：invalidate_skill_caches 同时清两层缓存（object_key 口径）。"""
    from src.storage import invalidate_skill_caches, zip_cache_get_with_hash, _mem_cache
    key = skill_key("custom", "7", "del-skill")
    _seed(env, key, "user", "7", "# to delete", skill="del-skill")
    assert skill_md_content(key, "del-skill") == "# to delete"   # 两层缓存已填充
    assert key in _mem_cache
    assert zip_cache_get_with_hash(key) is not None

    invalidate_skill_caches(key)
    assert key not in _mem_cache          # 内存缓存应被清除
    assert zip_cache_get_with_hash(key) is None   # 磁盘缓存条目应被清除

def test_p3_1_deleted_row_degrades_not_stale(env):
    """UC9-1（缓存视角）：DB 行删除后（hash 查不到）校验降级返回缓存内容——
    404 由详情端点 DB 行访问门保证（先查 DB 行再读缓存）。"""
    key = skill_key("custom", "7", "gone-skill")
    _seed(env, key, "user", "7", "# cached before delete", skill="gone-skill")
    assert skill_md_content(key, "gone-skill") == "# cached before delete"

    env["rows"].rows.pop(("gone-skill", "user", "7"), None)   # 模拟另一进程删除 DB 行
    assert skill_md_content(key, "gone-skill") == "# cached before delete"  # 降级不报错
```

**P1-1 真机复核（已执行，MySQL 后端）**：真实 DB 造 `github:octo-labs/gh-test` 前缀 repo 行 → `_db_hash_for_object_key` 命中（`helper_found_github_row: true`）→ hash 变更被 `_verify_skill_cache_hash` 感知返回 False（`verify_detects_change: true`，修复前该口径恒降级 True）→ 测试行已清理。

### 6.7c 第七轮收口核验（P1-1 修复后全边界复扫）

P1-1 修复落地后的边界复扫结论（本轮零新增问题，以下为核验记录）：

1. **provider 前缀对象键形状**（实测）：`github:`/`gitlab:`/带点 repo 名的对象键 `parse_skill_key` 全部正确解析（regex `[\w.\-]+` 覆盖，owner 剥前缀口径与 DB 行比对一致）——修复输入端无漏洞。
2. **跨 provider 同键共存的数据模型歧义**：DB 同时存在 `gitcode:acme/web` 与 `github:acme/web` 两行时（同 owner/repo 剥前缀后同对象键），`_db_hash_for_object_key` 返回首个匹配行——最坏情形 hash 不匹配多一次回源自愈（存储对象唯一 key，内容恒正确），**不服务旧内容**；且详情端点 owner 归属校验按 DB 行精确匹配（403），权限面不受 helper 匹配顺序影响。已落为独立测试锁住"安全退化"行为：

```python
def test_p1_1_cross_provider_same_key_coexistence(env):
    """跨 provider 同键共存：helper 返回任一匹配行 hash；最坏情形多一次回源自愈，
    不服务旧内容（存储对象唯一 key，回源内容恒正确）。锁住安全退化行为。"""
    from src.storage import _db_hash_for_object_key
    key = skill_key("repo", "acme/web", "my-skill")          # 两行共享同一对象键
    h_gitcode = _seed(env, key, "repo", "gitcode:acme/web", "# stored content")[1]
    h_github = _seed(env, key, "repo", "github:acme/web", "# stored content")[1]
    assert h_gitcode == h_github, "同存储对象 → 同 zip 字节 → 同 hash"

    # 两行共存：helper 必命中其一（任一 provider 行），不降级
    assert _db_hash_for_object_key(key, "my-skill") in (h_gitcode, h_github)

    # 覆盖更新任一行（github 行 hash 换新）→ 读侧必然感知（命中旧行则校验失败回源自愈；
    # 命中新行直接正确）
    _seed(env, key, "repo", "github:acme/web", "# stored content v2")
    assert skill_md_content(key, "my-skill") == "# stored content v2"
```

3. **`_zip_evict` 版本清理**：`ZIP_CACHE_MAX_VERSIONS=1`（v2.4 固定 key 模型），`by_tag` 按去文件名段归类——同一 object_key 只有一个条目，无陈旧版本滞留。
4. **zip index 并发写**：`tmp+replace` 原子（复确认 §4.3 结论），集群双机各写各的目录，无共享 index 竞争。
5. **任务投递链路 24 处 `get_skill_object_key`/`get_skill_by_id` 调用**：全部拿 `file_path` 对象键直取 zip（磁盘缓存），不经 hash 校验路径——与 #444 修复正交，claim 现签语义不变。

**七轮扫描终态矩阵**：

| 轮次 | 维度 | 发现与处置 |
|------|------|-----------|
| 1-2 | 进程内缓存 ×12 + 磁盘/DB | convert 同病 → 并入 P47/P49 |
| 3 | 存储模式 × 部署形态 × 投递链路 | F1（#447）；claim 现签守卫（UC7-5） |
| 4 | 预签名 URL / 删除门 / 列表 / 能力反馈 | S9 守卫；非 skill 项按用户指令移除 |
| 5 | worker 上报 / 并发上传 / 场景绑定 | 全部无问题（S10） |
| 6 | direct 物化 / 前端页面 | 全部无问题（S11） |
| 审计 | 13 维度全链路 | P1-1（已修+真机复核）、P2-1（已修 Refs）、P3-1（已补测试） |
| 7 | P1-1 修复后边界复扫 | 零新增；跨 provider 共存落测试（6.7c-2） |

### 6.8 P47 写侧贯通（upsert SQL）——用例代码

```python
def test_t1_write_side_hash(wdb):
    key = skill_key("custom", "7", "my-skill")
    zip_bytes = _make_zip("my-skill", "# v1")
    h = hashlib.sha256(zip_bytes).hexdigest()

    sid = upsert_skill_obs("user", "7", "my-skill", "coding", "d", key, "7", 0, h)
    assert sid and isinstance(sid, int)                    # 返回主键 id（int|None）
    _, db_hash, _ = get_skill_hash_by_key("my-skill", "user", "7")
    assert db_hash == h                                    # DB content_hash 落库

    zip_cache_put(key, zip_bytes, h)
    data, cached = zip_cache_get_with_hash(key)
    assert data == zip_bytes and cached == h               # zip 缓存条目带 hash

    h2 = hashlib.sha256(_make_zip("my-skill", "# v2")).hexdigest()
    sid2 = upsert_skill_obs("user", "7", "my-skill", "coding", "d", key, "7", 0, h2)
    assert sid2 == sid                                     # UPDATE 分支：同键更新同一行
    _, db_hash2, _ = get_skill_hash_by_key("my-skill", "user", "7")
    assert db_hash2 == h2                                  # hash 换新

def test_upsert_failure_returns_none(wdb, monkeypatch):   # DB 异常 → None（falsy），if not ok 语义不变
    monkeypatch.setattr(user_db, "db", _BrokenDB())
    assert upsert_skill_obs("user", "7", "x", "coding", "d", "k", "7", 0, "h") is None
```

### 6.9 环境部署类用例（不入库）

UC6/UC7/UC9-UC11 为环境部署类观察口径（双进程/集群/OBS 真机验证），不属单测范围——按验收清单在目标环境执行，工具不入库（交付纪律：测试脚本仅随 CI 的 pytest 用例）。

### 6.10 场景 S12（跨 provider 根治）——用例代码

S12 根治后为正向验收口径（不再是盲区锁定）：provider 键段隔离 + 定位层精确查询。UC12-1 ~ UC12-6 全部有随 CI 跑的 pytest 用例（UC12-5 复现存量行旧格式键全流程；UC12-6 以真实 SQLite managed_repos 表驱动 `_resolve_repo_key` 的 strict/非 strict/带前缀/单行四条路径），另有真实 SQLite 端到端复验在验证时执行过（两 provider 独立键、覆盖感知、存量兼容）。测试代码见 `tests/test_issue444_skill_cache_sync.py` S12 段。

**UC12-1 跨 provider 隔离 + 覆盖感知（根治验收）**

```python
def test_s12_uc12_1_cross_provider_isolation(env):
    k_gitcode = skill_key("repo", "gitcode:acme/web", "my-skill")
    k_github = skill_key("repo", "github:acme/web", "my-skill")
    assert k_gitcode != k_github, "根治：两 provider 不得共享对象键"
    _seed(env, k_gitcode, "repo", "gitcode:acme/web", "# gc v1", sid=51)
    _seed(env, k_github, "repo", "github:acme/web", "# gh v1", sid=52)
    assert skill_md_content(k_gitcode, "my-skill") == "# gc v1"   # B 读 gitcode v1 填缓存
    assert skill_md_content(k_github, "my-skill") == "# gh v1"
    _seed(env, k_github, "repo", "github:acme/web", "# gh v2", sid=52)  # A 向 github 行覆盖 v2
    assert skill_md_content(k_github, "my-skill") == "# gh v2", \
        "根治：同 provider 覆盖后免重启读到新内容（盲区已修复）"
    assert skill_md_content(k_gitcode, "my-skill") == "# gc v1", \
        "根治：另一 provider 的缓存不受覆盖影响（键隔离）"
```

**UC12-2 首读前覆盖（回源路径正确）**

```python
def test_s12_uc12_2_overwrite_before_first_read(env):
    k_github = skill_key("repo", "github:acme/web", "my-skill")
    _seed(env, k_github, "repo", "github:acme/web", "# v1", sid=52)
    _seed(env, k_github, "repo", "github:acme/web", "# v2", sid=52)  # 覆盖在 B 首读前
    assert skill_md_content(k_github, "my-skill") == "# v2", \
        "首读前覆盖：回源读存储对象最新字节（最后写者胜）"
```

**UC12-3 多行互不串扰、内容稳定（安全底线）**

```python
def test_s12_uc12_3_multi_row_hit_stable(env):
    k_gitcode = skill_key("repo", "gitcode:acme/web", "my-skill")
    k_github = skill_key("repo", "github:acme/web", "my-skill")
    _seed(env, k_gitcode, "repo", "gitcode:acme/web", "# gc v1", sid=51)
    _seed(env, k_github, "repo", "github:acme/web", "# gh v1", sid=52)
    assert skill_md_content(k_gitcode, "my-skill") == "# gc v1"
    assert skill_md_content(k_github, "my-skill") == "# gh v1"
    _seed(env, k_github, "repo", "github:acme/web", "# gh v2", sid=52)  # github 行换新
    outs = [skill_md_content(k_github, "my-skill") for _ in range(5)]
    assert len(set(outs)) == 1 and outs[0] == "# gh v2", f"github 键读数应稳定为 v2: {outs}"
    outs_gc = [skill_md_content(k_gitcode, "my-skill") for _ in range(5)]
    assert len(set(outs_gc)) == 1 and outs_gc[0] == "# gc v1", \
        f"gitcode 键读数应稳定不受影响: {outs_gc}"
```

**UC12-4 repo 冷读锚点建立（根治验收；对照旧格式 sid=0）**

```python
def test_s12_uc12_4_repo_cold_read_anchored(env):
    from src.storage import zip_cache_get_with_hash
    key = skill_key("repo", "gitcode:acme/web", "my-skill")
    _seed(env, key, "repo", "gitcode:acme/web", "# v1", sid=51)
    assert skill_md_content(key, "my-skill") == "# v1"   # 冷读回源
    mem_hit = mem_cache_get_with_hash(key)
    zip_hit = zip_cache_get_with_hash(key)
    assert mem_hit is not None and zip_hit is not None, "冷读回源后两级缓存应已填充"
    assert mem_hit[2] == 51 and zip_hit[2] == 51, \
        f"根治：repo 冷读应锚定 sid=51；实际 mem={mem_hit[2]} zip={zip_hit[2]}"
    # 对照：旧格式键（存量行）冷读 sid=0（兼容降级）
    k_legacy = key.replace("/skills/repo/gitcode/", "/skills/repo/")
    _seed(env, k_legacy, "repo", "gitcode:acme/web", "# legacy v1", sid=53)
    assert skill_md_content(k_legacy, "my-skill") == "# legacy v1"
    l_hit = mem_cache_get_with_hash(k_legacy)
    assert l_hit is not None and l_hit[2] == 0, \
        f"旧格式键冷读 sid=0（兼容降级）；实际: {l_hit[2] if l_hit else None}"
```

**UC12-5 存量行旧格式键兼容（降级不更糟）**

```python
def test_s12_uc12_5_legacy_key_compat(env):
    k_legacy = skill_key("repo", "gitcode:acme/web", "my-skill").replace(
        "/skills/repo/gitcode/", "/skills/repo/")   # 存量行 file_path（无 provider 段）
    _seed(env, k_legacy, "repo", "gitcode:acme/web", "# legacy v1")
    assert skill_md_content(k_legacy, "my-skill") == "# legacy v1", "存量行冷读应正常"
    assert skill_md_content(k_legacy, "my-skill") == "# legacy v1", "缓存命中应正常"
    _seed(env, k_legacy, "repo", "gitcode:acme/web", "# legacy v2")  # 另一进程覆盖
    assert skill_md_content(k_legacy, "my-skill") == "# legacy v2", \
        "存量行覆盖后应免重启读到新内容（兼容分支，行为与根治前一致）"
```

**UC12-6 写路径消歧（strict 模式，真实 SQL 驱动）**

```python
def test_s12_uc12_6_strict_repo_ambiguity(wdb):
    from fastapi import HTTPException
    from src.api.routes.skills import _resolve_repo_key
    conn = wdb["conn"]
    conn.execute("CREATE TABLE IF NOT EXISTS managed_repos (repo_key TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO managed_repos (repo_key) VALUES ('gitcode:acme/web')")
    conn.execute("INSERT INTO managed_repos (repo_key) VALUES ('github:acme/web')")
    conn.commit()

    with pytest.raises(HTTPException) as ei:             # strict + 双行并存 → 400 显式指引
        _resolve_repo_key("acme/web", strict=True)
    assert ei.value.status_code == 400 and "gitcode:acme/web" in str(ei.value.detail)

    rk = _resolve_repo_key("acme/web", strict=False)     # 读路径非 strict：原行为不变
    assert rk in ("gitcode:acme/web", "github:acme/web")
    assert _resolve_repo_key("github:acme/web", strict=True) == "github:acme/web"  # 带前缀直通

    conn.execute("DELETE FROM managed_repos WHERE repo_key='github:acme/web'")   # 单行不报错
    conn.commit()
    assert _resolve_repo_key("acme/web", strict=True) == "gitcode:acme/web"
```

### 6.11 第十轮路由级用例代码（P2-2 收口）

**UC12-9 upload 409 路由级回滚 / UC12-10 convert 路由核心序列 / UC12-11 upload 路由核心序列**

完整代码见 `tests/test_issue444_skill_cache_sync.py` 第十轮段（`test_route_upload_409_restores_previous_object` / `test_route_convert_overwrite_cross_process` / `test_route_upload_new_key_and_sid_flow`），均按路由 storage 分支行号逐行同序驱动：

```python
# UC12-9 核心（wdb：真实 upsert SQL + 路由 409 恢复序列）
_prev = store.read_bytes(key)                    # 路由：save 前留存旧字节（P-B）
store.save_bytes(key, new_zip)                   # 路由：save_bytes 覆盖
# upsert 失败（并发冲突）→ 路由 409 分支：
if not skill_id and _prev is not None:
    store.save_bytes(key, _prev)                 # 路由：恢复旧对象（DB 行未动）
assert store.objects[key] == old_zip

# UC12-10 核心（env：convert 路由 skills.py:927-940 同序）
assert env["storage"].copy_object(src_key, dst_key)
_sz = zip_cache_get_with_hash(src_key)
src_zip2, _sh = (_sz[:2] if _sz else (None, ""))
content_hash = _sh or hashlib.sha256(src_zip2).hexdigest()
zip_cache_put(dst_key, src_zip2, content_hash, dst_sid)   # dst 缓存锚定新行 id
assert skill_md_content(dst_key, name) == "# src v1"       # 另一进程读产物正确
# 源覆盖后产物不随源变（复制语义，行隔离）

# UC12-11 核心（env：upload 路由 skills.py:746-764 同序）
env["storage"].save_bytes(key, zip_bytes)
zip_cache_put(key, zip_bytes, content_hash, skill_id)     # 缓存 (hash, sid) 锚定
mem_cache_invalidate(key)
# 断言：三元组精确查询 sid+hash / 锚点查询 / zip+mem 缓存锚定
```

### 6.11 用例覆盖汇总

| 场景 | 用例 | 测试函数 | 状态 |
|------|------|---------|------|
| S1 | UC1-1 / UC1-4 | `test_t2_cross_process_invalidate` | ✅ |
| S1 | UC1-3 | `test_t2_repo_scope_with_prefix` / `test_t2_repo_scope_owner_without_prefix` | ✅ |
| S1 | UC1-2（上传进程自身即时失效） | 上传路径 `mem_cache_invalidate(object_key)` 直调（P49 代码）+ `test_upload_invalidates_caches`（既有套件） | ✅ |
| S2 | UC2-1 / UC2-2 | `test_t3_scope_isolation` | ✅ |
| S3 | UC3-1 / UC3-2 | `test_t5_cache_without_hash` / `test_t5_db_empty_hash` / `test_t5_db_exception` | ✅ |
| S4 | UC4-1 | `test_t7_official_regen` | ✅ |
| S4 | UC4-2 / UC4-3 | 既有套件（invalidate 链路 / zip 缓存层未改） | ✅ |
| S5 | UC5-1 / UC5-2 | `test_t4_convert_hash_reuse` | ✅ |
| S5 | UC5-3 | `test_t4_convert_dst_overwrite_no_restart` | ✅ |
| S5 | UC5-4（convert 后源/目标按新 key 失效） | `test_t4_convert_hash_reuse`（dst_key invalidate 口径）+ 代码直调 | ✅ |
| S8 | UC8-3（F3 联合） | `test_t6_multi_owner_same_name` | ✅ |
| P1-1 | repo 全 provider 前缀（github:/gitlab:/gitcode:/无前缀） | `test_p1_1_all_provider_prefixes` / `test_p1_1_repo_row_lookup_helper` | ✅（含 MySQL 真机复核） |
| P1-1 | 跨 provider 同键共存安全退化 | `test_p1_1_cross_provider_same_key_coexistence`（6.7c） | ✅ |
| P3-1 | UC4-2 删除两层缓存清除 | `test_p3_1_invalidate_skill_caches_both_layers` | ✅ |
| P3-1 | UC9-1 删除后降级语义（DB 门守卫） | `test_p3_1_deleted_row_degrades_not_stale` | ✅ |
| S12 | UC12-1（provider 键隔离 + 覆盖感知） | `test_s12_uc12_1_cross_provider_isolation` | ✅（根治验收；含真实 SQLite 端到端复验） |
| S12 | UC12-2（首读前覆盖回源正确） | `test_s12_uc12_2_overwrite_before_first_read` | ✅ |
| S12 | UC12-3（多行互不串扰不抖动） | `test_s12_uc12_3_multi_row_hit_stable` | ✅ |
| S12 | UC12-4（repo 冷读 sid 锚定；旧格式 sid=0 对照） | `test_s12_uc12_4_repo_cold_read_anchored` | ✅（根治验收） |
| S12 | UC12-5（存量旧格式键兼容） | `test_s12_uc12_5_legacy_key_compat` | ✅（单测 + 真实 SQLite 复验） |
| S12 | UC12-6（写路径 strict 消歧 400） | `test_s12_uc12_6_strict_repo_ambiguity`（真实 SQL 四路径） | ✅ |
| 收尾 P-A | UC12-7（物化链路读时校验） | `test_pa_zip_bytes_cross_process_invalidate` / `test_pa_zip_bytes_degrade_semantics` | ✅ |
| 收尾 P-B | UC12-8（upload 409 存储回滚） | `test_pb_upload_409_restores_previous_object` | ✅ |
| 收尾 P2-2 | UC12-9/10/11（路由级核心序列测试） | `test_route_upload_409_restores_previous_object` / `test_route_convert_overwrite_cross_process` / `test_route_upload_new_key_and_sid_flow` | ✅（第十轮） |
| 收尾 P2-1 | UC12-12（OBS E2E 脚本签名对齐） | scripts/verify_obs_skill_e2e.py 两参调用（AST 校验） | ✅（维护性修复，需 OBS 环境跑） |
| 根治 | repo 键模型含 provider 段 | `test_p1_1_all_provider_prefixes`（新格式精确查询 + 旧格式兼容分支） | ✅ |
| P47 | upsert 写 hash | `test_t1_write_side_hash` / `test_upsert_failure_returns_none` | ✅ |
| S6/S7/S9-S11 | UC6/UC7/UC9-UC11 | 环境部署类，目标环境执行（§6.9） | ⏳ 部署后验收 |
| S8 | UC8-1（F1）/ UC8-2（F2） | 已另立 issue #447 / #448 | 🔜 另单 |
