"""Issue #3928：跨引擎最终执行 WARNING（task/runtime/engine + 按 task 去重计数）。"""
import logging

from src.domains.runtime.engine import backend_health


def setup_function():
    backend_health.reset_cross_engine_for_tests()


def test_cross_engine_warning_fields_and_dedupe(caplog):
    backend_health.reset_cross_engine_for_tests()
    with caplog.at_level(logging.WARNING, logger="devops-bot"):
        assert backend_health.note_cross_engine(
            "t_3928_cross", 17641, "codearts-cred0",
            "hermes-worker-ecs-grape-wo-0001") is True
        assert backend_health.note_cross_engine(
            "t_3928_cross", 17641, "codearts-cred0",
            "hermes-worker-ecs-grape-wo-0001") is False
    msgs = [r.getMessage() for r in caplog.records if "CROSS_ENGINE_EXECUTION" in r.getMessage()]
    assert len(msgs) == 1, "同一 task 只告警一次"
    msg = msgs[0]
    assert "task_id=t_3928_cross" in msg
    assert "runtime_id=17641" in msg
    assert "engine=codearts" in msg
    assert "pointer_engine=hermes" in msg
    assert backend_health.cross_engine_warning_count() == 1


def test_same_engine_type_does_not_warn(caplog):
    backend_health.reset_cross_engine_for_tests()
    with caplog.at_level(logging.WARNING, logger="devops-bot"):
        assert backend_health.note_cross_engine(
            "t_3928_same", 100, "codearts-worker-a", "codearts-worker-b") is False
    assert backend_health.cross_engine_warning_count() == 0
    assert not [r for r in caplog.records if "CROSS_ENGINE_EXECUTION" in r.getMessage()]