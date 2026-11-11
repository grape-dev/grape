"""反回潮 ratchet：禁新增 ("gitcode", "github") 二元平台字面枚举。

背景（S107/G2 卡驱动改造后）：平台能力判定应全部经 platform card 派生
（get_platform_card / list_vcs_platforms），源码中出现"恰好两个不同平台
字面量"的行几乎必然是二元硬编码分叉（`in ("gitcode", "github")`、
`{"gitcode", "github"}` 集合白名单、双平台字面 dict 映射等），即回潮点。

扫描模式：src/**/*.py 非注释行，行内带引号平台字面量（gitcode/github/
gitee/gitlab）恰好出现两个不同名 → 视为二元枚举候选。三平台以上的枚举
（如 credential_registry 的 ("gitcode","github","gitee")）不算二元，不拦。

基线豁免：BASELINE 按 (相对 src/ 的路径, 行内容) 计数豁免——同文件同
内容行数超出基线计数即红，新增行（新内容或超量）一律红。每个基线点逐个
审阅后写死豁免理由。基线锚定的是卡驱动收口完成后（含并行批次落在工作区
的 try/except 异常回退分支）的快照。

父任务给定清单中三点不落入本模式，说明：
- src/prompt_builder.py:39-63（`_is_github_repo(...) else gitcode URL`
  二元三元分叉）：行内无成对带引号平台 token，属 D71 登记冻结的既有
  分叉，本 ratchet 不覆盖（后续单独收口）；
- src/domains/runtime/im/handler_async.py:879：("gitcode:","github:","gitlab:") 三元
  前缀遍历，非二元；
- src/api/event_pipeline.py:4201：`repo_gitcode_token` 变量赋值，无
  字面量枚举。

RED 验证（XPRESS）：临时向 src/ 注入一行二元枚举 → 本测试红 → 撤销 →
绿。注入不进 commit。
"""
import os
import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"

_PLATFORM_TOKEN = re.compile(r'''["'](gitcode|github|gitee|gitlab)["']''')

# ── D105-14 扩维：单平台字面量比较守卫（provider == "<平台>" 分叉形态）──
# 扫描面诚实声明：仅覆盖 provider 与平台字面量的直接 ==/!= 比较；字典映射/
# 动态分派/非 provider 变量名（plat == "github"）不在本模式，后者由上方
# 二元枚举 ratchet 与 BASELINE 冻结覆盖。不扫裸平台词（URL 拼接/变量名误伤
# 不可接受，且属 D71 冻结存量域）。预期增量成本≈零：卡驱动接入不产生本形态。
_COMPARE_PATTERN = re.compile(
    r'''\bprovider\s*(?:==|!=)\s*["'](gitcode|github|gitee|gitlab)["']'''
    r'''|["'](gitcode|github|gitee|gitlab)["']\s*(?:==|!=)\s*\bprovider''')

# (相对 src/ 的路径, 行内容 strip 后) → 基线豁免出现次数。
# 存量逐个审阅：全部为 D71/D105-3 登记冻结的既有能力分叉（转译/路径选择），
# 非新增判定——棘轮只保证不新增。

_PREFIX = {
    "vcs/": "domains/runtime/vcs/", "user/": "domains/mgmt/user/", "im/": "domains/runtime/im/",
    "runtime/": "domains/runtime/engine/", "agent/": "domains/runtime/agents/",
    "admin/": "domains/mgmt/admin/", "storage/": "domains/runtime/storage/",
    "observability/": "domains/insight/observability/",
}
_TOPMAP = {"repo.py": "kernel/repo.py", "repo_key.py": "kernel/repo_key.py",
           "repo_migrate.py": "kernel/repo_migrate.py", "token_resolver.py": "kernel/token_resolver.py",
           "token_service.py": "kernel/token_service.py", "session_manager.py": "kernel/session_manager.py",
           "prompt_builder.py": "kernel/prompt_builder.py", "config.py": "kernel/config.py",
           "mysql_compat.py": "kernel/mysql_compat.py", "worker_package.py": "kernel/worker_package.py",
           "outbound_ledger.py": "kernel/outbound_ledger.py", "api/event_pipeline.py": "domains/runtime/pipeline/event_pipeline.py"}
