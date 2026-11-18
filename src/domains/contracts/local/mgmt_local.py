"""MgmtService LocalProvider — 同实例跨 schema SQL 实现（Issue #699 Phase 5）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.3.

同实例模式：直接查 grape_mgmt schema（SQLite 用 mgmt_ 前缀）。
表名通过 t_mgmt() 映射，在 GRAPE_SCHEMA_MODE 未启用时返回裸表名（存量零回归）。
"""
from __future__ import annotations

import json
from typing import Any

from src.kernel import db_registry, session_manager
from src.kernel.db import t_mgmt

_DOMAIN = "mgmt"


def _t(name: str) -> str:
    """表名：自有 DSN → 裸名；共享进程库 → 跨 schema 前缀（与 RuntimeServiceLocal 同口径）。"""
    return name if db_registry.uses_own_dsn(_DOMAIN) else t_mgmt(name)
from src.kernel import db_registry, session_manager
from src.kernel.config import log
from src.domains.contracts.mgmt_service import MgmtService


def _provider_of_repo_key(repo_key: str) -> str:
    """repo_key → provider（权威解析走 parse_repo_key，不枚举平台字面量）。"""
    try:
        from src.kernel.repo_key import parse_repo_key
        return parse_repo_key(repo_key or "")[0] or ""
    except Exception as e:  # 解析失败不阻断合同读，落空后按无卡派生
        log.warning(f"mgmt_local: parse_repo_key({repo_key!r}) failed: {e}")
        return ""


def _provider_credential_columns(provider: str) -> tuple[str, str]:
    """provider → (bot token 列名, webhook token 列名)。

    D105-17/D106 口径：平台卡 token_storage_field / webhook_token_field 优先；
    卡缺省（字段为空或无卡）按 bot_<p>_token / <p>_webhook_token 派生——与
    platform_card builder 的缺省派生逐字节一致（存量 gitcode/github 列名不变）。
    派生列名只用于「该列存在时回落读」，物理列缺失（gitee/gitlab 终态：凭据
    只落 grape_t_repo_credentials）由调用方忽略，不报错。
    """
    tok_col = wh_col = ""
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        card = get_platform_card(provider) if provider else None
        if card is not None:
            tok_col = card.token_storage_field or ""
            wh_col = card.webhook_token_field or ""
    except Exception as e:
        log.warning(f"mgmt_local: platform card lookup failed for {provider!r}: {e}")
    if not tok_col and provider:
        tok_col = f"bot_{provider}_token"
    if not wh_col and provider:
        wh_col = f"{provider}_webhook_token"
    return tok_col, wh_col


def _repo_credential(repo_key: str, cred_type: str) -> str:
    """读 grape_t_repo_credentials 单仓库凭据（S82/D58 起为凭据唯一权威）。

    cred_type ∈ {'bot','webhook'}（平台语义由 repo_key 前缀承载，D99）；
    表不存在（旧库/裁剪库）或读失败 → 空串（调用方回落历史列），并留 warning。

    表归 insight（table_ownership.json），读写只经 insight public 的
    `repo_credential_value`（任意拓扑原语：自有 DSN 走 insight 连接，单库回落进程库），
    本模块不再持有该表的 SQL——原先在 mgmt 连接上直读，独立三库拓扑下必然 1146。
    """
    try:
        from src.domains.insight import public as _insight_public
        raw = _insight_public.repo_credential_value(repo_key, cred_type)
    except Exception as e:
        log.warning(f"mgmt_local: grape_t_repo_credentials read failed ({repo_key}, {cred_type}): {e}")
        return ""
    if not raw:
        return ""
    try:
        from src.domains.mgmt.user.db import _decrypt_repo_secret
        return _decrypt_repo_secret(raw) or ""
    except Exception as e:
        log.warning(f"mgmt_local: repo credential decrypt failed ({repo_key}, {cred_type}): {e}")
        return ""


