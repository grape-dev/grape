# B1 技能库 OBS zip 化 —— 最新设计方案（v3.0）

> v3.0（2026-08-14 评审）：**worker 技能获取改直连 OBS（模式 B）**——
> ⑧ 任务不再内嵌 zip 快照（**移除本次新增的 skill_zip 列**，无包袱直砍）；
> worker 直连 OBS（claim 现签预签名 URL）+ 失败降级服务端代拉
> （复用服务端 zip_cache）+ 可达性自适应（启动探测 B + 运行时能力反馈 C）；
> 老 worker 兼容走 skill_files（文本物化，既有机制保留）。
> 变更点：D5/D9/D10 决策、N10/N11、§5.7、§11、§12。
> 覆盖场景：官方同名覆盖被 R1 禁止 → 不存在覆盖者歧义（R2 只用 created_by）。

> 文档编号：GRP-DESIGN-20260813-02
> 版本：v3.0（固定 key 覆盖 + file_path/by 字段 + 官方管理闭环 + worker 直连 OBS/代拉）
> 日期：2026-08-13
> 范围：B1 技能库（上传、详情、转换、提升、模板下载、任务下发联动、worker 物化）
> 关联：`obs-object-key-spec.md`（对象键规范）、`obs-skill-storage-review.md`（v1 评审稿，已替代）、
>       `obs-storage-requirements.md`（需求）、`obs-storage-design.md`（总体设计，技能章节以本文为准）

---

## 1. 方案概述

**OBS zip 单对象为正体（固定 key 覆盖模型，对象键存 DB）→
服务端无技能目录树，仅两级缓存（磁盘 LRU zip 包 + 内存 LRU 提取结果）→
任务快照内嵌 zip base64（快照快递模式）→ worker 解码解压物化 + 显式恢复权限。**

```
OBS（正体，zip 单对象，固定 key 原子覆盖）
  {PREFIX}/skills/{scope}/{owner}/{skill_name}/{skill_name}.zip
      │
服务端（无技能目录树）
  上传    ：临时解压校验 → 规范化重打包（保权限位）→ 传 OBS 固定 key（覆盖）
            → DB upsert + 缓存失效
  详情    ：内存缓存提取结果 → miss 则磁盘 zip 缓存 → miss 则 OBS 下载 →
            内存解压提取（实测 0.05~0.21ms）；接口返回 content + zip_url
  转换/提升：OBS copyObject（固定 key → 固定 key）→ DB 回写新 key
  删除    ：OBS deleteObject 单对象 + DB + 清缓存
  enqueue ：经 OBS 地址 getObject 取 zip（磁盘缓存命中零下载）
            → base64 编码 → 写入快照 skill_zip 字段
      │
任务快照（共享 MySQL，快递载体）：skill_zip（zip base64，自包含）
  skill_files 双写过渡（老 worker 兼容）
      │
worker 物化：从快照取 skill_zip → base64 解码还原 zip 二进制
  → 写临时 zip → extractall → 手动恢复权限
  （external_attr>>16 & 0o777，剥离 setuid/setgid/sticky 防提权）
      │
opencode：DEVOPS_SKILLS_DIR 加载（官方规范结构：SKILL.md + references/ + scripts/）
```

### 1.1 核心决策（多轮讨论已对齐）

| # | 决策 | 理由 |
|---|------|------|
| D1 | OBS 存 zip（非目录树） | 单对象原子、可分享、权限随 external_attr 保留、二进制无损 |
| D2 | 无本地目录树（节点无技能状态） | 解耦；仅两级可丢缓存，正确性不依赖缓存 |
| D3 | **固定 key 覆盖（无版本段、无随机段）** | 多集群/多 worker 一致性：putObject 同 key 原子覆盖，读取即最新版，无 v1/v2 歧义；key 可预测但访问受临时签名 URL 保护；DB 为唯一事实源 |
| D4 | 快照内嵌 zip base64（非解压文本 JSON） | 服务端零解压、二进制/权限完整；修复现状"文本快照丢二进制丢权限"缺陷 |
| D5 | worker 物化显式恢复权限 | Python 3.12+ zipfile 不再自动恢复（安全移除），实测验证需手动 chmod |
| D6 | OBS_ENABLED=false 全链路行为与现状一致 | 默认关闭零影响，开关回退 |
| D7 | 缓存介质本地磁盘 + 进程内存，不引 Redis | zip 是可丢大二进制；Redis 的共享/一致性能力无用武之地 |
| D8 | 模板下载/技能下载：接口返回临时签名 URL（{url, expires_in}），前端点击下载时动态获取 | 符合"下载经 OBS 获取临时文件下载地址"需求；每次点击动态签发、短时效（300s），URL 不落库不持久化 |
| D9 | worker 文件获取 = 直连 OBS（模式 B）+ 服务端代拉 | worker 不持有 AK/SK：claim 时服务端现签预签名 URL（TTL 30min）→ worker 直连 OBS GET；失败 → 服务端代拉（复用 zip_cache）；可达性自适应（启动探测 + 运行时能力反馈） |
| D10 | 任务不内嵌技能 zip（DB 瘦身） | skill_zip 为本次需求新增列、未上线——无包袱直砍；任务只带 skill_key + 预签名 URL（claim 现签）；老 worker 兼容走 skill_files |
| D11 | 重复上传 = 覆盖更新（最后写者胜） | user 技能 owner 段不同天然隔离；repo 同名覆盖不限角色（评审确认 A）；official 管理面覆盖更新 |

---

## 2. 对象键与存储规范

### 2.1 对象键结构（固定 key，DB 存储，非现算）

```
{PREFIX}/skills/{scope}/{owner}/{skill_name}/{skill_name}.zip

{PREFIX}      环境前缀，默认 grape/prod（OBS_PREFIX 可配）
{scope}       custom（个人）/ repo（仓库）/ official（官方，预留）
{owner}       user_id（custom）或 owner/repo 两级（repo，不转义）
{skill_name}  技能名（^[\w.\-]+$）
```

