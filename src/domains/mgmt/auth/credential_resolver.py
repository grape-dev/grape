"""Issue #3427：凭据读侧单点解析器（带原因码 + 来源日志）。

架构归位（QA 复审 R1 硬阻断修复）：解析器需要读 mgmt/insight 的域内数据
（rc 行 / meta / external_identities），不得放在 kernel（R1：kernel 不得
import domains）。本模块落在 mgmt 域，经 mgmt.auth 直接暴露给
repos host / POST /api/issues / a2a 三入口；kernel 的 token_resolver
不再持有该职责，HTTP 状态提取保留在 domain 侧（runtime.vcs.token_error）。

语义与错误码分层见 src/kernel/credential_errors.py（唯一一份枚举/文案/映射）。
"""
from __future__ import annotations

from dataclasses import dataclass

from src.kernel.config import log
from src.kernel.ports import repos as _repos
from src.kernel.ports import tokens as _tokens
from src.kernel.ports.vcs_provider import VCSProviderPort


@dataclass
class CredentialResolution:
    """Issue #3427：带原因码的凭据解析结果。

    ok=True 时 token/source 有值（source ∈ {"rc", "global", "user"}）；
    ok=False 时 error_code ∈ CredentialErrorCode 值域（TOKEN_MISSING /
    TOKEN_DECRYPT_FAILED / TOKEN_REJECTED_UPSTREAM）。

    degraded 记录解析过程中的降级事实（如 `rc:decrypt_failed`、
    `global:rejected_upstream`），命中也保留——便于排障时知道为何选了低层。

    level 为策略链级别命名（repo_bot/global_bot/user），保留给调用方
    与既有测试对齐策略链词汇；source 才是驱动判定的字段。
    """
    ok: bool
    token: str = ""
    source: str = ""      # 命中层：rc（仓库级 bot）/ global（全局 bot）/ user（用户身份）
    login: str = ""       # probe 时上游返回的身份 login（托管复用，避免重复 GET /user）
    error_code: str = ""  # 失败原因码（CredentialErrorCode 值域）
    level: str = ""       # 兼容策略链级别命名（repo_bot/global_bot/user）
    degraded: "tuple[str, ...]" = ()


_SOURCE_TO_LEVEL = {"rc": "repo_bot", "global": "global_bot", "user": "user"}


