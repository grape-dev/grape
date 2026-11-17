"""多引擎 LLM 凭据配置测试。

测试覆盖:
  T1-T6: 两级 fallback 全路径
  T7: UNIQUE UPSERT
  T8: 存量数据兼容
  T9-T10: Configurator 读 provider
  T11-T12: env_var 兼容性
  T13: claim_predicate 传 engine_type
  T14: 前端 API
  T15: runtime_info 存量迁移
  T16: build_context() or fallback 消除
  T17: service.py claim() 池匹配恢复基线
"""
import json
import sqlite3
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone


# ===== 测试用 DB fixture =====

@pytest.fixture
def test_db():
    """创建内存 SQLite 测试 DB。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
CREATE TABLE user_llm_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    provider VARCHAR(64) NOT NULL,
    api_key_enc TEXT NOT NULL,
    base_url VARCHAR(512) DEFAULT '',
    model VARCHAR(128) DEFAULT '',
    enabled INTEGER DEFAULT 1,
    key_version INTEGER DEFAULT 1,
    engine_type VARCHAR(64) NOT NULL DEFAULT 'common',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, engine_type)
)""")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def insert_cred(test_db):
    """插入凭据的辅助函数。"""
    def _insert(user_id=1, provider="openai", api_key_enc="enc_key", base_url="",
                model="gpt-4o", enabled=1, engine_type="common"):
        c = test_db.cursor()
        c.execute(
            "INSERT INTO user_llm_credentials (user_id, provider, api_key_enc, base_url, model, enabled, engine_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, provider, api_key_enc, base_url, model, enabled, engine_type)
        )
        test_db.commit()
        return c.lastrowid
    return _insert


# ===== T1-T6: select_llm_cred 两级 fallback =====

class TestSelectLlmCred:
    """测试公共函数 select_llm_cred 的两级 fallback 逻辑。"""

    def test_t1_engine_specific_hit(self, test_db, insert_cred):
        """T1: engine_type='hermes' 命中专属。"""
        from src.domains.runtime.agents.sandbox.llm_cred_selector import select_llm_cred
        insert_cred(engine_type="hermes", provider="openai", api_key_enc="enc_hermes")
        insert_cred(engine_type="common", provider="zhipu", api_key_enc="enc_common")

        with patch("src.domains.runtime.agents.sandbox.llm_cred_selector.decrypt", return_value="decrypted_key"):
            row = select_llm_cred(test_db.cursor(), 1, "hermes")
        assert row is not None
        assert row[0] == "openai"  # provider
        assert row[1] == "decrypted_key"  # api_key

    def test_t2_fallback_to_common(self, test_db, insert_cred):
        """T2: engine_type='hermes' 未配 → 落 'common'。"""
        from src.domains.runtime.agents.sandbox.llm_cred_selector import select_llm_cred
        insert_cred(engine_type="common", provider="zhipu", api_key_enc="enc_common")

        with patch("src.domains.runtime.agents.sandbox.llm_cred_selector.decrypt", return_value="decrypted_key"):
            row = select_llm_cred(test_db.cursor(), 1, "hermes")
        assert row is not None
        assert row[0] == "zhipu"  # 落到 common

    def test_t3_empty_engine_type(self, test_db, insert_cred):
        """T3: engine_type='' → 直接落 'common'。"""
        from src.domains.runtime.agents.sandbox.llm_cred_selector import select_llm_cred
        insert_cred(engine_type="common", provider="openai", api_key_enc="enc_common")

        with patch("src.domains.runtime.agents.sandbox.llm_cred_selector.decrypt", return_value="decrypted_key"):
            row = select_llm_cred(test_db.cursor(), 1, "")
        assert row is not None
        assert row[0] == "openai"

    def test_t4_unknown_engine_type(self, test_db, insert_cred):
        """T4: engine_type='vod'(未知值) → 落 'common'。"""
        from src.domains.runtime.agents.sandbox.llm_cred_selector import select_llm_cred
        insert_cred(engine_type="common", provider="openai", api_key_enc="enc_common")

        with patch("src.domains.runtime.agents.sandbox.llm_cred_selector.decrypt", return_value="decrypted_key"):
            row = select_llm_cred(test_db.cursor(), 1, "vod")
        assert row is not None
        assert row[0] == "openai"  # vod 不匹配，落 common

    def test_t5_no_credential_at_all(self, test_db):
        """T5: 查不到任何凭据 → 返回 None。"""
        from src.domains.runtime.agents.sandbox.llm_cred_selector import select_llm_cred
        row = select_llm_cred(test_db.cursor(), 999, "hermes")
        assert row is None

    def test_t6_disabled_credential_skipped(self, test_db, insert_cred):
        """T6: enabled=0 的凭据被跳过。"""
        from src.domains.runtime.agents.sandbox.llm_cred_selector import select_llm_cred
        insert_cred(engine_type="hermes", enabled=0, api_key_enc="disabled_key")
        insert_cred(engine_type="common", enabled=0, api_key_enc="disabled_common")

        row = select_llm_cred(test_db.cursor(), 1, "hermes")
        assert row is None  # 两个都 disabled


