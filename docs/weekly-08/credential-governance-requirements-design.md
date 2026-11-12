# 凭据管理需求与设计 — 业界方法论对照 + 差距修复（G 系列闭环）

> 状态：需求+设计（合并文档）
> 基线：weekly-08 head 2d97419c9（全部事实实测核对，标注行号）
> 类型：安全加固（凭据治理域）
> 依赖：`credential-inventory-requirements.md`（凭据对象全景清单，本文档的 G 编号沿用其 §七）
> 敏感信息声明：本文档不含任何真实 IP / 域名 / 密码 / 账号 / token（全部 `<xxx>` 占位符）
> 纪律声明：按"方案先行"，本文档确认后才实施。每一项改动均先 grep 现有最接近写法复制模式，不发明新封装。

## 〇、问题 / 目标

grape 凭据体系对照业界成熟方法论（消除硬编码 / 密钥分离 / 短期凭据优先 / 自动轮转 / 全程审计），存在两类差距：

1. **覆盖面差距**：已有优质基础设施（credential_crypto AES-256-GCM、audit_logs hash 链、rta_/grape_/tk_ 哈希存储）只覆盖账户域和 external_identities，凭据域其余对象（AK/SK、bot token、session token）明文存储且零审计
2. **泄露面差距**：`GET /settings/credentials` 把明文 AK/SK 回显前端（前端 account.js:211-212 直接取真实值填输入框）；AK/SK 注入 worker 环境变量后无脱敏覆盖

目标：**不引入 Vault/外部 KMS 等新基础设施**（与 grape 自托管双主部署形态不匹配，属过度设计），而是把已有三件基础设施从"账户域覆盖"扩展到"凭据全域覆盖"。

### 业界方法论 → grape 威胁模型的翻译表（过滤掉不适用的）

| 业界原则 | 业界做法 | grape 适配结论 |
|---------|---------|--------------|
| 集中托管+加密 | 专用 Secret Manager | ✅ 适配：复用 credential_crypto（已验证的信封加密骨架） |
| 密钥与凭据分离 | KMS/HSM 托管 CMK | ⚠️ 部分适配：做不到独立 CMK（单机自托管），等价物 = 收紧 K1 兜底链，禁用"JWT_SECRET 派生"降级 |
| 动态短期凭据 | Vault 动态数据库凭据 | ❌ VCS 域不适用（GitCode/GitHub 无动态签发 API）；等价物 = tk_ TTL + JWT 短期 + OAuth refresh 循环（均已实现） |
| 自动轮转+双写 | ≤90 天轮转 | ⚠️ 已等价实现（bot token 60s TTL 缓存 + swap 七步 + invalidate）；新增 PAT 无续期提示即可 |
| 全程审计 | 每次获取都记录 | ✅ 适配：audit_logs hash 链已在，只需扩凭据域覆盖 |
| Secret 注入模式 | sidecar/CSI | ❌ 不适用（无 K8s）；env 注入模式保留，但补脱敏覆盖 |
| 不落盘 | 内存态凭据 | ✅ 已符合（GIT_ASKPASS/token 不落盘设计） |

## 一、需求清单

| 编号 | 需求 | 优先级 | 来源 |
|------|------|--------|------|
| FR-1 | 凭据读取端停止回显明文 AK/SK（G2） | **P0** | 泄露面：响应含明文 + 前端 DOM 驻留 |
| FR-2 | 凭据域写入/吊销/下发全事件审计（FR-INV 覆盖面扩展） | **P0** | 审计 46 处全在账户域，凭据域 0 处 |
| FR-3 | user_credentials AK/SK 明文接入 AES-256-GCM（G1） | P1 | 存储保护不对称（external_identities 已加密，此表明文） |
| FR-4 | bot token（全局 meta + 仓库级）接入加密（G3） | P1 | 同上；Issue #349 立项未闭环 |
| FR-5 | 生产模式禁用"JWT_SECRET 派生 credential_keys"降级 | P1 | 密钥分离原则的 grape 等价物 |
| FR-6 | AK/SK 环境变量注入值纳入 result_text 脱敏链 | P1 | 脱敏实测只覆盖 token（event_pipeline.py:3622），AK/SK 注入值不覆盖 |
| FR-7 | global-bot-token.json 迁移完成后主动提示删除（G5） | P2 | 文件仍含真实凭据明文 |
| FR-8 | crypto.json 废弃落地执行（G6） | P2 | weekly-06 设计已定但文件仍在 |
| FR-9 | session token（devops_agent_session.repo_gitcode_token）加密（G4） | P2 | 明文存储；量级评估后再定档 |

