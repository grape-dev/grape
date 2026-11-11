# D90-N3：bot token 通道加密——向前兼容方案（评审稿，2026-11-07）

**来源**：N2 派生挂账 (a)。用户拍板：可修复，但**必须向前兼容**。

**状态**：已确认并落地——f0442c2e8（写密读解/明文兼容/等值双形态/幂等
回填；bot_token_health verify 接线在性能补强批 b55268434）；测试
test_n3_bot_token_encryption（UC34）。

---

## 一、范围实码核对

**加密对象**：grape_t_repo_credentials.value + managed_repos 凭据列（双存储同步加密），
覆盖 8 个 cred_type（`_CRED_COLS`：bot_*/webhook_token，N2① 已锁此值域）。

**为什么 webhook_token 一起加密**：R14.1 语义 webhook_token ≡ bot token（写相同值），
明文等价于 bot token 明文——漏掉它=加密白做。

**读方清单**（改一处收口即可，实码核实）：
- `get_repo_credential_value`（db.py:2604）——grape_t_repo_credentials 唯一读口，
  8 处调用方全部经它
- `managed_repos` 列直读：token_resolver.py:924-928（RepoBotToken 回落）、
  db.py:1701/2368/2411（_update_repo_bot_token/webhook 重建）、repos.py 若干

**写方清单**：
- add_managed_repo（_cred_writes executemany）
- _update_repo_bot_token（UPDATE managed_repos 列）
- config_store global_bot_token_swap
- backfill（db_setup，存量搬迁）

## 二、向前兼容机制（复用 N1 已验证的同款）

credential_crypto.decrypt **天然明文兼容**（无冒号分隔符原样返回，design 已有
迁移期语义）——这正是向前兼容的钥匙：

1. **写侧**：4 个写入口 value 统一 `encrypt()` 后落库
2. **读侧**：`get_repo_credential_value` 返回前统一 `decrypt()`——
   历史明文行（无冒号）decrypt 原样返回 = **存量数据零迁移也能读**
3. **managed_repos 列同步**：同款读写包装（`_load_repo_secret`/`_store_repo_secret`
   helper，双方言两侧一致）

**回滚兼容**：新代码读明文旧行 ✓；旧代码读密文新行 ✗（会拿到密文串）——
即**升级后不可直接回滚到旧版本代码**（回滚前须跑反向脚本清空或将列值还原）。
这是向前兼容的边界，登记进 docstring。

## 三、幂等回填（存量明文行）

启动迁移（credentials_backfill_n1.py 同文件追加一步，复用其方言安全形态）：
- `SELECT repo_key, cred_type, value WHERE value NOT LIKE '%:%'`（明文判定：
  无两段冒号）
- 逐行 encrypt 写回；**幂等**（密文行含冒号跳过）
- managed_repos 列同款处理
- 已在 N1 修复的复合键定位（user_id,provider → repo_key,cred_type）沿用

## 四、验证面

- 实弹：旧 schema 库造明文行 → init_db → 回填加密 → 读侧回环 → 幂等重跑
- 明文兼容：加密后新增一行用旧值直插（模拟漏网明文）→ 读侧仍能读
- 宽域回归：repos 链/webhook 链/token 链守卫族

## 五、拍板请求

单方案（N1 同款机制，无选项）：4 写入口 encrypt + 2 读口 decrypt（明文兼容）
+ 启动幂等回填 + 回滚边界登记。**零 API 变更、零表结构变更、调用方零感知**。

~~回复"确认"即开工~~（已确认落地）。
