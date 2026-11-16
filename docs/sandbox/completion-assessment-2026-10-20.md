# 沙箱子系统开发完成度评估（feat/devbox-sandbox-integration2 @ 5ddd9aaa7）

> 评估日期：2026-10-20
> 对照基线：
> - 主设计 [cloud-sandbox-provider-pluggable-design.md](cloud-sandbox-provider-pluggable-design.md)（R1-R17 需求 / P1-P11 实施计划 / 验证矩阵）
> - 迁移记录 [../sandbox-migration-record.md](../sandbox-migration-record.md)
> - 增补设计 [sandbox-management-enhancements-design.md](sandbox-management-enhancements-design.md)

## 一、总体结论

| 维度 | 状态 |
|------|------|
| 代码层（静态对照 R1-R17） | **17/17 全部落地** ✅ |
| 迁移完整性 | 曾有 **2 个 P0 级漏点**，本会话已全部补齐（见 §四） |
| 本地部署验证 | Provider/模板/绑定/扫描/检查/编辑链路全部实测通过 ✅ |
| cloud_worker 端到端 | **未验证**（本机未部署 cloud worker，run/ 下无 cloud runtime）|
| 系统 Provider 预置（M13） | **形态变化**：db_setup 硬编码 seed 改为 init task 配置文件导入（等效，见 §三） |

## 二、R1-R17 需求逐项核查（静态代码 + DB 实测）

