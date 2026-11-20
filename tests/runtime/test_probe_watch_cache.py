"""Engine-probe cache/watch semantics anchors (user ruling 2026-11-20).

Three coupled behaviours are pinned here as source anchors (the code lives
inside long-lived coroutines, same style as test_provider_report_pending.py):

1. A template that probes OK but has no engines is a SUCCESS:
   ``_probe_templates_engines`` must append it to the result list with
   ``engines=[]`` (→ reported upstream + written to the probe-state negative
   cache with ok=true), and the caller must count pending items from the
   probe function's exact failure count — not from the old
   "scanned - engine-bearing" diff, which made every engine-less template
   count as forever-pending and kept the retry loop spinning every 1800s,
   re-creating sandboxes for nothing.
2. The probe retry loop must NOT exit after all pending items clear: it
   enters a watch mode (PROBE_WATCH_INTERVAL) and re-runs the report round,
   so a template added on the platform side after worker startup is still
   auto-discovered without a worker restart. The negative cache guarantees
   already-probed templates (engine-bearing or engine-less) are skipped, so
   a watch round only pays the list_templates scan plus probing new ids.
3. Fail counting inside the probe itself must be explicit
   (probe-failure counter + truncated-deferred count), never inferred from
   result-list length.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "src" / "domains" / "runtime" / "engine" / "cloud_worker.py"


def _worker_src() -> str:
    return WORKER.read_text(encoding="utf-8")


def _probe_func_slice(src: str) -> str:
    start = src.find("async def _probe_templates_engines(")
    assert start != -1, "_probe_templates_engines 缺失"
    end = src.find("from src.domains.runtime.engine.client import RuntimeClient", start)
    assert end != -1
    return src[start:end]


def test_engineless_template_is_probed_success_and_cached():
    src = _worker_src()
    seg = _probe_func_slice(src)
    # 无引擎的成功探测必须进结果列表（engines=[]）→ 上报 + 负缓存 ok=true
    assert 'out.append({"template_id": _tid, "engines": []})' in seg, \
        "无引擎模板未以 engines=[] 进结果列表——会导致对账永远差 1、循环永不停"
    # 旧差值对账公式必须不存在（存在即回归）
    assert "_probe_pre - len(_tpl_engines)" not in src, \
        "差值对账公式回归：无引擎模板会被误计为 pending"


def test_probe_returns_exact_failure_count():
    src = _worker_src()
    seg = _probe_func_slice(src)
    assert "tuple[list, int]" in src, "探测函数必须返回 (结果, 失败计数) 二元组"
    # 建箱异常显式计数
    assert "_probe_failed += 1" in seg, "建箱探测异常未计入失败计数"
    # 截断未探的模板计入失败（交给重试/看护轮次）
    assert "_deferred = _pre_cap - len(_tid_list)" in seg, \
        "截断未探的模板未计入失败计数"
    assert "return out, _probe_failed + _deferred" in seg, \
        "返回值必须携带精确失败计数"
    # 调用方消费返回的失败计数，不再自算差值
    assert "_failed += _probe_failed_cnt" in src, \
        "调用方未消费探测函数的精确失败计数"


def test_retry_loop_watches_for_new_templates_after_success():
    src = _worker_src()
    start = src.find("async def _probe_retry_loop():")
    assert start != -1, "_probe_retry_loop 缺失"
    end = src.find("asyncio.create_task(_probe_retry_loop())", start)
    assert end != -1
    seg = src[start:end]
    # 循环不得退出（看护模式）：全成功后周期重跑，新模板 id 不在负缓存 → 自动探测
    assert "while True:" in seg, "重试循环退出了——启动后平台新增模板将永不被发现"
    assert "while _pending:" not in seg, "退避条件仍作为外层循环（会退出）"
    assert 'PROBE_WATCH_INTERVAL' in src, "看护间隔必须可配（PROBE_WATCH_INTERVAL）"
    assert "retry loop exited" not in src, "循环退出语义残留"
    # 看护轮次复用 _report_providers_once——负缓存在函数内部挡住已探模板
    assert "_pending = await _report_providers_once()" in seg, \
        "看护轮次必须复用同一上报+探测入口（负缓存由其内部消费）"


def test_refresh_loop_triggers_probe_on_platform_template_change():
    src = _worker_src()
    start = src.find("async def _template_refresh_loop():")
    assert start != -1, "_template_refresh_loop 缺失"
    end = src.find("async def _cloud_run_loop", 0) if start == -1 else \
        src.find("_refresh_task = asyncio.create_task", start)
    assert end != -1
    seg = src[start:end]
    # refresh 循环（60s）顺路做轻量平台扫描，模板 id 新增 → set 事件唤醒探测循环
    assert 'config.get("probe_watch_trigger")' in seg, \
        "refresh 循环必须读取探测唤醒事件（跨作用域经共享 config 传递）"
    assert "_last_platform_tids" in seg, "平台模板 id 快照缺失——无法做变更检测"
    assert "_evt.set()" in seg, "发现新模板 id 必须 set 事件唤醒探测循环"
    # 首轮只建快照不触发（启动探测批已覆盖）
    assert "_pk in _last_platform_tids" in seg, \
        "变更 diff 必须排除首轮基线（否则每轮误触发）"
    # 扫描失败沿用旧快照（瞬时平台故障不掩盖后续新增模板）
    assert "_seen[_pk] = _old_ids or set()" in seg, \
        "扫描失败必须沿用旧快照——否则平台恢复后新增 id 无法被 diff 出"
    # 同步网络调用放线程池（60s 高频路径不阻塞事件循环/心跳调度）
    assert "await asyncio.to_thread(_ad.list_templates" in seg, \
        "list_templates 同步调用必须 to_thread（会阻塞 60s 循环内的心跳/claim 调度）"
    # 探测侧必须有对应等待端（事件+超时兜底），两端缺一即死代码
    assert 'asyncio.wait_for(_watch_evt.wait(), timeout=_probe_watch_interval)' in src, \
        "探测循环缺少事件等待端（refresh 侧 set 了也没人消费）"
