#!/bin/bash
# 共享文件消费者清单：同一资源被多个页面引用时，?v= 必须完全一致，
# 否则浏览器按 URL 缓存会出现「一个页面拿到新文件、另一个页面仍是旧缓存」的分叉。
# 格式：文件 → 消费者1,消费者2,...
declare -A SHARED_FILES=(
  ["tokens.css"]="admin.html,index.html"
  ["app.css"]="index.html,register.html,forgot.html"
  ["i18n.js"]="admin.html,index.html,register.html,forgot.html"
  ["api.js"]="index.html,register.html,forgot.html"
  ["pwd-blocklist.js"]="admin.html,index.html,register.html,forgot.html"
  ["app.js"]="index.html"
)

FAIL=0
for file in "${!SHARED_FILES[@]}"; do
  consumers="${SHARED_FILES[$file]}"
  IFS=',' read -ra arr <<< "$consumers"
  prev_ver=""
  prev_page=""
  for c in "${arr[@]}"; do
    ver=$(grep -oP "${file}\\?v=\\K[0-9a-zA-Z-]+" "src/api/frontend/shell/$c" 2>/dev/null | head -1)
    if [ -z "$ver" ]; then
      echo "WARN: $c does not reference $file"
      continue
    fi
    if [ -n "$prev_ver" ] && [ "$ver" != "$prev_ver" ]; then
      echo "FAIL: $file version mismatch: $prev_page=?v=$prev_ver vs $c=?v=$ver"
      FAIL=1
    fi
    prev_ver="$ver"
    prev_page="$c"
  done
done
exit $FAIL