- **固定 key 覆盖**：首次上传 putObject 创建，之后上传**覆盖同一 key**
  （OBS putObject 同 key 覆盖原子，无半写状态）；无版本段、无随机段。
- **key 可预测性**：文件名可枚举，但对象访问必须持临时签名 URL（300s，
  每次点击动态签发）——私有桶 + 签名 URL 双保护，可预测 key 不泄露内容。
- **DB 为事实源**：完整对象键存 skill 表 skill_path 字段（复用）；
  上传/转换/提升/删除全部按 DB 中的 key 操作。
- 目录段可读（运维/管理端 listObjects 友好），文件名固定 = 技能名。

完整示例：
```
grape/prod/skills/custom/123/hello-skill/hello-skill.zip
grape/prod/skills/repo/Jason7602/grape/my-skill/my-skill.zip
grape/prod/skills/official/incident-triage/incident-triage.zip
grape/prod/skills/template/skill-template.zip   （模板，固定 key，可覆盖）
```

### 2.2 zip 内标准结构（上传时统一规范化重打包）

```
{skill_name}/
  SKILL.md              ← 根目录，必需
  references/**         ← 可选子目录
  scripts/**            ← 可选，保留可执行权限位
  assets/**             ← 可选，二进制无损
```

打包规则：根 = {skill_name}/；逐文件写 external_attr（st_mode << 16）保权限；
丢弃 __MACOSX / .DS_Store / .git；跳过 symlink（罕见，规范明示）。

### 2.3 动态片段安全约束（拼 key 前必过）

user_id `^\d+$`；owner/repo 各段 `^[\w.\-]+$`；skill_name `^[\w.\-]+$`；
scope 固定枚举。对象访问安全依赖**临时签名 URL + 私有桶**（§7）。

---

## 3. 改动点详细清单（N1~N20）

> 改动点编号与实施里程碑对应（§10）。"改前/改后"以行为描述，"关键点"为实现要点与风险。

### N1 src/config.py —— OBS 配置项

- 位置：config.py 新增段（目录常量附近）
- 改前：无 OBS 相关配置
- 改后：新增 8 项（环境变量读取，与现有 DEVOPS_* 风格一致）
  ```python
  OBS_ENABLED     = os.environ.get("OBS_ENABLED", "false").lower() in ("1","true","yes")
  OBS_ENDPOINT    = os.environ.get("OBS_ENDPOINT", "https://obs.cn-north-4.myhuaweicloud.com")
  OBS_BUCKET      = os.environ.get("OBS_BUCKET", "")
  OBS_ACCESS_KEY  = os.environ.get("OBS_ACCESS_KEY", "")   # 仅环境变量，gitignored
  OBS_SECRET_KEY  = os.environ.get("OBS_SECRET_KEY", "")   # 仅环境变量，gitignored
  OBS_PREFIX      = os.environ.get("OBS_PREFIX", "grape/prod")
  OBS_URL_EXPIRES = int(os.environ.get("OBS_URL_EXPIRES", "300"))
  OBS_LOCAL_CACHE = os.environ.get("OBS_LOCAL_CACHE", "")  # 默认 data/cache/storage
  ```
- 关键点：AK/SK 不入配置文件、不入日志；配置缺失/不全 = 视为未启用（get_storage 返回 None）

### N2 src/storage/__init__.py（新增）—— 存储抽象 + 缓存 + key 生成

- 位置：新文件 src/storage/__init__.py
- 改前：无存储抽象层，业务代码直连本地路径
- 改后：
  - `StorageBackend` 抽象：save_bytes / save_stream / read_bytes / read_file /
    exists / list_keys / delete_prefix / temporary_url / backend_name
  - `get_storage()` 单例：OBS_ENABLED 且配置齐 → ObsStorage；否则 **None**
    （None 时业务走原本地流程，OBS_ENABLED=false 行为零变化）
  - 磁盘 LRU zip 缓存：key = 技能标识（对象键派生），容量上限 2GB、
    每技能保留 1 版；get/put 原子（临时文件 + os.replace）
  - 内存 LRU 提取结果缓存：key = skill_name → SKILL.md 文本，
    上限 50MB；上传/转换/删除时主动失效
  - `skill_key(scope, owner, skill_name)` → 固定对象键（唯一生成入口，
    内部拼 {PREFIX}/skills/{scope}/{owner}/{name}/{name}.zip，无版本段/随机段）
  - 关键点：缓存全部"可丢"（miss 从 OBS 重拉，正确性不依赖缓存）；
    覆盖上传后主动失效 mem_cache + zip_cache（无版本号天然失效机制，
    必须显式失效——见 N4）

### N3 src/storage/obs.py（新增）—— ObsStorage 实现

- 位置：新文件 src/storage/obs.py
- 改前：无
- 改后：基于 esdk-obs-python（import obs，已实测 3.26.6 可装可导入）
  ```python
  from obs import ObsClient
  class ObsStorage(StorageBackend):
      def __init__(self, endpoint, bucket, access_key, secret_key, prefix, default_expires):
          self.client = ObsClient(access_key_id=..., secret_access_key=..., server=endpoint)
      # save_stream: putObject(bucket, key, filepath=source)
      # read_file:   getObject → 落 local_path
      # read_bytes:  getObject → content
      # copy_object: copyObject(bucket, src, bucket, dst)
      # delete_prefix: listObjects(prefix) → deleteObjects 批量
      # temporary_url: createTemporarySignature(method="GET", bucket, key, expires)
  ```
- 关键点：SDK 方法名以实际版本为准（M0 验证）；全部操作按 DB 中的对象键；
  临时 URL 只签 GET；错误统一包装为 StorageError（业务侧 502/503 映射）

### N4 src/api/routes/skills.py upload_skill —— 上传 OBS 化

