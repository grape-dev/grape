# Spec: 云引擎身份锚定——register 按不变锚 UPSERT，改名不换行（rid 永不失效）

- 日期：2026-10-29
- 状态：spec 定稿待开工
- 背景：今日三次实证撞上同一根因——引擎名含可变段（模板 slug/凭据名），改名 → 新名不匹配旧行
  → INSERT 新行 → 旧 rid 作废 → 场景绑定悬空（17 保留→用户改绑 23→改名 s5→23 又变死行）。
  rename-cleanup（c1911c856）与改名提示语只是止血；本 spec 根治。
- 普通 worker 零影响：普通引擎名（hermes/opencode/codearts）天然是锚，现状即锚定行为；
  云引擎走新锚分支，普通 payload 路径一行不改。

## 1. 核心设计

```
云引擎锚（不变量元组）:
  (credential_id, engine_type 第1段, platform, provider_id, template_db_id)
  —— 数据来源: runtime_info（cloud_worker 注册必带 engine_type/platform/provider_id/template_id）

register 匹配（云引擎分支）:
  payload.runtime_info 带 provider_id 且 template_id → 按锚 SELECT
    ├─ 命中 → UPDATE（name/version/healthy/... + name=新四段名）→ rid 不变
    └─ 未命中 → INSERT 新行（首次）
  普通 payload（无 runtime_info）→ 原路径 (credential_id, lower(name), scope, owner_id) 不动
```

**改名语义变化**：改名 = 同一行 UPDATE name 字段（纯展示刷新），不产生新行、不触发下线注册、
场景绑定/心跳/引擎能力全不动。

## 2. 改动点

| # | 位置 | 改动 |
|---|---|---|
| ① | runtime.py register rt 循环（~246 匹配段） | 云引擎分流：runtime_info 解析出锚 → 按锚 SELECT id；命中 UPDATE（含 name）未命中 INSERT。锚匹配与原 name 匹配**互斥**（带 runtime_info.provider_id 即走锚） |
| ② | 模板 db_id 进锚 | runtime_info 现带 template_id（平台串 tpl-xxx）——锚用**平台 template_id**（worker 上报口径，比 db_id 稳：不受 DB 行删除影响）；同平台模板改名 tpl 不变 ✓ |
| ③ | cloud_worker.py | **零改动**——refresh 的 diff（added/deleted）基于 name，改名时 new_runtimes 含新名行但注册返回**同 rid** → added 逻辑自动跳过（rid 已在 runtime_ids）；心跳列表不动。核对 `_new_rid_by_name` 返回值使用处（rid 已存在 → 不加入心跳 → 正确） |
| ④ | rename-cleanup | 保留为纯防御（存量残留行 + 未来异常兜底），不再承担改名收敛主责 |
| ⑤ | 模板/Provider 改名提示语（sandbox.js v42 弹窗） | 文案更新：「改名后引擎名随之变化，但**场景绑定不受影响**（引擎身份不变）；约 1 分钟生效」 |
| ⑥ | 引擎卡片名变化 | 改名后 60s 内卡片 name 刷新（UPDATE 生效），无行数变化 |

## 3. 兼容与迁移

- **存量换名对**（如今日的 23[s4] + 25[s5]）：锚定上线后下一次注册按锚命中**先注册的行**（23），
  UPDATE name=s5 段 → 25 成为孤儿（rename-cleanup 下轮清）。可接受，无需预迁移。
- **旧行 runtime_info 为空的三段名存量**：无锚信息 → 走原 name 匹配（不破坏）；此类行已无活体
- UNIQUE 约束：现 (credential_id, name) 唯一？——核对 agent_runtimes 唯一键；锚定后同锚同行，
  name 更新不撞唯一键（改名后旧行 name 已 UPDATE）

## 4. 验收

1. 改凭据名 s5→s6：60s 内引擎卡片 name 变 `...s6:38`，**rid 不变**（API 核对 id 字段）
2. 场景绑定的 rid 在改名前后任务正常路由（绑定零操作）
3. 模板改名：同锚 UPDATE，行为同上
4. 普通 worker：注册 → rid 稳定（回归）；本机引擎 1/2/3 不受影响
5. 新凭据首注册：INSERT 新行（锚未命中路径）
6. 心跳/claim/引擎能力：rid 稳定 → 全链无感
7. 单测 4 例：锚命中 UPDATE 不换 rid / 锚未命中 INSERT / 普通 payload 原路径 / 存量换名对合流
8. 17 套件全量 + .17 实测一轮改名

## 5. 改动量

register 匹配段 ~30 行 + sandbox.js 提示语 2 处 + 单测 4 例 + .17 热修（cloud_worker 零改动，
服务端文件不涉 worker——**无需热修**，只重启服务端）。
预估半天。
