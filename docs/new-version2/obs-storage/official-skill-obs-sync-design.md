# 官方技能 OBS 自动同步方案设计

## 1. 问题

集群部署场景下，官方技能（scope=official）只存在本地磁盘 `skills/<agent_type>/<skill>/SKILL.md`，不自动同步到 OBS。导致：
- 远端 worker claim 任务后拿不到 skill_obs_url，无法直连 OBS 下载技能
- 多实例部署时各实例本地技能版本不一致
- 本地技能更新后无自动发现机制，需手动触发同步

## 2. 目标

1. 本地官方技能内容变更后自动发现
2. 变更后自动上传到 OBS，DB 的 file_path 写入对象键
3. 远端 worker claim 任务时能拿到预签名 URL
4. 不破坏现有非 OBS 模式（storage=None 时走本地磁盘）
5. 保持原有 DB 同步逻辑不变（只加不删，以 skill-description.json 为权威）

## 3. 现状分析

### 3.1 技能存储架构

```
本地磁盘: skills/<agent_type>/<skill>/SKILL.md
DB skill 表: skill_name, skill_path(目录路径), file_path(OBS对象键，官方技能为空), scope=official
OBS: 无官方技能对象
```

### 3.2 现有流程

| 流程 | 入口 | 问题 |
|------|------|------|
| 同步官方技能 | sync_official_skills (skills.py:94) | 只写 DB skill_path，不上传 OBS |
| 读取技能内容 | get_skill_detail_by_id (skills.py:288) | file_path 为空走 local 分支读本地磁盘 |
| 下载技能包 | download_skill_zip_by_id (skills.py:219) | 官方技能返回 400 "不提供下载" |
| claim 附 OBS URL | _attach_skill_obs (runtime.py:319) | get_skill_object_key 返回空串，不附 URL |
| 变更检测 | 无 | 无文件监听、无 hash/mtime 对比 |

### 3.3 原有 DB 同步逻辑

`_sync_official_skills`（db_setup.py 启动时执行）：
1. 读 configs/skill-description.json
2. 遍历 json 里每个技能
3. DB 有 → UPDATE（display_name/description/agent_type/skill_path）
4. DB 没有 → INSERT
5. json 里没有但 DB 有的技能 → 不管（只加不删，以 json 为权威）

## 4. 设计方案

### 4.1 整体架构

```
服务启动
    ↓
db_setup.py 同步 DB（skill_path + content_hash）——原有逻辑不变，加 content_hash
    ↓
OBS 模式？→ 启动 asyncio 定时任务（hash 检测 + 上传 OBS）
非 OBS 模式？→ 不启动定时任务
    ↓
定时扫描（每 30 分钟）：
  1. 遍历 skill-description.json 的技能，计算 content_hash
  2. hash 变了 → 打包 zip → 上传 OBS → 更新 DB file_path + content_hash
  3. hash 没变 → 跳过
    ↓
远端 worker claim 任务 → _attach_skill_obs 拿到 file_path → 签发预签名 URL
```

### 4.2 变更检测方案选型

| 方案 | 优点 | 缺点 | 选择 |
|------|------|------|------|
| watchdog/inotify 文件监听 | 实时 | 需要额外依赖，跨平台兼容性差 | 否 |
| 定时全量扫描 + hash 对比 | 简单可靠，无额外依赖 | 非实时（有延迟） | 是 |
| 启动时全量 + 手动触发 | 最简单 | 不能自动发现 | 否（作为补充） |

选择定时全量扫描 + hash 对比：
- 启动时执行一次全量同步（保证启动后状态正确）
- 运行时每 30 分钟定时增量扫描（可配置 `SKILL_SYNC_INTERVAL`，默认 1800 秒）
- 保留手动触发入口（admin 页面"同步官方技能"按钮）

### 4.3 content_hash 计算

对每个技能目录下的所有文件内容计算完整 SHA-256：

```python
def _compute_skill_hash(skill_dir: str) -> str:
    """计算技能目录的内容 hash（遍历所有文件，按文件名排序，取完整 SHA-256）"""
    import hashlib, os
    h = hashlib.sha256()
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "__MACOSX", ".git")]
        for f in sorted(files):
            rel = os.path.relpath(os.path.join(root, f), skill_dir)
            h.update(rel.encode())
            with open(os.path.join(root, f), "rb") as fp:
                h.update(fp.read())
    return h.hexdigest()  # 完整 64 字符十六进制
```

### 4.4 DB 改动

skill 表新增 `content_hash` 字段：

```sql
ALTER TABLE skill ADD COLUMN content_hash VARCHAR(256) DEFAULT '';
```

幂等迁移（db_setup.py 启动时执行，已有列则跳过）。

### 4.5 同步流程（新增 `sync_official_skills_to_obs` 函数）

位置：`src/storage/skill_sync.py`（独立模块）