- 位置：upload_skill（现 :158-243）
- 改前：收 zip → 临时解压 → 校验 → shutil.copytree 到本地 skills/custom|repo
- 改后（OBS 分支，storage 非 None 时）：
  1. 现有校验全保留（10MB、ZIP 魔数、zip slip、front matter、agent_type）
  2. 新增：解压后总大小 ≤100MB（zip 炸弹防护）
  3. 规范化重打包：根 = {skill_name}/，逐文件 external_attr = st_mode<<16
     保权限，丢弃 __MACOSX/.DS_Store/.git，跳过 symlink
  4. 固定 key：skill_key() 生成（无版本段/随机段）；putObject 原子覆盖
     （同 key 已存在 = 覆盖更新，最后写者胜；user 不同 owner 段天然隔离）
  5. storage.save_stream(key, 打包后的临时 zip)
  6. DB upsert：file_path 写固定 key（v2.5 字段职责分离：skill_path 恢复
     技能名语义）；created_by/updated_by 写当前登录用户名（OBS 与 local
     分支都写；v2.5 全量审计）
  7. 写磁盘 zip 缓存 + **显式失效** mem_cache（zip_cache put 覆盖同名 key
     即天然失效）
  - 关键点：覆盖无半写状态（putObject 原子）；已入队任务 skill_zip 快照
    自包含不受覆盖影响；并发上传后写胜（评审确认）
- 改后（local 分支）：行为与现状完全一致（copytree 原逻辑）
- 关键点：DB 记录与 OBS 对象的一致性（DB 先行 + 失败回滚）；重打包是
  上传链路的唯一新增成本（≤10MB，可忽略）

### N5 skills.py get_skill_detail —— 详情 OBS 化

- 位置：get_skill_detail（现 :111-153）
- 改前：按 scope+owner+name 拼本地路径 → 读 SKILL.md 文本
- 改后（OBS 分支）：
  1. 内存 LRU（skill_name+version）命中 → 直接返回（<1ms）
  2. miss：磁盘 zip 缓存 → miss：按 DB key read_file 拉 OBS zip 落缓存
  3. zipfile 内存读取 SKILL.md entry（实测 0.05~0.21ms，非全量解压）
  4. 写回内存 LRU
  5. 响应新增 zip_url 字段：storage.temporary_url(key)（渐进支持前端消费）
- 改后（local 分支）：不变
- 关键点：按 entry 提取不解压全部；提取结果缓存是热路径性能保证；
  zip_url 为新增字段，旧前端忽略即可（兼容）

### N6 skills.py convert_skill —— 转换 OBS 化

- 位置：convert_skill（现 :262-313）
- 改前：shutil.copytree(skills/custom/{uid}/{skill} → skills/repo/{repo}/{skill})
- 改后（OBS 分支）：
  1. 读源技能 DB 记录（scope=user）拿 key
  2. copyObject(源 key → 目标固定 key：repo/{owner}/{repo}/{skill}/{skill}.zip)
  3. DB：目标仓库技能记录创建（skill_path=目标固定 key）
  4. 写目标 zip 缓存
- 改后（local 分支）：copytree 原逻辑不变
- 关键点：copyObject 是 OBS 服务端复制（零下载零上传）；源保留（复制语义）；
  目标仓库同名冲突沿用现有 DB 唯一约束校验

### N7 skills.py delete_skill —— 删除 OBS 化

- 位置：delete_skill（现 :245-254）+ user/db.py delete_skill
- 改前：rmtree 本地目录 + DB 删除
- 改后（OBS 分支）：deleteObject(按 DB 中的固定 key 单对象) + DB 删除 +
  失效两级缓存
  ★ 固定 key 模型下每技能仅 1 个对象，删除 = 单对象删除（无需按前缀枚举；
  v2.3 版本的"按前缀清所有版本"逻辑随版本化一并废弃）
- 改后（local 分支）：rmtree 原逻辑
- 关键点：先 DB 后删对象（删对象失败仅告警，DB 已删可接受，对象由
  运维侧清理脚本兜底）；缓存失效顺序在 DB 删除后

### N8 skills.py download_skill_template —— 模板下载返回临时 URL

- 位置：download_skill_template（现 :84-108）
- 改前：本地 _template 目录打包 → FileResponse 文件流
- 改后（OBS 分支）：
  1. 本地 _template 打包（不变）
  2. 传 OBS 固定 key：{PREFIX}/skills/template/skill-template.zip（覆盖写）
  3. 接口返回 **{url, expires_in}**：storage.temporary_url(key) 动态签发
     （每次请求新 URL，300s 有效；token 经 query 鉴权，与现状一致）
- 改后（local 分支）：FileResponse 原逻辑（或同样返回临时 URL 由 LocalStorage
  签发内部端点，保持调用方一致——实现时二选一，推荐 local 返回原文件流）
- 关键点：符合"下载经 OBS 获取临时文件下载地址"需求；前端点击下载时
  动态获取地址（见 N16）；URL 短效、不落库不持久化

### N9 src/user/db.py —— 技能 DB 适配

- 位置：create_skill / delete_skill / get_skill_path / skill 相关查询
- 改前：skill_path 存 skill_name/空；delete 删本地目录；无版本概念
- 改后：
  - skill_path 语义恢复：local 模式存技能名（拼目录）；OBS 模式也存技能名
    （不再存对象键）
  - **file_path**：OBS 模式存完整固定对象键；local 模式留空
  - get_skill_object_key：直接返回 file_path（非空即对象键——不再特征判断）
  - get_skill_path：local 拼目录逻辑不变（skill_path 原语义）
  - create_skill/upsert：OBS 与 local 分支都写 created_by/updated_by
    （上传者用户名；seed 不写）
  - delete_skill：OBS 模式不删本地目录（无目录），按 file_path 删对象
  - **移除 current_version 相关函数**（v2.4 去版本化：get_current_version /
    bump_current_version 等不再需要；skill 表 current_version 列 DROP，见 N19）
- 关键点：函数签名向后兼容（local 模式调用方零改动）

### N10 src/runtime/service.py enqueue_task —— 任务下发（去 skill_zip，skill_files 双写保留）

- 位置：enqueue_task（现 :257-331）+ _pack_skill_dir
- 改前：skill_zip = base64(zip) 内嵌快照（v2.6 快照快递）
- 改后（v3.0）：
  ```python
  # 1) skill_files 双写保留（老 worker 兼容通道——文本物化）
  skill_files = _pack_skill_dir(skill, path)   # 本地目录或按需 OBS 拉取
  # 2) skill_zip 移除：不再打包内嵌（DB 瘦身）
  # 3) 对象键随任务：enqueue 时 get_skill_object_key 写入任务（可选）
  #    ——claim 时服务端现查 file_path 现签 URL（权威、永远最新）
  ```