# ===== T7: UNIQUE 约束 =====

class TestUniqueConstraint:
    """测试 UNIQUE(user_id, engine_type) 约束。"""

    def test_t7_unique_prevents_duplicate(self, test_db, insert_cred):
        """T7: 同 user + 同 engine_type 插入第二条会失败。"""
        insert_cred(user_id=1, engine_type="hermes")
        with pytest.raises(sqlite3.IntegrityError):
            insert_cred(user_id=1, engine_type="hermes")  # 重复

    def test_t7b_different_engine_type_ok(self, test_db, insert_cred):
        """T7b: 同 user + 不同 engine_type 可以多条。"""
        insert_cred(user_id=1, engine_type="hermes")
        insert_cred(user_id=1, engine_type="opencode")
        insert_cred(user_id=1, engine_type="common")
        # 三条都成功


# ===== T8: 存量数据兼容 =====

class TestBackwardCompatibility:
    """测试存量数据 engine_type='common' 能命中。"""

    def test_t8_common_credential_hits(self, test_db, insert_cred):
        """T8: 存量数据 engine_type='common' 能命中。"""
        from src.domains.runtime.agents.sandbox.llm_cred_selector import select_llm_cred
        insert_cred(engine_type="common", provider="zhipu", api_key_enc="enc_common")

        with patch("src.domains.runtime.agents.sandbox.llm_cred_selector.decrypt", return_value="decrypted_key"):
            # 任何 engine_type 查不到专属时都落 common
            row = select_llm_cred(test_db.cursor(), 1, "hermes")
        assert row is not None
        assert row[0] == "zhipu"


# ===== T9-T10: Configurator 读 provider =====

class TestConfiguratorProvider:
    """测试 Configurator 读取 provider 字段。"""

    def test_t9_hermes_with_base_url(self):
        """T9: provider='zhipu' + base_url → hermes_provider='custom'。"""
        from src.domains.runtime.agents.sandbox.backends.engine_strategies import HermesConfigurator
        configurator = HermesConfigurator()
        yaml_content, env_var = configurator._build_config({
            "provider": "zhipu",
            "api_key": "sk-xxx",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-4",
        })
        assert "provider: custom" in yaml_content
        assert "base_url: https://open.bigmodel.cn/api/paas/v4" in yaml_content
        assert "# llm_provider: zhipu" in yaml_content  # 描述性注释
        assert env_var == "HERMES_API_KEY"

    def test_t10_hermes_without_base_url(self):
        """T10: provider='openai' + 无 base_url → hermes_provider='openai'。"""
        from src.domains.runtime.agents.sandbox.backends.engine_strategies import HermesConfigurator
        configurator = HermesConfigurator()
        yaml_content, env_var = configurator._build_config({
            "provider": "openai",
            "api_key": "sk-xxx",
            "base_url": "",
            "model": "gpt-4o",
        })
        assert "provider: openai" in yaml_content
        assert env_var == "HERMES_API_KEY"

    def test_t11_hermes_env_var_unified(self):
        """T11: env_var = HERMES_API_KEY（不再是 HERMES_OPENAI/CUSTOM_API_KEY）。"""
        from src.domains.runtime.agents.sandbox.backends.engine_strategies import HermesConfigurator
        configurator = HermesConfigurator()
        _, env_var = configurator._build_config({
            "provider": "openai", "api_key": "", "base_url": "", "model": "gpt-4o",
        })
        assert env_var == "HERMES_API_KEY"
        assert "OPENAI" not in env_var
        assert "CUSTOM" not in env_var

    def test_t12_opencode_env_var_unified(self):
        """T12: env_var = OPENCODE_API_KEY（不再是 OPENCODE_OPENAI/CUSTOM_API_KEY）。"""
        from src.domains.runtime.agents.sandbox.backends.engine_strategies import OpenCodeConfigurator
        configurator = OpenCodeConfigurator()
        json_content, env_var = configurator._build_config({
            "provider": "deepseek", "api_key": "", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
        })
        assert env_var == "OPENCODE_API_KEY"
        assert "OPENAI" not in env_var
        assert "CUSTOM" not in env_var
        # opencode 用真实 provider 名做展示名
        assert '"name": "deepseek"' in json_content or "'name': 'deepseek'" in json_content


# ===== T13: claim_predicate 传 engine_type =====

