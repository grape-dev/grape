# 运维 checklist：平台卡 `bot_username` 与 clone 凭据（gitee 必读）

适用范围：`clone_auth=basic_password` 的平台（当前只有 gitee）。
依据：分支 `feat/clone-endpoint-single-source` 的只读真机验证
（`docs/weekly-11/clone-endpoint-multiplatform-issue.md` §3.1）。

## 0 一句话

gitee 的 HTTPS clone **只接受「令牌所属账号名 : 私人令牌」**。所以：

1. 平台卡 `bot_username` 必须等于该令牌所属账号名；
2. 该仓库在凭据表里的 bot token 必须**非空**。

两项缺一，clone 必失败（`The token username invalid` 或认证为空）。

## 1 真机实测结论（私有仓 `git ls-remote`）

| 用户名形态 | 个人仓（owner == 令牌账号） | 组织仓（owner != 令牌账号） |
|---|---|---|
| 令牌所属账号名 | rc=0 ✅ | rc=0 ✅ |
| 仓库 owner | rc=0（同名巧合） | rc=128 `The token username invalid` ❌ |
| `x-access-token` | rc=128 ❌ | rc=128 ❌ |
| 空用户名 | rc=128 ❌ | rc=128 ❌ |

结论：`owner` 兜底只在个人仓成立；**组织仓/他人仓必须配置 `bot_username`**。

## 2 取值来源

```bash
# 只读，令牌不进 shell 历史：用环境变量或本地 0600 文件提供
curl -s "https://gitee.com/api/v5/user?access_token=$TOK" | python3 -c 'import json,sys;print(json.load(sys.stdin)["login"])'
```

- 取的是 `login`（账号名），**不是**组织名、**不是**仓库 owner、**不是** grape 侧账号名。
- 同一平台只允许一个 bot（D110 一平台一行）：`bot_username` 与 bot token 必须同账号。

## 3 填在哪里（优先级从高到低）

| 层 | 位置 | 适用 |
|---|---|---|
| ① meta 覆盖（逐键） | `vcs_providers_config`（管理台可改，最高优先） | 任意拓扑，改完即生效 |
| ② 表行（运行时真源） | `grape_t_repo_platforms.bot_username`（mgmt 库） | **3 库部署以此为准** |
| ③ 文件卡 | `configs/platforms/gitee.json` 的 `bot_username` | 单库 / 未迁移环境 |

worker 引擎进程没有 `configs/` 也没有 mgmt 库，它经 `GET /api/runtime/platform-cards`
取的是**表行**（#3632）。因此 3 库部署只改 ③ **不生效**，必须改 ① 或 ②。

## 4 dev 库现状（2026-11 实测）

第一次只读核查（未改任何数据）：

```
grape_t_repo_platforms (gitee)   bot_username=(空) | bot_token_status=unknown
gitee 凭据行                     4 个仓 × (bot, webhook) = 8 行，value 长度全部 0
managed_repos                    gitee 仓 5 个（凭据行只覆盖 4 个）
```

随后只补了「用户名」这一项（纯文本列，可直接改）：

```sql
UPDATE grape_dev_mgmt.grape_t_repo_platforms
   SET bot_username='<令牌所属账号名>' WHERE provider='gitee';
```

- 回读：`bot_username` = 令牌所属账号名；
- 用该用户名 + 令牌对 **5 个托管仓**做只读 `ls-remote` → **5/5 rc=0**（含空仓/无分支仓）；
- 组织仓 `myfuture_1/*` 另测：owner 形态 rc=128，令牌账号名形态 rc=0 → 证明用户名这一项
  确实是"账号名"而不是 owner。

「令牌」这一项按加密格式补齐（不走明文直写）：

- dev 环境 `credential_keys` 为空 ⇒ 密钥由运行进程 `JWT_SECRET` 派生（kid=`derived`）；
- 按同一规则在服务端生成密文（`{kid}:{nonce}:{ciphertext}`，AES-GCM，89 字符）后写库；
- 5 个托管仓 bot 行全部补齐（`micro-svc` 原本缺行）；webhook 密钥属 gitee 侧配置，未臆造；
- 服务端解密校验：5/5 `plain_len=32`、sha256 与写入明文一致。

