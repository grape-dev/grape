"""Skills 路由：列表、上传、删除、个人技能转仓库技能"""

from src.kernel.domain_db import cursor as _domain_cursor
import io as _io

import hashlib, os, tempfile, zipfile, json, shutil, stat as _stat

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, Response, Query, Body
from pydantic import BaseModel

from src.domains.mgmt.user import db as user_db

from src.domains.mgmt.user.validator import validate_skill as validate_custom_skill, parse_skill_frontmatter

from src.api.deps import (get_current_user_or_api_key,
                          get_current_user_or_api_key_or_admin,
                          require_no_session_token,
                          require_no_session_token_or_admin,
                          require_org_member)
from src.kernel.tags import (MAX_TAG_LEN, encode_tags, oversized_tags,
                             parse_tags, prepare_tags, suggest_tags)

#: 技能标签上限（Issue #3202）：技能侧显式传参，kernel 默认 3 保持场景市场契约不变。
SKILL_MAX_TAGS = 5
#: 系统标签（agent 类型名）——用户自定义标签不得与之重名。
_SYSTEM_AGENT_TAGS = {"vod", "issue", "coding", "qa", "sre", "chat", "auto",
                      "VOD", "Issue", "Coding", "QA", "SRE", "Chat", "Auto"}

# skill 归运行域：列表/统计/单行读取走该域原语或具名查询
from src.domains.runtime import public as _rt_skills

router = APIRouter(prefix="/api/skills", tags=["skills"])

# 解压后总大小上限（zip 炸弹防护，见 obs-skill-storage-design.md §7）
MAX_EXTRACT_BYTES = int(os.environ.get("SKILL_MAX_EXTRACT_BYTES", str(100 * 1024 * 1024)))

def _resolve_repo_key(repo: str, strict: bool = False) -> str:
    """查 DB 确定实际 repo_key 前缀（gitcode: 或 github:）。

    strict=True（#444 根治，仅写路径 upload/convert 使用）：裸 repo 同时命中
    gitcode: 与 github: 两行时显式报错（歧义从"静默 LIMIT 1 不确定归属"变为
    "可处置的明确指引"），防止覆盖上传写错 provider 行。读路径（list/search/
    download）保持原 LIMIT 1 行为不变——读错列表无数据损害，收窄修复范围。
    """
    from src.kernel.repo_key import to_repo_key
    rk = to_repo_key(repo)
    if ":" in repo:
        return rk
    try:
        from src.kernel.session_manager import db as _db, db_lock as _dl
        with _dl:
            c = _db.cursor()
            from src.kernel.repo_key import repo_key_candidates
            _rks = tuple(repo_key_candidates(repo))
            rows = c.execute(f"SELECT repo_key FROM managed_repos WHERE repo_key IN ({','.join('?' * len(_rks))})",
                             _rks).fetchall()
            # S100/D75：歧义守卫 >= 2（候选集已卡派生多平台，写死 ==2 会漏
            # ≥3 平台同名仓）；文案动态列出命中平台，不再硬编码双平台
            if len(rows) >= 2 and strict:
                _hit = [((r[0] or "").split(":", 1)[0] or "裸键") for r in rows]
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": f"仓库 {repo} 同时存在于 {'、'.join(_hit)}，请指定完整仓库标识（如 gitcode:{repo}）后重试"})
            if rows:
                return rows[0][0]
    except HTTPException:
        raise
    except Exception:
        pass
    return rk

def _skill_category_payload(cat_l1_id: int = 0, cat_l2_id: int = 0) -> dict:
    """分类路径 {l1, l2}（Issue #2761）。"""
    from src.domains.mgmt.user.db import category_path
    return category_path(int(cat_l1_id or 0), int(cat_l2_id or 0))


def _normalize_skill_category(cat_l1_id: int, cat_l2_id: int) -> tuple[int, int]:
    """校验并归一化技能分类（Issue #2761）。

    统一口径见 `_category.normalize_category_pair`（与场景写路径共用）：
    - 一级/二级 ID 必须存在且层级正确（一级 level=1、二级 level=2）；
    - 二级必须属于所给一级（只给二级时由 parent_id 反推一级）。
    """
    from src.domains.mgmt.routes._category import normalize_category_pair
    return normalize_category_pair(cat_l1_id, cat_l2_id)


def _skill_tag_suggestions(name: str = "", description: str = "") -> list[str]:
    """规则型标签候选（仅返回真实命中项，最多 3 个）。"""
    return suggest_tags(name, description, candidates=(
        "AI", "自动化", "测试", "部署", "代码评审", "文档", "安全", "监控", "数据", "协作"))


def _normalize_skill_zip(skill_dir: str, skill_name: str) -> bytes:
    """规范化重打包技能 zip：根 = {skill_name}/，逐文件保留权限位（external_attr），
    丢弃系统目录。返回 zip 字节（OBS 存储正体，见 obs-skill-storage-design.md §2.2）。"""
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(skill_dir):
            dirs[:] = [d for d in dirs if d not in ("__MACOSX", ".git", "__pycache__")]
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, skill_dir).replace(os.sep, "/")
                arc = f"{skill_name}/{rel}"
                st = os.stat(full)
                info = zipfile.ZipInfo(arc)
                info.external_attr = (_stat.S_IMODE(st.st_mode)) << 16  # 保留权限位
                with open(full, "rb") as fh:
                    zf.writestr(info, fh.read())
    return buf.getvalue()


# ===== Issue #3168 技能详情页：摘要 / 版本 / 文件清单 / 评论 =====
_SUMMARY_MAX = 120
_COMMENT_MAX = 1000
# 相关技能池：只取热度前 N 作为共享标签/同类型打分的候选（全库技能 ~10² 量级，
# 300 覆盖全部活跃技能且打分开销有界；detail 页只展示前 _RELATED_SKILL_COUNT 条）。
_RELATED_POOL_SIZE = 300
# 详情页「相关技能」最多展示 6 张卡片（两行 3 列网格，UI 设计值）。
_RELATED_SKILL_COUNT = 6
# 评论分页上限：防止一次拉全表（默认 20，UI「加载更多」逐页取）。
_COMMENT_PAGE_MAX = 50
# 概览 intro 槽位（#3227）：键为固定槽位名（scope/steps/products/constraints/review）
# 加 5px 余量；单项文本 400 字对齐 _clean_summary 的概述截断口径（QA B2：数值须具名）。
_INTRO_KEY_MAX = 32
_INTRO_TEXT_MAX = 400
# 列宽切片上限（与 schema_ddl 的列定义逐字一致，防双后端 silent truncation 漂移）：
_VERSION_MAX = 64            # grape_t_skill_version.version VARCHAR(64)
_UPLOADER_MAX = 64           # grape_t_skill_version.uploaded_by VARCHAR(64)
_CHANGE_NOTE_MAX = 1000      # grape_t_skill_version.change_note TEXT，写入侧截断
_FILE_PATH_MAX = 512         # grape_t_skill_file.path VARCHAR(512)
_COMMENT_NAME_MAX = 128     # grape_t_skill_comment.user_name VARCHAR(128)
# 评论分页缺省 20（UI「加载更多」步长），上限见 _COMMENT_PAGE_MAX。
_COMMENT_PAGE_DEFAULT = 20
# Issue #3855：显示名称独立于 SKILL.md frontmatter name，最长 20 个 Unicode 码点
# （汉字/字母/数字/空格/emoji 各计 1，去除首尾空格后计数）。
SKILL_DISPLAY_NAME_MAX = 20


def _normalize_skill_display_name(raw) -> str:
    """去首尾空格；None 归一为空串（必填/长度校验由 _validate 负责）。"""
    return str(raw if raw is not None else "").strip()


def _skill_display_name_conflict(scope: str, owner_id: str, name: str,
                                 exclude_id: int | None = None) -> bool:
    """同 scope 内显示名称是否重复（去首尾空格 + 大小写不敏感）。

    决策（Issue #3855 spec §7.1/§8.2）：只约束用户录入的 display_name 列；
    空名存量行回退展示的源名称不纳入重名约束，历史源名称天然重复按 §8.2
    接受（按 id 区分）。因此同 scope 可能出现「有效展示名相同」的存量行，
    这是需求明确允许的口径，而非漏检。跨 scope 允许同名；比较留在 Python
    侧 casefold，规避 MySQL/SQLite LOWER/TRIM 语义差异（含 Unicode）。
    """
    target = _normalize_skill_display_name(name).casefold()
    if not target:
        return False
    if (scope or "") == "official":
        # 官方档 owner_id 历史存在 '' 与 0 两种写法，唯一性边界只按 scope 取数。
        rows = _rt_skills.read_all_any("skill_display_names_by_official_scope")
    else:
        rows = _rt_skills.read_all_any(
            "skill_display_names_by_scope", (scope or "", owner_id or ""))
    for row in rows or []:
        row_id = int(row[0] or 0)
        if exclude_id and row_id and row_id == int(exclude_id):
            continue
        if _normalize_skill_display_name(row[1]).casefold() == target:
            return True
    return False


def _validate_skill_display_name(raw, *, scope: str, owner_id: str,
                                 exclude_id: int | None = None) -> str:
    """校验显示名称：必填、≤20 Unicode 码点、同 scope 去重。

    错误语义：空值/仅空格/超长 → 400；同 scope 重名 → 409「名称已存在」。
    """
    name = _normalize_skill_display_name(raw)
    if not name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "名称不能为空"})
    if len(name) > SKILL_DISPLAY_NAME_MAX:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": f"名称不能超过 {SKILL_DISPLAY_NAME_MAX} 个字符"})
    if _skill_display_name_conflict(scope, owner_id, name, exclude_id=exclude_id):
        raise HTTPException(status_code=409, detail={"code": 409, "message": "名称已存在"})
    return name


def _converted_display_name(scope: str, owner_id: str, source_display,
                            source_name) -> str:
    """转换/复制新行的有效展示名：≤20 且同 scope 不重名。

    源自定义名称优先；缺省或超长时回退源名称并截断到上限，避免存量超长源名称
    作为 display_name 写入新行绕过 20 字约束；目标 scope 重名时追加 `~N` 后缀。
    （重名约束仍只针对 display_name 列，见 `_skill_display_name_conflict`。）
    """
    candidate = (_normalize_skill_display_name(source_display)
                 or _normalize_skill_display_name(source_name))
    if not candidate:
        candidate = "技能"
    if len(candidate) > SKILL_DISPLAY_NAME_MAX:
        candidate = candidate[:SKILL_DISPLAY_NAME_MAX].strip()
    if not candidate:
        candidate = "技能"
    if not _skill_display_name_conflict(scope, owner_id, candidate):
        return candidate
    for seq in range(2, 1000):
        suffix = f"~{seq}"
        head = candidate[:SKILL_DISPLAY_NAME_MAX - len(suffix)].rstrip()
        trial = f"{head}{suffix}" if head else suffix.lstrip("~")
        if not _skill_display_name_conflict(scope, owner_id, trial):
            return trial
    return candidate


def _is_official_skill_admin(user: dict) -> bool:
    """官方技能显示名称编辑权限：仅平台管理员（admin_accounts）可改。

    CONTRIBUTING §7「不弱化鉴权」：官方技能为部署内置物，不能任意登录用户改名。
    只信任认证依赖解析出的管理台身份：`require_admin` /
    `get_current_user_or_api_key_or_admin` 已校验 JWT role=admin 且
    admin_accounts token_version 一致后才置 `token_type='admin'`，此处直接复用
    该已解析身份，不再重复查库。**禁止**用 username 跨账户体系匹配——
    grape_accounts 与 admin_accounts 完全独立，同名普通账号不能借此取得改名权。
    """
    return (isinstance(user, dict)
            and str(user.get("token_type") or "") == "admin")


def _clean_summary(text: str, limit: int = _SUMMARY_MAX) -> str:
    """摘要清洗：禁「→」「->」链式符号，压空白后截断（D3）。"""
    import re as _re
    t = (text or "").replace("\r", " ").replace("\n", " ")
    t = _re.split(r"→|->", t, maxsplit=1)[0]
    t = _re.sub(r"\s+", " ", t).strip(" -—·")
    return t[:limit]


def _parse_version_tuple(version: str) -> tuple:
    """宽松版本比较：取数字段，缺省补 0（1.2 == 1.2.0）。"""
    import re as _re
    return tuple(int(x) for x in _re.findall(r"\d+", version or "")[:4]) or (0,)


def _version_gt(a: str, b: str) -> bool:
    ta, tb = _parse_version_tuple(a), _parse_version_tuple(b)
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    return ta > tb


def _bump_patch(version: str) -> str:
    parts = list(_parse_version_tuple(version))
    while len(parts) < 3:
        parts.append(0)
    parts[2] += 1
    return ".".join(str(x) for x in parts[:3])


def _next_skill_version(skill_id: int, incoming: str) -> str:
    """返回值即本次写入版本号；显式版本号不大于历史版本时 400 拦截（D6）。"""
    latest = _rt_skills.read_one_any("skill_version_latest", (int(skill_id),))
    latest_v = (latest[0] if latest else "") or ""
    incoming = (incoming or "").strip()
    if not latest_v:
        return incoming or "1.0.0"
    if incoming:
        if not _version_gt(incoming, latest_v):
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": f"版本号必须递增：当前最新 {latest_v}，本次 {incoming}"})
        return incoming
    return _bump_patch(latest_v)


