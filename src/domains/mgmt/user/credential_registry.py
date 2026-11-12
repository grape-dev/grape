"""D100 凭据类型注册表（代码常量，非 DB 表）。

方案稿：docs/weekly-09/d100-credential-type-schema-design.md §二。

形态裁定（用户拍板）：代码常量 + 纯函数，零 DB 依赖——与 rc 的
{'bot','webhook'} 白名单同构（D99 已验证的模式），守卫在应用层。
物理 credential_types 表为"运行时动态扩类型"设计，当前零消费方
（用不到不设计），触发条件见方案稿 §2.4。

三段值域（§2.2）——每段对应一张物理表的键值域：
- user 段   → user_credentials.provider（复合名承载，三族：云凭据裸名 /
              PAT <p>_pat / OAuth 副本 <p>_oauth（deprecated，D100 清退））
- repo 段   → grape_t_repo_credentials.cred_type（D99 归一值域，注册表仅登记对齐，
              消灭双处维护）
- system 段 → grape_t_system_credentials.cred_type（D100 批 1 建，消费方批 2+ 接，
              A1-A4 暂缓收编——定义≠收编）

守卫语义（P5=warning 期，用户确认）：未登记 key 写入先告警不拒绝——
存量库可能存在历史 provider key，零未登记命中告警一轮后再收紧为硬拒绝。
切换开关：ENFORCE = False（告警期）→ True（收紧期），一行翻转。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CredentialTypeSpec:
    """单个凭据类型定义。

    key:         复合名。user 段=user_credentials.provider 的值；
                 repo/system 段=对应表 cred_type 的值。
    scope:       归属域："user" | "repo" | "system"（分域互斥，UC42⑤ 守卫：
                 一个 key 只允许出现在一个段的值域里）。
    fields:      JSON dict 字段约定 ((name, required, sensitive), ...)。
                 user 段对应 config_json 内字段；system 段单值类型 fields=()
                 （value 直接存密文串，非 dict）。
                 sensitive=True 的字段审计脱敏按标记走（不靠字段名猜，
                 业界 __sensitive_fields__ 模式）；False=非敏感（如
                 oauth_client 的 client_id），审计可明文记录。
    storage_mode: 存储形态（业界出示型/验证型三分法）：
                 "encrypted"=出示型——需可解密出示给外部服务（PAT/AKSK，
                 默认值，现全量形态）；
                 "hashed"=验证型——本地 HMAC/比对用，明文永不落库
                 （为 webhook secret 类预留，当前无消费方，零迁移）；
                 "external_ref"=外部引用——本库只存指向外部 Secret
                 Manager 的引用（闸门③ 触发后的形态位）。
    description: 人类可读说明（管理面/审计可读性）。
    deprecated:  True = 清退过渡期值域（<p>_oauth）——写路径拒绝+告警，
                 读路径兼容存量（P1=a 冻结不删，行还在只是无人写）。
    """

    key: str
    scope: str
    fields: tuple
    description: str
    storage_mode: str = "encrypted"   # "encrypted" | "hashed" | "external_ref"
    deprecated: bool = False


# ---------------------------------------------------------------------------
# user 段：user_credentials.provider 值域（三族，与 db.py _CREDENTIALS_DDL
# 注释三族描述同源对齐——注释已改为指向本注册表，消灭双处维护）
# ---------------------------------------------------------------------------

# 族 1：云凭据裸名（settings.py/devstage.py 固定白名单，fields=ak/sk）
_CLOUD_SPECS = (
    CredentialTypeSpec(
        key="huawei_cloud",
        scope="user",
        fields=(("ak", True, True), ("sk", True, True)),
        description="华为云 AK/SK（settings.py update_credentials / devstage.py 写入；"
                    "runtime/service.py _collect_server_env 注入 HUAWEICLOUD_SDK_AK/SK）",
    ),
    CredentialTypeSpec(
        key="codearts",
        scope="user",
        fields=(("ak", True, True), ("sk", True, True)),
        description="CodeArts AK/SK（settings.py update_credentials 写入；"
                    "注入 CODEARTS_CLI_AK/SK）",
    ),
)

# 族 2：PAT 复合名（平台卡派生——平台清单动态，注册表按"派生规则"登记
# 而非逐平台枚举。已知平台实锚：gitcode_pat/github_pat/gitee_pat/gitlab_pat）
_PAT_FIELDS = (("token", True, True),)


# 扫雷⑦（中危①）：内置平台实锚补 gitlab（configs/platforms/gitlab.json 在册，
# gitlab PAT 写路径 settings.py platform_tokens 同样在用）。此处仍保留显式清单
# 供审计，但清单来源对齐平台卡注册表（唯一权威）——卡在册即登记，避免新平台
# 接入时本处漏改（gitlab 即漏改实例）。
def _known_platforms() -> tuple:
    """内置 PAT/OAuth 实锚平台清单：平台卡注册表驱动，异常时回退内置四平台。

    判定依据：configs/platforms/{gitcode,github,gitee,gitlab}.json 四卡均在册；
    写路径 settings.py set_credential(f"{_p}_pat") 的 _p 即来自该卡清单，故
    登记面与卡清单同源（list_vcs_platforms()），gitcode/github 存量兜底恒在
    （与 repos.py _supported_providers() 同口径）。
    """
    try:
        from src.domains.runtime.vcs.platform_card import list_vcs_platforms
        return tuple(sorted(set(list_vcs_platforms()) | {"gitcode", "github"}))
    except Exception:
        # 卡体系不可用（极端启动序）：回退内置四平台（含 gitlab，勿再漏）
        return ("gitcode", "github", "gitlab", "gitee")


def _pat_specs() -> tuple:
    """PAT 段规格生成：内置平台显式登记 + 卡驱动动态平台按规则匹配。

    为什么混合式：settings.py L552 set_credential(user_id, f"{_p}_pat")
    的 _p 来自 platform_card 平台清单（动态）。纯静态枚举会在新平台接入
    时漏登记（守卫误报）；纯动态匹配则丢失"显式清单"的可审计性。
    折中：实锚平台经 _known_platforms()（卡驱动 + 内置兜底）显式列出
    （文档价值），其余平台走 _is_dynamic_pat() 规则匹配（键形态 <p>_pat
    且 <p> 在平台卡清单中）。
    """
    known = _known_platforms()
    return tuple(
        CredentialTypeSpec(
            key=f"{p}_pat",
            scope="user",
            fields=_PAT_FIELDS,
            description=f"{p} 用户 PAT（settings.py platform_tokens 写入；"
                        f"token_resolver UserToken 级 PAT 优先链消费，P-3A）",
        )
        for p in known
    )


_PAT_SPECS = _pat_specs()


def _is_dynamic_pat(key: str) -> bool:
    """动态 PAT 规则匹配：<p>_pat 且平台在卡清单（platform_card）中。

    供 lookup() 在静态清单未命中时兜底——新平台（如 gitlab_pat）接入后
    卡在册即自动合法，注册表无需改代码。
    """
    if not key.endswith("_pat"):
        return False
    platform = key[: -len("_pat")]
    if not platform:
        return False
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        return get_platform_card(platform) is not None
    except Exception:
        # 卡体系不可用（极端启动序）：退回内置四平台判定，不放行未知键
        return platform in _known_platforms()


# 族 3：OAuth 副本 <p>_oauth（deprecated——D100 §三 快照清退，写点 W1 已删；
# 存量行 P1=a 冻结不删。规格保留供 lookup 命中后走 deprecated 拒写路径）
def _oauth_specs() -> tuple:
    known = _known_platforms()  # 扫雷⑦：同 PAT 段，补 gitlab
    return tuple(
        CredentialTypeSpec(
            key=f"{p}_oauth",
            scope="user",
            fields=(("access_token", True, True), ("refresh_token", False, True),
                    ("fetched_at", False, False), ("token_expires_at", False, False)),
            description=f"{p} OAuth 快照副本（D100 清退：权威=external_identities；"
                        f"写路径已拒绝，存量行冻结）",
            deprecated=True,
        )
        for p in known
    )


_OAUTH_SPECS = _oauth_specs()


# ---------------------------------------------------------------------------
# repo 段：grape_t_repo_credentials.cred_type 值域（D99 已收敛 {'bot','webhook'}，
# 读写点 12 处归一。注册表登记仅为对齐审计——rc 侧守卫仍在 db.py/repos.py
# 原位（_CRED_COLS/值域断言），本段不做第二处强校验，防双源漂移）
# ---------------------------------------------------------------------------

_REPO_SPECS = (
    CredentialTypeSpec(
        key="bot",
        scope="repo",
        fields=(),
        description="仓库 bot token（grape_t_repo_credentials 唯一权威；D99 值域归一）",
    ),
    CredentialTypeSpec(
        key="webhook",
        scope="repo",
        fields=(),
        description="仓库 webhook token/secret（D99 值域归一）",
    ),
)


# ---------------------------------------------------------------------------
# system 段：grape_t_system_credentials.cred_type 值域（D100 批 1 建表登记。
# 【定义≠收编】——A1-A4 真实 secret 留在 meta/env 现状，本段 key 仅声明
# 值域合法性；消费方接位（批 2+，各带触发条件）时才发生数据迁移。
# 单值类型 fields=()（value 直接存密文串）；多字段类型 fields 约定
# JSON dict 结构（value 存加密后的 JSON dict 密文，同 uc config_json 先例）
# ---------------------------------------------------------------------------

_SYSTEM_SPECS = (
    CredentialTypeSpec(
        key="oauth_client",
        scope="system",
        fields=(("client_id", True, False), ("client_secret", True, True)),
        description="OAuth 应用凭据（现存量：meta 表 oauth_<p>_client_id/secret 明文"
                    "+ oauth-providers.json 双写——A1，迁移待触发：OAuth client "
                    "运行时管理页立项）",
    ),
    CredentialTypeSpec(
        key="vlm_api_key",
        scope="system",
        fields=(),
        description="VLM(opengw) API key（现存量：env VLM_API_KEY > configs/"
                    "vlm-config.json——A2，迁移待触发：VLM 配置运行时变更需求）",
    ),
    CredentialTypeSpec(
        key="obs_aksk",
        scope="system",
        fields=(("ak", True, True), ("sk", True, True)),
        description="华为云 OBS AK/SK（现存量：env OBS_ACCESS_KEY/SECRET_KEY——A3，"
                    "迁移待触发；华为云 ECS 部署终态=IAM 委托，不经本表）",
    ),
    CredentialTypeSpec(
        key="resend_api_key",
        scope="system",
        fields=(),
        description="RESEND 邮件 API key（现存量：env RESEND_API_KEY——A4，"
                    "迁移待触发：邮件配置管理页立项）",
    ),
    CredentialTypeSpec(
        key="admin_beta_api_key",
        scope="system",
        fields=(),
        description="admin beta 申请 API key（现存量：env ADMIN_BETA_APPLY_API_KEY"
                    "——A4，迁移待触发同上）",
    ),
    # 测试型 key：UC42⑥ 门面实测用（批 1 零消费方下 use/resolve 回落/
    # 双审计断言的实测载体）。生产不消费、无 env fallback。
    CredentialTypeSpec(
        key="test_probe",
        scope="system",
        fields=(),
        description="D100 批 1 测试探针（UC42⑥ 门面五入口实测载体；"
                    "生产零消费，永不迁移）",
    ),
)


# ---------------------------------------------------------------------------
# 注册表组装与查询 API（纯函数，零 DB 依赖）
# ---------------------------------------------------------------------------

# 全量清单：三段拼接（tuple 保不可变；段内顺序即登记顺序）
SPECS: tuple = _CLOUD_SPECS + _PAT_SPECS + _OAUTH_SPECS + _REPO_SPECS + _SYSTEM_SPECS

# key → spec 索引（模块级一次性构建；dataclass frozen 保证运行期不可变）
_BY_KEY: dict = {s.key: s for s in SPECS}


def lookup(key: str) -> CredentialTypeSpec | None:
    """查登记。静态清单未命中时对 user 段 PAT 形态做卡驱动动态匹配。

    返回 None = 未登记（守卫据 ENFORCE 决定拒绝或告警放行）。
    """
    if not key:
        return None
    spec = _BY_KEY.get(key)
    if spec is not None:
        return spec
    # 动态 PAT 兜底命中后合成 spec（不进 _BY_KEY——平台下线卡删除后自动失效）
    if _is_dynamic_pat(key):
        return CredentialTypeSpec(
            key=key,
            scope="user",
            fields=_PAT_FIELDS,
            description=f"动态 PAT（platform_card 平台在册派生：{key}）",
        )
    return None


def is_deprecated(key: str) -> bool:
    """deprecated 判定：<p>_oauth 等清退值域。写路径拒绝+告警依据。"""
    spec = lookup(key)
    return bool(spec and spec.deprecated)


def known_keys(scope: str | None = None) -> tuple:
    """列出登记 key（scope 过滤可选）。管理面/审计/测试用。"""
    if scope is None:
        return tuple(s.key for s in SPECS)
    return tuple(s.key for s in SPECS if s.scope == scope)


def validate_fields(key: str, config: dict) -> tuple:
    """按注册表 fields 约定校验 config dict。返回 (ok, missing_fields)。

    仅校验"登记了的必填字段是否在"——多余字段不拒（config_json 是开放
    dict，云凭据可有备注等附加键）。fields=() 的单值类型恒 ok。
    fields 三元组 (name, required, sensitive)：本函数只消费 required 位
    （sensitive 位供审计脱敏路由，见 sensitive_fields()）。
    """
    spec = lookup(key)
    if spec is None or not spec.fields:
        return True, ()
    missing = tuple(item[0] for item in spec.fields
                    if item[1] and not (config or {}).get(item[0]))
    return (not missing), missing


def sensitive_fields(key: str) -> tuple:
    """按注册表 sensitive 标记返回敏感字段名（审计脱敏路由依据）。

    标记驱动而非字段名猜测（业界 __sensitive_fields__ 模式）——
    例：oauth_client 的 client_id 非敏感可明文记录，client_secret 敏感
    只可指纹。未登记 key 返回空串占位（调用方按全敏感兜底）。
    """
    spec = lookup(key)
    if spec is None or not spec.fields:
        return ()
    return tuple(item[0] for item in spec.fields if item[2])


def storage_mode_of(key: str) -> str:
    """存储形态查询（"encrypted" | "hashed" | "external_ref"）。

    现全量 "encrypted"（出示型）；"hashed"/"external_ref" 为形态位预留
    （验证型/闸门③ 外部引用），当前零消费方零迁移。
    """
    spec = lookup(key)
    return spec.storage_mode if spec else "encrypted"
