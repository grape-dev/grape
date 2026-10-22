# 设计文档：#372 用户 Token 驱动误判与静默失效修复

> 状态：设计完成
> 基线：weekly-08（2b36ebd50）
> 关联需求：issue372-user-token-core-requirements.md
> 分支：fix/issue372-user-token-core

## 0. weekly-08 已落地修复对照

合并 weekly-08 后，部分 FR 已被其他开发者提交的修复覆盖。设计前先对账，避免重复劳动。

| FR | weekly-08 commit | 覆盖范围 | 我们还需做 |
|----|------------------|---------|-----------|
| FR-11.1（session_store 建档防护）| f022bdfb9 | session_store.py ensure_row：空 assignee→user_id=0；非空→严格映射+EXISTS 守卫+ORDER BY | **需重做**：weekly-08 的 `AND gitcode_user!='' AND github_login!=''` 两列都卡，误杀单平台用户。改为按 repo_key provider 分查 |
| FR-11.2（同族查询防护）| f022bdfb9 | issues_center.py L74 已加 EXISTS 守卫 | **需补**：其余查询按 FR-17 分列查询方案修复 |
| FR-17（跨平台分列查询）| — | 全部未修 | **需做**：见 §2.5 详细方案 |
| FR-11.3（写入侧空串置 NULL）| — | 未修 | **需做** |
| FR-11.4（dev 存量善后）| — | 未修 | **需做（脚本）** |
| FR-11.5（回归测试）| f022bdfb9 | test_issue_owner_attribution.py 覆盖 QA01-QA07 | **已完成，补充 FR-11.2/11.3 的测试** |
| 探活时序（非 FR，相关）| 105bd0c2f | _probe_stale_repos 从 /repos/member 请求路径解耦为异步节流 | 探活入口已优化，但判定逻辑未改 |
| host 路由（非 FR，相关）| b95fdfff4 | host_repo 按 repo_key 的 provider 路由，不按登录平台 | 与 FR-5.3 GitHub 侧防护相关 |

## 1. 改动总览

按需求文档 §8 实施建议分批 commit。本设计文档覆盖全部 FR，每批次标注改动文件和行级方案。

### 批次总表

| Commit | FR | 优先级 | 改动文件 | 改动量 |
|--------|-----|--------|---------|--------|
| 0 | FR-11.2/11.3/11.4 | P0 | issues_center.py, session_manager.py, engine_select.py, session_store.py（写入侧）, 清洗脚本 | ~80 行 |
| 0b | FR-13 | P0 | deps.py | 2 行 |
| 1 | FR-1/FR-2/FR-8 | P0 | repos.py | ~80 行 |
| 2 | FR-9 | P0 | settings.py | ~30 行 |
| 3 | FR-3/FR-7 | P1 | deps.py, settings.py, 前端 js | ~60 行 |
| 4 | FR-4/FR-5/FR-6/FR-10 | P1/P2 | repos.py, settings.py, repo_manager.py, token_resolver.py | ~100 行 |
| 4b | FR-12 | P1/P2 | issues.py, event_pipeline.py, handler_async.py | ~80 行 |
| 4c | FR-14/FR-15 | P2 | handler_async.py, grape_account_db.py, db.py, grape_auth.py | ~40 行 |
| 4d | FR-16.1 | P1 | db_setup.py, 清理脚本 | ~30 行 |
| 5 | FR-18.1-18.3 | P0 | engine_select.py, session_store.py | ~30 行 |
| 5b | FR-18.4-18.5 | P1 | session_manager.py, grape_auth.py, prompt_builder.py, 11 处 OR 读点 | ~40 行 |
| 6 | FR-19 | P2 | 根治方案（独立窗口，另行评审） | — |
| 7 | FR-20.1-20.6 | P0 | db_setup.py, mysql_compat.py, repos.py, settings.py | ~80 行 |
| 7b | FR-20.7-20.15 | P1 | token_resolver.py, session_manager.py, session_store.py, settings.py, repo_manager.py, user/db.py, repos.py | ~60 行 |
| 7c | FR-20.16-20.18 | P2 | docs/, user/db.py, grape_account_db.py | ~20 行 |

---

## 2. Commit 0：FR-11 空串匹配修复（补完 weekly-08 未覆盖部分）

### 2.1 FR-11.2：5 处同族查询加空入参早退

**已有防护**（weekly-08 f022bdfb9 已修，不动）：
- session_store.py L122-138：空 assignee→user_id=0，非空→严格映射
- issues_center.py L74-76：已有 EXISTS 守卫

**需补防护**（5 处）：

**issues_center.py L79**：fallback 查询无空串过滤
```diff
-     row = c.execute(
-         "SELECT user_id FROM unified_user_mapping WHERE (gitcode_user=? OR github_login=?) AND user_id>0 LIMIT 1",
-         (gitcode_user, gitcode_user)).fetchone()
+     if gitcode_user:
+         row = c.execute(
+             "SELECT user_id FROM unified_user_mapping WHERE (gitcode_user=? OR github_login=?) AND user_id>0 "
+             "AND gitcode_user!='' AND github_login!='' LIMIT 1",
+             (gitcode_user, gitcode_user)).fetchone()
+     else:
+         row = None
```

**issues_center.py L95**：_get_feishu_open_id 空入参查 open_id
```diff
  def _get_feishu_open_id(gitcode_user: str) -> str:
+     if not gitcode_user:
+         return ""
      with db_lock:
          c = db.cursor()
```

**session_manager.py L431/433**：get_feishu_open_id 空入参
```diff
  def get_feishu_open_id(gitcode_user, app_id=""):
+     if not gitcode_user:
+         return None
      with db_lock:
```

**session_manager.py L469**：update_unified_user_info 查询无空串过滤
```diff
+     if not gitcode_user:
+         return False
      with db_lock:
          c = db.cursor()
          stored = c.execute(
              "SELECT user_id, feishu_open_id, feishu_name, app_id FROM unified_user_mapping WHERE gitcode_user=? OR github_login=?",
```
注：L469 的查询是按 gitcode_user 查已有行做更新决策，空入参会命中空串列行。加早退。

**engine_select.py L169**：_resolve_repo_user_id 空入参
```diff
      if issue_creator:
+         if not issue_creator.strip():
+             return 0
          c.execute(
              "SELECT user_id FROM unified_user_mapping "
              "WHERE (gitcode_user=? OR github_login=?) AND user_id>0 LIMIT 1",
```

### 2.2 FR-11.3：写入侧空串置 NULL

**session_manager.py** update_unified_user_info L497 的 UPDATE 语句：
```diff
  c.execute(
      "UPDATE unified_user_mapping SET gitcode_user=?, gitcode_nickname=?, feishu_open_id=?, feishu_name=?, app_id=?, updated_at=? WHERE user_id=?",
-     (gitcode_user, gitcode_nickname, open_id, feishu_name, final_app, datetime.now().isoformat(), user_id))
+     (gitcode_user or None, gitcode_nickname or None, open_id or None, feishu_name or None, final_app or None, datetime.now().isoformat(), user_id))
```
注：空串→None（SQLite 存 NULL）。但 gitcode_user 在调用方一定非空（前面已早退），主要是 feishu_open_id/feishu_name 可能空串。

**session_manager.py L231** INSERT OR IGNORE：
```diff
  db.execute(
      "INSERT OR IGNORE INTO unified_user_mapping (user_id, gitcode_user, gitcode_nickname) "
-     "SELECT id, username, nickname FROM grape_accounts WHERE id>0")
+     "SELECT id, CASE WHEN username='' THEN NULL ELSE username END, CASE WHEN nickname='' THEN NULL ELSE nickname END FROM grape_accounts WHERE id>0")
```

### 2.3 FR-11.4：存量清洗脚本

新增 `scripts/clean_empty_strings_to_null.py`：
- 扫描 unified_user_mapping 表，将 gitcode_user/github_login/feishu_open_id/feishu_name 空串列更新为 NULL
- 幂等：`UPDATE ... SET col=NULL WHERE col=''`
- dry-run 模式先统计

### 2.4 场景用例（FR-11）

（见需求文档场景 G）

### 2.5 FR-17 跨平台分列查询

**问题根因**：`(gitcode_user=? OR github_login=?)` 用一个用户名同时扫两列。GitCode 用户 "user_b" 和 GitHub 用户 "user_b" 是不同的人，OR 会返回错误行。

**修复策略**：按用户来源平台查对应列。有 repo_key 用 `parse_repo_key` 获取 provider；无 repo_key 但参数名声明 `gitcode_user` 时只查 gitcode_user 列。

#### 2.5.1 session_store.py ensure_row（FR-17.1，重做 weekly-08 的写法）

weekly-08 的 `AND m.gitcode_user != '' AND m.github_login != ''` 要求两列都非空，GitCode-only 用户（github_login 空）会被误杀。改为按 repo_key provider 分查：

```python
_assignee = assignee or (fields.get("assignee") or "")
if _assignee:
    from src.repo_key import parse_repo_key as _prk
    _provider, _, _ = _prk(repo_key)
    _col = "github_login" if _provider == "github" else "gitcode_user"
    _usr_expr = (f"COALESCE((SELECT m.user_id FROM unified_user_mapping m "
                 f"WHERE m.{_col} = ? AND m.user_id > 0 "
                 f"AND EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id = m.user_id "
                 f"            AND (ga.username = m.gitcode_user OR ga.username = m.github_login)) "
                 f"ORDER BY m.user_id LIMIT 1), 0)")
    _usr_pre = (_assignee,)
else:
    _usr_expr = "0"
    _usr_pre = ()
```

#### 2.5.2 pipeline.py _resolve_feishu_name（FR-17.1，加 repo_key 参数）

加 repo_key 可选参数，按 provider 分查。调用方 `_resolve_feishu_name_cached` 传 repo_key（`_get_issue_pipeline` 有 repo_key 参数）。

#### 2.5.3 session_manager.py get_feishu_open_id（FR-17.2，只查 gitcode_user）

参数名声明 `gitcode_user`，调用方从 grape_accounts.username 取值，只查 gitcode_user 列。

#### 2.5.4 session_manager.py update_unified_user_info L474（FR-17.2，只查 gitcode_user）

stored 查询从 OR 两列改为只查 gitcode_user 列。

#### 2.5.5 issues_center.py _get_feishu_open_id（FR-17.2，加 repo_key 可选参数）

有 repo_key 则按 provider 分查，无则只查 gitcode_user（调用方 chat.py 无 repo_key，降级安全）。

#### 2.5.6 chat.py L1348 UPDATE（FR-17.2，只查 gitcode_user）

参数来自 grape_accounts.username，只查/更新 gitcode_user 列。

#### 2.5.7 不改动的项

- `get_feishu_mapping`（session_manager.py L530）：全量导出，OR 语义正确
- `issues_center.py` L87 EXISTS 子查询：外层已按 provider 分查，EXISTS 内 OR 不影响正确性

### 2.6 场景用例（FR-17）

（见需求文档场景 K）

---

## 3. Commit 0b：FR-13 时区语义统一

### 3.1 改动

**deps.py L383**（GitCode token 过期检查）：
```diff
- token_expires_at = int(_dt.strptime(token_expires_at, "%Y-%m-%d %H:%M:%S").timestamp())
+ token_expires_at = int(_dt.strptime(token_expires_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
```

