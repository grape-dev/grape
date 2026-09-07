# 运营管理台用户仓库列表与用户面不一致修复方案

## 问题根因

用户面（grape-main Python）和运营管理台（admin Java）查"某用户的托管仓库"用了不同的关联逻辑：

- 用户面 `src/user/db.py:1066` get_managed_repos(user_id) → JOIN user_repos（is_join=1）
  = "这个用户加入了哪些托管仓库"（自己名下的 + 加入别人的）
- 运营管理台 ConsoleUserController → `m.repo_owner = u.username`
  = "这个用户名下有哪些仓库"（只看 repo_owner 等于用户名）

导致 xiaowu2119 在用户面能看到 10 个仓库（含加入的 signway/fluxgate、XOcto/HELLO-BLOG-SYS-2 等），
但运营管理台只看到 6 个（只看 repo_owner='xiaowu2119' 的）。

## 两张表的职责（不要合并）

- managed_repos：仓库级全局配置表（webhook/token/scene），一行=一个托管的仓库，跟用户无关
- user_repos：用户↔仓库成员关系表（is_join/is_admin），一行=某用户对某仓库的关系

一个仓库只有一份配置，但可以有多个成员。这是标准多对多拆法，不是冗余。

## 改动文件

2 个 Java 文件，共 4 处 SQL（3 处必须改，1 处可选待定）。

---

### 改动 1（必须）：ConsoleUserController.java detail 的 repoMatrix

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
                "INNER JOIN user_repos ur ON ur.repo_key = m.repo_key AND ur.is_join=1 " +
                "INNER JOIN user_accounts u ON u.id = ur.user_id WHERE u.id=? " +
                "ORDER BY m.full_name",
                (rs2, rowNum) -> {
                    Map<String, Object> r = new LinkedHashMap<>();
                    r.put("fullName", rs2.getString("full_name"));
                    r.put("repoKey", rs2.getString("repo_key"));
                    return r;
                }, id);
```

---

### 改动 2（必须）：ConsoleUserController.java repoIssues 的 userRepoKeys

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
                "INNER JOIN user_repos ur ON ur.repo_key = m.repo_key AND ur.is_join=1 " +
                "INNER JOIN user_accounts u ON u.id = ur.user_id " +
                "WHERE u.id=? AND t.issue_id IS NOT NULL AND t.issue_id<>''", String.class, id);
```

---

### 改动 3（必须）：ConsoleUserController.java issueTimeline 的 userRepoKeys

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
                "INNER JOIN user_repos ur ON ur.repo_key = m.repo_key AND ur.is_join=1 " +
                "INNER JOIN user_accounts u ON u.id = ur.user_id WHERE u.id=?", String.class, id);
```

---

### 改动 4（可选，待定）：StatsController.java activeUsers

文件：admin/backend/src/main/java/com/grape/admin/controller/StatsController.java
位置：约 L169-176

⚠️ 这处语义不同：原来是统计"仓库 owner 的活跃度"，改成 user_repos 后一个仓库的 task 会被
所有加入该仓库的成员各计一次（同一天同一用户不重复，但不同成员会叠加），活跃用户数会虚高。

两种选择：
A. 保持不动（统计口径=仓库所有者活跃度）
B. 改成 user_repos 口径（统计口径=所有加入成员活跃度，数值会涨）

如果选 B，改前：
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

改后：
```java
            grapeJdbc.query(
                "SELECT DATE(t.started_at) as label, COUNT(DISTINCT u.id) as cnt " +
                "FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_repos ur ON ur.repo_key = m.repo_key AND ur.is_join=1 " +
                "INNER JOIN user_accounts u ON u.id = ur.user_id " +
                "WHERE t.started_at IS NOT NULL " +
                "AND t.started_at >= DATE_SUB(NOW(), INTERVAL ? DAY) " +
                "GROUP BY label ORDER BY label",
                rs -> { ... }, days);
```

---

## 不需要改的

- HealthCenterController.java：健康中心是全平台全局视角，所有查询都不带用户维度过滤，
  不 JOIN user_accounts/user_repos，不需要改。
- RepoService.java listRepos：代码仓管理列表查的是 managed_repos 全量（findAll），
  不按用户过滤，不需要改。
- TemplatesService.java loadManagedReposByName：模板使用人数按 repo_name 聚合 owner，
  不涉及用户→仓库关系，不需要改。

## 构建部署

```bash
# 1. 改完代码后构建后端
cd admin/backend && mvn -B -q package -DskipTests

# 2. 部署
DEST=/opt/grape-admin
cp admin/backend/target/admin-backend-0.1.0.jar $DEST/backend/
systemctl restart admin-backend
```

## 验证

改完后在运营管理台打开 xiaowu2119 的用户详情：
- 仓库×Issue 矩阵应显示 10 个仓库（含 signway/fluxgate、signway/grape_skill、
  XOcto/HELLO-BLOG-SYS-2、XOcto/HELLO-BLOG-SYS-22），而不是 6 个
- Issue 列表/时间线能看到这些仓库的 issue 事件（signway/grape_skill 的 #5/#8/#9/#10/#11/#12、
  XOcto/HELLO-BLOG-SYS-22 的 #5/#6/#7 等）

## 风险

- 改动 1/2/3 是纯只读 SQL，不碰表结构、不碰写操作、不影响主服务
- issue 分组逻辑（按 repo_key|issue_id 组合 key）已支持多仓库，不受影响
- 原来能查到的仓库改后照样能查到（user_repos 里 is_join=1），只多不少
