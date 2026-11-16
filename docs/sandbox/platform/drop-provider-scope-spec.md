# Spec: 删除 sandbox_provider_config.scope/user_id/repo_key——Provider 范围全由凭据承担

- 日期：2026-10-30
- 状态：已实施（2026-10-30，并入"删系统类别"用户裁定：cred_id=0 Provider 整体废弃，权限单一条件=凭据 owner，手工创建端点 410）
- 背景（用户裁定）：Provider 表的 scope/user_id/repo_key 是凭据生成时的**快照拷贝**，
  与凭据失同步（同已删 name 列的问题）；三表三种枚举（凭据 global/user/repo、
  Provider system/user、引擎 global）历史分叉。范围（可见性/仓库绑定/全局性）单一权威源
  = 凭据表。现网仅 1 个 Provider（38），且其凭据 50 已吊销——38 是孤儿，无活数据。
- 本 spec 是 repo-llm-credential 的**前置地基**（那边的路由判定从"Provider scope"
  改为"凭据 scope"）。

## 用户裁定要点

1. Provider 不再有 scope 字段——范围全由凭据承担
2. Provider 表只留"接入配置"（platform/endpoint/key/extra/max_sandboxes/enabled）
   + 关联键 runtime_credential_id
3. 孤儿 Provider 38 顺手清（凭据已吊销，按"生命周期=凭据"裁定本该随级联消失）

## 改动点清单（7 项）

### 1. 数据层迁移（db_setup.py）

- 建表段 DDL 去 scope/user_id/repo_key 三列（连带 FK user_id REFERENCES）
- 幂等迁移：重建表（同删 name 列模式：foreign_keys=OFF + legacy_alter_table 双保险）
- 迁移前先清孤儿（runtime_credential_id 不在 runtime_credentials 里的行 DELETE）

### 2. 凭据生成链（settings.py:1581 区，生成 Provider 的 INSERT）

- INSERT 去 scope/user_id/repo_key（凭据生成时不再向 Provider 拷贝快照）
- UPDATE 分支同步（同 UPSERT 的 SET 列）

### 3. 列表可见性（settings.py:3043 base_where）

- `WHERE (spc.scope='system' OR spc.user_id=?)`
  → `WHERE (rc.scope='global' OR rc.owner_id=?)`（JOIN 凭据；无凭据孤儿已在迁移清掉，
  剩余系统预置 cred_id=0 → admin 可见，单独 OR 分支）
- 列表/详情 SELECT 输出的 scope 字段值改取 `rc.scope`（字段名保留——前端零改动，
  值域从 system/user 变 global/user/repo，与凭据一致）
- user_id/repo_key 输出改取凭据列（rc.owner_id/rc.repo_key）

### 4. 操作权限（4 端点：模板六端点/编辑/删除/删除预览/toggle/health/scan）

- 旧：`scope=='system' 且 cred_id=0 → 403`；`system+cred → _sb_cred_report_owner`
- 新：**`cred_id=0 → 403（系统预置，不可操作）`；`cred_id>0 → _sb_cred_report_owner`**
  （cred_id 单条件，不再看 scope；用户级 Provider 的 user_id==user.id 判定删除——
  凭据 owner 判定已覆盖）
- prow SELECT 的 scope 列引用清理（20 处读写点逐一）

### 5. worker 侧（cloud_worker.py:136）

- `agent_tags=['all'] if Provider scope=='system'` → 按 provider-configs 下发的凭据
  scope（服务端 provider-configs 端点 2164 区输出加 `credential_scope`）判 global

### 6. 前端（sandbox.js）

- 「系统」徽标：`scope==='system'` → `credential_scope==='global'`（或 cred_id=0 兜底
  系统预置——迁移后不存在，保底文案）
- 编辑弹窗 scope_level 选项（全局/用户/仓库）：字段含义变为**凭据级别只读展示**
  （级别改在凭据上改，平台编辑弹窗不再有 scope 选择——保留只读展示行）
- 详情卡 Scope 字段值取凭据 scope（global→全局/user→个人/repo→仓库+repo_key）

### 7. repo-llm-credential spec 联动修订（文档）

- 其 §3 路由判定"Provider scope"→"凭据 scope"（rc.scope='global'/'repo'→仓库 LLM 凭据；
  'user'→用户 LLM 凭据）——两 spec 的衔接点

## 验收

1. 迁移后 PRAGMA：Provider 表无 scope/user_id/repo_key 列；孤儿 38 已清（或凭据重生成后正常关联）
2. 生成凭据（global/user/repo 三档）→ Provider 落库无范围字段；列表显示对应级别
3. 权限：凭据 owner 可 toggle/编辑/删除自己的 Provider；伪造 cred_id=0 → 403
4. 可见性：global 凭据的 Provider 全员可见；user 凭据仅 owner（+admin）
5. worker：global 凭据的引擎 claim 全局任务正常（tags=['all']）
6. claim/模板/上报/吊销级联全链回归（凭据吊销 → Provider 级联删除按 cred_id，不受影响）
7. 17 套件全量 + .17 热修重启验证

## 改动量

- db_setup ~40 行（DDL+迁移+孤儿清理）
- settings.py ~80 行（20 读写点改造）
- runtime.py ~10 行（provider-configs 输出 credential_scope）
- cloud_worker.py ~3 行
- sandbox.js ~20 行
- 预估 1 天；先于 repo-llm-credential 开工
