#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
领域 RPC 冒烟探测脚本（domain-qa 验收使用，接管 coding C7 后端冒烟）
========================================
通过 Gateway WebChannel WS（默认 ws://127.0.0.1:19000/ws）按前后端真实协议
（webClient.ts：req 帧 `{"type":"req","id":...,"method":...,"params":{...}}`，
res 帧 `{"type":"res","id":...,"ok":...,"payload":...,"error":...,"code":...}`）
顺序发送一串 RPC，逐条断言收到 ok:true 的 res 回包。
（曾归属 domain-coding C7.2；v3.0 起随 C7 验收移交 domain-qa，权威维护在
  skills/domain-qa/references/scripts/ws_smoke.py。）

用法（服务已启动时）:
    python ws_smoke.py --ws ws://127.0.0.1:19000/ws \
        --method gov.policy.import --params '{"title":"冒烟","doc_no":"SMK-001","body":"x"}' \
        --method gov.policy.list --params '{}' --expect-key items

    --method 可重复：按顺序逐个发起，每个都必须收到 {ok:true,...} 回包才算通过。
    --expect-key <字段> 可重复（与 --method 位置对应）：断言该 RPC 成功回包的 payload 是
        JSON 对象且**直接包含指定业务字段**（如 items/total/records）。用于拦截「WS payload
        整包下发 {ok,data} 双层包裹」类缺陷（finance 实坑：数据库已落库但前端 res.items 取空、
        页面显示"暂无资产"；正确契约是 payload 下发业务数据本体 result.get("data")）。
    --timeout 单条等待上限（秒，默认 15）。

内置检查:
    若某方法成功回包 payload 恰为 {"ok":..., "data":...} 双层包裹（dispatch_<domain>_rpc
    返回结构被整包透传），直接判 FAIL 并提示疑似整包下发——即便未传 --expect-key。

退出码:
    0  全部 RPC 均收到 ok:true 回包且满足 payload 结构断言
    1  任一 RPC 缺回包 / 回包 ok=false / payload 结构不符 / 连接失败 / 参数错误
"""
import argparse
import asyncio
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


async def call(ws, method: str, params: dict, req_id: str, timeout: float):
    frame = {"type": "req", "id": req_id, "method": method, "params": params or {}}
    await ws.send(json.dumps(frame, ensure_ascii=False))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return {"ok": False, "error": f"等待回包超时(>{timeout}s): {method}", "code": "TIMEOUT"}
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") == "res" and data.get("id") == req_id:
            return data


async def run(args) -> int:
    import websockets

    if not args.method:
        print("[FAIL] 至少需要一个 --method <domain>.<x>.<y>")
        return 1

    failures = []
    try:
        async with websockets.connect(args.ws, open_timeout=10) as ws:
            for i, method in enumerate(args.method, start=1):
                req_id = f"smoke_{i}_{method.replace('.', '_')}"
                params = {}
                if i - 1 < len(args.params):
                    raw = args.params[i - 1]
                    if raw:
                        try:
                            params = json.loads(raw)
                        except json.JSONDecodeError as e:
                            print(f"[FAIL] --params {raw!r} JSON 解析失败: {e}")
                            return 1
                        if not isinstance(params, dict):
                            print(f"[FAIL] --params {raw!r} 必须是 JSON 对象 {{...}}")
                            return 1
                print(f"[..] {method} <- {json.dumps(params, ensure_ascii=False)}")
                res = await call(ws, method, params, req_id, args.timeout)
                if res.get("ok"):
                    payload = res.get("payload")
                    note = f"payload={json.dumps(payload, ensure_ascii=False)[:200]}"
                    # 内置检查：payload 恰为 {ok, data} 双层包裹 = dispatch 返回结构被整包下发（finance 实坑）
                    if (isinstance(payload, dict) and set(payload) == {"ok", "data"}):
                        msg = (f"{method} -> payload 疑似 {{ok,data}} 双层整包下发 "
                               f"{json.dumps(payload, ensure_ascii=False)[:200]}")
                        print(f"[FAIL] {msg}")
                        print("       正确契约：WS payload 必须是业务数据本体 result.get('data')，"
                              "禁止 send_response(..., payload=result or {})——否则前端 res.items 取空、页面'暂无资产'")
                        failures.append(msg)
                        continue
                    # 按位置取该方法的期望业务字段（若有）
                    expect_key = args.expect_key[i - 1] if i - 1 < len(args.expect_key) else None
                    if expect_key:
                        if isinstance(payload, dict) and expect_key in payload:
                            note = f"payload.{expect_key}={json.dumps(payload.get(expect_key), ensure_ascii=False)[:120]} " + note
                        else:
                            msg = (f"{method} -> payload 缺少业务字段 {expect_key!r} "
                                   f"(payload={json.dumps(payload, ensure_ascii=False)[:200]})")
                            print(f"[FAIL] {msg}")
                            print("       前端组件直接取 res.<业务字段>；若后端整包下发 {ok,data} 双层包裹则取空，"
                                  "页面显示'暂无资产'。payload 应为业务数据本体（result.get('data')）。")
                            failures.append(msg)
                            continue
                    print(f"[PASS] {method} -> ok:true {note}")
                else:
                    msg = f"{method} -> ok:false error={res.get('error') or res.get('code') or res}"
                    print(f"[FAIL] {msg}")
                    failures.append(msg)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] WS 连接/收包异常: {e}")
        return 1

    if failures:
        print(f"[FAIL] 未通过的 RPC 共 {len(failures)} 条")
        return 1
    print(f"[PASS] 全部 {len(args.method)} 条 RPC 收到 ok:true 回包")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="领域 RPC 冒烟探测（Gateway WebChannel WS）")
    ap.add_argument("--ws", default="ws://127.0.0.1:19000/ws", help="WebChannel WS 地址（Vite 代理 /ws 指向 gateway 19000）")
    ap.add_argument("--method", action="append", required=True,
                    help="需探测的 RPC 方法名（可重复，按顺序执行），如 --method gov.policy.search")
    ap.add_argument("--params", action="append", default=[],
                    help="与 --method 位置对应的 JSON 对象参数（可重复）；缺省用 {}")
    ap.add_argument("--timeout", type=float, default=15.0, help="单条回包等待上限秒数（默认 15）")
    ap.add_argument("--expect-key", action="append", default=[],
                    help="断言该 RPC 成功回包 payload 直接包含的业务字段（可重复，与 --method 位置对应）")
    args = ap.parse_args()
    if args.expect_key and len(args.expect_key) != len(args.method):
        print(f"[FAIL] --expect-key 数量({len(args.expect_key)})必须与 --method 数量({len(args.method)})一致（按位置对应）")
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())