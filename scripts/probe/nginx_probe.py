#!/usr/bin/env python3
"""Grape 统一探针（部署位置：<deploy-dir>/nginx_probe.py，crontab 每分钟执行；仓库内保留同一份便于重建）

Grape 统一探针 —— 每分钟探测 nginx（两节点）、node_exporter 与 LGTM 组件，经 OTLP 写入观测栈。

为什么需要它：
- Prometheus 只抓本机（Alloy 在容器里，非 root 改不了 scrape 配置），兄弟机没有 Prometheus；
- Grafana 没有 JSON/Infinity 数据源，把探针结果标准化成 Prometheus 指标是无侵入路径；
- OTLP :4318 由 otel-lgtm 内的 collector 接收并转成 Prometheus 指标。

产出指标：
  label node=hd002|vod
    nginx_probe_up / http_status / latency_ms        入口可用性（/api/health）
    nginx_conn_active / reading / writing / waiting   nginx stub_status 连接数
    host_probe_up / net_rx_bps / net_tx_bps / tcp_established / load1 / mem_available_ratio
  label upstream=<local-upstream>|<remote-upstream>
    lb_requests_5m / lb_share_percent               LB 近 5 分钟分发量与占比（从 Loki 反查）
  label component=grafana|prometheus|loki|tempo|alloy
    lgtm_probe_up / http_status / latency_ms          LGTM 各组件健康

用法：* * * * * /usr/bin/python3 <deploy-dir>/nginx_probe.py
"""
import json
import os
import os
import time
import urllib.request

OTLP = "http://127.0.0.1:4318/v1/metrics"
STATE = "/tmp/nginx_probe_state.json"

def _load_nodes() -> list:
    """节点清单：环境变量 PROBE_NODES（JSON 数组）优先，默认只探本机。"""
    import json as _json
    raw = os.environ.get("PROBE_NODES", "").strip()
    if not raw:
        return [{"node": "local", "nginx": "http://127.0.0.1:38080",
                 "exporter": "http://127.0.0.1:9100"}]
    try:
        nodes = _json.loads(raw)
    except Exception:
        return []
    return nodes if isinstance(nodes, list) else []


NODES = _load_nodes()

# LGTM 观测栈（容器或 systemd 部署，地址均由本机回环访问）
LGTM = [
    {"component": "grafana", "url": "http://127.0.0.1:3000/api/health"},
    {"component": "prometheus", "url": "http://127.0.0.1:9090/-/ready"},
    {"component": "loki", "url": "http://127.0.0.1:3100/ready"},
    {"component": "tempo", "url": "http://127.0.0.1:3200/ready"},
    {"component": "alloy", "url": "http://127.0.0.1:12345/-/ready"},
]


