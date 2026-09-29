# Plan — Issue #254 缓存命中校验 content_hash 回源

## 任务拆解

### Step 1: 改造 mem_cache 结构（src/storage/__init__.py）
- `_mem_cache` 从 `{skill_name: (content, size)}` 改为 `{skill_name: (content, size, content_hash)}`
- `mem_cache_put(skill_name, content, content_hash="")` 新增可选参数
- `mem_cache_get(skill_name)` 返回 `(content, content_hash)` 或 None

### Step 2: 改造 zip_cache 结构（src/storage/__init__.py）
- `zip_cache_put(object_key, data, content_hash="")` index 记录 hash
- `zip_cache_get(object_key)` 返回 `(data, content_hash)` 或 None
- 保持向后兼容：content_hash 缺省为空串

### Step 3: skill_md_content 增加缓存校验（src/storage/__init__.py）
- mem_cache 命中后查 DB content_hash（official scope），不一致则 invalidate + 回源
- get_skill_zip_bytes 磁盘缓存命中后校验 hash

### Step 4: skill_sync 传 hash（src/storage/skill_sync.py）
- `zip_cache_put(object_key, zip_data, current_hash)` 传入 hash

### Step 5: 测试（tests/test_skill_sync_obs.py）
- 缓存命中 + hash 一致 → 走缓存快路径
- 缓存命中 + hash 变更 → 回源 OBS
- mem_cache_put/get hash 存取
- zip_cache_put/get hash 存取

## 设计决策
- **向后兼容**：content_hash 参数默认空串，旧调用方不传仍可工作（hash="" 不校验）
- **校验范围**：仅 official scope 校验（custom/repo 无 DB hash 同步机制，且 upload/convert 已主动 invalidate）
- **性能**：hash 一致时仅多一次 DB 查询（单行主键查询，<1ms），不回源 OBS