def _record_skill_version(skill_id: int, version: str, change_note: str, user: dict) -> None:
    from src.kernel.id_gen import uuid7_str
    from datetime import datetime
    _rt_skills.exec_any("skill_version_insert", (
        uuid7_str(), int(skill_id), (version or "1.0.0")[:_VERSION_MAX],
        (change_note or "")[:_CHANGE_NOTE_MAX],
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        str(user.get("username") or user.get("id") or "")[:_UPLOADER_MAX]))
    # C3 非查询操作审计：版本行写入留痕（上传/更新/重传共用，detail 不落包内容）
    from src.domains.mgmt.auth import audit as _skill_audit
    _skill_audit.log_audit("skill_version_created",
                           user_id=str(user.get("id") or ""),
                           operator_id=str(user.get("id") or ""),
                           detail={"skill_id": int(skill_id),
                                   "version": (version or "1.0.0")[:_VERSION_MAX]})


def _validate_manual_summary(summary: str) -> str:
    """手工摘要校验：禁「→」「->」链式符号（D3），返回清洗值。"""
    import re as _re
    s = (summary or "").strip()
    if s and _re.search(r"→|->", s):
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": "摘要请用人话短句，不要包含「→」或「->」流程链符号"})
    return _clean_summary(s)


def _require_skill_summary(summary: str) -> str:
    """Issue #3202：摘要录入必填（≤120 字由 _clean_summary 截断保证）。"""
    s = (summary or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "摘要不能为空（必填，≤120 字）"})
    return _validate_manual_summary(s)


def _resolve_skill_summary(summary, description: str) -> str:
    """上传/更新入口的摘要解析（QA A14 契约兼容）。

    - 缺省（Form 字段未携带 → None）：从 SKILL.md 的 description 推导（清洗截断），
      存量 CLI / API 客户端（`grape skills upload` 不带 summary）不受影响；
    - 显式空值 / 纯空白：按 #3202 必填规则 400；
    - 显式值：走 #3202 校验（禁流程链符号 + 截断）。
    """
    if summary is None:
        return _clean_summary(description)
    return _require_skill_summary(summary)


def _system_tag_names() -> set[str]:
    """系统标签集合：agent 类型名 + 分类名（大小写不敏感）。"""
    names = {n.lower() for n in _SYSTEM_AGENT_TAGS}
    try:
        from src.domains.mgmt.user.db import list_skill_categories
        for cat in list_skill_categories() or []:
            name = str(cat.get("name") or "").strip()
            if name:
                names.add(name.lower())
    except Exception:  # noqa: BLE001 - 分类读取失败不影响系统标签硬约束
        pass
    return names


def _validate_skill_tags(raw) -> list[dict[str, str]]:
    """技能标签写侧校验（Issue #3202）：≤5 个 / 每个 ≤12 字符 / 去重 / 禁系统标签重名。"""
    tag_items, overflow = prepare_tags(raw, limit=SKILL_MAX_TAGS)
    if overflow:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": f"标签最多 {SKILL_MAX_TAGS} 个，超出：{'、'.join(overflow)}",
            "overflow": overflow})
    too_long = oversized_tags(tag_items, max_len=MAX_TAG_LEN)
    if too_long:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": f"单个标签最多 {MAX_TAG_LEN} 个字符，超出：{'、'.join(too_long)}",
            "oversized": too_long})
    system_names = _system_tag_names()
    conflicts = [t["name"] for t in tag_items if t["name"].lower() in system_names]
    if conflicts:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": f"标签不能与系统标签（Agent 类型/分类名）重名：{'、'.join(conflicts)}",
            "conflicts": conflicts})
    return tag_items


def _list_zip_entries(zip_bytes: bytes) -> list[tuple[str, int]]:
    """解析 zip 文件清单（去公共顶层目录，路径相对技能根）。"""
    if not zip_bytes:
        return []
    try:
        with zipfile.ZipFile(_io.BytesIO(zip_bytes), "r") as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
    except Exception:
        return []
    names = [i.filename.replace("\\", "/") for i in infos]
    prefix = ""
    if names and all("/" in n for n in names):
        head = names[0].split("/", 1)[0]
        if head and all(n.split("/", 1)[0] == head for n in names):
            prefix = head + "/"
    out = []
    for info, name in zip(infos, names):
        rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
        rel = rel.lstrip("/")
        if not rel:
            continue
        out.append((rel, int(info.file_size or 0)))
    return out


def _replace_skill_files(skill_id: int, entries: list[tuple[str, int]]) -> None:
    from src.kernel.id_gen import uuid7_str
    _rt_skills.exec_any("skill_file_delete_by_skill", (int(skill_id),))
    for path, size in entries:
        _rt_skills.exec_any(
            "skill_file_insert", (uuid7_str(), int(skill_id), path[:_FILE_PATH_MAX], int(size)))


def _ensure_official_skill_files(skill_id: int, scope: str, content: str) -> list[tuple[str, int]]:
    """文件清单缺失时按技能来源惰性补建（官方技能为部署内置目录，无 zip 可解析）。"""
    rows = _rt_skills.read_all_any("skill_files_by_skill", (int(skill_id),))
    if rows:
        return [(r[0], int(r[1] or 0)) for r in rows]
    if scope != "official":
        return []
    entries: list[tuple[str, int]] = []
    try:
        from src.kernel.config import resolve_official_skill_path
        row = _rt_skills.read_one_any("skill_name_path", (int(skill_id),))
        name = row[0] if row else ""
        skill_md = resolve_official_skill_path(name) if name else None
        if skill_md and os.path.exists(skill_md):
            base = os.path.dirname(skill_md)
            for root, _dirs, files in os.walk(base):
                for fn in files:
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, base).replace(os.sep, "/")
                    entries.append((rel, int(os.path.getsize(full))))
            entries.sort(key=lambda x: x[0])
    except Exception:
        entries = []
    if entries:
        _replace_skill_files(skill_id, entries)
    return entries


def _ensure_official_skill_version(skill_id: int, content: str, created_at: str) -> str:
    """官方技能无版本行时补 1.0.0（存量迁移口径，不伪造中间版本）。"""
    latest = _rt_skills.read_one_any("skill_version_latest", (int(skill_id),))
    if latest:
        return latest[0] or "1.0.0"
    version = ""
    try:
        fm = parse_skill_frontmatter_from_text(content or "")
        version = (fm.get("version") or "").strip()
    except Exception:
        version = ""
    version = version or "1.0.0"
    try:
        from src.kernel.id_gen import uuid7_str
        _rt_skills.exec_any("skill_version_insert", (
            uuid7_str(), int(skill_id), version, "存量官方技能版本补录",
            created_at or "", "system"))
    except Exception:
        pass
    return version


def parse_skill_frontmatter_from_text(text: str) -> dict:
    """从 SKILL.md 文本解析 front matter 的 key/value（详情页版本兜底）。"""
    import re as _re
    m = _re.match(r"^---\s*\n([\s\S]*?)\n---", text or "")
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _ensure_skill_access(skill_id: int, user: dict) -> tuple:
    """校验技能存在与可见性，返回 skill_meta_row（同名/归属口径与 by-id 一致）。"""
    row = _rt_skills.read_one_any("skill_meta_row", (int(skill_id),))
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})
    scope, owner_id = row[1] or "", row[2] or ""
    if scope == "user" and owner_id != str(user.get("id") or ""):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该技能"})
    if scope == "repo":
        try:
            from src.domains.mgmt.user.db import get_user_repos
            repo_keys = {r["repo_key"] for r in (get_user_repos(user.get("id")) or [])}
        except Exception:
            repo_keys = set()
        if owner_id not in repo_keys:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该技能"})
    return row


def _related_skills(skill_id: int, scope: str, agent_type: str, tags: list[str],
                    user: dict) -> list[dict]:
    """相关技能：同标签优先，无交集降级同 agent_type（D5）。"""
    rows = _rt_skills.read_all_any("skills_related_pool", (int(skill_id),))
    if not rows:
        return []
    repo_keys: set[str] = set()
    if user.get("id"):
        try:
            from src.domains.mgmt.user.db import get_user_repos
            repo_keys = {r["repo_key"] for r in (get_user_repos(user["id"]) or [])}
        except Exception:
            repo_keys = set()
    # tags 入参可能是字符串列表（详情 payload）或 dict 列表（parse_tags 产物），
    # 统一取「标签名」做集合交集（dict 不可 hash，混用必抛 TypeError）
    def _tag_names(items) -> set:
        out = set()
        for t in items or []:
            if isinstance(t, dict):
                n = str(t.get("name") or "").strip()
                if n:
                    out.add(n)
            elif t:
                out.add(str(t))
        return out

    my_tags = _tag_names(tags)
    ranked = []
    for r in rows:
        rid, name, disp, desc, atype, rscope, owner, raw_tags = r[0], r[1], r[2], r[3], r[4], r[5], r[6] or "", r[7] or ""
        if rscope == "user" and owner != str(user.get("id") or ""):
            continue
        if rscope == "repo" and owner not in repo_keys:
            continue
        rtags = parse_tags(raw_tags, limit=SKILL_MAX_TAGS)
        shared = len(my_tags & _tag_names(rtags))
        same_agent = 1 if atype == agent_type else 0
        if my_tags and shared == 0:
            continue
        if not my_tags and not same_agent:
            continue
        ranked.append((shared, same_agent, int(r[8] or 0), {
            "id": rid,
            "name": name,
            "display_name": disp or name,
            "description": desc or "",
            "summary": _clean_summary(r[10] or desc or ""),
            "agent_type": atype or "",
            "scope": rscope or "",
            "tags": rtags,
            "like_count": int(r[8] or 0),
            "category_id": int(r[9] or 0),
        }))
    ranked.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    return [x[3] for x in ranked[:_RELATED_SKILL_COUNT]]


def _decorate_skill_detail(payload: dict, user: dict, scope: str, content: str) -> dict:
    """by-id 返回补齐 D2/D3/D5/D6/D9/D11 字段（单一出口，两个存储分支共用）。"""
    sid = int(payload.get("id") or 0)
    if not sid:
        return payload
    extra = _rt_skills.read_one_any("skill_detail_extra_by_id", (sid,))
    summary = (extra[0] if extra else "") or ""
    payload["download_count"] = int((extra[1] if extra else 0) or 0)
    payload["created_at"] = (extra[2] if extra else "") or ""
    payload["updated_at"] = (extra[3] if extra else "") or ""
    payload["created_by"] = (extra[4] if extra else "") or ""
    payload["updated_by"] = (extra[5] if extra else "") or ""
    payload["summary"] = _clean_summary(summary or payload.get("description") or "")
    # Issue #3855：前端编辑入口与后端鉴权同源——官方技能仅管理员可改名。
    payload["can_edit_display_name"] = (
        scope != "official") or _is_official_skill_admin(user)
    # 概览 Tab 技能介绍人工改写（issue：概览可编辑）——JSON 解析失败按空处理
    try:
        _ov = _rt_skills.read_one_any("skill_intro_override_by_id", (sid,))
        payload["intro_override"] = json.loads((_ov[0] if _ov else "") or "{}") or {}
    except Exception:
        payload["intro_override"] = {}

    if scope == "official":
        payload["version"] = _ensure_official_skill_version(
            sid, content, payload.get("created_at") or "")
    else:
        latest = _rt_skills.read_one_any("skill_version_latest", (sid,))
        payload["version"] = (latest[0] if latest else "") or _version_from_content(content)
    versions_rows = _rt_skills.read_all_any("skill_versions_by_skill", (sid,))
    payload["versions"] = [
        {"version": r[0] or "", "uploaded_at": r[1] or "",
         "uploaded_by": r[2] or "", "change_note": r[3] or "",
         "is_latest": i == 0}
        for i, r in enumerate(versions_rows)
    ]
    files = _ensure_official_skill_files(sid, scope, content)
    payload["files"] = [{"path": p, "size": int(s)} for p, s in files]
    payload["file_count"] = len(files)
    try:
        payload["comment_count"] = int(
            (_rt_skills.read_one_any("skill_comment_count", (sid,)) or [0])[0] or 0)
    except Exception:
        payload["comment_count"] = 0
    payload["related"] = _related_skills(
        sid, scope, payload.get("agent_type") or "", list(payload.get("tags") or []), user)
    return payload


def _version_from_content(content: str) -> str:
    m = (content or "").split("---")
    if len(m) >= 3:
        try:
            return (parse_skill_frontmatter_from_text(content).get("version") or "").strip() or "—"
        except Exception:
            return "—"
    return "—"


def _validate_repo_param(repo: str) -> None:
    """校验 repo 查询参数：空 / owner/repo / <provider>:owner/repo 合规，其余 400。

    Issue #1790：不合规（裸仓库名、残留冒号、缺失 owner、多余路径段）必须显式报错，
    不能静默回退到官方列表——否则前端把错参数当成功，看到的列表是错的。
    平台前缀不硬编码：新平台接入后 <provider> 仍能通过，只校验结构。
    """
    if not repo:
        return

    def _bad(reason: str):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"repo 参数不合规（{reason}）: {repo!r}"})

    _rest = repo
    if ":" in repo:
        _prefix, _rest = repo.split(":", 1)
        if not _prefix or not _prefix.strip() or _prefix != _prefix.strip():
            _bad("平台前缀为空或不合法")
    if not _rest or _rest != _rest.strip():
        _bad("owner/repo 为空或含首尾空格")
    _segs = _rest.split("/")
    if len(_segs) != 2 or not _segs[0] or not _segs[1]:
        _bad("需为 owner/repo 两段且都非空")
    if any(c.isspace() for c in _rest):
        _bad("含空白字符")