_ROUTE_DOM = {"agents": "mgmt", "auth": "mgmt", "autopilot": "mgmt", "community": "mgmt", "devstage": "mgmt",
              "feedback": "mgmt", "feishu_card": "mgmt", "feishu_onboard": "mgmt", "grape_auth": "mgmt",
              "knowledge": "mgmt", "orgs": "mgmt", "patrol": "mgmt", "repos": "mgmt", "scenes": "mgmt",
              "settings": "mgmt", "skills": "mgmt", "templates": "mgmt",
              "chat": "runtime", "events": "runtime", "issues": "runtime", "pipeline": "runtime",
              "runtime": "runtime", "runtime_view": "runtime", "task_logs": "runtime", "tasks": "runtime",
              "webhook": "runtime", "dashboard": "insight", "health": "insight", "latency": "insight",
              "observability": "insight", "search": "insight"}


def _norm_path(rel: str) -> str:
    """把老布局相对路径归一到 arch 布局（基线键与实测路径同函数，避免棘轮误红）。"""
    if rel in _TOPMAP:
        return _TOPMAP[rel]
    if rel.startswith("api/routes/"):
        name = rel[len("api/routes/"):-3]
        if name in _ROUTE_DOM:
            return f"domains/{_ROUTE_DOM[name]}/routes/{name}.py"
    for old, new in _PREFIX.items():
        if rel.startswith(old) and not rel.startswith("domains/"):
            return new + rel[len(old):]
    return rel

_COMPARE_BASELINE_RAW = {
    # ── repo_manager：github 专属实现分派（D105-17 审阅冻结，治理后合理保留）──
    ('domains/mgmt/user/repo_manager.py', '_wf_denied = (provider == "github") if _wf is None else (not _wf)'): 1,
    ('domains/mgmt/user/repo_manager.py', 'if provider == "github":'): 1,
    # ① push workflows 判定（D105-18 三态 None 回退路径，github 实现绑定保留：
    # GitHub App token 无 workflows 权限）；② webhook
    # 函数指针分派 `if provider == "github"`：1——两处均为 github 实现绑定，
    # gitcode 卡 dialect 同为 github 无法用 dialect 区分；收口归
    # token_resolver 适配器端口（S62 后续）。
    # G2 数据化改造后已消失：gitee 单平台比较行（原豁免 1 处，行已不在）
    ('domains/mgmt/user/db.py', 'if provider != "github":'): 1,
    ('domains/runtime/vcs/platform_card.py', 'return "GitHub" if provider == "github" else "GitCode"'): 1,
    ('domains/runtime/vcs/platform_card.py', 'return "github.com" if provider == "github" else "gitcode.com"'): 1,
    # ── 合并裁定重同步（dc17f2812 后 weekly-10 恢复面，pristine 实锤）──
    ('api/issue_vcs_index.py', 'elif provider == "gitcode":'): 1,  # weekly-10-native (merge adjudication dc17f2812)
    ('api/issue_vcs_index.py', 'if provider == "github":'): 1,  # weekly-10-native (merge adjudication dc17f2812)
    ('api/routes/scenes.py', 'if provider == "github":'): 2,  # weekly-10-native (merge adjudication dc17f2812)
    # F4b 凭据终态化后摘除：user/db.py 写路径收敛到 repo_cred_write_keys 逻辑键路由，
    # 旧的二元列名比较行（bot_github/gitcode 三元式）已消失；存量读侧/健康检查
    # 能力分叉仍有 5 处单平台比较（实数冻结，F4b 收敛后 7→5）。
    ('domains/mgmt/user/db.py', 'if provider == "github":'): 1,  # dual-platform capability fork, reviewed freeze (5->4 G2 routing; ->1 S58 webhook 走 provider 端口)
    # ── r3 增量合并裁定重同步（base tip 9c4c770b8 / 本分支增量逐一实锤审阅）：──
    # IM 引擎通知出口按 provider 选渠道形态——weekly-10 原生（im/ 引擎目录整体并入）。
    ('domains/runtime/im/notify.py', 'if provider == "github":'): 1,  # weekly-10 IM engine native (r3 merge adjudication)
    ('domains/runtime/im/notify.py', 'if provider == "gitlab":'): 1,  # weekly-10 IM engine native (r3 merge adjudication)
    # config-driven API 客户端的 per-platform API 形态分叉（分页/分支引用等
    # 上游 API 差异；配置权威由 test_api_client_config_driven 守卫，此处为
    # D71 形态能力分叉冻结，非判定回潮）。
    ('domains/runtime/vcs/api_client.py', 'if self.provider == "gitcode":'): 2,  # per-API shape fork (D105 config-driven client, reviewed freeze)
    ('domains/runtime/vcs/api_client.py', 'elif self.provider == "github":'): 2,  # per-API shape fork (D105 config-driven client, reviewed freeze)
    ('domains/runtime/vcs/api_client.py', 'elif self.provider == "gitlab":'): 1,  # per-API shape fork (D105 config-driven client, reviewed freeze)
    # Issue #343：GitHub 写操作 bot token 优先/用户 token 回落的能力分叉
    # （GitCode 路径对称差异，登记冻结）。
    ('api/routes/issues.py', 'if provider == "github":'): 1,  # Issue #343 github bot-token-first capability fork (reviewed freeze)
}

