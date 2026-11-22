"""Issue #3740（先测后改）：RepoBotToken legacy 列回落降噪 + 短 TTL 缓存 + counter 指标。

合同（本文件即契约，实现前应全红）：

1. 日志降噪：每个 ``(repo_key, field)`` 首次 legacy 列回落必打 WARNING，其后按
   1/1000 采样打 WARNING，未命中采样降 DEBUG；日志文本保持
   ``… historical column fallback hit (rc miss) …`` 不变（回落分支下线判据的 grep 锚点）。
2. 指标：``grape_repo_bot_legacy_fallback_total{provider,repo}`` 按**实际 legacy 列存储读**
   全量计数，与采样解耦（日志少不等于数据丢）；缓存命中不计数（没读存储就不算回落）。
3. 缓存：``(repo_key, field)`` 进程内短 TTL 缓存（≤60s，``GRAPE_REPO_BOT_TOKEN_CACHE_TTL``
   可覆盖，0=关闭），缓存最终解析结果（rc 命中 / legacy 命中 / 全空），同请求不重复解析。
4. 审计口径：``credential_usage_log`` 随缓存降频——一次实际存储读一行审计，缓存命中不写
   （Issue 明列的「写放大」待消除项；``repo_bot_hit`` 在回填后由 0 变为有即可满足验收）。
5. 失效：TTL 到期、主动 ``invalidate_repo_bot_token_cache()``、``mark_expired`` 三条路径
   都必须让下一次解析读到新值（凭据轮换可见性）。
"""
import logging
import time
import uuid

import pytest

import src.kernel.repo_bot_observability as obs
import src.kernel.token_resolver as tr
from src.kernel.ports import repos as _repos
from src.kernel.ports import tokens as _tokens

_NS = "x3740"


def _rk(tag=""):
    """每次取新的 repo_key：进程级缓存/采样状态按 key 隔离，避免用例互染。"""
    return f"gitcode:{_NS}{tag}{uuid.uuid4().hex[:8]}/repo"


def _strategy(rk):
    return tr.RepoBotToken(tr.create_provider_from_repo_key(rk))


@pytest.fixture
def fake_ports(monkeypatch):
    """端口隔离：rc 缺失/命中与 legacy 列值由 state 控制，全用例零 DB 依赖。"""
    state = {"rc": "", "legacy": "", "rc_reads": 0, "legacy_reads": 0, "audit": []}

    def _get_repo_credential_meta(repo_key, cred_type):
        state["rc_reads"] += 1
        if cred_type != "bot" or not state["rc"]:
            return None
        return {"value": state["rc"], "id": "rc-id-1"}

    def _legacy_read(repo_key, column):
        state["legacy_reads"] += 1
        return state["legacy"]

    import src.kernel.persistence.credential_usage_log as cul

    monkeypatch.setattr(_tokens, "get_repo_credential_meta", _get_repo_credential_meta)
    monkeypatch.setattr(_tokens, "get_host_user_id", lambda repo_key: "")
    monkeypatch.setattr(_repos, "repo_bot_token_from_legacy_column_any", _legacy_read)
    monkeypatch.setattr(_repos, "repo_bot_token_status_any", lambda repo_key: "")
    monkeypatch.setattr(_repos, "set_repo_bot_token_status_any", lambda repo_key, status: True)

    def _log_usage(credential_id="", user_id=0, **kwargs):
        state["audit"].append(kwargs.get("operation", ""))

    monkeypatch.setattr(cul, "log_usage", _log_usage)
    tr.invalidate_repo_bot_token_cache()
    obs.reset_log_state()
    yield state
    tr.invalidate_repo_bot_token_cache()
    obs.reset_log_state()


@pytest.fixture
def no_cache(monkeypatch):
    """关闭缓存：每次解析都是真实存储读（采样/指标口径的基线环境）。"""
    monkeypatch.setenv("GRAPE_REPO_BOT_TOKEN_CACHE_TTL", "0")


@pytest.fixture
def long_cache(monkeypatch):
    monkeypatch.setenv("GRAPE_REPO_BOT_TOKEN_CACHE_TTL", "60")


def _fallback_warnings(caplog):
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and "historical column fallback hit" in r.getMessage()]