| # | 需求 | 状态 | 证据 |
|---|------|------|------|
| R1 | 多 Provider（name/platform/scope/user_id/repo_key） | ✅ | db_setup DDL + DB 实列（cube/user scope） |
| R2 | 系统 Provider 只读 | ✅ | settings.py 写操作 403 分支 |
| R3 | 用户 Provider CRUD（+toggle/health/delete-preview 两步删除） | ✅ | settings.py 全套端点 |
| R4 | 模板-Provider 关联（provider_id FK + CASCADE） | ✅ | sandbox_templates 表 + 删除级联实测 |
| R5 | 引擎模板唯一约束 | ✅ | engine_template_binding 表 UNIQUE(provider_id, engine_type)，一模板多引擎实测（hermes+opencode+codex） |
| R6/R7 | cloud_worker 自动同步 + 按模板展开 runtime | ✅ 代码 | _expand_templates（三段式 name `{engine}:{platform}:{provider_id}`） |
| R8 | 引擎 scope 独立（register 从 provider_id 解析） | ✅ | runtime.py register + owner_name 列 |
| R9 | 用户敏感数据走用户 token | ✅ | /env 返回 USER_GITCODE_TOKEN（本会话补齐，5ddd9aaa7） |
| R10 | worker 凭据只获取公共数据 | ✅ | /env LLM 凭据按 engine_type 两级查询（同上） |
| R11 | 池广播隔离（agent_tags） | ✅ | cloud_worker 系统=[\"all\"] / 用户=[] |
| R12 | /env 越权防护 | ✅ | runtime_id 非零时校验 credential_id 匹配（同上） |
| R13 | token 互斥注入 | ✅ | cloud_worker chat→USER / devops→BOT 分支 |
| R14 | SSL per-provider | ✅ | provider_id 落盘 + _post_create_hook |
| R15 | provider_name/platform 分离 | ✅ | CloudSandboxBase 双参数 |
| R16 | Runtime 孤儿清理 | ✅ | 三段式名 + offline 注册清理 |
| R17 | 过滤点直读 agent_runtimes | ✅ | claim_by_tag / _member_scope / _check_pool_engines_reachable |

测试证据：11 个沙箱相关套件 **397 passed**（仅 2 失败为旧分支继承的 TestCloneRepo mock 基线，stash 对照已排除）。

## 三、P1-P11 实施计划对照

| 阶段 | 状态 | 说明 |
|------|------|------|
| P1 DB 改造 | ✅ | 全部表结构落地（DB 实测列齐） |
| P2/P3 Provider+模板 CRUD | ✅ | 含本会话增补：编辑/检查/扫描/engines 显示 |
| P4 cloud_worker 展开 | ✅ 代码 | 未部署验证 |
| P5 SandboxManager/Context | ✅ | provider_id 路由 |
| P6 provider-configs 按 id | ✅ | /api/runtime/provider-configs |
| P7 存量迁移兼容 | ✅ | init task 幂等导入（**M13 形态变化**：原设计 db_setup 硬编码 3 模板 seed，实现为 sandbox_provider_init.py order=400 配置文件扫描导入——adapter 驱动，新增平台零改动，优于原设计） |
| P8 E2E（三引擎×多Provider×cloud_worker） | ❌ **未做** | 需部署 cloud worker 环境 |
| P9 /env 加固 | ✅ | 本会话补齐（5ddd9aaa7） |
| P10 USER_GITCODE_TOKEN 注入 | ✅ | cloud_worker 代码就位，待 P8 一并验证 |
| P11 安全验证 | ◐ 部分 | 代码就位（fencing/RedactEngine/proxy token），缺运行态验证 |

## 四、迁移漏点复盘（本会话发现并修复的 2 个 P0）

| # | 漏点 | 症状 | 修复 commit |
|---|------|------|------------|
| 1 | `RuntimeClient.get_task_env_full` | cloud_worker 执行任务即 AttributeError（云沙箱任务执行链路整体阻断） | f4aeaf954 |
| 2 | `/tasks/{id}/env` 只有 main 基础版 | 缺 §12 三块：越权校验（安全洞）/ USER_GITCODE_TOKEN（chat 链路断）/ LLM 凭据注入（云沙箱推理断） | 5ddd9aaa7 |

**为什么漏**：两处都源自旧 commit `7857a3592`（cloud_worker 独立进程 + 服务端 API 扩展）。该 commit 在迁移记录的 123 个 commit 明细表中**没有单列条目**——它改动横跨 8 个文件（新增 cloud_worker.py/client 段/runtime.py 段/db_setup 段），迁移时被当作"cloud_worker.py 整包移植已覆盖"处理，但其**落在共享文件（runtime.py、client.py）里的 hunk** 属于"第 2 类：集成适配"，需要逐 hunk 对照，这两个 hunk 被遗漏。同 commit 的 db_setup/cloud_worker.py 部分因整包移植而幸存。

**方法论教训（已补入 enhancements 文档 §六）**：
1. 迁移完整性检查必须包含**调用方-被调方符号对账**——`cloud_worker.py` 调 `client.get_task_env_full` 这类跨文件引用，只看"文件是否移植"发现不了缺口。可机械化：对移植后的 cloud_worker.py 做 `client.<method>` 全提取，逐一核对 client.py 是否定义。
2. **commit 分类粒度**：横跨"整包文件 + 共享文件"的 commit 不能整体标"整包移植"，共享文件部分必须单列。

## 五、与主设计的偏差

1. **M13 预置链路**（见 §三 P7）：实现优于设计（adapter 驱动 vs 硬编码）。
2. **模板 engines 读路径**：原设计 §2.1.3 要求所有读路径 JOIN binding——列表端点漏了（展示层 `engines` 列恒空），本会话修复（29af3af0b）。
3. **增量能力超出原设计**：模板扫描（adapter 可选能力 list_templates）、手动检查、编辑、tooltip 等，见 [sandbox-management-enhancements-design.md](sandbox-management-enhancements-design.md)。

## 六、遗留工作（按优先级）

1. **P8 E2E（最重要）**：部署 cloud worker（systemd/grape-cloud-worker.service 已就位），
   走完验证矩阵：系统/用户/仓库级 Provider runtime 注册 → claim → 建箱（tpl-f0c43d1b）
   → 引擎安装（hermes/opencode fallback / codearts 预装约束）→ token 互斥注入 → kill。
   本机已具备条件：CubeSandbox 平台 192.168.0.17、credential id=1(system) 已在 DB。
2. **悬空绑定治理**：模板删除后 binding.template_id 悬空（本会话手动清过一次）。
   建议 scan-templates 导入前顺带 DELETE 悬空行，或给 unbind 加按行删除。
3. **TestCloneRepo 2 例基线失败**：旧分支继承的 mock 队列不匹配，修一次 mock 序列即可
   （需算上 `_run_long_command` 的 4 个 rm -f 清理调用）。
4. **环境前提**：生产部署前必须配 configs/crypto.json（或 GRAPE_DB_ENCRYPTION_KEY），
   否则 Provider/LLM 凭据保存 500（fail-closed）；密钥丢失=已存密文全废。
