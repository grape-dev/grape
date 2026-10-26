# Issue #444 设计文档：skill 缓存跨进程一致性修复（P47-P49）

> **需求文档**：`issue-444-skill-cache-sync-requirements.md`（同目录）
> **分支**：`feat/issue-444-skill-cache-sync`（基线 weekly-08 head 595cde23f）
> **状态**：已实施——P47-P49 代码落地，测试 `tests/test_issue444_skill_cache_sync.py` + 既有套件更新后 82/82 通过

## 1. 设计总览

修复路线：**把变更信号写进 DB（content_hash 列），读时校验自动失效**——不引入跨进程广播/Redis（需求 §1.3 明确排除）。DB 是跨进程共享的真值源（WAL 模式），与删除链路"DB 行访问门"同构。

```
修复后数据流（另一进程 B 的读路径）：

B: skill_md_content(object_key, name)
   ├─ mem cache hit (object_key)  ── verify_hash(id) ── DB hash == cached? ── yes → 快路径返回
   │                                                    └─ no → invalidate → 回源
   ├─ zip cache hit (object_key)  ── verify_hash(id) ── 同上
   └─ miss → OBS/local read → extract → put(id hash)

A: upload/convert
   └─ save_bytes(object_key) → upsert_skill_obs(..., content_hash=H) →
      zip_cache_put(object_key, bytes, H) → mem_cache_invalidate(object_key)
```

三个修复点的关系：P47 让 hash 有了值（写侧），P48 让校验能查对行（读侧），P49 让缓存条目本身可寻址（key 口径）。三者缺一则防线仍失效。

## 2. 关键设计决策

| # | 决策 | 选项 | 选择 | 理由 |
|---|------|------|------|------|
| D1 | hash 口径 | a) `_compute_skill_hash`（skill 目录内容树 hash）；b) 规范化 zip 字节 sha256 | **b** | 上传/转换链路手里只有规范化后的 `zip_bytes`（a 需要目录，convert 场景拿不到）；b 与读侧回源后可比对（读侧拿到的是 OBS 上的同一 zip 字节）；sha256(zip) 满足"变更信号"语义（内容变则 zip 变；zip 元数据 timestamp 已被 `_normalize_skill_zip` 规范化，避免假变更）。官方同步 `skill_sync.py` 维持 `_compute_skill_hash`（目录口径）**不动**——两条写链路口径独立，只要各自读侧校验用同源 hash 即可。 |
| D2 | 读侧 hash 查询定位 | a) (skill_name, scope) LIMIT 1；b) 按主键 id | **b** | F3：user scope 多 owner 同名时 a 命中不确定行。写侧 `upsert_skill_obs` 拿到 row[0]（主键），缓存与校验全链路传 id。官方同步路径 `skill_sync` 已有 id（同款先例 skill_sync.py:198）。存量兼容：id 缺失时降级为 (name, scope) 查询（official 唯一，安全）。 |
| D3 | 内存缓存 key | a) skill_name（现状）；b) object_key | **b** | P49：同名跨 scope 串键。object_key 天然含 scope+owner+name（`skills/{scope}/{owner}/{name}/{name}.zip`），且与磁盘 zip 缓存同口径，`invalidate_skill_caches` 的解析逻辑复用。 |
| D4 | 校验函数签名 | `_verify_skill_cache_hash(skill_name, cached_hash)` 沿用 | 改为 `_verify_skill_cache_hash(object_key, skill_name, cached_hash)` | 读侧 `skill_md_content` 持有 object_key；从 `parse_skill_key(object_key)` 解析 (scope, owner, name) 供 D2 查询。保留向后兼容降级语义（§2.5 需求）。 |
| D5 | upsert 返回值 | bool | **改为 `int \| None`**（主键 id，失败 None） | D2 需要写侧拿到 id；upload/convert 判断 falsy 的现有代码零改动（0/None 均 falsy，`if not ok` 语义不变）。 |
| D6 | mem_cache API | key 参数语义改为 object_key | `mem_cache_get/put/invalidate(key, ...)`，内部实现不变 | 调用方共 3 处（storage 读写 + skills.py 两处 invalidate），全量改口径；不存在其他调用方（grep 核验）。 |

