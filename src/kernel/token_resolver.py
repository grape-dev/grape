"""Token 获取统一接口（策略模式 + 职责链）。

Issue #372 R18: 统一 13 个散落函数为 TokenResolver 策略链。

设计文档: docs/weekly-08/token-config/04-token-resolver.md

模块结构:
  TokenStrategy (基类)
    ├── ExplicitRepoBotToken      (第 1 级，Issue #3713 链首)
    ├── RepoBotToken              (第 2 级)
    ├── GlobalBotToken            (第 3 级)
    └── UserToken                 (第 4 级)
  TokenResult (数据类)
  TokenResolver (统一入口)
  TokenActionHandler 家族 (回帖身份分派)

职责边界（2026-11-22 归位）：本模块**只**负责 token 解析与身份分派。VCS 平台
适配器（建仓/协作/webhook/命名空间清单）已迁到 src/domains/mgmt/vcs/（base.py +
factory.py + 各平台 <dialect>.py）；
本模块通过 src/kernel/ports/vcs_provider.py 端口取得 provider，不 import 任何平台实现。

三个语义入口（+ Issue #3713 追加的选择器）:
  get_token()           — 获取可用执行 token（含过期短路）
  read_stored_token()   — 读取存储原始值（不过期检查、不刷新）
  resolve()             — 获取 TokenResult（token + level + provider）
  detect_and_mark_expired() — fail_task 401 统一检测 + 标记
  resolve_exec_token()  — 唯一执行凭据选择器（换券面 + 建树面共用，带 stage 分类）
"""

import threading
import time

from abc import ABC, abstractmethod

from dataclasses import dataclass, field

import os

from typing import Optional

from src.kernel import repo_bot_observability as _repo_bot_obs
from src.kernel.config import log
from src.kernel.ports import accounts as _accounts
from src.kernel.ports import pipeline as _pipeline
from src.kernel.ports import repos as _repos
from src.kernel.ports import tokens as _tokens
from src.kernel.ports import vcs_cards as _vcs_cards
from src.kernel.ports import vcs_provider as _vcs_provider
from src.kernel.ports.vcs_provider import VCSProviderPort

# 凭据不可用分类（Issue #3713）——**唯一一份**枚举，两个消费面各自渲染：
#   换券面（HTTP）  424 + code + stage（前端/CLI 需区分该轮换还是该重新配置）
#   任务面（worker）task errors 里的 `credential_<stage>`，不再一律渲染成「git 元数据只读」
# 强统一成同一份 JSON 会把 HTTP 语义漏进 worker 任务上下文，所以只统一枚举。
# 值空（empty）自 2026-11-22 起按「无行」处理，不再是显式失败分类；
# 常量保留给既有对外文案/渲染表（历史 424 stage=empty 的措辞）。
CREDENTIAL_STAGE_EMPTY = "empty"
CREDENTIAL_STAGE_UNDECRYPTABLE = "undecryptable"
CREDENTIAL_STAGE_EXPIRED = "expired"
CREDENTIAL_STAGES = (CREDENTIAL_STAGE_EMPTY, CREDENTIAL_STAGE_UNDECRYPTABLE,
                     CREDENTIAL_STAGE_EXPIRED)


# ────────────────────────────────────────────────────────────────────────────
# Issue #3740：RepoBotToken 解析结果进程内短 TTL 缓存
#
# 动因：token 解析是热路径（每次 VCS 调用前都走），一次 issue 收尾会对同一
# (repo_key, field) 连解析 3-4 次——每次一次 rc 查询 + 一次 managed_repos 列读 +
# 解密。缓存把「同请求重复解析」折叠成一次，同时把 legacy 回落日志/审计写入
# 从 O(解析次数) 降到 O(存储读次数)。
#
# 口径（spec 已接受）：TTL ≤60s，凭据轮换最多延迟一个 TTL 窗口；同进程内写路径
# （rc upsert/clear）与过期标记路径会主动失效，轮换即时可见。TTL=0 关闭缓存。
# ────────────────────────────────────────────────────────────────────────────
_REPO_BOT_CACHE_DEFAULT_TTL = 60.0
_REPO_BOT_CACHE_MAX_ENTRIES = 2048
_repo_bot_cache: dict = {}


def repo_bot_token_cache_ttl() -> float:
    """解析缓存 TTL（秒）：``GRAPE_REPO_BOT_TOKEN_CACHE_TTL`` 可覆盖，钳制到 [0, 60]。"""
    raw = os.environ.get("GRAPE_REPO_BOT_TOKEN_CACHE_TTL", "")
    try:
        ttl = float(raw) if str(raw).strip() != "" else _REPO_BOT_CACHE_DEFAULT_TTL
    except (TypeError, ValueError):
        ttl = _REPO_BOT_CACHE_DEFAULT_TTL
    if ttl <= 0:
        return 0.0
    return min(float(ttl), _REPO_BOT_CACHE_DEFAULT_TTL)


def _repo_bot_cache_get(key: tuple):
    """取未过期缓存项 (expires_at, source, value, cred_id)；无/过期返回 None。"""
    entry = _repo_bot_cache.get(key)
    if not entry:
        return None
    if entry[0] <= time.monotonic():
        _repo_bot_cache.pop(key, None)
        return None
    return entry


#: 不入缓存的解析来源：存储读失败（``error``）以及「rc 读失败后 legacy 回落」
#: （``legacy_rc_error``）。两者都是瞬时故障态，缓存会把错误冻结一个 TTL 窗口。
_UNCACHEABLE_SOURCES = frozenset({"error", "legacy_rc_error"})


def _repo_bot_cache_put(key: tuple, source: str, value: str, cred_id: str = "") -> None:
    """写缓存。存储读失败 / rc 读失败后的 legacy 回落不入缓存——瞬时故障不该被冻结一个 TTL。"""
    ttl = repo_bot_token_cache_ttl()
    if ttl <= 0 or source in _UNCACHEABLE_SOURCES:
        return
    if len(_repo_bot_cache) >= _REPO_BOT_CACHE_MAX_ENTRIES:
        now = time.monotonic()
        for stale in [k for k, v in _repo_bot_cache.items() if v[0] <= now]:
            _repo_bot_cache.pop(stale, None)
        if len(_repo_bot_cache) >= _REPO_BOT_CACHE_MAX_ENTRIES:
            _repo_bot_cache.clear()
    _repo_bot_cache[key] = (time.monotonic() + ttl, source, value or "", cred_id or "")


def invalidate_repo_bot_token_cache(repo_key: str = "", field: str = "") -> int:
    """失效解析缓存：给定 repo_key（可选 field）清对应项，空参清全表。

    返回清理条数。rc 写路径（upsert/clear）、过期标记路径与运维手工刷新调用；
    失效范围 = 进程内，跨进程仍受 ≤60s TTL 约束。
    """
    if not repo_key:
        n = len(_repo_bot_cache)
        _repo_bot_cache.clear()
        return n
    if field:
        return 1 if _repo_bot_cache.pop((str(repo_key), str(field)), None) is not None else 0
    stale_keys = [k for k in _repo_bot_cache if k[0] == str(repo_key)]
    for key in stale_keys:
        _repo_bot_cache.pop(key, None)
    return len(stale_keys)


# 链首显式仓库 bot 行的级别标识（换券面 / 任务面 / 审计共用同一个词）
EXPLICIT_REPO_BOT_LEVEL = "rc_bot"