# ────────────────────────────────────────── 1. 日志首打 + 1/1000 采样

def test_legacy_fallback_log_first_hit_and_sample(caplog, fake_ports, no_cache):
    """首打必报；其后 1/1000 采样；未采样降 DEBUG（不再淹告警面）。"""
    rk = _rk("log")
    fake_ports["legacy"] = "tok-legacy"
    strategy = _strategy(rk)
    caplog.set_level(logging.DEBUG, logger="devops-bot")

    for _ in range(999):
        assert strategy.get_token(rk) == "tok-legacy"
    first_batch = _fallback_warnings(caplog)
    assert len(first_batch) == 1, f"首打之外不应再打 WARNING：{len(first_batch)} 条"

    assert strategy.get_token(rk) == "tok-legacy"     # 第 1000 次命中采样点
    assert len(_fallback_warnings(caplog)) == 2, "第 1000 次应命中 1/1000 采样"

    assert all("rc miss" in r.getMessage() for r in _fallback_warnings(caplog))
    debug_lines = [r for r in caplog.records
                   if r.levelno == logging.DEBUG and "historical column fallback hit" in r.getMessage()]
    assert len(debug_lines) == 998, f"未采样记录应降 DEBUG（保留定位能力）: {len(debug_lines)}"


def test_legacy_fallback_log_state_is_per_repo_field(caplog, fake_ports, no_cache):
    """采样状态按 (repo_key, field) 隔离：新仓库首次仍必打（新仓库异常不被采样漏掉）。"""
    fake_ports["legacy"] = "tok-legacy"
    caplog.set_level(logging.DEBUG, logger="devops-bot")
    rk_a, rk_b = _rk("a"), _rk("b")
    assert _strategy(rk_a).get_token(rk_a) == "tok-legacy"
    assert _strategy(rk_a).get_token(rk_a) == "tok-legacy"   # 同 key 第 2 次：不报
    assert _strategy(rk_b).get_token(rk_b) == "tok-legacy"   # 另一 key 首次：报
    assert len(_fallback_warnings(caplog)) == 2


# ────────────────────────────────────────── 2. counter 指标（全量、与采样解耦）

def test_legacy_fallback_counter_counts_every_storage_read(fake_ports, no_cache):
    """指标按实际存储读全量计数：1000 次回落 → 计数值 1000（日志只有 2 条）。"""
    from prometheus_client import CollectorRegistry, generate_latest

    registry = CollectorRegistry()
    obs.install(registry)
    rk = _rk("metric")
    fake_ports["legacy"] = "tok-legacy"
    strategy = _strategy(rk)
    for _ in range(1000):
        strategy.get_token(rk)

    body = generate_latest(registry).decode()
    _owner = rk.split(":", 1)[1]
    assert "grape_repo_bot_legacy_fallback_total" in body
    expected = f'grape_repo_bot_legacy_fallback_total{{provider="gitcode",repo="{_owner}"}} 1000.0'
    assert expected in body, body


def test_legacy_fallback_counter_ignores_rc_hits(fake_ports, no_cache):
    """rc 命中（含回填后常态）不计 legacy 回落：指标语义 = 历史列回落次数。"""
    from prometheus_client import CollectorRegistry, generate_latest

    registry = CollectorRegistry()
    obs.install(registry)
    rk = _rk("rc")
    fake_ports["rc"] = "tok-rc"
    fake_ports["legacy"] = "tok-legacy"
    strategy = _strategy(rk)
    for _ in range(3):
        assert strategy.get_token(rk) == "tok-rc"

    body = generate_latest(registry).decode()
    _owner = rk.split(":", 1)[1]
    assert f'grape_repo_bot_legacy_fallback_total{{provider="gitcode",repo="{_owner}"}}' not in body


# ────────────────────────────────────────── 3. 短 TTL 缓存（同请求不重复解析）