**deps.py L542**（GitHub token 过期检查）：
```diff
- token_expires_at = int(_dt.strptime(token_expires_at, "%Y-%m-%d %H:%M:%S").timestamp())
+ token_expires_at = int(_dt.strptime(token_expires_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
```

需在文件顶部确认 `timezone` 已导入（`from datetime import timezone`）。

### 3.2 场景用例

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| I1 token 2h 后过期（+08 服务器） | OAuth 登录 expires_in=7200 | 触发 get_user_token_with_refresh | need_refresh=False，不发 refresh | FR-13.1 |
| I2 token 5 分钟后过期 | expires_in=300 | 同上 | need_refresh=True，正常刷新 | FR-13.1 回归 |
| I3 非 +08 服务器 | UTC 时区服务器 | 同 I1 | 判定正确（UTC 显式解析） | FR-13.1 |

---

## 4. Commit 1：FR-1/FR-2/FR-8 探活判定收紧

### 4.1 FR-1.3 token 能力前置检查

在 `_probe_stale_repos` 函数开头（L193 之后、all_repos 查询之前）新增：

```python
def _check_token_capability(api_token, github_token):
    """探活前验证 token 有效且有 hooks 读权限。返回 (ok, reason)。"""
    # 1. 验证 token 有效
    if api_token:
        try:
            req = urllib.request.Request(
                "https://gitcode.com/api/v5/user",
                headers={"Authorization": f"Bearer {api_token}"}, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass  # 200 = 有效
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return (False, "token_invalid")
            # 其他错误不阻断
        except Exception:
            pass
    # 2. 对任一托管仓库做 hooks 探测（403/空 = 权限不足）
    #    在调用方做，因为需要 repo owner/name
    return (True, None)
```

### 4.2 FR-1.1 hooks 空列表判"不确定"

```diff
  hook_ids = {h.get("id") for h in hooks if isinstance(h, dict)} if isinstance(hooks, list) else set()
- if expected_wh not in hook_ids:
-     stale_keys.append((repo_key, owner, repo_name))
-     log.debug(f"managed repos: webhook {expected_wh} not found for {repo_key}, marking stale")
+ if not hook_ids:
+     # 空列表 = 权限不足或无 hooks，判"不确定"，跳过
+     log.debug(f"managed repos: hooks list empty for {repo_key}, uncertain (skip)")
+ elif expected_wh not in hook_ids:
+     stale_keys.append((repo_key, owner, repo_name))
+     log.debug(f"managed repos: webhook {expected_wh} not found for {repo_key}, marking stale")
```

### 4.3 FR-1.2 仓库 404 双信号交叉验证

在 `_check_exists` 返回 ("deleted", ...) 后，加交叉验证：仓库 404 时检查是否仍在 all_repos 列表中（VCS 拉回的列表）。

实际上 `_probe_stale_repos` 内部没有 VCS 拉回的 user_repos 列表。交叉验证改为：404 后再查一次 hooks API，如果 hooks 返回 403 或非 404，则仓库可能仍存在（404 是 token 权限问题），跳过。

简化方案（更可靠）：404 时追加一次 `/repos/{owner}/{repo}` 的 bot token 查询（bot 是 maintainer，有读权限），bot token 也 404 才判删除。

```python
# 在 stale_keys 收集后、清理前，对 deleted 类型做双信号验证
if stale_keys:
    from src.token_resolver import TokenResolver, create_provider
    verified_deletes = []
    for repo_key, owner, repo_name in stale_keys:
        # 用 bot token 二次验证
        provider, _, _ = parse_repo_key(repo_key)
        resolver = TokenResolver(create_provider(provider))
        bot_token = resolver.read_stored_token(repo_key)  # 不检查过期
        if bot_token:
            try:
                url = f"https://gitcode.com/api/v5/repos/{owner}/{repo_name}" if provider != "github" else f"https://api.github.com/repos/{owner}/{repo_name}"
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bot_token}"}, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    pass  # 200 = 仓库仍存在，用户 token 404 是权限问题
                    log.info(f"probe: {repo_key} 404 with user token but 200 with bot token, skip (permission issue)")
                    continue
            except urllib.error.HTTPError as e2:
                if e2.code == 404:
                    verified_deletes.append((repo_key, owner, repo_name))
                # 其他错误跳过不判
            except Exception:
                pass  # 不判定
        else:
            # 无 bot token，无法二次验证，保留原判定但标记为 unverified
            verified_deletes.append((repo_key, owner, repo_name))
    stale_keys = verified_deletes
```

### 4.4 FR-2.1 webhook 丢失不删数据

将 stale_keys 分为两类：deleted（仓库删除）和 webhook_lost（webhook 丢失）。

```python
# webhook 丢失的处理（不删除数据）
webhook_lost_keys = []  # 从 stale_keys 中分离
# deleted 的走级联清理
# webhook_lost 的只置"待修复"状态
for repo_key, owner, repo_name in webhook_lost_keys:
    log.info(f"probe: webhook lost for {repo_key}, set pending-repair (no data deletion)")
    # 标记 managed_repos.webhook_status = 'pending_repair'（新增列或用 meta）
    # 前端读此状态显示"重检修复"横幅
```

新增 managed_repos 列 `webhook_status TEXT DEFAULT ''`：
- `''` = 正常
- `'pending_repair'` = 待修复
- repair_host 成功后清除

### 4.5 FR-2.3 单轮清理上限

```python
# 清理前保险丝
if len(stale_keys) > 2:
    log.warning(f"probe: {len(stale_keys)} stale repos in one round, abort entire round (safety fuse)")
    return
```

### 4.6 FR-8 兜底 token 收口

`_probe_stale_repos` 的 api_token 来自 `with_oauth_token`，已经是用户 token。FR-8 要求兜底 `get_repo_token_with_refresh` 也走能力前置检查——在函数入口统一做 FR-1.3 的 `_check_token_capability`，所有 token 来源都经过这一关。

### 4.7 场景用例

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| B1 webhook 真丢失 | 用户在 VCS 手动删 webhook（还有其他 hook）| 打开项目页 | hooks 非空且缺 expected_wh → 待修复状态+横幅，零数据删除 | FR-1.1/FR-2.1 |
| B2 webhook 查询空列表 | 窄 scope PAT | 同上 | 判不确定跳过 | FR-1.1 |
| B3 仓库真删除（双信号）| 仓库在 VCS 已删 | 同上 | 双信号成立→级联清理+审计日志 | FR-1.2/FR-2.2 |
| B4 仓库 404 但 VCS 列表仍在 | PAT 无该仓库读权限 | 同上 | 交叉验证跳过不判 | FR-1.2 |
| B5 批量误判保险丝 | token 失效致 10 仓同时 stale | 同上 | stale>2→整轮放弃+告警 | FR-2.3 |
| B6 兜底 token 复活路径 | 路径 B 探活走兜底 token | 同上 | 兜底 token 过能力前置检查 | FR-8 |

---

## 5. Commit 2：FR-9 设置页入口收敛

### 5.1 改动

**settings.py** `update_gitcode_repo_config`（L565）：

```python
def update_gitcode_repo_config(owner, repo, body, user):
    from src.user.db import update_managed_repo_bot_token, update_managed_repo_bot_username
    from src.user.repo_manager import _req  # 复用 R15 的 verify
    repo_key = _resolve_repo_key(f"{owner}/{repo}")
    if body.bot_token and not _is_placeholder(body.bot_token):
        token = body.bot_token.strip()
        # FR-9.1: verify token 有效性（与 R15 同口径）
        try:
            _req("GET", "https://gitcode.com/api/v5/user", token)
        except RuntimeError:
            raise HTTPException(400, detail={"code": 400, "error_code": "TOKEN_INVALID", "message": "Token 无效或已过期"})
        # 权限校验：托管人/admin
        # ...复用 host_repo 的 _check_admin 逻辑...
        update_managed_repo_bot_token(repo_key, token)
    if body.bot_username:
        update_managed_repo_bot_username(repo_key, body.bot_username)
    return {"message": "GitCode Repo 配置已更新"}
```

### 5.2 场景用例

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| E1 保存无效 token | 粘贴了过期 token | 设置页保存 | 400 TOKEN_INVALID 拒写 | FR-9.1 |
| E2 非托管人保存 | 普通成员 | 同上 | 403 拒绝 | FR-9.1 |
| E3 有效 token 但重建失败 | token 有效但 hooks 受限 | 同上 | token 写入+warning | FR-9.2 |

---

## 6. Commit 3：FR-3/FR-7 提示闭环

### 6.1 FR-7 OAuth refresh 失败标记

**deps.py** `return new_token or token`（L395/L553）：
```diff
  if need_refresh:
      new_token = _try_refresh_gitcode_token(user_id)
+     if not new_token:
+         # FR-7.1: 刷新失败标记 expired
+         from src.auth.grape_account_db import mark_identity_expired
+         mark_identity_expired(user_id, "gitcode")
      return new_token or token
```

### 6.2 FR-3 用户提示

设置页凭据区新增告警条，读取 external_identities.status=4 时显示。

### 6.3 场景用例

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| D2 OAuth 刷新失败 | refresh_token 也失效 | 任意写操作 | 立即 expired 标记；设置页"GitCode 登录已过期" | FR-7.1/7.3 |
| D3 用户重新 OAuth | D2 之后 | 重新登录 | expired 标记自动清除 | FR-7.2 |

---

## 7. Commit 4：FR-4/FR-5/FR-6/FR-10 同款位点+路径 B

### 7.1 FR-5.1 find_existing_webhook 空列表判不确定

**repo_manager.py** find_existing_webhook：
```diff
  def find_existing_webhook(api_token, owner, repo, webhook_url, _retries=3):
+     if not api_token:
+         return None  # 不确定
      ...
      hooks = _req("GET", url, api_token)
      if isinstance(hooks, list):
+         if not hooks:
+             return None  # 空列表=不确定，不返回"无 webhook"
          for h in hooks:
```

### 7.2 FR-5.2 _check_admin 异常区分

**repos.py** _check_admin（L973）已有穿透逻辑（P1-1 返工），但 except 需区分：
```diff
  except HTTPException:
      raise
+ except RuntimeError as e:
+     # token 权限不足或已过期，不是"确认无权限"
+     raise HTTPException(403, detail={"code": 403, "error_code": "TOKEN_PERMISSION_INSUFFICIENT",
+         "message": "Token 权限不足或已过期，请更新后重试或改用 OAuth"})
```

### 7.3 FR-6.1 webhook 查找改用 bot token

host_repo/create_new_repo 中 find_existing_webhook 的 token 参数，路径 B 时改用 bot token。

### 7.4 场景用例

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| C1 窄 PAT 托管仓库 | 用户 token 为 PAT | 点托管 | 提示"Token 权限不足" | FR-5.2 |
| C3 find_existing_webhook 空列表 | PAT 无 hooks 权限 | 触发调用 | 判不确定跳过新建 | FR-5.1 |
| F1 host 窄 PAT 保存提示 | 纯路径 B 部署 | 保存 PAT | 提示缺 issue 写权限 | FR-10.1 |
| F5 level 过期漂移 | host token 过期+路径 B | 触发事件 | 自循环检测读 config_level | FR-10.3 |

---

## 8. Commit 4b：FR-12 空值链治理

### 8.1 FR-12.2 孤儿需求操作收紧