- INSERT：_TASK_COLUMNS 去 skill_zip 列（已改）
- 关键点：任务不再内嵌 zip——worker 取技能走 claim 返回的
  skill_obs_url（直连）/ 代拉接口（降级）；老 worker 用 skill_files

### N11 src/runtime/agent.py —— worker 技能获取（直连 OBS + 降级链）

- 位置：_execute_task 技能物化段（现 :125-165）
- 改后（v3.0 降级链，worker 侧）：
  ```
  ① 任务带 skill_obs_url（claim 响应）→ worker GET 直连 OBS
     （超时 10s × 重试 3）→ 成功 → 解压物化（复用现有 extractall +
     权限恢复）✓
  ② URL 失败（worker 侧 OBS 不可达/过期）→ 调服务端代拉
     POST /tasks/{id}/skill-zip → base64 → 物化 ✓
     （服务端复用 zip_cache：命中零 OBS / miss 拉 OBS 更新缓存）
  ③ 代拉也失败 → 放弃任务回池（attempts+1）→ 下次 claim 重签 URL
     → 回 ①；attempts≥3 → failed
  ④ 无 URL（老 worker / 无 OBS 技能）→ skill_files 文本物化（既有）
  ```
- 能力协商：register 响应带 probe_url（启动探测 GET 5s）→
  声明 supports_obs_url；运行时连续 3 次走到 ② → claim 声明
  prefer_fallback=true（服务端任务直接 mode=server-fetch）；
  连续 3 次直连成功 → 恢复
- 关键点：worker 无 AK/SK（预签名 URL）；降级链覆盖全部失败场景；
  老 worker 零改动（skill_files 路径不变）
- 物化细节（zip 来源无论直连/代拉，同一逻辑）：
  extractall 到 cwd/.skills → 按 external_attr 恢复权限（0o777
  剥离 setuid/sticky 防提权；Python 3.12+ 手动 chmod）
  → env["DEVOPS_SKILLS_DIR"]=skill_dir（不变）
- 无 zip（老 worker / 无 OBS 技能）→ skill_files 文本物化（既有逻辑）

### N12~N15 admin（Java）—— 技能链路直连 OBS

- N12 SkillService.uploadSkill：
  - **官方同名禁止（R1）**：official 分支先查 scope='official' 同名 →
    已存在则 400「官方技能已存在同名「xxx」」（user/repo 不受限；
    **更新一律走独立接口 POST /{id}/update，upload 仅新增**）
  - 解压校验 → 规范化重打包（Java ZipOutputStream 写 external_attr 保权限）
    → OBS putObject 固定 key（原子覆盖，无版本）→ DB 写 file_path=对象键
    + created_by/updated_by=当前登录用户名（OBS 与落盘分支都写 by；
    落盘分支 file_path 留空）
- N13 SkillService.promoteToOfficial：copyRecursively+deleteRecursively →
  OBS copyObject（源固定 key → official 固定 key）→ DB scope/key 更新
  （updated_by=操作者，created_by 保留）；**未转存源（file_path 空）→
  自动转存**：打包上传 official 固定 key + file_path 回写对象键
  （与更新接口 B 方案一致；文件不在本机 warning 场景跳过转存）
- N14 SkillService.getSkillContent：读本地 SKILL.md → OBS 下载 zip →
  ZipInputStream 内存提取 SKILL.md（读 file_path）
- N15 新增 SkillService.deleteSkill（DELETE /api/admin/skills/{id}）：
  - 校验：scope='official' 且（**超管 OR created_by==当前登录用户**）→
    否则 403；**系统内置/存量（created_by=''）永不可删**（R3/R6）
  - 超管判定：userId → admin_user_role_rel → 角色 code=='SUPER_ADMIN'
  - 动作：OBS 对象（file_path 非空时）单对象删除 + DB 记录删除
  - 前端确认弹窗（设备管理样式 ⚠️ + 不可回退 + loading 态）
- N15b 新增 SkillService.updateSkill（POST /api/admin/skills/{id}/update）：
  - **独立更新接口**（不复用 upload）：校验 scope='official'
  - 权限：超管 OR created_by==当前用户 → 否则 403
  - **zip 目录名校验**：包内目录名 == 技能名 → 否则 400
    「技能包内目录名与技能名不匹配」
  - 内容更新：落盘覆盖 skills/{name}/ + **OBS 同步（B 方案：始终上传
    OBS——已转存覆盖对象；未转存自动转存 + file_path 回写对象键）**
  - DB：updated_by=操作者（created_by 保留）
- N15c pom.xml：新增 esdk-obs-java 依赖（华为 maven 镜像
  https://repo.huaweicloud.com/repository/maven/）
- 关键点：与主服务同一对象键规范（skill_key 规则 Java 侧复刻一份，
  两侧对照测试）；DB 直写共享 MySQL（现状即如此）；无主服务 API 中转
  （主服务技能接口为 GitCode OAuth 用户态，admin 无法复用）；admin 新增
  to-cluster（官方技能转集群）与 download（临时 URL）接口复用同一固定 key 规范；
  isObjectKey 改判 file_path 非空（v2.5）

### N15d ObsStorageClient 异常处理规范（v3.1 补充）

> 背景：admin Java 侧 ObsStorageClient 各方法对 OBS SDK 异常（`ObsException`）
> 捕获不一致——`readBytes`/`saveBytes` 未 catch OBS 异常导致裸抛 500；
> `temporaryUrl` catch 后转 `RuntimeException` 仍被全局兜底拦截成 500；
> `normalizeSkillZip` 声明 `throws IOException` 但调用方未 catch。
> 2026-08-30 技能 id=2002（file_path 指向不存在的 OBS 对象）触发 `getObject` 404 →
> `ObsException` 裸抛 → `GlobalExceptionHandler.handleOther` 返回 500
> "服务器内部错误"（无任何业务信息），暴露此缺陷。