## 二、不在范围

- 不引入 Vault / Infisical / OpenBao / 云 Secret Manager——部署形态不匹配，拒绝过度设计
- 不做 KMS/HCM 独立托管——自托管单机场景收益不抵复杂度
- VCS token 体系行为语义（过期框架/健康检查）——归 weekly-08/token-config/
- #372 系列缺陷——归 issue372-user-token-core-requirements.md
- 动态短期凭据改造——VCS 平台无此 API，不适用

## 三、设计（逐 FR）

### FR-1：读取端停止回显明文（G2）

**现状（实测）**：settings.py:102-103 `GET /settings/credentials` 返回 `"ak": hw_cred.get("ak","")` 真实值；前端 account.js:211-212 取 `hw.ak` 填输入框（type=password 遮蔽 + 👁 按钮可切换明文），并记录 `window._accountCloudCredOriginal` 供保存时比对"未改动不回传"。

**设计**：
1. 后端：`huawei_cloud`/`codearts` 节只返回 `ak_exists`/`sk_exists` 布尔（删除 `"ak"`/`"sk"` 字段）。gitcode/github 节已是 exists 风格，天然对齐
2. 前端：输入框 value 置空 + placeholder 提示"已配置（输入以覆盖）"；`_accountCloudCredOriginal` 改存 `{ak_set: bool, sk_set: bool}`；保存逻辑：非空才回传（空=不改），与现有"空串显式清除"语义用独立"清除"操作区分（清除按钮→显式 PUT 空串）
3. 兼容：PUT 语义不变（空串=清除语义保留）；老前端配新后端 = 输入框空白可覆盖输入，无破坏

**关键取舍**：业界"写后不可读"（write-only）模式的 grape 落地。用户丢失 AK 源头在华为云控制台可重查，回显便利性不值得泄露面。

### FR-2：凭据域审计

**现状（实测）**：audit_logs 为 hash 链（seq UNIQUE + prev_hash/curr_hash，集群并发冲突重试，auth/audit.py:80-145）；46 处 log_audit 全部账户域；凭据域 0 处。admin 保存平台凭据有 `admin_config_saved`（detail 只记字段名不记值——正确模式，admin/routes.py:201）。

**设计**：扩 EVENT_TYPES + 补点位，detail 一律**只记定位信息不记凭据值**（复制 admin_config_saved 模式）：

| 事件类型 | 触发点（实测行号） | detail 字段 |
|---------|------------------|------------|
| `user_credential_set` | settings.py update_credentials（每 provider 落库处） | `{provider, fields: ["ak"]}` |
| `user_credential_clear` | 同上（清除分支） | `{provider, fields}` |
| `runtime_credential_created` | settings.py:1445 create_runtime_credential | `{name, scope, org_id}` |
| `runtime_credential_revoked` | settings.py:1607 revoke 端点 | `{cid, name}` |
| `runtime_credential_scope_changed` | settings.py:1500 scope 端点 | `{cid, old_scope, new_scope}` |
| `repo_bot_token_set` | repos.py:1558 set_repo_bot_token | `{repo_key, provider}`（R15 流程末尾） |
| `repo_bot_token_cleared` | 同上 R15.5 清空分支 | `{repo_key}` |
| `credential_env_dispatch` | runtime.py:529 `/tasks/{id}/env` 下发 | `{task_id, providers: ["huawei_cloud"]}` |