```
输入：无
流程：
1. 读 configs/skill-description.json 获取官方技能清单（skill_name, agent_type, path）
2. 遍历每个技能：
   a. 解析本地目录路径：skills/<agent_type>/<skill>/
   b. 计算当前 content_hash
   c. 查 DB 该技能的 content_hash（按 skill_name + scope 查主键 id，再读 content_hash）
   d. hash 相同且（非OBS模式 或 file_path非空）→ 跳过（无变更）
   e. hash 不同，或 OBS模式下file_path为空 → 打包 zip → 上传 OBS（OBS 模式）→ 按主键 id 更新 DB
3. 返回同步结果（新增/更新/跳过数量）
```

OBS 模式判断：
- `storage = get_storage()` 为 None → 只更新 DB content_hash（非 OBS 模式行为不变）
- `storage` 不为 None → 打包 zip 上传 OBS + 更新 DB file_path=对象键 + content_hash

不处理删除——保持和原有 DB 同步逻辑一致（只加不删，以 json 为权威）。

### 4.6 OBS 对象键

使用现有 `skill_key()` 函数生成：

```python
from src.storage import skill_key
object_key = skill_key(scope="official", owner="", skill_name=skill_name)
# 结果：{PREFIX}/skills/official/{skill_name}/{skill_name}.zip
```

### 4.7 定时任务

在 `main.py` 启动时注册 asyncio 定时任务：

```python
from src.storage.skill_sync import start_skill_sync_scheduler
start_skill_sync_scheduler()
```

`start_skill_sync_scheduler` 实现（生产可用骨架）：

```python
import asyncio
import logging

logger = logging.getLogger(__name__)
_bg_tasks: set[asyncio.Task] = set()

async def _skill_sync_worker(interval: float = 1800.0):
    while True:
        try:
            await sync_official_skills_to_obs()
        except asyncio.CancelledError:
            logger.info("skill sync worker cancelled")
            raise  # 必须重新 raise，不能吞掉取消异常
        except Exception:
            logger.exception("skill sync run failed")
        # 无论成功失败都 sleep，避免雪崩式死循环
        await asyncio.sleep(interval)

def start_skill_sync_scheduler():
    # OBS 未启用时不启动调度
    from src.storage import get_storage
    if get_storage() is None:
        return
    interval = float(os.environ.get("SKILL_SYNC_INTERVAL", "1800"))
    t = asyncio.create_task(_skill_sync_worker(interval), name="skill-sync")
    _bg_tasks.add(t)  # 持有强引用，防止 GC 回收导致任务静默消失
    t.add_done_callback(_bg_tasks.discard)

async def stop_skill_sync_scheduler():
    for t in _bg_tasks:
        t.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)
```

关键设计点：
- 用 asyncio task（grape 是 FastAPI=asyncio 架构），不用 threading.Timer
- 启动即执行首次全量同步（不延迟，DB 已在 db_setup.py 里就绪）
- 之后每 `SKILL_SYNC_INTERVAL` 秒（默认 1800，可配置）执行增量扫描
- 循环内 catch 业务异常 + log.exception，不中断调度（防坑1）
- 间隔策略不固定时间点（技能同步属于后台巡检，允许时间漂移）
- 全局 `_bg_tasks` 集合持有 task 强引用，防止 GC 回收导致任务静默消失（防坑3）
- catch `asyncio.CancelledError` 并重新 raise，不吞取消异常（防坑4）
- OBS 未启用时（storage=None）直接 return 不启动调度
- shutdown 时 `stop_skill_sync_scheduler()` 统一 cancel + await 等待退出

### 4.8 改动点清单

| # | 文件 | 改动 | 说明 |
|---|------|------|------|
| 1 | src/api/db_setup.py | 新增 content_hash 字段 DDL 迁移 | ALTER TABLE 幂等，VARCHAR(256) |
| 2 | src/api/db_setup.py | _sync_official_skills INSERT 加 content_hash | 启动时 seed 写空 hash |
| 3 | src/user/db.py | get_skill_by_id 返回 content_hash | SELECT 加 content_hash 列 |
| 4 | src/user/db.py | 新增 get_skill_content_hash + update_skill_content_hash | 读取/更新 hash，基于主键 id |
| 5 | src/storage/skill_sync.py（新增） | _compute_skill_hash | 计算 hash（完整 SHA-256 64 字符） |
| 6 | src/storage/skill_sync.py（新增） | _pack_skill_zip | 打包技能目录为 zip |
| 7 | src/storage/skill_sync.py（新增） | sync_official_skills_to_obs | 同步逻辑（hash 检测 + 上传 OBS） |
| 8 | src/storage/skill_sync.py（新增） | start_skill_sync_scheduler + stop_skill_sync_scheduler | asyncio 定时调度 + GC 防护 + 优雅关闭 |
| 9 | src/api/main.py | 启动时调用 start_skill_sync_scheduler，shutdown 时调用 stop_skill_sync_scheduler | 注册定时任务 + 优雅关闭 |
| 10 | src/api/routes/skills.py | sync_official_skills API 改为调用新逻辑 | 手动触发也走 OBS 同步 |

### 4.9 非 OBS 模式兼容

- `storage = get_storage()` 为 None 时：
  - 定时调度不启动（start_skill_sync_scheduler 直接 return）
  - 手动触发 sync_official_skills API 时仍执行：更新 DB content_hash
  - file_path 保持为空
  - get_skill_detail_by_id 走 local 分支读本地磁盘
  - _attach_skill_obs 返回空（不附 URL）
  - 行为与改造前基本一致，新增了 content_hash 记录

