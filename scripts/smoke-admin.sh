#!/bin/bash
# Admin 冒烟测试 — 4 条关键路径
# 依赖：grape 服务运行在 localhost:3000
BASE="http://localhost:3000"
FAIL=0

echo "=== Admin Smoke Test ==="

# 1. admin 页面加载 + 包含 i18n.js + tokens.css
echo -n "1. Page loads with shared resources... "
resp=$(curl -s "$BASE/admin")
if echo "$resp" | grep -q 'i18n.js' && echo "$resp" | grep -q 'tokens.css'; then
  echo "PASS"
else
  echo "FAIL (missing i18n.js or tokens.css reference)"
  FAIL=1
fi

# 2. admin.json 语言包可加载（zh + en）
echo -n "2. admin.json locale packs loadable... "
zh_status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/static/locales/zh/admin.json")
en_status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/static/locales/en/admin.json")
if [ "$zh_status" = "200" ] && [ "$en_status" = "200" ]; then
  echo "PASS"
else
  echo "FAIL (zh=$zh_status en=$en_status)"
  FAIL=1
fi

# 3. tokens.css 可加载
echo -n "3. tokens.css loadable... "
tok_status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/static/css/tokens.css")
if [ "$tok_status" = "200" ]; then
  echo "PASS"
else
  echo "FAIL (status=$tok_status)"
  FAIL=1
fi

# 4. 健康面板 API 返回结构校验
echo -n "4. bot-token-health API structure... "
# 需要登录获取 token（简化：检查 endpoint 存在性）
health_status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/admin/bot-token-health")
if [ "$health_status" = "401" ] || [ "$health_status" = "200" ]; then
  echo "PASS (endpoint exists, status=$health_status)"
else
  echo "FAIL (status=$health_status, expected 200 or 401)"
  FAIL=1
fi

echo ""
if [ $FAIL -eq 0 ]; then
  echo "All smoke tests passed."
else
  echo "Smoke tests FAILED."
fi
exit $FAIL
