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
_failed=0

# 数据源 uid 映射：看板 JSON 里写的是某个环境的具体 uid，导入时按类型改写成目标环境的。
# 不设就用默认值（首次用 API 建出来的 uid 就是这几个）。
DS_UID_PROM="${GRAFANA_DS_PROM_UID:-prometheus}"
DS_UID_LOKI="${GRAFANA_DS_LOKI_UID:-loki}"
DS_UID_TEMPO="${GRAFANA_DS_TEMPO_UID:-tempo}"

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
  # 按 uid 或 name 判重：Grafana 会拒同名数据源，只按 uid 判会每次都报一次假失败
  if _api GET /api/datasources | grep -q "\"uid\":\"$uid\""; then
    echo "  ds $uid: 已存在，跳过"
    return 0
  fi
  if _api GET /api/datasources | grep -q "\"name\":\"$name\""; then
    echo "  ds $name: 同名已存在（uid 不同），跳过 —— 需要时用 GRAFANA_DS_*_UID 指过去"
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
  python3 - "$f" "$body" "$DS_UID_PROM" "$DS_UID_LOKI" "$DS_UID_TEMPO" <<'PY'
import json, sys
src, out = sys.argv[1], sys.argv[2]
ds_map = {"prometheus": sys.argv[3], "loki": sys.argv[4], "tempo": sys.argv[5]}


def remap(node):
    """把看板里按类型写死的数据源 uid 换成目标环境的 uid。"""
    if isinstance(node, dict):
        if isinstance(node.get("type"), str) and node["type"] in ds_map and "uid" in node:
            node["uid"] = ds_map[node["type"]]
        for v in node.values():
            remap(v)
    elif isinstance(node, list):
        for v in node:
            remap(v)
    return node


dash = remap(json.load(open(src, encoding="utf-8")))
json.dump({"dashboard": dash, "overwrite": True, "message": "imported from repo"},
          open(out, "w", encoding="utf-8"), ensure_ascii=False)
PY
  _resp="$(_api POST /api/dashboards/db "$body")"
  rm -f "$body"
  case "$_resp" in
    *'"status":"success"'*) echo "  看板 $_uid: 已导入（$_title）"; _shipped=$((_shipped+1)) ;;
    *) echo "  看板 $_uid: 失败 -> ${_resp:0:160}" >&2; _failed=$((_failed + 1)) ;;
  esac
done

# 4) 告警规则（alerts/*.json）：按 (folder, title) 幂等 upsert
#    不入库 repo 文件做任何改写 —— 文件夹归属与数据源 uid 都在内存里换成目标环境的值。
_ensure_folder() {  # name -> uid
  local name="$1"
  _api GET /api/folders | python3 -c 'import json,sys
try: fs=json.load(sys.stdin)
except Exception: fs=[]
print(next((f["uid"] for f in fs if f["title"]==sys.argv[1]), ""))' "$name"
}

_sync_alerts() {  # file folder_uid
  local f="$1" fuid="$2"
  local body
  body="$(mktemp)"
  python3 - "$f" "$body" "$fuid" "$DS_UID_PROM" "$DS_UID_LOKI" "$DS_UID_TEMPO" <<'PY'
import json, sys
src, out, fuid, prom, loki, tempo = sys.argv[1:7]
ds_map = {"prometheus": prom, "loki": loki, "tempo": tempo}
doc = json.load(open(src, encoding="utf-8"))
rules = doc.get("rules", doc if isinstance(doc, list) else [])
for r in rules:
    r["folderUID"] = fuid            # 只在内存里改，不写回 repo 文件
    for item in r.get("data", []):
        uid = item.get("datasourceUid")
        if uid in ds_map:
            item["datasourceUid"] = ds_map[uid]
json.dump(rules, open(out, "w", encoding="utf-8"), ensure_ascii=False)
PY
  _api GET /api/v1/provisioning/alert-rules | python3 - "$body" "$GRAFANA_URL" "$GRAFANA_AUTH" "$CHECK_ONLY" <<'PY'
import base64, json, sys, urllib.request, urllib.error

body_file, url, auth, check_only = sys.argv[1:5]
hdrs = {"Authorization": "Basic " + base64.b64encode(auth.encode()).decode(),
        "Content-Type": "application/json"}
want = json.load(open(body_file, encoding="utf-8"))


def req(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url + path, data=data, method=method, headers=hdrs)
    try:
        raw = urllib.request.urlopen(r, timeout=30).read()
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_body": e.read().decode()[:200]}


have = req("GET", "/api/v1/provisioning/alert-rules")
have = have if isinstance(have, list) else []
index = {(r.get("folderUID"), r.get("title")): r for r in have}
bad = 0
for r in want:
    key = (r.get("folderUID"), r.get("title"))
    cur = index.get(key)
    if check_only == "1":
        print(f"  规则 {r['title']}: {'已存在（未比对内容）' if cur else '缺失（--check，不写）'}")
        continue
    r.pop("updated", None)
    if cur:
        r["uid"] = cur["uid"]
        res = req("PUT", "/api/v1/provisioning/alert-rules/" + cur["uid"], r)
        print(f"  规则 {r['title']}: 更新")
    else:
        r.pop("uid", None)
        res = req("POST", "/api/v1/provisioning/alert-rules", r)
        print(f"  规则 {r['title']}: 新建")
    if isinstance(res, dict) and res.get("_err"):
        bad += 1
        print(f"    失败 {res['_err']} {res.get('_body', '')}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
  local rc=$?
  rm -f "$body"
  [ "$rc" -ne 0 ] && _failed=$((_failed + 1))
}

if [ -d "$REPO/deploy/grafana/alerts" ]; then
  GRAFANA_FOLDER="${GRAFANA_FOLDER:-Grape}"
  _fuid="$(_ensure_folder "$GRAFANA_FOLDER")"
  if [ -z "$_fuid" ] && [ "$CHECK_ONLY" != "1" ]; then
    _fuid="$(_api POST /api/folders "$(python3 -c 'import json,sys;print(json.dumps({"title":sys.argv[1]}))' "$GRAFANA_FOLDER")" \
      | python3 -c 'import json,sys;print(json.load(sys.stdin).get("uid",""))')"
  fi
  for f in "$REPO"/deploy/grafana/alerts/*.json; do
    [ -f "$f" ] || continue
    _sync_alerts "$f" "${_fuid:-}"
  done
fi

if [ "$_failed" -ne 0 ]; then
  echo "[grafana] 失败 ${_failed} 项（见上）" >&2
  exit 1
fi

if [ "${CHECK_ONLY}" = "1" ]; then
  echo "[grafana] --check 完成，未写入任何东西"
else
  echo "[grafana] 完成，导入 ${_shipped} 个看板"
fi