# (相对 src/ 的路径, 行内容 strip 后) → 基线豁免出现次数。
BASELINE_RAW = {
    # ── 存量主机/基址映射（D71 登记冻结，非能力判定）──
    ('repo.py', '_HOSTS = {"gitcode": "gitcode.com", "github": "github.com"}'): 1,
    ('repo.py', '_API_BASES = {"gitcode": "https://gitcode.com/api/v5", "github": "https://api.github.com"}'): 1,
    ('domains/runtime/engine/repogit.py', '_HOSTS = {"gitcode": "gitcode.com", "github": "github.com"}'): 1,
    # ── repo_key：um 平台列白名单（D39/S59 冻结）+ 存量迁移遍历 + 方言自映射 ──
    ('repo_key.py', '_UM_PROVIDER_COLUMNS = {"gitcode": "gitcode_user", "github": "github_login"}'): 1,
    ('repo_key.py', 'providers = ["gitcode", "github"]'): 1,
    ('repo_key.py', '_DIALECT_FALLBACK = {"github": "github", "gitlab": "gitlab"}'): 1,
    # ── docstring/注释提及两平台（非判定）──
    ('token_resolver.py', '"""provider 标识: "gitcode" / "github"。"""'): 1,
    ('domains/mgmt/user/db.py', 'provider: "gitcode" 查 GitCode 路径 B 仓库，"github" 查 GitHub 路径 B 仓库。'): 1,
    ('domains/mgmt/user/db.py', 'provider: "gitcode" 或 "github"'): 1,
    ('api/oauth_token.py', 'provider: "gitcode" 或 "github"'): 1,
    ('domains/mgmt/admin/db.py', "provider: 'gitcode' or 'github'"): 1,
    ('domains/mgmt/admin/routes.py', 'provider: str  # "gitcode" or "github"'): 1,
    ('domains/runtime/engine/bot_token_health.py', 'provider: "gitcode" 或 "github"'): 1,
    ('domains/runtime/engine/service.py', '_expired_provider = _provider.provider_name  # "gitcode" / "github"'): 1,
    ('api/event_pipeline.py', 'D29/S49：评论投递族原先的 "github"/"gitcode" 字面量统一收敛到此。'): 1,
    # ── domains/mgmt/user/db.py：D15 全局 bot token 文件字段映射 + 路径 B 存量 provider 分支（D71 冻结）──
    ('domains/mgmt/user/db.py', '_GLOBAL_BOT_FILE_FIELDS = {"gitcode": "gitcode_api_token", "github": "github_bot_token"}'): 1,
    # ── vcs 方言注册：github/gitlab 显式注册（gitcode 走默认方言，S107 后卡驱动选择）──
    ('domains/runtime/vcs/dialects.py', '_DIALECTS = {"github": GitHubDialect, "gitlab": GitLabDialect}'): 1,
    ('domains/runtime/vcs/platform_card.py', '_VALID_DIALECTS = ("github", "gitlab")'): 1,
    # ── issues 投递结果报告结构：存量两平台键（D71 冻结）──
    ('api/routes/issues.py', 'result = {"gitcode": "skipped", "github": "skipped", "message": ""}'): 1,
    ('api/routes/issues.py', '"issue_closed": {"gitcode": "skipped", "github": "skipped"}}'): 2,
    # ── repos 路由：存量平台集合遍历/并集兜底（D71 冻结）──
    ('api/routes/repos.py', '_platforms = {"gitcode", "github"}'): 2,
    ('api/routes/repos.py', 'for _p in ("github", "gitcode"):'): 1,
    ('api/routes/repos.py', 'return set(list_vcs_platforms()) | {"gitcode", "github"}'): 1,
    # ── 扫雷⑦ 批B1：credential_registry 实锚卡驱动（同 repos.py 口径，
    #    gitcode/github 存量兜底恒在，非二元能力判定）──
    ('domains/mgmt/user/credential_registry.py', 'return tuple(sorted(set(list_vcs_platforms()) | {"gitcode", "github"}))'): 1,
    # ── scenes/settings：并行收口批次引入的 try/except 异常回退分支（卡派生失败兜底）──
    ('api/routes/settings.py', '_probe_platforms = ["gitcode", "github"]'): 1,
    # G3 已清除：settings token 字段派生改 SETTINGS_TOKEN_FIELD_SUFFIX 契约开关（行已消失）
    # ── settings 路由：存量 PAT 字段映射 / 默认平台集合（D71 冻结）──
    ('api/routes/settings.py', '_LEGACY_PAT_FIELD_PROVIDERS = {"gitcode", "github"}'): 1,
    ('api/routes/settings.py', '_DEFAULT_PLATFORMS = {"gitcode", "github"}'): 1,
    # ── webhook：卡派生判定之后的存量能力兜底（D88/S115 收口残留面）──
    # 旧基线点 'if _provider not in ("gitcode", "github") or not _owner or not _name:'
    # 是 M2 合并版 webhook 形态；F1 搬迁回 r2 恢复版（dd45db977）后该行不存在，
    # 由下行我们的卡驱动守卫行顶替（F1 relocation 实测在位 ×1）。
    ('api/routes/webhook.py', 'if get_platform_card(provider) is None or provider in ("gitcode", "github"):'): 1,  # our card-driven guard (r2 webhook, reviewed freeze)
    # ── domains/mgmt/user/db.py：webhook token 存量列兜底（D71 形态冻结，_webhook_token_col_for
    #    同族——gitcode/github 两平台才有 webhook token 存量列，非能力判定）──
    ('domains/mgmt/user/db.py', 'if provider in ("gitcode", "github"):'): 1,  # D71 legacy webhook-token column fallback (reviewed freeze)
    # ── 合并裁定重同步（dc17f2812 后 weekly-10 恢复面）：
    # 以下均为 weekly-10 原生代码（pristine weekly-10 逐一实锤在位），
    # 按“weekly-10 为准”合并策略登记豁免，非新增回潮。
    ('api/event_pipeline.py', '_ledger_record_comment("github" if _is_github_repo(repo_key) else "gitcode",'): 0,  # 摘除：weekly-10 收口后行已消失（防呆见 test_baseline_points_still_exist）
    ('api/feedback_status.py', 'else "gitlab" if host.startswith("gitlab") else "gitcode")'): 1,  # weekly-10-native (merge adjudication dc17f2812)
    ('api/routes/issues.py', '_provider_name = "github" if is_github else "gitcode"'): 0,  # 摘除：r3 后已卡派生为 _provider_name = provider
    ('api/routes/scenes.py', 'for prov in ("github", "gitcode"):'): 1,  # weekly-10-native (merge adjudication dc17f2812)
    ('api/routes/scenes.py', 'if provider in ("github", "gitcode"):'): 2,  # weekly-10-native (merge adjudication dc17f2812)
    ('domains/runtime/engine/direct_executor.py', 'log.info(f"Exec agent: bot_token={\'set\' if repo_gitcode_token else \'none\'} (provider={\'github\' if repo_key.startswith(\'github:\') else \'gitcode\'})")'): 1,  # weekly-10-native (merge adjudication dc17f2812)
}


