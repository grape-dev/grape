#!/usr/bin/env bash
# Grape 运营管理台部署脚本（本机 /opt/grape-admin）
# 用法: bash deploy.sh   （需 root；前提：mvn/npm/java/nginx 已装，/etc/grape/admin.env 已配置）
set -euo pipefail

REPO=/opt/grape
BACKEND_DIR="$REPO/admin/backend"
FRONTEND_DIR="$REPO/admin/frontend"
DEST=/opt/grape-admin

echo "[1/5] 构建后端 jar..."
(cd "$BACKEND_DIR" && mvn -B -q package -DskipTests)

echo "[2/5] 构建前端 dist..."
(cd "$FRONTEND_DIR" && npm run build >/dev/null)

echo "[3/5] 拷贝产物（前端先清空，防旧 bundle 堆积）..."
mkdir -p "$DEST/backend" "$DEST/frontend" "$DEST/logs"
cp "$BACKEND_DIR/target/admin-backend-0.1.0.jar" "$DEST/backend/"
cp "$BACKEND_DIR/ai_hermes_query.py" "$DEST/backend/"
rm -rf "$DEST/frontend/"*
cp -r "$FRONTEND_DIR/dist/"* "$DEST/frontend/"

echo "[4/5] 启动服务（jar 未变化则跳过后端重启，避免 502 窗口）..."
cp "$REPO/admin/deploy/admin-backend.service" /etc/systemd/system/
cp "$REPO/admin/deploy/nginx-grape-admin.conf" /etc/nginx/conf.d/grape-admin.conf
systemctl daemon-reload
NEW_SHA=$(sha256sum "$BACKEND_DIR/target/admin-backend-0.1.0.jar" | awk '{print $1}')
if [ "$NEW_SHA" != "$(cat "$DEST/backend/.jar_sha" 2>/dev/null || true)" ]; then
  echo "  jar 有变化 → 重启 admin-backend"
  systemctl enable --now admin-backend >/dev/null 2>&1 || systemctl restart admin-backend
  echo "$NEW_SHA" > "$DEST/backend/.jar_sha"
else
  echo "  jar 无变化 → 跳过后端重启（无 502 窗口）"
  systemctl is-active admin-backend >/dev/null || systemctl start admin-backend
fi
nginx -t >/dev/null && systemctl enable --now nginx >/dev/null 2>&1 || systemctl restart nginx

echo "[5/5] 健康检查..."
sleep 10
curl -sf http://127.0.0.1/api/admin/health && echo && echo "部署完成: http://<公网IP>/ (域名就绪后改 nginx server_name)"
