# Token 获取统一接口（策略模式 + 职责链）

> 本文档是 token 配置体系的核心基础文档。所有其他文档都依赖本文档的 TokenResolver / Handler / VCSProvider。
> 必须最先实现。

## 需求项

### R18.1 Token 策略基类

- R18.1.1：定义 TokenStrategy 基类，包含 get_token / _fetch / _is_expired / mark_expired / clear_expired 方法
- R18.1.2：get_token 内部检查过期标记，过期则短路返回空串
- R18.1.3：每个策略有唯一 error_code 属性
- _fetch 契约（写死）：纯读存储值，不检查过期、不刷新、不调 VCS API。只查 DB / meta 表。供 read_stored_token() 和 get_token() 共用

### R18.2 四级策略实现

- R18.2.1：DevopsAgentSessionToken — 第 1 级 devops_agent_session.{session_token_field}
- R18.2.2：RepoBotToken — 第 2 级 managed_repos.bot_token，含 host_user_id 守卫 + 过期短路
- R18.2.3：GlobalBotToken — 第 3 级全局 bot token，含 host_user_id 守卫 + 过期短路
- R18.2.4：UserToken — 第 4 级用户 token，含 OAuth 自动刷新 + status=4 不短路（best-effort）

### R18.3 统一入口 TokenResolver

- R18.3.1：TokenResolver 按优先级链执行策略，第一个非空 token 胜出
- R18.3.2：detect_and_mark_expired 方法供 fail_task 401 统一调用
- R18.3.3：支持跳过特定策略（如 get_repo_bot_token 跳过第 1 级）
- read_stored_token() 方法：只调 _fetch()（纯读），不检查过期、不触发 OAuth refresh。供 A2A 鉴权 / session 写入用
- resolve() 方法：返回 TokenResult（token + level + provider + repo_key + expired），供需要级别差异的调用方用

### R18.4 向后兼容

- R18.4.1：现有 13 个函数保留为薄包装，委托 TokenResolver
- R18.4.2：调用方无需改动（函数签名和返回值不变）
- R18.4.3：现有测试全部通过
- R18.4.4：get_user_token_with_refresh / get_user_github_token_with_refresh 不被替代——UserToken 策略内部调用它们
- R18.4.5：get_repo_bot_token_no_global 的 fallback 链保持一致
- R18.4.6：所有短路检查新增在现有逻辑之后，未标记过期的 token 走原有逻辑完全不变

### R18.5 VCSProvider 接口

- R18.5.1-R18.5.6：VCSProvider 抽象接口（11 方法）+ GitCodeProvider + GitHubProvider + 工厂 + 差异矩阵
- R18.5.7：R15 流程通过 VCSProvider 接口调用，不直接调 _req / _github_req
- R18.5.8：R17 健康检查通过 provider.verify_token() 调用
- R18.5.9：新增 fetch_user_token(username, user_id) 方法
- R18.5.10：detect_and_mark_expired 中 verify_token 区分 401（标记过期）和超时/5xx（不标记，R21.3 修正）

### R18.6 TokenActionHandler 职责链

- R18.6.1-R18.6.7：TokenResult + TokenActionHandler 接口 + 四个 Handler 实现 + 工厂方法
- R18.6.8-R18.6.10：pipeline 自循环检测 / fail_task 评论 / _post_result_to_issue 改为通过 Handler 调用
- R18.6.11-R18.6.12：get_token_with_level() 新增；薄包装签名不变
- R18.6.13：Repo.bot_token 改为调 service 层方法（避免循环依赖）
- R18.6.14-R18.6.15：Chat / 飞书 VOD 改为调 resolve()
- R18.6.16：check_running_task 补充降级查询

## 设计方案