def get_bot_identity_token(repo_key: str) -> str:
    """回帖/评论专用：只解析 **bot 身份** token（仓库 bot → 全局 bot），不落用户 token。

    为什么需要（2026-11-15 实测）：路径 B 仓库（managed_repos.host_user_id 非空，如
    songspace/flask-blog）下，执行链按设计跳过 repo/global bot → 落到用户 token，
    于是 agent 处理完的回帖都挂在用户账号名下。回帖语义要求「系统/agent 发言」，
    必须以机器人账号（如 grace_bot）出镜；执行链（clone/推送）仍保持原优先级不变。
    """
    from src.kernel.repo_key import to_repo_key
    rk = to_repo_key(repo_key or "")
    if not rk:
        return ""
    try:
        provider = _vcs_provider.create_provider_from_repo_key(rk)
    except Exception as e:  # noqa: BLE001
        log.debug(f"get_bot_identity_token: provider 解析失败 ({repo_key}): {e}")
        return ""
    for strat in (RepoBotToken(provider, bypass_path_b=True),
                  GlobalBotToken(provider, bypass_path_b=True)):
        try:
            tok = strat.get_token(rk)
        except Exception as e:  # noqa: BLE001
            log.debug(f"get_bot_identity_token: {strat.level} 取 token 失败: {e}")
            tok = ""
        if tok:
            return tok
    return ""


@dataclass
class TokenResult:
    """token 获取结果，携带级别信息。"""
    token: str = ""               # token 字符串（向后兼容：薄包装取 .token 返回 str）
    level: str = ""               # "rc_bot" / "repo_bot" / "global_bot" / "user"（执行链实际生效级别，受过期短路影响）
    provider: Optional[VCSProviderPort] = None  # VCSProviderPort 实例
    repo_key: str = ""            # 仓库标识
    expired: bool = False         # 是否已标记过期（用户 token status=4 时 expired=True 但仍返回 token）
    issue_id: str = ""            # R20.6.1: 自循环检测用——is_self_loop_actor 需要查 running task
    config_level: str = ""        # FR-10.3: 配置级别（不受过期短路影响，供自循环检测读取）
    stage: str = ""               # Issue #3713: 显式失败分类（empty/undecryptable/expired），
                                  # 空串 = 未失败。换券面渲染 424+stage，任务面渲染
                                  # credential_<stage>（分类统一、渲染分离）。

class TokenStrategy:
    """token 获取策略基类。每个子类代表 fallback 链中的一级。

    方法分层契约（关键——决定了上层 get_token / read_stored_token 的语义分离）：
    - _fetch(): 纯读存储值。不检查过期、不刷新、不调 VCS API。只查 DB / meta 表。
      供 read_stored_token() 和 get_token() 共用，是两者的公共底层。
    - _is_expired(): 查过期标记。只读标记字段，不调 VCS API。
    - get_token(): = _fetch() + _is_expired() 检查。过期则短路返回空串。
      这是"执行 token"语义——需要当前可用的 token。
    """

    error_code = ""  # 子类覆盖

    def __init__(self, provider: VCSProviderPort):
        self.provider = provider

    def get_token(self, repo_key: str) -> str:
        """获取可用执行 token。_fetch 读值 → _is_expired 检查过期 → 过期则短路返回空串。"""
        token = self._fetch(repo_key)
        if token and self._is_expired(repo_key):
            return ""  # 短路：已标记过期，不返回
        return token

    def _normalize_repo_key(self, repo_key: str) -> str:
        """标准化 repo_key（to_repo_key）。空则返回空串。子类复用避免重复 import。"""
        from src.kernel.repo_key import to_repo_key
        rk = to_repo_key(repo_key)
        return rk or ""

    def _is_path_b(self, repo_key: str) -> bool:
        """路径 B 仓库判断（str(host_user_id or "") not in ("", "0")）。子类复用避免重复 import get_host_user_id。"""
        return str(self._get_host_user_id(repo_key) or "") not in ("", "0")

    def _get_host_user_id(self, repo_key: str) -> "str | int":
        """获取 host_user_id。子类复用避免重复 import。"""
        return _tokens.get_host_user_id(repo_key)

    @abstractmethod
    def _fetch(self, repo_key: str) -> str:
        """纯读存储值。不检查过期、不刷新、不调 VCS API。只查 DB / meta 表。"""
        raise NotImplementedError

    def _is_expired(self, repo_key: str) -> bool:
        """查过期标记。只读标记字段，不调 VCS API 验证。默认 False。"""
        return False

    def mark_expired(self, repo_key: str):
        """子类实现：标记 token 过期。"""
        pass

    def clear_expired(self, repo_key: str):
        """子类实现：清除过期标记。"""
        pass

    @property
    def level(self) -> str:
        """策略级别标识。子类覆盖。"""
        return ""