@router.get("", operation_id="skills_list", summary="List skills", description="Lists skills: official visible to all, user visible to owner, repo visible to members. Supports scope/agent_type/sub_scene filtering, sort and paging.")
def list_skills(user: dict = Depends(require_org_member), repo: str = "", scope: str = "",                agent_type: str = "", category_id: int = 0,
                cat_l1_id: int = 0, cat_l2_id: int = 0,
                q: str = "", sort: str = "", page: int = 1, page_size: int = 0):
    """返回技能列表：official 所有用户可见；user 本人可见；repo 指定仓库可见。
    scope 参数：official/repo/user 按维度过滤；all = 不限 scope（官方+个人+所有仓库）；不传 = 返回全部（兼容旧逻辑）。
    agent_type 参数：按 agent_type 过滤（vod/issue/coding/qa/sre/chat）。
    category_id 参数：按分类 ID 过滤（Issue #179）。
    q 参数：搜索词，按 skill_name / display_name / description 模糊匹配。
    sort 参数：latest（默认顺序）/ hot（按 like_count 降序）。
    page/page_size：分页参数，page_size=0 时不分页（返回全部，兼容旧逻辑）。
    扩展返回字段：is_featured, like_count, liked_by_me, scene_count, repo_count, scenes, category_id, category_name
    """
    from src.kernel.session_manager import db, db_lock
    from src.kernel.repo_key import to_repo_key
    from src.domains.mgmt.user.db import (batch_skill_liked, get_skill_usage_stats,
                             batch_skill_scenes, list_skill_categories,
                             list_category_dict, category_path_from_map)

    # Issue #1790（#1790/#1786 移植）：区分「未传 repo / 合规 owner/repo / 不合规」三态，
    # 不合规（裸仓库名/非法前缀/残留冒号）直接 400，不再静默回退官方列表。
    _validate_repo_param(repo)
    # repo 参数可能是 fullName（xiaowu2119/hello-skill），转成 repo_key（gitcode:xiaowu2119/hello-skill）
    repo_key = _resolve_repo_key(repo) if repo else ""

    # 可见范围/搜索/排序/分页都交给 owner 域（skill 归运行域）
    _org_id = user.get("org_id", "")
    if scope == "repo" and not repo:
        return {"skills": [], "total": 0, "page": page, "page_size": page_size}

    use_paging = bool(page_size and page_size > 0)

    skills = []
    skill_ids: list[int] = []
    # 批量查分类名称（避免 N+1）— 在 db_lock 外查，避免 SQLite cursor 复用冲突。
    # QA（#3648）：schema 缺列/缺表时分类名与分类树降级为空可接受，但禁止
    # 裸 except 静默吞掉（Issue 事故形态之一就是「列表为空且无报错」），
    # 必须 log.exception 留痕（堆栈进日志，可定位到具体缺失列/表）。
    from src.kernel.config import log as _cat_log
    cat_map = {}
    try:
        for cat in list_skill_categories():
            cat_map[cat["id"]] = cat["name"]
    except Exception:
        _cat_log.exception("list_skills: list_skill_categories failed (分类名降级为空)")
    try:
        _cat_dict_map = {c["id"]: c for c in list_category_dict("skill")}
    except Exception:
        _cat_dict_map = {}
        _cat_log.exception("list_skills: list_category_dict failed (分类路径降级为空)")
    total = 0
    try:
        total, _skill_rows = _rt_skills.skill_search(
            scope=scope, user_id=user.get("id"), repo_key=repo_key, org_id=_org_id,
            q=q, agent_type=agent_type, category_id=category_id or 0, sort=sort,
            page=page, page_size=page_size if use_paging else 0,
            cat_l1_id=cat_l1_id or 0, cat_l2_id=cat_l2_id or 0)
        for r in _skill_rows:
            cat_id = r[10] or 0
            l1_id = r[11] if len(r) > 11 else 0
            l2_id = r[12] if len(r) > 12 else 0
            raw_tags = r[13] if len(r) > 13 else ""
            raw_summary = r[14] if len(r) > 14 else ""
            skills.append({
                "id": r[0],
                "name": r[1],
                "display_name": r[2] or r[1],
                "description": r[3] or "",
                "summary": _clean_summary(raw_summary or r[3] or ""),
                "agent_type": r[4] or "",
                "type": "official" if r[5] == "official" else "repo" if r[5] == "repo" else "user-uploaded",
                "scope": r[5] or "",
                "is_featured": r[7] or 0,
                "like_count": r[8] or 0,
                "category_id": cat_id,
                "category_name": cat_map.get(cat_id, ""),
                "cat_l1_id": l1_id or 0,
                "cat_l2_id": l2_id or 0,
                "category": category_path_from_map(_cat_dict_map, l1_id, l2_id),
                "tags": parse_tags(raw_tags, limit=SKILL_MAX_TAGS),
            })
            skill_ids.append(r[0])
    except Exception as _search_exc:
        # Issue #3648：schema 故障（如 skill 缺 cat_l1_id）不得伪装成 200 空列表。
        # 记录完整堆栈并显式 5xx；前端/运维能据此定位，而不是被误导成「没有技能」。
        from src.kernel.config import log
        log.exception("list_skills: skill_search failed (schema/query error): %s",
                      _search_exc)
        raise HTTPException(status_code=503, detail={
            "code": 503,
            "message": "技能列表查询失败（数据库 schema 或查询异常），请查看服务端日志",
        }) from _search_exc

    # 批量查 liked_by_me
    liked_map = batch_skill_liked(str(user.get("id") or ""), skill_ids) if skill_ids else {}
    # 批量查使用统计（按本页 skill_ids 过滤）
    usage_stats = get_skill_usage_stats(skill_ids) if skill_ids else {}
    # 批量查所属场景
    scenes_map = batch_skill_scenes(skill_ids) if skill_ids else {}

    # 补充扩展字段
    for s in skills:
        s["liked_by_me"] = liked_map.get(s["id"], False)
        stats = usage_stats.get(s["id"], {})
        s["scene_count"] = stats.get("scene_count", 0)
        s["repo_count"] = stats.get("repo_count", 0)
        s["scenes"] = scenes_map.get(s["id"], [])

    if use_paging:
        return {"skills": skills, "total": total, "page": page, "page_size": page_size}
    return {"skills": skills}

@router.post("/{skill_id}/like", operation_id="skills_like_toggle", summary="Toggle skill like", description="Toggles a skill like.")
def toggle_skill_like(skill_id: int, user: dict = Depends(require_no_session_token)):
    """点赞 toggle：已赞→取消，未赞→点赞"""
    from src.kernel.session_manager import db, db_lock
    from src.domains.mgmt.user.db import toggle_skill_like as _toggle

    # 校验技能存在
    if not _rt_skills.skill_exists_active(skill_id):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    liked, like_count = _toggle(str(user.get("id") or ""), skill_id)
    return {"liked": liked, "like_count": like_count}

@router.get("/featured", operation_id="skills_featured", summary="List featured skills", description="Featured skills: is_featured=1 official skills ordered by like_count desc (default limit 5).")
def list_featured_skills(user: dict = Depends(require_no_session_token), limit: int = 0):
    """精选技能：is_featured=1 的 official 技能，按 like_count 降序。limit=0 时默认 5"""
    from src.domains.mgmt.user.db import (get_featured_skills, batch_skill_liked,
                             get_skill_usage_stats, batch_skill_scenes,
                             list_skill_categories)

    skills = get_featured_skills(limit=limit if limit > 0 else 5)
    if not skills:
        return {"skills": []}

    skill_ids = [s["id"] for s in skills]
    # 补充 liked_by_me
    liked_map = batch_skill_liked(str(user.get("id") or ""), skill_ids)
    # 补充使用统计和所属场景（按 skill_id 关联）
    usage_stats = get_skill_usage_stats(skill_ids) if skill_ids else {}
    scenes_map = batch_skill_scenes(skill_ids) if skill_ids else {}
    # 批量查分类名称
    cat_map = {}
    try:
        for cat in list_skill_categories():
            cat_map[cat["id"]] = cat["name"]
    except Exception:
        pass
    for s in skills:
        s["name"] = s["skill_name"]  # 前端用 name 字段
        s["scope"] = "official"
        s["liked_by_me"] = liked_map.get(s["id"], False)
        stats = usage_stats.get(s["id"], {})
        s["scene_count"] = stats.get("scene_count", 0)
        s["repo_count"] = stats.get("repo_count", 0)
        s["scenes"] = scenes_map.get(s["id"], [])
        s["category_name"] = cat_map.get(s.get("category_id", 0), "")
        s["cat_l1_id"] = s.get("cat_l1_id", 0)
        s["cat_l2_id"] = s.get("cat_l2_id", 0)
        s["category"] = _skill_category_payload(s["cat_l1_id"], s["cat_l2_id"])
        s["tags"] = parse_tags(s.get("tags", ""), limit=SKILL_MAX_TAGS)
        s["summary"] = _clean_summary(s.get("summary") or s.get("description") or "")
    # 精选技能按 admin 管理台设置的顺序排列
    if skills:
        try:
            from src.kernel.featured_order import get_featured_order
            _order = get_featured_order("skill")
            if _order:
                _order_map = {sid: i for i, sid in enumerate(_order)}
                skills.sort(key=lambda x: _order_map.get(x["id"], 9999))
        except Exception:
            pass
    return {"skills": skills}

@router.get("/agent-stats", operation_id="skills_agent_stats", summary="Skill stats (total + by agent_type)", description="Skill total + per agent_type counts; scope/repo filters match list_skills. User-only (web UI stats).")
def get_agent_stats(user: dict = Depends(require_no_session_token), scope: str = "", repo: str = ""):
    """返回技能总数 + 每个 agent_type 的技能数量。

    scope/repo 参数与 list_skills 一致，用于按维度统计。
    分类列表由 /api/skills/categories 独立获取，不在本接口返回。

    user_only：仅 Web UI / 用户级调用方消费（skills.js 页面统计），Agent 执行期无此场景；
    且 tk_ 会话 token 的 user_id 在仓库身份下为 0，scope=user 过滤 owner_id=0 亦无可达数据——故禁 session。
    """
    from src.kernel.session_manager import db, db_lock

    repo_key = _resolve_repo_key(repo) if repo else ""
    if scope == "repo" and not repo:
        return {"total": 0, "agents": []}

    agents = []
    total = 0
    # 可见范围条件与列表接口同一套（owner 域）
    try:
        for r in _rt_skills.skill_agent_counts(scope=scope, user_id=user.get("id"),
                                               repo_key=repo_key):
            agents.append({"agent_type": r[0] or "", "count": r[1]})
            total += r[1]
    except Exception:
        pass
    return {"total": total, "agents": agents}

@router.get("/stats", operation_id="skills_stats", summary="Skill usage stats (scene/repo aggregation)", description="Skill usage stats aggregated from scene_skill_ref: {skill_name: {scene_count, repo_count}}.")
def get_skill_stats(user: dict = Depends(require_no_session_token)):
    """技能使用统计（通过 scene_skill_ref 关联表 SQL 聚合）。

    返回 {skill_name: {scene_count: N, repo_count: N}}。
    """
    from src.domains.mgmt.user.db import get_skill_usage_stats
    return {"stats": get_skill_usage_stats()}

@router.get("/categories", operation_id="skills_categories", summary="List skill categories",
            description="Lists skill categories (Issue #179 skill-sync-category).")
def list_categories(user: dict = Depends(require_no_session_token)):
    """返回技能分类列表（Issue #179）；附统一分类树（Issue #2761）。"""
    from src.domains.mgmt.user.db import list_skill_categories, list_category_tree
    return {"categories": list_skill_categories(), "tree": list_category_tree("skill")}

@router.get("/tag-suggest", operation_id="skills_tag_suggest",
            summary="Suggest skill tags", description="Rule-based tag candidates (max 3) for skill name/description.")
def suggest_skill_tags(name: str = "", description: str = "",
                       user: dict = Depends(require_no_session_token)):
    return {"tags": _skill_tag_suggestions(name, description)}

@router.get("/search", operation_id="skills_search", summary="Search skills",
            description="Skill lookup for a task description: exact skill_name match first, then keyword match on description/display_name/agent_type.")