BASELINE = {(_norm_path(k[0]), k[1]): v for k, v in BASELINE_RAW.items()}
_COMPARE_BASELINE = {(_norm_path(k[0]), k[1]): v for k, v in _COMPARE_BASELINE_RAW.items()}


def _scan_binary_enums() -> Counter:
    """返回 ((相对src路径, 行内容), 出现次数)，仅统计恰好两个不同平台字面量的非注释行。"""
    counts: Counter = Counter()
    for dirpath, _dirnames, filenames in os.walk(SRC_ROOT):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = Path(dirpath) / fn
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if len(set(_PLATFORM_TOKEN.findall(line))) == 2:
                    counts[(_norm_path(str(p.relative_to(SRC_ROOT))), line)] += 1
    return counts


def test_no_new_binary_platform_enumerations():
    """Ratchet：源码二元平台枚举不得超出基线（新增点即红）。"""
    current = _scan_binary_enums()
    regressions = []
    for key, n in current.items():
        allowed = BASELINE.get(key, 0)
        if n > allowed:
            regressions.append(f"{key[0]}: x{n} (基线豁免 {allowed}): {key[1][:100]}")
    assert not regressions, (
        "发现新增二元平台字面枚举（应改为 platform card 派生判定）：\n"
        + "\n".join(regressions)
    )