class ExplicitRepoBotToken(TokenStrategy):
    """第 1 级（Issue #3713 链首）：显式配置的仓库 bot 行
    `grape_t_repo_credentials(repo_key, cred_type='bot')`。

    为什么必须新增一级，而不是给 RepoBotToken 加开关（评审 #202773482 修正 2）：
      · `RepoBotToken._fetch` 的 path-B 守卫在 rc 读取**之前**（`_is_path_b()` → return ""），
        于是 path-B 私有仓在策略链里根本读不到这一行；而换券路由在守卫之外直读它。
        同一份数据两个答案 → 换券 200、建树空 token、mirror refs=0、零交付仍 completed。
      · 给策略链开 bypass 开关去落地「统一」会同时解锁 RepoBot 与 GlobalBot 两级
        （见 TokenResolver.__init__）→ path-B 私仓拿到全局 bot 身份，
        那不是修 bug，是开身份越界口子。

    本级别只读这一行、不做 path-B 守卫（显式配置 = 显式意图）。
    可用性口径（2026-11-22 二次收敛）：
      · 空值（=无行）   → 跳过本级，交回原链（840431a8b）；
      · 过期标记滞后    → 跳过本级（C3）；
      · 解不开（unknown kid / 密钥不匹配）→ 不可用 → 跳过本级，回落仓库 bot/平台 bot；
        绝不把一次配置/轮换差异升级成流水线硬失败（gitee rc 毒行事故）。
    """

    error_code = "REPO_TOKEN_EXPIRED"   # 与 RepoBotToken 同一回写列（bot_token_status）

    @property
    def level(self) -> str:
        return EXPLICIT_REPO_BOT_LEVEL

    def _read_row(self, repo_key: str) -> Optional[dict]:
        """读 rc 行（值 + 审计锚点 + 解不开标记）。无行返回 None。"""
        try:
            return _tokens.get_repo_credential_meta(repo_key, "bot")
        except Exception as e:
            log.warning(f"ExplicitRepoBotToken: rc read failed: {e}")
            return None

    def _fetch(self, repo_key: str) -> str:
        """纯读 rc 值（不查过期——过期语义在 get_token/_is_expired）。"""
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return ""
        row = self._read_row(repo_key)
        if not row or row.get("undecryptable"):
            return ""
        value = str(row.get("value") or "")
        if not value:
            return ""
        self._audit_hit(row, repo_key)
        return value

    def _is_expired(self, repo_key: str) -> bool:
        """过期标记唯一来源：`managed_repos.bot_token_status == 'expired'`。

        与 RepoBotToken._is_expired 读同一列——**不要**假定 rc 行内有独立过期字段，
        否则守卫用例无法在旧码上构造出红（评审 #202773660 §二）。
        """
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return False
        try:
            return _repos.repo_bot_token_status_any(repo_key) == "expired"
        except Exception as e:
            log.debug(f"ExplicitRepoBotToken._is_expired: {e}")
        return False

    def resolve(self, repo_key: str) -> "tuple[str, str]":
        """一次读行 → `(可用值, stage)`：值为空且 stage 为空 = **无行**（交回原链）。

        合并读的原因：`get_token` / `resolve` / `resolve_exec_token` 每次都要问链首，
        先 `_fetch` 再 `stage` 会把同一条 rc 行查两遍（行缺失时也两遍）。
        """
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return "", ""
        row = self._read_row(repo_key)
        if not row:
            return "", ""
        if row.get("undecryptable"):
            # 解不开 ≠ 该仓没配：但也不能升级成硬失败（2026-11-22 gitee 事故）。
            # unknown kid（异主机/旧派生密钥写的行）按「不可用 → 回落」处理：
            # 跳过本级，落到仓库 bot / 平台 bot，任务与换券都不中断。
            _why = str(row.get("reason") or "decrypt_failed")
            log.warning(f"ExplicitRepoBotToken: rc row unusable ({_why}), skip to next level "
                        f"repo={repo_key}（密钥不匹配是配置/轮换问题，执行面降级）")
            return "", ""
        value = str(row.get("value") or "")
        if not value:
            # 空值 = 没有这行（840431a8b 定的契约）：写侧已改「空 → 删行」，存量空行
            # （gitee 仓库的历史空占位）也只有落回原链才不误伤；判显式失败会让这些仓
            # 424 / credential_empty —— 正是 6 个 gitee 仓库曾被卡死的形态。
            return "", ""
        if self._is_expired(repo_key):
            # 过期 ≠ 配置坏了：基线语义是「跳过这一级、落到下一档」。
            # 这里恢复该语义（2026-11-22 评审 C3）：否则一次标记滞后就把本来
            # 能跑的任务判 failed。明细分两类——expired 降级，空/解不开才算显式失败。
            log.warning(f"ExplicitRepoBotToken: rc row marked expired, skip to next level "
                        f"repo={repo_key}（换凭据是运维动作；执行面不因此中断）")
            return "", ""
        self._audit_hit(row, repo_key)
        return value, ""

    def _audit_hit(self, row: dict, repo_key: str) -> None:
        """D90-N4 bot token 消费审计（与 RepoBotToken 的 rc 命中同一 operation）。"""
        try:
            from src.kernel.persistence.credential_usage_log import log_usage
            log_usage(row.get("id") or "", 0,
                      credential_kind="repo_credentials",
                      executor_context="token_resolve",
                      operation="repo_bot_hit", repo_key=repo_key)
        except Exception:
            pass  # 审计失败不阻断

    def expired(self, repo_key: str) -> bool:
        """该仓显式 rc 行是否已被标记过期。

        执行面（resolve/stage）把 expired 当「跳过本级」处理（C3），所以 stage 不再返回
        expired；但**鉴权面**必须能把过期旧值挡在门外（read_stored_token 不查过期），
        否则 A2A 会拿一枚已知过期的 token 去调上游。故单独暴露这一个查询。
        """
        return self._is_expired(repo_key)

    def stage(self, repo_key: str) -> str:
        """显式行的可用性分类：'' = 无行或可用；否则 empty/undecryptable/expired。

        三态必须分开（换券路由曾把「无行」与「行存在但空」混成一个信号，踩过两次坑）：
          · 无行      → ''，交回原链（path-A 仓库/全局 bot；path-B 归属者）；
          · 解不开    → 视同不可用但**不判失败**（unknown kid / 密钥不匹配 → 跳过本级，回落下一档）；
          · 值被清空  → 视同无行（写侧空值即删行；存量空行交回原链，840431a8b）；
          · 已标过期  → 视同不可用但**不判失败**（跳过本级，落到下一档；C3）。
        """
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return ""
        row = self._read_row(repo_key)
        if not row:
            return ""
        if row.get("undecryptable"):
            return ""   # 解不开 = 不可用 → 跳过本级（与 resolve 同口径；C3/毒行事故）
        if not str(row.get("value") or ""):
            return ""   # 空值 = 无行（840431a8b），交回原链
        if self._is_expired(repo_key):
            return ""   # 过期 = 跳过这一级（C3 回滚硬失败；由下一档兜底）
        return ""

    def mark_expired(self, repo_key: str):
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return
        try:
            _repos.set_repo_bot_token_status_any(repo_key, "expired")
        except Exception as e:
            log.warning(f"ExplicitRepoBotToken.mark_expired: {e}")

    def clear_expired(self, repo_key: str):
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return
        try:
            _repos.set_repo_bot_token_status_any(repo_key, "")
        except Exception as e:
            log.warning(f"ExplicitRepoBotToken.clear_expired: {e}")


# ── Issue #3897 D：RepoBotToken 历史列回退日志降噪 ─────────────────────────────
# 生产 3.9 万条/小时 `historical column fallback hit (rc miss)`：token 解析是高频路径，
# 每条历史数据命中都打 WARNING 会把真正的告警淹掉。降噪口径（验收：0 条/h 或 debug）：
#   · 默认 DEBUG（仍可 `log.debug` 取证 / 灰度期临时调级别）；
#   · 同一 `(repo_key, field)` 保留 1 条 WARNING / 小时——下线判据 grep 仍能拿到
#     「还有哪些仓库在吃历史列」，不至于降噪后彻底失明；
#   · 按 repo 限频（不是全局一次），避免一条日志掩盖受影响面。
_FALLBACK_WARN_INTERVAL_SECONDS = 3600.0
_fallback_warn_at: dict = {}
_fallback_warn_lock = threading.Lock()


def _legacy_fallback_log(repo_key: str, field: str) -> None:
    """历史列命中日志：DEBUG 常态 + 每 (repo, field) 每小时最多 1 条 WARNING。"""
    _key = (repo_key, field)
    _now = time.monotonic()
    with _fallback_warn_lock:
        _last = _fallback_warn_at.get(_key, 0.0)
        _should_warn = (_now - _last) >= _FALLBACK_WARN_INTERVAL_SECONDS
        if _should_warn:
            _fallback_warn_at[_key] = _now
    _msg = (f"RepoBotToken: historical column fallback hit (rc miss) "
            f"repo={repo_key} field={field}")
    if _should_warn:
        log.warning(_msg)
    else:
        log.debug(_msg)


