# Spec: 仓库级 LLM 凭据——按沙箱平台 scope 路由模型凭据来源（用户裁定 2026-10-30）

- 日期：2026-10-30
- 状态：spec 定稿待开工
- 背景：现 LLM 凭据只有用户级（user_llm_credentials，用户设置页管理）。用户提出按
  Provider scope 路由凭据来源：全局/仓库级平台→仓库 LLM 凭据，个人级平台→用户 LLM 凭据。

## 1. 用户裁定汇总

1. 仓库设置页新增「LLM 凭据」设置区（对称用户设置已有的）
2. 路由：平台 scope=system（全局）或 user+repo_key（仓库级）→ 工作仓库的 LLM 凭据；
   平台 scope=user 无 repo_key（个人级）→ 用户设置 LLM 凭据
3. **独立表 `repo_llm_credentials`，加密存储**（crypto encrypt，同 user 表口径）
4. **仓库级凭据 = 仓库资产**：所有有权限使用该仓库的人的任务均可用，不限创建者本人；
   **仅仓库管理员可编辑/新增**（权限口径与仓库 Bot Token 一致）
5. **不降级**：取仓库凭据失败不得回退用户凭据（反之亦然）；缺凭据任务按失败处理
6. **回显不展示明文 key，留空不修改**（同仓库 Bot Token 交互模式）
7. 全局平台理论应从组织取凭据——**现无组织编辑界面，暂降级到仓库取，记 TODO 后续改**

## 2. 数据层

```sql
CREATE TABLE IF NOT EXISTS repo_llm_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key VARCHAR(512) NOT NULL,          -- 如 gitcode:And-ZJ2/hello-calculator
    engine_type VARCHAR(64) NOT NULL,        -- hermes/opencode/.../common(通用)
    provider VARCHAR(64) NOT NULL DEFAULT '',
    api_key_enc TEXT,
    base_url VARCHAR(512) DEFAULT '',
    model VARCHAR(128) DEFAULT '',
    enabled INTEGER DEFAULT 1,
    updated_by VARCHAR(64) DEFAULT '',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(repo_key, engine_type)
);
```

- 加密：`src/utils/crypto.encrypt/decrypt`（与 user_llm_credentials 同口径）
- 无迁移：新表 + 现网无数据

## 3. 路由层（核心改动）

`runtime.py /tasks/{task_id}/env`（~810 attach 处）：

```
[drop-provider-scope 联动修订 2026-10-30] Provider 表已无 scope 列——路由判定改读凭据:
task → runtime_id → agent_runtimes.runtime_info.provider_id
     → sandbox_provider_config.runtime_credential_id → runtime_credentials.scope 判定:
    rc.scope='global'（全局；TODO 组织级就绪前暂走仓库）或 'repo'（仓库级）
      → select_llm_cred(mode='repo', repo_key=task.repo_key, engine_type)
    rc.scope='user'（个人级）
      → select_llm_cred(mode='user', user_id, engine_type)
    取不到 → **任务级显式失败**（A，用户裁定 2026-10-30 补）：
      /tasks/{id}/env 返回 llm_cred_missing=true + source('repo'|'user') + 指引文案字段
      → cloud_worker 收到后**不建箱**（claim 后建箱前拦截，不浪费 43s 冷启动）
      → 任务 fail，result_text: 「缺少 LLM 凭据：该任务使用{仓库级|个人级}沙箱平台，
        请在【仓库设置|用户设置 → LLM 凭据】为引擎 xxx 配置」
```

`llm_cred_selector.select_llm_cred` 改造：加 `mode`/`repo_key` 参数；
- mode='repo' → repo_llm_credentials（表内两级保留：engine 专属→common）
- mode='user' → user_llm_credentials（原逻辑）
- **跨 mode 零回退**

无 runtime_info/非云引擎任务 → 维持现行为（user 级）——普通 worker 本机引擎不涉
沙箱平台 scope，归用户级（现状）。

## 4. API 层（settings.py，对称用户 LLM 凭据四端点）

```
GET    /settings/llm-credentials/repo?repo_key=…      列表(不回明文: api_key 配置状态布尔)
POST   /settings/llm-credentials/repo                 新增/更新(repo_key+engine_type UPSERT)
PUT    /settings/llm-credentials/repo/{id}/toggle     启停
DELETE /settings/llm-credentials/repo/{id}            删除
```

- 权限：**仓库管理员**（口径复用仓库 Bot Token 的判定——repo settings 写权限链）
- api_key：POST 留空 = 保留旧值（UPDATE 不带 api_key_enc）；非空 = encrypt 覆盖
- 回显：api_key 永不返回明文（只返回 `has_key: true/false`）

## 5. UI 层

| 位置 | 内容 |
|---|---|
| 仓库设置页（settings.js gitcode tab，Bot Token 组下方） | 「LLM 凭据」区：引擎类型下拉+provider/base_url/model/api_key(placeholder=已配置时'留空保留旧值')表单 + 已配列表（引擎/启停/删除）——表单结构参照 account.js LLM 区移植 |
| 平台编辑弹窗（sandbox.js，凭据来源提示） | 按 scope 显示：「任务执行时模型凭据取自：本仓库设置（仓库级/全局平台）」/「用户设置（个人级平台）」 |
| 凭据未配置状态提示（B，用户裁定补） | 来源对应位置无 enabled 配置时，弹窗/详情显示黄色「⚠ 未配置 LLM 凭据，任务将无法执行」+ 去配置跳转链接 |
| 平台详情卡（Provider 信息卡） | 同上一行提示（Scope 字段旁） |
| i18n zh/en | ~10 词条 |

## 6. 不做 / TODO

- 组织级凭据（全局平台的"正确"来源）——无组织编辑界面，暂从仓库取，**TODO 后续迁移**
  （本 spec 在代码注释与 docs/todo 留痕，迁移时只需改路由层 mode 判定）
- 跨级降级（用户裁定禁止）
- 普通 worker 本机引擎的凭据路由（维持用户级现状）

## 7. 实施顺序与验收

| 序 | 改动 | 验收 |
|---|---|---|
| 1 | 建表 + 四端点 | curl 全链：创建/回显(无明文)/toggle/删除；非管理员 403 |
| 2 | select_llm_cred mode 化 + env 路由 | repo 平台任务 → env 注入 repo 凭据；user 平台任务 → 用户凭据；repo 缺 → 不注入 |
| 3 | 仓库设置页 LLM 区 | 表单+列表+留空不改；无明文回显 |
| 4 | 平台弹窗/详情提示 | 三种 scope 文案正确 |
| 5 | E2E | 沙箱任务实测：仓库级平台跑任务 → 沙箱内 _LLM_* 为仓库 key |
| 5a | 缺凭据拦截（A） | 仓库级平台无配置 → 任务 fail 不建箱，result_text 含指引文案；配好后重跑通过 |
| 5b | 未配置提示（B） | 平台详情/弹窗显示 ⚠ 未配置 + 跳转；配置后消失 |
| 6 | 回归 | 相关 pytest + node --check + 渲染冒烟 |

## 8. 改动量

- 后端 ~150 行（表 15 + 端点 80 + 路由 40 + selector 15）
- 前端 ~120 行（settings.js LLM 区 80 + sandbox.js 提示 20 + i18n）
- 预估 1 天
