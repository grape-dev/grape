"""仓库平台卡（vcs_providers_config）——PlatformCard 值对象 + Builder + 注册缓存。

DDD 落位（见 docs/weekly-09/multi-platform-decouple-design.md 实现纪律）：
- PlatformCard = 值对象（不可变配置快照）；build_platform_card = Builder，
  D14 校验（dialect 枚举 / host 必填 / webhook_path 规范 / auth 枚举 / token_env 命名）
  作为构建 invariant，非法即构建失败拒绝（S18 的校验面）。
- 双配置卡分离（D10）：本卡不含任何 OAuth 字段；登录渠道卡 = auth/providers.py。
- 加载机制复用 auth/providers.py 模式：配置文件 → meta 表 → 内存缓存 + TTL +
  主动失效（invalidate_platform_card_cache）。

内置默认（gitcode/github/gitlab）与现状行为一致（向前兼容）；gitee 起所有新平台
纯配置接入。非标准平台走 register_vcs_backend 子类插件口（保留 escape hatch）。
"""
from __future__ import annotations

import json
import os
import re
import time
import threading
from dataclasses import dataclass, field

from src.kernel.config import log
from src.kernel.repo_key import parse_repo_key  # S85/D60③：provider 派生单一入口


_VALID_DIALECTS = ("github", "gitlab")
_VALID_API_AUTH = {"query_token", "bearer_header", "token_header", "private_token"}
# D41/S61：issue 写端点形态（实弹观测的 gitee 方言差异，能力位非平台名）
_VALID_ISSUE_WRITE_FORM = {"repo_path", "owner_param"}
_VALID_BRANCH_CREATE_FORM = {"form", "refs_sha"}
_VALID_WEBHOOK_VERIFY = ("hmac", "token_compare")
# D105-19：托管 webhook 形态（分派 webhook 三件套的实现家族）
_VALID_WEBHOOK_DIALECTS = ("github_web", "gitee_v5")
_VALID_CLONE_AUTH = ("x_access_token", "oauth2", "basic_password")