**采样与噪音控制**：`credential_env_dispatch` 是高频端点（远程 worker 每任务执行前调用），**只记 provider 集合不记值**，且考虑加节流（同 task_id 去重——审计表已有 seq 唯一，可加应用层 LRU 去重窗口，模式参考 deps.py 的 `_user_status_cache` TTL 缓存写法）。若噪音仍不可接受，降级为只审计"凭据内容变更"不审计"读取"（业界 JIT 审计的理想与现实的折衷）。

**顺带修正一个语义漂移**：全局 bot token 的读取链 get_global_gitcode_bot_token（user/db.py:1932）注释说"DB 唯一权威，文件 fallback 确保迁移失败时可用"——实测 file_fallback_fn 仍在运行时生效，与 #345 决策"文件不再被运行时访问"注释矛盾。本 FR 不改行为（fallback 是容灾合理保留），但文档口径统一为"DB 权威 + 文件只读容灾 fallback"，消除两处注释打架。

### FR-3：AK/SK 接入加密（G1）

**现状（实测）**：user/db.py:530 set_credential 直写 config_json 明文；读取两处（user/db.py:545 get_credentials、:572 get_credential）。

**设计**：**最小改动原则——不动表结构，加密粒度=整字段**：

1. 写入：`set_credential` 内 config_json 序列化后整串过 `credential_crypto.encrypt()`（复用 external_identities 已验证模式：`{kid}:{nonce}:{ct}` 格式，空串不加密，无冒号兼容明文）
2. 读取：`get_credentials`/`get_credential` 的 `json.loads` 前先过 `decrypt()`（decrypt 的"无冒号视为明文"兼容让存量明文行零迁移自然过渡——与 D10 迁移期机制同一设计）
3. 消费端零改动：`_collect_server_env`（service.py:487）拿到的已是解密后明文 dict，注入链路不变
4. 判定读取成功：冒烟用 `set_credential→get_credential` round-trip + 存量明文行回读

**为什么整字段加密而非逐 key**：AK/SK 同属一个 provider 的 config_json，整串加密保持读写对称、一处改动；逐 key 加密会让 dict 半明半密，读取端要做 per-key decrypt 组装，复杂度不匹配收益。

**风险**：K1 密钥丢失=AK/SK 全部不可恢复（external_identities 已有同款风险，非新增；密钥轮换 kid 机制已支持）。

### FR-4：bot token 接入加密（G3）

**现状（实测）**：全局 bot token 存 meta 表（save_config→set_meta_value，admin/config_store.py）；仓库级存 managed_repos.bot_gitcode_token（R15 流程写入）。

**设计**（与 FR-3 同模式，但有两个额外约束）：

1. **写入端 3 处**：admin save_config（config_store.py，bot 字段写 meta 前 encrypt）；R15 七步流程第 6 步 `_update_repo_bot_token`（写 managed_repos 前 encrypt）；token swap 流程写 DB 处
2. **读取端收敛**：全局走 get_global_gitcode_bot_token/get_global_github_bot_token 统一入口（60s 缓存内嵌 decrypt）；仓库级走 TokenResolver 的 RepoBotToken._fetch（token_resolver.py:604 已有 decrypt import 的邻位模式）——**先 grep 全仓 bot_gitcode_token 的裸 SELECT 点位收齐清单再动手**（防 #344 式"改了 A 处漏 B 处"）
3. **比对类读取的坑（关键）**：A2A 鉴权 `_authorize_a2a` 拿"存储原值"与请求头 token 做等值比对（webhook.py:298 `get_repo_bot_token_no_global`）——**必须拿到解密后原值再比对**，否则加密落地瞬间 A2A 全断。read_stored_token 语义保持"返回解密后的存储原值"，文档口径同步
4. **swap 流程比对**：global_bot_token_swap 遍历时"bot_token==旧 token"判断（config_store.py:161）读的也是存储值——同样必须解密后比对