**issues.py** L655/685/739 三处 `OR user_id=0`：
```diff
- c.execute("SELECT repo_key, issue_number, status FROM issue WHERE id=? AND (user_id=? OR user_id=0)",
+ c.execute("SELECT repo_key, issue_number, status FROM issue WHERE id=? AND (user_id=? OR ?=1)",
+           (issue_id, user["id"], getattr(user, 'is_admin', 0)))
```
注：admin（is_admin=1）可操作孤儿需求，普通用户只能操作自己的。

### 8.2 FR-12.4 飞书 pending 空 uid 防误建

**handler_async.py** 会话复用前检查 uid 有效性。

### 8.3 场景用例

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| H4 孤儿需求操作越权收紧 | 孤儿需求 id=X | 普通用户调确认完成 | 拒绝 403 | FR-12.2 |
| H7 飞书 pending 空 uid | 空 actor 事件 | 通知派发 | 不新建 task 会话 | FR-12.4 |

---

## 9. Commit 4c：FR-14/FR-15 覆盖写入+LIKE 转义

### 9.1 FR-14.1 INSERT OR REPLACE → ON CONFLICT

**handler_async.py** L1091/1170/1521 三处：
```diff
- c.execute("INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key, source, bind_by, im_app_id) VALUES (?,?,?,?,?)",
+ c.execute("INSERT INTO repo_im_group_binding (im_chat_id, repo_key, source, bind_by, im_app_id) VALUES (?,?,?,?,?) "
+           "ON CONFLICT(im_chat_id, repo_key) DO UPDATE SET source=excluded.source, bind_by=excluded.bind_by, im_app_id=excluded.im_app_id",
```

### 9.2 FR-15.1 LIKE 转义

**grape_auth.py** L980/990/1036/1056 四处：
```diff
- "WHERE u.user_id=? AND m.repo_key LIKE ? "
+ "WHERE u.user_id=? AND m.repo_key LIKE ? ESCAPE '\\' "
```
值侧：`provider + ":%"` → `provider + ":%".replace("%", "\\%").replace("_", "\\_")`

### 9.3 场景用例

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| I5 群重绑保配置 | notify_enabled=1 | 重新绑定 | notify_enabled 保持 1 | FR-14.1 |
| I8 LIKE 转义 | provider 含 % | 走解绑 | 仅匹配字面量 | FR-15.1 |

---

## 10. Commit 4d：FR-16.1 会话唯一约束

### 10.1 DDL

**db_setup.py** chat_conversations 表：
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_conv_repo_issue
ON chat_conversations (user_id, repo_key, issue_number, conv_type)
WHERE issue_number IS NOT NULL AND issue_number != '' AND conv_type IS NOT NULL AND conv_type != '';
```
注：部分唯一索引——普通会话（无 issue_number）不参与约束。

### 10.2 场景用例

| 用例 | 前置 | 操作 | 预期 | 覆盖 FR |
|------|------|------|------|---------|
| J1 双主并发建同一会话 | 双主共库 | 并发 INSERT | 一方撞约束降级复用 | FR-16.1 |
| J2 普通会话不受约束 | issue_number 空 | 创建多个 | 不触发唯一冲突 | FR-16.1 回归 |

---

## 11. 验证方法

每个 commit 完成后：
1. `python -m pytest tests/ -v`（全量测试，确保无回归）
2. 新增测试覆盖对应 FR 的场景用例
3. grep 确认旧模式零残留（如 `INSERT OR REPLACE` / `z-index:999` / 空 `OR github_login` 无守卫）
4. 前端改动 bump `?v=` 版本号

## 12. 向前兼容性

- 旧字段/旧行为保留，新增判定只收紧不放宽
- DDL 用 `CREATE ... IF NOT EXISTS` + `ALTER TABLE ... try/except` 兜底
- 空串清洗脚本幂等，可重复执行
- 前端告警条/角标为新增元素，不影响已有 UI

## 13. FR-18 um 主路径查询缺陷止血修复

止血方案：um 体系内最小修复，只修查询正确性与最危险的写侧漏洞，不换锚、不建表、不清数据、无切换窗口。

### 13.0 事实基线与根因

事实基线（评审先核）：
- 合入目标树：仓库根目录（engine_select.py:167-170、session_store.py:123-129 行号按此树）
- 数据库：grape_system_db@YOUR_DB_HOST
- R4 争议裁定：session_store.py:125 双列非空条件经四棵树全量核查不存在——grape-dev-repo 为裸 OR，其余树为单列查询或无此文件。若评审现场在其他环境确有双列非空条件，则 13.2 升回 P0，其余不受影响

数据现状（2026-10-21 实测 grape_system_db）：
- um：user_id>0 共 113 行；gitcode_user 113/113 非空且全部=grape_accounts.username（注册名）；github_login 非空 0 行
- external_identities：gitcode 90 行（69 行 external_login 非空）、github 21 行（6 行非空）
- issue 表：2504 行，user_id=0 共 356（gitcode 侧 343/2377≈14%，github 侧 13/128≈10%）。0 的真实成因是 um.gitcode_user 存注册名而 issue assignee 是平台真名，查询落空——即 R1 本身

根因（三条）：
- R1 命名空间混存：backfill 把 Grape 自由注册名灌进 um.gitcode_user（session_manager.py:231），三个命名空间混存两列。113/113 行实锤
- R2 可撞名冒领：回退链按 grape_accounts.username 匹配 actor，username 任何人自选
- R3 无唯一真源：um 平台列多写源打架（回填=注册名/PAT=真名/OAuth=不写），github_login 全库 0 行有效；um 无"该行经平台验证"标志

### 13.1 FR-18.1：主路径分列+空参守卫（engine_select.py:167-170）

```python
provider, _, _ = parse_repo_key(repo_key or "")
col = "github_login" if provider == "github" else "gitcode_user"
row = c.execute(
    f"SELECT user_id FROM unified_user_mapping "
    f"WHERE ?!='' AND {col}=? AND {col}!='' AND user_id>0 LIMIT 1",
    (issue_creator, issue_creator)).fetchone()
```

列名来自 provider 受控枚举，非用户输入，无注入面。gitcode 侧当前 um=注册名，分列后行为与现状一致；github 侧列空不中，交回退链。

### 13.2 FR-18.2：session_store.py:123-129 建卡归属同款分列（可选，语义统一）

本环境现状为裸 OR，无 R4 bug，故此项不修任何现行错误，价值仅是防未来 github 侧空串误配 + 与 13.1 语义统一。建议与 13.1 同 MR 顺手合入，不单独立项。若评审裁定其他环境存在双列非空版本，则此项升回 P0。

```python
_provider, _, _ = parse_repo_key(repo_key)
_col = "github_login" if _provider == "github" else "gitcode_user"
_usr_expr = (
    f"COALESCE((SELECT m.user_id FROM unified_user_mapping m "
    f"WHERE m.{_col}=? AND m.user_id > 0 "
    f"AND EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id = m.user_id AND ga.status=1) "
    f"ORDER BY m.user_id LIMIT 1), 0)")
_usr_pre = (_assignee,)
```

### 13.3 FR-18.3：回退链 EXISTS 收严（engine_select.py:176/190）

```sql
-- 回退 2（grape_accounts）加 EXISTS 校验——用 external_identities 验证（有授权语义）
SELECT ga.id FROM grape_accounts ga
WHERE LOWER(ga.username)=LOWER(?) AND ga.id>0 AND ga.status=1
  AND EXISTS (SELECT 1 FROM external_identities ei
              WHERE ei.grape_user_id=ga.id AND ei.provider=? AND ei.status=1
              AND ei.external_login=ga.username)
LIMIT 1
-- 参数 (actor, provider)
```

语义：注册名匹配仅在"该账号在平台侧验证过的 login（external_identities.external_login）恰好等于这个注册名"时生效。ei 行只能经 OAuth/PAT 创建（有授权语义），backfill 不碰它，攻击者拿别人名字注册时 ei 里不会有这个 login 的记录。

注意：不能用 um 验证 um（backfill 写的 um.gitcode_user=ga.username 结构性恒真，EXISTS 必然通过，等于自证循环）。

代价：现状 113 个账号里能满足"ei.external_login == 注册名"的只是少数（PAT 绑定用户里的一部分）。回退链从"几乎总命中"变成"很少命中"，gitcode 侧 issue 归属走回退链的那部分 user_id=0 占比会上升——升高的就是"正确拒绝"，须上线监控逐条核对。

user_repos JOIN 回退处加同款 EXISTS 条件。

### 13.4 FR-18.4：写侧守卫

1. update_unified_user_info（session_manager.py:469/497）：反查加空参守卫；UPDATE 改 CASE WHEN ?='' THEN gitcode_user ELSE ? END，空串不覆盖真值：
```python
c.execute(
    "UPDATE unified_user_mapping SET gitcode_user=CASE WHEN ?='' THEN gitcode_user ELSE ? END, "
    "feishu_open_id=?, feishu_name=?, app_id=?, updated_at=? WHERE user_id=?",
    (gitcode_user, gitcode_user, open_id or None, ...))
```

2. prompt_builder.py:328 提示词裸 SQL 加约束：空值不 UPDATE 该列、禁 user_id=0 占位行。

3. backfill 改源（session_manager.py:231）：
```python
# 原：INSERT OR IGNORE ... SELECT id, username, nickname FROM grape_accounts
# 改为：只建 (user_id) 空行 + 从 external_identities 回填平台列真值
"INSERT OR IGNORE INTO unified_user_mapping (user_id) SELECT id FROM grape_accounts WHERE id>0"
# 紧接从 ei 回填（仅填空值不覆盖 PAT 已写真值）
"UPDATE unified_user_mapping SET gitcode_user=("
"  SELECT ei.external_login FROM external_identities ei "
"  WHERE ei.grape_user_id=unified_user_mapping.user_id "
"  AND ei.provider='gitcode' AND ei.status=1 AND ei.external_login!='' LIMIT 1) "
"WHERE (gitcode_user IS NULL OR gitcode_user='')"
# github_login 同理
```
绑定过的人回填真 login（飞书 sync 传的本来就是平台 login，真 login 更对），未绑定的本地用户 um.gitcode_user 为空——这类用户的飞书绑定靠自己的注册/绑定动作建立。需跟飞书链路回归测试。

### 13.5 FR-18.5：其余 11 处 OR 读点空参守卫

issues_center:73/79/95、pipeline:53、chat.py:1328/1425、session_manager:431/433/469 等：加前置 ?!='' 守卫，维持 OR（飞书反查无平台语境，OR 属合理兜底）。

### 13.5b FR-18.6：OAuth 注册补写 um 平台列

grape_auth.py create_external_identity 成功后，按 provider 补写 um 平台列（照抄 settings.py:237 既有模式）：

```python
# 在 create_external_identity 成功后追加（grape_auth.py 三处调用点 L1548/1711/2044/2160）
from src.session_manager import db as _sm_db, db_lock as _sm_dl
with _sm_dl:
    _c = _sm_db.cursor()
    _c.execute("INSERT OR IGNORE INTO unified_user_mapping (user_id) VALUES (?)", (account["id"],))
    _col = "github_login" if provider == "github" else "gitcode_user"
    _c.execute(f"UPDATE unified_user_mapping SET {_col}=?, updated_at=datetime('now') WHERE user_id=?",
               (external_login, account["id"]))
    _sm_db.commit()