1311|## 八、Token 获取统一接口设计（策略模式）
1312|
1313|### 8.1 当前问题
1314|
1315|当前 token 获取散落在 13 个函数中，分布在 2 个文件（db.py / deps.py），逻辑高度重复但又不完全一致：
1316|
1317|| 函数 | 文件 | 职责 | 问题 |
1318||------|------|------|------|
1319|| get_repo_token_with_refresh | db.py | GitCode 统一入口（4级 fallback） | 与 get_github_token 逻辑重复 |
1320|| get_github_token | db.py | GitHub 统一入口（4级 fallback） | 与 get_repo_token_with_refresh 逻辑重复 |
1321|| get_repo_bot_token | db.py | 跳过 devops_agent_session（#343 语义） | fallback 链又是另一套 |
1322|| get_repo_bot_token_no_global | db.py | 不含全局兜底（A2A 鉴权用） | 又一套 fallback |
1323|| get_bot_token_by_repo | db.py | 按 owner/repo 查 | host_user_id 守卫嵌入 |
1324|| get_gitcode_token | db.py | 兼容包装 | 委托 get_repo_token_with_refresh |
1325|| get_task_token | db.py | task dict 取 token | 按 provider 分流 |
1326|| get_user_token_with_refresh | deps.py | 用户 GitCode token（带刷新） | 与 GitHub 版逻辑重复 |
1327|| get_user_github_token_with_refresh | deps.py | 用户 GitHub token（带刷新） | 与 GitCode 版逻辑重复 |
1328|| get_user_gitcode_token | db.py | 用户 token（不带刷新） | 又一个用户 token 入口 |
1329|| _get_global_bot_token | db.py | 全局 GitCode bot token | |
1330|| get_global_gitcode_bot_token | db.py | 同上（统一入口） | 命名重复 |
1331|| get_global_github_bot_token | db.py | 全局 GitHub bot token | |
1332|
1333|**核心问题**：
1334|1. 每个函数自己实现一套 fallback 链，新增 token 类型时需要改所有函数
1335|2. GitCode 和 GitHub 逻辑重复（两个平台只是 API 不同，fallback 策略相同）
1336|3. 过期检测/短路逻辑（R16.4）需要嵌入每个函数，散落不可控
1337|4. 调用方需要知道调哪个函数（13 个函数选哪个？），心智负担大
1338|
1339|### 8.2 设计目标
1340|
1341|- **统一入口**：调用方只需要一个函数 `get_token(repo_key, context)`，不需要知道 13 个函数选哪个
1342|- **策略链**：token 获取按优先级链执行，每级是一个策略，可插拔
1343|- **接口隔离**：GitCode 和 GitHub 各自实现 VCSProvider 接口，策略类通过接口调用 provider 能力，不耦合具体平台
1344|- **过期短路**：过期检测/短路逻辑（R16.4）在策略基类中统一实现，不散落
1345|- **向后兼容**：现有函数保留为薄包装，委托统一接口
1346|
1347|### 8.3 VCSProvider 接口设计
1348|
1349|#### 8.3.0 接口定义
1350|
1351|```python
1352|from abc import ABC, abstractmethod
1353|
1354|class VCSProvider(ABC):
1355|    """VCS 平台接口。GitCode / GitHub 各自实现，策略类通过此接口调用平台能力。"""
1356|
1357|    @abstractmethod
1358|    def verify_token(self, token: str) -> dict:
1359|        """验证 token 有效性，返回用户信息 dict（含 login 字段）。"""
1360|
1361|    @abstractmethod
1362|    def get_user_login(self, token: str) -> str:
1363|        """获取 token 对应的用户名。"""
1364|
1365|    @abstractmethod
1366|    def add_collaborator(self, api_token: str, owner: str, repo: str,
1367|                         username: str, permission: str) -> bool:
1368|        """邀 bot 为协作者。幂等：已是协作者返回 True。"""
1369|
1370|    @abstractmethod
1371|    def remove_collaborator(self, api_token: str, owner: str, repo: str,
1372|                            username: str) -> bool:
1373|        """移除协作者。best-effort，失败不阻断。"""
1374|
1375|    @abstractmethod
1376|    def setup_webhook(self, api_token: str, owner: str, repo: str,
1377|                      webhook_url: str, webhook_secret: str) -> int:
1378|        """创建 webhook，返回 webhook_id。"""
1379|
1380|    @abstractmethod
1381|    def remove_webhook(self, api_token: str, owner: str, repo: str,
1382|                       hook_id: str) -> bool:
1383|        """删除 webhook。"""
1384|
1385|    @abstractmethod
1386|    def token_storage_field(self) -> str:
1387|        """仓库级 bot token 存储列名。"""
1388|
1389|    @abstractmethod
1390|    def webhook_token_field(self) -> str:
1391|        """webhook token 存储列名。"""
1392|
1393|    @abstractmethod
1394|    def session_token_field(self) -> str:
1395|        """devops_agent_session token 列名。"""
1396|
1397|    @abstractmethod
1398|    def global_bot_meta_key(self) -> str:
1399|        """全局 bot token meta key。"""
1400|
1401|    @abstractmethod
1402|    def pat_create_url(self) -> str:
1403|        """PAT 创建页链接。"""
1404|
1405|    @abstractmethod
1406|    def fetch_user_token(self, username: str, user_id: int) -> str:
1407|        """获取用户 token（带刷新）。GitCode 调 get_user_token_with_refresh，GitHub 调 get_user_github_token_with_refresh。"""
1408|```
1409|
1410|#### 8.3.0.1 GitCodeProvider 实现
1411|
1412|```python
1413|class GitCodeProvider(VCSProvider):
1414|    """GitCode 平台实现。"""
1415|
1416|    API_BASE = "https://gitcode.com/api/v5"
1417|
1418|    def verify_token(self, token: str) -> dict:
1419|        from src.user.repo_manager import _req
1420|        return _req("GET", f"{self.API_BASE}/user", token)
1421|
1422|    def get_user_login(self, token: str) -> str:
1423|        data = self.verify_token(token)
1424|        return data.get("login", "") or ""
1425|
1426|    def add_collaborator(self, api_token, owner, repo, username, permission="maintainer"):
1427|        from src.user.repo_manager import add_collaborator
1428|        ok, _err = add_collaborator(api_token, owner, repo, username, permission)
1429|        return ok
1430|
1431|    def remove_collaborator(self, api_token, owner, repo, username):
1432|        from src.user.repo_manager import remove_collaborator
1433|        try:
1434|            remove_collaborator(api_token, owner, repo, username)
1435|            return True
1436|        except Exception:
1437|            return False
1438|
1439|    def setup_webhook(self, api_token, owner, repo, webhook_url, webhook_secret=""):
1440|        from src.user.repo_manager import setup_webhook
1441|        return setup_webhook(api_token, owner, repo, webhook_url, webhook_password=webhook_secret)
1442|
1443|    def remove_webhook(self, api_token, owner, repo, hook_id):
1444|        from src.user.repo_manager import remove_webhook
1445|        try:
1446|            remove_webhook(api_token, owner, repo, hook_id)
1447|            return True
1448|        except Exception:
1449|            return False
1450|
1451|    def token_storage_field(self) -> str:
1452|        return "bot_gitcode_token"
1453|
1454|    def webhook_token_field(self) -> str:
1455|        return "gitcode_webhook_token"
1456|
1457|    def session_token_field(self) -> str:
1458|        return "repo_gitcode_token"
1459|
1460|    def global_bot_meta_key(self) -> str:
1461|        return "global_bot_gitcode_token"
1462|
1463|    def pat_create_url(self) -> str:
1464|        return "https://gitcode.com/profile/personal_access_tokens"
1465|
1466|    def fetch_user_token(self, username: str, user_id: int) -> str:
1467|        from src.api.deps import get_user_token_with_refresh
1468|        return get_user_token_with_refresh(username, user_id) or ""
1469|```
1470|
1471|#### 8.3.0.2 GitHubProvider 实现
1472|
1473|```python
1474|class GitHubProvider(VCSProvider):
1475|    """GitHub 平台实现。"""
1476|
1477|    API_BASE = "https://api.github.com"
1478|
1479|    def verify_token(self, token: str) -> dict:
1480|        from src.user.repo_manager import _github_req
1481|        return _github_req("GET", f"{self.API_BASE}/user", token)
1482|
1483|    def get_user_login(self, token: str) -> str:
1484|        from src.user.repo_manager import get_user_github_login
1485|        return get_user_github_login(token)
1486|
1487|    def add_collaborator(self, api_token, owner, repo, username, permission="push"):
1488|        from src.user.repo_manager import add_github_collaborator
1489|        return add_github_collaborator(api_token, owner, repo, username, permission)
1490|
1491|    def remove_collaborator(self, api_token, owner, repo, username):
1492|        from src.user.repo_manager import remove_github_collaborator
1493|        try:
1494|            remove_github_collaborator(api_token, owner, repo, username)
1495|            return True
1496|        except Exception:
1497|            return False
1498|
1499|    def setup_webhook(self, api_token, owner, repo, webhook_url, webhook_secret=""):
1500|        from src.user.repo_manager import setup_github_webhook
1501|        return setup_github_webhook(api_token, owner, repo, webhook_url, webhook_secret=webhook_secret)
1502|
1503|    def remove_webhook(self, api_token, owner, repo, hook_id):
1504|        from src.user.repo_manager import remove_github_webhook
1505|        try:
1506|            remove_github_webhook(api_token, owner, repo, hook_id)
1507|            return True
1508|        except Exception:
1509|            return False
1510|
1511|    def token_storage_field(self) -> str:
1512|        return "bot_github_token"
1513|
1514|    def webhook_token_field(self) -> str:
1515|        return "github_webhook_token"
1516|
1517|    def session_token_field(self) -> str:
1518|        return "repo_github_token"
1519|
1520|    def global_bot_meta_key(self) -> str:
1521|        return "global_bot_github_token"
1522|
1523|    def pat_create_url(self) -> str:
1524|        return "https://github.com/settings/tokens"
1525|
1526|    def fetch_user_token(self, username: str, user_id: int) -> str:
1527|        from src.api.deps import get_user_github_token_with_refresh
1528|        return get_user_github_token_with_refresh(username, user_id) or ""
1529|```
1530|
1531|#### 8.3.0.3 Provider 工厂
1532|
1533|```python
1534|def create_provider(provider: str) -> VCSProvider:
1535|    """根据 provider 标识创建 VCSProvider 实例。"""
1536|    if provider == "github":
1537|        return GitHubProvider()
1538|    return GitCodeProvider()  # 默认 gitcode
1539|```
1540|
1541|#### 8.3.0.4 GitCode vs GitHub 差异矩阵
1542|
1543|| 维度 | GitCode | GitHub |
1544||------|---------|--------|
1545|| 验证 token API | GET https://gitcode.com/api/v5/user | GET https://api.github.com/user |
1546|| Accept header | application/json | application/vnd.github+json |
1547|| User-Agent | 不需要 | Grape-DevOps |
1548|| 仓库级 token 存储列 | bot_gitcode_token | bot_github_token |
1549|| webhook token 存储列 | gitcode_webhook_token | github_webhook_token |
1550|| devops_agent_session 列 | repo_gitcode_token | repo_github_token |
1551|| 全局 bot token meta key | global_bot_gitcode_token | global_bot_github_token |
1552|| 协作者权限枚举 | pull/push/admin/customized | pull/push/admin |
1553|| 协作者默认权限 | maintainer → admin | push |
1554|| 邀请幂等判断 | 中文关键词（"已经是项目中的成员"等） | 403 状态码 |
1555|| webhook body | password 字段 + push_events 等布尔字段 | config.secret + events 数组 |
1556|| webhook URL 后缀 | /webhook/gitcode | /webhook/github |
1557|| PAT 创建页 | https://gitcode.com/profile/personal_access_tokens | https://github.com/settings/tokens |
1558|
1559|### 8.4 策略模式设计
1560|
1561|#### 8.4.1 Token 策略基类
1562|
1563|```python
1564|class TokenStrategy:
1565|    """token 获取策略基类。每个子类代表 fallback 链中的一级。
1566|    
1567|    持有 VCSProvider 实例，通过接口获取存储字段名和 API 调用，不硬编码 provider 分支。
1568|
1569|    方法分层契约（关键——决定了上层 get_token / read_stored_token 的语义分离）：
1570|    - _fetch(): 纯读存储值。不检查过期、不刷新、不调 VCS API。只查 DB / meta 表。
1571|      供 read_stored_token() 和 get_token() 共用，是两者的公共底层。
1572|    - _is_expired(): 查过期标记（managed_repos.bot_token_status / meta.global_bot_token_status / external_identities.status）。
1573|      只读标记，不调 VCS API 验证。
1574|    - get_token(): = _fetch() + _is_expired() 检查。过期则短路返回空串。
1575|      这是"执行 token"语义——需要当前可用的 token。
1576|    
1577|    分层原则：过期检查和刷新逻辑在 get_token() / 上层方法中组合，_fetch() 保持纯读。
1578|    这样 read_stored_token() 只调 _fetch() 就能保证"读原始值不做任何运行时处理"，
1579|    不需要在策略类内部加任何 flag。
1580|    """
1581|
1582|    error_code = ""  # 子类覆盖
1583|
1584|    def __init__(self, provider: VCSProvider):
1585|        self.provider = provider  # VCSProvider 实例（GitCodeProvider / GitHubProvider）
1586|
1587|    def get_token(self, repo_key: str) -> str:
1588|        """获取可用执行 token。_fetch 读值 → _is_expired 检查过期 → 过期则短路返回空串。"""
1589|        token = self._fetch(repo_key)
1590|        if token and self._is_expired(repo_key):
1591|            return ""  # 短路：已标记过期，不返回
1592|        return token
1593|
1594|    def _fetch(self, repo_key: str) -> str:
1595|        """纯读存储值。不检查过期、不刷新、不调 VCS API。只查 DB / meta 表。
1596|        
1597|        契约：
1598|        - 返回当前存储的 token 原始值（可能已过期但仍存储在 DB 中）
1599|        - 不触发 OAuth refresh（refresh 是 UserToken._fetch 内部 get_user_token_with_refresh 的行为，
1600|          但 read_stored_token 场景不应走 refresh——见 UserToken._fetch 特殊说明）
1601|        - 不检查过期标记（_is_expired 是独立方法）
1602|        - 走缓存层（如 _cached_get_meta 的 60s 缓存——缓存值就是"存储值"的快照）
1603|        """
1604|        raise NotImplementedError
1605|
1606|    def _is_expired(self, repo_key: str) -> str:
1607|        """查过期标记。只读标记字段，不调 VCS API 验证。"""
1608|        return False
1609|
1610|    def mark_expired(self, repo_key: str):
1611|        """子类实现：标记 token 过期。"""
1612|        pass
1613|
1614|    def clear_expired(self, repo_key: str):
1615|        """子类实现：清除过期标记。"""
1616|        pass
1617|```
1618|
1619|#### 8.4.2 具体策略
1620|
1621|```python
1622|class DevopsAgentSessionToken(TokenStrategy):
1623|    """第 1 级：devops_agent_session.{session_token_field}"""
1624|
1625|    error_code = "SESSION_TOKEN_EXPIRED"
1626|
1627|    def _fetch(self, repo_key):
1628|        field = self.provider.session_token_field()  # repo_gitcode_token / repo_github_token
1629|        # 查 devops_agent_session 表（列名通过 provider 接口获取）
1630|        ...
1631|
1632|    def _is_expired(self, repo_key):
1633|        # 查 devops_agent_session.token_status == 'expired'（新增字段）
1634|        # 如未实现独立过期标记，返回 False（不短路，由调用方 401 兜底）
1635|        return False
1636|
1637|    def mark_expired(self, repo_key):
1638|        # 写 devops_agent_session.token_status = 'expired'
1639|        # 如未实现独立过期标记，仅记日志不写 DB
1640|        pass
1641|
1642|class RepoBotToken(TokenStrategy):
1643|    """第 2 级：managed_repos.{token_storage_field}"""
1644|
1645|    error_code = "REPO_TOKEN_EXPIRED"
1646|
1647|    def _fetch(self, repo_key):
1648|        if get_host_user_id(repo_key) > 0:
1649|            return ""  # 路径 B 跳过
1650|        field = self.provider.token_storage_field()  # bot_gitcode_token / bot_github_token
1651|        # 查 managed_repos（列名通过 provider 接口获取）
1652|        ...
1653|
1654|    def _is_expired(self, repo_key):
1655|        # 查 managed_repos.bot_token_status
1656|        ...
1657|
1658|    def mark_expired(self, repo_key):
1659|        # 写 managed_repos.bot_token_status = 'expired'
1660|        ...
1661|
1662|class GlobalBotToken(TokenStrategy):
1663|    """第 3 级：全局 bot token（meta 表 {global_bot_meta_key}）"""
1664|
1665|    error_code = "GLOBAL_TOKEN_EXPIRED"
1666|
1667|    def _fetch(self, repo_key):
1668|        if get_host_user_id(repo_key) > 0:
1669|            return ""  # 路径 B 跳过
1670|        meta_key = self.provider.global_bot_meta_key()  # global_bot_gitcode_token / global_bot_github_token
1671|        # 查 meta 表（key 通过 provider 接口获取）
1672|        ...
1673|
1674|    def _is_expired(self, repo_key):
1675|        # 查 meta.global_bot_token_status
1676|        ...
1677|
1678|    def mark_expired(self, repo_key):
1679|        # 写 meta.global_bot_token_status = 'expired'
1680|        ...
1681|
1682|class UserToken(TokenStrategy):
1683|    """第 4 级：用户 token（external_identities，带刷新）"""
1684|
1685|    error_code = "TOKEN_EXPIRED"
1686|
1687|    def _fetch(self, repo_key):
1688|        host_user_id = get_host_user_id(repo_key)
1689|        if host_user_id == 0:
1690|            return ""  # 路径 A 不走用户 token
1691|        # 通过 provider 接口获取用户 token（不硬编码 provider 分支）
1692|        # GitCodeProvider.fetch_user_token → get_user_token_with_refresh
1693|        # GitHubProvider.fetch_user_token → get_user_github_token_with_refresh
1694|        from src.auth.grape_account_db import get_grape_account_by_id
1695|        account = get_grape_account_by_id(host_user_id)
1696|        if account and account.get("username"):
1697|            return self.provider.fetch_user_token(account["username"], host_user_id)
1698|```
1699|
1700|#### 8.4.3 统一入口
1701|
1702|```python
1703|class TokenResolver:
1704|    """token 获取统一入口。按优先级链执行策略，第一个返回非空 token 的策略胜出。
1705|
1706|    上层语义方法（3 个入口，对应 3 种 token 语义）：
1707|    - get_token(): 获取可用执行 token（含过期短路）。供 pipeline / agent / clone / push 用。
1708|    - read_stored_token(): 读取存储原始值（不过期检查、不刷新）。供 A2A 鉴权 / session 写入用。
1709|    - resolve(): 获取执行 token + 级别信息（TokenResult）。供需要级别差异操作的调用方用。
1710|
1711|    底层共享策略链 self.strategies，三个方法走不同的检查路径：
1712|    - get_token → strategy.get_token() = _fetch() + _is_expired()
1713|    - read_stored_token → strategy._fetch()（纯读，不检查过期）
1714|    - resolve → strategy.get_token() + 级别标识
1715|    """
1716|
1717|    def __init__(self, provider: VCSProvider):
1718|        self.provider = provider
1719|        self.strategies = [
1720|            DevopsAgentSessionToken(provider),
1721|            RepoBotToken(provider),
1722|            GlobalBotToken(provider),
1723|            UserToken(provider),
1724|        ]
1725|
1726|    def get_token(self, repo_key: str) -> str:
1727|        """获取可用执行 token。含过期短路。返回空串=无可用 token。"""
1728|        for strategy in self.strategies:
1729|            token = strategy.get_token(repo_key)
1730|            if token:
1731|                return token
1732|        return ""
1733|
1734|    def read_stored_token(self, repo_key: str) -> str:
1735|        """读取存储的原始 token 值。不过期检查、不刷新、不调 VCS API。
1736|
1737|        契约（写死，不可变）：
1738|        1. 不检查过期标记——即使 token 被标记 expired 也返回原始值
1739|        2. 不触发 OAuth refresh——UserToken 策略的 _fetch 内部如果调 get_user_token_with_refresh
1740|           （会触发 refresh），read_stored_token 场景必须跳过 refresh 逻辑
1741|        3. 走缓存层——_cached_get_meta 的 60s 缓存仍生效（缓存值=存储值快照）
1742|        4. 返回空串=该 repo 无任何 token 存储（不是"过期"，是"未配置"）
1743|
1744|        UserToken._fetch 特殊说明：
1745|        UserToken._fetch 内部调 provider.fetch_user_token → get_user_token_with_refresh，
1746|        后者会检查 token_expires_at 并触发 OAuth refresh。这与 read_stored_token "不刷新"契约冲突。
1747|        解决方式：UserToken 新增 _fetch_stored() 方法（只读 external_identities.access_token，
1748|        不调 get_user_token_with_refresh），read_stored_token 调 _fetch_stored 而非 _fetch。
1749|        其他策略 _fetch 本身就是纯读，无需 _fetch_stored。
1750|
1751|        供 A2A 鉴权（get_repo_bot_token_no_global）/ session 写入（agents start/restart）用。
1752|        """
1753|        for strategy in self.strategies:
1754|            # UserToken 策略用 _fetch_stored 跳过 refresh
1755|            if hasattr(strategy, '_fetch_stored'):
1756|                token = strategy._fetch_stored(repo_key)
1757|            else:
1758|                token = strategy._fetch(repo_key)
1759|            if token:
1760|                return token
1761|        return ""
1762|
1763|    def detect_and_mark_expired(self, repo_key: str) -> str:
1764|        """fail_task 401 时调用：按优先级找到当前生效的策略，验证 token 有效性后标记过期，返回 error_code。
1765|
1766|        注意：不能直接标记——task 401 可能是网络抖动而非真过期，必须先调 VCS API 验证（R16.1.3/R16.1.4）。
1767|        """
1768|        for strategy in self.strategies:
1769|            token = strategy._fetch(repo_key)  # 不检查过期标记，查原始值
1770|            if token:
1771|                # 先验证 token 有效性（通过 provider 接口调 VCS API GET /user）
1772|                try:
1773|                    self.provider.verify_token(token)
1774|                    # token 验证有效 → 401 是其他原因（权限不足等），不标记过期
1775|                    return ""
1776|                except Exception:
1777|                    # token 验证无效（401）→ 标记过期
1778|                    # 幂等：已标记过则不重复标记（R16.1.6）
1779|                    if not strategy._is_expired(repo_key):
1780|                        strategy.mark_expired(repo_key)
1781|                    return strategy.error_code
1782|        # 所有策略 _fetch 都返回空（token 已被短路或未配置）→ 无可用 token
1783|        return "NO_TOKEN_AVAILABLE"  # R16.6.1
1784|```
1785|
1786|#### 8.4.4 调用方使用
1787|
1788|```python
1789|# 获取 token（替换 13 个函数）
1790|provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
1791|token = TokenResolver(provider).get_token(repo_key)
1792|
1793|# fail_task 401 检测（统一过期检测入口）
1794|error_code = TokenResolver(provider).detect_and_mark_expired(repo_key)
1795|```
1796|
1797|#### 8.4.5 向后兼容
1798|
1799|现有函数保留为薄包装：
1800|
1801|```python
1802|def get_repo_token_with_refresh(repo_key):
1803|    provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
1804|    return TokenResolver(provider).get_token(repo_key)
1805|
1806|def get_github_token(repo_key=""):
1807|    return TokenResolver(create_provider("github")).get_token(repo_key)
1808|
1809|def get_repo_bot_token(repo_key):
1810|    # #343 语义：跳过 devops_agent_session（第 1 级）
1811|
1811|    provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
1812|    resolver = TokenResolver(provider)
1813|    resolver.strategies = resolver.strategies[1:]  # 跳过第 1 级
1814|    return resolver.get_token(repo_key)
1815|
1816|def get_repo_bot_token_no_global(repo_key):
1817|    # A2A 鉴权用：不含全局兜底（跳过第 3 级全局）
1818|    # 现有代码 fallback 链：仓库级 → devops_agent_session → 用户 token（路径 B 降级）
1819|    provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
1820|    resolver = TokenResolver(provider)
1821|    resolver.strategies = [resolver.strategies[1], resolver.strategies[0], resolver.strategies[3]]  # 仓库级 + devops_agent_session + 用户 token，跳过第 3 级全局
1822|    return resolver.get_token(repo_key)
1823|
1824|def get_task_token(task):
1825|    # task dict 取 token：按 provider 分流
1826|    # 先取 task.repo_gitcode_token（task 行存入的 bot token），无则调 get_repo_token_with_refresh
1827|    _tok = task.get("repo_gitcode_token") or ""
1828|    if _tok:
1829|        return _tok
1830|    repo_key = task.get("repo_key") or ""
1831|    provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
1832|    return TokenResolver(provider).get_token(repo_key)
1833|```
1834|
1835|**不被 TokenResolver 替代的函数**：
1836|
1837|- `get_user_token_with_refresh(username, user_id)` / `get_user_github_token_with_refresh(username, user_id)`（deps.py）— 这两个函数接受 username + user_id 参数，不走 repo_key。它们是 UserToken 策略 `_fetch` 内部通过 `provider.fetch_user_token(username, user_id)` 调用的底层函数，不是 repo_key → token 的映射。account 页绑定时直接调用这两个函数，保持原样不替代。
1838|- `get_user_gitcode_token(user_id)`（db.py）— 用户 token 不带刷新版本，保持原样。
1839|
1840|**get_github_token D32-补丁兼容**：
1841|
1842|现有 get_github_token（db.py line 2278-2290）有一个 GitCode 版没有的 fallback：仓库 owner 的 external_identities GitHub token。这是 D32-补丁引入的逻辑——当仓库级 bot_github_token 为空时，查仓库 owner 的 GitHub identity access_token。
1843|
1844|TokenResolver 的标准策略链不包含这个 fallback（UserToken 只查 host_user_id，不查 owner）。
1845|处理方式：get_github_token 的薄包装中保留 D32-补丁逻辑，在 TokenResolver 返回空后追加 owner 查询：
1846|
1847|```python
1848|def get_github_token(repo_key=""):
1849|    provider = create_provider("github")
1850|    token = TokenResolver(provider).get_token(repo_key)
1851|    if token:
1852|        return token
1853|    # D32-补丁：TokenResolver 返回空时，查仓库 owner 的 GitHub identity
1854|    try:
1855|        from src.repo_key import parse_repo_key, to_repo_key
1856|        repo_key = to_repo_key(repo_key, provider="github")
1857|        _prov, owner, _name = parse_repo_key(repo_key)
1858|        if owner:
1859|            from src.auth.grape_account_db import get_grape_account_by_username, get_user_identities_with_tokens
1860|            acc = get_grape_account_by_username(owner)
1861|            if acc:
1862|                for i in get_user_identities_with_tokens(acc["id"]):
1863|                    if i.get("provider") == "github" and i.get("access_token"):
1864|                        return i["access_token"]
1865|    except Exception:
1866|        pass
1867|    return ""
1868|```
1869|
1870|**devops_agent_session repo_github_token 列兼容**：
1871|
1872|devops_agent_session 表（db_setup.py）只有 repo_gitcode_token 列，没有 repo_github_token 列。
1873|GitHubProvider.session_token_field() 返回 "repo_github_token"——查 DB 时列不存在会报错。
1874|
1875|处理方式：R16.5.3 已要求 ALTER TABLE devops_agent_session ADD COLUMN token_status。同时需新增 ALTER TABLE devops_agent_session ADD COLUMN repo_github_token TEXT DEFAULT ''（幂等 ALTER，try-except 保护）。
1876|
1877|DevopsAgentSessionToken._fetch 内部 try-except 保护：如果列不存在（未 ALTER），降级为返回空（GitHub 仓库不走 devops_agent_session 第 1 级，直接走第 2 级 managed_repos）。
1878|
1879|### 8.5 用户 token 特殊处理
1880|
1881|用户 token 策略（UserToken）与其他策略的区别：
1882|
1883|| 维度 | RepoBotToken / GlobalBotToken | UserToken |
1884||------|------------------------------|-----------|
1885|| token 类型 | PAT（手动输入） | OAuth token 或 PAT |
1886|| 自动刷新 | 无 | OAuth token 有 refresh_token 时自动刷新 |
1887|| 过期检测 | 主动健康检查（R17.2） | 被动检测（R10，task 401 时触发） |
1888|| 过期标记 | managed_repos / meta 表 | external_identities.status=4 |
1889|| 过期后行为 | 短路返回空串 | 仍返回 token（供 best-effort 发评论） |
1890|| 恢复方式 | 手动重新配置 | 用户重新绑定 |
1891|
1892|UserToken 策略的 `_fetch` 内部调用 `get_user_token_with_refresh`（已有逻辑），包含：
1893|1. `_has_refresh_token` 判断是否有 refresh_token
1894|2. 有 → 检查过期 + 调 `refresh_oauth_token` 自动刷新
1895|3. 无（PAT）→ 直接返回 token，不刷新
1896|4. status=4 → 仍返回 token（R10 设计意图，best-effort 发评论）
1897|
1898|UserToken 的 `_is_expired` 返回 False（不短路）——即使 status=4 也返回 token，保证 best-effort 发评论能力。这是用户 token 策略的特殊行为，在子类中实现，不影响其他策略。
1899|
1900|### 8.5 需求
1901|
1902|#### R18.1 Token 策略基类
1903|
1904|- R18.1.1：定义 TokenStrategy 基类，包含 get_token / _fetch / _is_expired / mark_expired / clear_expired 方法
1905|- R18.1.2：get_token 内部检查过期标记，过期则短路返回空串
1906|- R18.1.3：每个策略有唯一 error_code 属性
1907|
1908|#### R18.2 四级策略实现
1909|
1910|- R18.2.1：DevopsAgentSessionToken — 第 1 级 devops_agent_session.repo_gitcode_token
1911|- R18.2.2：RepoBotToken — 第 2 级 managed_repos.bot_token，含 host_user_id 守卫 + 过期短路
1912|- R18.2.3：GlobalBotToken — 第 3 级全局 bot token，含 host_user_id 守卫 + 过期短路
1913|- R18.2.4：UserToken — 第 4 级用户 token，含 OAuth 自动刷新 + status=4 不短路（best-effort）
1914|
1915|#### R18.3 统一入口 TokenResolver
1916|
1917|- R18.3.1：TokenResolver 按优先级链执行策略，第一个非空 token 胜出
1918|- R18.3.2：detect_and_mark_expired 方法供 fail_task 401 统一调用
1919|- R18.3.3：支持跳过特定策略（如 get_repo_bot_token 跳过第 1 级）
1920|
1921|#### R18.4 向后兼容
1922|
1923|- R18.4.1：现有 13 个函数保留为薄包装，委托 TokenResolver
1924|- R18.4.2：调用方无需改动（函数签名和返回值不变）
1925|- R18.4.3：现有测试全部通过
1926|- R18.4.4：get_user_token_with_refresh / get_user_github_token_with_refresh（deps.py）不被 TokenResolver 替代——它们是 username → token 的函数，UserToken 策略内部调用它们，保持原样
1927|- R18.4.5：get_repo_bot_token_no_global 的 fallback 链保持与现有代码一致（仓库级 → devops_agent_session → 用户 token，跳过全局）
1928|- R18.4.6：所有短路检查（R16.4）新增在现有 token 获取逻辑之后，未标记过期的 token 走原有逻辑完全不变
1929|
1930|#### R18.5 provider 接口隔离
1931|
1932|- R18.5.1：定义 VCSProvider 抽象接口，包含 verify_token / get_user_login / add_collaborator / remove_collaborator / setup_webhook / remove_webhook / token_storage_field / webhook_token_field / session_token_field / global_bot_meta_key / pat_create_url 方法
1933|- R18.5.2：GitCodeProvider 实现 VCSProvider——API https://gitcode.com/api/v5/user，存储列 bot_gitcode_token / gitcode_webhook_token / repo_gitcode_token，meta key global_bot_gitcode_token
1934|- R18.5.3：GitHubProvider 实现 VCSProvider——API https://api.github.com/user，存储列 bot_github_token / github_webhook_token / repo_github_token，meta key global_bot_github_token
1935|- R18.5.4：TokenStrategy 基类持有 VCSProvider 实例，_fetch / _is_expired / mark_expired 内部通过 provider 接口获取存储字段名，不硬编码 if provider == "gitcode" 分支
1936|- R18.5.5：TokenResolver 通过 create_provider 工厂方法创建对应 VCSProvider 实例传入策略链
1937|- R18.5.6：新增 provider 时只需实现 VCSProvider 接口 + 在 create_provider 工厂方法注册，不需要修改任何策略类代码
1938|- R18.5.7：R15 仓库级 token 配置流程中的验证 token / 邀 bot / 重建 webhook 全部通过 VCSProvider 接口调用，不直接调 _req / _github_req / add_collaborator / add_github_collaborator。_rebuild_webhook_with_bot_token 内部的 if provider == "github" 分支也改为通过 provider.remove_webhook() / provider.setup_webhook() 调用，接收 VCSProvider 实例而非 provider 字符串
1939|- R18.5.8：R17 健康检查也通过 provider.verify_token() 调用 VCS API，不直接调 _verify_bot_token。R17.2 健康检查遍历仓库级 token 时按 provider 创建 VCSProvider 实例调 verify_token
1940|- R18.5.9：VCSProvider 接口新增 fetch_user_token(username, user_id) → str 方法——GitCodeProvider 实现调 get_user_token_with_refresh，GitHubProvider 实现调 get_user_github_token_with_refresh。UserToken._fetch 通过 self.provider.fetch_user_token(username, user_id) 调用，不判断 provider 类型（避免 isinstance 硬编码分支）
1941|- R18.5.10：detect_and_mark_expired 中 provider.verify_token(token) 设短超时（5s），避免 VCS API 响应慢阻塞 fail_task。超时视为验证失败（标记过期），下次健康检查 200 时自动清除标记
1942|
1943|### R18.6 Token 操作职责链（TokenActionHandler）
1944|
1945|#### 问题
1946|
1947|R18.1-R18.5 的策略链只解决"取 token"——TokenResolver 返回一个 token 字符串，所有调用方用同一个字符串执行操作。但 4 级 token 在"用 token 做什么"上有各自独立的职责差异：自循环检测逻辑不同、评论 fallback 链不同、过期标记位置不同、webhook 操作权限不同。这些差异当前散落在 fail_task、_post_result_to_issue、pipeline 自循环检测等处的 if _is_user_token_repo / if host_user_id > 0 分支中，不可控且不完整（路径 B 自循环缺口即因此产生）。
1948|
1949|#### 设计
1950|
1951|引入 TokenActionHandler 职责链，与 TokenStrategy 策略链串联。策略链负责"取 token + 级别标识"，职责链负责"按级别执行差异化操作"。
1952|
1953|#### 4 级 token 职责差异矩阵
1954|
1955|| 维度 | 第 1 级 session | 第 2 级 仓库级 bot | 第 3 级 全局 bot | 第 4 级 用户 token |
1956||------|----------------|------------------|----------------|------------------|
1957|| actor | 取决于 token 所有者 | managed_repos.bot_username | global_bot_usernames | host_user VCS username |
1958|| 自循环检测 | 查 session 配置 | managed_repos.bot_username | global_bot_usernames | host_vcs_username + 路径 B 判断（R20.1） |
1959|| 过期影响 | agent 专属 | 单仓库 | 所有路径 A 仓库 | 用户路径 B 仓库 |
1960|| 评论 fallback | 无（scope 受限） | 全局 bot（如果有效） | 仓库级 bot（如果有效）/ 无 | 无（路径 B 无 bot） |
1961|| 过期标记 | devops_agent_session.token_status | managed_repos.bot_token_status | meta.global_bot_token_status | external_identities.status=4 |
1962|| 短路后降级 | 第 2 级 | 第 3 级 | 第 4 级（路径 B）/ 无（路径 A） | 不短路（best-effort） |
1963|| webhook 操作 | 不涉及 | R15 邀 bot + 重建 + 旧 bot 移除 | auto_upgrade | webhook_secret 解耦 |
1964|| 配置入口 | agent 管理员 | PUT /repos/bot-token | save_config | 用户重新绑定 |
1965|
1966|#### TokenResult
1967|
1968|策略链返回 TokenResult 而非裸字符串，携带级别信息供职责链分发：
1969|
1970|```python
1971|class TokenResult:
1972|    """token 获取结果，携带级别信息。"""
1973|    token: str           # token 字符串（向后兼容：薄包装取 .token 返回 str）
1974|    level: str           # "session" / "repo_bot" / "global_bot" / "user"
1975|    provider: VCSProvider  # VCSProvider 实例
1976|    repo_key: str        # 仓库标识
1977|    expired: bool        # 是否已标记过期（用户 token status=4 时 expired=True 但仍返回 token）
1978|```
1979|
1980|#### TokenActionHandler 接口
1981|
1982|```python
1983|class TokenActionHandler(ABC):
1984|    """token 操作职责链接口。按级别差异化执行 token 相关操作。"""
1985|
1986|    @abstractmethod
1987|    def is_self_loop_actor(self, actor: str, event_type: str,
1988|                           result: TokenResult) -> bool:
1989|        """判断 actor 是否是 agent 回声（自循环检测）。"""
1990|
1991|    @abstractmethod
1992|    def get_comment_token(self, result: TokenResult) -> str:
1993|        """获取用于发 Issue/PR 评论的 token。
1994|        可能不同于执行 token——例如仓库级 bot 过期时 fallback 到全局 bot。"""
1995|
1996|    @abstractmethod
1997|    def post_issue_comment(self, repo_key: str, issue_num: str,
1998|                           body: str, result: TokenResult) -> bool:
1999|        """发 Issue 评论。按级别差异化处理 actor 和标记。"""
2000|
2001|    @abstractmethod
2002|    def mark_expired(self, result: TokenResult) -> str:
2003|        """标记当前级别 token 过期，返回 error_code。"""
2004|
2005|    @abstractmethod
2006|    def clear_expired(self, result: TokenResult):
2007|        """清除当前级别过期标记（token 重新配置时调用）。"""
2008|```
2009|
2010|#### SessionTokenHandler（第 1 级）
2011|
2012|```python
2013|class SessionTokenHandler(TokenActionHandler):
2014|    """第 1 级：devops_agent_session token。agent 专属配置，scope 可能受限。"""
2015|
2016|    def is_self_loop_actor(self, actor, event_type, result):
2017|        # session token 的 actor 取决于 token 所有者
2018|        # 如未实现独立过期标记，降级为不检测
2019|        return False
2020|
2021|    def get_comment_token(self, result):
2022|        # session token scope 可能受限，不能用于发评论
2023|        # 降级到下一级：返回空，让调用方走 RepoBotTokenHandler
2024|        return ""
2025|
2026|    def post_issue_comment(self, repo_key, issue_num, body, result):
2027|        # session token 通常不用于发评论
2028|        return False
2029|
2030|    def mark_expired(self, result):
2031|        # 写 devops_agent_session.token_status = 'expired'
2032|        # 如未实现独立过期标记，仅记日志
2033|        return "SESSION_TOKEN_EXPIRED"
2034|
2035|    def clear_expired(self, result):
2036|        # 清 devops_agent_session.token_status = ''
2037|        pass
2038|```
2039|
2040|#### RepoBotTokenHandler（第 2 级）
2041|
2042|```python
2043|class RepoBotTokenHandler(TokenActionHandler):
2044|    """第 2 级：仓库级 bot token。单仓库独立 bot 账号。"""
2045|
2046|    def is_self_loop_actor(self, actor, event_type, result):
2047|        # actor = managed_repos.bot_username（仓库级 bot 账号）
2048|        # 查 managed_repos.bot_username WHERE repo_key=?
2049|        bot_username = get_repo_bot_username(result.repo_key)
2050|        return actor == bot_username if bot_username else False
2051|
2052|    def get_comment_token(self, result):
2053|        # 仓库级 bot 有效 → 用仓库级 bot token
2054|        if result.token and not result.expired:
2055|            return result.token
2056|        # 仓库级 bot 过期 → fallback 到全局 bot（如果有效）
2057|        global_token = get_global_bot_token(result.provider)
2058|        if global_token and not is_global_bot_expired(result.provider):
2059|            return global_token
2060|        return ""
2061|
2062|    def post_issue_comment(self, repo_key, issue_num, body, result):
2063|        comment_token = self.get_comment_token(result)
2064|        if not comment_token:
2065|            return False
2066|        # actor = 仓库级 bot_username → 在 bot_usernames 中 → 自循环检测命中
2067|        body_with_marker = body + "\n<!-- grape-auto-comment -->"
2068|        return post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token)
2069|
2070|    def mark_expired(self, result):
2071|        # 写 managed_repos.bot_token_status = 'expired'
2072|        set_repo_bot_token_status(result.repo_key, "expired")
2073|        return "REPO_TOKEN_EXPIRED"
2074|
2075|    def clear_expired(self, result):
2076|        # 清 managed_repos.bot_token_status = ''
2077|        set_repo_bot_token_status(result.repo_key, "")
2078|```
2079|
2080|#### GlobalBotTokenHandler（第 3 级）
2081|
2082|```python
2083|class GlobalBotTokenHandler(TokenActionHandler):
2084|    """第 3 级：全局 bot token。所有路径 A 仓库共用。"""
2085|
2086|    def is_self_loop_actor(self, actor, event_type, result):
2087|        # actor = global_bot_usernames
2088|        bot_usernames = get_global_bot_usernames(result.provider)
2089|        return actor in bot_usernames if bot_usernames else False
2090|
2091|    def get_comment_token(self, result):
2092|        # 全局 bot 有效 → 用全局 bot token
2093|        if result.token and not result.expired:
2094|            return result.token
2095|        # 全局 bot 过期 → fallback 到仓库级 bot（如果有效）
2096|        repo_bot_token = get_repo_bot_token(result.repo_key, result.provider)
2097|        if repo_bot_token and not is_repo_bot_expired(result.repo_key):
2098|            return repo_bot_token
2099|        # 路径 A 仓库无 host_user_id → 用户 token 也拿不到 → 无可用 token
2100|        return ""
2101|
2102|    def post_issue_comment(self, repo_key, issue_num, body, result):
2103|        comment_token = self.get_comment_token(result)
2104|        if not comment_token:
2105|            return False
2106|        body_with_marker = body + "\n<!-- grape-auto-comment -->"
2107|        return post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token)
2108|
2109|    def mark_expired(self, result):
2110|        # 写 meta.global_bot_token_status = 'expired'
2111|        set_meta_value("global_bot_token_status", "expired")
2112|        return "GLOBAL_TOKEN_EXPIRED"
2113|
2114|    def clear_expired(self, result):
2115|        # 清 meta.global_bot_token_status = ''
2116|        set_meta_value("global_bot_token_status", "")
2117|```
2118|
2119|#### UserTokenHandler（第 4 级）
2120|
2121|```python
2122|class UserTokenHandler(TokenActionHandler):
2123|    """第 4 级：用户 token。路径 B 专属，actor = host_user VCS username。
2124|
2125|    核心矛盾：路径 B 下 agent 和用户用同一个 token，actor 完全相同。
2126|    不能仅基于 actor 判断——需要引入 task 执行状态作为额外维度。
2127|    仅当"该 issue 有正在执行的 task" + "actor == host_vcs_username" + "事件类型是 agent 可能发的操作"
2128|    + "评论内容不含开发者指令" 四条都满足时，才视为 agent 回声跳过。
2129|    """
2130|
2131|    def is_self_loop_actor(self, actor, event_type, result):
2132|        # 条件 3：仅对 agent 可能发的操作类型检测
2133|        if event_type not in ("issue_comment", "note_hook_pr", "issue_updated"):
2134|            return False  # issue_opened 等用户正常操作豁免
2135|
2136|        # 条件 2：actor == host_user 的 VCS 用户名
2137|        host_vcs_username = get_host_vcs_username(result.repo_key, result.provider)
2138|        if not host_vcs_username:
2139|            return False  # 查询失败 → 降级为不检测
2140|        if actor != host_vcs_username:
2141|            return False  # actor 不是 host_user → 不是自循环
2142|
2143|        # 条件 4：该 issue/PR 有正在执行的 task
2144|        issue_id = resolve_issue_id_from_event(event_type, result)
2145|        if not issue_id:
2146|            return False  # 无法解析 issue_id → 不检测
2147|        has_running_task = check_running_task(result.repo_key, issue_id)
2148|        if not has_running_task:
2149|            return False  # 无正在执行的 task → 用户主动操作 → 放行
2150|
2151|        # 四条件全部满足 → agent 回声 → 跳过
2152|        return True
2153|
2154|    def get_comment_token(self, result):
2155|        # 路径 B = 无全局 bot token
2156|        # 用户 token 有效 → best-effort 用用户 token
2157|        if result.token:
2158|            return result.token
2159|        # 用户 token 过期 → 尝试全局 bot token（可能已配置但尚未 auto_upgrade）
2160|        global_token = get_global_bot_token(result.provider)
2161|        if global_token:
2162|            return global_token  # best-effort（可能 403）
2163|        return ""
2164|
2165|    def post_issue_comment(self, repo_key, issue_num, body, result):
2166|        comment_token = self.get_comment_token(result)
2167|        if not comment_token:
2168|            return False
2169|        # 注入标记——即使 agent 直接发评论也通过 Handler 注入标记
2170|        # 但 agent 自己调 VCS API 发的评论不经过 Handler → 标记缺失 → 依赖 is_self_loop_actor 兜底
2171|        body_with_marker = body + "\n<!-- grape-auto-comment -->"
2172|        try:
2173|            return post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token)
2174|        except Exception:
2175|            # 用户 token 过期 → 回帖失败 → R20.2 降级处理
2176|            return False
2177|
2178|    def mark_expired(self, result):
2179|        # 写 external_identities.status = 4
2180|        host_user_id = get_host_user_id(result.repo_key)
2181|        mark_identity_expired(host_user_id, result.provider.provider_name)
2182|        return "TOKEN_EXPIRED"
2183|
2184|    def clear_expired(self, result):
2185|        # 用户重新绑定 → 恢复 status=1（已有逻辑）
2186|        pass
2187|```
2188|
2189|#### 职责链分发
2190|
2191|```python
2192|def create_action_handler(result: TokenResult) -> TokenActionHandler:
2193|    """根据 TokenResult.level 创建对应 Handler。"""
2194|    if result.level == "session":
2195|        return SessionTokenHandler()
2196|    elif result.level == "repo_bot":
2197|        return RepoBotTokenHandler()
2198|    elif result.level == "global_bot":
2199|        return GlobalBotTokenHandler()
2200|    elif result.level == "user":
2201|        return UserTokenHandler()
2202|    return None  # 无 token → 无 Handler
2203|```
2204|
2205|#### 调用方使用
2206|
2207|```python
2208|# 取 token + 级别
2209|result = TokenResolver(provider).resolve(repo_key)
2210|if not result or not result.token:
2211|    # 无 token → R16.6 NO_TOKEN_AVAILABLE
2212|    pass
2213|
2214|# 需要级别差异的操作 → 通过 Handler
2215|handler = create_action_handler(result)
2216|
2217|# 步骤 2.0b 开发者指令检测（移到自循环检测之前，R20.6.2）
2218|# /done /cancel /resume 等指令优先处理，不进入自循环检测
2219|if event_type in ("issue_comment", "note_hook_pr"):
2220|    if check_developer_command(note_content):
2221|        handle_developer_command(...)
2222|        skip("developer_command")
2223|        return
2224|
2225|# 自循环检测（pipeline 步骤 2.0，通过 Handler 调用）
2226|if handler and handler.is_self_loop_actor(actor, event_type, result):
2227|    skip("self_loop")
2228|    return
2229|
2230|# 发评论（fail_task / _post_result_to_issue）
2231|comment_token = handler.get_comment_token(result)
2232|handler.post_issue_comment(repo_key, issue_num, body, result)
2233|
2234|# 不需要级别差异的操作 → 直接用 token 字符串
2235|# git push / git clone / API 查询等
2236|git_push(result.token)
2237|```
2238|
2239|#### 辅助函数
2240|
2241|```python
2242|def get_host_vcs_username(repo_key, provider):
2243|    """获取路径 B 仓库的 host 用户 VCS 平台用户名。
2244|    优先从 managed_repos.host_vcs_username 读取（R16.5.5 新增列），
2245|    为空时 fallback 到 external_identities 查询。"""
2246|    # 1. 查 managed_repos.host_vcs_username
2247|    username = get_managed_repo_field(repo_key, "host_vcs_username")
2248|    if username:
2249|        return username
2250|    # 2. fallback: 从 external_identities 查 host_user_id 对应的 provider username
2251|    host_user_id = get_host_user_id(repo_key)
2252|    if host_user_id > 0:
2253|        identity = get_external_identity(host_user_id, provider.provider_name)
2254|        return identity.get("username", "") if identity else ""
2255|    return ""
2256|
2257|def check_running_task(repo_key, issue_id):
2258|    """检查该 issue 是否有正在执行的 task（R20.1.8 + R20.10.5）。
2259|    
2260|    两级查询：
2261|    1. agent_tasks.status=running/dispatched（webhook pipeline 触发的 task）
2262|    2. devops_agent_session.last_active_at > now-300s AND status=active（飞书 VOD / Chat direct 等非 webhook 执行）
2263|    """
2264|    with db_lock:
2265|        c = db.cursor()
2266|        # 1. 查 agent_tasks
2267|        c.execute(
2268|            "SELECT 1 FROM agent_tasks WHERE repo_key=? AND issue_id=? "
2269|            "AND status IN ('running','dispatched') LIMIT 1",
2270|            (repo_key, issue_id)
2271|        )
2272|        if c.fetchone():
2273|            return True
2274|        # 2. 降级：查 devops_agent_session 最近活跃（覆盖非 webhook agent 执行）
2275|        c.execute(
2276|            "SELECT 1 FROM devops_agent_session WHERE repo_key=? "
2277|            "AND status='active' AND last_active_at > datetime('now', '-300 seconds') LIMIT 1",
2278|            (repo_key,)
2279|        )
2280|        return c.fetchone() is not None
2281|
2282|def check_developer_command(note_content):
2283|    """检查评论内容是否含开发者指令（/done /cancel /resume 等）。"""
2284|    note_lower = (note_content or "").lower().strip()
2285|    return any(kw in note_lower for kw in [
2286|        "/done", "完成任务", "任务完成", "确认完成",
2287|        "/cancel", "取消任务",
2288|        "/resume", "恢复任务"
2289|    ])
2290|```
2291|
2292|#### 向后兼容
2293|
2294|- 现有 13 个函数保留为薄包装，返回 `result.token`（str），签名不变
2295|- 新增 `TokenResolver.resolve()` 返回 `TokenResult`，需要级别信息的调用方调新接口
2296|- `TokenResolver.get_token()` 保留，内部调 `resolve()` 取 `.token`，向后兼容
2297|- pipeline 自循环检测、fail_task、_post_result_to_issue 改为调 `resolve()` + `create_action_handler()`
2298|- git push / git clone 等无级别差异的操作继续调 `get_token()`（str）
2299|
2300|#### 需求项
2301|
2302|- R18.6.1：定义 TokenResult 结构（token + level + provider + repo_key + expired），TokenResolver 新增 resolve() 返回 TokenResult
2303|- R18.6.2：定义 TokenActionHandler 抽象接口（is_self_loop_actor / get_comment_token / post_issue_comment / mark_expired / clear_expired）
2304|- R18.6.3：SessionTokenHandler 实现——is_self_loop_actor 降级为不检测，get_comment_token 返回空（scope 受限不用于发评论），mark_expired 写 devops_agent_session.token_status
2305|- R18.6.4：RepoBotTokenHandler 实现——is_self_loop_actor 查 managed_repos.bot_username，get_comment_token 仓库级过期时 fallback 全局 bot，mark_expired 写 managed_repos.bot_token_status
2306|- R18.6.5：GlobalBotTokenHandler 实现——is_self_loop_actor 查 global_bot_usernames，get_comment_token 全局过期时 fallback 仓库级 bot，mark_expired 写 meta.global_bot_token_status
2307|- R18.6.6：UserTokenHandler 实现——is_self_loop_actor 四条件组合判断：(1)路径B仓库 (2)actor==host_vcs_username (3)event_type in issue_comment/note_hook_pr/issue_updated (4)该issue有正在执行的task(status=running/dispatched)。条件4不满足→用户主动操作→放行。get_comment_token 用户 token 过期时 best-effort 全局 bot，mark_expired 写 external_identities.status=4。开发者指令(/done /cancel /resume)在自循环检测之前处理，不被误拦截
2308|- R18.6.7：create_action_handler 工厂方法按 TokenResult.level 分发 Handler
2309|- R18.6.8：pipeline 自循环检测改为通过 handler.is_self_loop_actor() 调用，消除现有 if actor in bot_usernames + if _is_user_token_repo 散落分支
2310|- R18.6.9：fail_task 评论 token 选择改为通过 handler.get_comment_token() 调用，消除现有 if _is_path_b 散落分支
2311|

