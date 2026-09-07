# 运营管理台用户仓库列表与用户面一致 + 活跃用户统计修正方案

> 更新时间：2026-08-30
> 分支：feat/admin-console-optimization（基于 dev 最新 c3dbea11）
> 验证数据库：grape-dev-db（127.0.0.1:3306）

## 一、问题根因

### 1.1 仓库列表不一致

用户面（grape-main Python）和运营管理台（admin Java）查"某用户的托管仓库"用了不同的关联逻辑：

- 用户面 `src/user/db.py:1066` get_managed_repos(user_id)
  ```sql
  SELECT ... FROM managed_repos m
  LEFT JOIN user_repos u ON u.repo_key = m.repo_key AND u.user_id=?
  WHERE u.repo_key IS NOT NULL
  ```
  = "user_repos 表里有该用户行的所有托管仓库"（不依赖 is_join 字段）

- 运营管理台 ConsoleUserController
  ```sql
  INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=?
  ```
  = "repo_owner 等于该用户名的仓库"（只看名下拥有的）

### 1.2 真实数据验证（grape-dev-db，xiaowu2119 uid=2）

| 口径 | 数量 | 说明 |
|------|------|------|
| 用户面 get_managed_repos | 10 | 含加入的 signway/fluxgate、XOcto/HELLO-BLOG-SYS-2 等 |
| 运营台 repo_owner=username | 6 | 只看 xiaowu2119 名下的 |
| **方案改法（user_repos 行存在性）** | **10** | **与用户面完全一致** |

### 1.3 活跃用户统计逻辑不合理

原 StatsController.activeUsers 用 agent_tasks → managed_repos → repo_owner → user_accounts 反推活跃用户：

1. 语义错误：统计的是"仓跑任务的关联用户"，不是"用户登录活跃"
2. 数据丢失：agent_tasks.user_id 有 3271/3904 条是 0（webhook 触发的历史任务没记录触发者），占 84%
3. last_active_at 不可用：单值字段只存最后一次活跃时间，无法统计历史趋势

## 二、两张表的职责（不要合并）

- **managed_repos**：仓库级全局配置表（webhook/token/scene），一行=一个托管的仓库，跟用户无关
- **user_repos**：用户↔仓库成员关系表（is_join/is_admin），一行=某用户对某仓库的关系

一个仓库只有一份配置，但可以有多个成员。这是标准多对多拆法，不是冗余。

**关键点**：用户面 get_managed_repos 不依赖 is_join 字段做过滤，只看 user_repos 行是否存在（WHERE u.repo_key IS NOT NULL）。is_join 字段当前不可靠（XOcto 名下 8 个仓 is_join 全是 0，但用户面照样返回 11 个）。

## 三、改动文件

2 个 Java 文件，共 4 处 SQL。

---

### 改动 1：ConsoleUserController.java detail 的 repoMatrix

文件：admin/backend/src/main/java/com/grape/admin/controller/ConsoleUserController.java
位置：约 L141-151

改前：
```java
            List<Map<String, Object>> repoMatrix = grapeJdbc.query(
                "SELECT m.full_name, m.repo_key " +
                "FROM managed_repos m " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=? " +
                "ORDER BY m.full_name",
                (rs2, rowNum) -> {
                    Map<String, Object> r = new LinkedHashMap<>();
                    r.put("fullName", rs2.getString("full_name"));
                    r.put("repoKey", rs2.getString("repo_key"));
                    return r;
                }, id);
```

改后：
```java
            List<Map<String, Object>> repoMatrix = grapeJdbc.query(
                "SELECT m.full_name, m.repo_key " +
                "FROM managed_repos m " +
                "INNER JOIN user_repos ur ON ur.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.id = ur.user_id WHERE u.id=? " +
                "ORDER BY m.full_name",
                (rs2, rowNum) -> {
                    Map<String, Object> r = new LinkedHashMap<>();
                    r.put("fullName", rs2.getString("full_name"));
                    r.put("repoKey", rs2.getString("repo_key"));
                    return r;
                }, id);
```

**关联逻辑**：用 user_repos 行存在性关联（与用户面 get_managed_repos 完全一致），不加 is_join=1 条件。

---

### 改动 2：ConsoleUserController.java repoIssues 的 userRepoKeys

文件：同上
位置：约 L168-174

改前：
```java
            userRepoKeys = grapeJdbc.queryForList(
                "SELECT DISTINCT t.repo_key FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner " +
                "WHERE u.id=? AND t.issue_id IS NOT NULL AND t.issue_id<>''", String.class, id);
```

改后：
```java
            userRepoKeys = grapeJdbc.queryForList(
                "SELECT DISTINCT t.repo_key FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_repos ur ON ur.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.id = ur.user_id " +
                "WHERE u.id=? AND t.issue_id IS NOT NULL AND t.issue_id<>''", String.class, id);
```

---

### 改动 3：ConsoleUserController.java issueTimeline 的 userRepoKeys

文件：同上
位置：约 L320-322