def resolve_credential_detailed(repo_key: str, provider: "VCSProviderPort | None" = None,
                                probe: bool = False) -> "CredentialResolution | None":
    """凭据读侧单点解析器（固定顺序 + 原因码 + 来源日志）。

    固定顺序（Spec，与执行链策略序一致）：仓库级 bot（grape_t_repo_credentials,
    cred_type='bot'）→ 全局 bot → 用户身份（external_identities）。命中即用。
    与 get_token 的差异：get_token 把「未配置 / 解密失败 / 上游 401」全部折叠为
    空串，本函数保留原因供入口渲染可行动文案。

    路径 B（host_user_id 非空）跳过 rc/global，与 RepoBotToken/GlobalBotToken
    策略语义一致（路径 B 用用户自己的身份）。显式绑定仓库级 bot token 会清空
    host_user_id（与存量 set_repo_bot_token 一致），绑定后即回到路径 A。

    命中/回退语义（与 Spec「命中即用」一致，但命中必须可用）：
    - 某层解密失败 → 记录 degraded=layer:decrypt_failed，跳过该层继续回落；
    - 本地已标记过期（rc bot_token_status / 全局 bot meta）→ 记
      degraded=layer:marked_expired 并跳过该层；全链未命中时按
      TOKEN_REJECTED_UPSTREAM 归因（Spec 复审：过期不得与未配置同码）；
    - probe=True 时对命中 token 调 provider.verify_token（GET /user），
      401 → 记录 degraded=layer:rejected_upstream，继续回落下一层
      （避免失效上层遮蔽有效下层）；非 401（超时/5xx）不阻断，仍返回 token
      （R21.3.5 口径）。
    - 全链未命中时按 `rejected_upstream > marked_expired > decrypt_failed >
      missing` 优先级返回原因码（前两者同为 TOKEN_REJECTED_UPSTREAM）。

    repo_key 无效（空/无法解析 provider）返回 None，调用方按旧口径处理。
    """
    from src.kernel.repo_key import to_repo_key
    rk = to_repo_key(repo_key or "")
    if not rk:
        return None
    if provider is None:
        # 与 bind/host 路径同源（src.kernel.token_resolver 的工厂转发），
        # 便于调用方/测试按单一目标打桩。
        from src.kernel.token_resolver import create_provider_from_repo_key
        provider = create_provider_from_repo_key(rk)
    p_name = provider.provider_name
    from src.kernel.credential_errors import CredentialErrorCode

    _degraded: list[str] = []
    _decrypt_failed = False
    _marked_expired = False

    def _note(layer: str, reason: str) -> None:
        _degraded.append(f"{layer}:{reason}")
        log.warning(f"credential resolve degraded: repo={rk} provider={p_name} "
                    f"layer={layer} reason={reason}")

    def _fail(code: str) -> "CredentialResolution":
        log.info(f"credential resolve failed: repo={rk} provider={p_name} "
                 f"error_code={code} degraded={list(_degraded)}")
        return CredentialResolution(ok=False, error_code=code,
                                    degraded=tuple(_degraded))

    def _try_hit(token: str, source: str) -> "CredentialResolution | None":
        """命中层可用性判定：probe 401 返回 None 表示继续回落，其余返回结果。"""
        log.info(f"credential resolve: repo={rk} provider={p_name} source={source}")
        _res = CredentialResolution(ok=True, token=token, source=source,
                                    level=_SOURCE_TO_LEVEL.get(source, ""))
        if not probe:
            _res.degraded = tuple(_degraded)
            return _res
        _probed = _probe_credential_token(_res, provider)
        if _probed.ok:
            _probed.degraded = tuple(_degraded)
            return _probed
        if _probed.error_code == CredentialErrorCode.TOKEN_REJECTED_UPSTREAM.value:
            _note(source, "rejected_upstream")
            return None
        _probed.degraded = tuple(_degraded)
        return _probed

    # 路径 B 判定（与策略链同口径）；导入一次，第 3 层用户身份复用同一引用
    _path_b = False
    _get_host_uid = None
    try:
        from src.domains.mgmt.user.db import get_host_user_id as _get_host_uid
        _path_b = str(_get_host_uid(rk) or "") not in ("", "0")
    except Exception:
        _path_b = False

    from src.domains.mgmt.auth.credential_crypto import decrypt as _crypto_decrypt

    # ── 第 1 层：仓库级 bot（grape_t_repo_credentials, cred_type='bot'）──
    if not _path_b:
        try:
            from src.domains.insight import public as _insight_public
            raw_rc = _insight_public.repo_credential_value(rk, "bot")
        except Exception as e:
            log.warning(f"resolve_credential_detailed: rc read failed repo={rk}: {e}")
            raw_rc = ""
        _rc_legacy = False
        if not raw_rc:
            # Issue #3427（复审 Spec-5）：与执行链 RepoBotToken 同口径——rc 为空时
            # 回落 managed_repos 历史 bot 列（仅 gitcode/github 有物理列），避免仅存
            # legacy 列的仓库被误判 TOKEN_MISSING。
            try:
                _field = ""
                try:
                    _field = getattr(provider, "token_storage_field", lambda: "")() or ""
                except Exception:
                    _field = ""
                if _field:
                    _legacy_raw = _repos.repo_bot_token_from_legacy_column_any(rk, _field)
                    if _legacy_raw:
                        raw_rc = _legacy_raw
                        _rc_legacy = True
                        log.info(f"credential resolve: repo={rk} provider={p_name} "
                                 f"source=rc(legacy column)")
            except Exception as e:
                log.warning(f"resolve_credential_detailed: legacy rc read failed "
                            f"repo={rk}: {type(e).__name__}")
        if raw_rc:
            try:
                _tok = (_tokens.repo_secret_decrypt(raw_rc) if _rc_legacy
                        else _crypto_decrypt(raw_rc))
            except Exception as e:
                log.warning(f"credential resolve: rc decrypt failed repo={rk}: "
                            f"err_type={type(e).__name__}")
                _decrypt_failed = True
                _note("rc", "decrypt_failed")
                _tok = ""
            if _tok:
                try:
                    from src.domains.mgmt.public import repo_bot_token_status_any
                    if repo_bot_token_status_any(rk) == "expired":
                        log.info(f"credential resolve: rc token marked expired, "
                                 f"skip repo={rk}")
                        _marked_expired = True
                        _note("rc", "marked_expired")
                        _tok = ""
                except Exception as e:
                    log.warning(f"credential resolve: rc status check failed "
                                f"repo={rk}: err_type={type(e).__name__}")
                if _tok:
                    _res = _try_hit(_tok, "rc")
                    if _res is not None:
                        return _res

    # ── 第 2 层：全局 bot（平台行 / meta 表 global_bot_{provider}_token）──
    if not _path_b:
        try:
            from src.domains.mgmt.user.db import (get_global_bot_token_for,
                                                  global_bot_token_decrypt_failed)
            _gtok = get_global_bot_token_for(p_name)
            _g_expired = False
            try:
                _status_key = provider.global_bot_status_meta_key()
                from src.domains.mgmt.user.db import get_meta_value
                _g_expired = bool(_status_key) and get_meta_value(_status_key) == "expired"
            except Exception:
                _g_expired = False
            if _gtok and not _g_expired:
                _res = _try_hit(_gtok, "global")
                if _res is not None:
                    return _res
            elif _gtok and _g_expired:
                _marked_expired = True
                _note("global", "marked_expired")
            elif (not _gtok) and global_bot_token_decrypt_failed(p_name):
                # Issue #3427（复审 Spec-3）：平台行密文存在但解不开——与
                # 「未配置」区分，全链未命中时归 TOKEN_DECRYPT_FAILED。
                log.warning(f"credential resolve: global bot ciphertext decrypt "
                            f"failed provider={p_name}")
                _decrypt_failed = True
                _note("global", "decrypt_failed")
        except Exception as e:
            log.warning(f"resolve_credential_detailed: global read failed repo={rk}: {e}")

    # ── 第 3 层：用户身份（external_identities，host_user_id）──
    try:
        _host_uid = str(_get_host_uid(rk) or "") if _get_host_uid else ""
        if _host_uid not in ("", "0"):
            from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
            _acc = get_grape_account_by_id(_host_uid)
            if _acc:
                from src.domains.mgmt.public import stored_access_token_any
                raw_user = stored_access_token_any(_acc["id"], p_name)
                if raw_user:
                    try:
                        _utok = _crypto_decrypt(raw_user)
                    except Exception as e:
                        log.warning(f"credential resolve: user identity decrypt "
                                    f"failed repo={rk}: err_type={type(e).__name__}")
                        _decrypt_failed = True
                        _note("user", "decrypt_failed")
                        _utok = ""
                    if _utok:
                        _res = _try_hit(_utok, "user")
                        if _res is not None:
                            return _res
    except Exception as e:
        log.warning(f"resolve_credential_detailed: user layer failed repo={rk}: {e}")

    # ── 全链未命中 ──
    if (any(d.endswith(":rejected_upstream") for d in _degraded)
            or _marked_expired):
        return _fail(CredentialErrorCode.TOKEN_REJECTED_UPSTREAM.value)
    if _decrypt_failed:
        return _fail(CredentialErrorCode.TOKEN_DECRYPT_FAILED.value)
    return _fail(CredentialErrorCode.TOKEN_MISSING.value)