class RepoBotToken(TokenStrategy):
    """第 2 级：managed_repos.{token_storage_field}

    bypass_path_b=True：回帖/评论专用身份解析——路径 B（用户自己的仓库）也允许用 bot 身份。
    默认 False 保持历史语义（路径 B 降级为用户 token），避免改到执行链。
    """

    error_code = "REPO_TOKEN_EXPIRED"

    def __init__(self, provider: VCSProviderPort, bypass_path_b: bool = False):
        super().__init__(provider)
        self.bypass_path_b = bypass_path_b

    @property
    def level(self) -> str:
        return "repo_bot"

    def _fetch(self, repo_key: str) -> str:
        """读仓库 bot token：先看进程内短 TTL 缓存，未命中才真正解析。

        缓存的 key 是 ``(repo_key, field)``——field 参与 key，是因为同一仓库的
        gitcode/github 两套存量列在平台切换前后可能各命中一次，混用会串值。
        """
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return ""
        # 路径 B 仓库跳过仓库级 bot token（降级场景脏写入防护）；
        # 回帖身份解析传 bypass_path_b=True —— agent 的评论必须来自 bot 账号。
        if self._is_path_b(repo_key) and not self.bypass_path_b:
            return ""
        field = self.provider.token_storage_field()  # bot_gitcode_token / bot_github_token
        cache_key = (repo_key, field or "")
        cached = _repo_bot_cache_get(cache_key)
        if cached is not None:
            return cached[2]
        token, source, cred_id = self._resolve_bot_token(repo_key, field)
        _repo_bot_cache_put(cache_key, source, token, cred_id)
        return token

    def _resolve_bot_token(self, repo_key: str, field: str) -> "tuple[str, str, str]":
        """真正解析 bot token，返回 ``(token, source, cred_id)``。

        source ∈ {``rc``, ``legacy``, ``legacy_rc_error``, ``none``, ``error``}：
        ``error`` = 存储读失败、``legacy_rc_error`` = rc 读失败后回落的 legacy 值——
        二者均不写缓存（瞬时故障不该被冻结一个 TTL）；``none`` = rc miss 且 legacy
        列空（含 rc miss 标记，正是要缓存的那类重复解析）。
        审计口径 =「一次实际存储读一行」：缓存命中不经过本函数，故写入随缓存降频
        （Issue #3740 明列的写放大待消除项）。
        """
        _rc_error = False
        # S82：grape_t_repo_credentials 唯一权威（rc 优先无条件；列=历史回落）
        # D99：cred_type 归一='bot'（值域 {'bot','webhook'}，平台由 repo_key 承载）
        try:
            _rc = _tokens.get_repo_credential_meta(repo_key, "bot")
            if _rc and _rc.get("value"):
                # D90-N4（拍板 1A/2B/3A）：bot token 消费审计（全量，Core 通道）
                try:
                    from src.kernel.persistence.credential_usage_log import log_usage
                    log_usage(_rc.get("id") or "", 0,
                              credential_kind="repo_credentials",
                              executor_context="token_resolve",
                              operation="repo_bot_hit", repo_key=repo_key)
                except Exception:
                    pass  # 审计失败不阻断
                return _rc["value"], "rc", str(_rc.get("id") or "")
        except Exception as _rc_err:
            _rc_error = True
            log.warning(f"RepoBotToken: grape_t_repo_credentials read failed: {_rc_err}")
        # F4/D106（终态第一批）：卡列空平台（gitee/gitlab）无物理列——
        # legacy 列回落分支对其不存在，直接返回空（不触发下方 SQL 报错噪音）。
        if not field:
            return "", ("error" if _rc_error else "none"), ""
        # ------------------------------------------------------------------
        # LEGACY 列回落分支（F4/D106 显式标记）：
        # 仅为 gitcode/github 存量行服务；gitee/gitlab 侧由 D106 回填迁移
        # （db_setup.backfill_gitee_gitlab_token_columns_to_rc）迁入 rc 后
        # 写路径置空，本分支对其零命中。
        # 独立消费审计（operation=repo_bot_hit_legacy_column，与 rc 命中的
        # repo_bot_hit 区分）：回填窗口内旧列消费不留审计盲区。
        # 退出 checklist（满足后本分支可删）：
        # ①回填迁移全量盖章 → ②写路径轮换置空存量列 → ③生产观察期
        #   grape_repo_bot_legacy_fallback_total 窗口增量 == 0 **且** 审计
        #   operation=repo_bot_hit_legacy_column 零命中（下线判据）→
        # ④删除本分支 + 评估 DROP COLUMN。
        #
        # Issue #3740：counter 全量计数（日志可降噪，计数不可）。日志降噪沿用
        # 基线 Issue #3897 口径（DEBUG 常态 + 每 (repo, field) 每小时 1 条
        # WARNING）；日志文本保持原样，仅用于 grep 定位——**日志不再是下线
        # 判据**，判据只认 counter 窗口增量与审计零命中。
        # ------------------------------------------------------------------
        try:
            # managed_repos 归 mgmt：列名与语句都由归属域持有（本文件不再拼 SQL）。
            # 任意拓扑原语：自有 DSN → mgmt 连接；单库回落进程库（同一份语句）。
            raw = _repos.repo_bot_token_from_legacy_column_any(repo_key, field)
            if raw:
                provider, repo = _repo_bot_obs.repo_labels(repo_key)
                _repo_bot_obs.observe_legacy_fallback(provider, repo)
                _legacy_fallback_log(repo_key, field)
                # N3：managed_repos 列同走通道加密，读侧解密（明文兼容覆盖存量）
                # F4/D106：legacy 列命中独立消费审计（回填期旧列消费可观测，
                # 与 rc 命中审计分离——旧列消费清零是回落分支下线前置）
                try:
                    from src.kernel.persistence.credential_usage_log import log_usage
                    log_usage("", 0, credential_kind="repo_credentials",
                              executor_context="token_resolve",
                              operation="repo_bot_hit_legacy_column",
                              repo_key=repo_key)
                except Exception:
                    pass
                # rc 读失败时仍回落 legacy 值（可用性优先），但**不缓存**：
                # 瞬时 rc 故障不该把 legacy 结果冻结一个 TTL；下一次解析必须
                # 重新尝试 rc，避免过期 legacy token 被缓存拖过一个窗口
                # （复审 Required#3）。source 用独立标记，缓存层据此跳过。
                return (_tokens.repo_secret_decrypt(raw),
                        ("legacy_rc_error" if _rc_error else "legacy"), "")
            return "", ("error" if _rc_error else "none"), ""
        except Exception as e:
            log.warning(f"RepoBotToken._fetch: {e}")
            return "", "error", ""

    def _is_expired(self, repo_key: str) -> bool:
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return False
        try:
            return _repos.repo_bot_token_status_any(repo_key) == "expired"
        except Exception as e:
            log.debug(f"RepoBotToken._is_expired: {e}")
        return False

    def mark_expired(self, repo_key: str):
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return
        try:
            _repos.set_repo_bot_token_status_any(repo_key, "expired")
        except Exception as e:
            log.warning(f"RepoBotToken.mark_expired: {e}")
        # Issue #3740：过期标记使缓存值失效——回帖链在 401 后必须读到轮换后的新值，
        # 不能继续吃 TTL 窗口内的旧 token。
        invalidate_repo_bot_token_cache(repo_key)

    def clear_expired(self, repo_key: str):
        repo_key = self._normalize_repo_key(repo_key)
        if not repo_key:
            return
        try:
            _repos.set_repo_bot_token_status_any(repo_key, "")
        except Exception as e:
            log.warning(f"RepoBotToken.clear_expired: {e}")
        invalidate_repo_bot_token_cache(repo_key)

