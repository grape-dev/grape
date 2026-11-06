"""Issue #1255：事件详情/实时日志弹窗的执行模型展示（task_model_summary 统一口径）。

钉死约定：
1. 用量优先：obs_llm_usage 按 task_id/run_kind='task' 聚合 SUM(input+output)，主模型取 token 最高者；
2. 并列兜底：token 相同时按 model_key 字典序升序，重复请求结果稳定不抖动；
3. 无用量回落 agent_runtimes.model_name（source='config'）；model_name 列缺失降级不报错；
4. 只返回模型名与 token 数，绝不透出 base_url/api_key 等敏感字段。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.observability import llm_usage as lu


def _make_task(task_id: str, runtime_id: int = 7):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt,"
            " repo_key, agent_type, user_id, status, usage_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, "sess-" + task_id, runtime_id, "p", "gitcode:org/repo", "coding", 42,
             "completed", "{}"))
        db.commit()


def _cleanup(task_id: str):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM obs_llm_usage WHERE task_id=?", (task_id,))
        c.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'rt-1255-%'")
        db.commit()


def _make_runtime(runtime_id: int, name: str, model_name: str = ""):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (runtime_id,))
        c.execute(
            "INSERT INTO agent_runtimes (id, credential_id, name, model_name) VALUES (?,?,?,?)",
            (runtime_id, 0, name, model_name))
        db.commit()


def test_usage_priority_and_top3_models():
    """多模型任务：主模型取 token 占比最高者；models 为 top3 明细。"""
    tid = "t_1255_multi"
    _make_task(tid)
    lu.record_usage(tid, [
        {"model": "glm-5.3", "input": 100, "output": 50, "turn": 0},   # 150
        {"model": "glm-5.3", "input": 10, "output": 5, "turn": 1},     # 聚合 165
        {"model": "deepseek-v4", "input": 60, "output": 30, "turn": 2},  # 90
        {"model": "qwen3-max", "input": 5, "output": 3, "turn": 3},    # 8
    ])
    s = lu.task_model_summary(tid)
    assert s["source"] == "usage"
    assert s["model"] == "glm-5.3"
    assert [m["model"] for m in s["models"]] == ["glm-5.3", "deepseek-v4", "qwen3-max"]
    assert s["models"][0]["tokens"] == 165
    # 明细拆分：输入/输出分别聚合（glm-5.3 = 110/55），供日志弹窗内联展示
    assert s["models"][0]["input"] == 110
    assert s["models"][0]["output"] == 55
    assert s["models"][1]["input"] == 60 and s["models"][1]["output"] == 30
    _cleanup(tid)


def test_tie_broken_by_model_key_asc():
    """token 并列时按 model_key 字典序升序兜底，重复调用不抖动。"""
    tid = "t_1255_tie"
    _make_task(tid)
    lu.record_usage(tid, [
        {"model": "zeta-model", "input": 50, "output": 50, "turn": 0},
        {"model": "alpha-model", "input": 60, "output": 40, "turn": 1},
    ])
    s1 = lu.task_model_summary(tid)
    s2 = lu.task_model_summary(tid)
    assert s1["model"] == "alpha-model" == s2["model"]
    _cleanup(tid)


def test_model_key_empty_or_unknown_falls_back_to_raw():
    """model_key 为空/unknown 时用 model_raw；两者皆空的行跳过。"""
    tid = "t_1255_raw"
    _make_task(tid)
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO obs_llm_usage (dedup_key, task_id, seq, turn, source, run_id, run_kind,"
            " provider_raw, model_raw, model_key, input_tokens, output_tokens)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid + ":1", tid, 1, 0, "hermes", tid, "task", "p", "raw-model-x", "unknown", 10, 5))
        c.execute(
            "INSERT INTO obs_llm_usage (dedup_key, task_id, seq, turn, source, run_id, run_kind,"
            " provider_raw, model_raw, model_key, input_tokens, output_tokens)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid + ":2", tid, 2, 1, "hermes", tid, "task", "p", "", "", 7, 3))
        db.commit()
    s = lu.task_model_summary(tid)
    assert s["source"] == "usage"
    assert s["model"] == "raw-model-x"
    _cleanup(tid)


def test_no_usage_falls_back_to_config_model():
    """无用量记录 → 回落 agent_runtimes.model_name（source='config'）。"""
    tid = "t_1255_cfg"
    _make_task(tid, runtime_id=1255001)
    _make_runtime(1255001, "rt-1255-config", model_name="claude-sonnet-4-5")
    s = lu.task_model_summary(tid, runtime_id=1255001)
    assert s == {"model": "claude-sonnet-4-5", "source": "config", "models": []}
    _cleanup(tid)


def test_missing_model_name_column_degrades_gracefully():
    """agent_runtimes 缺 model_name 列（老库）：配置回落为空、不抛 500，返回 unknown。"""
    tid = "t_1255_nocol"
    _make_task(tid, runtime_id=1255002)
    _make_runtime(1255002, "rt-1255-nocol", model_name="claude-sonnet-4-5")
    orig = lu._agent_runtimes_has_model_name
    try:
        lu._agent_runtimes_has_model_name = lambda cursor: False
        s = lu.task_model_summary(tid, runtime_id=1255002)
    finally:
        lu._agent_runtimes_has_model_name = orig
    assert s["model"] == "" and s["source"] == "unknown" and s["models"] == []
    _cleanup(tid)


def test_both_empty_returns_unknown():
    """无用量且无配置模型 → source='unknown'、model=''，前端统一占位。"""
    tid = "t_1255_none"
    _make_task(tid, runtime_id=1255003)
    _make_runtime(1255003, "rt-1255-empty", model_name="")
    s = lu.task_model_summary(tid, runtime_id=1255003)
    assert s["model"] == "" and s["source"] == "unknown" and s["models"] == []
    _cleanup(tid)


def test_summary_never_leaks_credentials():
    """安全约束：返回结构只含 model/source/models，无 base_url/api_key 等敏感键。"""
    tid = "t_1255_sec"
    _make_task(tid)
    lu.record_usage(tid, [{"model": "glm-5.3", "input": 1, "output": 1, "turn": 0}])
    s = lu.task_model_summary(tid)
    assert set(s.keys()) == {"model", "source", "models"}
    assert all(set(m.keys()) == {"model", "tokens", "input", "output"} for m in s["models"])
    blob = repr(s)
    assert "base_url" not in blob and "api_key" not in blob
    _cleanup(tid)