反面教训仍在：**禁止** `UPDATE ... SET value='<明文令牌>'` —— 明文形态会让
`credential_decrypt_failed` 与"没配令牌"难以区分（实测踩过一次，已回滚）。

## 4.1 本环境已执行的数据变更（可回滚）

```
1) bot_username（纯文本列）
   原值 ''，现值为令牌所属账号名
   回滚：UPDATE grape_t_repo_platforms SET bot_username='' WHERE provider='gitee';

2) bot 凭据（密文列，5 个托管仓，含原本缺行的那个仓）
   服务端解密校验 5/5：plain_len=32，sha256 与写入明文一致
   回滚：UPDATE grape_t_repo_credentials SET value='' WHERE repo_key LIKE 'gitee:%' AND cred_type='bot';

3) webhook 凭据：未动（属 gitee 侧配置，不臆造）
```

## 5.6 部署切换（3 库 4 进程，实操过的顺序）

```bash
cd <deploy_dir>
tar czf /root/grape_backup_$(date +%Y%m%d%H%M).tgz .grape          # 运行期技能目录先备份
git stash push -m pre-switch -- <脏文件>                            # 部署目录常见脏文件
git fetch origin <branch>:refs/remotes/origin/<verify-ref> --force
git checkout -f --detach <commit>                                   # -f 因 .grape 同名文件
git log -1 --format='%h %s'
./venv/bin/python -c "import sys;sys.path.insert(0,'.');from src.domains.runtime.vcs import clone_endpoint;print(clone_endpoint.__file__)"
systemctl restart grape-arch-mgmt grape-arch-event grape-arch-insight grape-arch-worker
for p in 8081 8082 8083 8084; do curl -s -o /dev/null -w "$p %{http_code}\n" http://127.0.0.1:$p/health; done
```

回滚：`git checkout --detach <原 HEAD>` → `git stash pop` → 重启四进程 → 必要时用备份 tar 恢复 `.grape`。

## 5 补法与校验

### 5.1 查现状（只读）

```sql
SELECT provider, host, dialect, enabled, clone_auth, bot_username, bot_token_status
FROM grape_dev_mgmt.grape_t_repo_platforms WHERE provider='gitee';

SELECT repo_key, cred_type, LENGTH(value) AS len
FROM grape_dev_mgmt.grape_t_repo_credentials WHERE repo_key LIKE 'gitee:%';
```

### 5.2 填 `bot_username`

- 首选：管理台「平台卡 / 能力」页改 `bot_username`（走 meta 或表行，视部署形态）；
- DBA 路线（先备份，仅改单列）：

```sql
UPDATE grape_dev_mgmt.grape_t_repo_platforms
   SET bot_username = '<token 所属账号名>'
 WHERE provider = 'gitee';
```

### 5.3 写 bot token（只能服务端加密写）

凭据列格式 `{kid}:{nonce}:{ciphertext}`（AES-GCM），必须用 `encrypt()` 生成；
在没有 `credential_keys` / `security.json` 的机器上**无法**构造可解密文。两条正路：

1. **管理台**：仓库凭据页粘贴令牌（服务端进程内加密落库）——首选；
2. **服务端脚本**（在有 `/etc/grape/*.env` 与 `security.json` 的机器上执行）：

```python
# 令牌从环境变量取，不进命令行/日志
import os
from datetime import datetime, timezone
from src.domains.mgmt.auth.credential_crypto import encrypt
from src.domains.insight.public import upsert_repo_credential_value

now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
tok = os.environ["GITEE_BOT_TOKEN"]          # 私人令牌
for rk in ("gitee:<owner>/<repo1>", "gitee:<owner>/<repo2>"):
    print(rk, upsert_repo_credential_value(rk, "bot", encrypt(tok), now))
```

**禁止** `UPDATE grape_t_repo_credentials SET value='<明文令牌>'`：写进去的不是密文，
凭据域解密会失败，故障现象与"没配令牌"不同（`credential_decrypt_failed`），更难排。

