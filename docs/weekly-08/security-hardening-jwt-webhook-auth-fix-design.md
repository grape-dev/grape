# 方案：安全加固 — JWT 默认密钥伪造 + GitHub webhook 无签名放行

> 状态：**已实施（TDD：红→绿）** ｜ 分支：`weekly-08-lyz`
> 关联：外部安全审计报告（`luojiyin1987`，私有披露）两项漏洞修复
> 敏感信息：本文仅含占位符（`<xxx>`）与代码标识符（文件:行号/变量名/请求头名），不含任何真实 IP、域名、口令、密钥、账号、token 值。

---

## 〇、问题现象（外部审计摘要）

| # | 漏洞 | 影响 |
|---|------|------|
| V1 | 未配置 `JWT_SECRET` 时落回**已知默认密钥**，可伪造任意用户/admin JWT | 攻击者持已知密钥签发 HS256 token → 冒充任意用户/管理员 |
| V2 | GitHub webhook 在缺失签名与 token 两个头时**直接放行**，调度流水线 | 攻击者伪造事件触发仓库流水线，甚至触发仓库删除级联清理 |

两处均经**代码评审确认**（审计方未对线上实例实测，本地复现路径已给出）。

---

## 一、目标语义

| 项目 | 现状 | 目标 |
|------|------|------|
| `JWT_SECRET` 缺省 | 落回已知默认字符串 | **不落回任何已知值**；未配置时开发/测试用进程内随机密钥，生产 fail-closed |
| 生产环境启动门禁 | `_startup_security_check()` **仅在 `env=production` 时执行**；`docker-compose.yml` 仅传 `TZ` | JWT 密钥校验**无条件执行**；`docker-compose.yml` 要求显式提供 `JWT_SECRET` |
| 开发/测试模式 | 未配置密钥时使用已知默认值 → 即使不上生产也可被本地对端伪造 | 未配置时生成**进程内随机密钥**（不可预测），并打印告警 |
| 密钥黑名单 | 含空串 + 历史默认值 + 常见占位 | 保留并**扩展**：删除已知默认值回落路径后，黑名单继续拦截任何已知/占位字符串 |
| GitHub webhook 鉴权 | 签名只在**存在**时校验，token 只在**存在**时校验，**两者皆缺 → 校验整体跳过** | **至少提供一种凭据且校验通过**；两者皆缺 → `401`；签名/ token 错误 → `403` |
| `/webhook/gitcode` 误入 GitHub 事件 | 转发到同一个弱校验函数 | 转发路径与 `/webhook/github` 使用**同一强校验** |

---

## 二、实施说明（对应原方案 Step 1/Step 2）

### Step 1.1 移除已知默认密钥回落（`src/api/jwt_utils.py:36-40`）

- `JWT_SECRET = os.environ.get("JWT_SECRET", "")` —— 缺省**空串**，不再回落到可预测字符串；`_DEFAULT_SECRET` 保留为空串常量（兼容历史引用）。
- `_INSECURE_SECRETS`（`jwt_utils.py:78`）：`{"", <历史默认>, "change-me-in-production", "grape-dev"}`——历史默认与旧兜底 `grape-dev` 一并继续拦截；HS256 最小长度校验 `len >= 32` 保留。

### Step 1.2 签名密钥解析「强校验 + 开发随机兜底」（`jwt_utils.py:84-99, 116-145`）

新增两个辅助函数：
- `_is_strong_secret(secret)`（`:84`）：非空、非黑名单、`len >= 32`；
- `_ensure_dev_ephemeral_secret()`（`:89`）：开发/测试兜底，`secrets.token_urlsafe(48)` **每次进程启动生成不同值**并打印告警（不可预测、重启后旧 token 失效，可接受）。

`_get_signing_secret()`（签发）与 `_get_verify_secret()`（验签）无 kid 密钥池时**依次回退**：强 `JWT_SECRET` → 开发随机兜底密钥；生产无任何有效密钥时由启动门禁拦截（`jwt_utils.py:116,130`）。

`validate_jwt_secret()`（`:101`）最终语义：`True` 当「密钥池已配置」或「JWT_SECRET 为强密钥」或「非生产（随机兜底）」，否则 `False`。

> 实现注记：原方案命名为 `ensure_signing_key()`，实际实现拆分为 `_is_strong_secret` + `_ensure_dev_ephemeral_secret` 两个小函数，语义与方案一致。

### Step 1.3 启动门禁默认开启（`src/api/main.py:17-38`）

- `_startup_security_check()`：JWT 密钥校验**无条件执行**（不再被 `is_production()` 提前跳过）；`env=development` 也不安全时仅在随机兜底态放行，其余 `SystemExit`。
- 密钥轮换池、邮件、凭据加密、cookie、redirect_uri 检查保留生产门禁。

### Step 1.4 部署配置（`docker-compose.yml` + 校验码密钥）