## 3. 改动清单（按文件）

### 3.1 `src/user/db.py`

- `upsert_skill_obs(scope, owner, skill_name, agent_type, description, object_key, created_by, category_id, content_hash="")`
  - UPDATE 语句补 `content_hash=?`；
  - INSERT 列清单补 `content_hash`；
  - 返回值 `bool` → `int | None`（row[0] 或 lastrowid；失败 None）。docstring 注明 Issue #444。
- `get_skill_content_hash(skill_name, scope="official")` 保持不动（官方同步在用，signature 兼容）。
- 新增 `get_skill_hash_by_id(skill_id: int) -> tuple[int, str, str]`：按主键查 (id, content_hash, file_path)，供读侧校验（D2）。

### 3.2 `src/storage/__init__.py`

- `mem_cache_get/mem_cache_get_with_hash/mem_cache_put/mem_cache_invalidate`：参数 `skill_name` 改名 `key`（object_key 口径，D3/D6），实现不变。
- `_verify_skill_cache_hash(object_key, cached_hash)`（D4）：
  ```python
  if not cached_hash: return True                      # 兼容：缓存无 hash
  info = parse_skill_key(object_key)
  # D2：优先按 id（写侧 upsert 时已把 id 存入缓存条目?——见 D7，读侧用 (scope,owner,name) 解析行）
  ```
- **D7（校验查询定位，实现细化）**：`parse_skill_key` 返回 (scope, owner, name)——user scope 时 owner 即 user_id，(name, scope, owner_id) 三元组在 UNIQUE 约束下唯一（db_setup L107），**无 F3 歧义**。故读侧查询用 `WHERE skill_name=? AND scope=? AND owner_id=?`，不依赖缓存存 id。新增 `get_skill_hash_by_key(skill_name, scope, owner_id)`（db.py），official 的 owner 为空串同款查询。D2 的"按主键 id"意图由此实现——(name,scope,owner) 唯一确定 id。
- `skill_md_content(object_key, skill_name)`：
  - `mem_cache_get_with_hash(object_key)`（key 口径）；
  - `_verify_skill_cache_hash(object_key, cached_hash)`；
  - 回源分支 `get_skill_content_hash(skill_name)` → `get_skill_hash_by_key(...)`；
  - `zip_cache_put/mem_cache_put` 均传 object_key + db_hash。
- `invalidate_skill_caches(object_key)`：`mem_cache_invalidate(object_key)`（口径同步；原按 skill_name 的调用改为 object_key）。

### 3.3 `src/api/routes/skills.py`

- `upload_skill`：
  ```python
  zip_bytes = _normalize_skill_zip(skill_dir, skill_name)
  storage.save_bytes(object_key, zip_bytes)
  content_hash = hashlib.sha256(zip_bytes).hexdigest()          # D1
  skill_id = upsert_skill_obs(..., content_hash=content_hash)  # D5
  if not skill_id: raise 409
  zip_cache_put(object_key, zip_bytes, content_hash)            # P47
  mem_cache_invalidate(object_key)                              # P49 口径
  ```
- `convert_skill`：同款三处——`content_hash` 从 `zip_cache_get_with_hash(src_key)` 拿源 hash（copy 的 zip 字节与源一致，hash 直接复用；源无 hash 则对 src_zip 现算 sha256）；`upsert_skill_obs(..., content_hash=...)`；`zip_cache_put(dst_key, src_zip, content_hash)`；`mem_cache_invalidate(dst_key)`（原 `skill_name` 改 `dst_key`，P49 连带点）。

### 3.4 不改动（显式）

