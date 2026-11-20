"""插件配置继承链 —— 运行时继承解析引擎（Issue #1082）。

五层 scope（组织→个人→仓库→节点→任务）差异覆盖（overlay）模型：
- 每层只写差异（overlay），未覆盖项运行时向上层自动继承（继承非同步）；
- resolve() 每次现场从存储逐层读取并归并，无快照/副本 —— 组织层改动
  下一次 resolve 立即生效，无需任何同步动作；
- 越具体的层优先级越高（固定顺序 org < personal < repo < node < task）。

存储：grape_t_plugin_config_overlays 表（DB 唯一权威源，见 src/api/db_setup.py）。
密钥命名项在 set_layer_overlay 落库前由统一凭据库（vault.py）置换为
引用句柄 —— overlay 与 resolve 视图全程不持明文（明文零副本）。
"""
from __future__ import annotations

import json

from src.kernel.config import log
from src.domains.mgmt.plugin_config.merge import merge_overlays
from src.domains.mgmt.auth.vault import CredentialVault, is_credential_name, is_handle
from src.kernel.session_manager import db, db_lock

# 五层 scope：低 → 高优先级（越具体越后归并、越后覆盖）
SCOPES = ("org", "personal", "repo", "node", "task")
_SCOPE_SET = set(SCOPES)


class ConfigInheritanceEngine:
    """五层配置继承解析引擎（DB 持久化 + 运行时现场归并）。"""

    def __init__(self, vault: CredentialVault | None = None):
        self._vault = vault or CredentialVault()

    def set_layer_overlay(self, scope: str, scope_key: str, plugin_id: str,
                          config: dict, updated_by: str = "") -> dict:
        """写一层差异覆盖（UPSERT，整层替换为本次传入的差异项）。

        落库前凭据零副本置换：密钥命名项（命名约定判定）的非空明文字符串
        统一入统一凭据库，overlay 内替换为引用句柄 —— 明文不落本存储。
        QA 审查修复：密钥命名键下的非空 dict/list 容器**整体**置换为句柄
        （容器序列化入凭据库），不再递归下探 —— 容器内明文同样零副本。
        scope 合法性校验：只允许五层枚举，防拼写错层静默丢失。
        scope_key 不得为空/空白（resolve 跳过空 key 层，空 key 写入即
        静默孤儿 overlay）；config 传 None 显式等价空差异层，其余非 dict
        一律 ValueError。返回落库后的该层 overlay（dict）。
        """
        if scope not in _SCOPE_SET:
            raise ValueError(
                f"非法 scope '{scope}'，只允许 {list(SCOPES)}（组织→个人→仓库→节点→任务）")
        if not (scope_key or "").strip():
            raise ValueError(
                "scope_key 不能为空（resolve 跳过空 key 层，空 key 写入即静默孤儿 overlay）")
        if not plugin_id:
            raise ValueError("plugin_id 不能为空")
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise ValueError("config 必须是对象（差异覆盖 dict）")
        provenance = f"{scope}:{scope_key}"
        stored = self._swap_secrets_to_handles(config, plugin_id, provenance)
        config_json = json.dumps(stored, ensure_ascii=False)
        with db_lock:
            row = self._fetch_row(scope, scope_key, plugin_id)
            if row is None:
                db.execute(
                    "INSERT INTO grape_t_plugin_config_overlays"
                    " (scope, scope_key, plugin_id, config_json, updated_by)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (scope, scope_key, plugin_id, config_json, updated_by))
            else:
                db.execute(
                    "UPDATE grape_t_plugin_config_overlays SET config_json=?, updated_by=?,"
                    " updated_at=datetime('now')"
                    " WHERE scope=? AND scope_key=? AND plugin_id=?",
                    (config_json, updated_by, scope, scope_key, plugin_id))
            db.commit()
        log.info(f"plugin_config: overlay saved scope={scope} key={scope_key}"
                 f" plugin={plugin_id} keys={len(stored)}")
        return stored

    def get_layer_overlay(self, scope: str, scope_key: str, plugin_id: str) -> dict:
        """读一层差异覆盖；未设置/损坏返回空 dict（该层不参与归并）。"""
        row = self._fetch_row(scope, scope_key, plugin_id)
        if row is None:
            return {}
        try:
            data = json.loads(row["config_json"])
            return data if isinstance(data, dict) else {}
        except Exception as e:
            log.warning(f"plugin_config: corrupt overlay json scope={scope}"
                        f" key={scope_key} plugin={plugin_id}: {e}")
            return {}

    def resolve(self, plugin_id: str, *, org: str = "", personal: str = "",
                repo: str = "", node: str = "", task: str = "") -> dict:
        """按 组织→个人→仓库→节点→任务 顺序现场归并出有效配置视图。

        scope 未提供的层（scope_key 为空）不参与归并 —— 回退到已提供层。
        每次调用现场读存储：无缓存副本，上层改动下一次调用立即生效。
        """
        scope_keys = {"org": org, "personal": personal, "repo": repo,
                      "node": node, "task": task}
        layers: list[dict] = []
        for scope in SCOPES:
            key = (scope_keys.get(scope) or "").strip()
            if not key:
                continue
            layers.append(self.get_layer_overlay(scope, key, plugin_id))
        return merge_overlays(layers)

    @staticmethod
    def _fetch_row(scope: str, scope_key: str, plugin_id: str) -> dict | None:
        with db_lock:
            cur = db.execute(
                "SELECT config_json FROM grape_t_plugin_config_overlays"
                " WHERE scope=? AND scope_key=? AND plugin_id=?",
                (scope, scope_key, plugin_id))
            row = cur.fetchone()
        if row is None:
            return None
        return {"config_json": row[0]}

    def _swap_secrets_to_handles(self, config: dict, plugin_id: str,
                                 provenance: str) -> dict:
        """递归置换：密钥命名项 → 统一凭据库引用句柄。

        字符串值：非空明文（非句柄）→ 入凭据库换句柄；已是句柄幂等保留。
        容器值（dict/list，QA 审查修复）：密钥命名键下**整体**置换 ——
        容器 JSON 序列化入凭据库换句柄，不再递归下探（容器内明文零副本；
        空容器无明文可置换，原样保留）。返回深拷贝后的可落库 dict，
        不动调用方入参。
        """
        return self._swap_walk(config, plugin_id, provenance)

    def _swap_walk(self, node, plugin_id: str, provenance: str):
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                name = str(key)
                if (isinstance(value, (dict, list)) and value
                        and is_credential_name(name)):
                    # 密钥命名键下容器整体置换（QA Spec#2）：容器内明文
                    # 不再逐叶判定 —— 序列化入凭据库，overlay 只留句柄
                    out[key] = self._vault.put_secret(
                        plugin_id, name,
                        json.dumps(value, ensure_ascii=False),
                        owner_id=provenance)
                elif (isinstance(value, str) and value
                        and not is_handle(value)
                        and is_credential_name(name)):
                    out[key] = self._vault.put_secret(
                        plugin_id, name, value, owner_id=provenance)
                else:
                    out[key] = self._swap_walk(value, plugin_id, provenance)
            return out
        if isinstance(node, list):
            return [self._swap_walk(v, plugin_id, provenance) for v in node]
        return node