#### 异常映射规范

| ObsStorageClient 方法 | OBS 失败场景 | 期望 HTTP 状态码 | 响应体 message |
|----------------------|-------------|-----------------|---------------|
| `readBytes(key)` | 对象不存在（404） | 404 | `技能文件不存在（OBS 对象缺失）：{key}` |
| `readBytes(key)` | OBS 不可达/权限/其他 | 502 | `OBS 读取失败：{key}` |
| `saveBytes(key, data)` | putObject 失败（任何原因） | 502 | `OBS 写入失败：{key}` |
| `temporaryUrl(key)` | 签名失败 | 502 | `OBS 临时 URL 签发失败：{key}` |
| `normalizeSkillZip(dir, name)` | IO 失败（磁盘/文件） | 500 | `技能打包失败：{skillName}` |

#### 实现规则

1. **ObsStorageClient 层**：每个 OBS 操作方法 catch `ObsException`（OBS SDK 根异常），
   按"对象不存在 vs 其他"区分：
   - 对象不存在（HTTP 404 / `obs_object_not_found`）→ 抛 `NotFoundException`
   - 其他 OBS 错误 → 抛 `BizException`（502 语义，由 GlobalExceptionHandler 映射）
   - `saveBytes`：putObject 无 404 语义，统一 catch → `BizException("OBS 写入失败")`
   - `temporaryUrl`：签名失败统一 catch → `BizException("OBS 临时 URL 签发失败")`

2. **SkillService 调用方**：
   - 调 `normalizeSkillZip`（throws IOException）处须 try-catch，转 `BizException`
   - 调 `readBytes`/`saveBytes`/`temporaryUrl` 不需额外 try-catch（ObsStorageClient
     已转为业务异常，由 GlobalExceptionHandler 统一映射）

3. **GlobalExceptionHandler 不变**：已有 `NotFoundException → 404`、
   `BizException → 400`、`Exception → 500` 三级映射；
   新增 `BizException` 可带 HttpStatus 参数以区分 400/502，或新增
   `ObsException` 专用 handler（推荐前者，最小改动）

4. **已有的安全方法不变**：`copyObject`/`deleteObject`/`deletePrefix`
   已有 catch(Exception) 返回 false/0，不抛异常（调用方判断返回值处理），
   保持现状

- 关键点：OBS 404 → 业务 404（不是 500）；OBS 故障 → 502（不是 500）；
  避免用户看到无信息量的"服务器内部错误"；异常 message 带 key 便于运维排查

### N16 前端 src/api/static/js/ —— 模板下载适配 + 可选增强

- 改前：downloadSkillTemplate() 直接 window.open('/api/skills/template?token=...')
  拿文件流
- 改后：**模板下载函数适配（一处）**：
  ```js
  async function downloadSkillTemplate() {
    const r = await API.get('/skills/template?token=' + localStorage.getItem('token'));
    // r = {url, expires_in} —— 临时签名 URL（用户点击时动态签发）
    window.open(r.url, '_blank');   // 直连 OBS 下载
  }
  ```
  上传/转换/删除/列表/详情：零改动（接口契约不变）
- 可选增强（评审点 8 待定）：详情页"下载技能 ZIP"按钮 —— 同模式：
  点击 → 调接口（或复用详情返回的 zip_url）→ 动态签发临时 URL → 下载；
  loading 态
- 关键点：下载地址一律"用户点击时动态获取"，短时效、不持久化；
  若做"下载技能 ZIP"按钮，接口复用 N5 的 zip_url 或单独签 URL 端点

### N17 scripts/migrate_skills_to_fixed_key.py（新增）—— 存量迁移 + 历史多版本收敛

- 位置：新文件 scripts/migrate_skills_to_fixed_key.py
- 改前：无
- 改后（两个职责）：
  1. **本地存量迁移**（OBS_ENABLED=true 场景）：枚举本地 skills/custom/、
     skills/repo/ 目录 → 逐技能按 N4 同款规范化打包 → skill_key() 生成固定
     key → 上传 OBS → DB 回写（file_path=key，v2.5；skill_path 保持技能名）
  2. **历史多版本收敛**（v2.3 版本化存量清理）：对每个 DB 技能记录，
     取当前对象（v{n}/...zip）内容 → putObject 固定 key（保留当前内容）
     → 删除该技能所有 v{n} 历史对象 → DB 回写（file_path=固定 key；
     skill_path 回写技能名——v2.5 字段职责分离）
  3. **存量 created_by/updated_by**：保持 ''（R6——不可删，仅 SQL 管理；
     重新上传后归属新用户）
  4. 支持 --dry-run（只列清单）、--delete-after（sha256 校验通过后删本地/历史对象）
- 关键点：先测试桶验证再部署桶；迁移后本地目录保留为"缓存"（删除需显式
  --delete-after）；幂等（已迁移技能跳过，按 DB file_path 非空判断）

### N18 requirements.txt —— 依赖

- 改后：新增 esdk-obs-python（仅主服务清单）；worker requirements-worker.txt
  不引入（worker 只用标准库 zipfile/base64）

### N19 src/api/db_setup.py —— DDL

- 改后：_AGENT_TASKS_DDL 加 skill_zip 列、skill_files 类型调整；存量库
  ALTER（SQLite + mysql_compat 各一条，幂等）；**skill 表 DROP COLUMN
  current_version**（v2.4 去版本化；该列为本期新增，无历史数据依赖）
  （详见 §4）

### N20 tests/ —— 测试

- 新增 tests/test_storage.py（StorageBackend 契约、zip 往返含权限、缓存 LRU、
  key 生成与白名单）；tests/test_skills_obs.py（skills.py OBS 分支全流程，
  mock ObsStorage）；回归 OBS_ENABLED=false 全量（基线 713 用例）

---

## 4. 表结构改动

### 4.1 skill 表（v2.4 去版本化 + v2.5 新增 3 列）