### FR-5：禁用 JWT_SECRET 派生降级

**现状（实测）**：security_config.py:84-92，credential_keys 来源链 `security.json > JWT_SECRET 派生 sha256 > dev 默认`。

**设计**：get_credential_keys 在 production 模式（判定逻辑复用现有 get_env 的"字面 production"标准）下：
- security.json credential_keys 未配置或含 `dev:` kid → 抛 RuntimeError 拒绝启动（复制 credential_crypto.py:77-81 生产强校验 cryptography 的模式）
- dev 模式行为不变

**风险**：现有生产部署若正靠"JWT_SECRET 派生"工作，升级即拒启。缓解：启动报错信息明确指引"在 security.json 配置 credential_keys（生成命令）"——报错即迁移文档，与 OMIT-2 模式一致。**部署说明同步**（DEPLOYMENT 文档补安全升级段落）。

### FR-6：AK/SK 注入值纳入脱敏

**现状（实测）**：result_text 脱敏只做 user token 字符串替换（event_pipeline.py:3622-3623、3745-3746 `_user_token_for_redact`）。AK/SK 经 `_collect_server_env` 注入 worker 进程环境变量后，agent 生成日志/结果若回显环境变量即泄露，且无脱敏防线。

**设计**：`_user_token_for_redact` 机制扩展为多值列表（保持函数签名最小变更）：
1. 事件管道侧在现有获取 user token 的同位点（event_pipeline.py:3386 附近），追加收集该 host/assignee 用户的 `get_credentials(user_id)` 中 ak/sk 值（同样只在非空时收集）
2. 替换处改为遍历列表逐值 `[REDACTED]`（保持既有 replace 语义，仅从单值变多值循环）
3. **不新增日志扫描兜底**（credential-filter.json 已定义敏感文件名拦截，环境变量值级过滤不在本次范围——避免为低频路径引入全文扫描的复杂度）

### FR-7：迁移完成提示删除（G5）

**设计**：`_migrate_global_bot_token_to_db`（user/db.py:1889 一带）迁移成功路径上，检测 `configs/global-bot-token.json` 仍含非空 token 时 log.warning 一次（含文件路径，不含值）+ admin 侧 `/admin/config` 响应追加 `gbt_file_residual: true` 标志，前端 admin 配置页显示提示条"全局 Bot Token 已迁移 DB，建议删除遗留文件 <path>"。**不自动删除文件**（破坏性操作留给管理员，符合运维权责分离）。

### FR-8：crypto.json 废弃执行（G6）

**设计**：weekly-06 v2 设计已定（密钥迁 security.json credential_keys）。落地动作：
1. `configs/crypto.json` 若存在且 security.json 未配 credential_keys → 启动 warning 提示迁移（不自动迁移——密钥迁移属运维操作）
2. .gitignore 已有条目保留；README/部署文档标注废弃
3. 本机该文件实际清理由运维执行（文档口径：值已不再被任何运行时读取路径引用——**实施前 grep 验证 `encryption_key` 在 src/ 零引用**，实测当前已零引用）

### FR-9：session token 加密（G4）

**设计（P2，先评估再定）**：devops_agent_session.repo_gitcode_token / issue_agent_session 同族列。写入端（agent 配置流）+ 读取端（TokenResolver DevopsAgentSessionToken._fetch）同 FR-3 模式。**排 P2 原因**：此 token 本身多为全局 bot token 的副本（#372 前架构），明文存储的实际增量泄露面 = 全局 bot token 已加密后的副本残留。FR-4 落地后新增写入自动加密，存量副本由 agent 重新配置自然覆盖——**优先做 FR-4，FR-9 视 FR-4 后的存量评估决定是否实施**。

## 四、实施顺序与依赖