def search_skills(q: str = "", user: dict = Depends(require_no_session_token)):
    """技能查找：根据任务描述推荐相关技能。

    先按 skill_name 精确匹配，匹配到则按名称过滤；
    匹配不到则按 description + display_name + agent_type 做关键词匹配。
    不调 LLM（第一期简单实现）——分词后匹配技能描述。
    返回 [{skill_id, name, display_name, description, category_id, category_name, match_reason}]
    """
    from src.kernel.session_manager import db, db_lock
    from src.domains.mgmt.user.db import list_skill_categories

    q = (q or "").strip()
    if not q:
        return {"skills": []}

    # 批量查分类名称（在 db_lock 外查，避免 SQLite cursor 复用）
    cat_map = {}
    try:
        for cat in list_skill_categories():
            cat_map[cat["id"]] = cat["name"]
    except Exception:
        pass

    # 1. 先按 skill_name 精确匹配
    with db_lock:
        c = db.cursor()
        from src.kernel.domain_read import runtime_lib as _ru_lib_4e3919_f
        _ru_lib_4e3919 = _ru_lib_4e3919_f(foreign_only=True)
        if _ru_lib_4e3919 is not None:
            exact_rows = _ru_lib_4e3919.read_all("skill_4e3919", (q, q),)
        else:
            exact_rows = _rt_skills.read_all_any("skill_official_exact", (q, q))

    if exact_rows:
        return {"skills": [
            {"skill_id": r[0], "name": r[1], "display_name": r[2] or r[1],
             "description": r[3] or "", "category_id": r[5] or 0,
             "category_name": cat_map.get(r[5] or 0, ""), "match_reason": "名称精确匹配"}
            for r in exact_rows
        ]}

    # 2. 关键词匹配：分词后匹配 description + display_name + agent_type
    # 简单分词：按空格/中文常见分隔符拆分，过滤太短的词
    import re as _re
    keywords = [w for w in _re.split(r'[\s,，、/;；]+', q) if len(w) >= 2]
    if not keywords:
        # 整个 q 作为单个关键词
        keywords = [q]

    with db_lock:
        c = db.cursor()
        from src.kernel.domain_read import runtime_lib as _ru_lib_ded9f4_f
        _ru_lib_ded9f4 = _ru_lib_ded9f4_f(foreign_only=True)
        if _ru_lib_ded9f4 is not None:
            all_skills = _ru_lib_ded9f4.read_all("skill_ded9f4", ())
        else:
            all_skills = _rt_skills.read_all_any("skill_official_all")

    results = []
    for r in all_skills:
        skill_id, name, display_name, desc, agent_type, cat_id = r
        display_name = display_name or name
        desc = desc or ""
        # 计算命中关键词
        hit_keywords = []
        searchable = f"{name} {display_name} {desc} {agent_type}".lower()
        for kw in keywords:
            if kw.lower() in searchable:
                hit_keywords.append(kw)
        if hit_keywords:
            match_reason = f"匹配关键词：{', '.join(hit_keywords)}"
            results.append({
                "skill_id": skill_id, "name": name, "display_name": display_name,
                "description": desc, "category_id": cat_id or 0,
                "category_name": cat_map.get(cat_id or 0, ""),
                "match_reason": match_reason,
                "_hit_count": len(hit_keywords),
            })

    # 按命中数降序
    results.sort(key=lambda x: x["_hit_count"], reverse=True)
    for r in results:
        del r["_hit_count"]

    return {"skills": results}

@router.get("/template")
def download_skill_template():
    """下载技能模板 ZIP（skills/_template 目录打包）。免鉴权（token 通过 query 传递）。"""
    from src.kernel.config import SKILLS_DIR

    template_dir = os.path.join(SKILLS_DIR, "_template")
    if not os.path.isdir(template_dir):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能模板未找到"})

    import io as _io
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(template_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, template_dir)
                zf.write(full, rel)
    buf.seek(0)
    # ===== OBS 分支（N8）：打包 → 传 OBS → 返回临时签名 URL（前端点击下载时动态获取）=====
    from src.domains.runtime.public import get_storage
    storage = get_storage()
    if storage is not None:
        key = "skills/template/skill-template.zip"
        storage.save_bytes(key, buf.getvalue())
        url = storage.temporary_url(key)
        return {"url": url, "expires_in": storage.default_expires}
    # ===== local 分支（原逻辑不变）=====
    from fastapi.responses import Response
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="skill-template.zip"'},
    )

@router.get("/by-id/{skill_id}/download", operation_id="skills_download", summary="Download skill ZIP", description="Downloads a skill package zip with a short-lived signed URL; OBS mode returns {url, expires_in}.")
def download_skill_zip_by_id(skill_id: int, user: dict = Depends(require_no_session_token),
                                   repo: str = ""):
    """下载技能包 zip（点击时动态签发，见 obs-skill-storage-design.md N8 下载模式）。

    按技能 id 精确查询（同名多 scope 记录无歧义）；OBS 模式返回 {url, expires_in}
    临时签名 URL（300s，不落库），浏览器直连 OBS 下载；local 模式返回 zip 文件流。
    官方技能（scope=official）为部署内置物，未转集群不提供下载。
    """
    from src.kernel.session_manager import db, db_lock
    from src.kernel.repo_key import to_repo_key, from_repo_key
    repo_key = _resolve_repo_key(repo) if repo else ""
    row = _rt_skills.skill_download_row(skill_id, user.get("id"), repo_key)
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能不存在"})
    skill_id, skill_path, scope, owner_id, name = row

    from src.domains.runtime.public import get_skill_storage
    storage = get_skill_storage()
    if storage is not None:
        from src.domains.mgmt.user.db import get_skill_object_key
        object_key = get_skill_object_key(skill_id)
        if object_key:
            url = storage.temporary_url(object_key)
            if url:  # OBS：签名 URL
                try:
                    _rt_skills.exec_any("skill_download_inc", (int(skill_id),))
                except Exception:
                    pass
                return {"url": url, "expires_in": getattr(storage, "default_expires", 300)}
            # 本地模式：直接回 zip 字节流
            data = storage.read_bytes(object_key)
            if data is not None:
                try:
                    _rt_skills.exec_any("skill_download_inc", (int(skill_id),))
                except Exception:
                    pass
                return Response(content=data, media_type="application/zip",
                                headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})
    # 官方技能未转集群模式：无对象存储对象，不提供下载
    if scope == "official":
        raise HTTPException(status_code=400, detail={"code": 400, "message": "官方技能为部署内置，未转集群模式不提供下载"})

    raise HTTPException(status_code=503, detail={"code": 503, "message": "OBS 存储未启用且技能对象缺失，下载失败。请检查服务端存储配置后重试。"})

@router.get("/by-id/{skill_id}", operation_id="skills_detail", summary="Skill detail (incl. SKILL.md)", description="Skill detail including SKILL.md. Access: official public, user=owner, repo=managed by caller.")
def get_skill_detail_by_id(skill_id: str, user: dict = Depends(get_current_user_or_api_key_or_admin),
                                 repo: str = ""):
    """返回技能详情（含 SKILL.md 内容）——支持技能 id 或技能名（Issue #630）。

    - 纯数字：按 id 精确查询；
    - 非数字：按 skill_name 解析（本人 user 技能 > 可见 repo 技能 > official），
      未知名称返回 404 而非 422，避免把「名称当 id 解析失败」暴露成参数错误。
    查到后按 scope 校验归属权限：official 可见；user 需本人；repo 需当前用户托管。
    """
    # Issue #630：name→sid 解析（复用 resolve_skill_id 的 user scope 逻辑）
    _raw = str(skill_id or "").strip()
    if not _raw.isdigit():
        from src.domains.mgmt.user.db import resolve_skill_id
        _repo_key = _resolve_repo_key(repo) if repo else ""
        _sid = resolve_skill_id(_raw, repo_key=_repo_key, user_id=str(user.get("id", "")))
        if not _sid:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})
        skill_id = _sid
    else:
        skill_id = int(_raw)

    from src.kernel.session_manager import db, db_lock
    from src.kernel.config import SKILLS_DIR

    _raw = str(skill_id or "").strip()
    if _raw.isdigit():
        row = _rt_skills.read_one_any("skill_detail_by_id", (int(_raw),))
    else:
        row = _rt_skills.read_one_any("skill_detail_by_name", (_raw, str(user.get("id") or "")))

    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    scope = row[5]
    owner_id = row[6] or ""
    is_featured = row[8] or 0
    like_count = row[9] or 0
    category_id = row[11] or 0

    # 查分类名称
    from src.domains.mgmt.user.db import get_skill_category
    cat = get_skill_category(category_id) if category_id else None
    category_name = cat["name"] if cat else ""
    l1_id = row[12] if len(row) > 12 else 0
    l2_id = row[13] if len(row) > 13 else 0
    raw_tags = row[14] if len(row) > 14 else ""
    category_payload = _skill_category_payload(l1_id, l2_id)
    tags = parse_tags(raw_tags, limit=SKILL_MAX_TAGS)

    # 归属校验
    # Issue #3855：管理台 JWT（token_type='admin'）能力仅限官方技能改名/详情；
    # admin_accounts.id 与 grape_accounts.id 是两套独立序列，若放行到 user/repo
    # 分支会按撞号 id 读到他人私有技能（confused deputy），故非 official 一律拒绝。
    if str(user.get("token_type") or "") == "admin" and scope != "official":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "管理台账号仅可访问官方技能"})
    if scope == "user" and owner_id != str(user["id"]):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该技能"})
    if scope == "repo":
        # 从后端查当前用户托管的仓库列表，校验 skill.owner_id 在列表里
        from src.domains.mgmt.user.db import get_user_repos
        user_repo_keys = {r["repo_key"] for r in (get_user_repos(user["id"]) if user.get("id") else [])}
        if owner_id not in user_repo_keys:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该技能"})

    # 扩展字段：liked_by_me + 所属场景
    from src.domains.mgmt.user.db import is_skill_liked, get_skill_scenes
    liked_by_me = is_skill_liked(str(user.get("id") or ""), skill_id)
    scenes = get_skill_scenes(row[7])  # row[7] = skill.id

    skill_dir = row[4] or row[0]
    current_id = row[7]
    from src.kernel.config import SKILLS_DIR  # 本地兜底分支按官方技能根解析
    # ===== 存储分支（N5）：两级缓存 + 内存解压提取 + zip_url 临时地址/local 直接读 =====
    from src.domains.runtime.public import get_skill_storage
    storage = get_skill_storage()
    object_key = ""
    if storage is not None:
        from src.domains.mgmt.user.db import get_skill_object_key
        object_key = get_skill_object_key(current_id)  # 官方技能转集群后 skill_path 也是对象键
    if object_key:
        from src.domains.runtime.public import skill_md_content
        content = skill_md_content(object_key, row[0]) or ""
        zip_url = ""
        try:
            zip_url = storage.temporary_url(object_key)  # 用户点击下载时动态签发
        except Exception:
            zip_url = ""
        # 文件清单缺失时从对象包解析（D9 存量官方技能/历史上传）
        try:
            if not _rt_skills.read_one_any("skill_file_count", (current_id,)) or \
                    int((_rt_skills.read_one_any("skill_file_count", (current_id,)) or [0])[0] or 0) == 0:
                _raw_zip = storage.read_bytes(object_key)
                _entries = _list_zip_entries(_raw_zip or b"")
                if _entries:
                    _replace_skill_files(int(current_id), _entries)
        except Exception:
            pass
        return _decorate_skill_detail({
            "id": current_id,
            "name": row[0], "display_name": row[1] or row[0], "description": row[2] or "",
            "agent_type": row[3] or "",
            "type": "official" if scope == "official" else "repo" if scope == "repo" else "user-uploaded",
            "scope": scope or "",
            "content": content,
            "zip_url": zip_url,
            "is_obs": True,  # OBS/集群模式（skill_path 为对象键）
            "skill_path": skill_dir or "",
            "is_featured": is_featured,
            "like_count": like_count,
            "liked_by_me": liked_by_me,
            "scenes": scenes,
            "category_id": category_id,
            "category_name": category_name,
            "cat_l1_id": l1_id or 0,
            "cat_l2_id": l2_id or 0,
            "category": category_payload,
            "tags": tags,
        }, user, scope, content)
    # ===== local 分支 =====
    # user/repo 技能：均衡负载环境下 local 存储会导致文件只存在单机，OBS 不可用时直接报 503
    if scope in ("user", "repo"):
        raise HTTPException(status_code=503, detail={"code": 503, "message": "OBS 存储未启用，技能详情获取失败。请检查服务端 OBS 配置后重试。"})
    # official 技能：目录在代码仓库中，两台机器都有，保留磁盘读取
    from src.kernel.config import resolve_official_skill_path
    _r = resolve_official_skill_path(row[0])
    if _r:
        skill_path = _r
    else:
        # fallback: skill_dir 可能是 3 层路径，尝试去掉中间层
        _parts = skill_dir.split("/")
        if len(_parts) >= 3:
            skill_path = os.path.join(SKILLS_DIR, _parts[0], _parts[-1], "SKILL.md")
        else:
            skill_path = os.path.join(SKILLS_DIR, skill_dir, "SKILL.md")
    content = ""
    if os.path.exists(skill_path):
        with open(skill_path, encoding="utf-8") as f:
            content = f.read()
    return _decorate_skill_detail({
        "id": current_id,
        "name": row[0], "display_name": row[1] or row[0], "description": row[2] or "",
        "agent_type": row[3] or "",
        "type": "official" if scope == "official" else "repo" if scope == "repo" else "user-uploaded",
        "scope": scope or "",
        "content": content,
        "is_obs": False,
        "skill_path": skill_dir or "",
        "is_featured": is_featured,
        "like_count": like_count,
        "liked_by_me": liked_by_me,
        "scenes": scenes,
        "category_id": category_id,
        "category_name": category_name,
        "cat_l1_id": l1_id or 0,
        "cat_l2_id": l2_id or 0,
        "category": category_payload,
        "tags": tags,
    }, user, scope, content)

