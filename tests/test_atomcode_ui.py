"""AtomCode engine identity and selection contracts."""
import pytest

from src.runtime.instances import canonical_engine, instance_name


@pytest.mark.parametrize('name', ['atomcode', 'atomcode-leo-mac', 'ATOMCODE-linux-1'])
def test_atomcode_instance_resolves_to_native_engine(name):
    assert canonical_engine(name) == 'atomcode'


def test_atomcode_selectable_in_engine_and_worker_filters():
    from src.api.routes.settings import _device_engine_types, _engine_type_of

    assert _engine_type_of({'instance_name': 'atomcode-leo-mac'}) == 'atomcode'
    assert _engine_type_of({'name': 'legacy-atomcode-leo'}) == 'atomcode'
    assert _device_engine_types({'runtimes': [{'name': 'atomcode'}, {'name': 'codex'}, {'name': 'atomcode'}]}) == ['atomcode', 'codex']
    assert instance_name('atomcode', 'Leo Mac', 1) == 'atomcode-leo-mac'


def test_atomcode_role_defaults_follow_codex_and_allow_explicit_restrictions():
    from src.runtime.engine_select import engine_role_tags

    assert engine_role_tags('atomcode') is None
    assert engine_role_tags('atomcode', '["coding", "qa"]') == ['coding', 'qa']


def test_atomcode_is_known_without_cli_installation(monkeypatch):
    from src.user.db import is_known_engine_name

    monkeypatch.setattr('src.agent.executor.discover_backends', lambda: [])
    assert is_known_engine_name('ATOMCODE') is True
