# Issue #1063 根因排查：git 凭据注入接口间歇 500/502

> 排查日期：2026-11-06 | 目标端点：`GET /api/repos/{owner}/{repo}/git-credential`
> （`operation_id=repos_git_credential`，`src/api/routes/repos.py`）

## 1. 结论摘要

**当前证据不足以判定唯一根因**：Issue 只提供了调用侧现象（3 次、间隔 15s 的换券请求全部失败，60s 后自愈），
未附带**服务端同一 `request_id` 的日志**、上游 GitCode OAuth 响应码、DB 锁等待时间或前置代理访问日志。
仅凭现有信息无法把 H1~H5 中任一假设提升为定论。

代码走查可确认两点**已确定的缺陷（本次已修复）**：

1. `_repo_platform_token` 的两个 token 获取闭包 `_bot()` / `_user()` 均以 `except Exception: return ""`
   吞掉全部异常，且全函数**零日志**。无论 DB 瞬时错误、凭据解密失败还是上游刷新异常，调用方只能看到统一的
   `502 {"code":502,"message":"GitCode 仓库 token 解析失败"}`，无法区分阶段，也无法重试判断。
2. 失败响应没有 `stage` / `error_code` / `retryable` / `retry_after_s`，CLI 只能静默回落原生 git，
   导致 push 失败且无告警（CLI 侧告警与重试见 grape-dev/grape-cli#69）。

因此本次交付先补齐**失败可解释 + 失败可观测**，并把根因定位所需的最小证据集固化为采集清单（§5）。

> 说明：从反馈现象看，调用侧记录的是 **HTTP 500**，而本端点既有的“token 均缺失”分支返回的是 **HTTP 502**。
> 两者不一致本身就是一个关键线索：说明失败**并未落在现有的 502 分支**，更可能是请求在进入/离开
> `_repo_platform_token` 前后由未捕获异常（全局 500 handler）或前置代理产生。本次新增的结构化日志可把
> 下一次故障直接分流到具体阶段。

## 2. 代码走查事实（修复前）

| 位置 | 事实 | 影响 |
|------|------|------|
| `src/api/routes/repos.py:2606` `_repo_platform_token` | 全函数无任何 `log` 调用 | 故障无服务端现场 |
| `src/api/routes/repos.py:2620` `_bot()` | `except Exception: return ""` | DB/解密/查询异常被吞，退化为“token 为空” |
| `src/api/routes/repos.py:2626` `_user()` | `except Exception: return ""` | 上游 OAuth 刷新异常、分布式锁异常被吞 |
| `src/api/routes/repos.py:2666-2667` | 两者均空时抛 `502` 固定文案 | 无法区分缺失 / 异常 / 上游故障 |
| `src/api/deps.py:547` `get_user_token_with_refresh` | 刷新失败可能返回旧 token 或空；锁异常有 warning | 上游 401/5xx 与锁竞争会表现为空 token |
| `src/api/middleware.py:42` | 请求日志仅记录 method/path/status/duration | 无凭据阶段维度，无法按 stage 聚合 |
| `src/api/exception_handlers.py:17` | 全局 500 返回通用文案 + `request_id` | 500 现场只能靠 request_id 关联，修复前无阶段日志 |

## 3. 假设逐条核对

| 假设 | 描述 | 现有证据 | 判定 |
|------|------|----------|------|
| H1 | 上游 GitCode OAuth 刷新抖动（5xx/超时） | 60s 后自愈与 OAuth 瞬时故障形态吻合；`get_user_token_with_refresh` 刷新失败会回落旧 token 或空 | **未证实**，需上游响应码/耗时 |
| H2 | DB 瞬时错误（SQLite busy/locked、解密失败）被 `except Exception` 吞 | 代码路径确实吞异常；但吞掉后走的是 **502**，与调用侧观测到的 **500** 不完全吻合 | **未证实（代码缺陷已修复可观测）**，需 DB 锁等待日志 |
| H3 | 分布式锁 `oauth_refresh:{uid}:gitcode` 获取失败/等待超时后刷新降级 | `src/api/deps.py` 锁失败有 warning 且会直接 `refresh_oauth_token`；可能放大上游压力 | **未证实**，需锁等待/失败日志 |
| H4 | bot token 缺失触发交叉回退，回退用户 token 也失败 | 交叉回退失败在修复前只能得到统一 502；无法判断是“缺失”还是“异常” | **未证实**，需 stage 日志（本次已补齐） |
| H5 | 前置代理（nginx/网关）返回 502/500 | 调用侧记录 500；代理层超时/连接重置常见且可 60s 自愈 | **未证实**，需代理 access/error log 与上游耗时 |