@router.get("/by-id/{skill_id}/versions", operation_id="skills_versions", summary="Skill version history", description="Returns the skill version history (latest first) for the detail page.")
def list_skill_versions(skill_id: int, user: dict = Depends(get_current_user_or_api_key)):
    """技能版本历史（D6）。"""
    _ensure_skill_access(skill_id, user)
    rows = _rt_skills.read_all_any("skill_versions_by_skill", (int(skill_id),))
    return {"versions": [
        {"version": r[0] or "", "uploaded_at": r[1] or "", "uploaded_by": r[2] or "",
         "change_note": r[3] or "", "is_latest": i == 0}
        for i, r in enumerate(rows)
    ]}


@router.get("/by-id/{skill_id}/files", operation_id="skills_files", summary="Skill package file tree", description="Returns the skill package file list with sizes for the detail page resources tab.")
def list_skill_files(skill_id: int, user: dict = Depends(get_current_user_or_api_key)):
    """技能包文件清单（D9）。"""
    row = _ensure_skill_access(skill_id, user)
    scope = row[1] or ""
    content = ""
    if scope == "official":
        try:
            from src.kernel.config import resolve_official_skill_path
            path = resolve_official_skill_path(row[0])
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    content = fh.read()
        except Exception:
            content = ""
    entries = _ensure_official_skill_files(int(skill_id), scope, content)
    return {"files": [{"path": p, "size": int(s)} for p, s in entries],
            "file_count": len(entries)}


@router.get("/by-id/{skill_id}/related", operation_id="skills_related", summary="Related skills", description="Related skills: shared tags first, falls back to same agent_type.")
def list_related_skills(skill_id: int, user: dict = Depends(get_current_user_or_api_key)):
    """相关技能（D5）。"""
    row = _ensure_skill_access(skill_id, user)
    raw_tags = row[7] if len(row) > 7 else ""
    tags = parse_tags(raw_tags, limit=SKILL_MAX_TAGS)
    skill = _rt_skills.read_one_any("skill_detail_by_id", (int(skill_id),))
    agent_type = skill[3] if skill else ""
    return {"skills": _related_skills(int(skill_id), row[1] or "", agent_type or "", tags, user)}


@router.get("/by-id/{skill_id}/comments", operation_id="skills_comments_list", summary="List skill comments", description="Paged user comments for a skill (newest first).")
def list_skill_comments(skill_id: int, page: int = 1, page_size: int = 20,
                        user: dict = Depends(get_current_user_or_api_key)):
    """技能用户评论（D11）：默认最新在前，每页 20 条。"""
    _ensure_skill_access(skill_id, user)
    page = max(1, int(page or 1))
    page_size = min(_COMMENT_PAGE_MAX, max(1, int(page_size or _COMMENT_PAGE_DEFAULT)))
    total = int((_rt_skills.read_one_any("skill_comment_count", (int(skill_id),)) or [0])[0] or 0)
    rows = _rt_skills.read_all_any(
        "skill_comments_page", (int(skill_id), page_size, (page - 1) * page_size))
    return {
        "comments": [
            {"id": r[0], "user_id": r[1] or "", "user_name": r[2] or "用户",
             "content": r[3] or "", "created_at": r[4] or ""}
            for r in rows
        ],
        "total": total, "page": page, "page_size": page_size,
    }


@router.post("/by-id/{skill_id}/comments", operation_id="skills_comments_create", summary="Post skill comment", description="Posts a plain-text user comment (login required, max 1000 chars).")
def create_skill_comment(skill_id: int, body: dict,
                         user: dict = Depends(require_no_session_token)):
    """发表技能评论（D11）：纯文本，限 1000 字。"""
    _ensure_skill_access(skill_id, user)
    content = str((body or {}).get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "评论内容不能为空"})
    if len(content) > _COMMENT_MAX:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": f"评论最多 {_COMMENT_MAX} 字"})
    from src.kernel.id_gen import uuid7_str
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cid = uuid7_str()
    name = user.get("username") or user.get("display_name") or "用户"
    _rt_skills.exec_any(
        "skill_comment_insert",
        (cid, int(skill_id), str(user.get("id") or ""), str(name)[:_COMMENT_NAME_MAX], content, now))
    # C3 非查询操作审计：评论发布留痕（detail 只记标识与字数，不落评论原文）
    from src.domains.mgmt.auth import audit as _skill_audit
    _skill_audit.log_audit("skill_comment_posted",
                           user_id=str(user.get("id") or ""),
                           operator_id=str(user.get("id") or ""),
                           detail={"skill_id": int(skill_id), "comment_id": cid,
                                   "chars": len(content)})
    return {"comment": {"id": cid, "user_id": str(user.get("id") or ""),
                        "user_name": name, "content": content, "created_at": now}}


@router.post("/upload", operation_id="skills_upload", summary="Upload skill (user personal / repo)", description="Uploads a skill ZIP. scope=user stores a personal skill; scope=repo requires repo. agent_type is required.")
async def upload_skill(file: UploadFile = File(...), user: dict = Depends(get_current_user_or_api_key),
                       scope: str = Form("user"), repo: str = Form(""), agent_type: str = Form(""),
                       category_id: int = Form(0), cat_l1_id: int = Form(0),
                       cat_l2_id: int = Form(0), tags: str = Form(""),
                       summary: "str | None" = Form(None), change_note: str = Form(""),
                       clear_category: bool = Form(False),
                       display_name: "str | None" = Form(None)):
    """上传技能 ZIP。scope=user 存个人技能；scope=repo 存仓库技能（需指定 repo）。agent_type 必填（技能类型）。category_id 可选（分类ID，Issue #179）。

    Issue #3855：display_name 为新增技能必填名称（表单必传）。为兼容无名称的
    历史 API 调用，缺省（None）时回退源名称；显式传入空串/仅空格/超长/同 scope
    重名均阻断保存。
    """
    # 直接调用（非 FastAPI 注入）时 Form 默认值不是 bool，避免误判为「清空分类」
    if not isinstance(clear_category, bool):
        clear_category = False
    VALID_AGENT_TYPES = {"vod", "issue", "coding", "qa", "sre", "chat", "auto"}
    if scope not in ("user", "repo"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 user 或 repo"})
    if scope == "repo" and not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库技能必须指定 repo"})
    if not agent_type:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "必须选择技能类型（agent_type）"})
    if agent_type not in VALID_AGENT_TYPES:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"技能类型不合法，可选: {', '.join(sorted(VALID_AGENT_TYPES))}"})

    # #219 会话 token（tk_，agent）上传：仅支持 scope=repo，且仓库必须与 token 绑定仓库一致（防越权写）。
    if user.get("auth_type") == "session_token":
        if scope != "repo":
            raise HTTPException(status_code=403, detail={"code": 403, "message": "会话 token 仅支持上传仓库技能（scope=repo）"})
        from src.kernel.repo_key import split_owner_repo
        _tok_owner, _tok_repo = split_owner_repo(user.get("repo_key") or "")
        if not _tok_owner or split_owner_repo(repo) != (_tok_owner, _tok_repo):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权上传到该仓库技能（仓库作用域不匹配）"})

    # 校验 category_id 存在性（0=未分类，>0 时必须在 skill_category 表中存在）
    if category_id > 0:
        from src.domains.mgmt.user.db import get_skill_category
        if not get_skill_category(category_id):
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"分类 ID {category_id} 不存在"})
    if not clear_category and (int(cat_l1_id or 0) or int(cat_l2_id or 0)):
        _normalize_skill_category(cat_l1_id, cat_l2_id)
    tag_items = _validate_skill_tags(tags)
    tags_json = encode_tags(tag_items, limit=SKILL_MAX_TAGS)

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Skill 文件不能超过 10MB"})

    tmp_dir = tempfile.mkdtemp()
    try:
        zip_path = os.path.join(tmp_dir, file.filename or "skill.zip")
        with open(zip_path, "wb") as f:
            f.write(content)

        extract_dir = os.path.join(tmp_dir, "extracted")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                # zip 炸弹防护：解压后总大小上限（见设计文档 §7）
                total = sum((i.file_size or 0) for i in zf.infolist())
                if total > MAX_EXTRACT_BYTES:
                    raise HTTPException(status_code=400, detail={"code": 400, "message": f"ZIP 解压后总大小超过 {MAX_EXTRACT_BYTES // (1024 * 1024)}MB 上限"})
                zf.extractall(extract_dir)
                # Python 3.12+ extractall 不再恢复 Unix 权限位 → 手动恢复
                # （否则后续规范化重打包读 st_mode 拿不到上传时的权限，见 obs-skill-storage-design.md §7）
                for info in zf.infolist():
                    mode = info.external_attr >> 16
                    if mode and not info.is_dir():
                        p = os.path.join(extract_dir, info.filename)
                        if os.path.isfile(p):
                            os.chmod(p, mode & 0o777)
        except HTTPException:
            raise
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "不是有效的 ZIP 文件，请上传 .zip 格式"})

        entries = os.listdir(extract_dir)
        if not entries:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "ZIP 文件为空"})

        # 过滤系统杂项目录（macOS __MACOSX、.DS_Store 等），取第一个真实目录
        real_entries = [e for e in entries if e not in ("__MACOSX", ".DS_Store", ".git") and not e.startswith(".")]
        if not real_entries:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "ZIP 中未找到技能目录"})

        skill_dir = os.path.join(extract_dir, real_entries[0])
        if not os.path.isdir(skill_dir):
            skill_dir = extract_dir

        skill_name = os.path.basename(skill_dir) if os.path.isdir(skill_dir) else real_entries[0]
        errors = validate_custom_skill(skill_dir, skill_name)
        if errors:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "Skill 校验失败", "errors": errors})

        # 从 SKILL.md Front Matter 提取描述（上传后技能列表需要展示）
        fm = parse_skill_frontmatter(skill_dir)
        description = fm.get("description", "") or ""
        summary_value = _resolve_skill_summary(summary, description)
        incoming_version = (fm.get("version") or "").strip()

        import shutil
        from src.kernel.session_manager import db, db_lock
        # ===== OBS/本地存储分支（N4）：规范化重打包 → 存固定 key（原子覆盖）→ DB upsert → 缓存 =====
        from src.kernel.config import log
        from src.domains.runtime.public import get_skill_storage
        storage = get_skill_storage()
        if storage is not None:
            from src.domains.runtime.public import skill_key, zip_cache_put, mem_cache_invalidate
            from src.domains.mgmt.user.db import upsert_skill_obs
            from src.kernel.repo_key import to_repo_key
            owner = _resolve_repo_key(repo, strict=True) if scope == "repo" else str(user["id"])
            db_scope = "repo" if scope == "repo" else "user"
            _existing = _rt_skills.read_one_any(
                "skill_id_by_owner_scope", (skill_name, db_scope, owner))
            _target_display = None
            # 直接调用（非 FastAPI 注入）时缺省值是 Form 对象而非 None，按未传处理。
            if isinstance(display_name, str):
                _target_display = _validate_skill_display_name(
                    display_name, scope=db_scope, owner_id=owner,
                    exclude_id=int(_existing[0]) if _existing else None)
            target_version = (_next_skill_version(int(_existing[0]), incoming_version)
                              if _existing else (incoming_version or "1.0.0"))
            try:
                # 固定 key（v2.4 去版本化）：putObject 同 key 原子覆盖（最后写者胜）；
                # user 不同 owner 段天然隔离，repo 同仓库同名覆盖不限角色
                object_key = skill_key("custom" if db_scope == "user" else db_scope,
                                       owner, skill_name)
                zip_bytes = _normalize_skill_zip(skill_dir, skill_name)
                # Issue #444 收尾（P-B）：save 前留存旧对象字节——upsert 失败（并发
                # 同名 409）时恢复，防止"存储已是新字节、DB hash 停留旧值"的指纹
                # 失真窗口（写序 save_bytes → upsert，最后写者胜；409 输者恢复旧字节
                # 使存储与 DB 记录回到一致）。对齐 convert 409 分支的回滚语义。
                _prev_bytes = storage.read_bytes(object_key)
                storage.save_bytes(object_key, zip_bytes)
                # Issue #444 (P47)：写入 content_hash，使另一进程读时校验可感知覆盖更新
                content_hash = hashlib.sha256(zip_bytes).hexdigest()
                skill_id = upsert_skill_obs(db_scope, owner, skill_name, agent_type,
                                            description, object_key, str(user["id"]),
                                            category_id, content_hash,
                                            display_name=_target_display)
                if not skill_id:
                    if _prev_bytes is not None:
                        storage.save_bytes(object_key, _prev_bytes)   # 恢复旧对象（DB 行未动）
                    raise HTTPException(status_code=409, detail={"code": 409, "message": f"技能 {skill_name} 记录写入失败（同名冲突或并发上传）"})
                if cat_l1_id or cat_l2_id or tags or clear_category:
                    try:
                        # 仅写实际提供的字段：未提供的一侧回填既有值，避免仅改标签时
                        # 以 0 覆盖既有分类（Issue #2761 QA）；clear_category 显式清空。
                        from src.domains.mgmt.routes._category import prepare_category_write
                        _cur = _rt_skills.read_one_any("skill_meta_row", (skill_id,))
                        _cur_l1 = int(_cur[5] or 0) if _cur and len(_cur) > 5 else 0
                        _cur_l2 = int(_cur[6] or 0) if _cur and len(_cur) > 6 else 0
                        _cur_tags = (_cur[7] if _cur and len(_cur) > 7 else "") or ""
                        _target_l1, _target_l2 = prepare_category_write(
                            cat_l1_id, cat_l2_id, _cur_l1, _cur_l2, clear_category)
                        _target_tags = tags_json if tags else _cur_tags
                        _rt_skills.update_skill_category_tags(
                            skill_id, _target_l1, _target_l2, _target_tags)
                    except Exception as _te:  # noqa: BLE001 - 分类/标签写入失败不阻断主流程
                        from src.kernel.config import log as _log
                        _log.warning(f"skill upload: category/tags update failed: {_te}")
                try:
                    _rt_skills.exec_any("skill_summary_update",
                                        (summary_value, int(skill_id)))
                    _record_skill_version(int(skill_id), target_version,
                                          change_note or "上传技能", user)
                    _replace_skill_files(int(skill_id), _list_zip_entries(zip_bytes))
                except HTTPException:
                    raise
                except Exception as _de:  # noqa: BLE001 - 详情附加数据失败不阻断主流程
                    log.warning(f"skill upload: detail metadata write failed: {_de}")
                zip_cache_put(object_key, zip_bytes, content_hash, skill_id or 0)  # put 覆盖同名 key + hash + id 锚点供读时校验
                mem_cache_invalidate(object_key)      # 覆盖后显式失效内存提取缓存（object_key 口径，P49）
                log.info(f"skill uploaded (storage={storage.backend_name()}, key={object_key})")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=502, detail={"code": 502, "message": f"技能上传存储失败: {e}"})
            return {"message": f"技能 {skill_name} 上传成功"}
        # 存储后端不可用（obs-only 且 OBS 未配置）
        raise HTTPException(status_code=503, detail={"code": 503, "message": "OBS 存储未启用且本地存储被禁用，技能上传失败。请检查服务端存储配置后重试。"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"上传失败: {e}"})
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

