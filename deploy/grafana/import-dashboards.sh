#!/usr/bin/env bash
# 把仓库里的 Grafana 看板导入到运行中的 Grafana —— 幂等，可反复跑。
#
# 为什么需要这个脚本：LGTM 容器没有挂卷（`docker run` 没带 -v），
# 容器一重建，Grafana 里的数据源和看板全部消失。看板只存在 Grafana 里、
# 不进仓库 = 随时会丢。所以 JSON 放在 deploy/grafana/dashboards/，
# 重建后跑一遍这个脚本就恢复。
#
# 用法：
#   bash deploy/grafana/import-dashboards.sh                      # 默认 127.0.0.1:3000, admin:admin
#   GRAFANA_URL=http://<sibling-host>:3000 GRAFANA_AUTH=admin:pw \
#     bash deploy/grafana/import-dashboards.sh
#   GRAFANA_URL=... bash deploy/grafana/import-dashboards.sh --check   # 只查差异不写
#
# 设计取舍：
#   · 数据源按 uid 固定（prometheus / loki / tempo / pyroscope / grape-mysql），
#     看板里的 expr 直接引用这些 uid；缺哪个补哪个，已存在的跳过（不覆盖你手改的配置）。
#   · 看板用 overwrite=true 导入 —— 以仓库为准，本地临时改动会被覆盖，这正是想要的。
#   · 全部走 Grafana HTTP API，不依赖 grafana-cli、不依赖容器内文件系统。
set -u

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DASH_DIR="$REPO/deploy/grafana/dashboards"
GRAFANA_URL="${GRAFANA_URL:-http://127.0.0.1:3000}"
GRAFANA_AUTH="${GRAFANA_AUTH:-admin:admin}"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

_api() {  # _api METHOD PATH [BODY_FILE]
  local m="$1" p="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -s -m 20 -u "$GRAFANA_AUTH" -X "$m" "$GRAFANA_URL$p" \
      -H 'Content-Type: application/json' --data-binary "@$body"
  else
    curl -s -m 20 -u "$GRAFANA_AUTH" -X "$m" "$GRAFANA_URL$p"
  fi
}

echo "[grafana] $GRAFANA_URL"

# 1) 可达性
if ! curl -s -m 8 -o /dev/null -w '' "$GRAFANA_URL/api/health"; then
  echo "[grafana] FAIL: 连不上 $GRAFANA_URL —— 先确认 LGTM 容器起来了" >&2
  exit 1
fi

# 2) 数据源（缺则补，已有则跳过）
_ensure_ds() {  # name uid type url [database] [user]
  local name="$1" uid="$2" typ="$3" url="$4" db="${5:-}" usr="${6:-}"
  local body
  if _api GET /api/datasources | grep -q "\"uid\":\"$uid\""; then
    echo "  ds $uid: 已存在，跳过"
    return 0
  fi
  if [ "$CHECK_ONLY" = "1" ]; then
    echo "  ds $uid: 缺失（--check，不写）"
    return 0
  fi
  body="$(mktemp)"
  python3 - "$body" "$name" "$uid" "$typ" "$url" "$db" "$usr" <<'PY'
import json, sys
out, name, uid, typ, url, db, usr = sys.argv[1:8]
d = {"name": name, "uid": uid, "type": typ, "access": "proxy",
     "url": url, "isDefault": False, "jsonData": {"httpMethod": "POST"}}
if db:
    d["database"] = db
    d["jsonData"]["timeInterval"] = "30s"
if usr:
    d["user"] = usr
json.dump(d, open(out, "w"), ensure_ascii=False)
PY
  _api POST /api/datasources "$body" | head -c 160; echo
  rm -f "$body"
}

_ensure_ds "Prometheus" "prometheus" "prometheus" "http://127.0.0.1:9090"
_ensure_ds "Loki"       "loki"       "loki"       "http://127.0.0.1:3100"
_ensure_ds "Tempo"      "tempo"      "tempo"      "http://127.0.0.1:3200"

# 3) 看板
_shipped=0
for f in "$DASH_DIR"/*.json; do
  [ -f "$f" ] || continue
  _title="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["title"])' "$f")"
  _uid="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("uid",""))' "$f")"
  if [ "$CHECK_ONLY" = "1" ]; then
    # 不能截断判断：Grafana 的响应开头是 meta，uid 在后面 → 截 60 字符会恒定判"缺失"
    if _api GET "/api/dashboards/uid/$_uid" | grep -q '"dashboard"'; then
      echo "  看板 $_uid: 已存在（未比对内容）"
    else
      echo "  看板 $_uid: 缺失（--check，不写）"
    fi
    continue
  fi
  body="$(mktemp)"
  python3 - "$f" "$body" <<'PY'
import json, sys
src, out = sys.argv[1], sys.argv[2]
json.dump({"dashboard": json.load(open(src, encoding="utf-8")),
           "overwrite": True, "message": "imported from repo"}, open(out, "w", encoding="utf-8"), ensure_ascii=False)
PY
  _resp="$(_api POST /api/dashboards/db "$body")"
  rm -f "$body"
  case "$_resp" in
    *'"status":"success"'*) echo "  看板 $_uid: 已导入（$_title）"; _shipped=$((_shipped+1)) ;;
    *) echo "  看板 $_uid: 失败 -> ${_resp:0:160}" >&2 ;;
  esac
done

if [ "$CHECK_ONLY" = "1" ]; then
  echo "[grafana] --check 完成，未写入任何东西"
else
  echo "[grafana] 完成，导入 $_shipped 个看板"
fi