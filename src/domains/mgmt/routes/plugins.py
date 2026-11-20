"""Plugins 路由：插件市场服务端 API（Issue #1084，总纲 #1078 B1；#1799 货架治理）。

市场货架：上传 / 列表 / 详情 / 分类 / 搜索 / 点赞；#1799 新增：
- DELETE /by-id/{id}            市场软删（status=revoked 台账保留，区别于 runtime 卸载）
- GET /by-id/{id}/versions      版本族全量列表（active/superseded/revoked）
- POST /by-id/{id}/rollback     生效版本切换（其余 active 降 superseded）
- 上传收口：同名版本族唯一生效（其余降 superseded），不再重复在架
复用 skills 已验证的"路由 + 对象存储三态 + 版本化迁移"模式：
- 三态 scope：official（部署内置，本任务不提供写入）/ user（个人）/ repo（仓库），
  可见性对齐 skills（official 全员可见、user 仅本人、repo 对托管成员可见）；
- 插件包 zip 落对象存储（OBS 优先，local 兜底），DB 只存元数据 + object_key；
- 上传链路接入 A1（#1079）L0 校验器适配层（src/api/plugin_l0.py），
  A1 未合入时内置最小校验 + 降级提示，非法 manifest 不入库（4xx + 明确错误码）。
"""
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import zipfile

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from src.api.deps import (get_current_user_or_api_key, require_admin,
                          require_no_session_token, require_org_member)
from src.kernel.id_gen import uuid7_str
from src.domains.mgmt.plugin_market.l0_adapter import validate_plugin_l0, l0_degraded_hint
from src.domains.mgmt.plugin_market.security import certification_projection as _certification_projection
from src.domains.mgmt.plugin_market.source_consistency import is_commit_sha40 as _is_commit_sha40

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

# 解压后总大小上限（zip 炸弹防护，复用 skills MAX_EXTRACT_BYTES 模式）
MAX_EXTRACT_BYTES = int(os.environ.get("PLUGIN_MAX_EXTRACT_BYTES", str(100 * 1024 * 1024)))
# 上传文件大小上限（插件包可比技能大，默认 50MB，env 可调）
MAX_UPLOAD_BYTES = int(os.environ.get("PLUGIN_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))

_RE_KEY_SEG = re.compile(r"^[A-Za-z0-9._-]+$")
# Issue #1798 QA 修复③：source_branch 目标分支声明（git ref 安全子集，quote 后进 URL path）
_RE_BRANCH_NAME = re.compile(r"^[A-Za-z0-9._\-/]{1,120}$")


def _valid_source_branch_name(name: str) -> bool:
    """source_branch 合法性（git ref 规则子集：禁空白/特殊符号、禁 .. 穿越段）。"""
    if not name:
        return True
    if not _RE_BRANCH_NAME.match(name):
        return False
    if ".." in name or name.startswith("-") or name.endswith("/"):
        return False
    return True

# plugin.status 台账状态唯一事实源（active=在架生效 / superseded=版本族历史 / revoked=软删）：
# 消除 15+ 处字面量硬编码（QA B3），拼写失误在此处一次性拦截。
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_REVOKED = "revoked"


def _get_plugin_storage():
    """插件域存储：复用技能域存储兜底链（OBS 优先，SKILL_STORAGE=auto/local 时本地兜底）。

    与 skills 三态存储同一后端（src/storage/obs.py + local.py），仅对象键布局不同
    （plugins/ 段），保证"三态存储复用 skills 的 OBS 模式"。
    """
    from src.domains.runtime.storage import get_skill_storage
    return get_skill_storage()


def _plugin_key(scope: str, owner: str, name: str, version: str) -> str:
    """插件包对象键（对齐 skill_key 白名单/隔离规范，layout 为 plugins 段）：
    {PREFIX}/plugins/{scope}/{owner_seg}/{name_seg}/{stem}-{version}.zip
    user scope: owner_seg = user_id；repo scope: owner_seg = {provider}/{owner}/{repo}
    （#444 同款 provider 段隔离）。

    name 传完整插件名（#1085 复审键碰撞修复）：@owner 命名空间名落
    _ns_{owner}/{bare} 段，与裸名键空间隔离——裸名 foo 与 @alice/foo
    （同发布者同 version 同 scope）不再共享同一对象（否则后传覆盖前者，
    前者入库指纹恒失配 409）。键段全部过白名单，映射单射：
    裸名 n → {n}/{n}-{v}.zip；命名空间名 → _ns_{o}/{b}/{b}-{v}.zip。
    """
    from src.domains.mgmt.plugin_market.security import parse_plugin_name
    if scope not in ("user", "repo"):
        raise ValueError(f"invalid scope: {scope}")
    ns, bare = parse_plugin_name(name)
    if ns:
        if not _RE_KEY_SEG.match(ns) or not _RE_KEY_SEG.match(bare or ""):
            raise ValueError(f"invalid plugin name: {name}")
        name_seg, stem = f"_ns_{ns}/{bare}", bare
    else:
        if not _RE_KEY_SEG.match(name or ""):
            raise ValueError(f"invalid plugin name: {name}")
        name_seg, stem = name, name
    if not _RE_KEY_SEG.match(version or ""):
        raise ValueError(f"invalid plugin version: {version}")
    if scope == "user":
        # D105（arch）：user_id 是 UUID 串（也兼容历史整数 id）
        if not re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$|^\d+$", str(owner or "")):
            raise ValueError(f"invalid user_id: {owner}")
        owner_seg = str(owner)
    else:
        _owner = owner or ""
        _provider = "gitcode"
        if ":" in _owner:
            _provider, _owner = _owner.split(":", 1)
            if not _RE_KEY_SEG.match(_provider):
                raise ValueError(f"invalid provider in repo_key: {owner}")
        parts = _owner.split("/", 1)
        if len(parts) != 2 or not _RE_KEY_SEG.match(parts[0]) or not _RE_KEY_SEG.match(parts[1]):
            raise ValueError(f"invalid repo_key: {owner}")
        owner_seg = f"{_provider}/{parts[0]}/{parts[1]}"
    from src.kernel.config import OBS_PREFIX
    prefix = (OBS_PREFIX or "").strip("/")
    return f"{prefix}/plugins/{scope}/{owner_seg}/{name_seg}/{stem}-{version}.zip"


def _resolve_repo_key(repo: str, strict: bool = False) -> str:
    """查 DB 确定实际 repo_key 前缀（gitcode: 或 github:），对齐 skills 同名函数。"""
    from src.kernel.repo_key import to_repo_key
    rk = to_repo_key(repo)
    if ":" in repo:
        return rk
    try:
        from src.kernel.session_manager import db as _db, db_lock as _dl
        with _dl:
            c = _db.cursor()
            rows = c.execute("SELECT repo_key FROM managed_repos WHERE repo_key IN (?, ?)",
                             (f"gitcode:{repo}", f"github:{repo}")).fetchall()
            if len(rows) == 2 and strict:
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": f"仓库 {repo} 同时存在于 gitcode 与 github，请指定完整仓库标识（如 gitcode:{repo}）后重试"})
            if rows:
                return rows[0][0]
    except HTTPException:
        raise
    except Exception:
        pass
    return rk


class PluginUpsertConflict(Exception):
    """同名同版本唯一键 (name, version, scope, owner) 写入冲突（跨进程并发竞态）。

    Spec #1799 决策表「upsert 优先，条件不满足时显式冲突提示」的显式冲突分支：
    进程内 db_lock 已串行化，此异常覆盖多进程/多实例同时 INSERT 同一唯一键的
    竞态，由上传端点映射为 409 显式冲突（区别于通用写入失败兜底）。
    """


def _upsert_plugin(name: str, version: str, display_name: str, description: str,
                   manifest_text: str, manifest_hash: str, category_id: int,
                   author: str, object_key: str, content_hash: str, scope: str,
                   owner_id: str, created_by: str,
                   namespace_owner: str = "", source_repo: str = "",
                   source_ref: str = "", source_content_hash: str = "") -> int | None:
    """插件记录 upsert（唯一键 (name, version, scope, owner_id)，覆盖更新语义）。

    Issue #1798：source_repo/source_ref/source_content_hash 来源字段随成功
    上传回写（无来源声明的 unverified 上传写空串，Spec 口径）。QA 修复④：
    同键无声明重传不得清空已回写的来源指纹（版本回溯），空新值保留旧值；
    显式新声明（强校验通过）正常覆盖。
    Issue #1799 上传收口：upsert 成功后同 (name, scope, owner) 其余 active 行
    降 superseded——本条成为唯一生效（在架）版本，源头消除重复在架记录。
    """
    from src.kernel.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, source_repo, source_ref, source_content_hash "
                "FROM grape_t_plugin WHERE name=? AND version=? AND scope=? AND owner_id=?",
                (name, version, scope, owner_id))
            row = c.fetchone()
            if row:
                # 空新值保留旧值（来源指纹不被无声明重传抹除；新声明覆盖）
                src_repo = source_repo or (row[1] or "")
                src_ref = source_ref or (row[2] or "")
                src_hash = source_content_hash or (row[3] or "")
                c.execute(
                    f"UPDATE grape_t_plugin SET display_name=?, description=?, manifest=?, manifest_hash=?, "
                    f"category_id=?, author=?, object_key=?, content_hash=?, created_by=?, "
                    f"namespace_owner=?, source_repo=?, source_ref=?, source_content_hash=?, "
                    f"status='{STATUS_ACTIVE}', revoked_at='', "
                    f"updated_at=datetime('now') WHERE id=?",
                    (display_name or name, description or "", manifest_text or "{}",
                     manifest_hash or "", category_id or "", author or "", object_key or "",
                     content_hash or "", created_by or "", namespace_owner or "",
                     src_repo, src_ref, src_hash, row[0]))
                plugin_id = str(row[0])
            else:
                plugin_id = uuid7_str()
                cur = c.execute(
                    "INSERT INTO grape_t_plugin (id, name, version, display_name, description, manifest, "
                    "manifest_hash, category_id, author, object_key, content_hash, scope, "
                    "owner_id, created_by, namespace_owner, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (plugin_id, name, version, display_name or name, description or "", manifest_text or "{}",
                     manifest_hash or "", category_id or "", author or "", object_key or "",
                     content_hash or "", scope, owner_id, created_by or "", namespace_owner or "",
                     STATUS_ACTIVE))
            # Issue #1799：同版本族其余在架行降 superseded（本行为唯一生效版本）
            _demote_other_active(c, name, scope, owner_id, plugin_id)
            db.commit()
            return plugin_id
    except sqlite3.IntegrityError as e:
        db.rollback()
        from src.kernel.config import log
        log.warning(f"upsert_plugin unique-key conflict: {e}")
        raise PluginUpsertConflict(f"{name}@{version} scope={scope} owner={owner_id}")
    except Exception as e:
        from src.kernel.config import log
        log.warning(f"upsert_plugin failed: {e}")
        return None