class MgmtServiceLocal(MgmtService):
    """同实例模式：直接查 grape_mgmt schema（SQLite 用 mgmt_ 前缀）。"""

    @property
    def _own_dsn(self) -> bool:
        return db_registry.uses_own_dsn(_DOMAIN)

    @property
    def _db(self):
        return db_registry.domain_db(_DOMAIN) or session_manager.db

    @property
    def _lock(self):
        return db_registry.domain_lock(_DOMAIN) if self._own_dsn else session_manager.db_lock

    def _managed_repo_row(self, c, repo_key: str) -> dict:
        """整行读出为 dict（SELECT *：列名随卡/库版本而变——旧库缺派生列时
        调用方按 .get() 落空，不做动态列名 SQL 拼接）。"""
        c.execute(
            f"SELECT * FROM {_t('managed_repos')} WHERE repo_key=?",
            (repo_key,),
        )
        row = c.fetchone()
        if not row:
            return {}
        cols = [d[0] for d in c.description] if c.description else []
        return dict(zip(cols, row)) if cols else dict(row)

    def get_repo_webhook_config(self, repo_key: str) -> dict:
        """B 校验 webhook 时调用——返回 repo_key, webhook_token, bot_username, org_id。

        凭据按 repo_key 前缀 provider 派生（卡列 → rc 单表优先），不再恒取
        gitcode_webhook_token（gitee/github/gitlab 仓此前恒空）。
        """
        provider = _provider_of_repo_key(repo_key)
        _, wh_col = _provider_credential_columns(provider)
        with self._lock:
            c = self._db.cursor()
            data = self._managed_repo_row(c, repo_key)
            if not data:
                return {}
            webhook_token = (
                _repo_credential(repo_key, "webhook")
                or (data.get(wh_col) if wh_col else "")
                or data.get("webhook_secret")
                or ""
            )
        return {
            "repo_key": data.get("repo_key", repo_key),
            "webhook_token": webhook_token,
            "bot_username": data.get("bot_username") or "",
            "org_id": data.get("org_id") or 0,
        }

    def get_agent_session(self, repo_key: str, agent_type: str) -> dict:
        """B 路由判定时调用——返回 runtime_id, skill, skill_id。

        devops_agent_session 无 runtime_id/skill_id 列，skill 从 skill 字段取，
        runtime_id 由 agent_type_runtime_pool 查询补全（0=未绑定）。
        """
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT id, repo_key, agent_type, skill, status "
                f"FROM {_t('devops_agent_session')} "
                f"WHERE repo_key=? AND agent_type=?",
                (repo_key, agent_type),
            )
            row = c.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in c.description] if c.description else []
            data = dict(zip(cols, row)) if cols else dict(row)
            # 补全 runtime_id：从 agent_type_runtime_pool 查询绑定的引擎
            try:
                c.execute(
                    f"SELECT runtime_id FROM {_t('agent_type_runtime_pool')} "
                    f"WHERE agent_type=? AND enabled=1 "
                    f"ORDER BY priority ASC, weight DESC LIMIT 1",
                    (agent_type,),
                )
                pool_row = c.fetchone()
                data["runtime_id"] = pool_row[0] if pool_row else 0
            except Exception:
                data["runtime_id"] = 0
            data["skill_id"] = data.get("skill", "")
            return data

    def get_scene_config(self, repo_key: str) -> list:
        """B 读取场景节点——返回 scene_id, nodes_json, name, display_name 列表。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT scene_id, nodes_json, name "
                f"FROM {_t('devops_pipeline_scene')} WHERE repo_key=? "
                f"AND is_active=1",
                (repo_key,),
            )
            rows = c.fetchall()
            if not rows:
                return []
            cols = [d[0] for d in c.description] if c.description else []
            return [dict(zip(cols, r)) for r in rows] if cols else [dict(r) for r in rows]

    def get_scene_pool_engine_ids(self, scene_id: str) -> list:
        """C 引擎选择时调用——返回场景绑定的引擎 ID 列表。

        场景→引擎绑定通过 agent_type_runtime_pool 间接关联（按 agent_type 查池）。
        """
        with self._lock:
            c = self._db.cursor()
            # 读场景 nodes_json 获取 agent_type 列表
            c.execute(
                f"SELECT nodes_json FROM {_t('devops_pipeline_scene')} "
                f"WHERE scene_id=?",
                (scene_id,),
            )
            row = c.fetchone()
            if not row:
                return []
            try:
                nodes = json.loads(row[0]) if row[0] else []
            except (json.JSONDecodeError, TypeError):
                return []
            # 提取 nodes 中的 agent_type
            agent_types = set()
            for node in nodes:
                tag = (node.get("tag") or node.get("skill") or "").lower()
                if tag:
                    agent_types.add(tag)
            if not agent_types:
                return []
            # 查池绑定
            placeholders = ",".join("?" * len(agent_types))
            c.execute(
                f"SELECT DISTINCT runtime_id FROM {_t('agent_type_runtime_pool')} "
                f"WHERE agent_type IN ({placeholders}) AND enabled=1",
                list(agent_types),
            )
            return [r[0] for r in c.fetchall()]

    def verify_session_token(self, token_hash: str) -> dict:
        """C Worker 鉴权时调用——返回 credential_id, scope, owner_id。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT id, session_key, repo_key, agent_type, user_id, expires_at "
                f"FROM {_t('session_api_tokens')} "
                f"WHERE token_sha256=?",
                (token_hash,),
            )
            row = c.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in c.description] if c.description else []
            data = dict(zip(cols, row)) if cols else dict(row)
            data["credential_id"] = data.get("id")
            data["scope"] = "session"
            data["owner_id"] = data.get("user_id", 0)
            return data

    def get_bot_config(self, repo_key: str) -> dict:
        """B/C 读取 Bot 配置——bot_token, bot_username, webhook_token。

        凭据按 repo_key 前缀 provider 派生（平台卡列名 → rc 单表优先）；不再
        恒取 bot_gitcode_token / gitcode_webhook_token（gitee/github/gitlab 仓
        经合同给下游的 bot/webhook token 此前恒空）。
        """
        provider = _provider_of_repo_key(repo_key)
        bot_col, wh_col = _provider_credential_columns(provider)
        with self._lock:
            c = self._db.cursor()
            data = self._managed_repo_row(c, repo_key)
            if not data:
                return {}
            bot_token = (
                _repo_credential(repo_key, "bot")
                or (data.get(bot_col) if bot_col else "")
                or ""
            )
            webhook_token = (
                _repo_credential(repo_key, "webhook")
                or (data.get(wh_col) if wh_col else "")
                or ""
            )
        return {
            "bot_token": bot_token,
            "bot_username": data.get("bot_username") or "",
            "webhook_token": webhook_token,
        }

    def get_skill(self, skill_id: str) -> Any:
        """技能加载——返回技能定义。"""
        with self._lock:
            c = self._db.cursor()
            try:
                skill_id_int = int(skill_id)
            except (ValueError, TypeError):
                skill_id_int = 0
            if not skill_id_int:
                # 按名称查
                c.execute(
                    f"SELECT * FROM {_t('skill')} WHERE skill_name=? LIMIT 1",
                    (skill_id,),
                )
            else:
                c.execute(
                    f"SELECT * FROM {_t('skill')} WHERE id=? LIMIT 1",
                    (skill_id_int,),
                )
            row = c.fetchone()
            if not row:
                return None
            cols = [d[0] for d in c.description] if c.description else []
            return dict(zip(cols, row)) if cols else dict(row)


    # ---- P2 服务接口补齐（N6/N8/N9/N10，mgmt 只读白名单） ----

    _REPO_PUBLIC_COLS = ("repo_key", "repo_owner", "repo_name", "full_name", "webhook_id",
                         "dev_scene", "host_status", "bot_username", "org_id", "deploy_url",
                         "bot_token_status", "host_vcs_username", "created_at", "is_admin")
    _REPO_SECRET_COLS = ("gitcode_webhook_token", "webhook_secret", "bot_gitcode_token",
                         "bot_github_token", "github_webhook_token")
    _MAPPING_COLUMNS = {"feishu": "feishu_open_id", "gitcode": "gitcode_user",
                        "github": "github_login", "dingtalk": "dingtalk_user_id"}

    def get_repo_bundle(self, repo_key: str) -> dict:
        """N6：仓库配置 + 凭据「有/无」 + webhook 配置 + 场景（不含 token 明文）。"""
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT {', '.join(self._REPO_PUBLIC_COLS)} FROM {_t('managed_repos')} "
                f"WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            if not row:
                return {}
            out = dict(zip(self._REPO_PUBLIC_COLS, row))
            c.execute(
                f"SELECT {', '.join(self._REPO_SECRET_COLS)} FROM {_t('managed_repos')} "
                f"WHERE repo_key=?", (repo_key,))
            flags = c.fetchone() or ()
            out["credentials"] = {
                name.replace("_token", ""): bool(flags[i]) for i, name in enumerate(self._REPO_SECRET_COLS)
            } if flags else {}
            c.execute(
                f"SELECT scene_id, name, is_active FROM {_t('devops_pipeline_scene')} "
                f"WHERE repo_key=? AND is_active=1 ORDER BY name", (repo_key,))
            out["scenes"] = [{"scene_id": r[0], "name": r[1] or r[0], "is_active": r[2]}
                             for r in c.fetchall()]
            return out

    def get_account(self, account_id: str) -> dict:
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT id, username, nickname, status FROM {_t('grape_accounts')} WHERE id=?",
                (account_id,))
            row = c.fetchone()
            if not row:
                return {}
            out = {"id": row[0], "username": row[1], "display_name": row[2] or row[1],
                   "status": row[3], "org_id": None}
            try:
                c.execute(f"SELECT org_id FROM {_t('org_member')} WHERE user_id=? "
                          f"ORDER BY org_id LIMIT 1", (account_id,))
                org = c.fetchone()
                out["org_id"] = org[0] if org else None
            except Exception:  # noqa: BLE001 - 旧库无 org_member 时不影响主字段
                out["org_id"] = None
            return out

    def get_knowledge(self, knowledge_id: int) -> dict:
        cols = ("id", "name", "description", "scope", "repo_key", "agent_type", "issue_num",
                "status", "updated_at", "content_hash", "obs_key", "file_path")
        with self._lock:
            c = self._db.cursor()
            c.execute(
                f"SELECT {', '.join(cols)} FROM {_t('agent_shared_knowledge')} WHERE id=?",
                (int(knowledge_id),))
            row = c.fetchone()
            if not row:
                return {}
            out = dict(zip(cols, row))
        out["display_name"] = out.get("name") or ""
        out["has_content"] = bool(out.pop("obs_key", "") or out.get("file_path"))
        return out

    def get_user_mapping_batch(self, keys: list) -> list:
        keys = list(keys or [])[:200]
        by_platform: dict[str, list] = {}
        for k in keys:
            platform = str((k or {}).get("platform") or "").strip().lower()
            external = str((k or {}).get("external_id") or "").strip()
            by_platform.setdefault(platform, []).append(external)
        found: dict[tuple, str] = {}
        with self._lock:
            c = self._db.cursor()
            for platform, values in by_platform.items():
                column = self._MAPPING_COLUMNS.get(platform)
                if not column:
                    continue
                values = [v for v in dict.fromkeys(values) if v]
                if not values:
                    continue
                marks = ",".join(["?"] * len(values))
                c.execute(
                    f"SELECT {column}, user_id FROM {_t('unified_user_mapping')} "
                    f"WHERE {column} IN ({marks}) AND user_id IS NOT NULL "
                    f"AND user_id NOT IN ('', '0')", tuple(values))
                for ext, uid in c.fetchall():
                    found.setdefault((platform, ext), uid)
        out = []
        for k in keys:
            platform = str((k or {}).get("platform") or "").strip().lower()
            external = str((k or {}).get("external_id") or "").strip()
            uid = found.get((platform, external))
            out.append({"platform": platform, "external_id": external,
                        "user_id": uid, "matched": bool(uid)})
        return out
