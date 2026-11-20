"""Issue #646: 引擎/凭据稳定标识（engine_uid / credential_uid）派生与工具。

设计（Spec v3，见 docs/issue-646/spec.md 与 Issue #646 评论）：
- engine_uid = UUIDv5(项目固定 namespace, 自然键)，服务端在注册/心跳/本机刷新时派生。
  自然键 = 身份字段（credential_uid | name | scope | owner_id | host_name），
  展示名/凭据名等可变字段不参与——同一 worker 重复注册、表重建后重算均得到相同 uid
  （幂等防漂移，根治 delete-重插与 _rebuild_agent_runtimes_inst_key 重建导致的 id 漂移）。
  Issue #868 升格：云引擎（身份锚在场）派生剔除 name 段改用不可变锚，锚分量
  provider_id/template_id 进一步升格为 provider_uid/template_uid（UUIDv4 表示，
  表重建/库恢复/多主控聚合零漂移），详见 derive_engine_uid 文档。
- credential_uid = UUIDv4，凭据创建/首次回填时一次性生成，终身不变
  （credential_name 可修改，不能作派生源）。
- 存储统一小写连字符规范形（36 位字符串），SQLite TEXT / MySQL 兼容。

本模块为纯函数层：不 import DB，供 db_setup 迁移、user/db.py、runtime 注册链路共用。
"""
from __future__ import annotations

import uuid

# 项目固定 namespace（Issue #646 专用，一经发布不可变更，否则全量 uid 漂移）。
# 随机生成后固化的常量，与任何业务字段无关。
GRAPE_ENGINE_UID_NAMESPACE = uuid.UUID("7c1f4a9e-2b6d-4e83-9f0a-5d8c3b21e647")

_UID_RE_HEX = "0-9a-f"


def _norm(part) -> str:
    """自然键分量的规范化：None→''，去首尾空白，小写（引擎名语义不区分大小写）。"""
    if part is None:
        return ""
    return str(part).strip().lower()


def _cloud_anchor_present(engine_type: str, provider_id: str, template_id: str,
                          provider_uid: str = "", template_uid: str = "") -> bool:
    """云引擎不可变身份锚是否在场（任一分量非空即视为云身份）。"""
    return any(((engine_type or "").strip(), (provider_id or "").strip(),
                (template_id or "").strip(), (provider_uid or "").strip(),
                (template_uid or "").strip()))


def _norm_uid(value) -> str:
    """uid 分量规范化：None→''，去空白，小写（UUID 规范形）。"""
    return "" if value is None else str(value).strip().lower()


def cloud_anchor_from_runtime_info(rt: dict) -> tuple:
    """从 worker 注册 payload 的 runtime 条目提取云引擎不可变身份锚。

    Issue #868 交叉预警修正：i5（feat/devbox-sandbox-integration5）云引擎 name 为
    四段式复合名 ``engine_type:tpl_slug:provider:provider_id``，模板/Provider 改名后
    name 随 refresh 刷新（改名跟随）——name 是可变展示字段，不能作 uuid5 派生源。
    身份锚取 i5 runtime_info 透传的不可变字段：
    - engine_type（别名 engine）
    - provider_id（不变的平台侧 Provider 主键）
    - template_id（别名 template_db_id，模板库自增 id，模板改名不影响）
    - provider_uid / template_uid（Issue #868 种子分量 id→uid 升格：i5 侧
      grape_t_sandbox_provider_config.provider_uid / grape_t_sandbox_templates.template_uid，
      UUIDv4 创建时一次性生成终身不变；升格后 id 可不透传，uid 已足够定位行）

    返回五元组 ``(engine_type, provider_id, template_id, provider_uid, template_uid)``。
    普通引擎（本机 PATH 发现 / 普通 worker）不携带这些键 → 全空五元组，
    走 legacy 派生（name 本身稳定=锚，存量 uid 零漂移）。
    """
    if not isinstance(rt, dict):
        return ("", "", "", "", "")
    engine_type = rt.get("engine_type") if rt.get("engine_type") is not None else rt.get("engine")
    template_id = (rt.get("template_id")
                   if rt.get("template_id") is not None else rt.get("template_db_id"))
    return (
        "" if engine_type is None else str(engine_type).strip(),
        "" if rt.get("provider_id") is None else str(rt.get("provider_id")).strip(),
        "" if template_id is None else str(template_id).strip(),
        _norm_uid(rt.get("provider_uid")),
        _norm_uid(rt.get("template_uid")),
    )


def derive_engine_uid(credential_uid: str, name: str, scope: str,
                      owner_id: str, host_name: str = "",
                      engine_type: str = "", provider_id: str = "",
                      template_id: str = "", provider_uid: str = "",
                      template_uid: str = "") -> str:
    """按自然键确定性派生 engine_uid（UUIDv5，小写规范形）。

    派生源只取身份字段（inst key 语义），分三形态：

    - **云引擎 uid 表示（provider_uid + template_uid 成对在场，Issue #868
      种子分量 id→uid 升格）**：
      ``credential_uid|engine_type|scope|owner_id|host_name|provider_uid|template_uid``
      ——自增 id（多主控聚合撞号/部分恢复错配/表重建换值均不可靠）与 name
      均不参与；uid 分量与 credential_uid 同构（UUIDv4，i5 创建时一次性生成
      终身不变），表重建/库恢复后 uid 零漂移。
    - **云引擎 id 表示（过渡，仅透传 id 锚的老 worker）**：
      ``credential_uid|engine_type|scope|owner_id|host_name|provider_id|template_id``
      ——T13 行为逐字节不变；uid 分量缺一不成对时不切换（防两轮漂移），
      i5 升格透传 uid 后经注册路径锚定改绑/映射式回填切换到 uid 公式。
    - **普通引擎（无锚，legacy 公式，保持逐字节不变）**：
      ``credential_uid|name|scope|owner_id|host_name``
      ——普通引擎 name 本身是稳定锚（inst key 语义），无可变段；存量 uid 零漂移。
      本机直连行（credential_id=0）credential_uid 取 ''。
    - 换机器/换端口 → 自然键变化 → 新 uid（语义上即为新引擎实例）。
    """
    if (provider_uid or "").strip() and (template_uid or "").strip():
        key = "|".join((
            _norm(credential_uid),
            _norm(engine_type),
            _norm(scope),
            _norm(owner_id),
            _norm(host_name),
            _norm_uid(provider_uid),
            _norm_uid(template_uid),
        ))
    elif _cloud_anchor_present(engine_type, provider_id, template_id):
        key = "|".join((
            _norm(credential_uid),
            _norm(engine_type),
            _norm(scope),
            _norm(owner_id),
            _norm(host_name),
            _norm(provider_id),
            _norm(template_id),
        ))
    else:
        key = "|".join((
            _norm(credential_uid),
            _norm(name),
            _norm(scope),
            _norm(owner_id),
            _norm(host_name),
        ))
    return str(uuid.uuid5(GRAPE_ENGINE_UID_NAMESPACE, key))


def new_credential_uid() -> str:
    """凭据稳定键：UUIDv4（随机），创建/回填时一次性生成，终身不变。"""
    return str(uuid.uuid4())


def is_uid_str(value) -> bool:
    """是否为 36 位小写连字符 UUID 规范形（宽松接受大写，判定即可）。"""
    if not isinstance(value, str):
        return False
    v = value.strip().lower()
    if len(v) != 36:
        return False
    try:
        return str(uuid.UUID(v)) == v
    except (ValueError, AttributeError, TypeError):
        return False
