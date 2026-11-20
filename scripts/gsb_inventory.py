#!/usr/bin/env python
"""GSB 可远程调用接口清单（代码侧权威 + 现网观测标记）。

用途：
  1. `python scripts/gsb_inventory.py`                → 打印 markdown 表
  2. `python scripts/gsb_inventory.py --json`         → 机器可读
  3. `python scripts/gsb_inventory.py --update-dashboard`
       → 把清单写进 Grafana 看板 `grape-gsb-bus` 的面板「GSB 可远程调用接口清单」

清单来源不是文档，而是 attach_internal_routes() 真正挂载的 router 对象
（mgmt=readonly+xwrite / runtime / insight），因此不会漂移。
「观测」列来自 Prometheus 的 grape_http_requests_total{route=~"/internal.*"}。
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DOMAINS = ("mgmt", "runtime", "insight")


def load_routers(domain: str):
    if domain == "mgmt":
        from src.domains.mgmt.routes.internal import readonly_router, xwrite_router
        return [("mgmt read-only", readonly_router), ("mgmt xwrite/apply", xwrite_router)]
    if domain == "runtime":
        from src.domains.runtime.routes.internal import router
        return [("runtime", router)]
    from src.domains.insight.routes.internal import router
    return [("insight", router)]


def collect():
    rows = []
    for domain in DOMAINS:
        for group, router in load_routers(domain):
            for r in getattr(router, "routes", []):
                path = getattr(r, "path", "")
                if not path.startswith("/internal/"):
                    continue
                methods = sorted(m for m in (getattr(r, "methods", set()) or set()) if m not in ("HEAD", "OPTIONS"))
                if not methods:
                    continue
                summary = (getattr(r, "summary", "") or getattr(r, "name", "") or "").strip()
                op_id = getattr(r, "operation_id", "") or ""
                rows.append({
                    "domain": domain, "group": group,
                    "method": "/".join(methods), "path": path,
                    "operation_id": op_id, "summary": summary,
                })
    rows.sort(key=lambda x: (DOMAINS.index(x["domain"]), x["path"]))
    return rows


def observed(prom: str):
    """route -> 6h 调用次数（Prometheus 现网观测）"""
    q = 'sum by (route) (increase(grape_http_requests_total{route=~"/internal.*"}[6h]))'
    url = prom.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": q})
    try:
        d = json.load(urllib.request.urlopen(url, timeout=15))
    except Exception as e:
        print(f"[warn] Prometheus 查询失败（观测列将留空）: {e}", file=sys.stderr)
        return {}
    out = {}
    for x in (d.get("data") or {}).get("result") or []:
        out[x["metric"].get("route", "")] = float(x["value"][1])
    return out


def markdown(rows, obs):
    lines = [
        "| 域 | 方法 | 接口 | 6h 调用 | 说明 |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for r in rows:
        n = obs.get(r["path"])
        hit = f"{n:,.0f}" if n else "—"
        path = r["path"]
        warn = ""
        if "{repo_key" in path:
            warn = " ⚠️ repo_key 在路径段（含 : 和 / 会被切段 → 404），跨机调用请改 query 参数"
        lines.append(f"| {r['domain']} | {r['method']} | `{path}` | {hit} | {r['summary'][:52]}{warn} |")
    total = len(rows)
    used = sum(1 for r in rows if obs.get(r["path"]))
    lines.append("")
    lines.append(f"> 代码侧共 **{total}** 个可远程调用的 `/internal/*` 接口；6 小时窗口内"
                 f"**{used}** 个被真实调用（其余为 0 = 尚无调用方，属正常基线）。")
    return "\n".join(lines)


PANEL_TITLE = "GSB 可远程调用接口清单（代码侧 · 谁暴露了什么）"


def update_dashboard(md: str, gf: str, panel_id: int = 23, folder_uid: str = "grape"):
    import base64
    auth = "Basic " + base64.b64encode(b"admin:admin").decode()

    def api(path, data=None):
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        r = urllib.request.Request(gf.rstrip("/") + path, data=body,
                                   method="POST" if data else "GET",
                                   headers={"Authorization": auth, "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(r, timeout=30))

    db = api("/api/dashboards/uid/grape-gsb-bus")["dashboard"]
    desc = ("由 scripts/gsb_inventory.py 从 attach_internal_routes() 实际挂载的 router 生成，"
            "不是手写文档：mgmt=只读白名单+xwrite/apply，runtime/insight=各自 internal router。"
            "「6h 调用」列取自 Prometheus `grape_http_requests_total{route=~\"/internal.*\"}`。")
    rows_n = md.count("\n| ")
    height = min(64, 12 + rows_n)   # markdown 表格每行约 1 个 grid 单位（含表头与统计行）
    _read = next((p for p in db["panels"] if p["title"].startswith("怎么读")), None)
    for p in db["panels"]:
        if p["id"] == panel_id:
            p["options"] = {"mode": "markdown", "content": md}
            p["description"] = desc
            p["gridPos"]["h"] = height
            # 高度会随接口数变化：把「读法说明」等后续面板整体下移，避免重叠
            if _read is not None:
                _read["gridPos"]["y"] = p["gridPos"]["y"] + height
            break
    else:
        y = _read["gridPos"]["y"] if _read else max(
            p["gridPos"]["y"] + p["gridPos"]["h"] for p in db["panels"])
        if _read:                       # 清单插在「怎么读」之前，读法说明顺延
            _read["gridPos"]["y"] = y + height
        db["panels"].append({
            "id": panel_id, "type": "text", "title": PANEL_TITLE, "description": desc,
            "gridPos": {"h": height, "w": 24, "x": 0, "y": y},
            "options": {"mode": "markdown", "content": md},
        })
    res = api("/api/dashboards/db", {"dashboard": db, "overwrite": True,
                                     "folderUid": folder_uid, "message": "刷新 GSB 可远程调用接口清单"})
    print(f"[grafana] 已更新看板 version={res.get('version')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--prom", default=os.environ.get("PROM_URL", "http://192.168.0.116:9090"))
    ap.add_argument("--update-dashboard", action="store_true")
    ap.add_argument("--grafana", default=os.environ.get("GRAFANA_URL", "http://192.168.0.116:3000"))
    a = ap.parse_args()

    rows = collect()
    obs = observed(a.prom) if not a.json else {}
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    md = markdown(rows, obs)
    print(md)
    if a.update_dashboard:
        update_dashboard(md, a.grafana)


if __name__ == "__main__":
    main()