def _demote_other_active(c, name: str, scope: str, owner_id: str, keep_id: int) -> None:
    """同 (name, scope, owner) 版本族其余 active 行降 superseded（保留 keep_id 行）。

    上传 upsert 与回滚共用（调用方持有 db_lock 并负责 commit）。
    """
    c.execute(
        f"UPDATE grape_t_plugin SET status='{STATUS_SUPERSEDED}', updated_at=datetime('now') "
        f"WHERE name=? AND scope=? AND owner_id=? AND status='{STATUS_ACTIVE}' AND id<>?",
        (name, scope, owner_id, keep_id))


def _list_plugin_categories() -> list[dict]:
    """grape_t_plugin_category 全量（is_active=1，order_num 升序）。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT id, name, slug, icon, order_num FROM grape_t_plugin_category "
            "WHERE is_active=1 ORDER BY order_num, id").fetchall()
    return [{"id": r[0], "name": r[1], "slug": r[2] or "", "icon": r[3] or "",
             "order_num": r[4] or 0} for r in rows]


def _get_plugin_category(category_id: "int | str") -> dict | None:
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT id, name, slug FROM grape_t_plugin_category WHERE id=?",
                        (category_id,)).fetchone()
    return {"id": row[0], "name": row[1], "slug": row[2] or ""} if row else None


def _is_plugin_liked(user_id, plugin_id: str) -> bool:
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT 1 FROM grape_t_plugin_likes WHERE user_id=? AND plugin_id=?",
                        (user_id, plugin_id)).fetchone()
    return row is not None


def _ensure_plugin_visible(scope: str, owner_id: str, user: dict) -> None:
    """插件可见性校验（三态对齐 skills：official 公开；user 仅本人；repo 托管成员）。

    详情与下载共用同一出口（#1085 复审去重），越权 → 403。"""
    if scope == "user" and owner_id != str(user["id"]):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该插件"})
    if scope == "repo":
        from src.domains.mgmt.user.db import get_user_repos
        user_repo_keys = {r["repo_key"] for r in (get_user_repos(user["id"]) if user.get("id") else [])}
        if owner_id not in user_repo_keys:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该插件"})


def _source_gate_form_contract(scope: str, source_repo: str, source_ref: str,
                               source_branch: str = "") -> dict | None:
    """来源声明契约校验（Issue #1798，upload 与 dry-run 共用同一契约 → 同一 400 口径）。

    - scope=repo 未声明 → 400 PLUGIN_SOURCE_REQUIRED（Spec：repo scope 必须强校验）；
    - 只声明一半（source_repo / source_ref / source_branch 任一缺失）→ 400
      PLUGIN_SOURCE_REQUIRED；
    - source_ref 非 40 位 sha → 400 PLUGIN_SOURCE_INVALID；
    - source_branch 非法分支名 → 400 PLUGIN_SOURCE_INVALID（QA 修复③声明位）；
    - scope=user 未声明 → 返回 unverified consistency（调用方直接采用，不拦截）。
    返回 None 表示声明完整合法，待评估。
    """
    s_repo = (source_repo or "").strip()
    s_ref = (source_ref or "").strip()
    s_branch = (source_branch or "").strip()
    if not s_repo and not s_ref and not s_branch:
        if scope == "repo":
            raise HTTPException(status_code=400, detail={
                "code": 400, "error_code": "PLUGIN_SOURCE_REQUIRED",
                "message": "仓库作用域（scope=repo）插件上传必须声明 source_repo 与 "
                           "source_ref（40 位 commit sha），用于来源一致性校验"})
        return {"status": "unverified", "source_repo": "", "source_ref": "",
                "source_branch": "", "head_sha": "",
                "warnings": ["未声明来源（source_repo/source_ref），"
                             "跳过来源一致性校验：consistency=unverified"]}
    if not s_repo or not s_ref:
        raise HTTPException(status_code=400, detail={
            "code": 400, "error_code": "PLUGIN_SOURCE_REQUIRED",
            "message": "source_repo 与 source_ref 必须同时声明（来源一致性校验）"})
    if not _is_commit_sha40(s_ref):
        raise HTTPException(status_code=400, detail={
            "code": 400, "error_code": "PLUGIN_SOURCE_INVALID",
            "message": f"source_ref 必须为 40 位完整 commit sha（当前值：{s_ref[:64]!r}）"})
    if not _valid_source_branch_name(s_branch):
        raise HTTPException(status_code=400, detail={
            "code": 400, "error_code": "PLUGIN_SOURCE_INVALID",
            "message": f"source_branch 不是合法的分支名（当前值：{s_branch[:64]!r}）"})
    return None


def _requester_authorized_repo_keys(user: dict | None, upload_repo_key: str = "") -> set:
    """来源仓平台凭据授权门（Issue #1798 QA 安全修复②，Standards-最差）。

    请求者可动用平台托管仓凭据读取的仓库集合（fail-closed，默认空）：
    - web/api-key 用户：其托管/成员仓库（user_repos 缓存）；
    - 会话 token（agent）：token 绑定仓库；
    - scope=repo 上传：上传仓本身（上传链路已验证其对该仓的写权限）。

    声明的 source_repo 不在集合内 → 仅匿名读（公开仓可校验，私仓 →
    unverified 降级），杜绝「用 bot token 探测任意私仓并回显文件树/字段值」。
    """
    keys: set = set()
    if user is None:
        return keys
    try:
        if upload_repo_key:
            keys.add(upload_repo_key)
        if (user.get("auth_type") or "") == "session_token":
            keys.add((user.get("repo_key") or "").strip())
        elif user.get("id"):
            from src.domains.mgmt.user.db import get_user_repos
            keys.update(r["repo_key"] for r in
                        (get_user_repos(user["id"]) or []) if r.get("repo_key"))
    except Exception:
        pass
    return {k for k in keys if k}