```

消除 GitHub OAuth 注册→重启间 um.github_login 空窗。gitcode OAuth 同理补写 gitcode_user=external_login。

### 13.5c 防复发判定（5 条链路 + 4 块拼图）

5 条链路逐条判定：

| 链路 | 状态 | 堵法 |
|------|------|------|
| backfill 灌注册名 | 18.4 改源后堵住 | INSERT 只建 (user_id) + ei 回填真值 |
| GitHub OAuth 不写 um | 18.6 补写后堵住 | 注册成功即写 um.github_login |
| 飞书注册名回写 | 18.7 分语境写入后堵住 | 拆 bind_feishu_identity 只写飞书三列 |
| bot 裸 SQL 造伪身份 | 已堵 | 18.4 prompt_builder 约束 |
| update_unified 空串洗值 | 已堵 | 18.4 CASE WHEN |

4 块拼图（缺一不可，防复发 MR 排期）：

| 块 | FR | 堵什么 | 量级 |
|----|-----|--------|------|
| 1 | 18.6 | OAuth 注册不写 um | ~8 行 |
| 2 | 18.4 | backfill 灌注册名 | ~15 行 + 飞书回归 |
| 3 | 18.7 | 飞书注册名回写 | 中型 MR（拆函数+迁移 3 处调用方） |
| 4 | 执行纪律 | 清洗清列保行 | 清洗脚本约束 |

关键漏洞详述（活口 3）：feishu_onboard:266 和 handler_async:1431 调 update_unified_user_info 传 grape_accounts 注册名。CASE WHEN ?='' THEN gitcode_user ELSE ? END 只判空串——注册名非空，直接覆盖 gitcode_user。链条：backfill 改源写入真 login → 用户扫码绑飞书 → update_unified_user_info(注册名, ...) → CASE 判定非空 → gitcode_user 被洗回注册名 → 伪身份再生。

结论：4 块拼图全完后 5 条链路全堵。当前止血分支维持现状合入（声明只止血不防复发），4 块拼图作为紧随其后的防复发 MR 排期。

### 13.5d FR-18.7：update_unified_user_info 分语境写入（防复发 MR）

问题：update_unified_user_info 被三条路径共用，语义不同：
- feishu_onboard:266（注册名语境）——扫码绑定飞书，传 grape_accounts.username
- handler_async:1431（注册名语境）——p2p 确认绑定，传 grape_accounts.username
- feishu_async:768（平台 login 语境）——协作者 sync，传平台真 login

前两条传注册名，CASE WHEN 判定非空直接覆盖 gitcode_user → 把 backfill 改源写入的真 login 洗回注册名。

修复：拆两个入口：
```python
def bind_feishu_identity(user_id, open_id, feishu_name="", app_id=""):
    """注册名语境：只写飞书三列，不碰平台列（gitcode_user/github_login）。
    供 feishu_onboard / handler_async p2p 确认调用。
    """
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR IGNORE INTO unified_user_mapping (user_id) VALUES (?)", (user_id,))
        c.execute(
            "UPDATE unified_user_mapping SET feishu_open_id=?, feishu_name=?, app_id=?, updated_at=? "
            "WHERE user_id=?",
            (open_id or None, feishu_name or None, app_id or None, datetime.now().isoformat(), user_id))
        db.commit()
        return True

# 原入口 update_unified_user_info 保留给平台 login 语境（feishu_async:768 / PAT 绑定 / backfill 改源）
```

调用方迁移：
- feishu_onboard:266 → 改调 bind_feishu_identity(user_id, open_id, ...)
- handler_async:1431 → 改调 bind_feishu_identity(user_id, open_id, ...)
- feishu_async:768 → 维持调 update_unified_user_info（平台 login 语境正确）

量级：函数签名改动 + 3 处调用方迁移 + 飞书链路回归，中型 MR，独立排期不塞进止血分支。

### 13.5e 清洗清列保行 + 时序依赖（执行纪律）

清洗存量伪身份时必须 UPDATE SET gitcode_user='' 保行，不可 DELETE 整行：
- INSERT OR IGNORE (user_id, ...) 按 user_id 主键判重——行在则跳过
- DELETE 整行后下次重启 backfill 按 SELECT id FROM grape_accounts 全量重灌，洗了白洗
- UPDATE SET gitcode_user='' 保行后 INSERT OR IGNORE 命中已有行不重灌

时序依赖（钉死）：
```
代码 MR（堵）：块 3 分语境写入（硬前置）+ 块 1 OAuth 钩子 + 块 2 backfill 改源
   ↓ 合入后
体检（只读）→ B 类人工甄别 → A 类分批清列（绑飞书子集单独评估）
   → github 回填 → 143 张 0 卡回填（assignee 非空部分）→ 全链路复测（含 OR 读点）
   ↓
剩余：63 张 assignee 空 0 卡（sender 历史回填或接受）+ C 类登记（根治版换锚消化）
```

4 条铁律：
1. 清列保行（不可 DELETE）。例外：A 类中 feishu_open_id 非空的行单独评估（清列后 OR 读点按注册名反查飞书会断——实测 18 行中 1 行 user_d 有此问题）
2. 清洗→回填顺序不可倒
3. B 类人工过目
4. 清洗硬依赖分语境写入先落地（不堵飞书回写就清洗 = 给洗回机制准备已清空的靶行）

A/B/C/D/E 分类（实测 dev 库，gitcode + github 双向对称）：

| 类 | gitcode 侧 | github 侧 | 处理 |
|----|-----------|-----------|------|
| A 伪身份 | 18 行注册名 | 0 行 | 清列保行 |
| B 撞名 | 注册名=他人 gitcode 真名 | gitcode_user=他人 github 真名（1 行 user33） | 双向体检+人工甄别 |
| C 合法同名 | — | 跨人并存 | 不清，根治版消化 |
| D 死数据 | 孤儿行 | status=0 行（user13 user_d 实测） | 排除出回填 |
| E login 缺失 | — | login 空+status=1（5 行 user24/25/27/29/32） | API 反查 login 回填 identity |

github 侧 4 个独立问题（补全）：
1. E 类：5 行 login 空 status=1，原 D 类 status=0 漏网 → API 按 external_id 反查 login
2. 反向 B 类：user33 gitcode_user='user_a'=user2 github 真名 → B 类体检双向
3. status=0 地雷：user13 user_d 实测 → 回填 SQL AND ei.status=1（代码已写）
4. 0 卡预期：28/29 无 github 绑定 → 大部分正确无主，不是清洗失败

0 卡回填范围：
- gitcode 侧：143 张 assignee 非空可回填，63 张 assignee 空不在范围
- github 侧：4 张 0 卡，预期仅绑定用户可归位，其余正确无主

OR 读点复测（清洗受益验证，含 github 侧）：
- get_feishu_open_id('user_a')：从歧义捞行变单行命中
- _repo_user_id('github:仓', 补 login 后的 login)：命中验证
- OR 读点传反向撞名 login（user_a）不误捞 user33

### 13.6 行为影响与验证

| 场景 | 止血前 | 止血后 |
|------|--------|--------|
| gitcode 仓 + PAT 绑定用户 | 主路径命中 | 分列命中（无变化） |
| issue 建卡（gitcode 用户） | user_id=0（R1 导致） | 分列对齐，不修现行错误 |
| 回退链撞名 actor | 回退命中冒领 | EXISTS 拒绝（堵死） |
| 主路径撞名（actor=注册名） | 直接命中 | 直接命中（止血版无解，已知残留） |
| github 仓 + GitHub 用户 | OR 不中，回退碰巧命中 | 分列不中 → EXISTS 正确拒绝 → 落 0（ei github 6 条有效 login 可兜底） |
| 空串 actor | 可能捞任意行 | 守卫拦截 |
| 飞书绑定/反查全链路 | 4 处按 um.gitcode_user=注册名反查 | CASE 守卫不误伤——传注册名时 ?≠'' 走 ELSE 正常写入；仅空串时保留真值 |

验证清单：
1. gitcode PAT 用户主路径命中回归（主场景，L1）
2. 空 actor 不捞行（L4）；回退撞名构造场景不冒领（L3）
3. 飞书绑定/反查全链路回归（L15）——18.4 CASE 守卫是唯一动写侧的点，重点验
4. 建卡归属抽查（343 基线上不劣化，L2）
5. 上线监控：issue user_id=0 占比，gitcode 侧 343、github 侧 13 为基线（L14）

已知残留风险（止血版无解，根治版消除）：
1. 主路径撞名堵不住（L13）：主路径先查 um，分列后 actor=注册名仍直接命中攻击者的 um 行，EXISTS 在回退链根本走不到。um 表无"该行经平台验证"标志（R3），止血范围内无解。根治版换锚消除
2. github 侧 issue 归属 0 占比可能升高（L14）：现状 10%，收紧后回退链"碰巧命中"消失（本就是错配），方向正确但须监控确认升高部分来自"正确拒绝"而非漏配
3. 存量 113 行注册名伪身份、外部协作者识别——根治版

### 13.7 与根治方案的关系

改动 1/3/4.1 是根治版子集（原样保留零浪费）；改动 3 的 EXISTS 在根治版被 identity 锚替代后删除。根治方案（identity 换锚）另行评审。

## 14. FR-19 数据治理与根治方案（独立窗口）

根治方案占位：identity 换锚 + um 存量清洗 + DEFAULT NULL。不在本版范围，另行评审。

## 15. FR-20 审计修复补完

三路子 agent 并行审计 + 基线对比测试发现的遗漏项。

### 15.1 FR-20.1：部分唯一索引误拦 general 会话修复

问题（db_setup.py:1833）：`WHERE issue_number IS NOT NULL AND issue_number != ''` 作用于 INTEGER 列——SQLite 类型亲和性使 `0 != ''` 求值为 true，issue_number=0 的 general 会话被纳入唯一约束。

修复：改为 `WHERE issue_number > 0 AND repo_key != '' AND conv_type = 'task'`，同时补 conv_type 列（FR-20.15）。

### 15.2 FR-20.2：MySQL 部分索引兼容

问题：MySQL 不支持部分索引 WHERE 子句，mysql_compat 正则不匹配，索引创建失败被 try/except 吞掉。

修复：在 mysql_compat.py 中扩展正则匹配 WHERE 子句，翻译为 MySQL 全列唯一索引（依赖 conv_type 列区分）。

### 15.3 FR-20.3/20.4：token 能力前置检查 + 兜底收口

问题：FR-1.3 设计的 _check_token_capability() 未实现。

实现：`_check_token_capability(token, provider)` 调 /api/v5/user 验证有效性 + hooks 权限探测，在 _probe_stale_repos 入口处调用，401/403 时放弃整轮 + 设置页告警。兜底 token 同样检查。

### 15.4 FR-20.5：设置页托管人/admin 权限校验

修复：update_gitcode_repo_config 中复用 _check_admin 逻辑，非托管人/admin 返回 403。

### 15.5 FR-20.6：401 检测收敛

修复：repos.py:1126 改用 `is_auth_401_error(_err) or "403" in _err`。

### 15.6 FR-20.7：resolve() 性能优化

修复：config_level 从胜出策略推算，不再单独调 resolve_config_level()。

### 15.7 FR-20.8：GitHub-only 用户飞书通知回退

修复：get_feishu_open_id 加 `OR github_login=?` 回退。

### 15.8 FR-20.10：过期 token 不写 session

修复：检查 _is_expired 后决定是否写入空串。

### 15.9 FR-20.11：webhook 重建失败 warning 返回

修复：update_managed_repo_bot_token 返回 {"ok", "warning", "related_repos"}，settings API 返回 warning 字段。

### 15.10 FR-20.12：find_existing_webhook 改用 bot token

修复：路径 B 时 find_existing_webhook 调用方改传 bot token。

### 15.11 FR-20.14：级联清理审计日志补完

修复：清理前后各写审计日志（含两路信号）。

### 15.12 FR-20.16/20.17/20.18：P2 修复

搭车文件剥离 + 异常捕获收窄为 sqlite3.IntegrityError + 共享 token 提示返回。

---

## 16. FR-21 GitHub 对称性修复（第四轮深度审计）

> 来源：三路子 agent 并行审计 + 主审计人独立全链路审查。
> 设计前逐行读取实际源码确认问题，逐条标注行为等价性。

### 16.1 FR-21.1：_repo_platform_token 未定义变量 user_id（P0）

**文件**：src/api/routes/repos.py:2132-2141

**当前代码（有 bug）**：
```python
        def _user():
            try:
                from src.api.deps import get_user_github_token_with_refresh
                username = user.get("username") or ""
                if not username and user.get("id", 0) > 0:
                    from src.auth.grape_account_db import get_grape_account_by_id
                    username = (get_grape_account_by_id(user["id"]) or {}).get("username", "")
                return get_user_github_token_with_refresh(username, user_id=user_id)  # ← user_id 未定义
            except Exception:
                return ""
