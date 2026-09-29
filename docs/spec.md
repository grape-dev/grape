# Issue #254 — 技能详情内容同步问题：缓存命中校验 content_hash 回源

## 1. 问题概述

官方技能（official）通过 `skill_sync` 定时任务（每 30 分钟）同步到 OBS 后，`zip_cache_put` + `mem_cache_invalidate` 仅在执行 sync 的那一台节点生效，其他节点的磁盘 zip 缓存和内存 LRU 缓存未被失效，导致详情接口 `skill_md_content()` 命中旧缓存返回旧 content，而下载接口 `temporary_url()` 实时签名永远返回最新——出现"下载新、详情旧"不一致。

本周同一根因已发生两次（上传覆盖一次、sync 更新一次），需治本修复。

## 2. 根因分析

**代码位置**：
- `src/storage/skill_sync.py` → `sync_official_skills_to_obs()`：上传新 zip 到 OBS 后调用 `zip_cache_put` + `mem_cache_invalidate`，但仅在当前节点生效
- `src/storage/__init__.py` → `skill_md_content()`：三级读取链 "内存 LRU → 磁盘 zip 缓存 → OBS 回源"，缓存命中时不校验 content_hash
- PR #349 已在单节点层面修复 sync 后缓存失效，但跨节点未覆盖

**核心缺陷**：缓存无版本校验——命中缓存时不比对 DB 中的 `content_hash`，无法发现内容已更新。

## 3. 修复方案

### 方案 A（主）：缓存命中时校验 content_hash 回源

在 `skill_md_content()` 的缓存命中路径增加 content_hash 校验：

```
skill_md_content(object_key, skill_name)
  → mem_cache_get(skill_name)
    → 命中 → 查 DB content_hash（按 skill_name + scope=official）
      → DB hash 与缓存记录的 hash 一致 → 返回缓存（快路径）
      → DB hash 与缓存记录的 hash 不一致 → 失效缓存 → 回源 OBS（慢路径）
  → get_skill_zip_bytes(object_key) 
    → 磁盘缓存命中 → 同样校验 content_hash
```

### 改动点

1. **`src/storage/__init__.py`**：
   - `_mem_cache` 结构：`{skill_name: (content, size, content_hash)}` → 存入时记录 hash
   - `mem_cache_put(skill_name, content, content_hash="")` → 新增可选 hash 参数
   - `mem_cache_get(skill_name)` → 返回 `(content, content_hash)` 元组
   - `zip_cache_put(object_key, data, content_hash="")` → index 记录 hash
   - `zip_cache_get(object_key)` → 返回 `(data, content_hash)` 元组
   - `skill_md_content()` → 缓存命中后比对 DB hash，不一致则失效回源
   - `get_skill_zip_bytes()` → 磁盘缓存命中后校验 hash

2. **`src/storage/skill_sync.py`**：
   - `sync_official_skills_to_obs()` → `zip_cache_put` 传入 `current_hash`

3. **`src/user/db.py`**：
   - 复用已有 `get_skill_content_hash(skill_name, scope)` 供缓存校验

## 4. 验收标准

1. `sync_official_skills_to_obs()` 上传新 zip 后，**所有节点**的 `skill_md_content()` 返回最新 content
2. 缓存命中且 hash 一致时走快路径（不回源 OBS），性能不退化
3. hash 不一致时失效缓存回源 OBS
4. 回归测试覆盖：缓存命中 + hash 变更 → 回源；hash 不变 → 走缓存
5. PR #349 + #350 的修复与测试保留，本方案在其基础上增强

## 5. 涉及文件

| 文件 | 改动 |
|------|------|
| `src/storage/__init__.py` | mem_cache/zip_cache 结构加 hash 字段；skill_md_content 增加缓存命中校验 |
| `src/storage/skill_sync.py` | zip_cache_put 传 content_hash |
| `tests/test_skill_sync_obs.py` | 新增缓存命中 + hash 校验场景测试 |