@dataclass(frozen=True)
class PlatformCard:
    """仓库平台卡（值对象，不可变）。

    与登录渠道卡（oauth_providers_config）正交：本卡只描述仓库托管能力，
    不含 auth_url/token_url/scope 等 OAuth 字段（D10）。
    """

    provider: str
    name: str
    dialect: str                      # github | gitlab（webhook/API 方言家族）
    host: str                         # git 传输 host（clone URL）
    webhook_path: str                 # /webhook/<provider>
    event_header: str                 # X-<Provider>-Event
    api_base: str
    api_auth: str                     # query_token | bearer_header
    webhook_verify: str = "hmac"      # hmac | token_compare
    # D105-19（用户拍板平台化到位）：托管 webhook 形态进卡——github_web=GitHub web-hooks
    # （config.url 匹配 / name=web 形态）；gitee_v5=v5 hooks（顶层 url 匹配 / form 形态）。
    # 必须显式声明（不按 dialect 派生——同为 github API 方言的 gitcode/gitee webhook
    # 形态实为 v5，派生会静默换家族）；未声明时托管消费侧 fail-closed（D105-17 冻结
    # 因此解除）。
    webhook_dialect: str = ""
    # D105-19：平台默认 workflows 写权限（三态回退位，替代 provider=="github" 硬分派）。
    # github=False（App token 无 workflow scope，push 前删 .github/workflows）；其余平台 True。
    workflows_write: bool = True
    clone_auth: str = "x_access_token"  # x_access_token | oauth2 | basic_password
    token_env: str = ""               # BOT_<PROVIDER>_TOKEN 环境变量名（空=派生）
    bot_username: str = ""            # B2：bot 身份（空=无 bot 协作能力）
    user_images_base: str = ""        # 平台编辑器图片附件基址（空=该平台无此机制）
    download_base: str = ""           # 仓库 ZIP 下载基址（github=codeload，gitcode=raw）
    issue_search: bool = False        # D36/S56：有无 issue 搜索端点（策略能力位，非平台名）
    issue_write_form: str = "repo_path"  # D41/S61：issue 写端点形态（repo_path=存量 /repos/{o}/{r}/issues；owner_param=gitee /repos/{o}/issues+repo 入 body，实弹观测）
    branch_create_form: str = "form"     # D44/S66：建分支形态（form=Gitee 系 v5 POST /branches {branch_name,refs}；refs_sha=GitHub 查 sha→POST /git/refs，存量两形态）
    # D105-19 补充：PR 列表 head 过滤参数形态（owner_ref=GitHub "owner:branch"，缺省
    # =存量 github 语义；bare_ref=裸分支名——gitcode 实弹实测：owner:ref 形态返回空
    # 列表、裸 ref 命中，gitee v5 同家族口径）。发错形态=服务端过滤成空→branch_guard
    # fail-open 守卫失效，故形态进卡不按 dialect 猜。
    pr_head_filter: str = "owner_ref"
    # 2026-11-13 if/else 收口：仓库 token 统一读链补充回落能力位（见 build 卡注释）
    owner_identity_token_fallback: bool = False
    # S40-S42（2026-11-13 代理能力位上翻）：providers/<p>.py 模块开关的卡键形态
    # （小写下划线）。默认 None=键缺席 → _pflag 回退模块开关（存量行为零变化）；
    # 卡显式声明（含 False）优先。Builder 只接受 bool；管理面"能力"tab 经卡驱动
    # 自动可见可编辑（field_docs 词条内置指导）。新平台纯配卡接入零 Python 模块。
    milestone_verify_readback: "bool | None" = None          # issue 改 milestone 后 GET 回读校验
    milestone_id_resolve_to_number: "bool | None" = None     # milestone_id 先解析为上游 number
    milestone_due_on_required: "bool | None" = None          # milestones 建立强制 due_on
    milestone_due_date_only: "bool | None" = None            # due_on 只认 YYYY-MM-DD
    milestone_due_time_only: "bool | None" = None            # due_on 只认 ISO datetime
    closing_refs_auto_close_default: "bool | None" = None    # closing keywords 上游自动关闭
    labels_array_form: "bool | None" = None                  # labels 必须数组形态
    labels_color_prefix_hash: "bool | None" = None           # labels 颜色 # 前缀 hex
    issue_update_owner_path: "bool | None" = None            # issue 更新 owner-path 形态
    issue_body_repo_bare: "bool | None" = None               # issue 写 body repo=纯仓名
    pr_comment_id_hex: "bool | None" = None                  # PR 评论 id 32 位 hex
    legacy_repo_token_getter: "bool | None" = None           # 存量四级 fallback 读链
    # S88/D64：github 独有怪癖能力位（非方言——gitee 同 github 方言但 API 跟随 gitcode v5）
    pr_comment_endpoint: str = "pulls"        # github="issues"（其 /pulls/{n}/comments 是 inline-review 端点）
    pr_labels_via_issues: bool = False        # github=True（PATCH /pulls 不接受 labels 字段，422 实测）
    # S89/D65：树 API 家族（github.com API=default_branch/recursive/直连 ref；
    # v5 系=branches[0]/commits?sha 预校验/recursive 拒绝——gitcode/gitee 存量语义）
    git_tree_family: str = "v5"
    # D22 存储字段进卡（内置默认=存量 Provider 类现值，逐字节对齐）
    # F4/D106 终态口径：空串=无物理列，仓库凭据唯一落点=grape_t_repo_credentials
    # （repo_key × cred_type 至多一行；契约见 configs/platforms/SCHEMA.md）
    token_storage_field: str = ""     # managed_repos 仓库 bot token 列（空=无托管列存储，凭据只落 grape_t_repo_credentials）
    webhook_token_field: str = ""     # managed_repos webhook token 列（空=同上）
    session_token_field: str = ""     # devops_agent_session token 列
    global_bot_meta_key: str = ""     # 全局 bot token meta key（空=派生 global_bot_<p>_token）
    global_bot_status_meta_key: str = ""  # 过期状态 meta key（空=派生 global_<p>_bot_token_status）
    pat_create_url: str = ""          # PAT 创建页（空=派生 https://<host>/profile/...）
    brand_color: str = ""             # 品牌色（#RRGGBB；空=前端兜底表）——台阶2：品牌资产进卡
    # K1 通用化：arch 基线遗留的「平台专用取 token 符号」在此声明（空=该平台无遗留符号）。
    # 代码侧不写平台名——deps.py 的兼容别名由本字段派生（模块级 __getattr__）。
    legacy_token_symbol: str = ""
    # 台阶3：资源 API 声明化映射段（空=未声明，走 providers 模块回退）。
    # 形态见 _build_resources；github 系 REST 形态为缺省（新卡最少配置）。
    resources: dict = field(default_factory=dict)
    # D105-14 评审差距一：能力显式声明（可选）。空=未声明（存量四平台，
    # 走既有推断不限制）；非空=白名单，消费侧对未声明能力 fail-closed 显式拒绝。
    capabilities: tuple = ()
    event_map_overrides: dict = field(default_factory=dict)
    fields_overrides: dict = field(default_factory=dict)

    def token_env_name(self) -> str:
        return self.token_env or f"BOT_{self.provider.upper()}_TOKEN"

    def user_token_env_name(self) -> str:
        return f"USER_{self.provider.upper()}_TOKEN"


class PlatformCardError(ValueError):
    """平台卡构建失败（校验 invariant 不满足）。"""


def _build_brand_color(provider: str, raw: dict) -> str:
    """台阶2：brand_color 校验（#RRGGBB 十六进制；空串=未配置合法）。"""
    c = str(raw.get("brand_color") or "").strip()
    if not c:
        return ""
    if len(c) != 7 or c[0] != "#" or any(ch not in "0123456789abcdefABCDEF" for ch in c[1:]):
        raise PlatformCardError(f"{provider}: brand_color 必须是 #RRGGBB 十六进制色值: {c!r}")
    return c


# 台阶3：resources 段合法键（声明化资源 API 映射）
_VALID_QUERY_STATES = {"open", "closed", "all"}


# D105-14 评审差距一：能力枚举（随卡驱动覆盖演进而扩充；新增须同步消费侧检查点）。
# 形态 "<resource>.<op>" 与 resources.rules 键同构 + 基础设施能力。
_VALID_CAPABILITIES = {
    "issues.query", "issues.create", "issues.update",
    "pulls.create", "pulls.comment",
    "branch.create", "webhook.verify", "repo.host",
}


