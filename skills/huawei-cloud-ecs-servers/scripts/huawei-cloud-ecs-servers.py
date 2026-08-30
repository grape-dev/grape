#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查询华为云 ECS 云服务器。

能力:
  list — 列出账号下 ECS 实例，可按名称/状态/规格过滤
  show — 查询单台服务器详情
  flavors — 查询 ECS 规格列表
  az — 查询可用区列表

认证:
  AK/SK 从项目知识（.project-info/ 下 JSON 文件）或环境变量获取。
  优先项目知识，缺失时扫描 HUAWEI/HW/HWC 前缀环境变量。

用法:
  python3 huawei-cloud-ecs-servers.py list --region cn-north-4 [--limit 10] [--name xxx] [--status ACTIVE]
  python3 huawei-cloud-ecs-servers.py show --region cn-north-4 --server-id <id>
  python3 huawei-cloud-ecs-servers.py flavors --region cn-north-4
  python3 huawei-cloud-ecs-servers.py az --region cn-north-4
  python3 huawei-cloud-ecs-servers.py health-check --region cn-north-4

退出码: 0=成功; 2=参数错误; 3=缺少认证; 4=API 调用失败
"""
import argparse, json, os, subprocess, sys, glob

VERSION = "0.1.0"

def _load_ak_sk():
    """获取 AK/SK——项目知识优先，环境变量回退。"""
    ak, sk = '', ''
    
    # 1. 项目知识：递归扫描 .project-info/ 下所有 JSON 文件
    for pattern in ["./.project-info/**/*.json", "../.project-info/**/*.json", "../../.project-info/**/*.json"]:
        for f in glob.glob(pattern, recursive=True):
            try:
                data = json.load(open(f))
                secrets = data.get("secrets", {})
                if "HUAWEI_AK" in secrets and not ak:
                    ak = secrets["HUAWEI_AK"]
                if "HUAWEI_SK" in secrets and not sk:
                    sk = secrets["HUAWEI_SK"]
            except:
                pass
    
    # 2. 环境变量回退
    if not ak or not sk:
        for k, v in os.environ.items():
            u = k.upper()
            if not (u.startswith('HUAWEI') or u.startswith('HW') or u.startswith('HWC')):
                continue
            if ('ACCESS_KEY' in u or u.endswith('_AK') or u == 'AK') and not ak:
                ak = v
            if ('SECRET_KEY' in u or u.endswith('_SK') or u == 'SK') and not sk:
                sk = v
    
    return ak, sk

def _ensure_hcloud():
    """确保 hcloud 已配置 AK/SK。"""
    ak, sk = _load_ak_sk()
    if not ak or not sk:
        print("错误：缺少认证，请设置华为云 AK/SK", file=sys.stderr)
        print("  方式一：export HUAWEI_AK=xxx; export HUAWEI_SK=xxx", file=sys.stderr)
        print("  方式二：.project-info/ 下 JSON 文件 secrets.HUAWEI_AK / secrets.HUAWEI_SK", file=sys.stderr)
        sys.exit(3)
    
    # 检查 hcloud 是否已配置正确的 AK
    try:
        result = subprocess.run(["hcloud", "configure", "list"], capture_output=True, text=True, timeout=10)
        if ak[:8] not in result.stdout:
            subprocess.run(["hcloud", "configure", "set", 
                            f"--access-key={ak}", f"--secret-key={sk}", "--region=cn-north-4"],
                           capture_output=True, timeout=10)
    except FileNotFoundError:
        print("错误：hcloud CLI 未安装", file=sys.stderr)
        sys.exit(4)
    except subprocess.TimeoutExpired:
        print("错误：hcloud configure 超时", file=sys.stderr)
        sys.exit(4)

def _run_cli(service, operation, args=None, timeout=30):
    """执行 hcloud CLI 命令。"""
    cmd = ["hcloud", service, operation]
    if args:
        cmd.extend(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            print(f"错误：hcloud 命令失败: {result.stderr}", file=sys.stderr)
            sys.exit(4)
        try:
            return json.loads(result.stdout)
        except:
            return {"raw": result.stdout}
    except subprocess.TimeoutExpired:
        print("错误：hcloud 命令超时（30s）", file=sys.stderr)
        sys.exit(4)

def _fmt(data, fmt="json"):
    if fmt == "md":
        if isinstance(data, dict):
            for key in ["servers", "flavors", "availabilityZoneInfo"]:
                items = data.get(key, [])
                if isinstance(items, list) and items:
                    cols = list(items[0].keys())[:8]
                    print("| " + " | ".join(cols) + " |")
                    print("| " + " | ".join(["---"]*len(cols)) + " |")
                    for item in items:
                        print("| " + " | ".join(str(item.get(c,""))[:50] for c in cols) + " |")
                    return
        print("```json")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print("```")
        return
    print(json.dumps(data, indent=2, ensure_ascii=False))

def cmd_list(args):
    _ensure_hcloud()
    cli_args = [f"--cli-region={args.region}", f"--limit={args.limit}"]
    if args.offset is not None:
        cli_args.append(f"--offset={args.offset}")
    if args.name:
        cli_args.append(f"--name={args.name}")
    if args.status:
        cli_args.append(f"--status={args.status}")
    if args.flavor:
        cli_args.append(f"--flavor={args.flavor}")
    r = _run_cli("ECS", "ListServersDetails", cli_args)
    _fmt(r, args.format)
    sys.exit(0 if r.get("count", 0) >= 0 else 4)

def cmd_show(args):
    _ensure_hcloud()
    cli_args = [f"--cli-region={args.region}", f"--server_id={args.server_id}"]
    r = _run_cli("ECS", "ShowServer", cli_args)
    _fmt(r, args.format)
    sys.exit(0)

def cmd_flavors(args):
    _ensure_hcloud()
    cli_args = [f"--cli-region={args.region}"]
    r = _run_cli("ECS", "ListFlavors", cli_args)
    _fmt(r, args.format)
    sys.exit(0)

def cmd_az(args):
    _ensure_hcloud()
    cli_args = [f"--cli-region={args.region}"]
    r = _run_cli("ECS", "NovaListAvailabilityZones", cli_args)
    _fmt(r, args.format)
    sys.exit(0)

def cmd_health(args):
    _ensure_hcloud()
    r = _run_cli("ECS", "NovaListAvailabilityZones", [f"--cli-region={args.region}"])
    az_count = len(r.get("availabilityZoneInfo", []))
    result = {"status": "ok", "service": "huawei-cloud-ecs", "version": VERSION, "az_count": az_count}
    _fmt(result, args.format)
    sys.exit(0)

def cmd_cap(args):
    caps = {"capability": "capability-list", "version": VERSION,
            "subcommands": ["health-check", "capability-list", "list", "show", "flavors", "az"],
            "subcommand_count": 6, "auth": "AK/SK", "region_env": "HUAWEI_REGION"}
    _fmt(caps, args.format)
    sys.exit(0)

def build_parser():
    p = argparse.ArgumentParser(prog="huawei-cloud-ecs-servers", description="查询华为云 ECS 云服务器")
    def ac(sp):
        sp.add_argument("--format", choices=["json", "md"], default="json", help="输出格式")
        sp.add_argument("--region", default=os.environ.get("HUAWEI_REGION", "cn-north-4"), help="区域")
    sub = p.add_subparsers(dest="command", required=True)
    
    ac(sub.add_parser("health-check", help="检查 API 可达性"))
    ac(sub.add_parser("capability-list", help="查看所有子命令"))
    
    pa = sub.add_parser("list", help="列出 ECS 服务器")
    pa.add_argument("--limit", type=int, default=10)
    pa.add_argument("--offset", type=int, default=None)
    pa.add_argument("--name", default=None)
    pa.add_argument("--status", default=None)
    pa.add_argument("--flavor", default=None)
    ac(pa)
    
    pa = sub.add_parser("show", help="查询单台服务器详情")
    pa.add_argument("--server-id", required=True)
    ac(pa)
    
    pa = sub.add_parser("flavors", help="查询 ECS 规格列表")
    ac(pa)
    
    pa = sub.add_parser("az", help="查询可用区列表")
    ac(pa)
    
    return p

DISPATCH = {
    "health-check": cmd_health, "capability-list": cmd_cap,
    "list": cmd_list, "show": cmd_show,
    "flavors": cmd_flavors, "az": cmd_az,
}

if __name__ == "__main__":
    args = build_parser().parse_args()
    DISPATCH[args.command](args)
