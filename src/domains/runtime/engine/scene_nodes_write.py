"""跨域写：场景节点单行写入（P3-4b）。

`devops_pipeline_scene` 是 **mgmt 基表**；runtime 进程（event/worker）改它的
`nodes_json` 属于跨域写，统一走 `mgmt lib + cross_write 幂等意图`，lib 不可用时
回落到调用方原有的本地游标 SQL（存量视图路径，零行为变化）。

三个调用点（P3-4b 接线）：
- `recovery.py::_readd_scene_bindings`   加回引擎时把 rid 补进节点
- `engine_select.py::cleanup_scene_engine_bindings`  移除已删引擎的绑定
- `engine_select.py::remap_scene_engine_bindings`    引擎行替换后重映射 rid

语义约定（重要）：
- **内容哈希幂等**：`set_scene_nodes` 按内容哈希比对，同内容直接 skipped；
- **expected_hash 只用于 lib 侧**：值是调用方读到的旧内容哈希（sha1 前 12 位）。
  不一致 = 期间有别的写入者改了该场景 → 本变换基于陈旧快照，**跳过而不回写**，
  避免把并发修改（如 admin 改场景节点）覆盖掉；
- **意图键** `scene-nodes:{row_id}:{new_hash}`：同一内容重复提交不重复执行。意图
  payload 只带最终内容与行号，不含 expected_hash —— 补偿重试时按幂等内容落地。
"""
from __future__ import annotations

import hashlib

# 返回码
UPDATED = "updated"      # lib 写成功（行已变更）
SKIPPED = "skipped"      # 内容已一致，无需写
MISMATCH = "mismatch"    # expected_hash 不一致（并发写已生效），本次不写
FAILED = "failed"        # lib 写失败（意图已登记，等 drain 补偿）
LOCAL = "local"          # lib 不可用，已用本地游标原生 SQL 写入


def content_hash(text: str) -> str:
    """与 mgmt lib `set_scene_nodes` 相同的哈希口径（sha1 hex 前 12 位）。"""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def write_scene_nodes(nodes_json: str, row_id: int, expected_hash: str, c,
                      now_iso: str, log=None, action: str = "scene_nodes_update") -> str:
    """写单个场景行的 nodes_json，返回 UPDATED/SKIPPED/MISMATCH/FAILED/LOCAL。

    c / now_iso：调用方持有的本地游标与时间戳，仅 lib 不可用时用于原生 SQL 回落。
    """
    from src.kernel.domain_read import mgmt_lib

    _mg = mgmt_lib(foreign_only=True)
    if _mg is None:
        c.execute(
            "UPDATE devops_pipeline_scene SET nodes_json=?, updated_at=? WHERE id=?",
            (nodes_json, now_iso, row_id))
        return LOCAL

    from src.kernel import cross_write

    _out: dict = {}

    def _apply() -> None:
        _out.update(_mg.set_scene_nodes(nodes_json, row_id=int(row_id),
                                        expected_hash=expected_hash or None))

    key = f"scene-nodes:{row_id}:{content_hash(nodes_json)}"
    res = cross_write.submit(
        "mgmt", action, key, {"rid": int(row_id), "nodes_json": nodes_json}, _apply)
    if res.get("status") == "duplicate":
        # 同内容已提交过（上次成功或仍在补偿队列）→ 视为无需再写
        return SKIPPED
    if res.get("status") == "failed":
        if log is not None:
            log.warning(f"scene nodes write failed (queued for retry): row={row_id} "
                        f"err={res.get('error')}")
        return FAILED
    got = str(_out.get("action") or "")
    if got == "updated":
        return UPDATED
    if got == "skipped":
        return SKIPPED
    if got == "mismatch":
        if log is not None:
            log.warning(f"scene nodes write skipped: row={row_id} content changed "
                        f"concurrently (expected_hash mismatch), keep newer content")
        return MISMATCH
    if log is not None:
        log.warning(f"scene nodes write: unexpected lib result {got!r} for row={row_id}")
    return FAILED