```
第一批（P0，互相独立）：
  FR-1 回显下线          —— 纯读取端，无依赖
  FR-2 审计覆盖          —— 零新基础设施，无依赖

第二批（P1，有顺序）：
  FR-5 K1 兜底链收紧     —— 先做（否则加密后的密钥仍可从 JWT_SECRET 推出）
  FR-3 AK/SK 加密        —— 依赖 FR-5（密钥独立后再加密才有分离意义）
  FR-4 bot token 加密    —— 依赖 FR-5；注意 A2A 比对/swap 比对两处解密语义
  FR-6 脱敏扩展          —— 依赖 FR-3（AK/SK 已解密在内存才谈得上收集）

第三批（P2）：
  FR-7 / FR-8 / FR-9
```

## 五、边界与风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| FR-4 加密后 A2A 比对断链 | 全部 A2A 跨仓调用 401 | 实施时先 grep 收齐"存储值比对"全部点位（A2A/swap/信任域三处已知），逐处加 decrypt；测试覆盖 test_a2a_* 系列 |
| FR-5 升级拒启 | 现网生产部署启动失败 | 报错内嵌迁移指引；发版说明置顶；判定复用现有 production 语义不引入新开关 |
| FR-3 密钥丢失 | AK/SK 不可恢复 | 与 external_identities 同款既有风险；发版说明强调 security.json 备份；credential_keys 多 kid 轮换已支持 |
| FR-1 前端兼容 | 老缓存页面保存空值 | 保存逻辑"空=不改"已天然兼容；PUT 空串=清除语义保留但仅从清除按钮触发 |
| FR-2 审计噪音 | audit_logs 膨胀 | /env 下发事件 LRU 去重；只记 provider 不记值 |
| 集群双主滚动升级窗口 | 一台已加密一台未加密 | decrypt 的明文兼容（无冒号=明文）让读侧双态可读；写侧 INSERT OR REPLACE 幂等；窗口期 <60s TTL |

## 六、测试设计

按 DOCUMENTATION-GUIDE §5 TDD（先红后绿）：

| FR | 测试要点 | 已有可参考测试 |
|----|---------|--------------|
| FR-1 | GET 响应不含 ak/sk 明文字段；PUT 覆盖/清除/不改三分支 | test_oauth_security_p0.py 的脱敏断言模式 |
| FR-2 | 每事件类型写入后 audit_logs 出现对应行，detail 无凭据值（断言字段集合不含 ak/sk/token 字符串）；hash 链连续性不破坏 | 现有 audit 相关测试（grep log_audit 测试） |
| FR-3 | set→get round-trip；存量明文行回读兼容；`_collect_server_env` 拿到解密值 | credential_crypto 现有单测 |
| FR-4 | RepoBotToken._fetch / get_global_*_bot_token 返回解密值；A2A `_authorize_a2a` 比对通过；swap 遍历命中旧 token | test_token_scenario_coverage_r20.py、test_global_token_swap_r21.py、test_a2a_* 系列 |
| FR-5 | production 模式未配 keys → RuntimeError；配 dev: kid → RuntimeError；development 模式行为不变 | security_config 相关现有测试（grep production 门禁测试） |
| FR-6 | result_text 含 AK/SK 明文 → 输出 [REDACTED] | event_pipeline 现有 redact 测试 |
| FR-7/8 | 迁移成功+文件残留 → 标志返回；src/ 中 encryption_key 零引用断言 | — |

回归门：全部现有 token/a2a/bot_token 系列测试零新失败（pre-existing 失败清单见 token-config-system-architecture.md §六）。

## References

- docs/weekly-08/credential-inventory-requirements.md（凭据全景清单，G 编号来源）
- src/auth/credential_crypto.py / src/auth/security_config.py（加密与密钥体系实现）
- src/auth/audit.py（hash 链审计基础设施）
- docs/weekly-08/token-config/04-token-resolver.md（read_stored_token 语义契约——FR-4 比对语义依据）
- src/admin/config_store.py（save_config 模式与 swap 触发点）
- docs/weekly-06/grape-account-system-tech-design-v2.md §2869（crypto.json 废弃决策）