- `skill_sync.py` 官方同步（`_compute_skill_hash` 目录口径 + `update_skill_content_hash` 按主键）——已正确，仅回归保障；
- 磁盘 zip 缓存结构（`zip_cache_*` 的 hash 字段机制已在 #254 落好，本次只是让写侧传值）；
- 删除链路（`invalidate_skill_caches` 调用处签名不变，内部口径自适应）；
- DB schema（skill.content_hash 列已存在，db_setup L4592 迁移）；
- `get_skill_content_hash`（skill_sync 依赖，签名兼容不动）。

## 4. 兼容与降级（需求 §2.5 逐条对照）

| 场景 | 行为 | 依据 |
|------|------|------|
| 存量缓存条目无 hash | `cached_hash` 空 → return True，不校验 | UC3-1 |
| 存量 DB 记录无 hash（修复前上传的） | db_hash 空 → return True | UC3-1 |
| DB 查询异常 | try/except → return True + warning | UC3-2 |
| 技能已删除 | 查询无行 → db_hash 空 → return True（删除安全由 DB 行访问门保证，UC9-1） | §4.3 论证 |
| hash 一致 | 内存命中直接返回，零额外 IO | UC1-4 快路径 |

升级路径：无需迁移——旧缓存条目/旧 DB 记录全部落入"不校验"兼容分支；下一次上传覆盖后 hash 链路即激活。

## 5. 测试设计

新增 `tests/test_issue444_skill_cache_sync.py`（随 CI 跑的 pytest，无外部依赖，monkeypatch DB 与存储）：

- **T1 写侧贯通**：fake storage + 内存 DB，走 upload 路径 → 断言 skill 行 content_hash 非空、zip 缓存条目 hash == DB hash（UC5-2 同款断言用于 upload）。
- **T2 读侧失效回源（核心，模拟另一进程）**：先读一次（缓存 v1）→ 改 DB hash + OBS 内容（模拟另一进程上传 v2，本进程缓存/DB 视角不变）→ 再读 → 断言返回 v2 且 OBS 被回源（读时校验触发）。
- **T3 scope 隔离**：同名 user/repo/official 三个 object_key 交替读 → 内容各自正确，互不覆盖（UC2-1/2-2）。
- **T4 convert 写 hash**：convert 后 dst 行 hash 非空且 == 源 hash；dst zip 缓存条目带 hash（UC5-1/5-2）。
- **T5 降级兼容**：缓存无 hash / DB hash 空 / 查询抛异常三情形 → 返回缓存内容不报错（UC3-1/3-2）。
- **T6 多 owner 同名**：两个 owner 各自同名 user 技能 → 各自读，hash 校验互不串扰（UC8-3/F3 联合）。
- **T7 回归**：官方同步 `test_skill_sync_obs.py` 全量通过（hash 写入路径未动）。

## 6. 性能影响

- 快路径（hash 一致）：1 次内存 dict 读 + 1 次主键索引查询（原为 0 次查询）——单行主键查 ~µs 级，且只在缓存命中时发生（需求 §2.4 约束满足）；
- 慢路径（hash 变化）：多 1 次 OBS 回源——这正是修复目的；
- 写路径：多 1 次 sha256（10MB 上限 → ~30ms），上传链路本就非热路径。

## 7. 风险与缓解

| 风险 | 缓解 |
|------|------|
| mem_cache key 口径改动遗漏调用方 | grep 全量核验仅 3 处调用（storage 内部 2 + skills.py 2），T3/T4 覆盖 |
| upsert 返回值类型变化破坏现有 `if not ok` 判断 | int/None 均 falsy 于 0/None，语义不变；T1 断言 |
| convert 源缓存无 hash（存量） | 回退现算 sha256(src_zip)（§3.3），不依赖源缓存状态 |
| 官方同步 hash 口径（目录树）与上传口径（zip 字节）并存 | 互不交叉：official 读侧校验查 official 行（同源 hash），user/repo 查各自行；T7 回归保障 |
