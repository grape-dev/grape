# 新增托管失败：全局 Bot 权限校验误判 + Webhook 未创建问题分析

> 现象：新增托管仓库报「全局 Bot（grape_bot）当前为0，未成为仓库维护者（Maintainer）」；
> 且虽然 owner 已邀请 bot、bot 已接受邀请，但 **webhook 始终未被创建**。
> 结论：根因是 GitCode API 429 限流导致权限校验误判，中断了整个托管流程，webhook 逻辑未执行。
> 日期：2026-09-05（生产部署 a4959590 后）

## 一、现象

1. 新增托管仓库时前端报错：「托管失败: 全局 Bot（grape_bot）当前为0，未成为仓库维护者（Maintainer）。请在 GitCode 仓库成员中将 grape_bot 权限提升为维护者后再试」
2. GitCode 侧确认：owner 已成功邀请 grape_bot，且 grape_bot 已接受邀请（成员里已是 Maintainer）
3. 但 **webhook 没有添加上**——仓库收不到任何 GitCode 事件

## 二、日志证据（生产 journal）

```
Sep 06 07:29:52 WARNING "get_member_permission failed: GitCode API GET
  .../repos/Jason7602/hello-calculator-07/collaborators/grape_bot/permission failed (429)
  {...,"error_message":"429 The number of times the user invokes this API is too large.
  Threshold: 50 times per user per Minute. Limited account:Jason7602."}"

Sep 06 07:29:52 WARNING "template host failed (non-fatal): 有空失败: 全局 Bot（grape_bot）当前为0，
  未成为仓库维护者（Maintainer），请在 GitCode 仓库成员中将 grape_bot 权限提升为维护者后再试"
```

（注：日志为中文乱码是终端 GBK 显示问题，实际内容即上述前端报错文案）

## 三、根因分析

### 3.1 代码执行顺序与各步 Token 归属（src/api/routes/repos.py host_repo）

> token 约定：`api_token` = **owner 个人 token**（当前登录用户）；`global_bot_token` =
> **grape_bot 的 token**（configs/global-bot-token.json）。
> **设计原则（需求方明确）**：托管链路中**但凡能查/能建的都用 bot 的 token**。
> owner token 只用于**必需的所有者动作**（L363 身份确认、L387 发起邀请——GitCode 拒绝 bot 用自己 token 邀请自己）。

```
步骤           代码                         使用谁的 token              结果
L363 身份验证   _req(GET /user)             owner 个人 token（必需）    ✓
L387 邀请 bot   add_collaborator(...bot)    owner 个人 token（必需）    ✓ 成功（邀请发出）
L401 bot 接受   accept_invite_for_repo      bot token（global_bot）     ✓ 成功（bot 接受）
L405 权限校验   get_member_permission(...)  ▸ 建议改为 bot token       ✗ 现状用 owner→429→None
L407 读取权限   access_level = 0            -                          = 0  < 40
L409 抛错       raise HTTPException(502)                              ← 在此中断
L411 查 webhook find_existing_webhook(...)  ▸ 建议仅 bot token         ← 未执行
L429 建 webhook setup_webhook(...)          ▸ 建议仅 bot token         ← 未执行（webhook 从未创建）
```

**结论**：webhook 创建逻辑（L411-444）位于**权限二次校验（L405-409）之后**。
429 限流使 `get_member_permission` 返回 `None`（repo_manager.py:196 catch 吞异常返回 None），
`access_level` 退化为 0 < 40 → 提前 raise 502 → 函数中断，**webhook 逻辑被整体跳过**。

> **关键点 1**：`get_member_permission`（L405）应改用 **bot token**——bot 用自己 token 查自己在仓库的权限，
> 既避开 owner 个人 token 的 50 次/分钟限流，又语义更正确（bot 自查自身状态）。
> **关键点 2**：`find_existing_webhook` / `setup_webhook` 应**仅用 bot token**——webhook 归 bot 管理，
> 若 bot token 建不了（如 bot 非维护者），说明 bot 未被成功加入成员，此时 webhook 即使建了也无法被 bot 维护，没有意义，应直接失败提示。

### 3.2 为什么「已邀请已接受」却仍误判

