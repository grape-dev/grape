# Issue #455 需求文档（Spec）

> 0→1 安装引导（OSS 主线）实测问题清单与改进建议
> 来源：实测用户在干净 Linux（Python 3.9 · SQLite · 单机 · GitCode+GitHub 双 OAuth）上按 README 从 0 到 1 安装的 8 个卡点。
> 目标分支：`weekly-XX`（场景基线，合入前以仓库实际开发周分支为准）。

## 1. 目标与价值

- 让新用户按官方 README 可在一次干净安装内走通「拉取 → 启动 → OAuth 登录 → 托管 → 首个任务」。
- 显著降低安装试错成本：环境/配置声明先行、启动可诊断、数据一致性、失败可解释、网络健壮性。
- 交付用户侧「0→1 安装自检清单」文档。

## 2. 范围

### In Scope（M1 → M2 → M3）

| 里程碑 | 优先级 | 卡点 | 内容 |
|--------|--------|------|------|
| M1 | P1 | 1 | README/requirements/doctor 声明 Python ≥3.10；启动入口低版本显式提示退出 |
| M1 | P1 | 2 | 对外基址（server_base_url）配置文档/示例；未配置时启动日志告警 + /api/health 提醒字段 |
| M1 | P1 | 3 | OAuth 凭据配置文件（configs/oauth-providers.json）文档化：双 provider client_id/secret/redirect_uri 示例 |
| M1 | P1 | 4 | SQLite 建表补齐 `pipeline_instances`（及核对被引用表全集）；清理逻辑访问前存在性检查降级 |
| M1 | P1 | 5 | GitHub OAuth 落库：事务提交/回滚确认 + 创建后读回校验，失败返回失败而非静默 302 |
| M2 | P2 | 6 | 托管接口 502：补充上游调用/错误码/超时上下文 + 可重试路径提示 |
| M2 | P2 | 7 | 非所有者托管 403：归属/权限前置校验 + 前端 UI 明确提示（i18n） |
| M3 | P3 | 8 | 拉取路径（git fetch/clone）有限次重试（网络类退避，4xx 快速失败）+ 镜像/代理文档 |
| M3 | P3 | 9 | 交付 `docs/0-to-1-checklist.md` 自检清单文档 |

### Out of Scope

- 云厂商特定错误码适配、多机/集群部署文档、OAuth 框架重构。

## 3. 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| Python 版本不满足时行为 | 显式提示退出 | 排查成本高是实测卡点根因 |
| SQLite 缺表处理 | 补建表 + 访问前存在性检查降级 | 双保险，兼容老实例 |
| GitHub 落库异常时回调 | 读回校验失败 → 失败响应 + ERROR | 数据一致性优先 |
| 403 提示 | 前置校验 + UI i18n 提示 | 用户可理解失败原因 |
| 网络重试 | 有限次退避（网络类错误），4xx 快速失败 | 避免挂死，可预期 |

## 4. 受影响模块

| 模块 | 改动 |
|------|------|
| README.md / README.zh.md / requirements*.txt / scripts/doctor.py | Python 下限声明、基址与 OAuth 配置说明 |
| src/api/run.py / src/api/main.py | 启动版本检查、基址未配置告警 |
| src/api/routes/health.py | 健康检查返回基址配置状态提醒字段 |
| src/api/db_setup.py | 补建 pipeline_instances 表 |
| src/api/onboarding.py / src/repo_cleanup.py | 清理前表存在性检查降级 |
| src/api/routes/grape_auth.py | GitHub OAuth 落库读回校验 |
| src/api/routes/repos.py | 托管 502 上下文、403 前置校验 |
| src/api/static/js/pages/repos.js + locales | 403 前端提示（i18n） |
| src/runtime/repogit.py（+相关拉取路径） | git 拉取有限次重试 |
| docs/0-to-1-checklist.md（新增） | 安装自检清单 |

## 5. 验收标准（对应 Issue 8 条 + 测试）

- [ ] README「环境要求」含 Python ≥3.10；低版本启动有显式提示
- [ ] 文档含对外基址配置步骤与示例；未配置时启动日志/健康检查有告警
- [ ] 文档含 OAuth 凭据配置文件、字段与双 provider 示例
- [ ] 全新 SQLite 实例清理流程不再报「表不存在」（新库补建表；老库存在性降级）
- [ ] 托管接口 502 有明确错误上下文与可重试路径
- [ ] GitHub OAuth 登录后账号与第三方绑定可查（读回校验通过）
- [ ] 非所有者托管有前置校验与 UI 明确失败原因
- [ ] 拉取路径有限次重试；文档含镜像/代理建议
- [ ] 交付「0→1 安装自检清单」文档
- [ ] 现有测试全部通过；新增测试覆盖新功能

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| grape_auth 落库改动影响 GitCode 登录路径 | High | 只动 GitHub 分支语义；读回校验失败即失败返回；补对称性测试 |
| 建表补齐影响存量库 | Med | CREATE TABLE IF NOT EXISTS + 存在性检查降级 |
| OAuth 文档与真实字段不一致 | Med | 以代码内 migrate_oauth_providers 逻辑逐字段核对 |
| 重试改动放大上游压力 | Low | 仅网络类错误、有限次、退避 |