```sql
-- v2.4：去版本化（SQLite/MySQL 迁移，幂等：列不存在跳过）
ALTER TABLE skill DROP COLUMN current_version;

-- v2.5：真实存储路径 + 创建/更新人审计
ALTER TABLE skill ADD COLUMN file_path  VARCHAR(512) DEFAULT '';  -- OBS 对象键（真实存储路径）；local 留空
ALTER TABLE skill ADD COLUMN created_by VARCHAR(64)  DEFAULT '';  -- 创建人（登录用户名）；seed 留空
ALTER TABLE skill ADD COLUMN updated_by VARCHAR(64)  DEFAULT '';  -- 最后操作人（转存/提升/覆盖时更新）
```

语义（v2.5 字段职责分离）：
- **skill_path**：恢复原始语义（技能标识/目录名，local 拼目录用），不再存对象键
- **file_path**：真实存储路径——OBS 模式存对象键；local 模式留空
- **obsMode 判定**：file_path 非空即 OBS 模式（不再特征判断 skill_path）
- **created_by/updated_by**：上传者（登录用户名）/最后操作人；系统内置 seed 留空
- 存量迁移：现有 skill_path=对象键的记录 → file_path=对象键 + skill_path 回写技能名；
  存量 created_by/updated_by 保持 ''（R6：不可删，仅 SQL 管理）

### 4.2 agent_tasks 表（+1 列、修 1 列）

```sql
-- SQLite
ALTER TABLE agent_tasks ADD COLUMN skill_zip TEXT DEFAULT '';
-- MySQL：必须 MEDIUMTEXT（zip base64 最大 13.3MB，TEXT 64KB 装不下）
ALTER TABLE agent_tasks ADD COLUMN skill_zip MEDIUMTEXT DEFAULT '';

-- MySQL：现状隐患修复（skill_files 大技能解压文本 >64KB 写不进）
ALTER TABLE agent_tasks MODIFY skill_files MEDIUMTEXT DEFAULT '{}';
```

**skill_zip 字段作用**：任务快照内嵌 zip base64 的载体。服务端 enqueue 时经
OBS 地址取 zip → base64 存入本列 → worker 从快照取回解码解压。这就是
"快照快递"模式中 zip 二进制进 DB 的容器。

**skill_files 字段作用**：旧格式（解压后文本 JSON），双写过渡期保留，
老 worker（未升级）仍读它；过渡期结束后退役删除。

### 4.3 双后端迁移

- 新建库：DDL 直接带新列（改 _SKILL_DDL / _AGENT_TASKS_DDL）；
- 存量库：ALTER TABLE ADD COLUMN（项目已有迁移机制，SQLite + mysql_compat 各一条，幂等：列存在跳过）。

---

## 5. 流程改动（现状 → 改造后）

### 5.1 上传
```
现状：收 zip → 解压 → 校验 → copytree 到本地 skills/custom|repo/
改造：收 zip → 临时解压 → 校验（zip slip/炸弹≤100MB/front matter）
    → 规范化重打包（根={skill}/、external_attr 保权限、弃系统文件）
    → 传 OBS 固定 key（putObject 原子覆盖，无版本段）
    → DB upsert（skill_path=固定 key）
    → 写磁盘 zip 缓存 + 失效内存提取缓存
```

### 5.2 详情
```
现状：按 scope+owner+name 拼本地路径 → 读 SKILL.md 文本
改造：内存 LRU 提取结果命中 → 直接返回（<1ms，与现状一致）
    → miss：磁盘 zip 缓存 → miss：OBS 下载 → 内存解压提取（实测 0.05~0.21ms）
    → 返回 content + zip_url（新字段，渐进支持前端消费）
```

### 5.3 转换 / 提升
```
现状：shutil.copytree 个人→仓库 / admin copy+delete → 官方
改造：OBS copyObject（源固定 key → 目标固定 key，无版本段）→ DB 回写新 key
     （提升：source 前缀 → official 前缀 + DB scope/key 更新）
```

### 5.4 删除
```
现状：rmtree 本地目录 + DB 删除
改造：OBS deleteObject（按 DB file_path 固定 key 单对象）+ DB 删除 + 清两级缓存
     （v2.3 的"按前缀清所有版本"废弃——固定 key 模型每技能仅 1 对象）
admin 官方删除（v2.6）：scope='official' 且（超管 OR created_by==当前登录用户）
     → 单对象删除（file_path 非空时）+ DB 记录；内置/存量（created_by=''）
     永不可删（R3/R6）；超管判定走 admin RBAC（SUPER_ADMIN 角色）
```

### 5.5 官方技能更新（v2.6 新增）
```
入口：admin 管理台「更新」按钮（✏️，仅超管或创建者可见）→ 更新弹窗选 zip
接口：POST /api/admin/skills/{id}/update（独立于 upload——upload 仅新增）
流程：
  1. 校验 scope='official' + 权限（超管 OR created_by==当前用户，403）
  2. 解压 zip → 包内目录名 == 技能名（否则 400「技能包内目录名与技能名不匹配」）
  3. 校验（validateCustomSkill 宽松规则）→ 落盘覆盖 skills/{name}/
  4. OBS 同步（B 方案）：始终上传 OBS——已转存覆盖对象（file_path 保留）；
     未转存自动转存（生成固定 key + saveBytes + file_path 回写对象键）
  5. DB：updated_by=操作者（created_by 保留）
效果：官方技能内容更新后必定进 OBS（多集群统一分发），转存标记不丢
```

### 5.6 模板下载
```
现状：window.open 直取 FileResponse 文件流
改造：OBS 模式 → 本地 _template 打包传 OBS → 接口返回 {url, expires_in}
      临时签名 URL → 前端点击下载时动态获取地址、直连 OBS 下载；
      local 模式返回原文件流
```

### 5.6 任务下发（worker 拿文件的核心链路，快照快递）
```
现状：服务端遍历本地技能目录 → 读文本 → {rel: 文本} JSON → skill_files 列
     → worker 从快照取 → 逐文件写文本
改造：服务端经 OBS 地址 getObject 取 zip（磁盘缓存命中零下载）
     → zip 二进制 → base64 → skill_zip 列
     → worker 从快照取 → base64 解码还原 zip → 写临时 zip
     → extractall → 恢复权限 → .skills/{skill}/ 完整落地
     （worker 不碰 OBS、不碰服务端文件系统，与现状快递方式一致）
```