- 现状 `get_member_permission` 用 **owner 的个人 token** 查询 bot 权限，触发 GitCode 每用户每分钟 50 次限流
- 该 owner（Jason7602）在同一分钟内多次执行模板创建 + 手动托管（hello-calculator-07 等），
  加上权限查询、仓库列表等调用，瞬时超过 50 次/分钟
- 429 被当作"查询失败"→ 返回值 None → 语义被曲解为「bot 无维护者权限」
- **若改用 bot token**：bot（grape_bot）自己的调用量低，基本不会触限流，误判可避免

### 3.3 附带风险：webhook token

`L358 webhook_token = api_token or bot_token or global_bot_token`——现状优先个人 token。
即使权限校验通过，个人 token 同样受 50 次/分钟限流约束，`setup_webhook` 也可能 429。
**应改为仅 `global_bot_token`**：webhook 由 bot 管辖，个人 token 限流不再影响托管。

## 四、影响面

- **新建托管仓库全部受影响**：同一个 owner 在一分钟内多次操作必然触发限流
- **间接影响**：重复托管同一仓库（重试）时 `find_existing_webhook` 查不到 → 一直走到 setup_webhook，
  但 setup 前又会被权限校验拦截，死循环报错
- 模板一键创建（create_from_template）里 host 是 non-fatal（`template host failed (non-fatal)`），
  所以模板能建出来但没 webhook；**手动新增托管是 fatal**，直接 502

## 五、修复方向（未改代码，供实现参考）

### 4.x 步骤必要性判定 & Token 归属小结

**各步骤 token 归属（决定性，按「凡能查能建一律 bot token」原则）**：

| 步骤 | 代码 | Token 归属 | 必要性 |
|------|------|-----------|--------|
| 邀请 bot | `add_collaborator` | **owner 个人 token**（必需：GitCode 拒绝 bot 邀请自己） | 必要（授予 maintainer） |
| bot 接受 | `accept_invite_for_repo` | **bot token**（global_bot_token） | 必要 |
| 权限校验①（add 失败兜底） | `get_member_permission`（L391） | **bot token**（bot 自查自身在仓库权限） | 必要（确认 bot 是否已存在当维护者） |
| 权限校验②（accept 成功后） | `get_member_permission`（L405） | **bot token** | **可降级为可选/非致命**（见下） |
| 查/建 webhook | `find_existing_webhook` / `setup_webhook` | **仅 bot token**（`global_bot_token`） | 必要（webhook 归 bot 管辖） |

> **为什么不一律用 owner token**：owner 个人 token 受 GitCode「每用户每分钟 50 次」限流，多人/高频操作必触 429；
> bot（grape_bot）单独一个账号，调用量低，基本不触限流。
> **为什么 webhook 必须用 bot token**：webhook 创建、删除、回调用途全程由 bot 维护。
> 若 bot token 建不了 webhook → 说明 bot 未被成功加入为成员，则 webhook 建了 bot 也无法维护（无意义），应直接失败。

**L405 是否需要的判断（用 bot token 前提下）**：
- `add_collaborator`（L387）返回 True → owner 已授予 bot maintainer；
  再 `accept_invite_for_repo`（L401）返回 True → bot 已接受。**两步成功即强证明 bot 已是维护者**。
- 因此 L405 的二次查询本质是**防呆冗余**，遇到 429/失败**不应阻塞托管**——应降级为「记录日志、继续走 webhook 创建」。
- 只有 `add_collaborator`（L387）返回 **False** 时，才需要 `get_member_permission`（L391）做**硬校验**（区分"已存在"还是"真失败"）——同样用 bot token。

> 结论：**L405 这步可以保留但必须非致命，且用 bot token**；保持硬校验的只有 L387 失败分支的 L391（也改 bot token）。
> **托管链路 owner token 使用面最小化**：仅 L363 身份确认 + L387 发起邀请两处。

### 方案 A（核心）：`repo_manager.get_member_permission` 加限流重试（调用方传 bot token）

