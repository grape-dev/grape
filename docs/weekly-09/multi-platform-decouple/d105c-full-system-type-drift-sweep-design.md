# D105c 全系统 id 类型漂移深扫与全修设计——需求 + 设计（2026-11-09）

> 本分片是「四路深扫 → 全修批次」的唯一权威需求/设计记录。机制固化（不变式守卫/新用户 e2e）细节见 d105b-account-fk-user-id-text-design.md「机制固化」节；台账见 06-ledger.md D105-7。

## 一、需求（用户裁定）

D105b 修复绑定转移 410 后，用户质询"为什么之前没发现、还有哪些类似问题"。四路并行深扫（schema / Python / 前端 / SQL 语义）确认同族问题共 **4 级 50+ 处**，用户裁定**全部修复 + 机制固化**。

## 二、问题成因（结构性归因，五层）

1. **中间态无门禁**：主键 TEXT 化分批推进，外键 INTEGER 的中间态没有机器可查的不变式约束，被新代码持续依赖。
2. **SQLite 亲和遮蔽**：'24' 写 INTEGER 列能存能查，错误只在 Python 裸比较一处显形，每例都像孤例。
3. **扫雷方法论缺陷**：历轮都是 grep 已知坏模式（伤疤驱动），未从 schema 出发反查引用列（模型驱动）。
4. **测试长在数值 id 上**：fixture 全是整数 id，没有不变式级断言（typeof 一致性），uuid 形态从未进过用例。
5. **实弹样本偏差**：uuid 用户从未真实存在，"注册新账号走核心链路"不在验证清单。

## 三、深扫发现全清单（按炸裂风险）

### P0——现存即坏
- 前端：runtimes.js:670/671/679 onclick 裸注入 uuid credential_id → SyntaxError，运行时页三按钮全废
- 后端同链路：settings.py:1849/1883/2009 `cid: int` → uuid 422
- SQL：`||` 拼接经 MySQL 翻译变逻辑 OR——pr_merged_sre_retry.py:318（重试备注被写成 0/1）、repos.py:1362（provider 反查恒真返错仓库，except: pass 吞错续跑）

### 高危——uuid 用户走到即 500/断
- schema 漏列：grape_t_patrol_findings.owner（0002 widen 名单漏）、issue.release_approved_by、organization.owner_id（第三批）
- Python：patrol.py:113 int(owner)；repos.py:1784/1875 与 event_pipeline.py:4220 `_host_uid > 0`；issues.py:1611 `user_id > 0`

### 中危——静默变义
- 时区双轨：datetime('now') 翻译成 NOW()（server 时区）vs UTC 比较——租约判定漂 8h、维护批删漂 8h；verification/login_throttle/audit 用 localtime 写戳
- session_api_tokens.user_id 存量 int 与新 str 互查（CLI 权限误判）；IdentityDTO.id:int 注解；IM `or 0` 哨兵；wal_checkpoint 未进白名单
- 前端 latent：40+ 处裸注入（对应表 uuid 化即集体 SyntaxError）、pipeline.js parseInt("42IK")→42 静默错发

### 低危/挂账
- settings.py:727 LIMIT 无序（全局开关读行不确定）；无 ORDER BY LIMIT 族；organization/org_id 第三批迁移时需把 org_id/owner_id 纳入名单（本轮名单未含，已写进第三批预警）

## 四、修复设计（四批并行，文件面互斥）

| 批 | 范围 | 关键设计 |
|---|------|---------|
| F1 前端 | js 全站裸注入加引号（P0 3 处 + latent 40+）+ pipeline.js ident 保形 + 版本 bump | 统一 `fn('id')` 引号注入（对齐 admin.js 已修风格） |
| F2 Python+schema | 6 处类型雷 str 归一；迁移 202611091100 把 owner/release_approved_by 纳入 TEXT 枚举 + 0002 widen 名单补全 | 比较边界统一 str() 双侧；哨兵 0 → ''/None |
| F3 SQL 语义 | `||` 翻译通用化（_CONCAT_RE 扩展或两处手改，安全取舍由实现定）；UTC 基线：mysql 连接 session `SET time_zone='+00:00'` + Python localtime→gmtime；wal_checkpoint 入白名单；cid:int→str；LIMIT 加确定性序 | 优先翻译层通用化（根治一类），手改兜底 |
| F4 机制 | 不变式守卫测试（引用 TEXT 主键的列必须 TEXT 且无混型——加表必红）+ uuid 新用户全生命周期回归 + 文档收口 | 把"坑清单不全"升维成"不变式自动校验" |

## 五、验收口径

- 守卫：本轮新增/扩展测试全绿（转移族/迁移族/凭据族/守卫族），基线既有失败双侧 diff 排除
- 实弹：双实例重启后健康检查、无新增告警；用户页面复验 runtime 凭据三按钮恢复
- 机制：schema 不变式守卫进 CI 常跑；deploy-checklist 增加"注册新账号走核心链路"步骤

## 六、边界（不做的，挂账）

- organization/org_id 第三批 TEXT 化（涉及 436 处引用，另批执行；org_id/owner_id 需纳入名单——本轮已写入预警）
- 迁移/自省链路在 MySQL 启动路径的 SQLite-only 依赖（sqlite_master/PRAGMA/typeof）——**已升格为 P0 部署门槛**：现网 MySQL（grape 库）实存且有历史真实流量（2026-11-09 实查，最后写入 10-21/10-22），接入本分支前必须完成 deploy-checklist-mysql-live-prod.md 全部门槛（启动迁移 MySQL 分支 + 存量数据演练）
- runtime_id/skill_id 等自增表 id 族（主键未 TEXT 化，自洽，无需处理）
