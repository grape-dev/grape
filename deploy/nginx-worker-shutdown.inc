# Issue #3847：主 nginx.conf（main 上下文）优雅退出窗口。
#
# `worker_shutdown_timeout` 只允许出现在 main 上下文（events/http 之外），因此本文件
# 不能放进 conf.d/*.conf（会被 http 上下文自动 include 而报错）。它是 `.inc`，需在
# 主配置 `/etc/nginx/nginx.conf` 顶部（`worker_processes` 之后、`events` 之前）手动 include：
#
#   include /etc/nginx/conf.d/nginx-worker-shutdown.inc;
#
# 语义：worker 收到 SIGTERM 后最多再等待 30s 让在途请求完成再强制退出；与
# deploy/systemd/*.service 的 TimeoutStopSec=45 对齐（systemd 再多给 15s 余量）。
# 配合发布脚本「摘流 → 排空 → 退出」确保 reload/重启不直接切断在途连接。

worker_shutdown_timeout 30s;