```python
def get_member_permission(token, owner, repo, username, _retries=3):
    """token 传全局 bot token（grape_bot 自查自身权限）。"""
    import time
    for i in range(_retries):
        try:
            resp = _req("GET", f"https://api.gitcode.com/api/v5/repos/{owner}/{repo}/collaborators/{username}/permission", token)
            return resp  # 成功
        except Exception as e:
            # 429 或 5xx → 退避重试；403/404（真无权限）→ 直接返回 None
            if getattr(e, "code", None) == 429 or "429" in str(e):
                time.sleep(2 ** i)  # 1s / 2s / 4s
                continue
            log.warning(f"get_member_permission failed: {e}")
            return None
    return None
```

> 区分「限流（可重试）」与「确定性失败（无权限）」：429/网络抖动重试后再判定；403/404 才确认无权限。
> **调用方统一传 `global_bot_token`**：bot 自查自身权限，避开 owner 限流。

### 方案 B（逻辑加固）：`repos.py` 权限校验非致命化（统一 bot token）

由于 `add_collaborator` 成功 + `accept_invite_for_repo` 成功，理论上 bot 已成维护者。
将二次权限校验从「失败即 fatal」改为（**用 bot token 而非 owner token**）：

```python
# 校验失败/限流时降级为警告（邀请链路已成功），不阻塞 webhook 创建
try:
    bot_perm = get_member_permission(global_bot_token, owner, repo_name, global_bot_username)
    access_level = (bot_perm or {}).get("access_level", 0) or 0
    if access_level < 40:
        log.warning(f"host: bot perm check inconclusive (level={access_level}), invite path succeeded, continue")
except Exception as _e:
    log.warning(f"host: bot perm check failed (non-fatal): {_e}")
# 继续执行 webhook 创建（用 bot token）...
```

> 同步修改 L391（add 失败兜底）也传 `global_bot_token`；两处 `get_member_permission` 均不再使用 owner token。

### 方案 C：`setup_webhook` 限流重试 + webhook 改用 bot token

- **webhook token 改为仅 `global_bot_token`**：删除 `webhook_token = api_token or ...`（repos.py:358）
  的个人 token 优先逻辑，统一用 bot token 建/查 webhook。
- `setup_webhook` / `find_existing_webhook` 加 429 退避重试（1s/2s/4s）。
- **语义**：webhook 归 bot 管辖。若 bot token 建 webhook 失败（403/404），说明 bot 未成为维护者，
  此时应直接失败提示（webhook 建了也无意义），而非退回个人 token 临时建一个 bot 维护不了的 webhook。

### 方案 D（操作缓解）：前端/接口层提示降级

- 「权限校验失败」与「确认无权限」分开报错：一个提示"系统繁忙请稍后重试"，一个才提示"请设置 bot 为维护者"
- 托管后若 webhook 缺失，提供「重新补建 webhook」入口

### 方案 E（误报修复）：错误文案与限流判定分级

**问题**：`repos.py:409` 报错文案 `当前为{role_name or access_level}` 在 `bot_perm=None`（429）时
打印裸数字 `0`，被误解为「全局 bot token 找不到 / token 为 0」。需把「限流导致的未判定」与「确认无权限」分开：

**E-1. `get_member_permission` 返回区分「限流」与「无权限」**：

```python
# repo_manager.py
_RETRYABLE_CODES = {429, 500, 502, 503, 504}


def get_member_permission(token, owner, repo, username, _retries=3) -> dict | None:
    """返回权限字典。token 传全局 bot token（grape_bot 自查）。429/5xx 退避重试；最终失败区分：
       - 返回 None  = 确定性失败（403/404 无权限 / 参数无效）
       - 抛限流阈值标记：供调用方识别「未判定」（本函数暂不抛，由调用方按 None+日志判断）
    """
    import time
    last = None
    for i in range(_retries):
        try:
            return _req("GET", f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}/permission", token)
        except RuntimeError as e:
            code = getattr(e, "code", None) or _extract_http_code(str(e))
            if code in _RETRYABLE_CODES or "too large" in str(e) or "429" in str(e):
                last = e
                time.sleep(2 ** i)  # 1s / 2s / 4s
                continue
            log.warning(f"get_member_permission failed: {e}")
            return None
    log.warning(f"get_member_permission rate-limited (final): {last}")
    raise RateLimitedError(f"GitCode API rate limited: {last}")  # 或返回特殊标记
```