- `docker-compose.yml`：`environment` 增加 `JWT_SECRET` 必需占位（`${JWT_SECRET:?required ...}`），缺失即 `docker compose up` 启动失败。
- `src/auth/verification.py:49-56`：`_hash_code` 原硬编码 `"grape-dev"` 兜底密钥改为复用 `_get_signing_secret()[0]`（强 JWT_SECRET / 密钥池 / 开发随机兜底），**不再有任何硬编码默认密钥**。

### Step 2.1 GitHub webhook 强校验（`src/api/routes/webhook.py:244-268`）

`_process_github_webhook` 的「若存在才校验」改为**强制**逻辑：

```python
signature = headers.get("X-Hub-Signature-256", "")
token_header = headers.get("X-GitHub-Token", "")
if not signature and not token_header:
    return 401  # 缺少任何凭据 → 拒绝，不进入流水线调度
if signature:
    computed = hmac.new(<webhook_secret>.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(f"sha256={computed}", signature):
        return 403
else:
    if token_header != <webhook_secret>:
        return 403
```

- 至少一种凭据且校验通过才继续；两者皆缺直接 `401`。
- 仓库删除事件（`_is_repo_delete_event`）路径在强校验**之后**，天然受保护。
- 保持「未托管仓库仍 `404`」行为不变（未知仓库不泄露存在性）。

### Step 2.2 GitCode 路由转发路径同步

- `/webhook/gitcode` 检测到 `X-GitHub-Event` 后（`webhook.py:146`）转发到 `_process_github_webhook`，天然复用 Step 2.1 强校验，无额外改动。

---

## 三、模块改动

| 模块 | 改动 | 状态 |
|------|------|------|
| `src/api/jwt_utils.py` | 移除已知默认密钥回落；`_is_strong_secret` / `_ensure_dev_ephemeral_secret` 统一签发/验签密钥解析；黑名单扩展（含 `grape-dev`）| ✅ 已实施 |
| `src/api/main.py` | `_startup_security_check()` JWT 密钥校验无条件执行；开发随机兜底态放行 | ✅ 已实施 |
| `src/auth/verification.py` | `_hash_code` 移除 `grape-dev` 硬编码兜底，复用有效签名密钥 | ✅ 已实施 |
| `docker-compose.yml` | `JWT_SECRET` 改为必需占位（缺失即启动失败） | ✅ 已实施 |
| `src/api/routes/webhook.py` | `_process_github_webhook` 凭据强校验（缺凭据 401 / 校验失败 403）；GitHub 与 GitCode 转发路径统一 | ✅ 已实施 |
| `tests/test_security_hardening.py` | 新增 19 个用例（UT-V1-01~07 / V2-01~08 + docker-compose + verification）| ✅ 已实施（红 8 → 绿 19） |

---

## 四、关键实现要点（已核实代码）

| 点 | 依据 |
|----|------|
| 缺省密钥风险确凿 | 原 `jwt_utils.py:34` 无 `JWT_SECRET` 时落回已知字符串；`docker-compose.yml` 原仅传 `TZ`，门禁在默认 `env=development` 下跳过 |
| 门禁曾被开发模式绕过 | 原 `main.py:26` `if not is_production(): return` 位于 JWT 校验之前；现已移后 |
| 签发/验签 fallback 链 | `_get_signing_secret()`（`jwt_utils.py:116`）与 `_get_verify_secret()`（`:130`）无 kid 时强 `JWT_SECRET` → 开发随机兜底；空值场景已被 `_ensure_dev_ephemeral_secret` 兜住 |
| token_version 门槛 | 伪造需 sub=用户 ID + ver=当前版本 + username 一致；新号=0 且自注册自满足，无法构成缓解 |
| webhook 缺口 | 原 244-256 的 `else` 分支只拦截「头存在且不匹配」，**两头皆缺 → 落到调度**；删除事件（260-268）同样可达。现缺任一凭据即 401 |
| 开发随机密钥跨进程 | 生产显式配置密钥（或密钥池）不受影响；仅开发/测试未配置时每次启动重新生成——测试在同一进程 create/verify，无跨进程依赖 |
| 兼容性 | 无 kid 旧 token 仍走强 `JWT_SECRET`（或开发随机兜底）验签；生产密钥池切换 kid 后旧 token 无需轮换（密钥不变仍可验） |

---

## 五、边界 / 风险

| 项 | 说明 |
|----|------|
| 存量部署未配置密钥 | 升级后启动直接失败 → 运维需先配 `JWT_SECRET` 或密钥轮换池；属预期 fail-closed |
| 开发/测试模式随机密钥 | token 重启失效；仅影响未配置密钥的本地开发与测试；生产必须显式配置 |
| 密钥轮换 | 若部署曾使用已知默认密钥，需**立即轮换**（`JWT_SECRET_KEYS` 增新 kid → 切换到新 kid → 旧 kid 保留 30 天 → 移除）；轮换使所有存量会话失效（refresh 亦失效），需接受重新登录 |
| webhook 兼容 | GitHub 官方 webhook 始终携带 `X-Hub-Signature-256`，强校验不破坏官方链路；`X-GitHub-Token` 为存量自定义通道，显式校验保留 |
| 仓库删除高破坏路径 | 强校验前置后，伪造删除事件无法再触发级联清理；合法删除事件正常放行 |
| 日志 | 新增拒绝日志仅含头名摘要与匹配结果，不落任何密钥/token 明文 |