```

**GitCode 分支（正确，L2150-2158）**：
```python
        def _user():
            try:
                from src.api.deps import get_user_token_with_refresh
                uid = user.get("id") or 0  # ← 正确定义
                ...
                return get_user_token_with_refresh(username, user_id=uid)
```

**修复**：
```python
                return get_user_github_token_with_refresh(username, user_id=user.get("id", 0))
```

**行为等价性**：`user.get("id", 0)` 与 GitCode 分支的 `uid = user.get("id") or 0` 语义一致——user dict 有 id 时取 id，无时取 0。等价 ✓

**注意**：这是基线代码 bug（非本分支引入）。`except Exception: return ""` 吞掉了 NameError——运行时不崩溃但返回空串，GitHub chat agent 拿不到 token。

### 16.2 FR-21.2：feishu_async sync_mapping GitHub 协作者写错列（P0）

**文件**：src/im/feishu_async.py:758-770

**当前代码（有 bug）**：
```python
        from src.session_manager import update_unified_user_info
        for collab in collabs:
            nick = collab.get("nick_name", "") or collab.get("login", "")
            login = collab.get("username", "") or collab.get("login", "")
            ...
            update_unified_user_info(login, m["open_id"], nick, gitcode_nickname=nick, app_id=ctx.app_id)
```

**问题**：`update_unified_user_info` 第一个参数写进 `gitcode_user` 列。GitHub 协作者 `collab["login"]` 是 GitHub 真名，写进 `gitcode_user` 列错误。

**修复方案**：按 provider 分流。GitHub 仓库的协作者不能直接调 `update_unified_user_info`——需要先从 GitHub login 反查 `grape_user_id`，再调 `bind_feishu_identity` 只写飞书三列。

```python
        provider, _, _ = parse_repo_key(repo_key)  # 已有 L730
        for collab in collabs:
            nick = collab.get("nick_name", "") or collab.get("login", "")
            login = collab.get("username", "") or collab.get("login", "")
            if not nick or not login:
                continue
            for m in members:
                if m.get("name") == nick:
                    if provider == "github":
                        # FR-21.2: GitHub 协作者 login 是 GitHub 真名，不能写 gitcode_user 列
                        # 先从 external_identities 查 grape_user_id，再只写飞书三列
                        _uid = _resolve_uid_by_platform_login(login, "github")
                        if _uid:
                            from src.session_manager import bind_feishu_identity
                            bind_feishu_identity(_uid, m["open_id"], feishu_name=nick, app_id=ctx.app_id)
                            matched += 1
                    else:
                        # GitCode 协作者 username 就是平台 login，写 gitcode_user 列正确
                        update_unified_user_info(login, m["open_id"], nick, gitcode_nickname=nick, app_id=ctx.app_id)
                        matched += 1
                    break
