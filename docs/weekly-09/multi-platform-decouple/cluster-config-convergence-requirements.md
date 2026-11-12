# 需求：OAuth/平台配置的集群共享优化（配置权威收敛 DB + 版本化种子导入）

> 状态：独立立项（从 D102 移交清单拆出——集群部署正确性需求，与多平台解耦分支主题正交）
> 优先级：P1（多实例部署的正确性前提）
> 关联：P1-5（get_provider 缓存缺陷）已在本分支修复（D102-D，对齐 platform_card 统一入口形态）——R1 前置已闭环
> 基准：feat/weekly-08-yaw-multi-platform-decouple（R1 落地后）

## 1. 背景

两根配置轴在多实例部署下共享语义不一致：登录渠道卡（meta 表，DB 共享 ✓）
vs 仓库平台卡（meta 覆盖快照 > 本地文件 > 代码内置，文件层每机一份会漂移 ✗）。

叠加缺陷：get_provider TTL 过期不重载（已修 D102-D）；invalidate 只失效本
实例内存，集群其他实例最坏等 TTL。

另有语义矛盾：admin CRUD 写 meta 覆盖文件层快照，但覆盖与文件间无版本关联
——回滚、审计、漂移检测做不到。

## 2. 目标

1. 多实例下任一实例配置变更，全集群 ≤60s 可见（无需重启/逐机操作）；
2. 配置权威唯一（DB meta），文件降级为版本化种子；
3. 带外直写 meta 机制上不再构成有效配置变更；
4. admin 管理面 CRUD 热生效语义不变。

## 3. 改动点（R1 已闭环，余 R2-R5）

### R1（已闭环 ✅）get_provider TTL 重载
本分支 D102-D 修复：无条件走 _load_providers（内含 TTL 双重检查锁），
对齐 platform_card.get_platform_card 统一入口形态；行为锁
test_d102d_provider_cache_ttl 3 用例。

### R2 TTL 收紧（缓行，见裁决）
原建议 300s→60s。**评估结论：缓行**——R3 落地后配置变更走"文件部署+
导入"或 admin 面板（均主动失效），TTL 只是集群其他实例收敛的兜底；
登录配置为分钟级变更场景，用轮询解决推送该解决的问题不划算。
先上 R3 观察，确有实时性诉求再启用（可结合 R4 版本号轮询一次到位）。

### R3 配置权威收敛 DB：文件 → 版本化种子导入
meta 存储升级（向后兼容）：{"content": {...}, "source_hash": sha256,
"imported_at", "imported_by": "startup|admin"}。
启动时种子导入：文件整体哈希与 meta source_hash 比对——一致跳过（幂等，
多实例并发启动安全）；不一致（文件有版本更新）导入并记录；admin 在线改过
（imported_by=admin 后 hash 又变）不自动顶回，WARNING + 管理面显示
"文件有更新未导入"（在线覆盖不被升级静默冲掉）。
运行时读路径只读 meta。**实施注意**：平台卡当前为"目录文件 + meta 覆盖"
双层合并，改纯 meta 后本地开发/测试环境无 meta 种子会空卡——种子导入触发
时机须含测试环境首启（或保留内置默认卡为最终兜底层，推荐后者，与
providers _DEFAULT_PROVIDERS 模式对齐）。

### R4 版本号快速收敛（可选，随 R2 裁决）
meta config_version 整型计数 + 各实例 10s 比对。与 R2 合并裁决。

### R5 闭环带外写入
R3 落地后直写 meta content 无 source_hash 不构成有效变更；管理面显示
source_hash/imported_at/一致性状态。

## 4. 验收标准

1. 双实例（共享 DB）：实例 A 改配置 → 实例 B TTL 内可见（登录端点实测）；
2. 带外写 meta 不经 invalidate → 最坏 TTL 收敛（R1 已达成单实例侧）；
3. 新增平台卡 json → 任一实例重启 → 全集群出现（种子导入）；
4. admin 在线覆盖后更新 json → 重启不顶掉在线覆盖，漂移可见；
5. admin CRUD 热生效不回归；S83 未知字段整卡拒绝守卫不回归；
6. 回归：四平台登录 + 仓库操作全链路、华为 OIDC 登录。

## 5. 兼容

meta 旧格式（纯 content）视为 source_hash="" 遗留数据，种子导入自然接管；
SQLite/MySQL 双后端适用（单行 KV 无方言问题）；并发首次导入 INSERT OR
REPLACE + hash 比对幂等。

## 6. 实现注意

tests-first：①get_provider TTL 重载行为锁（已交 test_d102d）；②种子导入
幂等（双启并发）；③admin 覆盖 vs 文件更新优先级；④带外写 meta 后 TTL 收敛。
_load_providers/_load_cards 线程锁语义保持（P2-2/BUG-15 勿回归）。
文档：多实例部署 Checklist（D96 体系）补"配置变更操作规程"——只允许
admin 管理面或部署流水线，禁止手改 meta。