def _build_capabilities(provider: str, raw: dict, rules: dict) -> tuple:
    """可选 capabilities 数组构建与校验（差距一）+ 覆盖一致性校验（差距二剩余）。

    - 取值必须在 _VALID_CAPABILITIES 枚举内（未知值 fail-closed）
    - 覆盖一致性：声明的 "<resource>.<op>" 能力必须有承载——卡 resources.rules
      有对应键，或 provider 在 _MODULES（代码模块承接）；两者皆无 = 声明了
      做不到的能力，构建期即红（把"零代码成立"从真机验证前移为可判定条件）
    """
    caps = raw.get("capabilities")
    if caps in (None, []):
        return ()
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        raise PlatformCardError(f"{provider}: capabilities 必须是字符串数组: {caps!r}")
    unknown = set(caps) - _VALID_CAPABILITIES
    if unknown:
        raise PlatformCardError(
            f"{provider}: capabilities 含未登记能力 {sorted(unknown)}，"
            f"允许枚举: {sorted(_VALID_CAPABILITIES)}（新增须同步消费侧检查点）")
    # 覆盖一致性（仅对 "<resource>.<op>" 形态；基础设施能力不查 rules）
    from src.domains.runtime.vcs.providers import _MODULES
    for cap in caps:
        if "." not in cap:
            continue
        if cap in rules.get("rules", {}) or f"{cap}" in rules.get("rules", {}):
            continue
        if provider in _MODULES:
            continue
        raise PlatformCardError(
            f"{provider}: capabilities 声明了 {cap!r} 但无承载"
            f"（resources.rules 缺 {cap!r} 键且无代码模块）——声明了做不到的能力")
    return tuple(sorted(set(caps)))


def card_capabilities(provider: str, card: "PlatformCard | None" = None) -> "tuple | None":
    """查询平台声明的能力集（差距一：能力一等公民、可查询）。

    card 缺省时走 get_platform_card（文件/meta 卡）；运行期构建的卡可显式传入。
    返回 None = 卡未声明（既有推断语义，不限制）；返回 tuple = 白名单，
    消费侧对集合外能力应显式拒绝（require_capability）。
    """
    card = card or get_platform_card(provider)
    if card is None:
        return None
    return tuple(card.capabilities) if card.capabilities else None


def require_capability(provider: str, capability: str,
                       card: "PlatformCard | None" = None) -> None:
    """消费侧 fail-closed：卡声明了能力白名单且不含 capability → 显式拒绝。

    卡未声明 capabilities（存量四平台）→ 不限制（既有推断语义）。
    """
    caps = card_capabilities(provider, card=card)
    if caps is not None and capability not in caps:
        raise PlatformCardError(
            f"{provider}: 未声明能力 {capability!r}（声明集 {list(caps)}）——"
            "该平台不支持此操作，显式拒绝而非静默降级")


_COMMON_TRIGGER_FIELDS = {"number"}  # D105-14：跨平台通用触发锚（issue 编号——模板示例 Closes #{number} 的合法域）