async def _evaluate_source_gate(scope: str, source_repo: str, source_ref: str,
                                 plugin_dir: str, manifest: dict,
                                 source_branch: str = "",
                                 user: dict | None = None,
                                 upload_repo_key: str = "") -> dict:
    """来源一致性评估（Issue #1798，不做 409 映射——upload 拦截 / dry-run 自检共用
    同一评估内核，保证 dry-run 返回同一份 diff 结果）。

    评估内核见 src/plugin_market/source_consistency.py（平台凭据读取为可打桩 seam）。
    - 声明了来源（user/repo 均可）：强校验，返回 pass/mismatch/unverified；
    - scope=user 未声明：unverified + 告警放行（兼容无仓库第三方插件）；
    - 平台凭据读不了来源仓：unverified 降级 + 显式告警（Spec 风险口径，不静默放行）；
    - 平台凭据授权门：仅请求者成员/归属仓库可动用凭据（QA 安全修复②）；
    - source_branch：目标分支声明位（QA Spec 修复③，空 = 默认分支）。
    """
    pre = _source_gate_form_contract(scope, source_repo, source_ref, source_branch)
    if pre is not None:
        return pre
    from src.domains.mgmt.plugin_market.source_consistency import evaluate_source_consistency
    from src.kernel.repo_key import to_repo_key
    s_repo = (source_repo or "").strip()
    s_ref = (source_ref or "").strip()
    s_branch = (source_branch or "").strip()
    authorized = to_repo_key(s_repo) in _requester_authorized_repo_keys(
        user, upload_repo_key)
    from fastapi.concurrency import run_in_threadpool
    return await run_in_threadpool(
        evaluate_source_consistency, plugin_dir, manifest, s_repo, s_ref,
        s_branch, authorized)


async def _check_source_consistency(scope: str, source_repo: str, source_ref: str,
                                    plugin_dir: str, manifest: dict,
                                    source_branch: str = "",
                                    user: dict | None = None,
                                    upload_repo_key: str = "") -> dict:
    """上传链路门控包装：评估 + mismatch → 409 PLUGIN_SOURCE_MISMATCH（含 diff 清单）。

    拦截先于任何存储写入（非 HEAD 产物不入对象存储不入库）。
    """
    consistency = await _evaluate_source_gate(scope, source_repo, source_ref,
                                              plugin_dir, manifest,
                                              source_branch=source_branch,
                                              user=user, upload_repo_key=upload_repo_key)
    if consistency.get("status") == "mismatch":
        raise HTTPException(status_code=409, detail={
            "code": 409, "error_code": "PLUGIN_SOURCE_MISMATCH",
            "message": "来源一致性校验未通过：上架包与来源仓库 HEAD 产物不一致"
                       "（差异清单见 consistency 明细）",
            "consistency": consistency})
    return consistency

def _ensure_plugin_writable(scope: str, owner_id: str, user: dict) -> None:
    """写口径校验（软删/回滚端点，QA S4 越权收口）。

    official 货架为部署内置（本模块不提供写入入口），任何登录用户不可经
    市场接口软删/回滚 official 条目 → 403；user/repo 归属校验同读口径。
    """
    if scope == "official":
        raise HTTPException(status_code=403, detail={
            "code": 403,
            "message": "official 货架条目为部署内置，不支持市场侧软删/回滚"})
    _ensure_plugin_visible(scope, owner_id, user)


def _load_visible_anchor(db, plugin_id: str, user: dict, write: bool = False):
    """锚点加载 + 归属校验（软删/版本列表/回滚共用出口，QA B2 去重）。

    SELECT by id → 不存在 404 → 归属校验（write=True 走写口径：official 403）。
    必须在调用方 db_lock 段内执行（读写合并单锁段，QA S5 TOCTOU 收口）。
    返回 (id, name, version, scope, owner_id, status)。
    """
    row = db.execute(
        "SELECT id, name, version, scope, owner_id, status FROM grape_t_plugin WHERE id=?",
        (plugin_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "插件未找到"})
    if write:
        _ensure_plugin_writable(row[3], row[4], user)
    else:
        _ensure_plugin_visible(row[3], row[4], user)
    return row


@router.get("", operation_id="plugins_list", summary="List plugins",
            description="Lists plugins: official visible to all, user visible to owner, repo visible to members. "
                        "Supports scope/repo/category_id filtering, q keyword, sort (latest/hot) and paging.")
def list_plugins(user: dict = Depends(require_org_member), repo: str = "", scope: str = "",
                 category_id: str = "", q: str = "", sort: str = "",
                 page: int = 1, page_size: int = 0):
    """返回插件列表：official 所有用户可见；user 本人可见；repo 指定仓库可见。

    scope 参数：official/repo/user 按维度过滤；all/不传 = 返回全部（可见范围内）。
    q 参数：搜索词，按 name / display_name / description / author 模糊匹配。
    sort 参数：latest（updated_at 降序）/ hot（like_count 降序）/ 不传按 updated_at 降序。
    page/page_size：分页参数，page_size=0 时不分页。
    """
    from src.kernel.session_manager import db, db_lock

    repo_key = _resolve_repo_key(repo) if repo else ""
    conditions = [f"status='{STATUS_ACTIVE}'"]
    params = []
    if scope == "official":
        conditions.append("scope='official'")
    elif scope == "repo":
        if not repo:
            return {"plugins": [], "total": 0, "page": page, "page_size": page_size}
        conditions.append("scope='repo'")
        conditions.append("owner_id=?")
        params.append(repo_key)
    elif scope == "user":
        conditions.append("scope='user'")
        conditions.append("owner_id=?")
        params.append(str(user["id"]))
    else:
        # 可见范围全量：official + 本人 user 插件 +（指定 repo 时）该仓库插件
        if repo_key:
            conditions.append("(scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?))")
            params.extend([str(user["id"]), repo_key])
        else:
            conditions.append("(scope='official' OR (scope='user' AND owner_id=?))")
            params.append(str(user["id"]))

    # 组织隔离（对齐 skills）：official（org_id=0）对所有组织可见
    _org_id = user.get("org_id", 0)
    if _org_id and scope not in ("repo", "user"):
        conditions.append("(org_id=0 OR org_id=?)")
        params.append(_org_id)

    q = (q or "").strip()
    if q:
        conditions.append("(name LIKE ? OR display_name LIKE ? OR description LIKE ? OR author LIKE ?)")
        kw = f"%{q}%"
        params.extend([kw, kw, kw, kw])

    if category_id:
        conditions.append("category_id=?")
        params.append(category_id)

    order_by = ("ORDER BY like_count DESC, updated_at DESC" if sort == "hot"
                else "ORDER BY updated_at DESC")
    where_sql = " WHERE " + " AND ".join(conditions)
    use_paging = page_size and page_size > 0

    plugins = []
    plugin_ids: list[int] = []
    cat_map = {}
    try:
        for cat in _list_plugin_categories():
            cat_map[cat["id"]] = cat["name"]
    except Exception:
        pass
    with db_lock:
        c = db.cursor()
        try:
            if use_paging:
                offset = (page - 1) * page_size
                c.execute(f"SELECT COUNT(*) FROM grape_t_plugin{where_sql}", tuple(params))
                total = c.fetchone()[0]
                c.execute(
                    f"SELECT id, name, version, display_name, description, author, like_count, "
                    f"scope, owner_id, category_id, updated_at, certification, namespace_owner "
                    f"FROM grape_t_plugin{where_sql} {order_by} LIMIT {int(page_size)} OFFSET {int(offset)}",
                    tuple(params))
            else:
                c.execute(
                    f"SELECT id, name, version, display_name, description, author, like_count, "
                    f"scope, owner_id, category_id, updated_at, certification, namespace_owner "
                    f"FROM grape_t_plugin{where_sql} {order_by}", tuple(params))
            for r in c.fetchall():
                cat_id = r[9] or 0
                card = _certification_projection(r[11])
                plugins.append({
                    "id": r[0],
                    "name": r[1],
                    "version": r[2] or "1.0.0",
                    "display_name": r[3] or r[1],
                    "description": r[4] or "",
                    "author": r[5] or "",
                    "type": "official" if r[7] == "official" else "repo" if r[7] == "repo" else "user-uploaded",
                    "scope": r[7] or "",
                    "like_count": r[6] or 0,
                    "category_id": cat_id,
                    "category_name": cat_map.get(cat_id, ""),
                    "certification": card["certification"],
                    "certified": card["certified"],
                    "namespace_owner": r[12] or "",
                    "updated_at": r[10] or "",
                })
                plugin_ids.append(r[0])
        except Exception:
            pass

    # 使用量（Issue: 卡片既展示点赞也展示使用量）——按「场景节点引用数」计：
    # devops_pipeline_scene.nodes_json 各节点 plugins 数组引用该插件的场景节点数。
    # 口径说明：以「场景节点引用」计数（而非用户数或 agent 调用次数）——
    # 用户数会低估（多场景共用一个插件只算一次/用户），agent 调用数依赖执行链路
    # 打点尚无稳定数据源；场景引用是当前可实时取准、语义最接近"被多少流水线在用"
    # 的口径，后续有执行打点后可叠加真实调用量。
    usage_map = {}
    try:
        import json as _uj
        with db_lock:
            _uc = db.cursor()
            for _r in _uc.execute("SELECT nodes_json FROM devops_pipeline_scene").fetchall():
                try:
                    _nodes = _uj.loads(_r[0]) if _r[0] else []
                except Exception:
                    continue
                for _n in (_nodes or []):
                    for _pn in ((_n.get("plugins") if isinstance(_n, dict) else None) or []):
                        _pn = str(_pn).strip()
                        if _pn:
                            usage_map[_pn] = usage_map.get(_pn, 0) + 1
    except Exception as _ue:
        from src.kernel.config import log as _ulog
        _ulog.warning(f"plugins usage_map build failed: {_ue}")

    # 批量查 liked_by_me（对齐 skills batch 模式，避免 N+1）
    liked_map = {}
    if plugin_ids and user.get("id"):
        from src.kernel.session_manager import db as _db, db_lock as _dl
        placeholders = ",".join(["?"] * len(plugin_ids))
        with _dl:
            c2 = _db.cursor()
            rows = c2.execute(
                f"SELECT plugin_id FROM grape_t_plugin_likes WHERE user_id=? AND plugin_id IN ({placeholders})",
                [str(user["id"])] + plugin_ids).fetchall()
        liked_ids = {r[0] for r in rows}
        liked_map = {pid: (pid in liked_ids) for pid in plugin_ids}
    for p in plugins:
        p["liked_by_me"] = liked_map.get(p["id"], False)
        p["usage_count"] = usage_map.get(p["name"], 0)

    if use_paging:
        return {"plugins": plugins, "total": total, "page": page, "page_size": page_size}
    return {"plugins": plugins}


