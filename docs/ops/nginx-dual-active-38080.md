# 213 公网入口 38080：双节点双活（2026-11-16）

## 拓扑

```
公网 120.46.213.1:38080 (EIP → NAT → 192.168.4.245)
  └─ nginx/213  sites-enabled/grape-lb.conf        ← 唯一入口（listen 38080 只有 1 个 server 块）
       upstream grape_pool_sticky { ip_hash; 127.0.0.1:38090; 192.168.0.116:38080; }
       upstream grape_pool_rr     { 127.0.0.1:38090; 192.168.0.116:38080; }
       /api/events|chat|task-logs|runtime|tasks → sticky（有状态/SSE/认领）
       其余                                       → rr（无状态）
          ├─ 213:38090 内层 nginx（grape-cluster.conf）→ 38081 mgmt / 38082 event / 38083 worker / 38084 insight
          └─ 116:38080 内层 nginx（同构）              → 38081..38084
```

两节点共享同一套三库：`grape_stage_mgmt@213:3311` / `grape_stage_runtime@116:3307` / `grape_stage_insight@213:3312`。
`JWT_SECRET` 两侧 .env md5 一致（`5b9a1785…`），会话令牌落 `session_api_tokens`（共享库）。

## 为什么要去重

`grape-cluster.conf` 里曾有一个 `listen 38080` 的外层块（`upstream grape_lb` 只指本机 38090），
与 `grape-lb.conf` 的 38080 块冲突：nginx 只让先加载的那个当默认 server，于是
`grape-lb.conf` 的 `grape_pool_sticky/rr`（含 116）**一直是死配置**，116 从未真正收到流量。
去重动作：删掉 `grape-cluster.conf` 的外层 38080 块与 `grape_lb` upstream，只保留它的内层 38090 块；
`grape-lb.conf` 成为唯一入口（同时补 `location /internal/ { deny all; return 404; }`）。

验证：`nginx -T | grep -c "listen 38080;"` → **1**；`nginx -T | grep -c "listen 38090;"` → 1。

## 关键指令

```
proxy_next_upstream error timeout http_502 http_503 http_504 non_idempotent;
proxy_next_upstream_tries 3;
max_fails=1 fail_timeout=15s;        # 节点重启 → 一次失败即摘除 15s
add_header X-LB-Upstream $upstream_addr always;   # 观测落到哪一跳（含重试链）
```

`non_idempotent` 是必需的：nginx 默认不重试 POST，节点重启窗口里用户会直接看到 502
（实测 8 次登录里 3 次 502，`upstream=` 只有 116 一跳、无重试）。加上后回归 8/8 200。

## 粘滞与灰度

- 粘滞：`ip_hash`，同一客户端 IP 固定同节点（SSE/任务流/认领都在 sticky 池）。
- 灰度：改 `weight`（如 213 weight=9 / 116 weight=1 → 约 10% 客户端切到 116），
  验证完再恢复 1:1；`ip_hash` 的权重只影响客户端落到哪个节点的概率。

## 验收记录（2026-11-16 08:1x）

```
nginx -t                        → successful
listen 38080 server 块          → 1（去重完成）
落地节点分布（登录 ×10）         → 213:5 / 116:5
会话一致性（token 复用 ×20）     → /api/auth/me 100% 200（无 401 抖动）
数据一致性                      → /api/chat/config 两侧 md5 相同（658b65e7…）
                                  /api/chat/conversations 两侧同 8065 字节
粘性                            → 同一源 IP 连续 4 次全部 192.168.0.116 / 全部本机（不跳）
故障切换                        → 停 116 mgmt，登录 ×6 全 200（经 213）；重启后 116 回到 5/5 轮转
内部端点                        → /internal/ 404（两层都拒）
公网入口                        → SPA 200 / login 200
```

## 回滚

```bash
# 配置回滚（备份为去重前的两份原件）
cp /root/grape-cluster.conf.bak-20261116-081131 /etc/nginx/sites-available/grape-cluster.conf
cp /root/grape-lb.conf.bak-20261116-081131      /etc/nginx/sites-available/grape-lb.conf
nginx -t && systemctl reload nginx

# 只摘 116（保留新结构）
sed -i '/192.168.0.116:38080/d' /etc/nginx/sites-available/grape-lb.conf && nginx -t && systemctl reload nginx
```

## 双活相关的前置条件（都已满足）

1. 两节点 `JWT_SECRET` 一致（否则 A 节点签发的 JWT 在 B 节点 401）。
2. 三库共享、`session_api_tokens` 共享（否则同一 token 跨节点失效）。
3. 两节点静态资源版本一致（`app.js?v=266` / `chat-view.js?v=58`）。
4. 两节点 `SWEEPER_LOCK_MODE=own`（`/etc/systemd/system/grape@.service.d/20-sweeper-lock.conf`），
   sweeper 互斥靠 MySQL `GET_LOCK`，避免两节点双跑清扫。