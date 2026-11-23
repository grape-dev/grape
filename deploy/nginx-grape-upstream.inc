# Issue #3847：网关发布/热加载窗口 502 风暴加固——公共反代重试与连接复用片段。
#
# 用法：在每个 `server { }` 内 include（location 可覆盖本文件的默认值）：
#   include conf.d/nginx-grape-upstream.inc;
#
# 背景：10:12–10:14 发布窗口内网关对健康 upstream 回 1,173 次 502（5xx 率 1.63%，
# 基线 0.077%），而同一时段上游 `GET /api/health`、`/static/openapi.json` 仍 200。
# 根因是 reload/摘流瞬间在途请求与 keepalive 连接被切断：网关既未重试，也复用了
# 对端已关闭的连接。本片段统一开启「失败重试下一个 upstream」并让 HTTP/1.1 长连接
# 真正可复用；upstream 侧 keepalive 参数见 nginx-grape-upstream-keepalive.inc。
#
# keepalive 对齐：客户端侧 65s < upstream 侧 90s（见 keepalive.inc），
# 保证网关不会复用对端已按更短超时关闭的连接。
#
# ⚠️ Connection 头必须在**每个反代 location 内**显式重设：nginx 的 proxy_set_header
# 按「层级整体覆盖」继承——location 内只要出现任意一条 proxy_set_header，server 级
# 这里设的 `Connection ""` 就整体失效，upstream 侧会重新收到 `Connection: close`，
# keepalive 池退化为每次新建连接。模块级为 `proxy_set_header Connection "";`，
# WS/SSE 升级类 location 改用 `map $http_upgrade $grape_connection_upgrade` 条件切换
# （四份模板已在文件头声明该 map）。改模板时新增 location 务必同步，契约用例
# tests/contracts/test_issue_3847_gateway_graceful.py 会校验。

keepalive_timeout 65s;                 # 客户端侧 keepalive，须小于 upstream 侧 90s
proxy_http_version 1.1;                # upstream keepalive 仅 HTTP/1.1 生效
proxy_set_header Connection "";        # 仅对「location 未定义 proxy_set_header」生效，见上

# 发布窗口内上游被重启/摘流时，对健康上游自动重试一次（tries 2 = 首次 + 1 次重试）。
# 只对连接类错误与 502/503 重试，避免把业务 4xx 放大成重复写请求（POST 等非幂等方法
# 默认不重试，不会重复写入）。
#
# ⚠️ 生效前提：upstream 必须 ≥2 个 peer。nginx 不会把请求重投给同一个 peer，单 peer
# upstream 下 tries 2 完全空转（实测仍只有 1 次连接尝试）。四份模板因此把本机地址
# 声明成两个同址 peer——发布窗口内被切断的 GET 在进程恢复后可命中第二次尝试；
# 多实例部署（LB/多副本）时请改成真实的多副本地址而非同址重复。
proxy_next_upstream error timeout http_502 http_503;
proxy_next_upstream_tries 2;
proxy_next_upstream_timeout 10s;