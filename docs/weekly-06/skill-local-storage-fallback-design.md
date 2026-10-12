# 方案：技能上传本地落盘兜底（OBS 未启用时单机可用）

> 状态：技术方案（2026-10-11）｜分支：weekly-06-lyz
> 动机：单机生产（203.0.113.2，`StorageBackend: local (OBS not enabled)`）上 `sre/grape-skill-uploader` 执行 `grape skills upload --scope repo` 返回 **HTTP 503「OBS 存储未启用」**，技能无法上传。OBS 未启用不应阻断单机场景的自定义技能上传。
> 目标：新增**本地磁盘存储后端**，让技能域在 **OBS 不可用（单机）时本地落盘可用**；集群/多实例仍强制 OBS（本地仅单机语义）。改动**收敛在技能域**，不改变其它域对 `get_storage()`==None 的既有本地回退行为。

---

## 1. 背景与问题（代码核对 2026-10-11）

### 1.1 现状：技能域无本地兜底，直接 503

- `storage/__init__.py:140 get_storage()`：OBS 配置齐全才返回 `ObsStorage`，否则 `None`（日志 `StorageBackend: local (OBS not enabled)`）。
- 技能域四处端点把 `get_storage() is None` 直接判死：
  - `skills.py:742` upload → 503「OBS 存储未启用，技能上传失败」
  - `skills.py:507` download → 503「…技能下载失败」
  - `skills.py:591` detail（user/repo scope）→ 503「…技能详情获取失败」
  - `skills.py:923` convert → 503「…技能转换失败」
- 历史原因：原 local 分支被移除，注释「均衡负载环境下 local 存储会导致文件只存在单机」。

### 1.2 单机下 503 不合理

- 「文件只存在单机」的问题仅在**多实例负载均衡**时存在；单机只有一个服务进程，本地落盘与 OBS 等价。
- 其它域（附件 `issues_center.py:1403`、知识、project-info 双写）在 OBS 未启用时均有本地回退——**技能域是唯一被要求"必须 OBS"的例外**，导致单机连用户个人/仓库技能都无法上传。

---

## 2. 方案设计

### 2.1 新增 `LocalStorage` 后端（`src/storage/local.py`）

实现 `StorageBackend` 全接口（storage/__init__.py:51-131），落盘根 = `SKILL_STORAGE_DIR`（默认 `<部署根>/data/skills/`，env 可调；不用 OBS 缓存目录 `data/cache/storage`，避免与"可丢缓存"语义混淆；与附件 `data/issue_attachments`、会话 `data/sessions.db` 同属服务端数据目录）：

| 接口 | 本地实现 |
|------|----------|
| `backend_name()` | `"local"` |
| `save_bytes(key, data)` | 写 `{root}/{key}`（父目录 mkdir，原子写；返回 key；key 与 `skill_key(...)` 完全一致 → 本地路径含 OBS_PREFIX 段） |
| `save_stream(key, source)` | 拷贝文件 |
| `read_bytes(key)` | 读回 bytes / None |
| `read_file(key, local_path)` | 拉取到 local_path |
| `exists(key)` | os.path.isfile |
| `list_keys(prefix)` | 枚举 `{root}/{prefix}*` 相对路径 |
| `delete_object(key)` / `delete_prefix(prefix)` | 删除单对象 / 删前缀 |
| `copy_object(src, dst)` | 本地字节复制（convert 用；OBS 用 copyObject） |
| `temporary_url(...)` | 返回空（下载时本地直接回字节流） |
| `generate_file_urls(...)` | 空 dict（基类默认，Worker 走 content 兜底，与旧 local 语义一致） |

- **key 语义对齐**：key 由现有 `skill_key(scope, owner, name)`（v2.4 固定 key）构造 → 本地路径 `{SKILL_STORAGE_DIR}/{key}`（默认 `data/skills/grape/prod/skills/{scope}/{owner}/{name}/{name}.zip`，含 OBS_PREFIX 段，与对象 key 一一对应），读/删一致。

### 2.2 技能域存储选择器（`src/storage/__init__.py` `get_skill_storage`）

```python
def get_skill_storage() -> StorageBackend | None:
    """技能域存储：OBS 可用 → OBS；否则 SKILL_STORAGE=auto/local → LocalStorage；否则 None。"""
    return _build_skill_storage(get_storage())   # 不缓存：每次现算（OBS 由 get_storage 单例持有）
```
- 配置开关 `SKILL_STORAGE = auto|obs|local`（`src.config`，默认 `auto`）：
  - `obs` → 仅 OBS，未配置仍 None（保持现状）；
  - `auto` → OBS 有则 OBS，否则 local（默认行为，单机开箱可用）；
  - `local` → 强制本地（单机显式声明）。
- **不缓存**（每次现算）：LocalStorage 无状态、构建廉价；避免测试/运行切换 OBS 与本地后技能域仍拿旧后端。
- 执行端取包与详情回源也走选择器：`storage/__init__.py` 的 `get_skill_zip_bytes` / `skill_md_content` 由 `get_storage()` 改为 `get_skill_storage()` → 本地模式下 Agent 拉取自定义技能 zip 同样可用。

### 2.3 现有端点接入本地兜底（**不新增端点**）

> 仅让**原有 5 个技能端点**在本地模式下也能正常工作（走 LocalStorage），不新增任何 API/路由。