```

**新增辅助函数** `_resolve_uid_by_platform_login`（在 feishu_async.py 中）：
```python
def _resolve_uid_by_platform_login(login: str, provider: str) -> int:
    """从 external_identities 按 platform login 查 grape_user_id。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT ei.grape_user_id FROM external_identities ei "
            "WHERE ei.provider=? AND ei.external_login=? AND ei.status=1 LIMIT 1",
            (provider, login)).fetchone()
        return row[0] if row else 0
```

**行为等价性**：
- GitCode 仓库：维持原逻辑（`update_unified_user_info` 写 `gitcode_user` 列）——等价 ✓
- GitHub 仓库：从"写错列"改为"查 user_id + 只写飞书三列"——修正了错误行为 ✓
- GitHub 协作者未绑定 grape 账号时：`_resolve_uid_by_platform_login` 返回 0 → 跳过（不写映射）——比原行为更好（原行为写错列比不写更糟）

### 16.3 FR-21.3：探活 GitHub-only 触发（P1）

**文件**：src/api/routes/repos.py:434-449

**当前代码**：
```python
    if "gitcode" not in providers:
        return  # 探活目前只走 gitcode token（GitHub 仓在函数内部按需解析）
```

**修复**：
```python
    if "gitcode" not in providers and "github" not in providers:
        return  # 无任何平台 token，不探活
```

**token 获取**：当前 `with_oauth_token(user, "gitcode", lambda t: t)` 硬编码 gitcode。改为按用户主 provider 取 token——如果用户有 gitcode token 用 gitcode，否则用 github。

```python
    _probe_provider = "gitcode" if "gitcode" in providers else "github"
    api_token = with_oauth_token(user, _probe_provider, lambda t: t) or ""
```

**行为等价性**：
- 有 GitCode 的用户：`_probe_provider = "gitcode"`，行为不变 ✓
- 只有 GitHub 的用户：原来 return（不探活），现在用 github token 探活——新增能力 ✓

### 16.4 FR-21.4：settings.py GET GitHub 仓库配置（P1）

**文件**：src/api/routes/settings.py:558-598

**当前代码**：GET endpoint 硬编码 `bot_gitcode_token`：
```python
        c.execute("SELECT bot_gitcode_token, bot_username, ... FROM managed_repos WHERE repo_key=?", ...)
        row = c.fetchone()
        ...
        "has_bot_token": bool(row and row[0]),  # ← 恒查 bot_gitcode_token
```

**修复**：按 repo_key 前缀选列：
```python
        from src.repo_key import parse_repo_key
        _provider, _, _ = parse_repo_key(repo_key)
        _token_col = "bot_github_token" if _provider == "github" else "bot_gitcode_token"
        c.execute(f"SELECT {_token_col}, bot_username, ... FROM managed_repos WHERE repo_key=?", ...)
```

**行为等价性**：
- GitCode 仓库：查 `bot_gitcode_token`，行为不变 ✓
- GitHub 仓库：从"恒查 bot_gitcode_token（恒 false）"改为查 `bot_github_token`——修正 ✓

### 16.5 FR-21.5：repo_cleanup.py _normalize_keys 通用化（P1）

**文件**：src/repo_cleanup.py:55-64

**当前代码**：只剥离 `gitcode:` 前缀。

**修复**：用 `parse_repo_key` 剥离任意 provider 前缀：
```python
def _normalize_keys(repo_key: str) -> list:
    from src.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    base = f"{owner}/{repo}" if owner and repo else repo_key
    return list(set([repo_key, f"{provider}:{base}", base]))
```

**行为等价性**：
- GitCode 仓库：`parse_repo_key("gitcode:owner/repo")` → ("gitcode","owner","repo") → 返回 `["gitcode:owner/repo", "owner/repo"]`——与原行为一致 ✓
- GitHub 仓库：`parse_repo_key("github:owner/repo")` → ("github","owner","repo") → 返回 `["github:owner/repo", "owner/repo"]`——新增 ✓

### 16.6 验证方法

1. FR-21.1：`python3 -c "from src.api.routes.repos import _repo_platform_token"` 不报 NameError
2. FR-21.2：mock `external_identities` 表 + `sync_repo_feishu_mapping` 对 GitHub 仓库调用 → 验证飞书 open_id 写入 bind_feishu_identity 而非 update_unified_user_info
3. FR-21.3：mock 只绑 GitHub 的用户 → 验证 `_start_stale_probe_async` 不 return
4. FR-21.4：GET `/settings/gitcode-repo/` 对 GitHub 仓库 → has_bot_token 正确
5. FR-21.5：`_normalize_keys("github:owner/repo")` 返回含 `["github:owner/repo", "owner/repo"]`
6. 全量测试通过

### 16.7 FR-21.7：unbind_provider 清 um 平台列（P1）

**文件**：src/api/routes/grape_auth.py:1046（delete_external_identity 之后）

**当前代码**：unbind_provider 删除 external_identities + user_repos + user_credentials，但不清 um 平台列。

**修复**：在 delete_external_identity 之后、清理 user_repos 之前，追加清列：
```python
    # FR-21.7: 清 um 平台列——解绑后 um.gitcode_user/github_login 残留会导致飞书反查命中旧身份
    from src.repo_key import um_column_for_provider
    _um_col = um_column_for_provider(provider)
    from src.session_manager import db as _sm_db, db_lock as _sm_dl
    with _sm_dl:
        _c = _sm_db.cursor()
        _c.execute(f"UPDATE unified_user_mapping SET {_um_col}='' WHERE user_id=?", (user["id"],))
        _sm_db.commit()
```

**行为等价性**：
- GitCode 解绑：清 um.gitcode_user=''，external_identities 中 gitcode 行已删。原来不清 → 飞书反查仍命中。修正 ✓
- GitHub 解绑：清 um.github_login=''，external_identities 中 github 行已删。原来不清 → 同上。修正 ✓
- 不碰飞书三列（feishu_open_id/feishu_name/app_id）——解绑平台不该清飞书绑定 ✓

### 16.8 FR-21.8：ensure_default_agent_sessions 按 provider 写 token 列（P1）

**文件**：src/api/session_store.py:547-572

**当前代码**：`upsert_agent_session(repo_gitcode_token=_token, ...)` 硬编码 `repo_gitcode_token` 参数。

**修复**：按 repo_key 的 provider 选列名，动态传参：
```python
    from src.repo_key import parse_repo_key
    _provider_name, _, _ = parse_repo_key(repo_key)
    _token_kwarg = "repo_github_token" if _provider_name == "github" else "repo_gitcode_token"
    ...
    upsert_agent_session(
        repo_key=repo_key, agent_type=_at,
        runtime_id=_rid, skill=_sk, skill_id=_skill_id,
        status="idle",
        enable=None, subscribe_events=None,
        im_app_id=None, im_app_secret=None, im_provider=None,
        **{_token_kwarg: _token},
    )
```

**注意**：`upsert_agent_session` 的参数 `repo_gitcode_token` 有默认值 `""`，GitHub 仓库传 `repo_github_token=token` 时 `repo_gitcode_token` 取默认空串。需要确认 `upsert_agent_session` 的 UPDATE 语句处理——当前 UPDATE 写 `repo_gitcode_token=?`，GitHub 仓库需要写 `repo_github_token=?`。

**深入分析**：`upsert_agent_session` 的 UPDATE（L415-417）硬编码 `repo_gitcode_token=?`。仅改调用方不够——函数内部也要按 provider 选列。但改 `upsert_agent_session` 签名影响面大（所有调用方）。

**替代方案**（更安全）：`ensure_default_agent_sessions` 不改 `upsert_agent_session`，而是在调用后单独 UPDATE GitHub 仓库的 token 列：
```python
    if _token and _provider_name == "github":
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE devops_agent_session SET repo_github_token=? WHERE repo_key=? AND agent_type=?",
                      (_token, repo_key, _at))
            db.commit()
```

**采用替代方案**——不改变 `upsert_agent_session` 签名和内部逻辑，只在 GitHub 仓库时追加一次 UPDATE。

**行为等价性**：
- GitCode 仓库：维持原逻辑（`upsert_agent_session(repo_gitcode_token=_token)`），行为不变 ✓
- GitHub 仓库：原来 token 不写入 → 追加 UPDATE 写入 `repo_github_token` 列 ✓

### 16.9 FR-21.9：agents API 按 provider 查 token 列（P1）

**文件**：src/api/routes/agents.py:198-223

**当前代码**：
```python
c.execute("SELECT id, runtime_id, skill, skill_id, repo_gitcode_token, ... FROM devops_agent_session WHERE repo_key IN ...")
...
"repo_gitcode_token_exists": bool(row[4]),
```

**修复**：按 repo_key 前缀选列。由于 SQL 的列名不能参数化，用 f-string（列名来自受控枚举无注入面）：
```python
    from src.repo_key import parse_repo_key
    # agents API 可能查多个 repo_key，取第一个的 provider（同一请求通常同 provider）
    _provider = "gitcode"
    if _repo_keys:
        _provider, _, _ = parse_repo_key(_repo_keys[0])
    _token_col = "repo_github_token" if _provider == "github" else "repo_gitcode_token"
    c.execute(f"SELECT id, runtime_id, skill, skill_id, {_token_col}, ... FROM devops_agent_session WHERE ...")
    ...
    "repo_token_exists": bool(row[4]),
```

**注意**：字段名从 `repo_gitcode_token_exists` 改为 `repo_token_exists`——需确认前端消费方。grep 确认前端零消费此字段（R25.8 已脱敏为布尔，前端无引用）。

**行为等价性**：
- GitCode 仓库：查 `repo_gitcode_token`，返回 `repo_token_exists`（字段名变了但前端零消费）✓
- GitHub 仓库：查 `repo_github_token`，返回 `repo_token_exists=true` ✓

### 16.10 FR-21.10：_purge_chat_im_binding 按 user_id 清飞书映射（P1）

**文件**：src/api/routes/chat.py:1348

**当前代码**：
```python
            elif gitcode_user:
                c.execute("UPDATE unified_user_mapping SET feishu_open_id='', feishu_name='', app_id='', updated_at=? WHERE gitcode_user=?",
                          (_now, gitcode_user))
```

**问题分析**：
- 参数 `gitcode_user` 实际值是 `user.get("username", "")`（grape_accounts 注册名），不是平台 login
- 原代码用注册名扫 `gitcode_user` 列——存量 um 行可能存注册名（旧 backfill）也可能存平台真 login（新 backfill），注册名不一定匹配
- 用 `OR github_login=?` 会误清同名 GitHub 用户的飞书映射——注册名恰好等于某 GitHub 用户真 login 时命中错误行
- 函数已有 `user_id` 参数——应按 `user_id` 精确匹配，无歧义

**修复**：改为按 `user_id` 清当前用户的飞书三列：
```python
            elif gitcode_user:
                c.execute("UPDATE unified_user_mapping SET feishu_open_id='', feishu_name='', app_id='', updated_at=? WHERE user_id=?",
                          (_now, user_id))
```

**行为等价性**：
- GitCode 用户：原代码按 `gitcode_user=注册名` 查 → 可能命中也可能不命中（取决于存量数据）。新代码按 `user_id` 精确命中 → 一定清到正确行。修正 ✓
- GitHub 用户：原代码按 `gitcode_user=注册名` 查 → gitcode_user 列不存 GitHub 注册名 → 不命中 → 飞书映射不被清。新代码按 `user_id` 命中 → 正确清除。修正 ✓
- 撞名场景：原代码 `OR github_login=?` 会误清同名行。新代码按 `user_id` 精确匹配 → 无误清。修正 ✓
- 语义：`_purge_chat_im_binding` 的意图是"清当前用户的飞书映射"——按 user_id 是最精确的语义对齐 ✓

### 16.11 验证方法（补充）

7. FR-21.7：mock um 表 + unbind_provider → 验证 um 平台列被清空
8. FR-21.8：GitHub 仓库 ensure_default_agent_sessions → 验证 repo_github_token 列非空
9. FR-21.9：GET /agents?repo_key=github:xxx → repo_token_exists=true
10. FR-21.10：GitHub 用户 _purge_chat_im_binding → feishu_open_id 被清空

---

## 17. FR-22：兼容性修复（深度审计发现）

### 17.1 FR-22.1：cooldown.py DELETE feishu_onboard_task 缺少参数（P0）

**文件**：src/admin/cooldown.py L121

**现状**：
```python
c.execute("DELETE FROM feishu_onboard_task WHERE user_id=?")
```

**问题**：SQL 有 `?` 占位符但未传参数元组 `(user_id,)`。SQLite 报 `incorrect number of bindings`。

**修复**：
```python
c.execute("DELETE FROM feishu_onboard_task WHERE user_id=?", (user_id,))
```

**行为等价性**：与原基线代码一致（原代码就有参数），是本分支改动时遗漏。修正 ✓

### 17.2 FR-22.2：issues.py 权限收窄 role 字段缺失（P1）

**文件**：src/api/routes/issues.py L655/804/858

**现状**：
```python
c.execute("SELECT ... FROM issue WHERE id=? AND (user_id=? OR (user_id=0 AND ?='admin'))",
          (issue_id, user["id"], user.get("role", "")))
```

**问题**：`get_current_user` 返回的 dict 没有 `role` 字段，`user.get("role", "")` 永远是空串。`user_id=0 AND ''='admin'` 永远 false，非创建者对 user_id=0 的 issue 操作全部 404。

**场景**：
- issue.user_id=0 表示创建者未注册平台或 webhook 自动创建的 issue
- 修复前（基线）：任何登录用户都能操作 user_id=0 的 issue（`user_id=? OR user_id=0`）
- 我们改成 `user_id=? OR (user_id=0 AND role='admin')` 想收窄到 admin，但 role 字段不存在

**修复**：回退到原行为 `user_id=? OR user_id=0`。权限收窄需要先在 get_current_user 补 role 字段，不在本次范围。

```python
# 三处统一回退
c.execute("SELECT ... FROM issue WHERE id=? AND (user_id=? OR user_id=0)",
          (issue_id, user["id"]))
```

**行为等价性**：与基线一致。权限收窄作为独立 FR 另行设计（需先补 get_current_user 的 role 字段）。修正 ✓

### 17.3 FR-22.3：engine_select EXISTS 大小写不匹配（P1）

**文件**：src/runtime/engine_select.py L230/L243

**现状**：
```python
# 二级回退
"AND EXISTS (SELECT 1 FROM external_identities ei "
"WHERE ei.grape_user_id=ga.id AND ei.provider=? AND ei.status=1 "
"AND ei.external_login=LOWER(?) AND ei.external_login!='') "

# user_repos 回退
"AND EXISTS (SELECT 1 FROM external_identities ei "
"WHERE ei.grape_user_id=ga.id AND ei.provider=? AND ei.status=1 "
"AND ei.external_login=LOWER(?) AND ei.external_login!='') "
```

**问题**：`ei.external_login=LOWER(?)` 只对输入做 LOWER，但 external_login 列存储时未做 LOWER。大小写不匹配。

**场景**：
- issue_creator = "user_d"（含大写）
- external_identities.external_login = "user_d"（原值存储）
- 查询条件：`ei.external_login = LOWER('user_d')` = `'jason7602'` ≠ `'user_d'` → 不匹配
- 一级查询（um）未命中时，二级回退也失败 → user_id=0 → issue 归属错误

**修复**：两侧都 LOWER：
```python
"AND LOWER(ei.external_login)=LOWER(?) AND ei.external_login!='') "
```

**行为等价性**：
- 大写 login "user_d"：`LOWER('user_d')=LOWER('user_d')` → `'jason7602'='jason7602'` → 匹配 ✓
- 小写 login "jason"：`LOWER('jason')=LOWER('jason')` → 匹配 ✓
- 混合大小写存储 "JaSoN"：`LOWER('JaSoN')=LOWER('JaSoN')` → 匹配 ✓
- 不影响 MySQL（LOWER 是标准 SQL 函数）✓

### 17.4 FR-22.4：_is_constraint_error SQLite 不匹配（P2）

**文件**：src/session_manager.py `_is_constraint_error`

**现状**：
```python
def _is_constraint_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return 'integrity' in msg or 'duplicate' in msg or '1062' in str(exc)
```

**问题**：SQLite 报 `UNIQUE constraint failed: test.code`，消息含 'constraint' 不含 'integrity'/'duplicate'/'1062'。`_is_constraint_error` 返回 False，异常被 re-raise。

**场景**：
- save_binding_ticket 生成同一 ticket code（10s TTL 内极低概率）
- SQLite 报 `sqlite3.IntegrityError: UNIQUE constraint failed: binding_tickets.ticket`
- `_is_constraint_error` 检查：'integrity' not in 'unique constraint failed' → False
- 异常 re-raise → 票据保存失败

**修复**：
```python
def _is_constraint_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return 'integrity' in msg or 'constraint' in msg or 'duplicate' in msg or '1062' in str(exc)
```

**行为等价性**：
- SQLite `UNIQUE constraint failed`：含 'constraint' → True ✓
- SQLite `NOT NULL constraint failed`：含 'constraint' → True（也属于约束冲突，重试合理）✓
- MySQL `Duplicate entry`：含 'duplicate' → True ✓
- MySQL `1062`：含 '1062' → True ✓
- 非约束异常（如网络错误）：不含上述关键词 → False → re-raise ✓

### 17.5 FR-22.5：get_feishu_open_id username vs platform login（P2）

**文件**：src/session_manager.py `get_feishu_open_id`

**现状**：handler_async 用 `grape_accounts.username`（注册名）调 `get_feishu_open_id(username, app_id)`。改动后 WHERE 只查 `gitcode_user=?`。如果注册名 != 平台 login 且未触发 FR-18.6 同步，查不到 feishu_open_id。

**场景**：
- 用户注册名 = "user_b"，平台 login = "login_b"
- FR-18.4 backfill 后 um.gitcode_user = "login_b"（从 external_identities 填充）
- handler_async 调 get_feishu_open_id("user_b", app_id="xxx")
- WHERE gitcode_user="user_b" → 不匹配 "login_b" → 查不到 → 飞书通知无法送达

**修复**：get_feishu_open_id 增加 user_id 回退——gitcode_user 和 github_login 都查不到时，从 grape_accounts 查 username 对应的 user_id，再按 user_id 查 um 行的 feishu_open_id。

```python
def get_feishu_open_id(gitcode_user, app_id=""):
    if not gitcode_user:
        return None
    with db_lock:
        c = db.cursor()
        # 1. 先查 gitcode_user 列
        if app_id:
            c.execute("SELECT feishu_open_id FROM unified_user_mapping WHERE gitcode_user=? AND app_id=?", (gitcode_user, app_id))
            row = c.fetchone()
            if not row or not row[0]:
                # 2. 回退查 github_login 列
                c.execute("SELECT feishu_open_id FROM unified_user_mapping WHERE github_login=? AND app_id=?", (gitcode_user, app_id))
                row = c.fetchone()
        else:
            c.execute("SELECT feishu_open_id FROM unified_user_mapping WHERE gitcode_user=?", (gitcode_user,))
            row = c.fetchone()
            if not row or not row[0]:
                c.execute("SELECT feishu_open_id FROM unified_user_mapping WHERE github_login=?", (gitcode_user,))
                row = c.fetchone()
        if row and row[0]:
            return row[0]
        # 3. FR-22.5: 注册名回退——从 grape_accounts 查 user_id，再按 user_id 查 um
        try:
            c.execute("SELECT id FROM grape_accounts WHERE LOWER(username)=LOWER(?) AND status=1 LIMIT 1", (gitcode_user,))
            ga_row = c.fetchone()
            if ga_row:
                if app_id:
                    c.execute("SELECT feishu_open_id FROM unified_user_mapping WHERE user_id=? AND app_id=?", (ga_row[0], app_id))
                else:
                    c.execute("SELECT feishu_open_id FROM unified_user_mapping WHERE user_id=?", (ga_row[0],))
                row = c.fetchone()
                return row[0] if row else None
        except Exception:
            pass
        return None
```

**行为等价性**：
- 注册名 = 平台 login "user_c"：步骤 1 命中 → 返回 open_id ✓（回归）
- 注册名 "user_b" != 平台 login "login_b"：步骤 1/2 未命中 → 步骤 3 按 username 查到 user_id → 按 user_id 查到 open_id ✓
- 未注册用户 "unknown"：三个步骤都未命中 → 返回 None ✓
- 不影响 app_id 过滤逻辑 ✓

### 17.6 验证方法（FR-22 补充）

11. FR-22.1：调用 _anonymize_account(user_id) → 验证不报错，feishu_onboard_task 行被删除
12. FR-22.2：issue.user_id=0 + 非创建者 → POST /issues/{id}/confirm_done → 200
13. FR-22.3：issue_creator="user_d" + um 未命中 → _repo_user_id 返回正确 user_id
14. FR-22.4：save_binding_ticket 同 code 二次调用 → 冲突重试成功
15. FR-22.5：注册名 != 平台 login → get_feishu_open_id(注册名) → user_id 回退命中

---

## 18. FR-23：webhook_id 空串写 MySQL 整型列导致 UPDATE 回滚（现网实测发现）

### 18.1 FR-23.1：_update_repo_bot_token webhook_id 空串 MySQL 不兼容（P0）

**文件**：src/user/db.py L2358-2391

**现状**：
```python
def _update_repo_bot_token(repo_key: str, bot_token: str, provider: str = "gitcode",
                           webhook_token: str = "", webhook_id: str = ""):
    ...
    c.execute(
        "UPDATE managed_repos SET bot_gitcode_token=?, host_user_id=0, webhook_secret='', "
        "bot_token_status='', gitcode_webhook_token=?, webhook_id=? WHERE repo_key=?",
        (bot_token, webhook_token or bot_token, str(webhook_id) if webhook_id else "", repo_key),
    )
```

**问题链路**：
1. 用户在设置页保存 Bot Token
2. `update_managed_repo_bot_token` 调用 `_rebuild_webhook_with_bot_token`
3. Bot Token 对仓库无 webhook 管理权限 → 删旧 webhook 403 → 返回空串 `""`
4. `_update_repo_bot_token(webhook_id="")` 执行
5. `str(webhook_id) if webhook_id else ""` → 写入空串到 `webhook_id INTEGER DEFAULT 0` 列
6. SQLite：类型亲和性隐式转 0，不报错（但语义不准确）
7. MySQL：严格类型检查 → `(1366, "Incorrect integer value: '' for column 'webhook_id' at row 1")` → UPDATE 回滚 → bot token 静默丢失

**场景**：
- MySQL 生产环境，用户保存 Bot Token
- Bot Token 有效（verify_token 通过）但对该仓库无 webhook 管理权限
- 删旧 webhook 返回 403 → 重建中止 → webhook_id 为空串
- MySQL 拒绝空串写入整型列 → 整个 UPDATE 回滚
- managed_repos.bot_gitcode_token 仍为空，host_user_id 不变
- 仓库卡在路径 B，用户以为已切换到路径 A

**修复**：`_update_repo_bot_token` 中 webhook_id 写入前统一转整型，空串/空值写 0：

```python
def _update_repo_bot_token(repo_key: str, bot_token: str, provider: str = "gitcode",
                           webhook_token: str = "", webhook_id: str = ""):
    ...
    # FR-23.1: webhook_id 列是 INTEGER DEFAULT 0，空串写 MySQL 整型列报 1366
    # 统一转 int：空串/None/0 → 0（无 webhook），非零字符串 → int
    _wh_id = 0
    if webhook_id:
        try:
            _wh_id = int(webhook_id)
        except (ValueError, TypeError):
            _wh_id = 0
    ...
    c.execute(
        "UPDATE managed_repos SET bot_gitcode_token=?, host_user_id=0, webhook_secret='', "
        "bot_token_status='', gitcode_webhook_token=?, webhook_id=? WHERE repo_key=?",
        (bot_token, webhook_token or bot_token, _wh_id, repo_key),
    )
```

**行为等价性**：
- webhook 重建成功返回 "12345"：`int("12345")` = 12345，写 12345 ✓
- webhook 重建失败返回 ""：空串 → `_wh_id = 0`，写 0 ✓
- webhook_id 为 None：`if webhook_id` 为 False → `_wh_id = 0`，写 0 ✓
- webhook_id 为 0：`if webhook_id` 为 False（0 是 falsy）→ `_wh_id = 0`，写 0 ✓
- SQLite 回归：原来写空串被隐式转 0，现在显式写 0，行为一致 ✓
- MySQL 修复：写 int 而非 str，不再触发 1366 ✓

**影响范围**：3 处调用 `_update_repo_bot_token` 的路径全部受益：
- update_managed_repo_bot_token（L1774）：设置页保存 Bot Token
- auto_upgrade_path_b_repos 重建失败路径（L2472）：路径 B → A 升级
- auto_upgrade_path_b_repos 重建成功路径（L2478）：路径 B → A 升级

**注意**：此 bug 是基线 pre-existing（weekly-08 17509551a 中完全一样），非本分支引入。但本分支的 FR-20.11 新增了 warning 返回，使"webhook 重建失败"从静默变为可见——反而帮助发现了这个 pre-existing bug。

### 18.2 验证方法（FR-23 补充）

16. FR-23.1 P1：MySQL 后端 + Bot Token 无 webhook 权限 → 保存 Bot Token → webhook_id=0 写入成功，bot_gitcode_token 写入成功，返回 warning
17. FR-23.1 P2：MySQL 后端 + Bot Token 有 webhook 权限 → 保存 Bot Token → webhook_id=新ID 写入成功
18. FR-23.1 P4：SQLite 后端 → 同 P1 → 行为不变（写 0 替代隐式转 0）
19. FR-23.1 P5：webhook_id="12345" → int("12345")=12345 → 写 12345

---

## 19. FR-24：解除托管校验用注册名而非平台 login（现网实测发现）

### 19.1 FR-24.1：_check_unmanage_vcs_permission 注册名当平台 login（P0）

**文件**：src/api/routes/repos.py L1810-1870

**现状**：
```python
def _check_unmanage_vcs_permission(user: dict, owner: str, repo_name: str, repo_provider: str) -> None:
    username = (user.get("username") or "").strip()
    ...
    # 仓库所有者天然有完全权限
    if username == owner:
        return
    ...
    # GitCode
    gc_token = get_user_token_with_refresh(username, user.get("id", 0))
    data = get_member_permission(gc_token, owner, repo_name, username)
    # GitHub
    gh_token = get_user_github_token_with_refresh(username, user.get("id", 0))
    role = get_github_collaborator_permission(gh_token, owner, repo_name, username)
```

**问题**：`username` 是 grape 注册名，不是 VCS 平台 login。三处使用点都拿注册名当平台 login：
1. `if username == owner`：注册名 "user_a" != owner "login_a" → 捷径不走
2. `get_member_permission(token, owner, repo, username)`：GET /collaborators/user_a/permission → 404
3. `get_github_collaborator_permission(token, owner, repo, username)`：同理 404

**场景**：
- 用户 grape 注册名 = "user_a"，GitCode 平台 login = "login_a"
- 仓库 owner = "login_a"（用户自己的仓库）
- 解除托管时：username "user_a" != owner "login_a" → 查 GitCode API → 404 → 403 拒绝
- 用户被锁在自己的仓库之外

**对比：托管侧（host_repo）已修**：
```python
# host_repo 中的正确做法（repos.py:1135）
def _get_user(token):
    return _req("GET", f"https://gitcode.com/api/v5/user", token)
user_data = with_oauth_token(user, "gitcode", _get_user)
current_user = user_data.get("login", "")  # 真实平台 login
if current_user == owner:
    is_admin = True
```

**修复**：对齐 host_repo 模式——用 `with_oauth_token` + VCS `/user` 接口获取真实平台 login，再用 login 做 owner 比较和协作者权限查询。

```python
def _check_unmanage_vcs_permission(user: dict, owner: str, repo_name: str, repo_provider: str) -> None:
    # FR-24.1: 用 VCS /user 接口获取真实平台 login，不用 grape 注册名
    # 对齐 host_repo 的 _get_user 模式
    if repo_provider == "github":
        def _get_gh_user(token):
            from src.user.repo_manager import _github_req
            return _github_req("GET", f"{_GITHUB_API_BASE}/user", token)
        try:
            user_data = with_oauth_token(user, "github", _get_gh_user)
        except HTTPException:
            raise  # token 过期/无授权
        vcs_login = (user_data or {}).get("login", "")
        if not vcs_login:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户 GitHub 身份"})
        if vcs_login == owner:
            return
        # 用 vcs_login 查协作者权限
        from src.api.deps import get_user_github_token_with_refresh
        gh_token = get_user_github_token_with_refresh(user["username"], user_id=user.get("id", 0))
        from src.user.repo_manager import get_github_collaborator_permission
        role = (get_github_collaborator_permission(gh_token, owner, repo_name, vcs_login) or "").lower()
        ...
    else:
        # GitCode
        def _get_gc_user(token):
            from src.user.repo_manager import _req
            return _req("GET", f"https://gitcode.com/api/v5/user", token)
        try:
            user_data = with_oauth_token(user, "gitcode", _get_gc_user)
        except HTTPException:
            raise
        vcs_login = (user_data or {}).get("login", "")
        if not vcs_login:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户 GitCode 身份"})
        if vcs_login == owner:
            return
        # 用 vcs_login 查协作者权限
        from src.api.deps import get_user_token_with_refresh
        gc_token = get_user_token_with_refresh(user["username"], user_id=user.get("id", 0))
        from src.user.repo_manager import get_member_permission, _RateLimitedError
        data = get_member_permission(gc_token, owner, repo_name, vcs_login) or {}
        ...
```

**行为等价性**：
- 注册名 == 平台 login "user_c"：`with_oauth_token` 拿 login "user_c" → login==owner → 通过 ✓（回归）
- 注册名 "user_a" != 平台 login "login_a"：`with_oauth_token` 拿 login "login_a" → login==owner → 通过 ✓
- 非所有者 maintainer：login "login_a" → 查 /collaborators/login_a/permission → access_level=40 → 通过 ✓
- 非所有者 developer：login "login_a" → 查 → access_level=30 → 403 拒绝 ✓
- token 过期：`with_oauth_token` 触发刷新，刷新失败 → 401 "授权已过期" ✓
- 无平台授权：`with_oauth_token` 无 token → 401 ✓
- GitHub 仓库：`with_oauth_token(user, "github", ...)` → 拿 GitHub login → 同流程 ✓

**注意**：此 bug 是基线 pre-existing（weekly-08 17509551a 中完全一样），非本分支引入。但与本分支 FR-21 修的是同一类根因（平台身份应从 external_identities 或 VCS /user 接口解析）。

### 19.2 验证方法（FR-24 补充）

20. FR-24.1 Q1：注册名≠login + owner → POST /repos/remove → with_oauth_token 拿 login → login==owner → 通过
21. FR-24.1 Q2：注册名≠login + maintainer → 查 /collaborators/{login}/permission → access_level=40 → 通过
22. FR-24.1 Q4：注册名==login 回归 → with_oauth_token 拿 login → login==owner → 通过
23. FR-24.1 Q5：GitHub 仓库 → with_oauth_token(user, "github", ...) → 拿 GitHub login → 通过
24. FR-24.1 Q7：token 过期 → with_oauth_token 刷新失败 → 401

---

## 20. FR-25：session_store issue 归属 EXISTS 守卫注册名误判（全仓扫描发现）

### 20.1 FR-25.1：session_store EXISTS 守卫 ga.username vs m.gitcode_user（P1）

**文件**：src/api/session_store.py L130

**现状**：
```python
_usr_expr = (f"COALESCE((SELECT m.user_id FROM unified_user_mapping m "
             f"WHERE m.{_col} = ? AND m.user_id > 0 "
             f"AND EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id = m.user_id "
             f"            AND (ga.username = m.gitcode_user OR ga.username = m.github_login)) "
             f"ORDER BY m.user_id LIMIT 1), 0)")
```

**问题**：EXISTS 守卫比较 `ga.username = m.gitcode_user`，但 FR-18.4 后 `m.gitcode_user` 存平台 login（从 external_identities 回填），`ga.username` 存注册名。注册名 != 平台 login 时恒假。

**场景**：
- 用户注册名 "user_a"，GitCode login "login_a"
- FR-18.4 backfill 后 um.gitcode_user = "login_a"（平台 login）
- grape_accounts.username = "user_a"（注册名）
- EXISTS：`ga.username = m.gitcode_user` → `"user_a" = "login_a"` → False
- COALESCE 回 0 → issue.user_id = 0
- 需求待办、通知归属全断

**对比 issues_center._get_user_id_by_gitcode**：
```python
# 一级查询（带 EXISTS，同样有此 bug）
row = c.execute("SELECT um.user_id ... WHERE um.{_col}=? ... EXISTS (... ga.username=um.gitcode_user ...)").fetchone()
if not row:
    # 二级回退（不带 EXISTS，能查到）
    row = c.execute("SELECT user_id FROM unified_user_mapping WHERE {_col}=? AND user_id>0 LIMIT 1").fetchone()
```

issues_center 有二级回退所以能查到 user_id，但 session_store 没有回退——单查无兜底，是真缺陷。

**修复**：EXISTS 守卫改为 `ga.id = m.user_id AND ga.status = 1`，不再比较 username 和平台 login。

```python
_usr_expr = (f"COALESCE((SELECT m.user_id FROM unified_user_mapping m "
             f"WHERE m.{_col} = ? AND m.user_id > 0 "
             f"AND EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id = m.user_id AND ga.status = 1) "
             f"ORDER BY m.user_id LIMIT 1), 0)")
```

**行为等价性**：
- 注册名 "user_a" != login "login_a"：`ga.id = m.user_id AND ga.status = 1` → 只验证账号存在且未禁用 → 命中 ✓
- 注册名 == login "user_c"：同上 → 命中 ✓（回归）
- 禁用账号（ga.status=0）：`ga.status = 1` 不命中 → user_id=0 ✓
- um 无匹配行：COALESCE 回 0 → user_id=0 ✓（回归）
- 伪身份防护：FR-18.4 已从源头解决（backfill 从 external_identities 回填而非 grape_accounts.username）✓

**注意**：issues_center._get_user_id_by_gitcode 也有同样的 EXISTS 比较，但有二级回退兜底所以不会导致 user_id=0。为保持一致性，issues_center 的 EXISTS 也应同步修改（但优先级低，因为回退已兜底）。

### 20.2 验证方法（FR-25 补充）

25. FR-25.1 R1：注册名≠login + issue 建档 → EXISTS ga.status=1 命中 → user_id=正确值
26. FR-25.1 R2：注册名==login 回归 → 同上 → user_id=正确值
27. FR-25.1 R3：禁用账号 → ga.status=1 不命中 → user_id=0
28. FR-25.1 R5：GitHub 仓 → 按 github_login 列查 → EXISTS 命中 → user_id=正确值

### 20.2 FR-25.2：engine_select EXISTS 外层注册名错位键（P1）

**文件**：src/runtime/engine_select.py L234/L258

**现状**（修复前）：
```python
# L2 二级回退
"SELECT ga.id FROM grape_accounts ga "
"WHERE LOWER(ga.username)=LOWER(?) AND ga.id>0 AND ga.status=1 "
"AND EXISTS (SELECT 1 FROM external_identities ei "
"WHERE ei.grape_user_id=ga.id AND ei.provider=? AND ei.status=1 "
"AND LOWER(ei.external_login)=LOWER(?) AND ei.external_login!='')"
# 参数：(issue_creator, _provider, issue_creator)

# L3 user_repos 回退
"SELECT ur.user_id FROM user_repos ur "
"JOIN grape_accounts ga ON ga.id = ur.user_id "
"WHERE LOWER(ga.username)=LOWER(?) AND ga.status=1 "
"AND EXISTS (SELECT 1 FROM external_identities ei ...)"
# 参数：(issue_creator, _provider, issue_creator, repo, ...)
```

**问题**：外层 `LOWER(ga.username)=LOWER(issue_creator)` 用平台 login 匹配注册名。issue_creator 是 VCS 平台 login（如 "login_a"），ga.username 是 grape 注册名（如 "user_a"），两者不等 → 外层匹配失败 → 即使 EXISTS 内层的 external_identities 验证能通过，整个查询也返回空 → user_id=0。

**场景**：
- issue_creator = "login_a"（平台 login）
- grape_accounts.username = "user_a"（注册名）
- external_identities.external_login = "login_a"（平台 login）
- 外层：LOWER("user_a") = LOWER("login_a") → False → 查询失败
- EXISTS 内层本可以匹配，但外层先过滤掉了

**修复**：去掉外层的 `LOWER(ga.username)=LOWER(?) AND`，只保留 `ga.id>0 AND ga.status=1 AND EXISTS(...)`。EXISTS 已经用 `LOWER(ei.external_login)=LOWER(?)` 验证了平台 login 匹配，不需要外层再用注册名匹配。参数同步减少一个 issue_creator。

**行为等价性**：
- 注册名 == 平台 login "user_a"：外层去掉后，EXISTS 仍匹配 → 返回 user_id ✓（回归）
- 注册名 "user_a" != 平台 login "login_a"：外层去掉后，EXISTS 匹配 external_login="login_a" → 返回 user_id ✓
- 无 external_identities 记录：EXISTS 不命中 → user_id=0 ✓
- 禁用账号：ga.status=1 不命中 → user_id=0 ✓

### 20.3 验证方法（FR-25 补充）

29. FR-25.2 L2 回退：注册名≠login + um 未命中 → EXISTS 匹配 external_login → 返回 user_id
30. FR-25.2 L3 回退：注册名≠login + um 未命中 + L2 未命中 → user_repos JOIN + EXISTS → 返回 user_id
31. FR-25.2 回归：注册名==login → EXISTS 仍匹配 → 返回 user_id

---

## 21. FR-26：模板 onboarding owner 优先级反转（全仓扫描发现）

### 21.1 FR-26.1：templates.py owner 优先用注册名而非平台 login（P1）

**文件**：src/api/routes/templates.py L277

**现状**（修复前）：
```python
owner = user.get("username", "") or owner_login
```

**问题**：`user.get("username")` 是 grape 注册名，`owner_login` 是 VCS 平台 login（从 `get_user_gitcode_login` / `get_user_github_login` 获取）。优先级反了——注册名非空时 `owner_login` 永远不被用到。

**owner 下游使用点**：
1. `trigger_onboarding(repo_key, owner, ...)` → `_create_onboarding_tasks(repo_key, owner, ...)` → `create_standard_task(..., assignee=owner, ...)` → `_post_gitcode_issue(bot_token, owner, rname, ..., assignee=assignee)` — GitCode API 指派 Issue，assignee 需要平台 login
2. `_get_user_id_by_gitcode(owner, repo_key)` — 查 `um.{_col}=?`，um 存的是平台 login（FR-18.4 后从 external_identities 回填）

**场景**：
- 用户注册名 "user_a"，GitCode 平台 login "login_a"
- `owner = "user_a" or "login_a"` → `owner = "user_a"`（注册名优先）
- `create_standard_task(assignee="user_a")` → GitCode API 指派 "user_a" → 平台无此用户 → 指派失败
- `_get_user_id_by_gitcode("user_a", repo_key)` → 查 `um.gitcode_user="user_a"` → um 存的是 "login_a" → 查不到 → user_id=0

**修复**：
```python
owner = owner_login or user.get("username", "")
```

**行为等价性**：
- owner_login 非空（正常路径，L190/198 已赋值且为空时 raise）：用平台 login → 两个下游使用点都正确 ✓
- 注册名 == 平台 login "user_c"：`owner_login = "user_c"` → owner="user_c" → 同 ✓（回归）
- 注册名 "user_a" != 平台 login "login_a"：`owner_login = "login_a"` → owner="login_a" → 指派成功 + user_id 正确 ✓
- GitHub 仓：`owner_login = get_user_github_login(api_token)` → 平台 login ✓

**owner_login 赋值安全性**：`owner_login` 在 L190（GitHub）/L198（GitCode）赋值，为空时 L191/197 raise HTTPException。except HTTPException 也 raise。同名仓库冲突分支（L224）走到时 `owner_login` 已赋值。L211 `full_name = f"{owner_login}/{repo_name}"` 已使用——到 L277 时 `owner_login` 一定已赋值。

### 21.2 验证方法（FR-26 补充）

32. FR-26.1 S1：注册名≠login + 模板创建 → owner=login_a → GitCode 指派成功 + user_id 正确
33. FR-26.1 S2：注册名==login 回归 → owner=login → 同
34. FR-26.1 S3：GitHub 仓 → owner=GitHub login → 指派成功
