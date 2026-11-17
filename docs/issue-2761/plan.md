# Issue #2761 方案设计

## 1. 数据模型

### category_dict（新增，mgmt 域）

| 列 | 说明 |
|----|------|
| id | 自增主键 |
| parent_id | 一级域为 0，二级指向一级 id |
| level | 1 / 2 |
| name / slug | 展示名 / 稳定标识 |
| icon / order_num | 展示用 |
| applies_to | `scene` / `skill` / `both` |
| is_active | 1 启用 |

种子：10 个一级域（IT研发、运营域、客户支持、人力资源、财务与会计、供应链与采购、
生产制造、销售域、办公协作、其他）及二级子类，`applies_to='both'`。

### 扩展列

- `scene_market.cat_l1_id / cat_l2_id`（INTEGER DEFAULT 0）；旧 `category_id` 不动。
- `skill.cat_l1_id / cat_l2_id / tags`（INTEGER / TEXT）；旧 `category_id` 不动。
- 标签存储：JSON 数组 `[{"name": "...", "source": "manual"|"auto"}]`。

### 迁移（`db_setup.py` 注册式迁移）

`202611170042 issue2761-category-dict-and-tags`（kind=safe，幂等）：
1. `CREATE TABLE IF NOT EXISTS category_dict` + 种子；
2. `_ensure_column` 补 5 个扩展列；
3. `scene_market.tags` 旧逗号串就地回填为 manual JSON（已是规范 JSON 的行跳过）。
   DDL/回填 SQL 均落在 owner 域（mgmt），避免新增跨域表引用。

## 2. 标签规则（`src/kernel/tags.py`）

- `parse_tags`：兼容 JSON 数组字符串 / list / dict / 中英文逗号串；
- `prepare_tags`：归一化后按 3 个截断，返回超限标签名（写侧 400 点名）；
- 去重大小写不敏感；同标签 manual 覆盖 auto；
- 读路径统一 `limit=3` 截断。

## 3. API

| 端点 | 说明 |
|------|------|
| `GET /api/categories/tree?scope=` | 统一两级树（新增 `categories.py` 路由） |
| `GET /api/scenes/market/categories` | 保留旧 `categories` 一级列表 + 新增 `tree` |
| `GET /api/skills/categories` | 同上 |
| `GET /api/scenes/market` / `GET /api/skills` | 新增 `cat_l1_id` / `cat_l2_id` 过滤；返回 `category` / `tags` |
| `GET /api/scenes/market/{id}` / `GET /api/skills/by-id/{id}` | 返回分类路径与 ≤3 标签 |
| `GET /api/scenes/tag-suggest` / `GET /api/skills/tag-suggest` | 规则型候选（名称/描述关键词） |
| `POST/PUT /api/scenes/market` | 接受 `cat_l1_id / cat_l2_id / tags`；>3 → 400 |
| `POST /api/skills/upload`、`.../update` | 新增 `cat_l1_id / cat_l2_id / tags` 表单字段 |

## 4. 同步层

- `scene_sync`：`category_l1/category_l2` 或 `category` slug 命中字典才写 `cat_*`；
  未命中告警并保留库内旧值；标签以 `source=auto` 写 JSON。
- `skill_sync`：同样按 slug 命中写入；`skill-description.json` 的 `tags` 写 auto JSON；
  未命中的字段沿用库里旧值，不写 0 制造脏数据。

## 5. 前端

- `SceneCard.vue`：卡片展示分类路径与 ≤3 标签 chip。
- `SceneMarket.vue`：新增一级/二级下拉筛选（`cat_l1_id / cat_l2_id`），旧分类 tab 保留。
- `SkillsView.vue`：卡片展示分类路径与标签；新增两级筛选行；上传表单增加一级/二级
  级联选择与标签输入框，提交到 upload 表单字段。

## 6. 风险与兼容

| 风险 | 处理 |
|------|------|
| 存量库迁移台账已盖章 | 走注册式迁移，空库/存量库双向可执行 |
| 标签格式混用 | 读路径双解析，迁移回填 JSON，写侧统一编码 |
| 分类字段双写不一致 | 旧 `category_id` 原样保留；新字段只在新链路写 |
| 资产未迁移导致分类缺失 | cat_* 保持 0/旧值，不阻断展示 |
| 拆库拓扑归属 | `category_dict` 登记 mgmt；同步层通过 mgmt public 读字典 |