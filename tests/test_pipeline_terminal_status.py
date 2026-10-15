"""Pipeline status must represent the latest stage, not any prior success."""
import pytest
from src.api.routes.issues import _pipeline_from_events


@pytest.mark.parametrize('current', ['queued', 'running', 'failed', 'cancelled', 'completed'])
def test_latest_stage_status_is_not_hidden_by_previous_success(current):
    events = [
        {'seq': 1, 'agent_type': 'qa', 'status': 'completed'},
        {'seq': 2, 'agent_type': 'sre', 'status': current},
    ]
    result = _pipeline_from_events('gitcode:test/repo', '1', events)
    assert result['status'] == current
    assert result['segments'][-1]['status'] == current


def test_successful_retry_can_replace_a_previous_failure():
    events = [
        {'seq': 1, 'agent_type': 'qa', 'status': 'completed'},
        {'seq': 2, 'agent_type': 'sre', 'status': 'failed'},
        {'seq': 3, 'agent_type': 'sre', 'status': 'completed'},
    ]
    assert _pipeline_from_events('gitcode:test/repo', '1', events)['status'] == 'completed'


def test_empty_pipeline_remains_unknown():
    assert _pipeline_from_events('gitcode:test/repo', '1', [])['status'] == 'unknown'