def test_baseline_points_still_exist():
    """看护：基线点被删除后应从 BASELINE 摘除，防止豁免清单腐化成永久白名单。"""
    current = _scan_binary_enums()
    stale = [key for key in BASELINE if current.get(key, 0) < BASELINE[key]]
    assert not stale, f"基线点已消失，请从 BASELINE 摘除：{stale}"


def _scan_single_platform_compares() -> Counter:
    """返回 ((相对src路径, 行内容), 出现次数)：provider 与单平台字面量直接比较。"""
    counts: Counter = Counter()
    for dirpath, _dirnames, filenames in os.walk(SRC_ROOT):
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = Path(dirpath) / fn
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if _COMPARE_PATTERN.search(line):
                    counts[(_norm_path(str(p.relative_to(SRC_ROOT))), line)] += 1
    return counts


def test_no_new_single_platform_provider_compares():
    """D105-14 棘轮扩维：provider 与单平台字面量的直接比较不得新增（新增即红）。

    存量命中已逐个审阅冻结（_COMPARE_BASELINE，均为 D71/D105-3 登记的既有
    能力分叉）。扫描面仅 provider 直接比较形态，诚实边界见 _COMPARE_PATTERN 注释。
    """
    current = _scan_single_platform_compares()
    regressions = []
    for key, n in current.items():
        allowed = _COMPARE_BASELINE.get(key, 0)
        if n > allowed:
            regressions.append(f"{key[0]}: x{n} (基线豁免 {allowed}): {key[1][:100]}")
    assert not regressions, (
        "发现新增 provider 单平台字面量比较（应改为 platform card 派生判定）：\n"
        + "\n".join(regressions)
    )


def test_compare_baseline_points_still_exist():
    """看护：比较基线点消失后应从 _COMPARE_BASELINE 摘除（防豁免清单腐化）。"""
    current = _scan_single_platform_compares()
    stale = [key for key in _COMPARE_BASELINE if current.get(key, 0) < _COMPARE_BASELINE[key]]
    assert not stale, f"基线点已消失，请从 _COMPARE_BASELINE 摘除：{stale}"