class GlobalBotToken(TokenStrategy):
    """第 3 级：全局 bot token（meta 表 {global_bot_meta_key}）

    bypass_path_b=True：回帖/评论专用身份解析（理由同 RepoBotToken）。
    """

    error_code = "GLOBAL_TOKEN_EXPIRED"

    def __init__(self, provider: VCSProviderPort, bypass_path_b: bool = False):
        super().__init__(provider)
        self.bypass_path_b = bypass_path_b

    @property
    def level(self) -> str:
        return "global_bot"

    def _fetch(self, repo_key: str) -> str:
        # 路径 B 仓库跳过全局 bot token（§13.1）；回帖身份解析允许 bypass。
        if self._is_path_b(repo_key) and not self.bypass_path_b:
            return ""
        # 复用现有 _cached_get_meta 缓存逻辑
        # S87/D63：统一 get_global_bot_token_for（meta 派生键 + 存量两平台文件 fallback）
        token = _tokens.get_global_bot_token_for(self.provider.provider_name)
        # D91：缺失告警（路径 A 全局 bot 是主力凭据，静默空串会让"配置错误不可见"
        # ——失败只在任务报错时暴露，运维无从区分"未配置"与"配置错/被误删"）。
        # 路径 A UserToken 不生效，此处空串=策略链必然失败，告警是唯一观测点。
        if not token:
            log.warning(
                f"global bot token missing for provider={self.provider.provider_name} "
                f"(repo_key={repo_key}): 路径 A 仓库策略链将无可用凭据——"
                f"请检查 meta key global_bot_{self.provider.provider_name}_token 或文件 fallback"
            )
        return token

    def _is_expired(self, repo_key: str) -> bool:
        status_key = self.provider.global_bot_status_meta_key()
        try:
            return _tokens.get_meta_value(status_key) == "expired"
        except Exception:
            return False

    def mark_expired(self, repo_key: str):
        status_key = self.provider.global_bot_status_meta_key()
        try:
            _tokens.set_meta_value(status_key, "expired")
            _tokens.invalidate_global_bot_cache()
        except Exception as e:
            log.warning(f"GlobalBotToken.mark_expired: {e}")

    def clear_expired(self, repo_key: str):
        status_key = self.provider.global_bot_status_meta_key()
        try:
            _tokens.set_meta_value(status_key, "")
            _tokens.invalidate_global_bot_cache()
        except Exception as e:
            log.warning(f"GlobalBotToken.clear_expired: {e}")

class UserToken(TokenStrategy):
    """第 4 级：用户 token（external_identities，带刷新）。

    特殊行为：
    - _fetch 内部调 get_user_token_with_refresh（含 OAuth 自动刷新）
    - R25.3: _is_expired 检查 status=4 → 执行链短路返回空串（与其他层一致）
    - 回帖链通过 UserTokenHandler.get_comment_token() 调 _fetch() 绕过过期检查（best-effort）
    - 新增 _fetch_stored() 方法：只读 external_identities.access_token，不触发 refresh。
      供 read_stored_token() 调用。
    """

    error_code = "TOKEN_EXPIRED"

    @property
    def level(self) -> str:
        return "user"

    def _fetch(self, repo_key: str) -> str:
        host_user_id = self._get_host_user_id(repo_key)
        if str(host_user_id or "") in ("", "0"):
            return ""  # 路径 A 不走用户 token
        try:
            account = _accounts.get_grape_account_by_id(host_user_id)
            if account and account.get("username"):
                return self.provider.fetch_user_token(account["username"], host_user_id)
        except Exception as e:
            log.warning(f"UserToken._fetch: {e}")
        return ""

    def _fetch_stored(self, repo_key: str) -> str:
        """只读 external_identities.access_token（解密），不触发 OAuth refresh。

        供 read_stored_token() 调用——A2A 鉴权 / session 写入只需原始值。
        """
        host_user_id = self._get_host_user_id(repo_key)
        if str(host_user_id or "") in ("", "0"):
            return ""
        try:
            account = _accounts.get_grape_account_by_id(host_user_id)
            if not account:
                return ""
            # external_identities 归 mgmt：列序与 status IN (1, 4) 口径由归属域持有
            raw = _accounts.stored_access_token_any(account["id"], self.provider.provider_name)
            if raw:
                return _tokens.decrypt(raw)
        except Exception as e:
            log.warning(f"UserToken._fetch_stored: {e}")
        return ""

    def _is_expired(self, repo_key: str) -> bool:
        """R25.3: 检查 status=4（token 过期），执行链短路返回空串。

        与 RepoBotToken / GlobalBotToken 三层一致——
        过期标记后 get_token() 短路，不再返回过期 token。

        回帖链通过 UserTokenHandler.get_comment_token() 调 _fetch() 绕过此检查，
        保留 best-effort 发评论能力。
        """
        host_user_id = self._get_host_user_id(repo_key)
        if str(host_user_id or "") in ("", "0"):
            return False  # 路径 A 仓库不走用户 token
        return _accounts.is_identity_expired(host_user_id, self.provider.provider_name)

    def mark_expired(self, repo_key: str):
        host_user_id = self._get_host_user_id(repo_key)
        if str(host_user_id or "") not in ("", "0"):
            _accounts.mark_identity_expired(host_user_id, self.provider.provider_name)

    def clear_expired(self, repo_key: str):
        """用户重新绑定时 create/update_external_identity 已恢复 status=1，无需额外操作。"""
        pass

def detect_and_mark_expired(repo_key: str) -> str:
    """模块级便捷入口：按 repo_key 造 provider → TokenResolver 验证并标记过期。

    IM 直连执行（vod/chat handler）401 时需要；实现本是 TokenResolver 的方法，
    原先按模块函数导入必然 ImportError，这里补薄封装，口径与 service.fail_task R18 一致。
    """
    try:
        _provider = _vcs_provider.create_provider_from_repo_key(repo_key)
        return TokenResolver(_provider).detect_and_mark_expired(repo_key) or ""
    except Exception as e:
        log.warning(f"detect_and_mark_expired({repo_key}) failed: {e}")
        return ""


def resolve_exec_token(repo_key: str) -> TokenResult:
    """模块级便捷入口（Issue #3713）：按 repo_key 造 provider → 统一执行凭据选择器。

    建树面（worker `repogit`）与换券面（mgmt 路由）都从这里取判定，避免再各写一遍。
    provider 造不出来时返回空结果（stage 空 = 无法归因，交回上层原语义）。
    """
    try:
        return TokenResolver(_vcs_provider.create_provider_from_repo_key(repo_key)).resolve_exec_token(repo_key)
    except Exception as e:
        log.warning(f"resolve_exec_token({repo_key}) failed: {e}")
        return TokenResult(token="", level="", repo_key=repo_key or "")