@router.delete("/by-id/{skill_id}", operation_id="skills_delete", summary="Delete skill", description="Deletes a skill by id after an ownership check. official undeletable; user=owner; repo=managed by caller.")
def delete_skill_by_id(skill_id: int, user: dict = Depends(require_no_session_token),
                        scope: str = "user", repo: str = ""):
    """删除技能——按主键 id 精确查询，查到后做归属校验，再删除。

    权限校验：official 不允许删除；user 需本人；repo 需当前用户托管该仓库。
    """
    from src.kernel.session_manager import db, db_lock
    from src.kernel.repo_key import to_repo_key

    with db_lock:
        c = db.cursor()
        from src.kernel.domain_read import runtime_lib as _ru_lib_f41425_f
        _ru_lib_f41425 = _ru_lib_f41425_f(foreign_only=True)
        if _ru_lib_f41425 is not None:
            row = _ru_lib_f41425.read_one("skill_f41425", (skill_id,))
        else:
            row = _rt_skills.read_one_any("skill_path_owner_scope", (skill_id,))

    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    skill_name = row[0]
    owner_id = row[1] or ""
    skill_scope = row[3] or "user"

    # official 不允许删除
    if skill_scope == "official":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "官方技能不允许删除"})

    # 归属校验
    if skill_scope == "user" and owner_id != str(user["id"]):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权删除该技能"})
    if skill_scope == "repo":
        from src.domains.mgmt.user.db import get_user_repos
        user_repo_keys = {r["repo_key"] for r in (get_user_repos(user["id"]) if user.get("id") else [])}
        if owner_id not in user_repo_keys:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权删除该技能"})

    # 存储对象清理（OBS 或单机本地）
    from src.domains.runtime.public import get_skill_storage
    storage = get_skill_storage()
    object_key = ""
    if storage is not None:
        from src.domains.mgmt.user.db import get_skill_object_key
        try:
            object_key = get_skill_object_key(skill_id)
        except Exception:
            pass

    # 物理删除（按主键 id）
    # P3-5f：skill 在 runtime 域；lib 不可用回落本地 SQL
    from src.kernel.config import log as _log_dsk
    from src.kernel.domain_read import runtime_lib as _rt_lib_dsk
    from src.kernel import cross_write as _xw_dsk
    _dsk_done = False
    _rt_dsk = _rt_lib_dsk(foreign_only=True)
    if _rt_dsk is not None:
        _rt_dsk_ok = _rt_dsk
        _dsk_done = _xw_dsk.submit_if_lib(
            _rt_dsk_ok, "runtime", "skill_delete_by_id", f"skill-del-id:{skill_id}",
            {"skill_id": int(skill_id)},
            lambda: _rt_dsk_ok.delete_skill_by_id(int(skill_id)), _log_dsk)
    if not _dsk_done:
        _rt_skills.skill_delete_by_id(skill_id)

    if storage is None:
        from src.kernel.config import log
        log.warning("存储后端不可用，仅清理 DB 记录，对象文件需手动清理 (skill_id=%s, scope=%s, name=%s)", skill_id, skill_scope, skill_name)

    if object_key:
        from src.domains.runtime.public import invalidate_skill_caches
        try:
            storage.delete_object(object_key)
        except Exception:
            pass
        invalidate_skill_caches(object_key)

    return {"message": f"技能 {skill_name} 已删除"}

VALID_SKILL_AGENT_TYPES = {"vod", "issue", "coding", "qa", "sre", "chat", "auto"}

@router.post("/by-id/{skill_id}/update", operation_id="skills_update",
             summary="Replace a skill package with a new ZIP (Issue #561)",
             description="Overwrites an existing skill's content by uploading a new ZIP. Keeps skill id/scope/owner/scene refs/likes, refreshes content_hash and invalidates both cache levels. official skills cannot be updated; user scope requires ownership; repo scope requires the caller to manage the repo.")
async def update_skill_by_id(skill_id: int, file: UploadFile = File(...),
                             user: dict = Depends(require_no_session_token),
                             agent_type: str = Form(""), category_id: str = Form(""),
                             cat_l1_id: int = Form(0), cat_l2_id: int = Form(0),
                             tags: str = Form(""), summary: "str | None" = Form(None),
                             change_note: str = Form(""),
                             clear_category: bool = Form(False)):
    """按 skill_id 上传 ZIP 覆盖更新既有技能（Issue #561：卡片直接更新，免删了重传）。

    权限对齐 delete：official 不允许更新；user 需本人；repo 需当前用户托管该仓库。
    写序对齐 #444：save 前留存旧对象字节，upsert 失败回滚；成功后两级缓存失效。
    ZIP 内技能目录名必须与既有 skill_name 一致（upsert 按 (name, scope, owner) 命中
    原行，保留 id/场景引用/点赞）；不一致返回 400 明确指引。
    agent_type / category_id 可选，缺省沿用原记录值。
    """
    if not isinstance(clear_category, bool):
        clear_category = False

    # 1. 查既有记录（按主键 id 精确）——只读，不持有 db_lock（Issue #3168：
    # 单库拓扑下 _rt_skills 内部会再取 db_lock，非重入锁会自锁）
    from src.kernel.domain_read import runtime_lib as _ru_lib_9f2ee7_f
    _ru_lib_9f2ee7 = _ru_lib_9f2ee7_f(foreign_only=True)
    if _ru_lib_9f2ee7 is not None:
        row = _ru_lib_9f2ee7.read_one("skill_9f2ee7", (skill_id,))
    else:
        row = _rt_skills.read_one_any("skill_meta_row", (skill_id,))
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    skill_name, skill_scope, owner_id, cur_agent_type, cur_category_id = row[0], row[1] or "user", row[2] or "", row[3] or "", row[4] or 0
    cur_l1 = row[5] if len(row) > 5 else 0
    cur_l2 = row[6] if len(row) > 6 else 0
    cur_tags = row[7] if len(row) > 7 else ""

    # 2. 权限校验（对齐 delete）
    if skill_scope == "official":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "官方技能不允许更新"})
    if skill_scope == "user" and owner_id != str(user["id"]):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权更新该技能"})
    if skill_scope == "repo":
        from src.domains.mgmt.user.db import get_user_repos
        user_repo_keys = {r["repo_key"] for r in (get_user_repos(user["id"]) if user.get("id") else [])}
        if owner_id not in user_repo_keys:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权更新该技能"})

    # 3. 可选字段：缺省沿用原值；提供则校验
    new_agent_type = (agent_type or "").strip() or cur_agent_type
    if new_agent_type not in VALID_SKILL_AGENT_TYPES:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"技能类型不合法，可选: {', '.join(sorted(VALID_SKILL_AGENT_TYPES))}"})
    cat_str = (category_id or "").strip()
    if cat_str:
        try:
            new_category_id = int(cat_str)
        except ValueError:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "分类 ID 必须为整数"})
        if new_category_id > 0:
            from src.domains.mgmt.user.db import get_skill_category
            if not get_skill_category(new_category_id):
                raise HTTPException(status_code=400, detail={"code": 400, "message": f"分类 ID {new_category_id} 不存在"})
    else:
        new_category_id = int(cur_category_id or 0)

    if not clear_category and (int(cat_l1_id or 0) or int(cat_l2_id or 0)):
        _normalize_skill_category(cat_l1_id, cat_l2_id)
    tag_items = _validate_skill_tags(tags)
    tags_json = encode_tags(tag_items, limit=SKILL_MAX_TAGS)
    update_cat_tags = bool(cat_l1_id or cat_l2_id or tags or clear_category)
    from src.domains.mgmt.routes._category import prepare_category_write
    target_l1, target_l2 = prepare_category_write(
        cat_l1_id, cat_l2_id, cur_l1, cur_l2, clear_category)
    target_tags = tags_json if tags else (cur_tags or "")

    # 4. ZIP 校验（复用 upload 链：大小/炸弹/结构/SKILL.md）
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Skill 文件不能超过 10MB"})

    tmp_dir = tempfile.mkdtemp()
    try:
        zip_path = os.path.join(tmp_dir, file.filename or "skill.zip")
        with open(zip_path, "wb") as f:
            f.write(content)

        extract_dir = os.path.join(tmp_dir, "extracted")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                total = sum((i.file_size or 0) for i in zf.infolist())
                if total > MAX_EXTRACT_BYTES:
                    raise HTTPException(status_code=400, detail={"code": 400, "message": f"ZIP 解压后总大小超过 {MAX_EXTRACT_BYTES // (1024 * 1024)}MB 上限"})
                zf.extractall(extract_dir)
                for info in zf.infolist():
                    mode = info.external_attr >> 16
                    if mode and not info.is_dir():
                        p = os.path.join(extract_dir, info.filename)
                        if os.path.isfile(p):
                            os.chmod(p, mode & 0o777)
        except HTTPException:
            raise
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "不是有效的 ZIP 文件，请上传 .zip 格式"})

        entries = os.listdir(extract_dir)
        if not entries:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "ZIP 文件为空"})
        real_entries = [e for e in entries if e not in ("__MACOSX", ".DS_Store", ".git") and not e.startswith(".")]
        if not real_entries:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "ZIP 中未找到技能目录"})

        skill_dir = os.path.join(extract_dir, real_entries[0])
        if not os.path.isdir(skill_dir):
            skill_dir = extract_dir
        zip_skill_name = os.path.basename(skill_dir) if os.path.isdir(skill_dir) else real_entries[0]

        # 名称一致性（核心：upsert 按 (name, scope, owner) 命中原行；名称漂移会新建行而非更新）
        if zip_skill_name != skill_name:
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"ZIP 内技能目录名「{zip_skill_name}」与既有技能「{skill_name}」不一致，请保持同名目录后重新上传（如需新增技能请使用上传）"})

        errors = validate_custom_skill(skill_dir, skill_name)
        if errors:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "Skill 校验失败", "errors": errors})

        fm = parse_skill_frontmatter(skill_dir)
        description = fm.get("description", "") or ""
        summary_value = _resolve_skill_summary(summary, description)
        incoming_version = (fm.get("version") or "").strip()
        target_version = _next_skill_version(int(skill_id), incoming_version)

        # 5. 存储写序（对齐 #444：留存旧字节 → 覆盖 → upsert → 失败回滚 → 缓存失效）
        from src.kernel.config import log
        from src.domains.runtime.public import get_skill_storage
        storage = get_skill_storage()
        if storage is None:
            raise HTTPException(status_code=503, detail={"code": 503, "message": "OBS 存储未启用且本地存储被禁用，技能更新失败。请检查服务端存储配置后重试。"})
        from src.domains.runtime.public import skill_key, zip_cache_put, mem_cache_invalidate
        from src.domains.mgmt.user.db import upsert_skill_obs, get_skill_object_key
        object_key = get_skill_object_key(skill_id) or skill_key(
            "custom" if skill_scope == "user" else skill_scope, owner_id, skill_name)
        try:
            zip_bytes = _normalize_skill_zip(skill_dir, skill_name)
            _prev_bytes = storage.read_bytes(object_key)
            storage.save_bytes(object_key, zip_bytes)
            content_hash = hashlib.sha256(zip_bytes).hexdigest()
            new_id = upsert_skill_obs(skill_scope, owner_id, skill_name, new_agent_type,
                                      description, object_key, str(user["id"]),
                                      new_category_id, content_hash,
                                      display_name=None)  # #3855：保留已录入名称，为空才回填源名称
            if not new_id or int(new_id) != int(skill_id):
                if _prev_bytes is not None:
                    storage.save_bytes(object_key, _prev_bytes)  # 回滚旧对象（DB 行未动）
                raise HTTPException(status_code=409, detail={"code": 409, "message": f"技能 {skill_name} 记录更新失败（并发冲突或同名记录漂移），已回滚，请重试"})
            if update_cat_tags:
                try:
                    _rt_skills.update_skill_category_tags(
                        int(new_id), target_l1, target_l2, target_tags)
                except Exception as _te:  # noqa: BLE001 - 分类/标签写入失败不阻断主流程
                    from src.kernel.config import log as _log
                    _log.warning(f"skill update: category/tags update failed: {_te}")
            try:
                _rt_skills.exec_any("skill_summary_update",
                                    (summary_value, int(new_id)))
                _record_skill_version(int(new_id), target_version,
                                      change_note or "上传更新", user)
                _replace_skill_files(int(new_id), _list_zip_entries(zip_bytes))
            except Exception as _de:  # noqa: BLE001 - 详情附加数据失败不阻断主流程
                log.warning(f"skill update: detail metadata write failed: {_de}")
            zip_cache_put(object_key, zip_bytes, content_hash, int(new_id))
            mem_cache_invalidate(object_key)
            log.info(f"skill updated (storage={storage.backend_name()}, key={object_key}, skill_id={skill_id})")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"技能更新存储失败: {e}"})
        return {"message": f"技能 {skill_name} 更新成功", "skill_id": int(skill_id), "skill_name": skill_name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"更新失败: {e}"})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