def _get(url, timeout=5):
    req = urllib.request.Request(url, headers={"User-Agent": "grape-probe/2"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _parse_exporter(text):
    out = {}
    for line in text.splitlines():
        if line.startswith('node_network_receive_bytes_total{device="eth0"}'):
            out["rx"] = float(line.rsplit(" ", 1)[1])
        elif line.startswith('node_network_transmit_bytes_total{device="eth0"}'):
            out["tx"] = float(line.rsplit(" ", 1)[1])
        elif line.startswith("node_netstat_Tcp_CurrEstab"):
            out["tcp"] = float(line.rsplit(" ", 1)[1])
        elif line.startswith("node_load1 "):
            out["load1"] = float(line.rsplit(" ", 1)[1])
        elif line.startswith("node_memory_MemAvailable_bytes"):
            out["mem_avail"] = float(line.rsplit(" ", 1)[1])
        elif line.startswith("node_memory_MemTotal_bytes"):
            out["mem_total"] = float(line.rsplit(" ", 1)[1])
    return out


def _parse_stub_status(text):
    """解析 nginx stub_status 纯文本（Reading/Writing/Waiting 的值是独立 token）。"""
    import re as _re
    out = {}
    m = _re.search(r"Active connections:\s*(\d+)", text)
    if m:
        out["active"] = float(m.group(1))
    m = _re.search(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s*$", text, _re.M)
    if m:
        out["accepts"] = float(m.group(1))
        out["handled"] = float(m.group(2))
        out["requests"] = float(m.group(3))
    m = _re.search(r"Reading:\s*(\d+)\s+Writing:\s*(\d+)\s+Waiting:\s*(\d+)", text)
    if m:
        out["reading"] = float(m.group(1))
        out["writing"] = float(m.group(2))
        out["waiting"] = float(m.group(3))
    return out



LOKI_QUERY = "http://127.0.0.1:3100/loki/api/v1/query"
LB_QUERY = ('sum by (upstream) (count_over_time({job="nginx-lb"} | upstream=~".+" | upstream!~".*," [5m]))')


def _lb_share():
    """从 Loki 反查 LB 近 5 分钟按上游分发的条数，换算成绝对量与百分比。"""
    import urllib.parse
    url = LOKI_QUERY + "?" + urllib.parse.urlencode({"query": LB_QUERY, "time": int(time.time())})
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        rows = data.get("data", {}).get("result", []) or []
        counts = {}
        for r in rows:
            up = (r.get("metric") or {}).get("upstream") or ""
            if not up:
                continue
            try:
                counts[up] = float(r["value"][1])
            except Exception:
                continue
        total = sum(counts.values())
        out = []
        for up, cnt in counts.items():
            out.append(("lb_requests_5m", cnt, {"upstream": up}))
            if total > 0:
                out.append(("lb_share_percent", round(cnt * 100.0 / total, 1), {"upstream": up}))
        return out
    except Exception:
        return []


def collect():
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:
            state = {}

    metrics = []  # (name, value, {labels})
    now = time.time()

    for spec in NODES:
        node = spec["node"]
        labels = {"node": node}

        # ① 入口可用性
        up, code, latency = 0, 0, 0.0
        t0 = time.perf_counter()
        try:
            code, _ = _get(spec["nginx"] + "/api/health", timeout=5)
            latency = (time.perf_counter() - t0) * 1000
            up = 1 if code < 500 else 0
        except Exception:
            latency = (time.perf_counter() - t0) * 1000
            up, code = 0, 0
        metrics += [("nginx_probe_up", up, labels),
                    ("nginx_probe_http_status", code, labels),
                    ("nginx_probe_latency_ms", round(latency, 1), labels)]

        # ② nginx 连接数（stub_status）
        try:
            _, body = _get(spec["nginx"] + "/nginx_status", timeout=5)
            st = _parse_stub_status(body.decode("utf-8", "ignore"))
            for key, name in (("active", "nginx_conn_active"), ("reading", "nginx_conn_reading"),
                              ("writing", "nginx_conn_writing"), ("waiting", "nginx_conn_waiting")):
                if key in st:
                    metrics.append((name, st[key], labels))
            if "requests" in st:
                metrics.append(("nginx_requests_cum", st["requests"], labels))
            metrics.append(("nginx_status_up", 1, labels))
        except Exception:
            metrics.append(("nginx_status_up", 0, labels))

        # ③ 节点资源
        try:
            _, body = _get(spec["exporter"] + "/metrics", timeout=5)
            cur = _parse_exporter(body.decode("utf-8", "ignore"))
            prev = state.get(node, {})
            rx_bps = tx_bps = None
            if prev.get("ts") and cur.get("rx") is not None and prev.get("rx") is not None:
                dt = max(1e-3, now - float(prev["ts"]))
                rx_bps = max(0.0, (cur["rx"] - prev["rx"]) / dt)
                tx_bps = max(0.0, (cur["tx"] - prev["tx"]) / dt)
            metrics.append(("host_probe_up", 1, labels))
            if cur.get("tcp") is not None:
                metrics.append(("host_tcp_established", cur["tcp"], labels))
            if cur.get("load1") is not None:
                metrics.append(("host_load1", cur["load1"], labels))
            if cur.get("mem_avail") is not None and cur.get("mem_total"):
                metrics.append(("host_mem_available_ratio", cur["mem_avail"] / cur["mem_total"], labels))
            if rx_bps is not None:
                metrics += [("host_net_rx_bps", rx_bps, labels), ("host_net_tx_bps", tx_bps, labels)]
            state[node] = {"ts": now, "rx": cur.get("rx"), "tx": cur.get("tx")}
        except Exception:
            metrics.append(("host_probe_up", 0, labels))

    # ④ LGTM 组件健康
    for spec in LGTM:
        comp = spec["component"]
        labels = {"component": comp}
        up, code, latency = 0, 0, 0.0
        t0 = time.perf_counter()
        try:
            code, _ = _get(spec["url"], timeout=5)
            latency = (time.perf_counter() - t0) * 1000
            up = 1 if code < 500 else 0
        except Exception:
            latency = (time.perf_counter() - t0) * 1000
            up, code = 0, 0
        metrics += [("lgtm_probe_up", up, labels),
                    ("lgtm_probe_http_status", code, labels),
                    ("lgtm_probe_latency_ms", round(latency, 1), labels)]

    metrics += _lb_share()

    return metrics, state


def push(metrics):
    ts = str(int(time.time() * 1e9))
    points = {}
    for name, value, labels in metrics:
        key = (name, tuple(sorted(labels.items())))
        points.setdefault(key, []).append({
            "asDouble": float(value), "timeUnixNano": ts,
            "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in labels.items()],
        })
    payload = {"resourceMetrics": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "grape-probe"}}]},
        "scopeMetrics": [{"metrics": [{"name": name, "gauge": {"dataPoints": pts}}
                                      for (name, _labels), pts in points.items()]}],
    }]}
    req = urllib.request.Request(OTLP, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def main():
    metrics, state = collect()
    try:
        json.dump(state, open(STATE, "w"))
    except Exception:
        pass
    code = push(metrics)
    if code >= 300:
        print(f"probe push failed: {code}")


if __name__ == "__main__":
    main()