@router.get("/categories", operation_id="plugins_categories", summary="List plugin categories",
            description="Lists plugin categories (grape_t_plugin_category table).")
def list_categories(user: dict = Depends(require_org_member)):
    """返回插件分类列表。"""
    return {"categories": _list_plugin_categories()}


@router.get("/by-id/{plugin_id}", operation_id="plugins_detail", summary="Plugin detail",
            description="Plugin detail incl. manifest content and zip_url (short-lived signed URL). "
                        "Access: official public, user=owner, repo=managed by caller.")
def get_plugin_detail(plugin_id: str, user: dict = Depends(get_current_user_or_api_key)):
    """返回插件详情（含 manifest JSON 与 zip_url 临时签名 URL）。

    权限校验：official 可见；user 需本人；repo 需当前用户托管该仓库（对齐 skills 详情）。
    """
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id, name, version, display_name, description, manifest, manifest_hash, "
            "category_id, author, like_count, object_key, scope, owner_id, created_by, "
            "content_hash, namespace_owner, certification, certified_by, certified_at, updated_at "
            f"FROM grape_t_plugin WHERE id=? AND status='{STATUS_ACTIVE}'",
            (plugin_id,)).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "插件未找到"})

    (pid, name, version, display_name, description, manifest_text, manifest_hash,
     category_id, author, like_count, object_key, scope, owner_id, created_by,
     content_hash, namespace_owner, certification, certified_by, certified_at,
     updated_at) = row

    # 归属校验（对齐 skills 详情；与下载共用同一出口）
    _ensure_plugin_visible(scope, owner_id, user)

    cat = _get_plugin_category(category_id) if category_id else None
    category_name = cat["name"] if cat else ""
    liked_by_me = _is_plugin_liked(user.get("id", 0), pid) if user.get("id") else False

    # zip_url：OBS 模式动态签发临时 URL（对象缺失返回空串，与 skills 详情一致）。
    # B2 复审收口：zip_url 与 /download 同为取包出口，签发前重算包指纹与入库
    # content_hash 恒时比对——不一致 409 拒发（不给绕过完整性校验的临时 URL）；
    # 存量空指纹放行（Spec 存量口径：下次上传补算）。
    zip_url = ""
    storage = _get_plugin_storage()
    if storage is not None and object_key:
        from src.domains.mgmt.plugin_market.security import verify_content_hash
        try:
            data = storage.read_bytes(object_key)
        except Exception:
            data = None
        if data is not None:
            if not verify_content_hash(content_hash, data):
                raise HTTPException(status_code=409, detail={
                    "code": 409,
                    "message": f"插件包完整性校验失败（content_hash 与包内容不一致，疑似被篡改）: "
                               f"{name}@{version}"})
            try:
                zip_url = storage.temporary_url(object_key) or ""
            except Exception:
                zip_url = ""

    cert = _certification_projection(certification)
    return {
        "id": pid,
        "name": name,
        "version": version or "1.0.0",
        "display_name": display_name or name,
        "description": description or "",
        "manifest": manifest_text or "{}",
        "manifest_hash": manifest_hash or "",
        "author": author or "",
        "created_by": created_by or "",
        "scope": scope or "",
        "type": "official" if scope == "official" else "repo" if scope == "repo" else "user-uploaded",
        "like_count": like_count or 0,
        "liked_by_me": liked_by_me,
        "category_id": category_id or "",
        "category_name": category_name,
        "zip_url": zip_url,
        "is_obs": bool(object_key),
        "content_hash": content_hash or "",
        "namespace_owner": namespace_owner or "",
        "certification": cert["certification"],
        "certified": cert["certified"],
        "certified_by": certified_by or "",
        "certified_at": certified_at or "",
        "updated_at": updated_at or "",
    }


@router.get("/by-id/{plugin_id}/download", operation_id="plugins_download",
            summary="Download plugin package with content_hash verification",
            description="B2 #1085 消费链路统一入口（下载/安装/物化取包）：从对象存储读包后"
                        "重算 sha256 指纹与入库 content_hash 比对——不一致 409 拒绝（防篡改），"
                        "对象缺失 404。可见性对齐详情：official 公开；user 仅本人；repo 托管成员。")
def download_plugin(plugin_id: str, user: dict = Depends(get_current_user_or_api_key)):
    """下载插件包（content_hash 完整性校验）。

    - 指纹一致 → 200 zip 字节（X-Content-Hash 回带入库指纹，供客户端二次校验）；
    - 存量空指纹（B1 存量口径：下次上传补算）放行；
    - 指纹不一致（存储被篡改）→ 409，篡改字节不落出。
    """
    from fastapi import Response
    from src.domains.mgmt.plugin_market.security import verify_content_hash, parse_plugin_name
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        row = db.execute(
            "SELECT name, version, object_key, content_hash, scope, owner_id "
            f"FROM grape_t_plugin WHERE id=? AND status='{STATUS_ACTIVE}'", (plugin_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "插件未找到"})
    name, version, object_key, content_hash, scope, owner_id = row

    # 可见性对齐详情端点（official 公开；user 仅本人；repo 托管成员；共用同一出口）
    _ensure_plugin_visible(scope, owner_id, user)

    if not object_key:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "插件包对象未登记"})
    storage = _get_plugin_storage()
    data = storage.read_bytes(object_key) if storage is not None else None
    if data is None:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "插件包对象缺失"})

    if not verify_content_hash(content_hash, data):
        raise HTTPException(status_code=409, detail={
            "code": 409,
            "message": f"插件包完整性校验失败（content_hash 与包内容不一致，疑似被篡改）: "
                       f"{name}@{version}"})

    bare_name = parse_plugin_name(name)[1] or name
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bare_name}-{version}.zip"',
            "X-Content-Hash": content_hash or "",
        })


class CertificationBody(BaseModel):
    certified: bool


@router.put("/by-id/{plugin_id}/certification",
            operation_id="plugins_set_certification",
            summary="Mark / revoke official certification (admin only)",
            description="B2 #1085 管理端认证位入口（D5 双轨）：标记/撤销官方认证并留痕"
                        "（grape_t_plugin_certification_audit）；撤销后列表/详情卡片标记同步消失。")
def set_plugin_certification(plugin_id: str, body: CertificationBody,
                             admin: dict = Depends(require_admin)):
    """认证位标记/撤销：certification = official（标记）/ none（撤销），操作留痕。"""
    from src.domains.mgmt.plugin_market.security import set_certification
    try:
        return set_certification(plugin_id, body.certified, admin.get("username", ""))
    except LookupError:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "插件未找到"})


@router.delete("/by-id/{plugin_id}", operation_id="plugins_market_delete",
               summary="Revoke shelf entry (soft delete)",
               description="Issue #1799 B1：市场侧条目软删——status=revoked + revoked_at 留痕，"
                           "行与对象包保留（台账可追溯），货架/详情/下载随之下架。"
                           "与 runtime 卸载 DELETE /api/plugins/{name}（注销已安装插件）语义不同、互不影响。"
                           "权限：user scope 仅本人；repo scope 需托管成员；official 货架为部署内置，"
                           "不支持市场侧软删（403）；重复删 409 显式冲突提示。")