交叉判断：H2/H4 在本端点内应表现为 **502**，而反馈为 **500**，因此 **H1（上游抖动导致请求整体超时后被代理转 500/502）**
与 **H5（代理层）** 的可能性相对更高；但在拿到同一 `request_id` 的三层（代理 → 应用 → 上游）日志前不下定论。

## 4. 复现探针（15s × 3 换券）

用于在故障窗口内确认“是否可重试、哪一阶段失败”：

```bash
# 使用会话级临时 token（tk_），每 15s 调一次，共 3 次；不打印 token 明文
for i in 1 2 3; do
  date -u +%FT%TZ
  curl -sS -o /tmp/gc_cred_$i.json -w 'http=%{http_code} time=%{time_total}\n' \
    -H "Authorization: Bearer ${GRAPE_SESSION_TOKEN}" \
    "${GRAPE_SERVER_URL}/api/repos/${REPO_OWNER}/${REPO_NAME}/git-credential"
  sleep 15
done
```

判读：
- 返回 `502` 且 `detail.stage=bot_token_lookup` → H4/H2（bot 读取/DB）。
- 返回 `502` 且 `detail.stage=user_token_refresh` → H1/H3（上游刷新/锁）。
- 返回 `502` 且 `detail.stage=both_unavailable` → 凭据确实缺失（非抖动）。
- 返回 `200` 但下一跳 git 失败 → 问题在 token 有效性，不在注入端点。
- 返回 `500` + `request_id` → 按 §5 用 request_id 拉三层日志。
- 响应体任何字段出现 token 明文 → 立即按泄露事件处理（本次已加测试守卫）。

## 5. 证据采集清单（按 request_id 关联）

故障发生时请采集以下最小集合（脱敏后附到 Issue）：

1. **应用日志**：`request_id=<rid>` 的完整记录，重点字段
   `git_credential_failed` / `stage` / `error_class` / `provider` / `repo_key` / `auth_type` / `agent_type` / HTTP `status` / `duration_ms`。
2. **上游 GitCode OAuth/API**：换券窗口内刷新请求的 HTTP 状态码与耗时（401/429/5xx/超时）。
3. **DB**：同窗口 `database is locked` / busy timeout / 解密失败相关日志与慢查询。
4. **分布式锁**：`oauth_refresh:*` 的获取等待时长、失败/降级记录。
5. **前置代理**：nginx/网关 access log（`upstream_status`、`request_time`）与 error log（超时、连接重置）。
6. **时间对齐**：调用侧失败时间（反馈为 3×15s）与服务端日志 UTC 时间戳对齐。

## 6. 本次修复后的观测口径

- 失败统一为 `502`（非 500），`detail` 含 `stage`（`bot_token_lookup` / `user_token_refresh` /
  `both_unavailable`）、`error_code=GIT_CREDENTIAL_UNAVAILABLE`、`retryable=true`、`retry_after_s=15`、`provider`。
- 服务端日志事件 `git_credential_failed` 为单行 JSON，含上述阶段与异常类名，不含 token 明文。
- 未预期异常仍由全局 handler 返回 `500 + request_id`，并在抛出点记录 `stage=credential_resolve` 与异常类名。
- 服务端**不重试**（避免放大上游压力），有限次带间隔重试与告警由 grape-cli#69 负责。

## 7. 后续动作

- 按 §5 采集一次真实故障现场后回填本节，将 H1~H5 收敛为单一根因与修复项。
- 若确认上游 OAuth 抖动（H1/H3），评估刷新重试/熔断指标；若确认代理层（H5），调整网关超时与重试策略。
- CLI 侧闭环由 grape-dev/grape-cli#69 跟踪。