def _build_resources(provider: str, raw: dict) -> dict:
    """台阶3：resources 段 schema 校验（守卫面，非法即 PlatformCardError）。

    形态（github 系 REST 形态为缺省，缺省键即 github 语义，新卡最少配置）：
        "resources": {
          "repo_segment": "repos/{owner}/{repo}",       # 仓库资源段（必含 {owner}/{repo} 占位符）
          "path_map": {"/pulls": "/merge_requests"},     # 网关子路径 → 原生子路径
          "query_state": {"open": "opened", ...},        # 中性 state → query 值（键⊆open/closed/all）
          "rules": {"issues.create": {"title": "title",  # <resource>.<op> → 字段映射（apply_rules 形态）
                                       "assignee": {"drop": true},
                                       "state": {"native": "state", "values": {...}}}}
        }
    缺省语义：repo_segment=repos/{owner}/{repo}；path_map={}；rules={}（同名透传）；
    query_state=恒等。transform 回调不可 JSON 化——卡规则只支持 str/native+values/drop。
    """
    res = raw.get("resources")
    if res in (None, {}):
        return {}
    if not isinstance(res, dict):
        raise PlatformCardError(f"{provider}: resources 必须是对象: {res!r}")
    unknown = set(res.keys()) - {"repo_segment", "path_map", "query_state", "rules"}
    if unknown:
        raise PlatformCardError(f"{provider}: resources 含未知键 {sorted(unknown)}")
    out: dict = {}

    seg = str(res.get("repo_segment") or "repos/{owner}/{repo}")
    if "{owner}" not in seg or "{repo}" not in seg:
        raise PlatformCardError(f"{provider}: resources.repo_segment 必须含 {{owner}}/{{repo}} 占位符: {seg!r}")
    if seg.startswith("/"):
        raise PlatformCardError(f"{provider}: resources.repo_segment 不应以 / 开头: {seg!r}")
    out["repo_segment"] = seg

    path_map = res.get("path_map") or {}
    if not isinstance(path_map, dict):
        raise PlatformCardError(f"{provider}: resources.path_map 必须是对象: {path_map!r}")
    for k, v in path_map.items():
        if not (isinstance(k, str) and k.startswith("/")) or not (isinstance(v, str) and v.startswith("/")):
            raise PlatformCardError(f"{provider}: resources.path_map 键值必须是 / 开头的路径: {k!r}: {v!r}")
    out["path_map"] = dict(path_map)

    qs = res.get("query_state") or {}
    if not isinstance(qs, dict):
        raise PlatformCardError(f"{provider}: resources.query_state 必须是对象: {qs!r}")
    bad = set(qs.keys()) - _VALID_QUERY_STATES
    if bad:
        raise PlatformCardError(f"{provider}: resources.query_state 键必须是 {'/'.join(sorted(_VALID_QUERY_STATES))}: {sorted(bad)}")
    out["query_state"] = {str(k): str(v) for k, v in qs.items()}

    rules = res.get("rules") or {}
    if not isinstance(rules, dict):
        raise PlatformCardError(f"{provider}: resources.rules 必须是对象: {rules!r}")
    for key, rule in rules.items():
        if not isinstance(key, str) or "." not in key or not all(key.split(".")):
            raise PlatformCardError(f"{provider}: resources.rules 键必须形如 '<resource>.<op>': {key!r}")
        if not isinstance(rule, dict):
            raise PlatformCardError(f"{provider}: resources.rules[{key!r}] 必须是对象: {rule!r}")
        for field_name, r in rule.items():
            if isinstance(r, str) and r:
                continue
            if not isinstance(r, dict):
                raise PlatformCardError(
                    f"{provider}: resources.rules[{key!r}][{field_name!r}] 必须是 str/"
                    '{"drop": true}/{"native": ..., "values": ...}: {r!r}')
            if r.get("drop"):
                continue
            # D105-14 改动点 C：模板插值第四形态构建期校验（报错三要素）
            if "template" in r:
                if r.get("values") is not None or "transform" in r or "drop" in r:
                    raise PlatformCardError(
                        f"{provider}: resources.rules[{key!r}][{field_name!r}] "
                        "template 与 values/drop/transform 互斥: {r!r}")
                mode = r.get("mode") or "replace"
                if mode not in ("replace", "append", "prepend"):
                    raise PlatformCardError(
                        f"{provider}: resources.rules[{key!r}][{field_name!r}] "
                        f"mode 必须是 replace/append/prepend: {mode!r}")
                tmpl = r.get("template")
                if not isinstance(tmpl, str) or not tmpl:
                    raise PlatformCardError(
                        f"{provider}: resources.rules[{key!r}][{field_name!r}] "
                        f"template 必须是非空字符串: {tmpl!r}")
                placeholders = set(re.findall(r"\{(\w+)\}", tmpl))
                if not placeholders:
                    raise PlatformCardError(
                        f"{provider}: resources.rules[{key!r}][{field_name!r}] "
                        "template 至少含 1 个 {占位符}（防误用为纯字面量）")
                res_key, res_op = key.split(".", 1)
                from src.domains.runtime.vcs.providers.github import default_rule_fields
                allowed = (default_rule_fields(res_key, res_op) | set(rule.keys())
                              | _COMMON_TRIGGER_FIELDS)
                unknown_ph = placeholders - allowed
                if unknown_ph:
                    raise PlatformCardError(
                        f"{provider}: resources.rules[{key!r}][{field_name!r}] "
                        f"占位符 {sorted(unknown_ph)} 不在该 (resource,op) 中性字段集 "
                        f"{sorted(allowed)}（拼写错构建期即红）")
                bad_trigger = r.get("trigger")
                if bad_trigger is not None and bad_trigger not in allowed:
                    raise PlatformCardError(
                        f"{provider}: resources.rules[{key!r}][{field_name!r}] "
                        f"trigger {bad_trigger!r} 不在中性字段集 {sorted(allowed)}")
                continue
            if "native" not in r:
                raise PlatformCardError(
                    f"{provider}: resources.rules[{key!r}][{field_name!r}] 缺 native"
                    f"（合法形态 str / drop:true / native+values）: {r!r}")
            values = r.get("values")
            if values is not None and not isinstance(values, dict):
                raise PlatformCardError(f"{provider}: resources.rules[{key!r}][{field_name!r}].values 必须是对象: {values!r}")
    out["rules"] = dict(rules)
    return out


def _explicit_or_default(raw: dict, key: str, default: str) -> str:
    """F4/D106：显式空串语义——键存在（含 ""）即尊重原值（""=无物理列存储），
    仅键缺席才回退 default（派生列名）。meta 运行时覆盖与 configs/platforms
    文件共用同一构建入口，语义一致。"""
    if key in raw:
        return str(raw.get(key) or "")
    return default


def _opt_bool(raw: dict, key: str) -> "bool | None":
    """S40-S42：可选布尔能力键——键缺席/空值→None（=回退模块开关），显式
    布尔（含 False）尊重。与 _explicit_or_default 同"显式声明优先"口径。"""
    if key in raw:
        v = raw.get(key)
        if isinstance(v, bool):
            return v
        if v is None or str(v).strip() == "":
            return None
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    return None