class TokenResolver:
    """token 获取统一入口。按优先级链执行策略，第一个返回非空 token 的策略胜出。

    三个语义入口：
    - get_token(): 获取可用执行 token（含过期短路）。供 pipeline / agent / clone / push 用。
    - read_stored_token(): 读取存储原始值（不过期检查、不刷新）。供 A2A 鉴权 / session 写入用。
    - resolve(): 获取 TokenResult（token + level + provider）。供需要级别差异的调用方用。
    - resolve_config_level(): 获取配置级别（不检查过期、不触发 refresh）。供自循环检测用。
    - detect_and_mark_expired(): fail_task 401 统一检测 + 标记。
    """

    def __init__(self, provider: VCSProviderPort):
        """链序固定：显式仓库 bot 行 → 仓库 bot → 全局 bot → 用户凭据，path-B 守卫全部保留。

        **不提供 bypass_path_b**（2026-11-21 撤除）：一旦暴露这个参数，调用方很容易
        对 git-credential 这类路径裸传 True —— 那会把「仓库无 bot、平台有全局 bot」的
        path-B 私仓推到全局 bot 账号上（通常不是协作者 → push 403），是身份错用。
        需要「回帖必须来自 bot 账号」语义的调用方，直接用策略级的
        get_bot_identity_token()（内部 RepoBotToken/GlobalBotToken 自带 bypass）。
        """
        self.provider = provider
        # 策略链序 = 架构常量（D90-N2② 2026-11-07；D101 方案三 2026-11-08
        # 四改三——第 1 级 DevopsAgentSessionToken 退役：R25.9 回声治理后
        # session token 列零写入、策略恒空转，死级清除）。
        # 每级回答一个归属问题——
        # 1 仓库 bot → 2 全局 bot → 3 用户凭据。
        # 变更链序 = 架构变更，须走契约评审（评审载体=
        # docs/weekly-09/d101-task-token-snapshot-retirement-design.md §二）；
        # source/cred_type 只是元数据（审计归因/观测分轨），不驱动排序
        # （动态排序会使命中序列不可比、挂账 #12 零命中判据失效）。
        self.strategies = [
            # 0 显式配置的仓库 bot 行（#3713）→ 1 仓库 bot → 2 全局 bot → 3 用户凭据。
            # 链首这一级只读 rc 行、不做 path-B 守卫：显式配置的意图必须优先于
            # 「path-B 降级」策略，否则同一份数据在换券面/建树面会给出两种答案。
            ExplicitRepoBotToken(provider),
            RepoBotToken(provider),
            GlobalBotToken(provider),
            UserToken(provider),
        ]

    def _explicit_head(self):
        """链首显式级（按 level 查，不按下标/类型——调用方会裁剪策略链）。"""
        for strategy in self.strategies:
            if strategy.level == EXPLICIT_REPO_BOT_LEVEL:
                return strategy
        return None

    def _explicit_stage(self, repo_key: str) -> str:
        """链首显式 rc 行的不可用分类（'' = 无行或可用）。"""
        head = self._explicit_head()
        return head.stage(repo_key) if head is not None else ""

    def _explicit_resolve(self, repo_key: str) -> "tuple[str, str]":
        """链首一次读行 → `(值, stage)`；无该级（链被裁剪）→ `("", "")`。"""
        head = self._explicit_head()
        if head is None:
            return "", ""
        return head.resolve(repo_key)

    def resolve_exec_token(self, repo_key: str) -> TokenResult:
        """唯一执行凭据选择器（Issue #3713）：换券面与建树面共用同一份判定。

        语义（评审 #202773482 修正 2，落地形状）：
          ① rc 行可用            → 用它（level=rc_bot）；
          ② rc 行存在但不可用     → 显式失败（token 空 + stage 分类，**不降级**）；
          ③ 无 rc 行             → 落原链（path-A 仓库/全局 bot；path-B 归属者），
                                  身份守卫天然保留（不经 bypass_path_b）。

        返回的 `stage` 是「分类」，各消费面自行渲染：
          换券面 → HTTP 424 + code + stage；任务面 → task errors 的 `credential_<stage>`。
        """
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        from src.kernel.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        head = self._explicit_head()
        if head is not None:
            explicit_token, stage = self._explicit_resolve(repo_key)
            if stage:
                return TokenResult(token="", level=EXPLICIT_REPO_BOT_LEVEL,
                                   provider=self.provider, repo_key=repo_key,
                                   config_level=EXPLICIT_REPO_BOT_LEVEL, stage=stage)
            if explicit_token:
                return TokenResult(token=explicit_token, level=EXPLICIT_REPO_BOT_LEVEL,
                                   provider=self.provider, repo_key=repo_key,
                                   config_level=EXPLICIT_REPO_BOT_LEVEL)
        for strategy in self.strategies:
            if strategy is head:
                continue
            token = strategy.get_token(repo_key)
            if token:
                return TokenResult(token=token, level=strategy.level,
                                   provider=self.provider, repo_key=repo_key,
                                   expired=strategy._is_expired(repo_key),
                                   config_level=strategy.level)
        return TokenResult(token="", level="", provider=self.provider, repo_key=repo_key)

    def get_token(self, repo_key: str) -> str:
        """获取可用执行 token。含过期短路。返回空串=无可用 token。

        Issue #3713：链首显式 rc 行「存在但不可用」时**不落链**——这是显式失败语义
        （修凭据是运维动作）。否则过期/被清空的行会被下两级悄悄顶替成别的身份。
        """
        if not repo_key:
            return ""
        from src.kernel.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        if not repo_key:
            return ""
        head = self._explicit_head()
        if head is not None:
            explicit_token, stage = self._explicit_resolve(repo_key)
            if stage:
                log.warning(f"TokenResolver.get_token: explicit repo bot credential unusable "
                            f"({stage}) repo={repo_key} —— 不降级（身份不能混）")
                return ""
            if explicit_token:
                return explicit_token
        for strategy in self.strategies:
            if strategy is head:
                continue
            token = strategy.get_token(repo_key)
            if token:
                return token
        return ""

    def read_stored_token(self, repo_key: str) -> str:
        """读取存储的原始 token 值。不过期检查、不触发 OAuth refresh、不调 VCS API。

        契约（写死，不可变）：
        1. 不检查过期标记——即使 token 被标记 expired 也返回原始值
        2. 不触发 OAuth refresh——UserToken 策略用 _fetch_stored 跳过 refresh
        3. 走缓存层——_cached_get_meta 的 60s 缓存仍生效
        4. 返回空串=该 repo 无任何 token 存储（不是"过期"，是"未配置"）

        供 A2A 鉴权（get_repo_bot_token_no_global）/ session 写入用。

        Issue #3713 提醒：链首 rc 行同样参与本链（值可用即返回）。本函数**不**查过期
        是写死的契约，所以「rc 行已标 expired 时不得把旧值当可用 token 发出去」这件事
        由调用方（`get_repo_bot_token_no_global`）显式摘掉 rc/repo_bot 两级来做。
        """
        if not repo_key:
            return ""
        from src.kernel.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        if not repo_key:
            return ""
        for strategy in self.strategies:
            # UserToken 策略用 _fetch_stored 跳过 refresh
            if hasattr(strategy, '_fetch_stored'):
                token = strategy._fetch_stored(repo_key)
            else:
                token = strategy._fetch(repo_key)
            if token:
                return token
        return ""

    def resolve(self, repo_key: str) -> TokenResult:
        """获取 TokenResult（token + level + provider + repo_key + expired + config_level）。

        遍历策略链，第一个返回非空 token 的策略胜出。
        返回的 TokenResult 携带级别信息供 TokenActionHandler 分发。
        FR-10.3: config_level 字段填充配置级别（不受过期短路影响）。
        """
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        from src.kernel.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        head = self._explicit_head()
        if head is not None:
            explicit_token, stage = self._explicit_resolve(repo_key)
            if stage:
                return TokenResult(token="", level=EXPLICIT_REPO_BOT_LEVEL,
                                   provider=self.provider, repo_key=repo_key,
                                   config_level=EXPLICIT_REPO_BOT_LEVEL, stage=stage)
            if explicit_token:
                return TokenResult(token=explicit_token, level=EXPLICIT_REPO_BOT_LEVEL,
                                   provider=self.provider, repo_key=repo_key,
                                   config_level=EXPLICIT_REPO_BOT_LEVEL)
        # FR-20.7: config_level 从胜出策略推算，不再单独调 resolve_config_level（避免两次策略链扫描）
        for strategy in self.strategies:
            if strategy is head:
                continue
            token = strategy.get_token(repo_key)
            if token:
                expired = strategy._is_expired(repo_key)
                return TokenResult(
                    token=token,
                    level=strategy.level,
                    provider=self.provider,
                    repo_key=repo_key,
                    expired=expired,
                    config_level=strategy.level,
                )
        # 全部策略未命中：config_level 取 resolve_config_level 结果
        _config_level = self.resolve_config_level(repo_key).level
        return TokenResult(token="", level="", provider=self.provider, repo_key=repo_key, config_level=_config_level)

    def resolve_config_level(self, repo_key: str) -> TokenResult:
        """获取配置级别 TokenResult（不检查过期、不触发 refresh）。

        供自循环检测（handler.is_self_loop_actor）用——判断仓库的 token 配置级别，
        不需要实际 token 值（可能已过期），只需要知道是路径 A（bot token）还是路径 B（user token）。

        与 resolve() 的区别：
        - resolve() 调 get_token()（含过期短路），token 过期时可能跳到下一级 → level 不准确
        - resolve_config_level() 调 _fetch()（纯读，不检查过期），返回真实配置级别

        UserToken 策略用 _fetch_stored 跳过 OAuth refresh。
        """
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        from src.kernel.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        if not repo_key:
            return TokenResult(token="", level="", provider=self.provider, repo_key="")
        for strategy in self.strategies:
            if hasattr(strategy, '_fetch_stored'):
                token = strategy._fetch_stored(repo_key)
            else:
                token = strategy._fetch(repo_key)
            if token:
                # FR-20.10: resolve_config_level 也填充 expired 字段
                # 供 session_store 判断是否写入 session token
                _expired = strategy._is_expired(repo_key) if hasattr(strategy, '_is_expired') else False
                return TokenResult(
                    token=token,
                    level=strategy.level,
                    provider=self.provider,
                    repo_key=repo_key,
                    expired=_expired,
                )
        # 无 token 配置 → 返回空级别（pipeline 不检测自循环）
        return TokenResult(token="", level="", provider=self.provider, repo_key=repo_key)

    def detect_and_mark_expired(self, repo_key: str) -> str:
        """fail_task 401 时调用：按优先级找到当前生效的策略，验证 token 有效性后标记过期。

        流程：
        1. 遍历策略链，用 _fetch（纯读，不检查过期标记）找到第一个有 token 的策略
        2. 调 provider.verify_token(token) 验证 token 有效性
           - 验证有效 → 401 是其他原因（权限不足等），不标记过期，返回 ""
           - 验证 401 → 标记过期（幂等：已标记则不重复），返回 error_code
           - 验证超时/5xx → 不标记（R21.3.5），返回 ""（401 可能是其他原因）
        3. 所有策略 _fetch 都返回空 → 返回 "NO_TOKEN_AVAILABLE"（R16.6.1）

        R21.3.5: 区分 401 和超时/5xx——只对 401 标记过期。
        R21.3.3: 原 R18.5.10 "超时视为验证失败" 设计取消。
        """
        for strategy in self.strategies:
            token = strategy._fetch(repo_key)  # 不检查过期标记，查原始值
            if token:
                # 幂等：已标记过则不重复标记（R16.1.6）
                if strategy._is_expired(repo_key):
                    return strategy.error_code  # 已标记，直接返回 error_code
                # 先验证 token 有效性（通过 provider 接口调 VCS API GET /user）
                try:
                    self.provider.verify_token(token)
                    # token 验证有效 → 401 是其他原因，不标记过期
                    return ""
                except Exception as e:
                    # R21.3.5: 区分 401 和超时/5xx
                    http_code = _vcs_cards.extract_http_status_from_exc(e)
                    if http_code == 401:
                        # token 验证 401 → 标记过期
                        strategy.mark_expired(repo_key)
                        return strategy.error_code
                    else:
                        # 超时/5xx → 不标记（R21.3.5），返回空
                        log.warning(f"TokenResolver.detect_and_mark_expired: verify non-401 error for {repo_key}: {str(e)[:100]}")
                        return ""
        # 所有策略 _fetch 都返回空（token 已被短路或未配置）→ 无可用 token
        return "NO_TOKEN_AVAILABLE"  # R16.6.1