### 5.4 校验（只读，令牌不进命令行）

```python
# 骨架：用平台卡派生出来的用户名 + 令牌做 ls-remote（等价 worker 的注入形态）
import base64, subprocess, os
user, tok = "<bot_username>", os.environ["GITEE_TOKEN"]          # 令牌从环境变量取
b64 = base64.b64encode(f"{user}:{tok}".encode()).decode()
cmd = ["git", "-c", f"http.extraheader=Authorization: Basic {b64}",
       "ls-remote", "--heads", "https://gitee.com/<owner>/<repo>.git"]
p = subprocess.run(cmd, capture_output=True, text=True,
                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
print(p.returncode, p.stderr.strip().splitlines()[:1])            # 期望 rc=0
```

### 5.5 日志关键字（排障第一步）

```
成功取卡      platform cards from server contract: ['gitee']
卡缺用户名    clone_endpoint: gitee 的平台卡未声明 bot_username ... 回退为仓库 owner
上游拒绝      The token username invalid
```

## 6 验收 checkbox

- [ ] `grape_t_repo_platforms` 中 gitee 行存在且 `enabled=1`
- [ ] `bot_username` = 令牌所属账号名（非空）
- [ ] 目标仓的 bot token 长度 > 0（经接口写入）
- [ ] **组织仓** `ls-remote` 返回 rc=0（个人仓通过不足以证明配置正确）
- [ ] worker 日志里没有 `回退为仓库 owner`
- [ ] 跑一条真实 coding 任务后，`<workspace>/<org>/<repo>/issues/<n>/coding/` 下有 `.git`

## 7 常见误判

| 误判 | 后果 |
|---|---|
| `bot_username` 填组织名 / owner | 组织仓 rc=128，个人仓看似正常 → 误判"已配好" |
| 填 grape 侧账号名（登录 grape 的用户名） | gitee 不认，rc=128 |
| 只改 `configs/platforms/gitee.json` | 3 库 worker 读表行，文件卡不生效 |
| 只填 `bot_username`，token 仍为空 | 用户名对了但密码空，认证失败 |
| 认为"代码修好就万事大吉" | 本分支只负责派生；账号名与令牌是数据，必须配置 |

## 8 关联

- 代码：#3632 后继（本分支 `feat/clone-endpoint-single-source`）
- 文档：`clone-endpoint-multiplatform-issue.md` §3.1、`clone-endpoint-single-source-design.md` §9.6
- 卡字段说明：`configs/platforms/gitee.json` → `_fields.bot_username`

## 7 新增平台接入清单（纯配卡）

本分支之后，**新增一个仓库平台 = 加一张卡**，clone 链路不需要改代码。

```
□ 1 卡字段最小集（host / dialect / api_base / api_auth / clone_auth /
      bot_username / token_env / webhook_path / webhook_verify / webhook_dialect）
     · host 必填；clone_auth ∈ {x_access_token, oauth2, basic_password}
     · basic_password 时 bot_username 必须填**令牌所属账号名**（见 §1 真机证据）
     · token_env 大写下划线；缺省派生 BOT_<PROVIDER>_TOKEN
□ 2 落库位置：3 库部署 → 写表行 grape_t_repo_platforms（或 meta 覆盖）；
     只放 configs/platforms/ 对 worker 引擎无效（worker 读表/契约）
□ 3 凭据：按 §5.3 用服务端 encrypt 写入该平台的 bot 行（每个托管仓一行）
□ 4 webhook：按卡的 webhook_path 配到平台侧，验签方式与卡 webhook_verify 一致
□ 5 验收：
      tests/runtime/test_new_platform_card_only_onboarding.py（纯配卡 clone 链路）
      tests/runtime/test_clone_endpoint_multiplatform.py（四平台地址/用户名）
      tests/api/test_channel_capability_bits.py（通道能力位与子路径）
□ 6 运行时确认：worker 日志出现 platform cards from <source>: ['<新平台>']
```

**不需要改的**：`repogit` / 沙箱 executor / sandbox_context / takeover / git-credential
—— 它们都只调 `clone_endpoint`，平台差异全在卡里。