| 端点（原样保留） | 现状 | 改后 |
|------|------|------|
| `POST /skills/upload`（L713-743） | storage None → 503 | `_storage = get_skill_storage()`；None → 503（记录原因）；本地=走 `save_bytes` + `upsert_skill_obs` + `zip_cache_put` + `mem_cache_invalidate`（与 OBS 分支同构） |
| `GET /{skill_id}/download`（L~450-520） | 同上 | 改用选择器；本地 `temporary_url` 为空 → `read_bytes` 直接回 zip 字节流（不新增通道） |
| `GET /{skill_id}` detail（L~540-620） | 同上 | 同上（content 照喂，zip_url 本地为空） |
| `POST /skills/convert`（L~880-925） | 同上 | 同上 |
| `DELETE /by-id/{skill_id}`（L753+） | 已按 DB+storage 删除 | 改用选择器（本地 delete_object） |

> 关键：**统一 `_storage = get_skill_storage()`**，原 OBS 分支逻辑原样复用（save→upsert→cache），只是 storage 实例换成 LocalStorage；不另写第二套业务逻辑。

### 2.4 缓存/内存一致

- 本地 `save_bytes` 后照常 `zip_cache_put(key, bytes)` + `mem_cache_invalidate(name)`；
- 读取路径若走 `read_entry_*` / `get_skill_zip`（现按 OBS object_key 读）→ LocalStorage 的 `read_bytes/read_file` 天然可读本地 key，无需改缓存层。

### 2.5 DB 与官方技能

- `skill` 表不变：`object_key=skill_key(...)`，`skill_path` 留空/推导；列表/搜索（DB 驱动）不动；
- 官方技能仍走官方仓库+构建期 `sync_skills.sh`，与该方案无关（只解决 user/repo 自定义技能）。

---

## 3. 各模块改动

| 模块 | 改动 |
|------|------|
| `src/config.py` | `SKILL_STORAGE`（auto|obs|local，默认 auto）+ `SKILL_STORAGE_DIR`（默认 `data/skills`） |
| `src/storage/local.py`（新） | `LocalStorage(StorageBackend)` 全接口（含 copy_object/temporary_url=""） |
| `src/storage/__init__.py` | `get_skill_storage()` 选择器（不缓存）+ `get_skill_zip_bytes` / `skill_md_content` 回源改走技能存储 |
| `src/api/routes/skills.py` | upload/download/detail/convert/delete 统一 `_storage = get_skill_storage()`；None 才 503（附原因日志）；download 本地回字节流 |
| `tests/` | 见 §五 |

---

## 4. 影响面 / 边界 / 护栏

| 项 | 说明 |
|----|------|
| 其它域 | 不改 `get_storage()` 语义（仍 None when 无 OBS），其它域本地回退行为**不变**；仅技能域改用 `get_skill_storage()` |
| 单机 | auto → 本地落盘，5 端点全可用 |
| 集群/多实例 | `SCENE_HINT`/文档强制：多实例必须 `SKILL_STORAGE=obs` 或共享存储（NFS/PV）；`local` 仅单机，否则技能互不可见（在 `get_skill_storage()` 打 WARNING：`detected multi-instance with local skill storage`，由运维订阅日志） |
| 迁移 | 本地存量技能 → 后续开 OBS 用现成 `scripts/migrate_skills_to_obs.py` 单向迁移 |
| 数据 | 本地技能存 `SKILL_STORAGE_DIR`（默认 `data/skills/`，服务端数据目录，随会话备份） |
| 兼容 | 已有 DB 记录（若曾存 OBS）在 local 模式读不到对象 → detail/下载返回空/404；存量单机无此情况（此前全 503） |
| 日志 | 未配置本地根 / 渲染失败均记 WARNING 并回 503 附原因（不再静默） |

---

## 5. 验证

| 用例 | 断言 |
|------|------|
| UT-01 `LocalStorage.save_bytes/read_bytes/exists/read_file` 往返一致；key 含嵌套目录可建 | 本地后端 |
| UT-02 `list_keys(prefix)` / `delete_object` / `delete_prefix` 正确 | 本地后端 |
| UT-03 `get_skill_storage()`：SKILL_STORAGE=auto 无 OBS → LocalStorage；=obs 无 OBS → None；=local 强制 LocalStorage | 选择器 |
| UT-04 `POST /skills/upload`（OBS 关，auto）→ 200「上传成功」+ DB `skill` 行 `object_key` 指向本地 key + zip 缓存命中 | 主路径（#事故还原） |
| UT-05 download/detail/convert 在 local 下 200 且内容一致 | 读链路 |
| UT-06 delete 后本地文件与 DB 记录同步移除 | 删链路 |
| UT-07 OBS 开启路径回归：仍走 ObsStorage，不回归 | 兼容 |
| 回归 | 附件/知识/project-info 的本地回退（get_storage()==None）行为不变 |

---

## 6. 落档修订说明

- 只解决**单机 + OBS 未启用**下技能域可用；集群语义不变（OBS/共享存储强制）。
- 改动刻意收敛在技能域（新增 LocalStorage + 选择器 + 5 端点接入），不改 `get_storage()` 与其它域。
- 后续若单机也想把官方技能远程拉取换成对象存储，另行设计；本方案不含。
- 已与 `sre/grape-skill-uploader` 现状核对：失败点即 `skills.py:743` 的 503，落盘后该 runbook 无需改动。