**E-2. `repos.py` 报错文案区分「强度」**：

```python
# 限流/未判定 → 提示重试而不误报权限为 0（统一用 bot token）
try:
    bot_perm = get_member_permission(global_bot_token, owner, repo_name, global_bot_username)
except Exception:
    raise HTTPException(status_code=503, detail={"code": 503,
        "message": "托管暂时失败：GitCode API 调用过于频繁，请稍后重试"})

access_level = (bot_perm or {}).get("access_level", 0) or 0
if access_level < 40:
    role_name = (bot_perm or {}).get("role_name", "")
    raise HTTPException(status_code=502, detail={"code": 502,
        "message": f"托管失败: 全局 Bot（{global_bot_username}）未成为仓库维护者（Maintainer，当前权限={role_name or f'level {access_level}'}）。请在 GitCode 仓库成员中将 {global_bot_username} 权限设为维护者后重试"})
```

**要点**：
- **429/限流 → 503「系统繁忙请重试」**，不再显示「权限为 0」误导
- **403/404 → 仍 502「请设置维护者」**，但文案改为 `权限={role_name or level N}`，明确是权限而非 token
- token 校验问题与权限问题彻底分离：token 缺失是 `repos.py:359-360` 的「GitCode API Token 未配置」（真找不到），与权限校验是两条错误路径

### 方案 F（核心增强）：托管状态标记 + 页面告警 + 自愈

> 需求方提出：上述任意环节故障（429 中断等）后，项目**仍能进入托管工作界面**（只是功能不完整）。
> 应在进入项目页面时**顶部横幅告警**，并支持**手动检查/重跑未完成的托管流程**。

**F-1. `managed_repos` 增加托管状态列（持久标识）**

```sql
ALTER TABLE managed_repos ADD COLUMN host_status TEXT DEFAULT '';   -- ''=正常 completed / bot_missing=bot未成功加入 / webhook_missing=webhook未创建 / partial=流程部分执行
```

取值约定：
| 值 | 含义 | 判定 |
|----|------|------|
| `''`（空） | 托管完整（默认，旧数据视为完整） | bot 已是维护者 + webhook 存在 |
| `bot_missing` | bot 未成功加入成员 | `get_member_permission(bot)` 非 maintainer |
| `webhook_missing` | webhook 未创建/失效 | `find_existing_webhook` 无结果 |

- host_repo 各阶段写状态：邀请前置 `partial`；bot 确认后置空（完整）；任一步失败记录失败态。
- 迁移幂等：`ALTER TABLE` try/except 沿用现有模式；存量行默认 `''`。

**F-2. 进入项目异步检查**（`GET /api/dashboard/{owner}/{repo}` 响应携带 `host_status`）

- dashboard 路由异步检查该 repo 的实时托管状态（bot 是否维护者 + webhook 是否存在），更新 `managed_repos.host_status`。
- 返回体增加 `host_status` 字段，前端据此显示告警横幅。

**F-3. 页面顶部告警横幅（前端 dashboard view）**

- `host_status != ''` 时，在页面**最上方**显示红色横幅：
  - `bot_missing` → 「该项目 grape_bot 未成功加入项目，请手动添加 bot 至项目」（附「刷新检查」按钮）
  - `webhook_missing` → 「项目 webhook 缺失，Grape 无法接收仓库事件」（附「补建 webhook」按钮）
- 横幅提供「立即重检」按钮 → 调自愈接口（F-4），避免用户手动刷新整页。

**F-4. 自愈接口** `POST /api/repos/{owner}/{repo}/repair-host`

```python
# 幂等自愈：按 host_status 缺失步骤补齐
1. bot_missing:
   - add_collaborator(owner_token, owner, repo, bot, "maintainer")   # 邀请
   - accept_invite_for_repo(bot_token, owner, repo)                  # bot 接受
   - get_member_permission(bot_token, owner, repo, bot)              # 校验
   - 已是维护者 → 清空 bot_missing；标记 webhook_missing 待补
2. webhook_missing:
   - find_existing_webhook(bot_token, owner, repo, webhook_url)
   - 无 → setup_webhook(bot_token, owner, repo, webhook_url)         # bot token 建
   - 成功 → 清空 webhook_missing
3. 全部完成 → host_status='' → 返回 {"status": "ok", "host_status": ""}
```