@router.patch("/{skill_id}", operation_id="skills_patch_meta",
              summary="Skill metadata edit without re-uploading the package",
              description="Issue #3202: updates tags/summary/description/category/agent_type in place. Issue #3855: official skills allow display_name edits by platform admins only (other fields and non-admin callers 403); user skills require ownership; repo skills require the caller to manage the repo. Refreshes updated_at without creating a new version row.")
def patch_skill_meta(skill_id: int, body: dict = Body(...),
                     user: dict = Depends(require_no_session_token_or_admin)):
    """免传包更新技能元信息（C8）：tags/summary/description/category/agent_type。

    权限对齐 delete/update：user 需本人；repo 需当前用户托管该仓库。
    Issue #3855：official 技能仅放开 display_name（其余元信息维持 403）；
    user/repo 在既有字段白名单上追加 display_name。
    版本号不变（不写 grape_t_skill_version），仅刷新 updated_at。
    """
    row = _rt_skills.read_one_any("skill_detail_by_id", (int(skill_id),))
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})
    skill_name = row[0] or ""
    cur_display = row[1] or ""
    cur_desc = row[2] or ""
    cur_agent = row[3] or ""
    skill_scope = row[5] or "user"
    owner_id = row[6] or ""
    cur_cat_id = int(row[11] or 0)
    cur_l1 = int(row[12] or 0) if len(row) > 12 else 0
    cur_l2 = int(row[13] or 0) if len(row) > 13 else 0
    cur_tags = row[14] if len(row) > 14 else ""

    body = body if isinstance(body, dict) else {}
    if skill_scope == "official":
        # 官方技能改名为管理员专属操作（否则任意 JWT/API Key 可改名官方技能）；
        # 且只放开名称编辑，任何其它元信息字段仍拒绝。
        if not _is_official_skill_admin(user):
            raise HTTPException(status_code=403, detail={
                "code": 403, "message": "官方技能仅管理员可修改名称"})
        _non_name = sorted(set(body) - {"display_name"})
        if _non_name:
            raise HTTPException(status_code=403, detail={
                "code": 403, "message": "官方技能仅允许修改显示名称"})
    elif str(user.get("token_type") or "") == "admin":
        # Issue #3855：管理台 JWT 只允许改官方技能名称。admin_accounts.id 与
        # grape_accounts.id 独立自增，撞号时不能按 owner_id/托管仓库放行到
        # user/repo 技能（否则管理员可改他人技能元信息）。
        raise HTTPException(status_code=403, detail={
            "code": 403, "message": "管理台账号仅可修改官方技能名称"})
    elif skill_scope == "user" and owner_id != str(user.get("id")):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权修改该技能"})
    elif skill_scope == "repo":
        from src.domains.mgmt.user.db import get_user_repos
        user_repo_keys = {r["repo_key"] for r in (get_user_repos(user["id"]) if user.get("id") else [])}
        if owner_id not in user_repo_keys:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权修改该技能"})

    allowed = {"tags", "summary", "description", "category_id",
               "cat_l1_id", "cat_l2_id", "agent_type", "clear_category", "intro",
               "display_name"}
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": f"不支持更新的字段：{'、'.join(unknown)}"})
    if not any(k in body for k in allowed):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "没有可更新的字段"})

    new_display = cur_display
    if "display_name" in body:
        new_display = _validate_skill_display_name(
            body.get("display_name"), scope=skill_scope, owner_id=owner_id,
            exclude_id=int(skill_id))

    extra = _rt_skills.read_one_any("skill_detail_extra_by_id", (int(skill_id),))
    cur_summary = (extra[0] if extra else "") or ""

    new_desc = cur_desc
    if "description" in body:
        new_desc = str(body.get("description") or "")

    new_agent = cur_agent
    if "agent_type" in body:
        new_agent = str(body.get("agent_type") or "").strip()
        if new_agent not in VALID_SKILL_AGENT_TYPES:
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": f"技能类型不合法，可选: {', '.join(sorted(VALID_SKILL_AGENT_TYPES))}"})

    new_category_id = cur_cat_id
    if "category_id" in body:
        try:
            new_category_id = int(body.get("category_id") or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail={"code": 400, "message": "分类 ID 必须为整数"})
        if new_category_id > 0:
            from src.domains.mgmt.user.db import get_skill_category
            if not get_skill_category(new_category_id):
                raise HTTPException(status_code=400, detail={"code": 400, "message": f"分类 ID {new_category_id} 不存在"})

    clear_category = body.get("clear_category")
    if not isinstance(clear_category, bool):
        clear_category = False
    target_l1, target_l2 = cur_l1, cur_l2
    if "cat_l1_id" in body or "cat_l2_id" in body or clear_category:
        try:
            in_l1 = int(body.get("cat_l1_id") or 0) if "cat_l1_id" in body else 0
            in_l2 = int(body.get("cat_l2_id") or 0) if "cat_l2_id" in body else 0
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail={"code": 400, "message": "分类 ID 必须为整数"})
        if not clear_category and (in_l1 or in_l2):
            _normalize_skill_category(in_l1, in_l2)
        from src.domains.mgmt.routes._category import prepare_category_write
        target_l1, target_l2 = prepare_category_write(
            in_l1, in_l2, cur_l1, cur_l2, clear_category)

    tags_json = cur_tags or ""
    if "tags" in body:
        tag_items = _validate_skill_tags(body.get("tags"))
        tags_json = encode_tags(tag_items, limit=SKILL_MAX_TAGS)

    new_summary = cur_summary
    if "summary" in body:
        new_summary = _require_skill_summary(str(body.get("summary") or ""))

    if "intro" in body:
        intro = body.get("intro")
        if intro is None:
            intro = {}
        if not isinstance(intro, dict):
            raise HTTPException(status_code=400, detail={
                "code": 400, "message": "intro 必须为对象（槽位名→文本）"})
        cleaned_intro = {}
        for k, v in (intro or {}).items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise HTTPException(status_code=400, detail={
                    "code": 400, "message": "intro 的键值必须均为字符串"})
            key = k.strip()
            val = v.strip()
            if not key or len(key) > _INTRO_KEY_MAX:
                continue
            if len(val) > _INTRO_TEXT_MAX:
                raise HTTPException(status_code=400, detail={
                    "code": 400, "message": f"intro 单项文本不得超过 {_INTRO_TEXT_MAX} 字"})
            if val:
                cleaned_intro[key] = val
        _rt_skills.exec_any("skill_intro_override_update", (
            json.dumps(cleaned_intro, ensure_ascii=False), int(skill_id)))

    _rt_skills.exec_any("skill_meta_patch_update", (
        new_desc, new_agent, int(new_category_id or 0), int(target_l1 or 0),
        int(target_l2 or 0), tags_json, new_summary, new_display,
        str(user.get("username") or user.get("id") or "")[:_UPLOADER_MAX], int(skill_id)))
    # C3 非查询操作审计：元信息免传包更新留痕（detail 只记字段名，不记值）
    from src.domains.mgmt.auth import audit as _skill_audit
    _skill_audit.log_audit("skill_meta_patched",
                           user_id=str(user.get("id") or ""),
                           operator_id=str(user.get("id") or ""),
                           detail={"skill_id": int(skill_id),
                                   "fields": sorted(set(body) & allowed)})

    latest = _rt_skills.read_one_any("skill_version_latest", (int(skill_id),))
    updated = _rt_skills.read_one_any("skill_detail_extra_by_id", (int(skill_id),))
    return {
        "message": f"技能 {skill_name} 信息已更新",
        "skill_id": int(skill_id),
        "summary": new_summary,
        "display_name": new_display,
        "tags": parse_tags(tags_json, limit=SKILL_MAX_TAGS),
        "agent_type": new_agent,
        "category_id": int(new_category_id or 0),
        "cat_l1_id": int(target_l1 or 0),
        "cat_l2_id": int(target_l2 or 0),
        "updated_at": (updated[3] if updated else "") or "",
        "version": (latest[0] if latest else "") or "",
    }


class ConvertSkillRequest(BaseModel):
    skill_name: str
    repo: str  # 目标仓库 repo_key（如 Jason7602/skill-evaluator）
    source_scope: str = "user"  # 源技能 scope：user 或 repo（默认 user 兼容旧调用）

@router.post("/convert", operation_id="skills_convert", summary="Convert skill (cross scope)", description="Copies a skill between personal(user) and repo scope; the original record and directory are kept (copy, not move).")
def convert_skill(body: ConvertSkillRequest, user: dict = Depends(require_no_session_token)):
    """将技能在个人(scope=user)与仓库(scope=repo)之间双向复制。

    原技能记录与目录保留（复制而非移动）。
    source_scope='user' → 复制到仓库技能；source_scope='repo' → 复制到个人技能。
    """
    skill_name = (body.skill_name or "").strip()
    repo = (body.repo or "").strip()
    source_scope = (body.source_scope or "user").strip()
    if not skill_name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "技能名不能为空"})
    if not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "目标仓库不能为空"})
    if source_scope not in ("user", "repo"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"不支持的 source_scope: {source_scope}"})

    from src.kernel.repo_key import from_repo_key
    repo_key = _resolve_repo_key(repo, strict=True)  # 写路径（convert）：歧义显式报错（#444 根治）
    user_id = str(user["id"])

    from src.kernel.session_manager import db, db_lock

    # 1. 查源技能记录
    if source_scope == "user":
        # 个人技能 → 仓库技能：从 user_db 查
        skill_rec = next((s for s in user_db.list_skills(user["id"]) if s["skill_name"] == skill_name), None)
        if not skill_rec:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "个人技能未找到或不属于当前用户"})
    else:
        # 仓库技能 → 个人技能：从 skill 表查
        with db_lock:
            c = db.cursor()
            from src.kernel.domain_read import runtime_lib as _ru_lib_21ba32_f
            _ru_lib_21ba32 = _ru_lib_21ba32_f(foreign_only=True)
            if _ru_lib_21ba32 is not None:
                c = _ru_lib_21ba32.read_one("skill_21ba32", (skill_name, repo_key),
            ).fetchone()
            else:
                row = _rt_skills.read_one_any("skill_repo_full_row", (skill_name, repo_key))
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库技能未找到或不属于该仓库"})
        skill_rec = {
            "id": row[0], "skill_name": row[1], "agent_type": row[2] or "",
            "description": row[3] or "", "scope": row[4], "owner_id": row[5],
            "file_path": row[6] or "", "category_id": row[7] or 0,
        }

    # 2. 确定目标方向 + 去重检查
    if source_scope == "user":
        dst_scope, dst_owner = "repo", repo_key
        dst_skill_key_scope = "repo"
        dedup_msg = f"目标仓库已存在同名技能「{skill_name}」"
        exists = _rt_skills.skill_repo_count_by_name(skill_name, repo_key)
    else:
        dst_scope, dst_owner = "user", user_id
        dst_skill_key_scope = "custom"
        dedup_msg = f"个人技能已存在同名技能「{skill_name}」"
        exists = any(s["skill_name"] == skill_name for s in user_db.list_skills(user["id"]))
    if exists:
        raise HTTPException(status_code=409, detail={"code": 409, "message": dedup_msg})

    # ===== 存储分支：copy → DB 回写（OBS copyObject / 本地 copy）=====
    from src.domains.runtime.public import get_skill_storage
    storage = get_skill_storage()
    if storage is not None:
        from src.domains.runtime.public import skill_key, zip_cache_get, zip_cache_put, mem_cache_invalidate, zip_cache_get_with_hash, get_skill_zip_bytes
        from src.domains.mgmt.user.db import get_skill_object_key, upsert_skill_obs
        src_key = get_skill_object_key(skill_rec["id"])
        if not src_key:
            src_label = "个人" if source_scope == "user" else "仓库"
            raise HTTPException(status_code=404, detail={"code": 404, "message": f"源{src_label}技能未在 OBS（历史数据请先执行迁移），无法转换"})
        dst_key = skill_key(dst_skill_key_scope, dst_owner, skill_name)
        if not storage.copy_object(src_key, dst_key):
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"OBS 复制技能失败: {src_key} -> {dst_key}"})
        # Issue #444 (P47)：convert 同款写 content_hash——copy 的 zip 字节与源一致，
        # hash 复用源缓存条目的 hash；源无 hash（存量）则对 src_zip 现算
        _src_zip = zip_cache_get_with_hash(src_key)
        src_zip, _src_hash = (_src_zip[:2] if _src_zip else (None, ""))
        if src_zip is None:
            src_zip = get_skill_zip_bytes(src_key) or b""
        content_hash = _src_hash or (hashlib.sha256(src_zip).hexdigest() if src_zip else "")
        # Issue #3855：复制/转换保留源自定义显示名称（新建行不再回退 skill_name）。
        # 显式计算有效展示名：≤20 字上限 + 目标 scope 唯一，避免存量超长源名称
        # 因传 display_name=None 回填而绕过长度限制写入新行。
        _src_detail = _rt_skills.read_one_any("skill_detail_by_id", (int(skill_rec["id"]),))
        _src_display = (_src_detail[1] if _src_detail and len(_src_detail) > 1 else "") or ""
        _target_display = _converted_display_name(
            dst_scope, dst_owner, _src_display, skill_name)
        skill_id = upsert_skill_obs(dst_scope, dst_owner, skill_name, skill_rec.get("agent_type", ""),
                                    skill_rec.get("description", ""), dst_key, user_id,
                                    skill_rec.get("category_id", 0), content_hash,
                                    display_name=_target_display)
        if not skill_id:
            storage.delete_object(dst_key)
            raise HTTPException(status_code=409, detail={"code": 409, "message": dedup_msg})
        # 源 zip 若在磁盘缓存，复制到目标 key（懒加载兜底亦可）；写侧拿到新行 id，
        # 直接锚定（目标行是新 upsert 的行，id 即 dst 锚点）
        if src_zip:
            zip_cache_put(dst_key, src_zip, content_hash, skill_id or 0)
        mem_cache_invalidate(dst_key)  # Issue #444 (P49)：object_key 口径（原 skill_name 会失效错键）
        dst_label = f"仓库 {repo}" if dst_scope == "repo" else "个人技能"
        return {"message": f"技能 {skill_name} 已复制到{dst_label}", "skill_name": skill_name, "scope": dst_scope, "repo": repo if dst_scope == "repo" else ""}

    # local 分支已移除：均衡负载环境下 local 存储会导致文件只存在单机
    raise HTTPException(status_code=503, detail={"code": 503, "message": "OBS 存储未启用，技能转换失败。请检查服务端 OBS 配置后重试。"})