### 5.7 worker 物化
```
现状：json.loads(skill_files) → 逐文件写文本（丢二进制、丢权限）
改造：base64 解码 → 写临时 zip → extractall → 手动恢复权限
      （external_attr>>16 & 0o777，剥 setuid/sticky）→ .skills/{skill}/ 完整落地
      → 脚本可执行（实测 deploy.sh 运行成功）
```

---

## 6. 影响点

### 6.1 功能/架构影响

| 维度 | 影响 |
|------|------|
| 存储形态 | 本地目录树 → OBS zip（固定 key 覆盖、原子更新、天然分享） |
| 服务端状态 | 技能目录状态消失 → 仅两级可丢缓存（磁盘 zip LRU 2GB/每技能 1 版 + 内存 LRU 提取结果） |
| 任务协议 | 新增 skill_zip（zip base64，快照快递）；skill_files 双写过渡期后退役 |
| 能力修复 | 二进制/权限/非 UTF-8：从"静默丢失"到"完整保留+显式恢复"（现状缺陷修复） |
| 脚本类技能 | ✅ 已放开（2026-08）：官方规范 scripts/ 文本脚本（.py/.sh/.js 等）允许上传——zip 化权限保留 + worker 物化恢复已验证（端到端实测 run_code.py 上传/详情/清理通过）；仍禁止二进制可执行/平台原生（.exe/.so/.dll/.bin/.jar/.class/.bat），防原生代码注入（validator FORBIDDEN_EXTS） |
| worker 获取方式 | **不变**：快照快递（从快照取内容落地），只是内容从文本变 zip 二进制 |
| 前端 | 模板下载函数适配（fetch 拿临时 URL 再下载，一处）；上传/转换/删除/列表/详情零改动；可选增强（下载按钮/loading，同模式：点击动态签 URL） |
| 官方技能/模板源 | 不变（部署物留本地）；官方技能可经「转存至 OBS」（admin 管理面）上传 OBS 固定 key |
| 重复上传 | 覆盖更新（最后写者胜）：user 不同 owner 天然隔离；repo 同名覆盖不限角色；**官方同名禁止上传**（R1：scope='official' 同名 → 400） |
| 官方删除（v2.6） | official 且（超管 OR created_by==当前登录用户）→ 可删；内置/存量 created_by='' 永不可删（R3/R6）；更新路径 = 独立更新接口（POST /{id}/update，自动转存） |
| 字段语义（v2.5） | skill_path 恢复技能名语义；file_path=OBS 对象键（真实存储路径）；created_by/updated_by=创建人/最后操作人（全量审计，主服务 user/repo 也写） |
| OBS_ENABLED=false | 全链路行为与现状一致 |

### 6.2 性能影响（含实测数据）

| 路径 | 耗时 | 说明 |
|------|------|------|
| 详情-热（内存缓存命中） | <1ms | 与现状一致 |
| 详情-提取 | 0.05~0.21ms | 按 entry 解压，实测亚毫秒 |
| 详情-冷（OBS 下载） | 100KB≈8ms / 1MB≈78ms / 9MB≈700ms（@100Mbps） | 仅首次，前端 loading 兜住 |
| 上传 | 重打包 CPU 开销可忽略（≤10MB） | 与现状校验成本同级 |
| 任务下发 | 磁盘缓存命中零下载零解压；miss 一次 OBS 下载 | 快照 base64 膨胀 1.33×（zip 已压缩） |
| DB 膨胀（快照内嵌代价） | 每任务重复存 zip（≤13.3MB/任务） | 技能低频变化高频复用；任务量增大可演进 key 引用（§12） |
| 缓存介质 | 本地磁盘 + 进程内存，不引 Redis | zip 可丢大二进制；项目架构非目标即不引 Redis |

### 6.3 运维影响（新增）

- OBS 配置：endpoint / bucket / AK-SK（环境变量，gitignored）/ prefix / URL 有效期
- 磁盘缓存容量管理（LRU 2GB / 每技能保留 3 版）
- 存量迁移脚本（N17）
- 安全基线：私有桶、CORS 不开（前端直连仅下载场景可选开放）

### 6.4 明确不受影响

- 官方技能、模板源、DB 主体（除 §4 两列）、worker 执行流程（opencode）、
  admin DB 读写、列表接口、OBS_ENABLED=false 全部行为。

---

## 7. 安全设计

| 项 | 方案 |
|----|------|
| Bucket | 私有 ACL，禁公共读 |
| 密钥 | AK/SK 仅环境变量，不入代码/日志，不落 worker |
| 对象键 | DB 存储 + 动态片段白名单；固定 key 可预测，访问依赖**临时签名 URL（私有桶 + 300s 有效期）**双保护 |
| 临时 URL | 仅 GET，短有效期（默认 300s，可配）；接口返回给前端（{url, expires_in}），每次点击动态签发，不落库不持久化 |
| 权限恢复 | 只取 0o777（剥 setuid/setgid/sticky），防 zip 提权 |
| 上传校验 | 服务端：zip slip、解压后 ≤100MB（炸弹）、front matter、大小 ≤10MB |
| zip 规范 | 统一重打包，弃 __MACOSX/.git/symlink，根结构固定 |

---

## 8. 兼容性与回退

1. 开关回退：OBS_ENABLED=false（默认）→ get_storage()=None → 全链路现状行为；
   缺 AK/SK/bucket 等同未启用。
2. OBS 故障：上传失败明确报错不产生半成品（DB 先行+回滚删对象）；
   详情/任务下发走缓存兜底，冷缓存未命中 503+告警。
3. 老 worker：skill_files 双写过渡（新 worker 优先 skill_zip），过渡后退役双写。
4. 存量数据：迁移脚本（dry-run + sha256 + delete-after），上传前本地为正体，
   上传后 OBS 为正体。
