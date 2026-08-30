# poll 注入 webhook 端口 bug 修复方案

> 版本：v1.0
> 日期：2026-08-30
> 状态：**已分析，待实施**（含结论、根因、修复点、验证）
> 影响版本：`new-version3`（`main` 同源）

---

## 1. 现象

`data/logs/devops-bot.log` 持续刷大量 WARNING（每轮 poll 每个新 issue 一条）：

```
poll: inject failed for #N: <urlopen error [Errno 111] Connection refused>
```

**级别分布**（当日文件）：INFO 30839 / **WARNING 10462** / ERROR 2 ——WARNING 中绝大多数是本条。

## 2. 根因

`src/runtime/cleanup.py` `_poll_gitcode_issues_once` 硬编码注入地址为不存在的端口：

```python
# cleanup.py:589-590
webhook_req = _urllib.Request(
    f"http://127.0.0.1:38080/webhook/gitcode",   # ← 端口 38080 无人监听
    ...
```

**实测端口**（生产，`ss -tlnp`）：

| 端口 | 监听 | 用途 |
|---|---|---|
| `8080` | ✅ pid 2388556 | webhook 服务（`run_webhook.py` → `port=8080`） |
| `3000` | ✅ pid 2388555 | 主 API |
| `38080` | ❌ **无** | 代码误指向 |

- `git log --follow cleanup.py`：该行自引入后从未修改，一直是 `38080`。
- 仓库中仅 `redis-cache-analysis.md` 提及 `0.0.0.0:38080`（老架构描述），服务实际早已迁到 `8080/3000`，但 cleanup 注入地址未同步。
- 结果：GitCode issue **webhook 兜底注入永远失败**（真实 webhook 若丢事件则无法补注入），并刷 WARNING。

> ⚠️ 与飞书 WS 消息无关（飞书走 WS 长连接）；这是独立的上游事件兜底链路故障。

## 3. 权威 webhook 地址来源（已验证）

生产 `get_webhook_url()` 实际返回（优先级：`configs/server-config.json` > meta 表 > 空串）：

```json
// configs/server-config.json
{
  "server_base_url": "https://grape.example.com",
  "webhook_url": "https://grape.example.com/webhook/gitcode"
}
```

外网实测：`POST https://grape.example.com/webhook/gitcode` 返回 **400**（缺 token/body 即被校验拦截，但连接通、webhook 逻辑可达）→ 说明**用 HTTPS 外网地址注入是可行的**（nginx → 8080）。

## 4. 修复方案

**文件**：`src/runtime/cleanup.py` `_poll_gitcode_issues_once`

**替换** `38080` 硬编码为权威地址：

```python
from src.user.db import get_webhook_url

# ...循环内、构造 payload 后：
_wh = get_webhook_url()
if not _wh:
    log.warning("poll: webhook_url 未配置，跳过注入")
    continue

webhook_req = _urllib.Request(
    _wh,                                     # ← 替换 f"http://127.0.0.1:38080/webhook/gitcode"
    data=payload,
    headers={
        "Content-Type": "application/json",
        "X-GitCode-Token": webhook_token or "poll",
        "X-Gitcode-Event": "issue_opened",
    },
    method="POST")
```

要点：
- 复用 `src.user.db.get_webhook_url()`（读 `configs/server-config.json` 的 `webhook_url`），**不引入新硬编码**。
- 保留现有 token 校验头（`webhook_token or "poll"`）。
- 注入走 HTTPS → nginx → 8080，与真实 GitCode webhook 同链路，语义一致。

## 5. 可选项（建议但不阻塞）

1. **文档校正**：`src/api/static/redis-cache-analysis.md` 中 `0.0.0.0:38080` 为过时描述，同步更正。
2. **降噪（独立优化）**：`_poll_gitcode_issues_once` 对"DB 已有 event"的 issue 在 `continue` 分支不打日志，减少重复放大；以及 poll 失败/WARNING 聚合。与本次修复可分开评估。

## 6. 影响与风险

- **范围**：仅改 webhook 注入目标地址，不涉及飞书 WS、不涉及 systemd/端口/nginx 配置。
- **收益**：
  - 恢复 GitCode issue 兜底注入（webhook 丢事件时不再漏）；
  - 消除 `poll: inject failed ... Connection refused` 刷屏（预计当日 WARNING 显著下降）。
- **验证**：
  1. 重启服务后，日志不再出现 `inject failed ... Connection refused`；
  2. 出现 `poll: injected issue #N -> seq=…` 即表示兜底注入已生效；
  3. 抽查 `events` 表出现 poll 补入的 `issue_opened` 记录。

---

## 附：相关路径速查

- 修复文件：`src/runtime/cleanup.py:589-590`（`_poll_gitcode_issues_once`）
- 权威来源：`src/user/db.py:1382` `get_webhook_url()` → `src/config.py:read_server_config` → `configs/server-config.json`
- 关联过时文档：`src/api/static/redis-cache-analysis.md`（38080 描述）
- 日志位置：`data/logs/devops-bot.log`（JSON，实时）；旧文本 `data/devops-bot.log` 已不再写入