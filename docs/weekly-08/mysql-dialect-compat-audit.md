# grape weekly-08 MySQL 兼容性问题清单与修复方案（审计底稿）

> 评审稿的完整证据版。审计基线：origin/weekly-08 @ 2d47f9f35，MySQL 后端实例。
> 正式需求见同目录 mysql-dialect-compat-requirements.md。按 DOCUMENTATION-GUIDE
> 红线，本文所有 IP/账号/仓库名/凭据均为占位符。

审计方法：AST 提取全库 1907 处 execute() 调用 SQL → 逐条经
mysql_compat.translate_sql 翻译 → MySQL EXPLAIN 解析验证 → 42 处失败逐一定性
（区分真问题/误报/有守卫设计）→ 运行日志与真实 DB 状态交叉核验。
触发场景：某用户（下称 <user>）解绑 GitCode 报 500（MySQL 后端）；SQLite 后端
全部不可见。

---

## 一、系统性根因

mysql_compat 翻译层"白名单正则"机制的三条漏网通道：

R-1 字符串字面量保护（KWP）刻意跳过字符串内部 → 反斜杠语法炸弹原样放行（F1）
R-2 `datetime('now')` 正则只匹配无参数形式 → 带修饰符变体漏翻（N1）
R-3 未列入翻译规则的方言函数/语句原样透传（CAST/PRAGMA/DISTINCT-ORDER BY）

放大器：大量 `except: pass` 把漏网炸弹从"500 报错"降级为"静默错误数据"。

---

## 二、问题清单（按严重度）

### P0 —— 直接 500

F1 解绑 500：ESCAPE '\' 方言炸弹
- 位置：src/api/routes/grape_auth.py 1024/1035/1088/1111（unbind 全链路 4 处）
- MySQL 报 1064（near ''\\' AND ...'），confirm=false 阶段即 500；SQLite 正常
- 实测：病灶 SQL 单独复现 1064；纯参数化 LIKE 在 MySQL 跑通（<user> 名下查出
  2 个在管仓库 <repo_a>/<repo_b>）
- 数据状态：<user> 绑定无损——500 发生在第一段只读查询，写操作未执行

### P1 —— 静默失效（被 except 吞掉）

F2 affected_count 静默归零（grape_auth.py:1088）→ 前端显示"无受影响仓库"，
实际有 2 个。错误数据进入用户决策
F3 半途状态风险：user_repos 清理（1111）在绑定软删之后、审计之前，无事务边界
N1 datetime('now', '-60 seconds') 带参变体不翻译，3 处：
  a) feishu_onboard.py:314 —— stale 恒 False → 用户被 409 卡死无法开始新任务
  b) event_pipeline.py:84 —— _check_session_active 恒 False
  c) dashboard.py:1312 —— events_week 指标静默错
N2 CAST(x AS INTEGER) 不翻译（MySQL 需 AS SIGNED）：auth_token.py:132 过期
  session token 清理静默永不执行，表只增不减

### P2 —— 特定场景中断 / 潜伏升级损坏

N3 im/handler_async.py:1508 SELECT DISTINCT ... ORDER BY 未选中列 →
  ONLY_FULL_GROUP_BY（默认 sql_mode，实测）下 3065 → IM /repo 兜底卡片中断
N4 db_setup.py:1885 PRAGMA index_list('issue') 不翻译 → 唯一索引升级 prep
  永远失败（运行日志实锤）。全新库索引天生 unique（SHOW INDEX 实测）暂无影响；
  遗留库同名非唯一索引将永远无法升级
F4 grape_auth.py:1035 路径 B 查询 except: pass → 409 确认列表漏报

### P3

F6 unbind 入口无 provider 白名单。um_column_for_provider() 对未知 provider
默认返回 gitcode_user → 传 provider=foo 会误清 um 的 gitcode_user 列

### 已排除的嫌疑（有证据，不是问题）