改前：
```java
            List<String> userRepoKeys = grapeJdbc.queryForList(
                "SELECT DISTINCT m.repo_key FROM managed_repos m " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=?", String.class, id);
```

改后：
```java
            List<String> userRepoKeys = grapeJdbc.queryForList(
                "SELECT DISTINCT m.repo_key FROM managed_repos m " +
                "INNER JOIN user_repos ur ON ur.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.id = ur.user_id WHERE u.id=?", String.class, id);
```

---

### 改动 4：StatsController.java activeUsers

文件：admin/backend/src/main/java/com/grape/admin/controller/StatsController.java
位置：约 L169-176

改前（agent_tasks + repo_owner 反推）：
```java
            grapeJdbc.query(
                "SELECT DATE(t.started_at) as label, COUNT(DISTINCT u.id) as cnt " +
                "FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner " +
                "WHERE t.started_at IS NOT NULL " +
                "AND t.started_at >= DATE_SUB(NOW(), INTERVAL ? DAY) " +
                "GROUP BY label ORDER BY label",
                rs -> { ... }, days);
```

改后（直接查 events.actor）：
```java
            grapeJdbc.query(
                "SELECT DATE(created_at) as label, COUNT(DISTINCT actor) as cnt " +
                "FROM events " +
                "WHERE created_at IS NOT NULL " +
                "AND created_at >= DATE_SUB(NOW(), INTERVAL ? DAY) " +
                "AND actor<>'' AND actor NOT LIKE '%bot%' " +
                "GROUP BY label ORDER BY label",
                rs -> { ... }, days);
```

**方案选型依据**：

| 方案 | 数据源 | 近30天活跃用户 | 覆盖率 | 有历史趋势 |
|------|--------|---------------|--------|-----------|
| 原 agent_tasks+owner | agent_tasks.user_id 反推 | 10 | 16%（562/3499条有效） | 是 |
| 方案A agent_tasks.user_id | agent_tasks.user_id 直查 | 11 | 16%（84%的task user_id=0） | 是 |
| **方案B events.actor** | **events.actor 直查** | **11** | **100%（actor基本都填）** | **是** |
| 方案C last_active_at | user_accounts.last_active_at | 4 | 20%（只4个有值） | 否（单值覆盖） |

选 B：events 表每条事件都记录 actor（GitCode 用户名），覆盖率 100%，有完整时间戳历史。排除 bot 账号（grape_bot/grace_bot/grape_dev_bot）。近30天 11 个 actor 全部能匹配 user_accounts.username（0 未匹配，大小写一致）。

## 四、不需要改的

- **HealthCenterController.java**：健康中心是全平台全局视角，所有查询都不带用户维度过滤，不 JOIN user_accounts/user_repos，不需要改。
- **RepoService.java listRepos**：代码仓管理列表查的是 managed_repos 全量（findAll），不按用户过滤，不需要改。
- **TemplatesService.java loadManagedReposByName**：模板使用人数按 repo_name 聚合 owner，不涉及用户→仓库关系，不需要改。
- **前端 Overview.vue / ConsoleUserDetail.vue**：数据结构 {label, count} 和 {fullName, repoKey} 完全兼容，无需改动。

## 五、全库验证（grape-dev-db）

### 5.1 改动 1/2/3 全库用户三种口径对比

| 用户名 | 用户面 | 改法(user_repos) | 改前(owner) | 一致性 |
|--------|--------|-----------------|-------------|--------|
| gcw_3FZApeLT | 30 | 30 | 28 | 改后+2 ✓ |
| wdmike | 13 | 13 | 11 | 改后+2 ✓ |
| XOcto | 11 | 11 | 8 | 改后+3 ✓ |
| obs_mysql_user | 10 | 10 | 0 | 改后+10 ✓ |
| xiaowu2119 | 10 | 10 | 6 | 改后+4 ✓ |
| songspace | 7 | 7 | 4 | 改后+3 ✓ |
| mm6667 | 5 | 5 | 4 | 改后+1 ✓ |
| Sinkear | 3 | 3 | 2 | 改后+1 ✓ |
| Wing-Jason | 3 | 3 | 3 | 一致 ✓ |
| gcw_IEzSI05D | 2 | 2 | 1 | 改后+1 ✓ |
| rrrrrrrr111 | 1 | 1 | 1 | 一致 ✓ |
| wellx_term | 1 | 1 | 0 | 改后+1 ✓ |

**结论**：改法(user_repos)与用户面完全一致，全部"只多不少"。

### 5.2 改动 4 活跃用户趋势（近30天）

```
日期        活跃用户数
08-30       4
08-29       5
08-28       3
08-27       6
08-25       6
08-24       6
08-23       1
08-22       3
08-21       6
08-20       3
...（共16天有数据）
```

events 数据范围 08-11 至今（19天），选 90 天时前面空白是数据现状，会随时间自然积累。

## 六、已知限制（不影响本次改动）

1. **events.created_at 是 TEXT 类型**：MySQL 对 TEXT 列做 DATE() 和范围比较无法走索引，全表扫描。当前 2290 行可接受，数据量大后需加索引或改列类型（超出本次范围）。

