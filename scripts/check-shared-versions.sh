#!/bin/bash
# 共享文件消费者清单：同一资源被多个页面引用时，?v= 必须完全一致，
# 否则浏览器按 URL 缓存会出现「一个页面拿到新文件、另一个页面仍是旧缓存」的分叉。
# 只做跨页一致性；「只升不降」由 tests/api/test_frontend_cache_version_ratchet.py
# （精确冻结映射）与 tests/api/test_i18n_locale_cache_ratchet.py（历史下界）负责。
# 格式：文件 → 消费者1,消费者2,...
declare -A SHARED_FILES=(
  ["tokens.css"]="admin.html,index.html"
  ["app.css"]="index.html,register.html,forgot.html"
  ["i18n.js"]="admin.html,index.html,register.html,forgot.html"
  ["api.js"]="index.html,register.html,forgot.html"
  ["pwd-blocklist.js"]="admin.html,index.html,register.html,forgot.html"
)

FAIL=0
for file in "${!SHARED_FILES[@]}"; do
  consumers="${SHARED_FILES[$file]}"
  IFS=',' read -ra arr <<< "$consumers"
  # 至少有 2 个消费者才有比较意义（单消费者条目是 no-op）
  if [ "${#arr[@]}" -lt 2 ]; then
    echo "WARN: $file 只有 ${#arr[@]} 个消费者，跨页一致性检查无意义"
    continue
  fi
  prev_ver=""
  prev_page=""
  for c in "${arr[@]}"; do
    [ -f "src/api/frontend/shell/$c" ] || continue
    # 取该页全部引用（同一页重复引用必须同版本，只取首处会漏判）
    mapfile -t vers < <(grep -oP "${file}\\?v=\\K[0-9a-zA-Z-]+" "src/api/frontend/shell/$c" 2>/dev/null | sort -u)
    if [ "${#vers[@]}" -eq 0 ]; then
      echo "WARN: $c does not reference $file"
      continue
    fi
    if [ "${#vers[@]}" -gt 1 ]; then
      echo "FAIL: $c 内 $file 出现多个版本: ${vers[*]}"
      FAIL=1
      continue
    fi
    ver="${vers[0]}"
    if [ -n "$prev_ver" ] && [ "$ver" != "$prev_ver" ]; then
      echo "FAIL: $file version mismatch: $prev_page=?v=$prev_ver vs $c=?v=$ver"
      FAIL=1
    fi
    prev_ver="$ver"
    prev_page="$c"
  done
done
exit $FAIL