二次补扫（f-string 动态 SQL 220 处、变量/拼接型 SQL 48 处、剩余 SQLite 函数族
julianday/date('now'/ON CONFLICT/INSERT OR REPLACE/random 全库 grep）追加排除：

- PRAGMA table_info/index_list/index_info 的 f-string 调用点（event_store:609、
  user/db 741-753、runtime/service:418、db_setup 各处）：Cursor 层特判已覆盖，
  运行时实测 unquoted 形式工作正常（events 1457 条 completed、completed_at
  全有值、日志零告警为证）
- INSERT OR REPLACE / INSERT OR IGNORE / ON CONFLICT：翻译层均正确转出
  REPLACE INTO / INSERT IGNORE / ON DUPLICATE KEY UPDATE
- IN (...) 列表：全部为 `?` 占位符参数化拼接，无值内联
- dashboard.py:430 strftime：except 内有 TIMESTAMPDIFF 的 MySQL fallback，
  设计内降级，功能可用
- issues_center:567 等全部 datetime('now') 命中点均为无参形式，翻译正常
- issues_center/session_store/observability 等动态拼接 SQL：片段均为 `?` 风格
- issues.py:472 / bot_lease.py:362：审计工具假参数导致的类型报错，真实参数无问题
- observability SHOW STATUS：仅 MySQL 后端分支执行；EXPLAIN 不支持 SHOW，误报
- db_setup metadata_locks 查询：权限探测，失败有兜底
- 全库反斜杠/ESCAPE 静态扫描：仅已知 4 处，无其他

---

## 三、修复方案

### 方案 A：mysql_compat 翻译层补齐（根治）

A1 datetime 全变体翻译：正则改 `datetime\('now'[^)]*\)`
    - 固定字面量参数 → DATE_SUB(NOW(), INTERVAL n SECOND/DAY)
    - 绑定参数变体（仅 feishu_onboard:314 一处）→ 调用点改应用层预计算 cutoff
      字符串（Python 生成 "%Y-%m-%d %H:%M:%S"，双方言一致）
A2 新增规则：CAST(x AS INTEGER) → CAST(x AS SIGNED)
A3 PRAGMA index_list 等价：information_schema.statistics 或调用点后端分支
A4 模块文档追加团队规则四条（见 requirements §四）

### 方案 B：调用点修复（止血）

B1 grape_auth.py 4 处改纯参数化 LIKE，删 ESCAPE 与 _escape_like_pattern
    （模式串为常量前缀，无通配符，已在 MySQL 实测通过）
B2 unbind 入口 provider 白名单 {gitcode, github}
B3 解绑链路一致失败语义：读/校验前置，写失败 → 部分失败可见（不做真回滚）
B4 handler_async.py:1508 排序列入 SELECT
B5 db_setup.py:1885 PRAGMA index_list 加 MySQL 分支

### 方案 C：吞错治理

C1 用户决策数据路径（409 确认列表、affected_count）的 except: pass →
    log.warning + 保守值，禁止静默归零
C2 N1/N2 各点同步清理吞错，让残余故障可见

### 方案 D：CI 防回归

D1 审计脚本固化 tools/ci_check_sql_dialect.py（AST+翻译+EXPLAIN，全量 1907
    条本机 1 分钟内跑完，已验证可行）
D2 解析失败（非 1146/1054 动态 schema 类）即 fail the build

### 修复范围收窄说明

按"职责清晰，不为实现功能而加功能"：B/C 只改确认有问题的点；A 只补确认的
三个盲区；D 是检测不是拦截，零生产风险。全部方案无破坏性路径——最坏情况是
某条翻译规则引入新错误，被 D1 在合入前拦住。

---

## 四、优先级与依赖

第一批（止血）：F1(B1)+F6(B2)+F2(C1 部分) —— 单文件，当天可发
第二批（翻译层根治）：N1(A1)+N2(A2)+N4(A3/B5)
第三批（链路加固）：F3(B3)+F4(C1)+N3(B4)+C2
第四批（防回归）：CI(D1/D2)+规范文档 —— 建议提前到第二批之前

评审要点：
1. A1 带绑定参数的 datetime('now', ?) 只有一处，改应用层预计算（推荐）vs
   兼容层动态翻译，二选一
2. B3 一致失败语义做到什么程度——推荐顺序执行+部分失败可见，不引入过度设计
3. D1 落库位置（独立 CI job vs 现有流水线挂一步）

验收场景 S1-S8 见 mysql-dialect-compat-requirements.md §五。

---

## 附：实施决策记录（2026-10-22 评审后）

评审拍板"统一使用通用标准 SQL 写法"，§三 方案 A1 的"兼容层翻译 datetime
带参变体"被否决——不为 SQLite 私有方言打补丁。实际实施：

- A1 改为调用点标准 SQL：三处带参调用（feishu_onboard:314 / event_pipeline:84
  / dashboard.py:1312）应用层预计算 cutoff + `?` 参数；兼容层只保留无参
  `datetime('now')` 翻译（历史存量形态）；
- A2（CAST AS INTEGER→SIGNED）保留：CAST AS INTEGER 是 SQL 标准，MySQL 只认
  SIGNED——吸收 MySQL 偏差属兼容层本职，与设计原则一致；
- A3 改为 regex 容差：PRAGMA index_list/index_info 接受单引号参数（一行
  字符类补 `'`），Cursor 特判剥引号——不新增翻译分支；
- D1 的 CI 脚本落地为 tests/mysql_dialect_check.py（standalone MySQL 验证）+
  pytest 内两条 AST 防回归守卫，替代原"全量 EXPLAIN 解析"重方案。

场景用例代码实现见 mysql-dialect-compat-requirements.md §四点五。