def revoke_plugin_shelf_entry(plugin_id: str,
                              user: dict = Depends(require_no_session_token)):
    """市场货架条目软删（复用 plugin.status 列，零迁移）。

    - 越权（非 owner / 非托管成员 / official 条目）→ 403，条目保持 active；
    - 条目不存在 → 404；已 revoked 再删 → 409 显式冲突（提示重传恢复路径）；
    - 成功 → status=revoked + revoked_at/updated_at 留痕，重复条目处置不再直改数据库。

    TOCTOU 收口（QA S5）：状态检查与 UPDATE 同一 db_lock 段，UPDATE 附加
    status<>revoked 守卫——并发窗口内被抢先软删时 rowcount=0 → 显式 409，不双 200。
    """
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        row = _load_visible_anchor(db, plugin_id, user, write=True)
        _pid, name, version, _scope, _owner, status = row
        _conflict = {
            "code": 409,
            "message": f"插件 {name}@{version} 已处于 revoked（软删）状态，无需重复删除；"
                       f"台账保留，如需恢复可重新上传该版本"}
        if status == STATUS_REVOKED:
            raise HTTPException(status_code=409, detail=_conflict)
        cur = db.execute(
            f"UPDATE grape_t_plugin SET status='{STATUS_REVOKED}', revoked_at=datetime('now'), "
            f"updated_at=datetime('now') WHERE id=? AND status<>'{STATUS_REVOKED}'",
            (plugin_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=409, detail=_conflict)
        db.commit()
    log.info(f"plugin shelf entry revoked: id={plugin_id} {name}@{version} "
             f"operator={user.get('username') or user.get('id')}")
    return {"message": f"插件 {name}@{version} 已软删（revoked），台账保留",
            "plugin_id": plugin_id, "status": STATUS_REVOKED}


@router.get("/by-id/{plugin_id}/versions", operation_id="plugins_versions",
            summary="List all versions of a plugin (ledger)",
            description="Issue #1799 B1：返回某插件同 (name, scope, owner) 版本族全部记录"
                        "（active=生效 / superseded=被顶替历史 / revoked=软删台账），"
                        "生效版本排前，updated_at 降序。可见性对齐详情端点。")
def list_plugin_versions(plugin_id: str, user: dict = Depends(get_current_user_or_api_key)):
    """版本族全量列表：同名插件全部在架/历史版本可查（含软删台账状态）。"""
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        row = _load_visible_anchor(db, plugin_id, user)
        _pid, name, _anchor_version, scope, owner_id, _status = row
        rows = db.execute(
            "SELECT id, name, version, display_name, status, like_count, certification, "
            "namespace_owner, updated_at FROM grape_t_plugin "
            "WHERE name=? AND scope=? AND owner_id=? "
            f"ORDER BY (status='{STATUS_ACTIVE}') DESC, updated_at DESC, id DESC",
            (name, scope, owner_id)).fetchall()
    versions = []
    for r in rows:
        vid, vname, vversion, vdisplay, vstatus, vlikes, vcert, vns, vupdated = r
        card = _certification_projection(vcert)
        versions.append({
            "id": vid,
            "name": vname,
            "version": vversion or "1.0.0",
            "display_name": vdisplay or vname,
            "status": vstatus or STATUS_ACTIVE,
            "effective": vstatus == STATUS_ACTIVE,
            "like_count": vlikes or 0,
            "certification": card["certification"],
            "certified": card["certified"],
            "namespace_owner": vns or "",
            "updated_at": vupdated or "",
            "scope": scope,
            "owner_id": owner_id,
        })
    return {"plugin_id": plugin_id, "name": name, "scope": scope,
            "owner_id": owner_id, "total": len(versions), "versions": versions}


class RollbackBody(BaseModel):
    """回滚请求体：目标版本号（版本族内唯一键 (name, version, scope, owner) 定位）。"""
    version: str


@router.post("/by-id/{plugin_id}/rollback", operation_id="plugins_rollback",
             summary="Roll back effective version",
             description="Issue #1799 B1：版本族内切换生效版本——目标版本置 active，"
                         "其余 active 行降 superseded（历史版本保留可再回滚）。"
                         "revoked 目标不可回滚（409 显式冲突，提示重传恢复）。"
                         "权限对齐软删端点：user 本人；repo 托管成员；official 部署内置 403。")
def rollback_plugin_version(plugin_id: str, body: RollbackBody,
                            user: dict = Depends(require_no_session_token)):
    """生效版本切换（回滚）：以锚点条目定位版本族 (name, scope, owner)。

    TOCTOU 收口（QA S5）：锚点/目标检查与激活 UPDATE 同一 db_lock 段；
    激活 UPDATE 带 status<>revoked 守卫——竞态窗口内目标被软删 → rowcount=0
    → 409；收尾降权（_demote_other_active）保证版本族恒单 active。
    """
    from src.kernel.session_manager import db, db_lock

    target_version = (body.version or "").strip()
    if not target_version:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "version 不能为空"})

    with db_lock:
        row = _load_visible_anchor(db, plugin_id, user, write=True)
        _pid, name, _anchor_version, scope, owner_id, _status = row
        target = db.execute(
            "SELECT id, status FROM grape_t_plugin WHERE name=? AND version=? AND scope=? AND owner_id=?",
            (name, target_version, scope, owner_id)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail={
                "code": 404,
                "message": f"插件 {name} 在该作用域下不存在版本 {target_version}，"
                           f"可先 GET /api/plugins/by-id/{plugin_id}/versions 查看全部版本"})
        target_id, target_status = target
        if target_status == STATUS_REVOKED:
            raise HTTPException(status_code=409, detail={
                "code": 409,
                "message": f"版本 {target_version} 已被软删（revoked），不可回滚；"
                           f"如需恢复请重新上传该版本"})
        # 幂等判断下推 SQL（QA B6）：除目标外版本族内无其余 active 行且目标已生效 → noop
        other_active = db.execute(
            f"SELECT COUNT(*) FROM grape_t_plugin WHERE name=? AND scope=? AND owner_id=? "
            f"AND status='{STATUS_ACTIVE}' AND id<>?",
            (name, scope, owner_id, target_id)).fetchone()[0]
        if target_status == STATUS_ACTIVE and not other_active:
            return {"message": f"版本 {target_version} 已是生效版本，无需回滚",
                    "plugin_id": plugin_id, "effective_version": target_version,
                    "status": "noop"}
        cur = db.execute(
            f"UPDATE grape_t_plugin SET status='{STATUS_ACTIVE}', updated_at=datetime('now') "
            f"WHERE id=? AND status<>'{STATUS_REVOKED}'",
            (target_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=409, detail={
                "code": 409,
                "message": f"版本 {target_version} 已被软删（revoked），不可回滚；"
                           f"如需恢复请重新上传该版本"})
        # 目标置 active（superseded/其余 active 全部让位）
        _demote_other_active(db, name, scope, owner_id, target_id)
        db.commit()
    log.info(f"plugin rolled back: {name}@{target_version} (family scope={scope} "
             f"owner={owner_id}) operator={user.get('username') or user.get('id')}")
    return {"message": f"插件 {name} 生效版本已切换为 {target_version}（原生效版本转历史）",
            "plugin_id": plugin_id, "effective_version": target_version,
            "status": STATUS_ACTIVE}


@router.get("/search", operation_id="plugins_search", summary="Search plugins",
            description="Plugin lookup: exact name/display_name match first, then keyword match "
                        "on description/display_name/author.")
def search_plugins(q: str = "", user: dict = Depends(require_org_member)):
    """插件查找：先按 name/display_name 精确匹配；未命中则分词后按
    description + display_name + author 关键词匹配（对齐 skills search 模式，不调 LLM）。
    返回 [{plugin_id, name, version, display_name, description, author, category_name, match_reason}]。
    """
    from src.kernel.session_manager import db, db_lock

    q = (q or "").strip()
    if not q:
        return {"plugins": []}

    cat_map = {}
    try:
        for cat in _list_plugin_categories():
            cat_map[cat["id"]] = cat["name"]
    except Exception:
        pass

    # 1. name / display_name 精确匹配
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, name, version, display_name, description, author, category_id "
            f"FROM grape_t_plugin WHERE status='{STATUS_ACTIVE}' AND (name=? OR display_name=?) ORDER BY name",
            (q, q))
        exact_rows = c.fetchall()

    if exact_rows:
        return {"plugins": [
            {"plugin_id": r[0], "name": r[1], "version": r[2] or "1.0.0",
             "display_name": r[3] or r[1], "description": r[4] or "",
             "author": r[5] or "", "category_id": r[6] or 0,
             "category_name": cat_map.get(r[6] or 0, ""), "match_reason": "名称精确匹配"}
            for r in exact_rows
        ]}

    # 2. 关键词匹配：分词后匹配 description + display_name + author
    keywords = [w for w in re.split(r"[\s,，、/;；]+", q) if len(w) >= 2]
    if not keywords:
        keywords = [q]

    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, name, version, display_name, description, author, category_id "
            f"FROM grape_t_plugin WHERE status='{STATUS_ACTIVE}' ORDER BY name")
        all_rows = c.fetchall()

    results = []
    for r in all_rows:
        pid, name, version, display_name, desc, author, cat_id = r
        display_name = display_name or name
        desc = desc or ""
        hit_keywords = []
        searchable = f"{name} {display_name} {desc} {author}".lower()
        for kw in keywords:
            if kw.lower() in searchable:
                hit_keywords.append(kw)
        if hit_keywords:
            results.append({
                "plugin_id": pid, "name": name, "version": version or "1.0.0",
                "display_name": display_name, "description": desc,
                "author": author or "", "category_id": cat_id or 0,
                "category_name": cat_map.get(cat_id or 0, ""),
                "match_reason": f"匹配关键词：{', '.join(hit_keywords)}",
                "_hit_count": len(hit_keywords),
            })

    results.sort(key=lambda x: x["_hit_count"], reverse=True)
    for r in results:
        del r["_hit_count"]
    return {"plugins": results}


