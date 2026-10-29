# Spec: Issue #473 — 设备与引擎页 runtime 列表范围筛选

> 来源：Issue #473 Spec（评论 id=196119974）| 分支：feat/issue-473（base local-liufei）

## 目标
运行驾驶舱「设备与引擎」页 runtime 列表新增**范围筛选**（全部/仓库/个人），避免全局所有设备/引擎一锅端展示。

## 现状
- `/settings/agent-runtimes`（Engines）与 `/settings/runtime-devices`（Workers）已有 scope（local/user/repo/global）标签筛选、`q` 搜索、分页、吊销过滤（#417/#436/#465）与 `repo` 数据隔离（#277），但无「查看范围」维度：默认口径 = 全局 + 个人 + 我所在仓库的仓库级，全局设备全部混入。

## 期望行为
- 列表新增范围控件：**全部（默认，=现有全量口径）/ 仓库（仅当前仓库 repo scope 设备与引擎，复用 #277 repo 过滤口径）/ 个人（仅当前登录用户名下 user scope 设备与引擎，含 owner=当前用户的本机引擎）**。
- 范围与既有 scope 标签、搜索、分页、吊销过滤组合共存；空结果返回正常空态（不回退 #436）。
- 控件展示当前选中范围（高亮 + 范围标签前缀）。

## 验收标准
- [x] 范围控件含当前选中范围提示；仓库→仅当前仓 repo scope；个人→仅当前用户 user scope（含个人本机引擎）；全部→全量口径
- [x] 与搜索/分页/吊销过滤组合正确，无空结果误报（不回退 #436）
- [x] 新增测试覆盖各范围口径及组合过滤场景；现有测试通过

## PR #668 审查修复（QA Required + Nice-to-have）
- [x] Required：range=user 的「含本机引擎」落地——is_local 条目以引擎 DB 行 owner_id（agent_runtimes.owner_id）判定归属（输出 owner 对 cid=0 恒空，原判定不可达）；本机设备行补充 local_owner_id；补 cid=0 测试（本人可见 / 他人不可见 / 无主不误入 + 全量口径回归）
- [x] Nice-to-have：未知 range 值不再静默回落全量——两端点 range 参数升级 Literal[""/"all"/"repo"/"user"]，非法值 422（测试覆盖）
- [x] Nice-to-have：range 遮蔽内置——文件内无内置 range() 调用（AST 确认），注释说明保留 API 命名一致性
- [x] Nice-to-have：新测试接入 CI——windows-smoke.yml 新增 linux-backend job 跑 #473 后端 pytest + 前端 node 源断言

## 范围外
- 权限模型重构（沿用 #277 可见性规则）；驾驶舱其它页面。