def build_platform_card(provider: str, raw: dict) -> PlatformCard:
    """Builder：dict → PlatformCard，校验失败抛 PlatformCardError（S18 校验面）。"""
    if not provider or not isinstance(provider, str) or not provider.replace("_", "").isalnum():
        raise PlatformCardError(f"provider 名非法: {provider!r}")
    if not isinstance(raw, dict):
        raise PlatformCardError(f"{provider}: 配置必须是对象")
    dialect = raw.get("dialect", "github")
    if dialect not in _VALID_DIALECTS:
        raise PlatformCardError(f"{provider}: dialect 必须是 {'/'.join(_VALID_DIALECTS)}，得到 {dialect!r}")
    host = str(raw.get("host") or "").strip()
    if not host or "/" in host or " " in host:
        raise PlatformCardError(f"{provider}: host 非法: {host!r}")
    webhook_path = str(raw.get("webhook_path") or f"/webhook/{provider}")
    if not webhook_path.startswith("/webhook/") or len(webhook_path) <= len("/webhook/"):
        raise PlatformCardError(f"{provider}: webhook_path 必须形如 /webhook/<name>: {webhook_path!r}")
    api_base = str(raw.get("api_base") or f"https://{host}").strip().rstrip("/")
    if not api_base.startswith(("http://", "https://")):
        raise PlatformCardError(f"{provider}: api_base 非法: {api_base!r}")
    api_auth = raw.get("api_auth", "query_token" if dialect == "github" else "bearer_header")
    if api_auth not in _VALID_API_AUTH:
        raise PlatformCardError(f"{provider}: api_auth 必须是 {'/'.join(_VALID_API_AUTH)}")
    webhook_verify = raw.get("webhook_verify", "hmac" if dialect == "github" else "token_compare")
    if webhook_verify not in _VALID_WEBHOOK_VERIFY:
        raise PlatformCardError(f"{provider}: webhook_verify 必须是 {'/'.join(_VALID_WEBHOOK_VERIFY)}")
    # D105-19：webhook 形态（github_web/gitee_v5）。必须卡显式声明——同为 github
    # API 方言的 gitcode/gitee webhook 形态实为 v5，从 dialect 派生会静默换家族，
    # 故不做缺省推导；未声明时托管消费侧 fail-closed（显式报错，不猜）。
    webhook_dialect = str(raw.get("webhook_dialect") or "")
    if webhook_dialect and webhook_dialect not in _VALID_WEBHOOK_DIALECTS:
        raise PlatformCardError(f"{provider}: webhook_dialect 必须是 {'/'.join(_VALID_WEBHOOK_DIALECTS)}: {webhook_dialect!r}")
    clone_auth = raw.get("clone_auth", "x_access_token")
    if clone_auth not in _VALID_CLONE_AUTH:
        raise PlatformCardError(f"{provider}: clone_auth 必须是 {'/'.join(_VALID_CLONE_AUTH)}")
    token_env = str(raw.get("token_env") or "").strip()
    if token_env and (not token_env.isupper() or not token_env.replace("_", "").isalnum()):
        raise PlatformCardError(f"{provider}: token_env 必须是大写下划线命名: {token_env!r}")
    issue_write_form = str(raw.get("issue_write_form") or "repo_path")
    if issue_write_form not in _VALID_ISSUE_WRITE_FORM:
        raise PlatformCardError(f"{provider}: issue_write_form 必须是 {'/'.join(_VALID_ISSUE_WRITE_FORM)}: {issue_write_form!r}")
    branch_create_form = str(raw.get("branch_create_form", "form"))
    if branch_create_form not in _VALID_BRANCH_CREATE_FORM:
        raise PlatformCardError(f"{provider}: branch_create_form 必须是 {'/'.join(_VALID_BRANCH_CREATE_FORM)}: {branch_create_form!r}")
    pr_head_filter = str(raw.get("pr_head_filter") or "owner_ref")
    if pr_head_filter not in ("owner_ref", "bare_ref"):
        raise PlatformCardError(f"{provider}: pr_head_filter 必须是 owner_ref/bare_ref: {pr_head_filter!r}")
    # 2026-11-13 if/else 收口配套：仓库 token 统一读链的补充回落能力位。
    # true = 标准策略链（session→repo_bot→global→user(host_user_id)）全空后，
    # 再按仓库 owner 用户名查其该平台 identity token（D32-补丁语义下沉，
    # 原为 db.get_github_token 尾段专属回落）。新平台需要该语义时卡内声明即可，
    # 零代理/DB 层平台分支。
    owner_identity_token_fallback = bool(raw.get("owner_identity_token_fallback", False))
    pr_comment_endpoint = str(raw.get("pr_comment_endpoint") or "pulls")
    if pr_comment_endpoint not in ("pulls", "issues"):
        raise PlatformCardError(f"{provider}: pr_comment_endpoint 必须是 pulls/issues: {pr_comment_endpoint!r}")
    pr_labels_via_issues = bool(raw.get("pr_labels_via_issues") or False)
    git_tree_family = str(raw.get("git_tree_family") or "v5")
    if git_tree_family not in ("github_api", "v5"):
        raise PlatformCardError(f"{provider}: git_tree_family 必须是 github_api/v5: {git_tree_family!r}")
    _res_built = _build_resources(provider, raw)
    return PlatformCard(
        provider=provider,
        name=str(raw.get("name") or provider.capitalize()),
        dialect=dialect,
        host=host,
        webhook_path=webhook_path,
        event_header=str(raw.get("event_header") or f"X-{provider.capitalize()}-Event"),
        api_base=api_base,
        api_auth=api_auth,
        webhook_verify=webhook_verify,
        webhook_dialect=webhook_dialect,
        workflows_write=bool(raw.get("workflows_write", True)),
        clone_auth=clone_auth,
        token_env=token_env,
        bot_username=str(raw.get("bot_username") or ""),
        user_images_base=str(raw.get("user_images_base") or ""),
        download_base=str(raw.get("download_base") or ""),
        issue_search=bool(raw.get("issue_search", False)),
        issue_write_form=str(raw.get("issue_write_form", "repo_path")),
        branch_create_form=str(raw.get("branch_create_form", "form")),
        pr_head_filter=pr_head_filter,
        owner_identity_token_fallback=owner_identity_token_fallback,
        # S40-S42：代理能力键上翻——键缺席 None（_pflag 回退模块开关，存量零变化），
        # 显式声明（含 False）优先。raw.get(...) or None 归一空串/缺省→None。
        milestone_verify_readback=_opt_bool(raw, "milestone_verify_readback"),
        milestone_id_resolve_to_number=_opt_bool(raw, "milestone_id_resolve_to_number"),
        milestone_due_on_required=_opt_bool(raw, "milestone_due_on_required"),
        milestone_due_date_only=_opt_bool(raw, "milestone_due_date_only"),
        milestone_due_time_only=_opt_bool(raw, "milestone_due_time_only"),
        closing_refs_auto_close_default=_opt_bool(raw, "closing_refs_auto_close_default"),
        labels_array_form=_opt_bool(raw, "labels_array_form"),
        labels_color_prefix_hash=_opt_bool(raw, "labels_color_prefix_hash"),
        issue_update_owner_path=_opt_bool(raw, "issue_update_owner_path"),
        issue_body_repo_bare=_opt_bool(raw, "issue_body_repo_bare"),
        pr_comment_id_hex=_opt_bool(raw, "pr_comment_id_hex"),
        legacy_repo_token_getter=_opt_bool(raw, "legacy_repo_token_getter"),
        pr_comment_endpoint=str(raw.get("pr_comment_endpoint", "pulls")),
        pr_labels_via_issues=bool(raw.get("pr_labels_via_issues", False)),
        git_tree_family=str(raw.get("git_tree_family", "v5")),
        # F4/D106（凭据存储终态化第一批）：token_storage_field / webhook_token_field
        # 显式 "" 被尊重（=无物理列，凭据只落 grape_t_repo_credentials 单表）；仅键缺席时
        # 才回退派生列名（gitcode/github 存量列语义）。契约见 configs/platforms/SCHEMA.md。
        token_storage_field=_explicit_or_default(raw, "token_storage_field", f"bot_{provider}_token"),
        webhook_token_field=_explicit_or_default(raw, "webhook_token_field", f"{provider}_webhook_token"),
        session_token_field=str(raw.get("session_token_field") or f"repo_{provider}_token"),
        global_bot_meta_key=str(raw.get("global_bot_meta_key") or f"global_bot_{provider}_token"),
        global_bot_status_meta_key=str(raw.get("global_bot_status_meta_key")
                                       or f"global_{provider}_bot_token_status"),
        pat_create_url=str(raw.get("pat_create_url")
                           or f"https://{host}/profile/personal_access_tokens"),
        brand_color=_build_brand_color(provider, raw),
        legacy_token_symbol=str(raw.get("legacy_token_symbol") or "").strip(),
        resources=_res_built,
        capabilities=_build_capabilities(provider, raw, _res_built),
        event_map_overrides=dict(raw.get("event_map") or {}),
        fields_overrides=dict(raw.get("fields") or {}),
    )