@router.post("/{plugin_id}/like", operation_id="plugins_like_toggle", summary="Toggle plugin like",
             description="Toggles a plugin like: liked -> unlike, not liked -> like.")
def toggle_plugin_like(plugin_id: str, user: dict = Depends(require_org_member)):
    """点赞 toggle：已赞→取消，未赞→点赞（grape_t_plugin_likes 唯一键 + like_count 原子增减）。"""
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        row = c.execute(f"SELECT 1 FROM grape_t_plugin WHERE id=? AND status='{STATUS_ACTIVE}'", (plugin_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "插件未找到"})
        uid = user.get("id", 0)
        liked_row = c.execute("SELECT 1 FROM grape_t_plugin_likes WHERE user_id=? AND plugin_id=?",
                              (uid, plugin_id)).fetchone()
        if liked_row:
            c.execute("DELETE FROM grape_t_plugin_likes WHERE user_id=? AND plugin_id=?", (uid, plugin_id))
            c.execute("UPDATE grape_t_plugin SET like_count=like_count-1 WHERE id=?", (plugin_id,))
            db.commit()
            liked = False
        else:
            c.execute("INSERT INTO grape_t_plugin_likes (id, user_id, plugin_id) VALUES (?, ?, ?)", (uuid7_str(), str(uid), str(plugin_id)))
            c.execute("UPDATE grape_t_plugin SET like_count=like_count+1 WHERE id=?", (plugin_id,))
            db.commit()
            liked = True
        like_count = c.execute("SELECT like_count FROM grape_t_plugin WHERE id=?", (plugin_id,)).fetchone()[0]
    return {"liked": liked, "like_count": like_count or 0}


def _extract_plugin_package(content: bytes, filename: str) -> tuple:
    """上传 zip → 解压 + 插件根定位 + manifest 解析（upload 与 dry-run 共用流水，
    QA 修复⑧：两入口 ~45 行复制提取为单一实现，400 口径完全一致）。

    校验链：大小上限 → zip 解压（含解压总量上限）→ 空包/杂项包 →
    manifest.json 所在目录定位（#1502 项3：插件根 = manifest 所在目录）→
    manifest JSON 解析。返回 (tmp_dir, plugin_dir, manifest_text, manifest)，
    调用方 finally 负责 shutil.rmtree(tmp_dir)。
    """
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "message": f"插件包不能超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB"})
    tmp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(tmp_dir, filename or "plugin.zip")
    with open(zip_path, "wb") as f:
        f.write(content)
    extract_dir = os.path.join(tmp_dir, "extracted")
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            total = sum((i.file_size or 0) for i in zf.infolist())
            if total > MAX_EXTRACT_BYTES:
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": f"ZIP 解压后总大小超过 {MAX_EXTRACT_BYTES // (1024 * 1024)}MB 上限"})
            zf.extractall(extract_dir)
    except HTTPException:
        raise
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "不是有效的 ZIP 文件，请上传 .zip 格式"})

    entries = os.listdir(extract_dir)
    if not entries:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "ZIP 文件为空"})
    # 过滤系统杂项目录（对齐 skills 上传链）
    real_entries = [e for e in entries if e not in ("__MACOSX", ".DS_Store", ".git") and not e.startswith(".")]
    if not real_entries:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "ZIP 中未找到插件目录"})

    # Issue #1502 项3：插件根判定 = manifest.json 所在目录（不再取第一个条目）。
    # 兼容根平铺与 <插件名>/ 单子目录前缀；多条 manifest.json 取最浅层级。
    manifest_path = None
    plugin_dir = extract_dir
    for _root, _dirs, _files in os.walk(extract_dir):
        _dirs[:] = [d for d in _dirs
                    if d not in ("__MACOSX", ".DS_Store", ".git") and not d.startswith(".")]
        if "manifest.json" in _files:
            manifest_path = os.path.join(_root, "manifest.json")
            plugin_dir = os.path.abspath(_root)
            break
    if not manifest_path:
        raise HTTPException(status_code=400, detail={
            "code": 400, "error_code": "PLUGIN_MANIFEST_MISSING",
            "message": "插件目录缺少 manifest.json（须位于插件目录根）"})
    try:
        with open(manifest_path, encoding="utf-8-sig") as f:
            manifest_text = f.read()
        manifest = json.loads(manifest_text)
    except Exception as e:
        raise HTTPException(status_code=400, detail={
            "code": 400, "error_code": "PLUGIN_MANIFEST_INVALID",
            "message": f"manifest.json 不是合法 JSON: {e}"})
    return tmp_dir, plugin_dir, manifest_text, manifest


@router.post("/upload", operation_id="plugins_upload", summary="Upload plugin (user personal / repo)",
             description="Uploads a plugin ZIP (manifest.json required in plugin dir). "
                         "scope=user stores a personal plugin; scope=repo requires repo. "
                         "L0 validation runs synchronously (A1 adapter; builtin minimal fallback when A1 not merged).")
