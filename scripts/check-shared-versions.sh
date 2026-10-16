#!/bin/bash
# 共享文件消费者清单
# 格式：文件 → 消费者1,消费者2,...
declare -A SHARED_FILES=(
  ["tokens.css"]="admin.html,index.html"
  ["i18n.js"]="admin.html,index.html"
)

FAIL=0
for file in "${!SHARED_FILES[@]}"; do
  consumers="${SHARED_FILES[$file]}"
  IFS=',' read -ra arr <<< "$consumers"
  prev_ver=""
  for c in "${arr[@]}"; do
    ver=$(grep -oP "${file}\?v=\K[0-9a-zA-Z-]+" "src/api/static/$c" 2>/dev/null | head -1)
    if [ -z "$ver" ]; then
      echo "WARN: $c does not reference $file"
      continue
    fi
    if [ -n "$prev_ver" ] && [ "$ver" != "$prev_ver" ]; then
      echo "FAIL: $file version mismatch: ${arr[0]}=?v=$prev_ver vs $c=?v=$ver"
      FAIL=1
    fi
    prev_ver="$ver"
  done
done
exit $FAIL