- 全程用 bot token（见方案 A/C 原则）；owner token 仅 F-4.1 邀请用。
- 幂等：重复点「重检」不重复邀请/建 webhook；某步已满足则跳过。

## 六、验收标准

- [ ] 高频调用 owner 托管仓库时，不再因 429 误报「bot 权限为 0」而中断
- [ ] 邀请+接受成功后，即使权限查询 429，webhook 依然被创建
- [ ] `get_member_permission` 遇到 429 自动重试（1s/2s/4s），仅确定性无权限（404/403）才返回 None
- [ ] **权限校验与 webhook 创建统一使用 bot token（非 owner 个人 token）**；owner token 仅用于身份确认（L363）与发起邀请（L387）
- [ ] **webhook 仅用 bot token 创建**：bot 非维护者时 webhook 建不了 → 明确报「bot 未成为维护者」，不退回个人 token
- [ ] `setup_webhook` 429 时重试成功
- [ ] 托管成功日志包含 webhook_id，重复托管幂等返回已有 webhook
- [ ] **用户看到 429 时提示「系统繁忙请稍后重试」（503），不再出现误导性「权限为 0 / token 找不到」**
- [ ] token 真缺失（文件不存在/字段空）时提示「GitCode API Token 未配置」，与权限问题错误路径分离
- [ ] 前端错误展示能区分「限流重试」与「确认无权限（请设维护者）」
- [ ] **`managed_repos` 有 `host_status` 列**，托管中断时正确标记 `bot_missing` / `webhook_missing` / `partial`
- [ ] **托管失败后仍能进入项目页面，页面最上方显示告警横幅**（bot 未加入 / webhook 缺失对应文案）
- [ ] **每次进入项目异步检查托管状态**并更新 `host_status`
- [ ] **横幅「立即重检」按钮调 `POST /repair-host` 自愈**：已加入+webhook 已建 → 状态置空；未加入 → 重新邀请并校验
- [ ] 自愈接口幂等：重复调用不重复邀请/建 webhook
- [ ] 自愈成功横幅消失，项目功能完整可用

## 附：关键代码位置

| 位置 | 说明 |
|------|------|
| `src/api/routes/repos.py:381-448` | host_repo 邀请→校验→webhook 全流程（L405-409 验证中断点，L411/429 webhook 未执行处；L409 误报文案「当前为0」） |
| `src/api/routes/repos.py:356-360` | webhook_token 取值（现优先个人 token，拟改仅 bot token） |
| `src/api/routes/dashboard.py:865` | `GET /{owner}/{repo}` 仓库详情入口（方案 F 在此返回 `host_status` 驱动告警） |
| `src/api/routes/repos.py`（新增） | `POST /{owner}/{repo}/repair-host` 自愈接口（方案 F-4） |
| `src/api/static/js/pages/dashboard.js` | 前端仓库详情视图（方案 F-3 顶部告警横幅 + 重检按钮注入点） |
| `src/user/db.py:360-373` | `managed_repos` 建表（方案 F-1 加 `host_status` 列） |
| `src/user/repo_manager.py:185-197` | `get_member_permission`（429 直接返回 None，无重试、不区分限流与无权限；调用方现传 owner token，拟改 bot token） |
| `src/user/repo_manager.py` | `_req`（HTTPError 统一转 RuntimeError，429 code 被吞掉） |
| `src/user/repo_manager.py` | `add_collaborator`（owner token）/ `accept_invite_for_repo`（bot token） |
| `src/user/repo_manager.py` | `setup_webhook` / `find_existing_webhook`（拟改用 bot token + 429 重试） |
| `src/api/routes/repos.py:338-354` | 全局 bot token 读取（token 真缺失时的报错路径，与权限校验分离） |
| GitCode API | `GET /api/v5/repos/{owner}/{repo}/collaborators/{u}/permission`（每用户 50 次/分钟 限流） |