5. v2.3 版本化存量：历史多版本收敛脚本（N17）——保留当前版本内容迁固定 key、
   删除历史版本对象；覆盖模型下无版本回滚（内容更新 = 覆盖，不可回滚，
   与多集群一致性强需求匹配）。

---

## 9. 测试方案

| 层 | 内容 |
|----|------|
| 单测 | StorageBackend 契约；zip 打包/解压往返（含权限位）；key 白名单与固定 key 生成；两级缓存 LRU/覆盖失效 |
| 路由单测 | skills.py OBS 分支（mock ObsStorage）：上传（重打包校验/**同 key 覆盖断言**）/详情（提取+zip_url）/转换/删除（单对象）/模板 302 |
| 链路单测 | enqueue skill_zip 生成（经 OBS 地址取 zip → base64）；worker 物化 → 脚本可执行断言（含权限） |
| 回归 | OBS_ENABLED=false 下 pytest tests/ 全量（基线 713）零失败 |
| 端到端（OBS_E2E=1 门控） | 真实桶：上传→详情→任务→worker 物化→脚本执行；**重复上传覆盖同一 key 断言** |
| admin | mvn -q compile；提升 copyObject 单测（mock）；uploadSkill 覆盖语义单测 |

---

## 10. 实施计划

| 里程碑 | 内容 |
|--------|------|
| M0 | N1/N2/N3/N18/N19：配置 + 存储抽象 + ObsStorage + 依赖 + DDL（含 current_version DROP） |
| M1 | N4~N8/N16：skills.py 五接口 OBS 化（固定 key 覆盖上传、单对象删除、模板/技能下载返回临时 URL + 前端适配） |
| M2 | N9/N10/N11：DB 适配（去版本）+ 快照 skill_zip + worker 物化 |
| M3 | N12~N15：admin Java 直连 OBS（固定 key 覆盖 + to-cluster/download，编译验证） |
| M4 | N17：存量迁移 + 历史多版本收敛脚本（测试桶验证 → 部署桶） |
| M5 | N20：全量测试 + 回归 + 提交特性分支 |

---

## 11. 待评审确认点

| # | 决策点 | 推荐 |
|---|--------|------|
| 1 | ~~版本化：key 带 v{n} + DB current_version~~ → **v2.4 改固定 key 覆盖（无版本段/随机段，putObject 原子覆盖）** | ✅ 已确认 |
| 2 | 对象键 DB 存储（复用 skill_path） | ✅ |
| 3 | 快照 skill_zip（zip base64，快照快递）+ 老 worker 双写过渡 | ✅ |
| 4 | 模板下载/技能下载：接口返回临时签名 URL（{url, expires_in}），前端点击时动态获取 | ✅ 按需求修正 |
| 5 | zip 炸弹上限 100MB | ✅ |
| 6 | 缓存：磁盘 LRU（2GB/1版）+ 内存 LRU 提取结果，不引 Redis；覆盖后显式失效 | ✅ |
| 7 | 详情接口新增 zip_url 字段（前端本期不动） | ✅ |
| 8 | 详情页可选增强（下载按钮/loading）本期做否 | 待定 |
| 9 | admin Java 直连 OBS（esdk-obs-java） | ✅ |
| 10 | 迁移脚本本期包含（存量迁移 + 历史多版本收敛，测试桶先验证） | ✅ |
| 11 | worker 获取方式 = 快照快递（服务端经 OBS 地址取 zip → 快照 → worker 解码解压） | ✅ 已确认 |
| 12 | 重复上传 = 覆盖更新（最后写者胜）：user 不同 owner 天然隔离；repo 同名覆盖不限角色 | ✅ 已确认 |
| 13 | current_version 列 DROP（本期新增字段，无历史依赖） | ✅ 已确认 |
| 14 | v2.5：file_path 字段（真实存储路径，OBS 对象键；local 留空；VARCHAR(512)） | ✅ 已确认 |
| 15 | v2.5：created_by/updated_by（VARCHAR(64)；创建人/最后操作人；主服务 user/repo 也写） | ✅ 已确认 |
| 16 | v2.5：官方同名禁止上传（scope='official' 同名 → 400「官方技能已存在同名」） | ✅ 已确认 |
| 17 | v2.6：官方删除权限 = 超管 OR created_by==当前登录用户；内置/存量 created_by='' 永不可删 | ✅ 已确认 |
| 18 | v2.5：存量 created_by/updated_by 保持 ''（不可删，仅 SQL 管理） | ✅ 已确认 |
| 19 | v2.6：官方技能更新独立接口 POST /{id}/update（超管 OR 创建者 + zip 目录名校验；upload 仅新增） | ✅ 已确认 |
| 20 | v2.6：更新接口始终上传 OBS（B 方案——未转存自动转存 + file_path 回写）；promote 未转存也自动转存 | ✅ 已确认 |
| 21 | v2.6：admin 操作区图标按钮 + title 简洁描述 + 弹窗统一设备管理样式 + 名称截断 | ✅ 已确认 |
| 22 | v3.0：worker 直连 OBS（claim 现签预签名 URL 30min；不落库）——去 skill_zip（本次新增未上线直砍）；失败降级服务端代拉（复用 zip_cache） | ✅ 已确认 |
| 23 | v3.0：可达性自适应——启动探测（probe_url 复用技能对象）+ 运行时能力反馈（连续 3 失败 prefer_fallback / 3 成功恢复；claim 动态声明零落库） | ✅ 已确认 |
| 24 | v3.0：老 worker 兼容走 skill_files（文本物化保留）；实施顺序 = 直连/代拉 + 同批移除 skill_zip | ✅ 已确认 |

---

## 12. 明确不做（本期范围外）

- **key 引用（worker 直连 OBS）**：~~列为未来演进~~ —— **v3.0 已实施**
  （claim 现签预签名 URL + 代拉复用 zip_cache；详见 N10/N11/§5.7）
- worker 直连 OBS 的其他形态（AK/SK 落 worker 等，安全不允许）
- 官方技能上云、B2~B6（安装包/project-info/知识/event.json）OBS 化
- 前端 JSZip 直连消费 zip（文件树等，等真实需求再整体切换）
- 技能在线编辑、Redis 缓存引入