async def upload_plugin(file: UploadFile = File(...), user: dict = Depends(get_current_user_or_api_key),
                        scope: str = Form("user"), repo: str = Form(""),
                        category_id: str = Form("0"),
                        source_repo: str = Form(""), source_ref: str = Form(""),
                        source_branch: str = Form("")):
    """上传插件 ZIP。scope=user 存个人插件；scope=repo 存仓库插件（需指定 repo）。

    manifest.json 必须位于插件目录根（必填字段 name/version，完整 schema 属 A1 #1079）。
    上传时同步 L0 校验（适配层），非法 manifest 4xx + 明确错误码，不入库。
    Issue #1798：声明 source_repo/source_ref（40 位 commit sha）时做来源一致性
    门控（scope=repo 必须声明；非 HEAD 产物 409 PLUGIN_SOURCE_MISMATCH 拦截）；
    source_branch 可选声明目标分支（缺省默认分支，release 分支打包不误拦）。
    """
    if scope not in ("user", "repo"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 user 或 repo"})
    if scope == "repo" and not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库插件必须指定 repo"})

    # 会话 token（tk_，agent）上传：仅支持 scope=repo，且仓库必须与 token 绑定仓库一致（对齐 skills #219 防越权写）
    if user.get("auth_type") == "session_token":
        if scope != "repo":
            raise HTTPException(status_code=403, detail={"code": 403, "message": "会话 token 仅支持上传仓库插件（scope=repo）"})
        from src.kernel.repo_key import split_owner_repo
        _tok_owner, _tok_repo = split_owner_repo(user.get("repo_key") or "")
        if not _tok_owner or split_owner_repo(repo) != (_tok_owner, _tok_repo):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权上传到该仓库插件（仓库作用域不匹配）"})

    # category_id 存在性校验（0/空=未分类；uuid 化后为字符串句柄）
    if str(category_id or "0") not in ("0", ""):
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            n = c.execute("SELECT COUNT(*) FROM grape_t_plugin_category WHERE id=? AND is_active=1",
                          (category_id,)).fetchone()[0]
        if not n:
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"分类 ID {category_id} 不存在"})

    content = await file.read()

    tmp_dir = ""
    try:
        tmp_dir, plugin_dir, manifest_text, manifest = _extract_plugin_package(
            content, file.filename)
        plugin_name = os.path.basename(plugin_dir)

        # L0 校验（适配层，上传时同步；非法不入库）
        file_names = []
        for _root, _dirs, _files in os.walk(plugin_dir):
            for _fn in _files:
                file_names.append(os.path.relpath(os.path.join(_root, _fn), plugin_dir).replace(os.sep, "/"))
        l0 = validate_plugin_l0(manifest, file_names)
        if not l0["ok"]:
            raise HTTPException(status_code=400, detail={
                "code": 400, "error_code": "PLUGIN_L0_INVALID",
                "message": "插件 L0 校验失败", "errors": l0["errors"],
                "validator": l0["validator"], "degraded": l0["degraded"]})

        m_name = str(manifest.get("name") or "").strip() or plugin_name
        m_version = str(manifest.get("version") or "").strip() or "1.0.0"
        display_name = str(manifest.get("display_name") or "").strip() or m_name
        description = str(manifest.get("description") or "").strip()
        author = str(manifest.get("author") or "").strip() or user.get("username", "")

        # ===== B2 #1085：@owner 命名空间防抢注（发布归属校验，越权 403 不入库）=====
        # 发布者身份：user scope = 账号 username；repo scope = 仓库 owner 段
        # （含会话 token 上传，对齐其 scope=repo 限制）。
        from src.domains.mgmt.plugin_market.security import ensure_namespace_ownership, NamespaceViolationError
        if scope == "repo":
            upload_repo_key = _resolve_repo_key(repo, strict=True)
            from src.kernel.repo_key import split_owner_repo
            ns_publisher = split_owner_repo(upload_repo_key)[0] or user.get("username", "")
        else:
            ns_publisher = user.get("username", "")
        try:
            ns_owner, bare_name = ensure_namespace_ownership(m_name, ns_publisher)
        except NamespaceViolationError as e:
            raise HTTPException(status_code=403,
                                detail={"code": 403, "message": str(e)})

        # ===== Issue #1798：「上架包 == 仓库 HEAD 产物」来源一致性门控 =====
        # 拦截先于任何存储写入（非 HEAD 产物不入对象存储不入库）
        consistency = await _check_source_consistency(
            scope, source_repo, source_ref, plugin_dir, manifest,
            source_branch=source_branch, user=user,
            upload_repo_key=(upload_repo_key if scope == "repo" else ""))

        from src.domains.mgmt.routes.skills import _normalize_skill_zip
        from src.kernel.session_manager import db, db_lock
        from src.kernel.config import log
        storage = _get_plugin_storage()
        if storage is not None:
            owner = _resolve_repo_key(repo, strict=True) if scope == "repo" else str(user["id"])
            # Issue #1798：来源字段回写（仅强校验通过的上传；unverified 写空串）
            if consistency.get("status") == "pass":
                src_repo = consistency.get("source_repo") or ""
                src_ref = consistency.get("source_ref") or ""
                src_hash = (consistency.get("canonical_hash") or {}).get("package") or ""
            else:
                src_repo = src_ref = src_hash = ""
            try:
                # 对象键以完整插件名派生（裸名与 @owner 名键空间隔离，#1085 复审），
                # 归属由 namespace_owner 列承载
                object_key = _plugin_key(scope, owner, m_name, m_version)
                zip_bytes = _normalize_skill_zip(plugin_dir, m_name)
                # 写序对齐 skills #444：save 前留存旧对象字节，upsert 失败回滚
                _prev_bytes = storage.read_bytes(object_key)
                storage.save_bytes(object_key, zip_bytes)
                content_hash = hashlib.sha256(zip_bytes).hexdigest()
                manifest_hash = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
                plugin_id = _upsert_plugin(m_name, m_version, display_name, description,
                                           manifest_text, manifest_hash, category_id,
                                           author, object_key, content_hash, scope,
                                           owner, str(user["id"]), ns_owner,
                                           source_repo=src_repo, source_ref=src_ref,
                                           source_content_hash=src_hash)

                try:
                    plugin_id = _upsert_plugin(m_name, m_version, display_name, description,
                                               manifest_text, manifest_hash, category_id,
                                               author, object_key, content_hash, scope,
                                               owner, str(user["id"]), ns_owner)
                except PluginUpsertConflict:
                    # Spec 决策表「条件不满足时显式冲突提示」分支：唯一键并发写入竞态
                    if _prev_bytes is not None:
                        storage.save_bytes(object_key, _prev_bytes)
                    raise HTTPException(status_code=409, detail={
                        "code": 409,
                        "message": f"插件 {m_name}@{m_version} 上传冲突：唯一键 "
                                    f"(name, version, scope, owner) 已被并发写入，请重试"})
                if not plugin_id:
                    if _prev_bytes is not None:
                        storage.save_bytes(object_key, _prev_bytes)
                    raise HTTPException(status_code=409, detail={
                        "code": 409, "message": f"插件 {m_name}@{m_version} 记录写入失败（同名冲突或并发上传）"})
                log.info(f"plugin uploaded (storage={storage.backend_name()}, key={object_key})")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=502, detail={"code": 502, "message": f"插件上传存储失败: {e}"})
            resp = {"message": f"插件 {m_name}@{m_version} 上传成功", "plugin_id": plugin_id,
                    "l0_validator": l0["validator"], "consistency": consistency}
            if l0["degraded"]:
                resp["l0_degraded_hint"] = l0_degraded_hint()
            # Issue #1795：L0 软提示（新插件首版建议 0.0.1）随成功响应返回，不阻断
            if l0.get("l0_warnings"):
                resp["l0_warnings"] = l0["l0_warnings"]
            return resp
        # 存储后端不可用（obs-only 且 OBS 未配置）
        raise HTTPException(status_code=503, detail={"code": 503,
                                                     "message": "对象存储未启用且本地存储被禁用，插件上传失败。请检查服务端存储配置后重试。"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"上传失败: {e}"})
    finally:
        import shutil
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


@router.post("/validate-consistency", operation_id="plugins_validate_consistency",
             summary="Dry-run source consistency check (no persist)",
             description="Issue #1798 dry-run：预上传自检「上架包 == 仓库 HEAD 产物」。"
                         "入参与 /upload 同（file + scope + source_repo + source_ref），"
                         "返回同一份 consistency 评估（diff 清单/告警），不写对象存储、"
                         "不落库。mismatch 在 dry-run 中不映射 409——自检输出以 200 + "
                         "consistency.status 呈现，由调用方（如 plugin-publish）决定处置。")
async def validate_plugin_consistency(file: UploadFile = File(...),
                                       user: dict = Depends(get_current_user_or_api_key),
                                       scope: str = Form("user"),
                                       source_repo: str = Form(""),
                                       source_ref: str = Form(""),
                                       source_branch: str = Form("")):
    """来源一致性 dry-run 自检（不落库即返回同一份 diff 结果，AC6）。

    与 /upload 共用同一评估内核（_evaluate_source_gate → evaluate_source_consistency）
    与同一来源声明契约（400 口径一致）；差异仅在：mismatch 不拦截、零存储/DB 写入。
    """
    if scope not in ("user", "repo"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 user 或 repo"})

    content = await file.read()

    tmp_dir = ""
    try:
        # zip→manifest 流水与 /upload 共用同一实现（QA 修复⑧：口径一致零复制）
        tmp_dir, plugin_dir, _manifest_text, manifest = _extract_plugin_package(
            content, file.filename)

        consistency = await _evaluate_source_gate(scope, source_repo, source_ref,
                                                 plugin_dir, manifest,
                                                 source_branch=source_branch,
                                                 user=user)
        return {"dry_run": True, "consistency": consistency}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"一致性校验失败: {e}"})
    finally:
        import shutil
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

# ═══ Issue #1089 挂点⑤：插件安装/卸载/捆绑技能（C1；与市场 API 共存，端点改名避让）═══

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, ConfigDict

from src.api.deps import require_org_member, require_no_session_token
from src.kernel.config import log


class PluginInstallBody(BaseModel):
    """安装请求体 = plugin.json 清单透传（QA Standards-7：Pydantic body）。

    清单字段权威校验归 registry.parse_plugin_manifest（credentials/
    bundled_skills 等结构化字段由其校验），这里 extra="allow" 保持
    plugin.json 全字段透传，仅约束必须为 JSON 对象。
    """
    model_config = ConfigDict(extra="allow")
    name: str = ""


@router.get("/installed", operation_id="plugins_runtime_list", summary="List plugins",
            description="Lists installed plugins (plugin.json manifests registered server-side, #1089).")
def list_runtime_plugins_api(user: dict = Depends(require_org_member)):
    from src.domains.mgmt.plugins.registry import list_plugins
    return {"plugins": list_plugins()}


@router.post("/install", operation_id="plugins_runtime_install", summary="Install plugin",
             description="Installs (registers) a plugin from its plugin.json manifest; bundled_skills linkage: missing skills are created and annotated, existing skills only annotated (#1089 hook ⑤).")
def install_runtime_plugin_api(body: PluginInstallBody, user: dict = Depends(require_no_session_token)):
    from src.domains.mgmt.plugins.registry import install_plugin
    out = install_plugin(body.model_dump())
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "插件清单校验失败: " + "; ".join(out.get("errors") or [])})
    return out


@router.get("/agent-defaults", operation_id="plugins_runtime_agent_defaults", summary="Agent-type default plugins",
            description="Issue #1089 hook ③ frontend selector data: agent-description.json agent.plugins per agent type (seed defaults pushed to runtimes via register desired_plugins).")