# 内置默认：三平台与现状行为一致（D14 存量回归锁的基线）
_DEFAULT_CARDS_RAW: dict[str, dict] = {
    "gitcode": {
        "name": "GitCode", "dialect": "github", "host": "gitcode.com",
        "webhook_dialect": "gitee_v5", "pr_head_filter": "bare_ref",
        "webhook_path": "/webhook/gitcode", "event_header": "X-GitCode-Event",
        "api_base": "https://gitcode.com/api/v5", "api_auth": "query_token",
        "webhook_verify": "hmac", "clone_auth": "x_access_token",
        "bot_username": "my_grape_bot",
        "user_images_base": "https://raw.gitcode.com/user-images",
        "download_base": "https://raw.gitcode.com",
    },
    "github": {
        "name": "GitHub", "dialect": "github", "host": "github.com",
        "webhook_dialect": "github_web", "workflows_write": False,
        "issue_search": True, "branch_create_form": "refs_sha",
        "webhook_path": "/webhook/github", "event_header": "X-GitHub-Event",
        "api_base": "https://api.github.com", "api_auth": "token_header",
        "webhook_verify": "hmac", "clone_auth": "x_access_token",
        "download_base": "https://codeload.github.com",
        "pat_create_url": "https://github.com/settings/tokens",
        "pr_comment_endpoint": "issues", "pr_labels_via_issues": True,
        "git_tree_family": "github_api",
    },
    "gitlab": {
        "name": "GitLab", "dialect": "gitlab", "host": "gitlab.com",
        "webhook_path": "/webhook/gitlab", "event_header": "X-Gitlab-Event",
        "api_base": "https://gitlab.com/api/v4", "api_auth": "private_token",
        "webhook_verify": "token_compare", "clone_auth": "oauth2",
        "download_base": "https://gitlab.com",
        # F4/D106：凭据存储终态化——gitlab 仓凭据只落 grape_t_repo_credentials，
        # 不再派生 managed_repos 物理列（gitee 同口径走 configs/platforms/gitee.json）。
        "token_storage_field": "", "webhook_token_field": "",
    },
}