- `storage` 不为 None 时：
  - 定时调度启动，每 30 分钟增量扫描
  - 同步逻辑上传 zip 到 OBS + 更新 DB file_path + content_hash
  - get_skill_detail_by_id 走 OBS 分支
  - _attach_skill_obs 签发预签名 URL
  - 远端 worker 直连 OBS 下载

## 5. 影响分析

### 5.1 不受影响

| 组件 | 理由 |
|------|------|
| 用户技能（scope=user） | 不在同步范围，走原有上传/存储流程 |
| 仓库技能（scope=repo） | 不在同步范围 |
| 非 OBS 模式 | storage=None 时调度不启动，行为基本不变（新增 content_hash 记录） |
| worker 本地技能扫描 | _scan_local_skills 扫描 worker 目录，不受影响 |
| skill_path 字段 | 保持不变（本地目录路径），新增 file_path 和 content_hash |
| DB 删除逻辑 | 保持原有逻辑（只加不删），不新增软删除/重新激活 |

### 5.2 受影响

| 组件 | 影响 | 风险 |
|------|------|------|
| skill 表 | 新增 content_hash 列 | 低（ALTER TABLE 幂等迁移） |
| sync_official_skills API | 增加上传 OBS 逻辑 | 中（首次同步可能上传较多技能，耗时较长） |
| 服务启动 | 启动即执行首次同步 | 低（asyncio task 不阻塞启动和请求） |
| OBS 存储 | 新增 official 技能对象 | 低（按现有 skill_key 规则存对象） |

### 5.3 集群多实例并发分析

集群部署多个实例时，所有实例共享同一个 MySQL 数据库（`configs/db-backend.json` backend=mysql），各实例定时任务独立运行，可能同时扫描并上传官方技能到 OBS。

**DB 层面——MySQL InnoDB 行锁保护**：
- 多实例同时执行 `UPDATE skill SET content_hash=?, file_path=? WHERE skill_name=? AND scope='official'` 时，MySQL InnoDB 对同一行加行锁，串行执行 UPDATE
- 两个实例更新同一行，一个先执行成功释放锁，另一个再执行
- 更新的值相同（hash 一样、file_path 一样），谁先谁后结果一样
- 不会数据损坏，不会死锁（不同技能是不同行，不存在锁等待链）
- 证据：`src/session_manager.py` 第 39 行 `db_lock = threading.RLock()`（同进程串行）+ MySQL InnoDB 行锁（跨进程串行）

**OBS 层面——无条件覆盖，内容相同结果正确**：
- 多实例同时 PUT 同一个对象键时，OBS 的行为是 last-write-wins
- `src/storage/obs.py` 第 66-68 行 `save_bytes` 调用 `putObject`，无 If-Match/If-None-Match 条件判断
- 所有实例的本地技能来源相同（git pull 同一个仓库），zip 内容一致，覆盖结果正确

**hash 对比减少并发概率**：
- hash 相同 → 跳过，不触发 DB UPDATE 和 OBS PUT
- 只有 hash 变了才写
- 30 分钟定时扫描，两个实例同时发现 hash 变了的窗口极小
- 即使同时触发，DB 行锁和 OBS last-write-wins 保证结果正确

**check-then-act 竞态分析**：
- 同步流程：查 DB hash → 和本地 hash 对比 → 不同则上传 OBS + 更新 DB
- 竞态场景：实例 A 和 B 同时查到 DB hash=abc，都发现本地 hash=def≠abc，都去上传 + 更新
- OBS：两边 PUT 同一个 key，内容相同，结果正确
- DB：两边 UPDATE 同一行，MySQL 行锁串行，A 先写 hash=def，B 再写 hash=def，结果正确
- 不会出现 A 写了一半 B 读到中间状态的问题，`with db_lock` + MySQL 行锁保证 UPDATE 原子性

**版本不一致场景**：实例 A git pull 了新技能版本，实例 B 还没 pull。实例 A 上传新版本到 OBS。实例 B 定时扫描时本地 hash 没变（还是旧版本），不上传。OBS 上是新版本，结果正确。等实例 B 也 pull 了，hash 变了再上传覆盖，内容一样，结果正确。

**结论**：不需要分布式锁。MySQL InnoDB 行锁保证 DB 写串行不冲突，OBS 并发 PUT 内容相同时结果正确，hash 对比保证只传有变更的，官方技能数量少体积小重复上传开销可忽略。

## 6. 验证方式

1. 非 OBS 模式：启动后手动修改 skills/ 下某技能 → 触发同步 → DB content_hash 更新 → file_path 仍为空
2. OBS 模式：启动后自动同步 → DB file_path 有值 → get_skill_detail_by_id 返回 is_obs=true → download_skill_zip_by_id 返回预签名 URL
3. 变更检测：修改某技能 SKILL.md → 等 30 分钟 → DB content_hash 变化 → OBS 对象更新
4. 远端 worker：claim 任务 → skill_obs_url 有值 → worker 下载到最新技能