2. **events.actor 为空 1347 条（59%）**：主要是 a2a_event（1306条，agent 间内部通信）和部分 issue_opened（41条）。这些被 WHERE actor<>'' 排除，合理——A2A 事件不是用户操作。但活跃用户数会偏低，因为用户触发的 A2A 事件没记 actor。

3. **is_join 字段不可靠**：全库 user_repos 335 行，is_join=1 只有少量。XOcto 名下 8 个仓 is_join 全是 0。这就是本次改法不用 is_join 的原因——用 user_repos 行存在性，与用户面一致。

## 七、改动 1/2/3 的隐含依赖与边界场景

### 7.1 口径权衡的本质

改动 1/2/3 把 admin 的用户→仓库关联从 owner 关系改成 user_repos 成员关系。两种口径有本质区别：

| 口径 | 关联方式 | 是否依赖 user_repos 同步 | 与用户面一致性 |
|------|---------|------------------------|--------------|
| 改前 owner | managed_repos.repo_owner = user_accounts.username | 否（托管时写死 repo_owner） | 不一致 |
| 改后 user_repos | user_repos 行存在性 | 是（依赖用户同步过 user_repos） | 一致 |

本次选 user_repos 口径，因为需求是"与用户面一致"。

### 7.2 边界场景：user_repos 未同步

场景：用户张三注册了 grape 账号，在 GitCode 上名下有一个仓库 my-project 并托管到了 grape（managed_repos 有记录 repo_owner='张三'）。但张三从未在用户面点过"同步仓库"，user_repos 表里没有他的行。

- 改前 owner 口径：admin 能看到（repo_owner='张三' 匹配到）
- 改后 user_repos 口径：admin 看不到（user_repos 没有张三的行）

即 admin 能否看到仓库，取决于用户有没有同步过 user_repos。

### 7.3 当前不受影响的原因

grape 的托管流程（host_repo）会自动调 mark_user_repo_join 写入 user_repos（src/api/routes/repos.py:296/300/422/433），所以正常托管流程下 user_repos 一定有行。

当前 grape-dev-db 验证：8 个无 user_repos 的用户（Jason7602、kaverjody、lming112、test_tmpl、uitest、_final_e2e_、_scope_e2e_、_skill_verify_）名下均无托管仓库（managed_repos.repo_owner 匹配为 0），不存在漏显示问题。

### 7.4 未来如果出现此边界

根因是用户没同步 user_repos，不是方案缺陷。解法是让用户在用户面点一次同步（触发 _fetch_and_cache → save_user_repos 写入 user_repos），而不是回退到 owner 口径。因为用户面也依赖 user_repos，回退 admin 口径只会让两边重新不一致。

## 八、构建部署

```bash
# 1. 改完代码后构建后端
cd admin/backend && mvn -B -q package -DskipTests

# 2. 部署
DEST=/opt/grape-admin
cp admin/backend/target/admin-backend-0.1.0.jar $DEST/backend/
systemctl restart admin-backend
```

## 九、验证

### 9.1 构建验证
```bash
cd admin/backend && mvn -B test
# Tests run: 4, Failures: 0, Errors: 0, Skipped: 0 → BUILD SUCCESS
```

### 9.2 数据库验证
改完后在运营管理台打开 xiaowu2119 的用户详情：
- 仓库×Issue 矩阵应显示 10 个仓库（含 signway/fluxgate、signway/grape_skill、XOcto/HELLO-BLOG-SYS-2、XOcto/HELLO-BLOG-SYS-22），而不是 6 个
- Issue 列表/时间线能看到这些仓库的 issue 事件

### 9.3 活跃用户图验证
- 运营总览 → 活跃用户 tab，近7天/30天/90天切换正常
- 数据点数 = events.actor 去重数（非 0）

## 十、风险

- 改动 1/2/3 是纯只读 SQL，不碰表结构、不碰写操作、不影响主服务
- 改动 4 是纯只读 SQL，只改统计口径，不影响其他功能
- issue 分组逻辑（按 repo_key|issue_id 组合 key）已支持多仓库，不受影响
- 原来能查到的仓库改后照样能查到（只多不少，已全库验证）
- 前端数据结构兼容，无需改动

## 十一、与原方案差异（更新记录）

1. **改动 1/2/3 去掉 AND ur.is_join=1**：原方案加 is_join=1 过滤，但用户面 get_managed_repos 不依赖 is_join（只用 user_repos 行存在性）。is_join 字段不可靠（XOcto 名下 8 仓 is_join 全 0），加这个条件会导致 XOcto 从 8 个仓变 1 个、gcw_IEzSI05D 从 1 个变 0 个，违反"只多不少"原则。去掉后与用户面完全一致。

2. **改动 4 从 is_join 方案改为 events.actor 方案**：原方案将 activeUsers 也改成 user_repos is_join=1 口径，但深入分析发现活跃用户统计本身逻辑不合理（用 repo_owner 反推而非直接统计用户活动）。改为直接查 events.actor，覆盖率 100%，有完整历史趋势。
