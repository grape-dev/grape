# Skill 详情查询和删除修复方案

## 一、问题

1. get_skill_detail_by_id 用前端传的 repo 参数做 SQL 过滤，前端传不带 gitcode: 前缀，数据库存带前缀 → 匹配不到 → 404
2. delete_skill 同样用前端传的 repo 参数做 DELETE 条件 → 匹配不到 → 404

## 二、方案

### get_skill_detail_by_id 修复

按 id 查 skill，查到后做归属校验：
- official → 直接返回
- user → 校验 owner_id == 当前用户 id
- repo → 从后端 user_repos 表查当前用户仓库列表，校验 skill.owner_id 在列表里

不依赖前端传的 repo 参数。

### delete_skill 修复

按 id 查 skill（前端已有 skill_id），查到后做归属校验：
- official → 不允许删除
- user → 校验 owner_id == 当前用户 id
- repo → 从后端 user_repos 表查当前用户仓库列表，校验 skill.owner_id 在列表里

用 skill_id 查删除，不用 skill_name + repo 匹配。

## 三、改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/skills.py | get_skill_detail_by_id 改为 id 查+归属校验；delete_skill 改为 id 查+归属校验 |

## 四、不受影响

- list_skills（已修复 to_repo_key 转换）
- upload_skill
- 前端 agents.js / skills.js
