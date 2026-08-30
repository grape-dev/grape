# Skill 表 owner_id 归一化修复方案

## 一、背景

2026-08-06 commit 9cbe098 引入 repo_key 统一带 gitcode: 前缀，但漏了 skills.py 和 user_db.py 的 skill 写入/查询路径。导致：
- upload_skill / convert_skill 写入 owner_id 不带前缀
- 存量数据被迁移补了前缀
- 两种格式共存，查询时匹配失败

## 二、方案

所有写入 skill 表 owner_id 的地方统一用 to_repo_key 归一化；所有用 owner_id 构建文件路径的地方用 from_repo_key 去前缀。

## 三、改动文件

| 文件 | 改动点数 | 改动 |
|------|---------|------|
| src/api/routes/skills.py | 3 | upload_skill / convert_skill / download_skill_zip_by_id 加 to_repo_key + from_repo_key |
| src/api/routes/chat.py | 2 | list_skills / _find_skill 加 to_repo_key |
| src/user/db.py | 7 | create_skill / delete_skill / upsert_skill_obs / resolve_skill_id / get_skill_path / ensure_repo_skill_dir 加 to_repo_key 或 from_repo_key |

## 四、具体改动

### 4.1 skills.py — upload_skill（写入归一化）

第 419 行 `repo_key = repo` → `repo_key = to_repo_key(repo)`
第 427 行 DELETE 和第 429 行 create_skill 的 owner_id 用归一化后的 repo_key

### 4.2 skills.py — convert_skill（写入归一化）

第 538 行校验和第 578 行 upsert_skill_obs 和第 604 行 create_skill 的 owner_id 用 `to_repo_key(repo)`
第 598 行 local 目录路径用 `from_repo_key(repo_key)`

### 4.3 skills.py — download_skill_zip_by_id（查询+路径归一化）

第 188 行 SQL 的 owner_id 参数用 to_repo_key(repo)
第 215 行 local 目录路径用 from_repo_key(owner_id)

### 4.4 chat.py — list_skills + _find_skill（查询归一化）

第 764 行和第 881 行 owner_id 参数用 to_repo_key(repo)

### 4.5 user_db.py — create_skill（写入归一化）

第 952 行 owner_id 参数在调用方归一化（skills.py 调用时传 to_repo_key(repo)）

### 4.6 user_db.py — delete_skill（查询归一化）

第 973 行 owner_id 参数在调用方归一化

### 4.7 user_db.py — upsert_skill_obs（写入归一化）

第 916 行 owner_id 参数在调用方归一化（skills.py upload_skill / convert_skill 调用时传 to_repo_key(repo)）

### 4.8 user_db.py — resolve_skill_id（查询归一化）

第 859 行 owner_id 参数在调用方归一化（event_pipeline / service.py 调用时传 to_repo_key）

### 4.9 user_db.py — get_skill_path（路径归一化）

第 878 行 repo 目录路径用 from_repo_key(owner_id)

### 4.10 user_db.py — ensure_repo_skill_dir（路径归一化）

第 959 行 目录路径用 from_repo_key(owner_id)

### 4.11 user_db.py — delete_skill 目录删除（路径归一化）

第 983 行 目录路径用 from_repo_key(owner_id)

## 五、不动的地方

- storage skill_key：已处理（第 155 行自己去前缀）
- db_setup 迁移逻辑：保留（存量数据补前缀）
- skills.py list_skills：已加 to_repo_key（之前改的）
- skills.py get_skill_detail_by_id：已改为 id 查+归属校验
- skills.py delete_skill：已加 to_repo_key + from_repo_key

## 六、验证

1. upload_skill repo scope → owner_id 带前缀入库
2. convert_skill → 校验用带前缀匹配
3. download_skill_zip_by_id → 查询和路径都匹配
4. chat.py list/find → 查询匹配
5. user_db get_skill_path → 路径正确
6. 已有数据不受影响（迁移已补前缀）