def _get_sub_skill_zip(skill_id: int, skill_name: str) -> bytes | None:
    """子技能端点 OBS 分支共用（Issue #448）：取技能 zip 字节。

    本地 references/ 目录未命中时调用。对象键缺失（历史数据未迁移）返回 None
    （沿用既有 404/空列表口径）；有对象键但存储未启用 → 503（对齐 detail 端点）。
    """
    from src.domains.mgmt.user.db import get_skill_object_key
    from src.domains.runtime.public import get_skill_storage, get_skill_zip_bytes
    object_key = get_skill_object_key(skill_id)
    if not object_key:
        return None
    if get_skill_storage() is None:
        raise HTTPException(status_code=503, detail={
            "code": 503, "message": "OBS 存储未启用，子技能读取不可用。请检查服务端 OBS 配置后重试。"})
    return get_skill_zip_bytes(object_key)

@router.get("/{skill_id}/sub", operation_id="skills_sub_list", summary="List sub-skills of a skill package", description="Lists a skill package's sub-skills from the references/ directory.")
def list_sub_skills(skill_id: int, user: dict = Depends(get_current_user_or_api_key)):
    """返回技能包的子技能列表（从 skill_path 的 references/ 目录读取）"""
    from src.kernel.session_manager import db, db_lock
    import re as _re

    row = _rt_skills.read_one_any("skill_name_path", (skill_id,))
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    skill_name, skill_path = row[0], row[1] or ""
    from src.kernel.config import SKILLS_DIR, resolve_official_skill_path
    _r = resolve_official_skill_path(skill_name)
    if _r:
        base_dir = os.path.dirname(_r)
    else:
        # fallback: skill_path 可能是 3 层，尝试去掉中间层
        _parts = skill_path.split("/")
        if len(_parts) >= 3:
            base_dir = os.path.join(SKILLS_DIR, _parts[0], _parts[-1])
        else:
            base_dir = os.path.join(SKILLS_DIR, skill_path) if skill_path else ""
    ref_dir = os.path.join(base_dir, "references")

    if not os.path.isdir(ref_dir):
        # ===== OBS 分支（Issue #448）：user/repo 技能从 zip 提取 references/ =====
        from src.domains.runtime.public import list_zip_sub_skills
        zip_bytes = _get_sub_skill_zip(skill_id, skill_name)
        if zip_bytes is not None:
            return {"skill_name": skill_name, "sub_skills": list_zip_sub_skills(zip_bytes, skill_name)}
        return {"skill_name": skill_name, "sub_skills": []}

    subs = []
    for entry in sorted(os.listdir(ref_dir)):
        sub_dir = os.path.join(ref_dir, entry)
        if not os.path.isdir(sub_dir):
            continue
        sub_md = os.path.join(sub_dir, "SKILL.md")
        # Issue #509（维护方回退口径「列出 + 标注」）：列表恢复枚举 references/ 全部目录（不静默过滤），
        # 新增 readable 标注（= 该目录下是否存在 SKILL.md）；无 SKILL.md 详情仍 404（read_sub_skill 不变）。
        readable = os.path.isfile(sub_md)
        display_name = entry
        description = ""
        file_count = 0
        if readable:
            try:
                with open(sub_md, encoding="utf-8") as f:
                    front = f.read(500)
                m = _re.search(r'description:\s*["\']?(.*?)["\']?$', front, _re.MULTILINE)
                if m:
                    description = m.group(1)[:200]
                dm = _re.search(r'name:\s*["\']?(.*?)["\']?$', front, _re.MULTILINE)
                if dm:
                    display_name = dm.group(1)
            except Exception:
                pass
        for _, _, files in os.walk(sub_dir):
            file_count += len(files)
        subs.append({
            "name": entry,
            "display_name": display_name,
            "description": description,
            "file_count": file_count,
            "readable": readable,
        })

    return {"skill_name": skill_name, "sub_skills": subs}

@router.get("/{skill_id}/sub/{sub_name}", operation_id="skills_sub_get", summary="Read sub-skill SKILL.md", description="Reads a sub-skill's SKILL.md content.")
def read_sub_skill(skill_id: int, sub_name: str, user: dict = Depends(get_current_user_or_api_key)):
    """读取技能包子技能的 SKILL.md 内容"""
    from src.kernel.session_manager import db, db_lock

    row = _rt_skills.read_one_any("skill_name_path", (skill_id,))
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    from src.kernel.config import SKILLS_DIR, resolve_official_skill_path
    _r = resolve_official_skill_path(row[0])
    if _r:
        base_dir = os.path.dirname(_r)
    else:
        _sp = row[1] or ""
        _parts = _sp.split("/")
        if len(_parts) >= 3:
            base_dir = os.path.join(SKILLS_DIR, _parts[0], _parts[-1])
        else:
            base_dir = os.path.join(SKILLS_DIR, _sp) if _sp else ""
    sub_md = os.path.join(base_dir, "references", sub_name, "SKILL.md")

    if not os.path.isfile(sub_md):
        # ===== OBS 分支（Issue #448）：user/repo 技能从 zip 提取子技能 =====
        from src.domains.runtime.public import read_zip_sub_skill
        zip_bytes = _get_sub_skill_zip(skill_id, row[0])
        sub_data = read_zip_sub_skill(zip_bytes, row[0], sub_name) if zip_bytes else None
        if sub_data is None:
            raise HTTPException(status_code=404, detail={"code": 404, "message": f"子技能 {sub_name} 未找到"})
        return {"name": sub_name, "content": sub_data["content"], "references": sub_data["references"]}

    try:
        with open(sub_md, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"读取失败: {e}"})

    # 也读取 references/ 下的文件列表
    ref_dir = os.path.join(base_dir, "references", sub_name, "references")
    refs = []
    if os.path.isdir(ref_dir):
        for r in sorted(os.listdir(ref_dir)):
            rp = os.path.join(ref_dir, r)
            if os.path.isfile(rp):
                try:
                    with open(rp, encoding="utf-8") as f:
                        refs.append({"name": r, "content": f.read()[:2000]})
                except Exception:
                    refs.append({"name": r, "content": ""})

    return {"name": sub_name, "content": content, "references": refs}

@router.get("/package-sub/{pkg_id}/{sub_name}", operation_id="skills_package_sub_get", summary="Get sub-skill detail from package", description="Returns a sub-skill detail from a package (same shape as skill detail).")
def package_sub_skill(pkg_id: int, sub_name: str, user: dict = Depends(get_current_user_or_api_key)):
    """返回技能包子技能详情（格式与 skill detail 一致，支持导航到独立详情页）"""
    from src.kernel.session_manager import db, db_lock
    import re as _re

    with db_lock:
        c = db.cursor()
        from src.kernel.domain_read import runtime_lib as _ru_lib_8d3c93_f
        _ru_lib_8d3c93 = _ru_lib_8d3c93_f(foreign_only=True)
        if _ru_lib_8d3c93 is not None:
            row = _ru_lib_8d3c93.read_one("skill_8d3c93", (pkg_id,))
        else:
            row = _rt_skills.read_one_any("skill_name_path_scope", (pkg_id,))
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "父技能未找到"})

    from src.kernel.config import SKILLS_DIR, resolve_official_skill_path
    _r = resolve_official_skill_path(row[0])
    if _r:
        base_dir = os.path.dirname(_r)
    else:
        _sp = row[1] or ""
        _parts = _sp.split("/")
        if len(_parts) >= 3:
            base_dir = os.path.join(SKILLS_DIR, _parts[0], _parts[-1])
        else:
            base_dir = os.path.join(SKILLS_DIR, _sp) if _sp else ""
    sub_md = os.path.join(base_dir, "references", sub_name, "SKILL.md")

    content = None
    if os.path.isfile(sub_md):
        try:
            with open(sub_md, encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"读取失败: {e}"})
    else:
        # ===== OBS 分支（Issue #448）：user/repo 技能从 zip 提取子技能 =====
        from src.domains.runtime.public import read_zip_sub_skill
        zip_bytes = _get_sub_skill_zip(pkg_id, row[0])
        sub_data = read_zip_sub_skill(zip_bytes, row[0], sub_name) if zip_bytes else None
        content = sub_data["content"] if sub_data else None
    if content is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"子技能 {sub_name} 未找到"})

    # 从 frontmatter 解析 name 和 description
    display_name = sub_name
    description = ""
    m = _re.search(r'^---\s*\n(.*?)\n---', content, _re.DOTALL)
    if m:
        front = m.group(1)
        dm = _re.search(r'name:\s*["\']?(.*?)["\']?\s*$', front, _re.MULTILINE)
        if dm:
            display_name = dm.group(1)
        desc_m = _re.search(r'description:\s*["\']?(.*?)["\']?\s*$', front, _re.MULTILINE)
        if desc_m:
            description = desc_m.group(1)[:200]

    return {
        "id": f"{pkg_id}/{sub_name}",
        "name": sub_name,
        "display_name": display_name,
        "description": description,
        "agent_type": "sre",
        "scope": row[2] or "official",
        "content": content,
        "type": "package-sub",
    }

def _safe_zip_name(filename: str | None) -> str:
    """净化 multipart filename 为安全 .zip 文件名（CWE-22 路径穿越防护，Issue #856）。

    - None/空白 → 默认 "skill.zip"（保持原有缺省行为）
    - 含 / 或 \\ 路径分隔符（绝对路径、目录穿越片段）→ 非法（返回空串，调用方返回 400）
    - 必须以 .zip 结尾，否则非法
    """
    if not filename or not filename.strip():
        return "skill.zip"
    name = filename.strip()
    if "/" in name or "\\" in name or name in (".", ".."):
        return ""
    if not name.lower().endswith(".zip"):
        return ""
    return name

def _restore_zip_permissions(zf: zipfile.ZipFile, extract_dir: str) -> None:
    """恢复解压成员的 Unix 权限位（Python 3.12+ extractall 不再恢复）。

    chmod 前对每个成员做 realpath 前缀校验（Issue #856 纵深防御）：
    不在 extract_dir 下的成员跳过并记日志，杜绝 info.filename 伪造穿越。
    """
    from src.kernel.config import log
    real_root = os.path.realpath(extract_dir)
    for info in zf.infolist():
        mode = info.external_attr >> 16
        if mode and not info.is_dir():
            p = os.path.join(extract_dir, info.filename)
            if os.path.realpath(p).startswith(real_root + os.sep):
                if os.path.isfile(p):
                    os.chmod(p, mode & 0o777)
            else:
                log.warning("skill zip member escapes extract dir, skip chmod: %r", info.filename)