def test_fetch_cache_collapses_repeated_resolution(caplog, fake_ports, long_cache):
    """同一 (repo_key, field) 在 TTL 内只解析一次：1 次存储读、1 条日志、1 行审计。"""
    rk = _rk("cache")
    fake_ports["legacy"] = "tok-legacy"
    strategy = _strategy(rk)
    caplog.set_level(logging.DEBUG, logger="devops-bot")

    tokens = [strategy.get_token(rk) for _ in range(4)]
    assert tokens == ["tok-legacy"] * 4
    assert fake_ports["legacy_reads"] == 1, f"同 TTL 窗口应只读一次存储: {fake_ports['legacy_reads']}"
    assert fake_ports["rc_reads"] == 1, f"rc 探测也应被缓存: {fake_ports['rc_reads']}"
    assert len(_fallback_warnings(caplog)) == 1, "同请求不应连打 3-4 条同日志"
    assert fake_ports["audit"] == ["repo_bot_hit_legacy_column"], fake_ports["audit"]


def test_rc_hit_cached_and_audited_once_per_storage_read(fake_ports, long_cache):
    """rc 命中同样缓存；审计口径 = 一次实际存储读一行（写放大随缓存降频）。"""
    rk = _rk("rchit")
    fake_ports["rc"] = "tok-rc"
    strategy = _strategy(rk)

    assert [strategy.get_token(rk) for _ in range(3)] == ["tok-rc"] * 3
    assert fake_ports["rc_reads"] == 1, fake_ports["rc_reads"]
    assert fake_ports["legacy_reads"] == 0
    assert fake_ports["audit"] == ["repo_bot_hit"], fake_ports["audit"]


def test_miss_is_cached_too(fake_ports, long_cache):
    """全空解析（rc miss + legacy 列空）也缓存：无凭据仓库不再每次打 2 次库。"""
    rk = _rk("miss")
    strategy = _strategy(rk)
    assert [strategy.get_token(rk) for _ in range(3)] == ["", "", ""]
    assert fake_ports["rc_reads"] == 1 and fake_ports["legacy_reads"] == 1
    assert fake_ports["audit"] == []


# ────────────────────────────────────────── 4. 失效路径（TTL / 主动 / 过期标记）

def test_fetch_cache_ttl_expiry(fake_ports, monkeypatch):
    """TTL 到期后重新解析：轮换在 ≤TTL 内可见（spec 已接受的时间窗）。"""
    monkeypatch.setenv("GRAPE_REPO_BOT_TOKEN_CACHE_TTL", "0.05")
    rk = _rk("ttl")
    fake_ports["legacy"] = "tok-old"
    strategy = _strategy(rk)
    assert strategy.get_token(rk) == "tok-old"
    fake_ports["legacy"] = "tok-new"
    time.sleep(0.12)
    assert strategy.get_token(rk) == "tok-new"
    assert fake_ports["legacy_reads"] == 2


def test_fetch_cache_invalidated_on_rotation(fake_ports, long_cache):
    """主动失效（写路径轮换调用）后立即读到新值，不必等 TTL。"""
    rk = _rk("rot")
    fake_ports["legacy"] = "tok-old"
    strategy = _strategy(rk)
    assert strategy.get_token(rk) == "tok-old"

    fake_ports["legacy"] = "tok-rotated"
    assert tr.invalidate_repo_bot_token_cache(rk) == 1
    assert strategy.get_token(rk) == "tok-rotated"
    assert tr.invalidate_repo_bot_token_cache() >= 0      # 全清亦可用


def test_mark_expired_invalidates_cache(fake_ports, long_cache):
    """过期标记路径失效缓存：标过期后 token 轮换立即生效（不返回陈旧值）。"""
    rk = _rk("exp")
    fake_ports["legacy"] = "tok-before"
    strategy = _strategy(rk)
    assert strategy.get_token(rk) == "tok-before"
    fake_ports["legacy"] = "tok-after-rotate"
    strategy.mark_expired(rk)
    assert strategy.get_token(rk) == "tok-after-rotate"


def test_cache_ttl_is_clamped_to_spec_ceiling(monkeypatch):
    """TTL 口径受 spec 约束（≤60s）：越界配置被钳制，避免凭据陈旧超窗。"""
    monkeypatch.setenv("GRAPE_REPO_BOT_TOKEN_CACHE_TTL", "3600")
    assert tr.repo_bot_token_cache_ttl() == 60.0
    monkeypatch.setenv("GRAPE_REPO_BOT_TOKEN_CACHE_TTL", "0")
    assert tr.repo_bot_token_cache_ttl() == 0.0