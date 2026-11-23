# Issue #3847：upstream keepalive 复用参数（在每个 `upstream { }` 块内 include）。
#
# 用法：
#   upstream grape_server {
#       server 127.0.0.1:38080;
#       include conf.d/nginx-grape-upstream-keepalive.inc;
#   }
#
# upstream 侧 keepalive_timeout 必须**大于**客户端侧（nginx-grape-upstream.inc 的 65s），
# 否则网关会复用 upstream 已关闭的空闲连接，表现为发布窗口内的 502 风暴。

keepalive 32;                # 每个 upstream 保活的空闲连接数
keepalive_timeout 90s;       # 90s > 客户端侧 65s，留出安全余量
keepalive_requests 1000;     # 单连接最大复用次数，避免长寿命连接累积状态