_CACHE: dict[str, PlatformCard] = {}
_cache_lock = threading.Lock()
_cache_ts: float = 0.0
_CACHE_TTL = 300.0
_loaded = False


def _repo_root(start: str) -> str:
    """向上找仓库根（含 configs/ 或 scripts/ 的目录），模块搬迁后仍稳。"""
    cur = os.path.dirname(os.path.abspath(start))
    while cur and cur != "/":
        if os.path.isdir(os.path.join(cur, "configs")) or os.path.isdir(os.path.join(cur, "scripts")):
            return cur
        cur = os.path.dirname(cur)
    return os.path.dirname(os.path.abspath(start))

_CONFIGS_DIR = os.path.join(_repo_root(__file__), "configs", "platforms")


def _builtin_raws_from_dir(dir_path: str) -> "dict[str, dict]":
    """S83/D59：从 configs/platforms/*.json 读内置卡 raw（每平台一个文件 =
    能力聚合点，替代代码内 dict 作为值的载体；_DEFAULT_CARDS_RAW 保留为
    目录缺失时的代码兜底）。

    守卫（S70 教训的结构性免疫）：文件键集合 ⊆ PlatformCard dataclass
    字段名——未知键（拼写错误/臆造能力位）整卡拒绝并 error 级响亮
    （静默丢键=能力静默丢失，S69 反模式）。
    """
    import dataclasses as _dc
    import glob as _glob
    import json as _json
    valid_fields = {f.name for f in _dc.fields(PlatformCard)}
    raws = {}
    for path in sorted(_glob.glob(os.path.join(dir_path, "*.json"))):
        provider = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, encoding="utf-8") as f:
                raw = _json.load(f) or {}
        except Exception as e:
            log.error(f"platform_card: 配置文件解析失败 {path}: {e}（整卡拒绝）")
            continue
        # 下划线前缀 = 文档键（JSON 没有注释语法，用 _readme / _fields / _note 承载说明）。
        # 先剥离说明键再做键集校验与卡构建——说明不会变成能力位，也不会触发"未知键整卡拒绝"。
        card_raw = {k: v for k, v in raw.items() if not str(k).startswith("_")}
        unknown = set(card_raw.keys()) - valid_fields
        if unknown:
            log.error(f"platform_card: {provider} 配置文件含未知键 {sorted(unknown)} "
                      f"（合法键=PlatformCard 字段），整卡拒绝——修文件或扩展 Builder")
            continue
        try:
            build_platform_card(provider, card_raw)  # 校验预演（非法整卡拒绝）
            raws[provider] = card_raw
        except PlatformCardError as e:
            log.error(f"platform_card: {provider} 卡构建失败: {e}（整卡拒绝）")
    return raws


_TABLE_SKIP = {"id", "provider", "source", "config_hash", "created_at", "updated_at", "enabled"}
# 跨域读原语返回的列（与 mgmt.public._PLATFORM_CARD_COLUMNS 同序，凭据列不出域）
_PLATFORM_CARD_COLUMNS = (
    "provider", "name", "dialect", "host", "api_base", "api_auth", "webhook_path",
    "event_header", "webhook_verify", "webhook_dialect", "pr_head_filter", "clone_auth",
    "issue_write_form", "branch_create_form", "issue_search", "git_tree_family",
    "pr_comment_endpoint", "pr_labels_via_issues", "workflows_write", "legacy_token_symbol",
    "pat_create_url", "token_storage_field", "webhook_token_field", "bot_username",
    "bot_token_status", "bot_checked_at", "owner_fallback", "download_base",
    "user_images_base", "brand_color",
)
# 2026-11-14：凭据能力表已删除；这些字段只在平台卡文件里声明（见 configs/platforms/*.json）


def _raws_from_table() -> dict:
    """D109：表就绪后从 grape_t_repo_platforms（+caps）构造平台卡 raw。

    未就绪 / 读失败 → 空 dict，调用方保持「meta > 文件 > 代码兜底」原路径。
    """
    try:
        from src.domains.platform.importer import is_table_backed

        if not is_table_backed():
            return {}
        # 平台对象表归 mgmt 域：拆库拓扑下必须走 mgmt public 原语（lib 命名查询），
        # 不能在本进程库里 ORM 直读——那是另一份空/陈旧副本（三库四进程守护）。
        from src.domains.mgmt import public as _mgmt_public

        out: dict[str, dict] = {}
        # 这里**保留** enabled_only：disabled 平台不用表行覆盖卡，卡仍从
        # configs/platforms/*.json 解析 → "用户面不提供"不等于"内网不可用"
        # （方言/host/托管链路还要能工作）。与用户面目录的判据是两件事。
        for row in _mgmt_public.repo_platform_cards(enabled_only=True):
            out[str(row.get("provider") or "")] = {
                col: row.get(col) or ""
                for col in _PLATFORM_CARD_COLUMNS
                if col not in _TABLE_SKIP
            }
        return {k: v for k, v in out.items() if k}
    except Exception as e:
        log.warning(f"repo_platforms table read failed, fallback to meta+file: {e}")
        return {}