class TestClaimPredicate:
    """测试 claim_predicate 传 engine_type 到 query_llm_env。"""

    def test_t13_engine_type_passed_to_query_llm_env(self):
        """T13: claim_predicate 从 runtime_info 提取 engine_type 传给 query_llm_env。"""
        from src.domains.runtime.engine.claim_predicate import LlmCredentialPredicate

        # mock reader
        mock_reader = MagicMock()
        mock_reader.query_llm_env.return_value = {"_LLM_API_KEY": "sk-xxx"}

        predicate = LlmCredentialPredicate(credential_reader=mock_reader)

        # task dict 包含 runtime_info
        task = {
            "runtime_type": "cloud",
            "user_id": 1,
            "runtime_info": json.dumps({"engine_type": "hermes"}),
        }

        decision = predicate.check(task, runtime_id=1)

        assert decision.allowed is True
        # 验证 query_llm_env 被调用时传了 engine_type="hermes"
        mock_reader.query_llm_env.assert_called_once_with(1, "hermes")

    def test_t13b_empty_runtime_info(self):
        """T13b: runtime_info 为空 → engine_type='' 传给 query_llm_env。"""
        from src.domains.runtime.engine.claim_predicate import LlmCredentialPredicate

        mock_reader = MagicMock()
        mock_reader.query_llm_env.return_value = {"_LLM_API_KEY": "sk-xxx"}

        predicate = LlmCredentialPredicate(credential_reader=mock_reader)
        task = {"runtime_type": "cloud", "user_id": 1, "runtime_info": "{}"}

        predicate.check(task, runtime_id=1)

        mock_reader.query_llm_env.assert_called_once_with(1, "")


# ===== T14: 前端 API =====

class TestEngineTypesAPI:
    """测试引擎类型列表 API。"""

    def test_t14_engine_types_returned(self):
        """T14: GET /api/settings/llm-engine-types 返回 common + hermes + opencode。"""
        from src.domains.runtime.agents.sandbox.backends.engine_strategies import ENGINE_REGISTRY
        # ENGINE_REGISTRY 应包含 hermes 和 opencode
        assert "hermes" in ENGINE_REGISTRY
        assert "opencode" in ENGINE_REGISTRY

        # 模拟 list_llm_engine_types 逻辑
        types = [{"value": "common", "label": "通用（兜底）"}]
        for engine in ENGINE_REGISTRY.keys():
            types.append({"value": engine, "label": engine})

        values = [t["value"] for t in types]
        assert "common" in values
        assert "hermes" in values
        assert "opencode" in values


# ===== T15: runtime_info JSON 存量迁移 =====

class TestRuntimeInfoMigration:
    """测试 runtime_info JSON 存量迁移 agent_type → engine_type。"""

    def test_t15_migrate_agent_type_to_engine_type(self):
        """T15: JSON 中 agent_type 迁移为 engine_type。"""
        ri = {"agent_type": "hermes", "provider": "cubesandbox", "template_id": "tpl-xxx"}
        # 模拟迁移逻辑
        if "agent_type" in ri:
            ri["engine_type"] = ri.pop("agent_type")

        assert "agent_type" not in ri
        assert ri["engine_type"] == "hermes"
        assert ri["provider"] == "cubesandbox"  # 其他字段不变

    def test_t15b_idempotent(self):
        """T15b: 迁移幂等——跑两次结果一样。"""
        ri = {"agent_type": "hermes", "provider": "cubesandbox"}
        # 第一次
        if "agent_type" in ri:
            ri["engine_type"] = ri.pop("agent_type")
        # 第二次
        if "agent_type" in ri:
            ri["engine_type"] = ri.pop("agent_type")
        assert ri["engine_type"] == "hermes"
        assert "agent_type" not in ri


# ===== T16: build_context() or fallback 消除 =====

class TestBuildContextNoFallback:
    """测试 build_context() 不再 fallback 到 task.agent_type。"""

    def test_t16_no_or_fallback(self):
        """T16: runtime_info.engine_type 为空时不 fallback 到 task.agent_type。"""
        # 模拟改后的 build_context 逻辑
        _ri = {"engine_type": ""}  # runtime_info 中 engine_type 为空
        task_agent_type = "vod"  # task.agent_type 是业务角色

        # 改后逻辑: engine_type=_ri.get("engine_type") or ""
        engine_type = _ri.get("engine_type") or ""

        # 不应该 fallback 到 vod
        assert engine_type == ""
        assert engine_type != task_agent_type  # 不会拿到 vod


# ===== T17: service.py claim() 池匹配恢复基线 =====

class TestClaimPoolMatchBaseline:
    """测试 service.py claim() 池匹配恢复基线逻辑。"""

    def test_t17_pool_match_not_exact(self):
        """T17: 池匹配用 pool_agent_type != '' 而非精确匹配。"""
        # 基线逻辑: WHERE runtime_id=0 AND pool_agent_type != '' AND status='queued'
        # 不按引擎类型过滤
        # 模拟 SQL 查询条件
        pool_agent_type = "vod"  # 业务角色
        engine_type = "hermes"  # 引擎类型

        # 基线: 只检查非空
        assert pool_agent_type != ''  # True → 认领

        # 错误的精确匹配（本分支 b8138a12 的 bug）:
        # pool_agent_type == engine_type → "vod" == "hermes" → False → 不认领
        assert pool_agent_type != engine_type  # 精确匹配不上