def runtime_plugins_agent_defaults_api(user: dict = Depends(require_org_member)):
    from src.domains.mgmt.plugins.resolver import _load_agent_description, get_agent_plugins
    try:
        types = sorted((_load_agent_description() or {}).keys())
    except Exception:
        types = []
    return {"defaults": [{"agent_type": t, "plugins": get_agent_plugins(t)} for t in types]}


@router.get("/{name}", operation_id="plugins_runtime_detail", summary="Plugin detail",
            description="Returns one installed plugin by name (credentials / bundled_skills resolved).")
def runtime_plugin_detail_api(name: str, user: dict = Depends(require_org_member)):
    from src.domains.mgmt.plugins.registry import get_plugin
    plugin = get_plugin(name)
    if not plugin:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"插件不存在: {name}"})
    return plugin


@router.get("/{name}/skills", operation_id="plugins_runtime_bundled_skills", summary="Plugin bundled skills",
            description="Returns the plugin's bundled_skills with skill-market rows (annotation state).")
def runtime_plugin_bundled_skills_api(name: str, user: dict = Depends(require_org_member)):
    from src.domains.mgmt.plugins.registry import get_plugin, plugin_skill_source_map
    plugin = get_plugin(name)
    if not plugin:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"插件不存在: {name}"})
    source_map = plugin_skill_source_map()
    skills = []
    for bs in plugin.get("bundled_skills") or []:
        sn = bs.get("skill_name", "")
        skills.append({
            **bs,
            "source_plugin": plugin.get("display_name") or name,
            "annotated": sn in source_map,
        })
    return {"plugin": name, "skills": skills}


@router.delete("/{name}", operation_id="plugins_runtime_uninstall", summary="Uninstall plugin",
               description="Uninstalls a plugin; bundled skills created by the plugin are removed, pre-existing skills keep but lose the source annotation (#1089 hook ⑤).")
def uninstall_runtime_plugin_api(name: str, user: dict = Depends(require_no_session_token)):
    from src.domains.mgmt.plugins.registry import uninstall_plugin
    out = uninstall_plugin(name)
    if not out.get("ok"):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "; ".join(out.get("errors") or ["插件不存在"])})
    log.info(f"plugin uninstalled via API: {name} operator={user.get('username') or user.get('id')}")
    return out


@router.post("/{name}/credentials", operation_id="plugins_runtime_save_credentials", summary="Save plugin credentials",
             description="Saves the current user's credentials for a plugin (user_credentials provider=plugin:{name}); consumed by the scene-apply credential negotiation (#1089 hook ①). Values are write-only; GET returns existence only.")
def save_runtime_plugin_credentials_api(name: str, body: dict, user: dict = Depends(require_no_session_token)):
    from src.domains.mgmt.user.db import set_credential
    from src.domains.mgmt.plugins.registry import get_plugin
    from src.kernel.session_manager import db as _cdb, db_lock as _cdblock
    if not get_plugin(name):
        # 归并缝补（B2×C1）：市场上架（plugin 表）与 C1 注册表（grape_t_plugin_registry）是两张表，
        # 市场插件未走 C1 安装流程时 registry 无行——回退查市场表
        with _cdblock:
            _mrow = _cdb.execute(f"SELECT id FROM grape_t_plugin WHERE name=? AND status='{STATUS_ACTIVE}'", (name,)).fetchone()
        if not _mrow:
            raise HTTPException(status_code=404, detail={"code": 404, "message": f"插件不存在: {name}"})
    config = (body or {}).get("config")
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "config 必须是对象（{凭据键: 值}）"})
    # 只保存插件声明过的凭据键（防注入未声明字段）
    # B2 市场插件的凭据声明从 manifest 解析（registry 无行时 get_plugin 返回 None）
    _mkt_manifest = {}
    with _cdblock:
        _mrow2 = _cdb.execute(f"SELECT manifest FROM grape_t_plugin WHERE name=? AND status='{STATUS_ACTIVE}'", (name,)).fetchone()
    if _mrow2 and _mrow2[0]:
        try: _mkt_manifest = json.loads(_mrow2[0])
        except Exception: pass
    _declared_creds = (get_plugin(name) or {}).get("credentials") or _mkt_manifest.get("credentials") or []
    declared_keys = {f.get("key") or f.get("name") for f in _declared_creds if f.get("key") or f.get("name")}
    cleaned = {str(k): ("" if v is None else str(v)) for k, v in config.items() if k in declared_keys}
    if not set_credential(user.get("id", 0), f"plugin:{name}", cleaned):
        raise HTTPException(status_code=500, detail={"code": 500, "message": "凭据保存失败"})
    log.info(f"plugin credentials saved: plugin={name} user={user.get('username') or user.get('id')} keys={sorted(cleaned.keys())}")
    return {"message": "凭据已保存", "configured_keys": sorted(cleaned.keys())}


@router.get("/{name}/config", operation_id="plugins_config_get", summary="Get my plugin param defaults",
             description="A4 配置继承链·个人档：读取当前用户对该插件的参数默认值（grape_t_plugin_config_overlays scope=personal）。未配置返回空对象。")
def get_plugin_config(name: str, user: dict = Depends(get_current_user_or_api_key)):
    from src.kernel.session_manager import db as _db, db_lock as _dblock
    with _dblock:
        row = _db.execute(
            "SELECT config_json FROM grape_t_plugin_config_overlays WHERE scope='personal' AND scope_key=? AND plugin_id=?",
            (str(user.get("id", 0)), name)).fetchone()
    return {"plugin": name, "config": json.loads(row[0]) if row else {}}


@router.put("/{name}/config", operation_id="plugins_config_put", summary="Save my plugin param defaults",
            description="A4 配置继承链·个人档：保存当前用户对该插件的参数默认值（仅写差异项，config 合并进既有 overlay）。任务运行时 Runner 从继承链现场解析。")
def put_plugin_config(name: str, body: dict, user: dict = Depends(get_current_user_or_api_key)):
    from src.kernel.session_manager import db as _db, db_lock as _dblock
    config = (body or {}).get("config")
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "config 必须是对象（{参数名: 默认值}）"})
    cleaned = {str(k): ("" if v is None else str(v)) for k, v in config.items()}
    with _dblock:
        c = _db.cursor()
        row = c.execute(
            "SELECT config_json FROM grape_t_plugin_config_overlays WHERE scope='personal' AND scope_key=? AND plugin_id=?",
            (str(user.get("id", 0)), name)).fetchone()
        merged = json.loads(row[0]) if row else {}
        merged.update(cleaned)
        if row:
            c.execute("UPDATE grape_t_plugin_config_overlays SET config_json=?, updated_by=? WHERE scope='personal' AND scope_key=? AND plugin_id=?",
                      (json.dumps(merged, ensure_ascii=False), str(user.get("username") or ""), str(user.get("id", 0)), name))
        else:
            c.execute("INSERT INTO grape_t_plugin_config_overlays (id, scope, scope_key, plugin_id, config_json, updated_by) VALUES (?,'personal',?,?,?,?)",
                      (uuid7_str(), str(user.get("id", 0)), name, json.dumps(merged, ensure_ascii=False), str(user.get("username") or "")))
        _db.commit()
    return {"message": "参数默认值已保存", "plugin": name, "config": merged}


@router.get("/{name}/credentials", operation_id="plugins_runtime_get_credentials", summary="Plugin credential existence",
            description="Returns which credential keys are configured for the current user (no values — privacy parity with /credentials).")
def get_runtime_plugin_credentials_api(name: str, user: dict = Depends(require_org_member)):
    from src.domains.mgmt.user.db import get_credential
    from src.domains.mgmt.plugins.registry import get_plugin
    from src.kernel.session_manager import db as _gdb, db_lock as _gdblock
    plugin = get_plugin(name)
    if not plugin:
        # B2×C1 归并缝补：市场插件（plugin 表）未走 C1 安装流程时 registry 无行
        with _gdblock:
            _grow = _gdb.execute(f"SELECT id FROM grape_t_plugin WHERE name=? AND status='{STATUS_ACTIVE}'", (name,)).fetchone()
        if not _grow:
            raise HTTPException(status_code=404, detail={"code": 404, "message": f"插件不存在: {name}"})
    cfg = get_credential(user.get("id", 0), f"plugin:{name}") or {}
    configured_keys = sorted([k for k, v in cfg.items() if str(v or "").strip()])
    # B2 市场插件不在 registry → plugin 可能 None，此时跳过 completeness 判定
    declared_required_keys = set()
    if plugin:
        declared_required_keys = {
            f.get("key") or f.get("name")
            for f in (plugin.get("credentials") or []) if f.get("required")
        }
    complete = declared_required_keys.issubset(set(configured_keys)) if declared_required_keys else None

    return {"plugin": name,
            "configured_keys": configured_keys,
            "complete": complete}