class TokenActionHandler(ABC):
    """R18.6.2: token 操作职责链接口。按级别差异化执行 token 相关操作。

    策略链负责"取 token + 级别标识"，职责链负责"按级别执行差异化操作"。
    """

    @abstractmethod
    def is_self_loop_actor(self, actor: str, event_type: str,
                           result: TokenResult) -> bool:
        """R18.6.8: 判断 actor 是否是 agent 回声（自循环检测）。"""

    @abstractmethod
    def get_comment_token(self, result: TokenResult) -> str:
        """R18.6.9: 获取用于发 Issue/PR 评论的 token。
        可能不同于执行 token——例如仓库级 bot 过期时 fallback 到全局 bot。"""

    @abstractmethod
    def post_issue_comment(self, repo_key: str, issue_num: str,
                           body: str, result: TokenResult,
                           agent_type: str = "agent") -> bool:
        """R18.6.10: 发 Issue 评论。按级别差异化处理 actor 和标记。
        成功返回 True，失败返回 False（调用方据此 append RESULT_POST_FAILED）。
        agent_type 透传真实 Agent 身份（如 vod/coding/sre），避免占位「agent agent」。"""

    @abstractmethod
    def mark_expired(self, result: TokenResult) -> str:
        """标记当前级别 token 过期，返回 error_code。"""

    @abstractmethod
    def clear_expired(self, result: TokenResult):
        """清除当前级别过期标记（token 重新配置时调用）。"""

class SessionTokenHandler(TokenActionHandler):
    """D101 方案三：session 级已退役（第 1 级策略删除后 level 值域只剩
    repo_bot / global_bot / user，create_action_handler 不再产出本类）。

    基线保留：类符号与其方法签名仍在此（基线模块/用例按名引用），
    仅移除对已退役 DevopsAgentSessionToken 的依赖——session 列零写入后
    无物可标记/清除，故降级为无副作用。
    """

    def is_self_loop_actor(self, actor, event_type, result):
        return False  # session token 的 actor 取决于 token 所有者，降级为不检测

    def get_comment_token(self, result):
        return ""  # session token scope 可能受限，不用于发评论

    def post_issue_comment(self, repo_key, issue_num, body, result, agent_type="agent"):
        # session token 通常不用于发评论
        return False

    def mark_expired(self, result):
        # D101 方案三：session 列零写入（upsert 形参已删、解绑清理段已删），
        # 无 session token 可标记——error_code 同步退役（见 test_d101s ③）。
        return ""

    def clear_expired(self, result):
        # 同上：session token_status 不再由本域写入。
        return None

class RepoBotTokenHandler(TokenActionHandler):
    """R18.6.4: 第 2 级仓库级 bot token。单仓库独立 bot 账号。"""

    def is_self_loop_actor(self, actor, event_type, result):
        # actor = managed_repos.bot_username
        if not actor or not result.repo_key:
            return False
        try:
            # managed_repos 归 mgmt（原语句在 kernel 直连进程主库，三库拓扑下恒 1146）
            bot_username = _repos.repo_bot_username_any(result.repo_key)
            return bool(bot_username) and actor == bot_username
        except Exception as e:
            log.debug(f"RepoBotTokenHandler.is_self_loop_actor: {e}")
        return False

    def get_comment_token(self, result):
        # 仓库级 bot 有效 → 用仓库级 bot token
        if result.token and not result.expired:
            return result.token
        # 仓库级 bot 过期 → fallback 到全局 bot
        if result.provider:
            try:
                return _tokens.get_global_bot_token_for(result.provider.provider_name) or ""
            except Exception:
                pass
        return ""

    def post_issue_comment(self, repo_key, issue_num, body, result, agent_type="agent"):
        comment_token = self.get_comment_token(result)
        if not comment_token or not result.provider:
            return False
        body_with_marker = body + "\n<!-- grape-auto-comment -->"
        return _post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token, agent_type)

    def mark_expired(self, result):
        if result.provider and result.repo_key:
            RepoBotToken(result.provider).mark_expired(result.repo_key)
        return "REPO_TOKEN_EXPIRED"

    def clear_expired(self, result):
        if result.provider and result.repo_key:
            RepoBotToken(result.provider).clear_expired(result.repo_key)