def _probe_credential_token(resolution: "CredentialResolution",
                            provider: "VCSProviderPort") -> "CredentialResolution":
    """解析成功后探活一次（GET /user）：上游 401 → TOKEN_REJECTED_UPSTREAM。

    与 Spec 风险缓解一致：仅在解析成功后探活；超时/5xx 不阻断（R21.3.5 口径）。
    401 时 token 已被上游明确拒绝，返回原因码（由调用方决定是否回落下一层）。
    日志仅记录状态码与异常类型，不落上游报文原文（防泄露 token/内部 URL）。
    """
    if not resolution or not resolution.ok or not resolution.token:
        return resolution
    from src.kernel.credential_errors import CredentialErrorCode
    try:
        _info = provider.verify_token(resolution.token)
        _login = (_info.get("login") or "") if isinstance(_info, dict) else ""
        if _login:
            resolution.login = _login
        return resolution
    except Exception as e:
        from src.domains.runtime.vcs.token_error import extract_http_status_from_exc
        http_code = extract_http_status_from_exc(e)
        if http_code == 401:
            log.info(f"credential probe: upstream rejected token (401) "
                     f"source={resolution.source}")
            return CredentialResolution(ok=False,
                                        error_code=CredentialErrorCode.TOKEN_REJECTED_UPSTREAM.value)
        log.warning(f"credential probe: non-401 error, keep token: "
                    f"source={resolution.source} status={http_code} "
                    f"err_type={type(e).__name__}")
        return resolution