---

## 六、测试用例设计与结果（TDD：先红后绿）

| 编号 | 用例 | 断言 | 结果 |
|------|------|------|------|
| UT-V1-01 | 无 `JWT_SECRET` 不启用已知默认值 | `_DEFAULT_SECRET == ""` | ✅ |
| UT-V1-01b | 未配置时签发密钥不可预测 | `_get_signing_secret()` 非空、`>=32`、非默认 | ✅ |
| UT-V1-02 | 黑名单非空且测试环境（dev）放行 | `len(_INSECURE_SECRETS) > 0`；`validate()==True` | ✅ |
| UT-V1-03 | 生产弱密钥（`<32`）拒绝 / 开发放行 | 生产 `validate()==False`；开发 `==True` | ✅ |
| UT-V1-04 | 生产无密钥启动失败 | `validate()==False`（`SystemExit` 由门禁拦截） | ✅ |
| UT-V1-05 | 开发无密钥随机兜底 sign/verify | `create→verify` 同进程通过 | ✅ |
| UT-V1-06 | 伪造 token 攻击回归 | 历史默认密钥签的 token `verify_access_token()==None` | ✅ |
| UT-V1-07 | 密钥池配置时 `JWT_SECRET` 可为空 | 签发/验签走池密钥；`validate()==True` | ✅ |
| UT-V1-08 | docker-compose 要求 `JWT_SECRET` | compose 文件含必需占位 | ✅ |
| UT-V1-09 | verification `_hash_code` 复用有效密钥 | 输出 64 位 hex | ✅ |
| UT-V2-01 | 缺签名与 token → 401 不调度 | `401`；`run_pipeline` 未被调 | ✅ |
| UT-V2-02 | 错误签名 → 403 不调度 | `403` | ✅ |
| UT-V2-03 | 正确 HMAC → 放行调度 | `200`；`run_pipeline` 被调 | ✅ |
| UT-V2-04 | 仅合法 `X-GitHub-Token` → 放行 | `200`；调度 | ✅ |
| UT-V2-05 | 非法 token → 403 不调度 | `403` | ✅ |
| UT-V2-06 | GitCode 转发 GitHub 事件缺凭据 → 401 | `401`；不调度 | ✅ |
| UT-V2-07 | 伪造仓库删除缺凭据 → 401 不清理 | `401`；`run_pipeline` 未调；级联代理未触发 | ✅ |
| UT-V2-07b | 合法签名触发删除事件 → 放行 | `200` | ✅ |
| UT-V2-08 | 未托管仓库保持 404 | `404` | ✅ |

> **红阶段**（修复前）：UT-V1-01/02/06/07/08 + V2-01/06/07 共 8 例失败（V2：200/调度；V1：可预测密钥）；
> **绿阶段**（修复后）：`tests/test_security_hardening.py` 19/19 通过。
> 回归：`tests/test_login_provider.py / test_grape_auth.py / test_auth_token.py / test_auth_routes.py / test_api_key_lifecycle.py / test_token_error.py / test_webhook_echo_and_rollback.py / test_a2a_queue.py / test_agent_internal_auth.py / test_admin_whitelist_user_crud.py` 共 195 通过、2 失败（`test_webhook_echo_and_rollback` 两例，已确认**变更前即失败**，与本次无关）。

---

## 七、参考

- `src/api/jwt_utils.py:36-40, 78-99, 101-145`：默认密钥移除、黑名单、强密钥判定、开发随机兜底、签发/验签 fallback 链
- `src/api/main.py:17-38`：`_startup_security_check()`（JWT 校验无条件执行）
- `src/auth/security_config.py:49-60`：`is_production()` 缺省 development
- `src/auth/verification.py:49-56`：`_hash_code` 复用有效签名密钥
- `configs/security.example.json`：生产配置模板（`env` / `jwt.secret_keys` / `credential_keys`）
- `docker-compose.yml:9`：`JWT_SECRET` 必需占位
- `src/api/routes/webhook.py:216-268`：`_process_github_webhook`（244-268 强校验）；`138-146` GitCode 转发
- `src/api/deps.py:56-67`：token_version / username 一致性校验（伪造门槛，非缓解）
- `tests/test_security_hardening.py`：新增安全加固测试（V1/V2 共 19 例）
- `docs/weekly-08/DOCUMENTATION-GUIDE.md`：本文遵守的文档红线与 TDD 约定