class GlobalBotTokenHandler(TokenActionHandler):
    """R18.6.5: 第 3 级全局 bot token。所有路径 A 仓库共用。"""

    def is_self_loop_actor(self, actor, event_type, result):
        if not actor:
            return False
        try:
            # S87/D63：统一口径——provider 的全局 bot username 集合（github 单值源）
            _bot_names = _tokens.get_global_bot_usernames_for(result.provider.provider_name)
            return actor in _bot_names if _bot_names else False
        except Exception:
            return False

    def get_comment_token(self, result):
        # 全局 bot 有效 → 用全局 bot token
        if result.token and not result.expired:
            return result.token
        # 全局 bot 过期 → fallback 到仓库级 bot
        if result.provider and result.repo_key:
            try:
                from src.kernel.token_resolver import RepoBotToken
                _repo_strategy = RepoBotToken(result.provider)
                _repo_token = _repo_strategy._fetch(result.repo_key)
                if _repo_token and not _repo_strategy._is_expired(result.repo_key):
                    return _repo_token
            except Exception:
                pass
        return ""

    def post_issue_comment(self, repo_key, issue_num, body, result, agent_type="agent"):
        comment_token = self.get_comment_token(result)
        if not comment_token or not result.provider:
            return False
        body_with_marker = body + "\n<!-- grape-auto-comment -->"
        return _post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token, agent_type)

    def mark_expired(self, result):
        if result.provider and result.repo_key:
            GlobalBotToken(result.provider).mark_expired(result.repo_key)
        return "GLOBAL_TOKEN_EXPIRED"

    def clear_expired(self, result):
        if result.provider and result.repo_key:
            GlobalBotToken(result.provider).clear_expired(result.repo_key)

class UserTokenHandler(TokenActionHandler):
    """R18.6.6: 第 4 级用户 token。路径 B 专属。

    核心矛盾：路径 B 下 agent 和用户用同一个 token，actor 完全相同。
    需要引入 task 执行状态作为额外维度（四条件组合判断）。
    """

    def is_self_loop_actor(self, actor, event_type, result):
        # R20.1.1: 四条件组合判断
        if event_type not in ("issue_comment", "note_hook_pr", "issue_updated"):
            return False
        if not result.repo_key:
            return False
        # 条件 1: 路径 B 仓库（通过 result.level == "user" 已确认）
        # 条件 2: actor == host_vcs_username
        try:
            host_vcs_username = _accounts.get_host_vcs_username(result.repo_key, result.provider.provider_name if result.provider else "")
            if not host_vcs_username or actor != host_vcs_username:
                return False
        except Exception:
            return False
        # 条件 4: 该 issue 有正在执行的 task
        return _pipeline.check_running_task(result.repo_key, result.issue_id or "")

    def get_comment_token(self, result):
        # R25.3: 回帖链 best-effort——直接用 _fetch 读存储 token（绕过 _is_expired 检查）
        # 执行链（get_token/resolve）对 status=4 短路返回空串，但回帖需要试发"Token 已过期"评论
        if result.provider and result.repo_key:
            try:
                _ut = UserToken(result.provider)
                _tok = _ut._fetch(result.repo_key)  # 绕过 _is_expired，best-effort
                if _tok:
                    return _tok
            except Exception:
                pass
        # 用户 token 完全不可用 → fallback 全局 bot token
        if result.provider:
            try:
                return _tokens.get_global_bot_token_for(result.provider.provider_name) or ""
            except Exception:
                pass
        return ""

    def post_issue_comment(self, repo_key, issue_num, body, result, agent_type="agent"):
        comment_token = self.get_comment_token(result)
        if not comment_token or not result.provider:
            return False
        body_with_marker = body + "\n<!-- grape-auto-comment -->"
        try:
            return _post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token, agent_type)
        except Exception:
            # 用户 token 过期 → 回帖失败 → R20.2 降级处理
            return False

    def mark_expired(self, result):
        if result.provider and result.repo_key:
            UserToken(result.provider).mark_expired(result.repo_key)
        return "TOKEN_EXPIRED"

    def clear_expired(self, result):
        # 用户重新绑定时 create/update_external_identity 已恢复 status=1
        pass

def create_action_handler(result: TokenResult) -> TokenActionHandler:
    """R18.6.7: 根据 TokenResult.level 创建对应 Handler。

    D101 方案三：session 级已退役——第 1 级删除后 level 值域只剩
    repo_bot / global_bot / user（session 分支随之消亡，防复活守卫见
    tests/test_d101s_session_cleanup.py）。
    """
    if result.level == EXPLICIT_REPO_BOT_LEVEL:
        # Issue #3713：链首新增的显式仓库 bot 级别。它与 repo_bot 共用
        # `managed_repos.bot_token_status` 一列（mark_expired/clear_expired 落同一列、
        # 回写 error_code 同为 REPO_TOKEN_EXPIRED），所以复用同一个 Handler；
        # 若此处不映射，401/403 之后的过期回写对新级别**静默失效**。
        return RepoBotTokenHandler()
    if result.level == "repo_bot":
        return RepoBotTokenHandler()
    elif result.level == "global_bot":
        return GlobalBotTokenHandler()
    elif result.level == "user":
        return UserTokenHandler()
    return None

def _post_comment_via_provider(provider: VCSProviderPort, repo_key: str,
                                issue_num: str, body: str, token: str,
                                agent_type: str = "agent") -> bool:
    """R18.6.10 辅助函数：通过 provider 接口发 Issue 评论。

    内部委托 event_pipeline._post_result_to_issue 的评论发送逻辑，
    但只发评论（不含 conclusion 提取等业务逻辑），加 grape-auto-comment 标记。
    agent_type 透传真实 Agent 身份（如 vod/coding/sre），避免占位「agent agent」。
    """
    if not token or not repo_key or not issue_num:
        return False
    try:
        from src.kernel.repo_key import from_repo_key
        _repo = from_repo_key(repo_key)
        if not _repo:
            return False
        # 复用现有 _post_result_to_issue 发评论（含去重、conclusion 提取等逻辑）
        _pipeline._post_result_to_issue(_repo, str(issue_num), body, token, agent_type, repo_key,
                                        agent_result=False)
        return True
    except Exception as e:
        from src.kernel.config import log
        log.warning(f"_post_comment_via_provider failed: {e}")
        return False

def credential_workflows_write(user_id: str, provider: str) -> "bool | None":
    """D105-18：凭据级 workflows 写能力（三态，gh-aw ADR-0002 显式声明制）。

    返回：True=显式声明有写权限（push 不删 workflows）；False=显式声明受限
    （删）；None=未声明（调用方回退平台名旧逻辑，保证存量零回归）。
    高权限是凭据配置时显式声明、可审计的事实——不做运行时内省（平台不
    暴露 token scope）。声明位置：凭据域 <p>_pat 配置的 workflows_write 键。
    """
    try:
        cfg = _tokens.get_credential(user_id, provider, kind="pat") or {}
        if "workflows_write" not in cfg:
            return None
        return cfg.get("workflows_write") is True
    except Exception:
        return None

def _audit_source(user_id: str, provider: str, source: str) -> None:
    """D102-M：用户级 token 命中来源审计（usage_log，失败不阻断）。

    source ∈ {pat, oauth}——fetch_user_token 双通道命中来源进
    credential_usage_log（"配了 PAT 走了 OAuth"类问题可从审计排查）。
    """
    try:
        from src.kernel.persistence.credential_usage_log import log_usage
        log_usage("", user_id, credential_kind="user_credentials",
                  executor_context="token_resolve",
                  operation=f"user_token_hit_{source}", repo_key=f"{provider}:")
    except Exception:
        pass  # 审计失败不阻断业务