## 二维矩阵（token 场景分类模型）

| 来源 \ 消费 | obtain（保证有效） | peek（原始值比对） |
|------------|------------------|-------------------|
| bot | 执行 token：pipeline / project_info / onboarding / events / autopilot / Issues / vcs / Repo.bot_token | A2A 鉴权：read_stored_token |
| user | 身份 token：建单 / Chat / scenes / templates verify | — |
| config | — | webhook 验证 → 不属于 token 体系 |

不属于 token 体系的场景：webhook 验证（配置项比对）、settings verify（验证动作）、scenes experience（with_oauth_token 独立链路）。

## 涉及代码文件

- 新增 `src/token_service.py`（service 层，桥接 Repo 和 TokenResolver，避免循环依赖）
- 新增 `src/token_resolver.py`（或在 `src/user/db.py` 中实现 TokenResolver / TokenStrategy / VCSProvider）
- `src/repo.py`（Repo.bot_token 改为调 service 层 `get_repo_bot_token()`）

## 验收标准

1. get_token() 含过期短路——过期标记的 token 返回空串
2. read_stored_token() 纯读——不检查过期、不触发 OAuth refresh、不调 VCS API
3. resolve() 返回 TokenResult（token + level + provider + repo_key + expired）
4. 四个 Handler 各自实现 is_self_loop_actor / get_comment_token / mark_expired
5. VCSProvider 接口 11 个方法，GitCodeProvider / GitHubProvider 各自实现
6. 新增 provider 只需实现 VCSProvider 接口 + 在 create_provider 工厂注册
7. 现有 13 个薄包装函数签名不变，返回 str
8. Repo.bot_token 经 service 层，不直接 import TokenResolver（避免循环依赖）
9. _fetch() 契约：纯读存储值，不检查过期、不刷新、不调 VCS API

## 依赖场景

被所有其他文档依赖，必须最先实现。