def builtin_cards_from_dir(dir_path: str) -> "dict[str, PlatformCard]":
    """S83/D59：configs/platforms/*.json → 平台卡（raw 读取复用
    _builtin_raws_from_dir，守卫语义一致）。"""
    return {p: build_platform_card(p, raw)
            for p, raw in _builtin_raws_from_dir(dir_path).items()}


def _load_cards() -> None:
    global _loaded, _cache_ts
    if _loaded and (time.time() - _cache_ts < _CACHE_TTL):
        return
    with _cache_lock:
        if _loaded and (time.time() - _cache_ts < _CACHE_TTL):
            return
        cards: dict[str, PlatformCard] = {}
        raw_cfg = ""
        try:
            from src.domains.mgmt import public as user_db
            raw_cfg = user_db.get_meta_value("vcs_providers_config") or ""
        except Exception as e:  # DB 未就绪（测试/启动早期）→ 走默认
            log.debug(f"platform_card: read meta failed ({e}), using defaults")
        # S83/D59 优先级链：meta（admin 运行时覆盖）> configs/platforms/ 文件
        # （内置默认载体，每平台一个文件）> _DEFAULT_CARDS_RAW（代码兜底，
        # 目录缺失时才用）。
        # D105-19：meta 对文件卡是【逐键覆盖】——meta 只覆盖它声明的键，缺席键
        # 落到文件卡值。存量 meta 常是整卡全量 dump（admin UI 保存形态），若做
        # 整卡替换，代码/文件新增的卡字段（如 webhook_dialect）会被 stale dump
        # 静默抹掉，导致托管消费侧 fail-closed。显式置空语义不变（键存在即尊重）。
        if os.path.isdir(_CONFIGS_DIR):
            _base_raws = _builtin_raws_from_dir(_CONFIGS_DIR)
        else:
            log.warning(f"platform_card: 配置目录缺失 {_CONFIGS_DIR}，回退代码内置默认")
            _base_raws = {p: dict(raw) for p, raw in _DEFAULT_CARDS_RAW.items()}
        if raw_cfg:
            try:
                data = json.loads(raw_cfg)
                for provider, raw in (data or {}).items():
                    _merged = dict(_base_raws.get(provider) or {})
                    _merged.update(raw or {})
                    _base_raws[provider] = _merged
            except Exception as e:
                log.warning(f"platform_card: vcs_providers_config 解析失败: {e}")
        # D109：表就绪后 DB 表为运行时真源——表内 provider 覆盖 meta/文件合并结果；
        # 文件独有平台仍保留（防表缺行致平台消失）。
        _tbl_raws = _raws_from_table()
        if _tbl_raws:
            for _p, _raw in _tbl_raws.items():
                _base_raws[_p] = dict(_raw)
            log.info(f"platform cards from table: {sorted(_tbl_raws)}")
        cards = {}
        for provider, raw in _base_raws.items():
            try:
                cards[provider] = build_platform_card(provider, raw)
            except PlatformCardError as e:
                # meta 合并产物非法：退回纯文件卡（meta 覆盖被拒），不静默丢平台
                _fb = (_builtin_raws_from_dir(_CONFIGS_DIR).get(provider)
                       if os.path.isdir(_CONFIGS_DIR) else None)
                if _fb is not None:
                    log.warning(f"platform_card: {provider} meta 合并卡非法（{e}），回退文件卡")
                    cards[provider] = build_platform_card(provider, _fb)
                else:
                    log.error(f"platform_card: {provider} 卡构建失败: {e}（整卡拒绝）")
        _CACHE.clear()
        _CACHE.update(cards)
        _loaded = True
        _cache_ts = time.time()


def invalidate_platform_card_cache() -> None:
    """admin 改平台卡后主动失效缓存（复用 P2-2 热失效模式）。"""
    global _loaded
    with _cache_lock:
        _loaded = False
    log.info("platform card cache invalidated")


def get_platform_card(provider: str) -> "PlatformCard | None":
    if not provider:
        return None
    _load_cards()
    return _CACHE.get(provider)


def list_vcs_platforms() -> list[str]:
    _load_cards()
    return sorted(_CACHE.keys())


def display_name_for_repo_key(repo_key: str) -> str:
    """S76/D51：平台展示名按 repo_key 前缀取卡 name（无卡回退存量二分
    默认——裸键=gitcode）。替代 chat/IM 展示层的 "GitHub"/"GitCode" 字面
    二分（gitee 仓显示错平台名）。"""
    provider = parse_repo_key(repo_key or "")[0]  # S85/D60③：统一 parse_repo_key
    try:
        card = get_platform_card(provider)
        if card is not None:
            return card.name or provider
    except Exception:
        pass
    return provider or "unknown"  # 无卡：回退 provider key（不冒充具体平台）


def host_for_repo_key(repo_key: str) -> str:
    """S76/D51：issue 链接 host 按 repo_key 前缀取卡 host（无卡回退存量
    二分默认）。替代 issues 路由的 "github.com"/"gitcode.com" 字面二分
    （gitee 仓 issue_url 指错平台）。"""
    provider = parse_repo_key(repo_key or "")[0]  # S85/D60③：统一 parse_repo_key
    try:
        card = get_platform_card(provider)
        if card is not None:
            return card.host or provider
    except Exception:
        pass
    return f"{provider}.com" if provider else ""  # 无卡：按 provider 推导（不冒充具体平台）
