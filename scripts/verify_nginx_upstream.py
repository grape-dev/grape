#!/usr/bin/env python3
"""只读校验 nginx 网关 ``/api/runtime*`` 上游端口归属（Issue #3764）。

复用 ``deploy_preflight.py`` 的配置解析纯函数，不改写任何文件、不 reload nginx，
可在现网节点（如 ecs-grape-master-001）部署前后直接执行：

    python3 scripts/verify_nginx_upstream.py --nginx-conf /etc/nginx/conf.d/grape.conf
    nginx -T > /tmp/nginx-T.txt && python3 scripts/verify_nginx_upstream.py --nginx-dump /tmp/nginx-T.txt

退出码：0 = 通过（或未探测到配置而 skip）；1 = 上游错配/无法解析；2 = 参数/读取错误。

断言边界：只解析配置文件内的字面 upstream ``server`` 行；``include`` 注入的
peers 文件（如 ``/etc/nginx/conf.d/grape-worker-peers.conf*``）为人工维护，
不在断言覆盖内——可用 ``nginx -T`` 转储（会展开 include）人工复核其中端点。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_preflight as dp  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="只读校验 nginx /api/runtime* 上游端口归属")
    ap.add_argument("--nginx-conf", default="", help="nginx 配置文件路径")
    ap.add_argument("--nginx-dump", default="", help="`nginx -T` 转储文件路径（与 --nginx-conf 等价）")
    ap.add_argument("--ports", default=os.environ.get("GRAPE_PROCESS_PORTS", ""),
                    help="覆盖端口事实源，如 worker=8083 或 mgmt=8081,event=8082,...")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    try:
        ports = dp.parse_ports(args.ports)
    except ValueError as exc:
        print(f"verify_nginx_upstream: {exc}", file=sys.stderr)
        return 2
    try:
        sources = dp.load_nginx_sources(args.nginx_conf or None, args.nginx_dump or None)
    except OSError as exc:
        print(f"verify_nginx_upstream: 读取 nginx 配置失败: {exc}", file=sys.stderr)
        return 2

    check = dp.check_upstream_role_sources(sources, ports=ports)
    source = ", ".join(s.label for s in sources)
    if args.json:
        print(json.dumps({"source": source, "pass": check.ok, **check.as_dict()},
                         ensure_ascii=False, indent=2))
    else:
        print(f"{'PASS' if check.ok else 'FAIL'}  {check.name}  {check.detail}")
        if check.hint:
            print(f"      ↳ {check.hint}")
    return 0 if check.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
