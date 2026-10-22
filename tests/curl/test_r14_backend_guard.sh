#!/bin/bash
# R14 后端守卫测试：无邮件配置时 3 个发码接口返回 503
# 用法: bash tests/curl/test_r14_backend_guard.sh <jwt_token>
# 前置: 服务运行在 localhost:3000，未配置邮件服务
# 获取 token: 登录后从浏览器 localStorage.getItem('token') 复制

BASE="http://localhost:3000"
TOKEN="${1:-}"
if [ -z "$TOKEN" ]; then
  echo "Usage: bash test_r14_backend_guard.sh <jwt_token>"
  echo "获取 token: 登录后从浏览器 localStorage.getItem('token') 复制"
  exit 1
fi

passed=0
failed=0

check_503() {
  local name=$1
  local url=$2
  local data=$3
  local status
  if [ -n "$data" ]; then
    status=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$url" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d "$data")
  else
    status=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$url" \
      -H "Authorization: Bearer $TOKEN")
  fi
  if [ "$status" = "503" ]; then
    echo "  ✓ $name → 503 EMAIL_NOT_CONFIGURED"
    passed=$((passed + 1))
  else
    echo "  ✗ $name → got $status (expected 503)"
    failed=$((failed + 1))
  fi
}

echo "R14 后端守卫测试"
echo "================="
check_503 "set-password/send-code" "$BASE/api/auth/set-password/send-code" "{}"
check_503 "change-email/send-code" "$BASE/api/auth/change-email/send-code" '{"new_email":"test@example.com"}'
check_503 "delete-account/send-code" "$BASE/api/auth/delete-account/send-code" ""

echo ""
echo "$passed passed, $failed failed"
[ $failed -eq 0 ]
