"""统一 VCS API 客户端：跨平台 Issue/PR REST API 调用抽象。

支持 GitCode / GitHub / GitLab 三个平台，提供统一的 API 接口。
逐步替代 repogit.py 中分散的 API 调用。

参见 docs/weekly-02/issue-49-multi-vcs-extension-design.md §3.8
"""
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from src.kernel.config import log
# Issue #4064：日志统一脱敏入口（只作用于日志，请求与返回值不变）
from src.domains.runtime.vcs.redact import redact_text, redact_url

# Issue #3737：日志是凭据唯一泄露通道，URL 与上游 error body 写日志前一律先过
# redact_url / redact_text（单一出口收口在 vcs.redact，Issue #4064），随后再按
# _LOG_BODY_MAX 截断：既保留可排障的上游摘要，又避免整段 body 把日志撑爆。
# 先脱敏后截断：截断只会切到已掩码的 ***，不会把凭据切成半截片段。
_LOG_BODY_MAX = 500


# ── 建单幂等：open 标题索引缓存（Issue #3250）────────────────────────────────
# 非 github 平台没有标题过滤参数，只能 `state=open&per_page=50&page=N` 全量翻页：
# 积压越慢越贵（实测 grape-dev/grape 330 open = 7 页 4.5s；2000 open ≈ 30s+）。
# 按仓库缓存「完整 open 标题 → number」索引，TTL 内：
#   · 标题命中 → 直接返回；
#   · 索引完整且标题不在 → 直接判「不存在」，不再翻页；
# 新建成单成功后把标题写回缓存（remember_open_issue_title），批量造单不必每次翻页。
# 语义边界：TTL 内平台外新建的同名 issue 可能漏判（原实现同样不防并发建单，可接受）。
_OPEN_TITLE_TTL_SECONDS = float(os.environ.get("ISSUE_DEDUP_TITLE_CACHE_TTL", "60"))
_OPEN_TITLE_MAX_REPOS = int(os.environ.get("ISSUE_DEDUP_TITLE_CACHE_REPOS_MAX", "200"))
_open_title_cache: dict[tuple, dict] = {}
_open_title_lock = threading.Lock()


def _open_title_key(provider: str, owner: str, repo: str) -> tuple:
    return ((provider or "").lower(), (owner or "").lower(), (repo or "").lower())


def _open_title_get(provider: str, owner: str, repo: str) -> dict | None:
    """取未过期的仓库标题索引；过期即丢弃。"""
    key = _open_title_key(provider, owner, repo)
    with _open_title_lock:
        ent = _open_title_cache.get(key)
        if not ent:
            return None
        if time.time() - ent.get("ts", 0) > _OPEN_TITLE_TTL_SECONDS:
            _open_title_cache.pop(key, None)
            return None
        return ent


def _open_title_put(provider: str, owner: str, repo: str,
                    titles: dict, complete: bool) -> None:
    key = _open_title_key(provider, owner, repo)
    with _open_title_lock:
        if key not in _open_title_cache and len(_open_title_cache) >= _OPEN_TITLE_MAX_REPOS:
            # 简单容量上限：淘汰最旧的一条
            _oldest = min(_open_title_cache, key=lambda k: _open_title_cache[k].get("ts", 0))
            _open_title_cache.pop(_oldest, None)
        _open_title_cache[key] = {"titles": dict(titles), "complete": bool(complete),
                                  "ts": time.time()}


def remember_open_issue_title(provider: str, owner: str, repo: str, title: str,
                              number) -> None:
    """建单成功后把标题写回索引（只更新已存在的缓存条目，不新建）。"""
    if not title or number is None:
        return
    key = _open_title_key(provider, owner, repo)
    with _open_title_lock:
        ent = _open_title_cache.get(key)
        if ent is not None:
            ent["titles"][str(title).strip()] = number


def invalidate_open_issue_title_cache(provider: str, owner: str, repo: str) -> None:
    with _open_title_lock:
        _open_title_cache.pop(_open_title_key(provider, owner, repo), None)


# Issue #315: GitCode 编辑器上传图片（raw.gitcode.com/user-images/...）链接识别与鉴权下载。
# 该类资源匿名拉取 403，需带 token；提取为纯函数便于单测。
# D16 泛化：图片基址来自平台卡 user_images_base（空=该平台无此机制），
# gitcode 走默认卡；新平台（如 gitee）可配置自己的附件域名，零代码。
_USER_IMAGE_URL_PREFIX = "https://raw.gitcode.com/user-images/"


USER_IMAGE_MAX_SIZE = 20 * 1024 * 1024   # 单图 20MB 上限
USER_IMAGE_TIMEOUT = 15                  # 单图下载 15s 超时
USER_IMAGE_MAX_COUNT = 8                 # 单 Issue 最多采集 8 张（超出降级说明）

# Issue #1748: 评论附件（视频/图片）https://gitcode.com/user-attachments/...
# 该类资源匿名拉取 401，需带平台 token 下载。
_USER_ATTACHMENT_URL_RE = re.compile(r"https://gitcode\.com/user-attachments/[^\s\)\"'<>]+")
_USER_ATTACHMENT_URL_PREFIX = "https://gitcode.com/user-attachments/"
USER_ATTACHMENT_MAX_SIZE = 50 * 1024 * 1024    # 与视频理解 50MB 阈值一致，避免整段下载后才判超限
USER_ATTACHMENT_TIMEOUT = 60                   # 下载 60s 超时（视频较大）
# 单次评论最多采集的媒体附件数。取值依据（Issue #1748 复审，MR !1145 Critical-3）：
# 与同族 USER_IMAGE_MAX_COUNT（编辑器上传图片）对齐为 8，避免「同一评论里图片比编辑器
# 图片少采 2 个」的口径分裂；8 也是单条评论里人工可一次性上传/贴链的实际上限，
# 超出部分按「exceeds count limit」降级说明（不静默丢弃）。
USER_ATTACHMENT_MAX_COUNT = USER_IMAGE_MAX_COUNT
_USER_ATTACHMENT_ALLOWED_CTYPES = ("image/", "video/", "application/octet-stream")
# 单一扩展名白名单：落盘前必须命中，杜绝外部扩展名（.html/.php/.js 等）直通公开静态目录。
USER_ATTACHMENT_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
USER_ATTACHMENT_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv", ".flv"}
USER_ATTACHMENT_ALLOWED_EXTS = USER_ATTACHMENT_IMAGE_EXTS | USER_ATTACHMENT_VIDEO_EXTS
_CONTENT_TYPE_EXT_MAP = {
    "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
    "image/svg+xml": ".svg", "image/png": ".png", "image/bmp": ".bmp",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-msvideo": ".avi", "video/x-matroska": ".mkv",
}


def _user_images_base_for(provider: str) -> str:
    """平台编辑器图片附件基址：卡有值→卡值；卡存在但空→该平台无此机制（""）；
    无卡（异常/未注册）→ gitcode 默认（存量行为）。"""
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        card = get_platform_card(provider)
        if card is not None:
            return (card.user_images_base or "").rstrip("/") + "/"
    except Exception:
        pass
    return _USER_IMAGE_URL_PREFIX


def paginate_pages(fetch_page, page_size: int = 100, max_pages: int = 20) -> list | dict:
    """通用分页收集：``fetch_page(page)`` 返回列表或 ``{"error": ...}``。

    结果不足 ``page_size`` 或为空即停止；任一页返回 error 时原样返回 error。
    Issue #993：VcsApiClient.list_prs 与 branch_guard 的 grape CLI 查询共用，
    避免两处分页逻辑漂移。
    """
    items: list = []
    for page in range(1, max_pages + 1):
        part = fetch_page(page)
        if isinstance(part, dict) and part.get("error"):
            return part
        if not isinstance(part, list) or not part:
            break
        items.extend(part)
        if len(part) < page_size:
            break
    return items


def extract_user_image_urls(text: "str | None", provider: str = "gitcode") -> list[str]:
    """从文本提取平台编辑器图片附件 URL（按平台卡 user_images_base 派生正则）。

    覆盖形态：![alt](url)、<img src="url">、裸 URL。去重保序；空文本/平台无此机制返回 []。
    """
    if not text:
        return []
    _base = _user_images_base_for(provider)
    base = _base.rstrip("/")
    if not base:
        return []
    base = base + "/"
    _re = re.compile(re.escape(base) + r'[^\s\)"\'<>]+')
    return list(dict.fromkeys(_re.findall(text)))


def extract_gitcode_user_image_urls(text: "str | None") -> list[str]:
    """向后兼容包装：统一走 extract_user_image_urls(text, "gitcode")（D17 卡派生）。"""
    return extract_user_image_urls(text, "gitcode")


def _download_authed(url: str, token: str, *, allowed_prefix: str, ctype_prefixes: tuple,
                     max_size: int, timeout: int, accept: str = "", label: str = "asset",
                     ctype_error: str = "unsupported content-type") -> dict:
    """Issue #315/#1748 共用：带 token 下载白名单 URL 的通用实现（消除重复代码）。

    返回 {"content": bytes, "content_type": str, "size": int} 或 {"error": str}。
    token 仅构造 Authorization 请求头，绝不写入日志或返回值。
    """
    # D16 泛化语义由调用方承载：_download_user_image 的 allowed_prefix 取
    # _user_images_base_for(provider)（平台卡可配置附件基址），本通用实现只做
    # 参数化前缀校验（weekly-10 #315/#1748 共用口径，勿回退硬编码 user-images 检查）。
    if not url.startswith(allowed_prefix):
        return {"error": f"url not allowed (only {allowed_prefix})"}
    headers = {"Authorization": f"Bearer {token}"}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype_prefixes and not any(ctype.startswith(p) for p in ctype_prefixes):
                return {"error": f"{ctype_error}: {ctype or 'unknown'}"}
            chunks = []
            total = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_size:
                    return {"error": f"{label} exceeds size limit ({max_size} bytes)"}
                chunks.append(chunk)
            if total == 0:
                return {"error": f"empty {label} content"}
            content = b"".join(chunks)
            return {"content": content, "content_type": ctype, "size": total}
    except urllib.error.HTTPError as e:
        log.warning(f"{label} download failed: {redact_url(url)} -> HTTP {e.code}")
        return {"error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        _reason = getattr(e, "reason", e)
        if "timed out" in str(_reason).lower() or "timeout" in str(_reason).lower():
            log.warning(f"{label} download failed: {redact_url(url)} -> timeout ({timeout}s)")
            return {"error": "timeout"}
        log.warning(f"{label} download failed: {redact_url(url)} -> {redact_text(_reason)}")
        return {"error": str(_reason)}
    except Exception as e:
        log.warning(f"{label} download failed: {redact_url(url)} -> {redact_text(e)}")
        return {"error": str(e)}


def _download_user_image(url: str, token: str, max_size: int = USER_IMAGE_MAX_SIZE,
                         timeout: int = USER_IMAGE_TIMEOUT) -> dict:
    """带 token 下载单张 user-images 图片（内部实现，供 VcsApiClient 复用）。"""
    return _download_authed(
        url, token, allowed_prefix=_USER_IMAGE_URL_PREFIX, ctype_prefixes=("image/",),
        max_size=max_size, timeout=timeout, accept="image/*", label="image",
        ctype_error="non-image content-type")


def extract_gitcode_user_attachment_urls(text: "str | None") -> list[str]:
    """Issue #1748：从评论正文中提取 gitcode.com/user-attachments 附件 URL。

    覆盖形态：Markdown 链接/图片、<a href>/<img src>、裸 URL。
    去重保序（首个出现顺序）。纯函数，无 IO；text 为 None/空返回 []。
    """
    if not text:
        return []
    urls = _USER_ATTACHMENT_URL_RE.findall(text)
    return list(dict.fromkeys(urls))


def _url_ext(url: str) -> str:
    """取 URL 末段小写扩展名（忽略查询串），无扩展名返回空串。"""
    last = (url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return os.path.splitext(last)[1].lower()


def attachment_kind(url: str, content_type: str = "") -> str:
    """按 Content-Type / URL 扩展名判定附件类型：image / video / file。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("video/"):
        return "video"
    ext = _url_ext(url)
    if ext in USER_ATTACHMENT_VIDEO_EXTS:
        return "video"
    if ext in USER_ATTACHMENT_IMAGE_EXTS:
        return "image"
    return "file"


def is_media_attachment_url(url: str) -> bool:
    """已知扩展名时预先过滤非媒体附件（避免下载/转存 zip 等），未知扩展名放行待 Content-Type 判定。"""
    ext = _url_ext(url)
    return (not ext) or ext in USER_ATTACHMENT_ALLOWED_EXTS


def _attachment_ext(url: str, content_type: str = "", kind: str = "") -> str:
    """从 URL 末段/Content-Type 推断附件扩展名，仅接受媒体白名单；不合法返回空串。

    外部扩展名（.html/.php/.js 等）绝不透传，避免平台公开静态目录存储型 XSS；
    调用方拿到空串时应拒绝落盘。
    """
    ext = _url_ext(url)
    if ext in USER_ATTACHMENT_ALLOWED_EXTS:
        return ext
    ct = (content_type or "").split(";")[0].strip().lower()
    ct_ext = _CONTENT_TYPE_EXT_MAP.get(ct)
    if ct_ext in USER_ATTACHMENT_ALLOWED_EXTS:
        return ct_ext
    return ""


def _download_user_attachment(url: str, token: str, max_size: int = USER_ATTACHMENT_MAX_SIZE,
                              timeout: int = USER_ATTACHMENT_TIMEOUT) -> dict:
    """Issue #1748：带平台 token 下载评论附件（gitcode.com/user-attachments/...）。"""
    return _download_authed(
        url, token, allowed_prefix=_USER_ATTACHMENT_URL_PREFIX,
        ctype_prefixes=_USER_ATTACHMENT_ALLOWED_CTYPES, max_size=max_size, timeout=timeout,
        label="attachment", ctype_error="unsupported content-type")


def _dedup_error_text(data: dict) -> str:
    """D36/S56：查重失败错误文本——携带 token_error 可分类的 (NNN) 形态。

    传输层 error dict 形如 {"error": "HTTP 401"}；异常链的 401 识别
    （extract_http_status_from_exc）按 "(401)" 括号形态提取——本 helper
    把状态码转成该形态，保证 github bot token 401 命中「Bot Token 失效」
    文案分支而不是通用文案（is_auth_error_exc 契约，行为回归防线）。
    """
    import re as _re
    _err = str(data.get("error", ""))
    _m = _re.match(r"HTTP (\d{3})", _err)
    _code = f"({_m.group(1)})" if _m else ""
    return f"幂等查重失败（token 可能失效）: API request failed {_code}: {_err[:200]}"


def _native_issue_number(n):
    """D41/S61/#3055：issue number 保持平台原生形态——int 平台（GitCode/GitHub）
    返回 int；字符串 ident 平台（gitee "IKSQ4O"）返回 str（int() 强转
    会炸且丢形态）。转发内核唯一实现 `src.kernel.issue_ident.native_issue_number`。"""
    from src.kernel.issue_ident import native_issue_number
    return native_issue_number(n)


class VcsPathContractError(ValueError):
    """平台方言的路径契约被违反（例：gitlab 卡下拼出 /repos/ 前缀）。

    为什么用异常而不是告警：对方平台对不存在的路由会回 404，而 404 在业务层
    被解读成「资源不存在」——查重、探活、删除感知会全部静默失效（2026-11-20
    实锤：GitLab 重名预检恒返回「可用」）。宁可 500 暴露，也不要假阴性。
    """


class VcsApiClient:
    """统一 VCS API 客户端：Issue 评论 / PR 操作 / 文件读写。

    Args:
        provider: VCS 平台标识（gitcode/github/gitlab）
        token: API access token
        owner: 仓库所有者
        repo: 仓库名称
    """

    def __init__(self, provider: str, token: str, owner: str, repo: str):
        self.provider = provider
        self.token = token
        # S85/#466 收口：owner 路径段可带 provider 前缀（git-repo 代理面
        # github:owner/repo 形态），上游 URL 一律裸 owner——r5 livefire 实锤
        # gitee 建单拼出 /repos/gitee:example-user/issues 404。
        # 剥前缀统一走 repo_key.strip_provider_prefix（模块头铁律：业务代码
        # 禁止自行拼/剥前缀；S85 全仓派生清零——前缀集数据驱动，禁止字面量枚举）。
        from src.kernel.repo_key import strip_provider_prefix
        self.owner = strip_provider_prefix(owner)
        self.repo = repo

    @property
    def _project_path(self) -> str:
        """GitLab project id 路径段：owner 可含子组斜杠（grp/sub/proj），

        必须整体编码——只编码 repo 段会拼出 /projects/grp/sub%2Fproj，
        GitLab 要的是 /projects/grp%2Fsub%2Fproj，否则 404。
        其余平台（github/gitee/gitcode）没有子组，等价于旧拼法。
        """
        from urllib.parse import quote as _q
        return _q(f"{self.owner}/{self.repo}", safe="")

    # === Issue 评论 ===

    def _ledger(self, content_type: str, content_id, issue_number, body: str = "") -> None:
        """Issue #453 代发台账（写点8，客户端层收口）：写操作成功后入账，best-effort。

        在 VcsApiClient 四个写方法（评论/关issue/建PR/合PR）统一调用——所有调用方
        （git_repo_api / issues.py / issues_center / create_pr_for_repo）自动覆盖。
        content_id：字符串/数字 ID（评论 ID / issue 编号 / PR 编号）。
        """
        try:
            from src.kernel.outbound_ledger import record_outbound, content_hash_md5
            from src.kernel.repo_key import to_repo_key
            if content_id is None or str(content_id) == "":
                return
            _rk = to_repo_key(f"{self.owner}/{self.repo}", self.provider)
            record_outbound(self.provider, _rk, content_type, str(content_id),
                            issue_number=str(issue_number or ""),
                            content_hash=content_hash_md5(body) if body else "")
        except Exception as e:
            from src.kernel.config import log
            log.warning(f"VcsApiClient ledger record failed (non-blocking): {e}")

    def post_issue_comment(self, issue_number: int, body: str) -> dict:
        """在 Issue 上发表评论。"""
        # D22：全平台统一（方言决定路径：gitlab 走 notes，其余走 comments）
        _resp = self._request("POST", self._generic_path(
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
            f"/issues/{issue_number}/notes"), {"body": body})
        # Issue #453 台账：响应 dict 里的评论 ID 入账（无 ID 时记 issue_number 兼值）
        if isinstance(_resp, dict) and "error" not in _resp:
            from src.kernel.outbound_ledger import extract_content_id
            _cid = extract_content_id(_resp, "issue_comment")
            self._ledger("issue_comment", _cid or issue_number, issue_number, body)
        return _resp

    def list_issue_comments(self, issue_number: int, per_page: int = 100,
                            max_pages: int = 20, timeout: int = 30) -> list:
        """获取 Issue 评论列表（Issue #1739：门禁标注跨进程幂等去重用）。

        分页拉取全部评论：只取首页会让评论超过 per_page 的 Issue 漏查旧标记，
        导致跨进程去重失效、重复标注。上游忽略 page（返回同一页）或空页即停止，
        最多 ``max_pages`` 页，避免异常上游无界翻页。

        Returns:
            list: 评论列表；失败返回已取到的部分（保持 best-effort 语义）。
        """
        # D22 收敛：垫片族已删，三平台与其他平台统一走 _request 卡驱动；
        # 方言派生路径（gitlab → project 编码 + notes；其余 → github 形态）逐字节保留。
        _out: list = []
        _prev = None
        for _page in range(1, max_pages + 1):
            _resp = self._request("GET", self._generic_path(
                f"/repos/{self.owner}/{self.repo}/issues/{issue_number}"
                f"/comments?per_page={per_page}&page={_page}",
                f"/issues/{issue_number}/notes?per_page={per_page}&page={_page}"),
                timeout=timeout)
            if not isinstance(_resp, list) or not _resp:
                break
            if _resp == _prev:
                break
            _prev = _resp
            _out.extend(_resp)
            if len(_resp) < per_page:
                break
        return _out

    def list_pr_comments(self, pr_number: int, per_page: int = 100,
                         max_pages: int = 20, timeout: int = 30) -> list:
        """获取 PR 评论列表（分页全量，Issue #2816）。

        路径由平台卡 ``pr_comment_endpoint`` 派生：github 系 PR 与 Issue 共用
        ``/issues/{n}/comments``；gitcode/gitee 系用 ``/pulls/{n}/comments``；
        gitlab 方言用 ``/merge_requests/{n}/notes``。分页策略与
        ``list_issue_comments`` 一致（空页/重复页/短页停止，最多 max_pages）。
        """
        card = self._platform_card()
        if card is None:
            return []
        if card.dialect == "gitlab":
            _base = f"/projects/{self._project_path}/merge_requests/{pr_number}/notes"
        elif (getattr(card, "pr_comment_endpoint", "") or "pulls") == "issues":
            _base = f"/repos/{self.owner}/{self.repo}/issues/{pr_number}/comments"
        else:
            _base = f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/comments"
        _out: list = []
        _prev = None
        for _page in range(1, max_pages + 1):
            _resp = self._request(
                "GET", f"{_base}?per_page={per_page}&page={_page}", timeout=timeout)
            if not isinstance(_resp, list) or not _resp:
                break
            if _resp == _prev:
                break
            _prev = _resp
            _out.extend(_resp)
            if len(_resp) < per_page:
                break
        return _out

    # === Issue 查询 ===


    def create_issue(self, title: str, body: str = "", assignee: str = "") -> dict:
        """创建 Issue（D20/S31，命令面补齐：卡驱动路径+鉴权，方言 assignee 语义）。"""
        # D22：全平台统一走卡路径（gitcode/gitee form 语义、github JSON+token 头，
        # gitlab 走 projects/issues——后者为此前 unsupported 的新增能力）
        card = self._platform_card()
        if card is None:
            return self._unsupported()
        if self._dialect_is_gitlab():
            data: dict = {"title": title, "description": body}
            # Issue #3580 复审：GitLab 只认数值 assignee_ids——用户名先解析成 id，
            # 解析不到就放弃指派（不把用户名原样塞进 assignee_id 让上游 400/静默忽略）。
            _ids = self._gitlab_assignee_ids(assignee)
            if _ids:
                data["assignee_ids"] = _ids
        else:
            data = {"title": title, "body": body}
            if assignee:
                data["assignee"] = assignee
        _form = card.api_auth == "query_token"  # Gitee 系 v5 form 实测语义
        # D41/S61：issue 写端点形态按卡能力位派生（gitee 实弹观测：
        # POST /repos/{owner}/issues + repo 入 body，非 repo_path）
        if card.issue_write_form == "owner_param":
            data = dict(data)
            data["repo"] = self.repo
            return self._request("POST", f"/repos/{self.owner}/issues", data, form=_form)
        return self._request("POST", self._generic_path(
            f"/repos/{self.owner}/{self.repo}/issues", "/issues"), data, form=_form)

    def get_issue(self, issue_number: int, timeout: float = 30.0) -> dict:
        """获取 Issue 详情。"""
        return self._request("GET", self._generic_path(
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
            f"/issues/{issue_number}"), timeout=timeout)

    # === Issue 关闭 ===

    def close_issue(self, issue_number: int) -> dict:
        """关闭 Issue（设置 state=closed）。

        Returns:
            dict: API 返回的 Issue JSON，或 {"error": ...} 错误信息。
        """
        # D22：关闭语义按方言/鉴权派生（无平台名分支）：
        # gitlab → PUT state_event；Gitee 系 v5（query_token）→ state=close+repo+title；
        # github → state=closed
        card = self._platform_card()
        if card is None:
            return self._unsupported()
        if self._dialect_is_gitlab():
            _resp = self._request("PUT", self._generic_path(
                "", f"/issues/{issue_number}"), {"state_event": "close"})
        elif getattr(card, "issue_write_form", "repo_path") == "owner_param":
            # D41/S61：gitee 实弹观测形态——PATCH /repos/{owner}/issues/{ident}
            # + form {state: closed, repo: 仓名, title}（读路径仍 repo_path）
            try:
                _info = self.get_issue(issue_number)
                _title = str(_info.get("title") or "") if isinstance(_info, dict) else ""
            except Exception:
                _title = ""
            _resp = self._request("PATCH", f"/repos/{self.owner}/issues/{issue_number}",
                                  {"state": "closed", "repo": self.repo, "title": _title}, form=True)
        elif card.api_auth == "query_token":
            try:
                _info = self.get_issue(issue_number)
                _title = str(_info.get("title") or "") if isinstance(_info, dict) else ""
            except Exception:
                _title = ""
            _resp = self._request("PATCH", self._generic_path(
                f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
                f"/issues/{issue_number}"),
                {"state": "close", "repo": f"{self.owner}/{self.repo}", "title": _title})
        else:
            _resp = self._request("PATCH", self._generic_path(
                f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
                f"/issues/{issue_number}"), {"state": "closed"})
        # Issue #453 台账（写点10）：关闭 issue 入账——issue_closed 事件回流对账
        if isinstance(_resp, dict) and "error" not in _resp:
            self._ledger("issue_close", issue_number, issue_number)
        return _resp

    # === Issue 查重（D36/S56 端口方法）===

    def find_open_issue_by_title(self, title: str, max_pages: int = 1000) -> "int | None":
        """按标题查找同名 open Issue，返回其 number（无则 None）。

        D36/S56：策略归适配器内部——github 走 Search API 精确匹配
        （in:title + repo scope，#344 语义）；其余平台（gitee/gitcode 等）
        分页遍历（while 靠末页不足 50 条终止，#344 取消页上限语义）。
        传输 error（含 token 失效 401）抛 RuntimeError——不静默放行
        重复建单（#343 契约）。调用方零平台分支（端口-适配器形态）。
        """
        try:
            return self._find_open_issue_impl(title, max_pages)
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"幂等查重失败（token 可能失效）: {e}")

    def _find_open_issue_impl(self, title: str, max_pages: int) -> "int | None":
        import urllib.parse as _up
        # D36/S56：策略按卡能力位分派（issue_search），不比平台名——
        # 有搜索端点的平台走精确匹配，无则分页遍历（第 5 平台配卡即得）
        _card = self._platform_card()
        if _card is not None and _card.issue_search:
            # 搜索端点策略：Search API 精确匹配（in:title + repo scope，#344 语义）
            _q = _up.quote(f'{title} in:title repo:{self.owner}/{self.repo} is:issue is:open')
            data = self._request("GET", f"/search/issues?q={_q}&per_page=5")
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(_dedup_error_text(data))
            if not isinstance(data, dict) or "items" not in data:
                _msg = data.get("message", "") if isinstance(data, dict) else str(data)[:200]
                if _msg:
                    raise RuntimeError(f"幂等查重失败（GitHub API 异常）: {_msg}")
                return None
            for it in (data.get("items") or []):
                if isinstance(it, dict) and str(it.get("title") or "").strip() == title.strip():
                    n = it.get("number")
                    if n:
                        return _native_issue_number(n)
            return None
        # 非 github：分页遍历（形态兼容 {data:[]}/items/records 与 list）
        # Issue #3250：先查短 TTL 的仓库标题索引；命中/完整索引判无 → 不再翻页。
        _title = title.strip()
        _cached = _open_title_get(self.provider, self.owner, self.repo)
        if _cached is not None:
            if _title in _cached["titles"]:
                return _cached["titles"][_title]
            if _cached.get("complete"):
                return None
        _seen: dict = dict(_cached["titles"]) if _cached else {}
        _found = None
        page = 1
        _complete = False
        while page <= max_pages:
            # 2026-11-22：路径按卡方言派生（此前硬编码 /repos/… 对 gitlab 是
            # 不存在的路由，恒 404 → 查重被误报成「token 可能失效」）。
            # GitLab 侧 state 取值是 opened（不是 open），列表返回裸数组。
            data = self._request(
                "GET",
                self._generic_path(
                    f"/repos/{self.owner}/{self.repo}/issues?state=open&per_page=50&page={page}",
                    f"/issues?state=opened&per_page=50&page={page}",
                ))
            if isinstance(data, dict):
                if "error" in data:
                    raise RuntimeError(_dedup_error_text(data))
                _cand = data.get("data") if isinstance(data.get("data"), list) else (
                    data.get("items") or data.get("records") or [])
                items = _cand or []
            else:
                items = data if isinstance(data, list) else []
            if not items:
                _complete = True
                break
            for it in items:
                if not isinstance(it, dict):
                    continue
                _t = str(it.get("title") or "").strip()
                st = str(it.get("state") or "").lower()
                if not _t or st not in ("open", "opened", "active", "1", "todo"):
                    continue
                n = it.get("number") or it.get("iid") or it.get("id")
                if not n:
                    continue
                _seen.setdefault(_t, _native_issue_number(n))
                if _t == _title and _found is None:
                    _found = _native_issue_number(n)
                    break
            if _found is not None:
                break   # 早退：索引不完整（complete=False），仅用于同名复用
            if len(items) < 50:
                _complete = True
                break
            page += 1
        _open_title_put(self.provider, self.owner, self.repo, _seen, _complete)
        return _found  # type: ignore[return-value]

    def get_issue_state(self, issue_number: int) -> str:

        """获取 Issue 当前 state（open/closed），失败返回空串。"""
        resp = self.get_issue(issue_number)
        if isinstance(resp, dict) and "error" not in resp:
            return str(resp.get("state", "")).lower()
        return ""

    # === PR 操作 ===

    def create_pr(self, title: str, head: str, base: str, body: str = "") -> dict:
        """创建 Pull Request。"""
        data = {"title": title, "head": head, "base": base, "body": body}
        _resp = self._request("POST", self._generic_path(
            f"/repos/{self.owner}/{self.repo}/pulls", "/merge_requests"), data)
        # Issue #453 台账（写点11）：建 PR 入账——PR 编号（number/iid）为对账键
        if isinstance(_resp, dict) and "error" not in _resp:
            from src.kernel.outbound_ledger import extract_content_id
            self._ledger("pr", extract_content_id(_resp, "issue"), "", body)
        return _resp

    def merge_pr(self, pr_number: int, merge_method: str = "", title: str = "") -> dict:
        """合并 Pull Request。

        Issue #558：merge_method（merge/squash/rebase）与 title（自定义合并提交标题）
        可选透传；缺省空值不进 body（存量行为不变）。GitLab 无 merge_method 字段，
        squash 映射为 {"squash": True}。
        """
        _body = {k: v for k, v in {"merge_method": (merge_method or "").strip(),
                                   "title": (title or "").strip()}.items() if v}
        # D22：gitlab 方言 squash 语义保留，其余透传 merge_method
        if self._dialect_is_gitlab():
            _gl_body = {"squash": True} if _body.get("merge_method") == "squash" else {}
            _resp = self._request("PUT", self._generic_path(
                "", f"/merge_requests/{pr_number}/merge"), _gl_body)
        else:
            _resp = self._request("PUT", self._generic_path(
                f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/merge",
                f"/merge_requests/{pr_number}/merge"), _body or {})
        # Issue #453 台账（写点11）：合并 PR 入账——pr_merge 事件回流对账
        if isinstance(_resp, dict) and "error" not in _resp:
            self._ledger("pr_merge", pr_number, "")
        return _resp

    def get_pr(self, pr_number: int) -> dict:
        """获取 PR 详情。"""
        return self._request("GET", self._generic_path(
            f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}",
            f"/merge_requests/{pr_number}"))

    # === 仓库信息 ===


    def list_branches(self, max_pages: int = 5, per_page: int = 100,
                      with_truncation: bool = False):
        """分支列表（D13 命令面，卡驱动分页）：[{name, protected}]。

        ``with_truncation=True`` 时返回 ``(branches, truncated)``：达到分页上限仍满页，
        说明列表被截断，调用方不能把「未命中」当作「分支确实不存在」（#3758）。
        查询失败时 ``_request`` 返回 ``{"error": ...}``，此处显式抛出 ``RuntimeError``，
        让调用方按预期运行期失败处理，而不是静默得到空列表。
        """
        branches: list[dict] = []
        if self._dialect_is_gitlab():
            path = f"/projects/{self._project_path}/repository/branches"
        else:
            path = f"/repos/{self.owner}/{self.repo}/branches"
        page = 1
        while page <= max_pages:
            data = self._card_or_legacy_get(f"{path}?per_page={per_page}&page={page}")
            if isinstance(data, dict) and data.get("error"):
                # 兼容旧调用方：默认形态失败返回空列表；需要区分「失败 vs 空仓库」的
                # 调用方（with_truncation=True）显式拿到 RuntimeError。
                if with_truncation:
                    raise RuntimeError(str(data.get("error")))
                break
            if not isinstance(data, list) or not data:
                break
            for b in data:
                branches.append({"name": b.get("name", ""),
                                 "protected": bool(b.get("protected", False))})
            if len(data) < per_page:
                return (branches, False) if with_truncation else branches
            page += 1
        truncated = page > max_pages
        return (branches, truncated) if with_truncation else branches

    def get_default_branch(self) -> str:
        """读取仓库默认分支（方言正确，gitlab 走 project 编码路径）。

        ``get_repo_info`` 的 gitlab 分支历史上返回 ``/repos/...``（GitLab 404），
        导致默认分支读取为空并让回退落到任意分支（#3758 R3）。这里显式按方言
        选择路径，取不到时返回空串，由调用方决定「不回退任意分支」。
        """
        if self._dialect_is_gitlab():
            path = f"/projects/{self.owner}%2F{self.repo}"
        else:
            path = f"/repos/{self.owner}/{self.repo}"
        info = self._request("GET", path)
        if isinstance(info, dict) and not info.get("error"):
            return str(info.get("default_branch") or "")
        return ""

    def list_prs(self, state: str = "all", head: str = "",
                 max_pages: int = 20) -> list | dict:
        """分页查询 PR 列表（Issue #993：按 head 分支查 merged/closed 状态）。

        ``per_page=100`` 且全量翻页，避免 PR 超过单页时漏掉 merged PR。返回列表；
        查询失败返回 ``{"error": ...}``。#1739 分页契约（短页即停）不变。
        D22 收敛：垫片族已删，全平台统一走 _request 卡驱动（gitlab 方言 →
        merge_requests + project 编码路径；github 方言 → pulls 形态），head
        过滤仅 github 方言（存量 #993 口径：gitcode/github 卡方言均为 github）。
        """
        _card = self._platform_card()
        if _card is None:
            return {"error": f"unsupported provider: {self.provider}"}
        if self._dialect_is_gitlab():
            def _fetch(page):
                path = (f"/projects/{self._project_path}/merge_requests"
                        f"?state={state}&per_page=100&page={page}")
                return self._request("GET", path)
        else:
            # head 过滤形态按卡 pr_head_filter（owner_ref=GitHub "owner:branch" 缺省；
            # bare_ref=v5 家族裸 ref——gitcode 实弹实测 owner:ref 返回空列表，发错
            # 形态会把列表过滤成空导致 branch_guard fail-open）
            _head_val = (f"{self.owner}:{head}"
                         if getattr(_card, "pr_head_filter", "owner_ref") == "owner_ref"
                         else head)
            _head_q = f"&head={_head_val}" if head else ""
            def _fetch(page):
                path = (f"/repos/{self.owner}/{self.repo}/pulls?state={state}"
                        f"{_head_q}&per_page=100&page={page}")
                return self._request("GET", path)
        return paginate_pages(_fetch, page_size=100, max_pages=max_pages)

    def _dialect_is_gitlab(self) -> bool:
        card = self._platform_card()
        return bool(card is not None and card.dialect == "gitlab")

    def _gitlab_assignee_ids(self, assignee: str) -> list:
        """GitLab 指派：用户名 → assignee_ids（Issue #3580 复审，对齐 PR #1854）。

        GitLab 写接口只认**数值 user id**（`assignee_ids`），传用户名会被上游 400/忽略。
        解析序：纯数字入参直接采用（历史调用点传 id 的形态零回归）→ 否则
        `GET /users?username=<名字>` 精确匹配后取 id。解析不到只告警并放弃指派，
        不阻断建 Issue（指派是可选语义，PR 上游同口径）。
        """
        _raw = str(assignee or "").strip()
        if not _raw:
            return []
        if _raw.isdigit():
            return [int(_raw)]
        from urllib.parse import quote as _quote
        try:
            users = self._request("GET", f"/users?username={_quote(_raw)}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"gitlab assignee resolve failed ({_raw}): {e}")
            return []
        for u in users if isinstance(users, list) else []:
            if not isinstance(u, dict):
                continue
            if str(u.get("username", "")).lower() == _raw.lower() and u.get("id"):
                return [int(u["id"])]
        log.warning(f"gitlab assignee not found: {_raw}")
        return []

    def create_branch(self, branch_name: str, refs: str = "main") -> dict:
        """D44/S66：创建分支（方言模板，零平台名分支）。

        - gitlab 系：POST /projects/{o}%2F{r}/repository/branches（branch/ref 参数）
        - branch_create_form=refs_sha（GitHub 存量形态）：查 base 分支 sha → POST /git/refs
        - 其余（form，Gitee 系 v5 存量形态）：POST /repos/{o}/{r}/branches {branch_name, refs}

        错误形态与其他命令一致：返回 {"error": "HTTP <code>", "status_code": <code>}，
        调用方按 status_code 映射（409/422=已存在、403=权限，与存量路由文案对齐）。
        """
        card = self._platform_card()
        if self._dialect_is_gitlab():
            return self._card_request("POST",
                f"/projects/{self._project_path}/repository/branches",
                {"branch": branch_name, "ref": refs}, form=True)
        if card is not None and getattr(card, "branch_create_form", "form") == "refs_sha":
            base = refs or "main"
            info = self._card_request("GET", f"/repos/{self.owner}/{self.repo}/branches/{base}")
            sha = ((info or {}).get("commit") or {}).get("sha", "") if isinstance(info, dict) else ""
            if not sha:
                rinfo = self._card_request("GET", f"/repos/{self.owner}/{self.repo}")
                default_branch = (rinfo or {}).get("default_branch", "main") if isinstance(rinfo, dict) else "main"
                info = self._card_request("GET",
                    f"/repos/{self.owner}/{self.repo}/branches/{default_branch}")
                sha = ((info or {}).get("commit") or {}).get("sha", "") if isinstance(info, dict) else ""
            if not sha:
                return {"error": "no base sha", "status_code": 502}
            return self._card_request("POST", f"/repos/{self.owner}/{self.repo}/git/refs",
                {"ref": f"refs/heads/{branch_name}", "sha": sha})
        return self._card_request("POST", f"/repos/{self.owner}/{self.repo}/branches",
            {"branch_name": branch_name, "refs": refs}, form=True)

    def get_file(self, path: str, ref: str = "") -> dict:
        """读取仓库任意文本文件（contents API，base64 解码）。

        供仓库地图读取仓库自维护的 CAPABILITIES.md / REPO_MAP.md 等能力清单。
        返回 {"content": <decoded>, "endpoint": <命中端点>}；未命中返回空 content。
        """
        path = str(path or "").strip().lstrip("/")
        if not path:
            return {"content": "", "endpoint": ""}
        import base64
        from urllib.parse import quote

        if self._dialect_is_gitlab():
            rinfo = self._card_request("GET", f"/projects/{self._project_path}")
            default_branch = (
                (rinfo or {}).get("default_branch", "main")
                if isinstance(rinfo, dict) else "main"
            )
            branch = ref or default_branch
            data = self._card_request(
                "GET",
                f"/projects/{self._project_path}/repository/files/"
                f"{quote(path, safe='')}?ref={branch}",
            )
            content = (data or {}).get("content", "") if isinstance(data, dict) else ""
            if content:
                try:
                    return {"content": base64.b64decode(content).decode("utf-8"),
                            "endpoint": "gitlab_file"}
                except Exception:
                    return {"content": "", "endpoint": ""}
            return {"content": "", "endpoint": ""}

        suffix = f"?ref={ref}" if ref else ""
        data = self._card_request(
            "GET", f"/repos/{self.owner}/{self.repo}/contents/{path}{suffix}")
        if not isinstance(data, dict) or data.get("error"):
            return {"content": "", "endpoint": ""}
        content = data.get("content", "")
        if not content:
            return {"content": "", "endpoint": ""}
        try:
            return {"content": base64.b64decode(content).decode("utf-8"),
                    "endpoint": "contents"}
        except Exception:
            return {"content": "", "endpoint": ""}

    def get_readme(self) -> dict:
        """D44/S66：拉取仓库 README（读侧方言模板：形态按卡，token=构造时传入）。

        探测链（无平台名分支）：
        ① /repos/{o}/{r}/readme（GitHub 系原生端点，{content: base64}）
        ② /repos/{o}/{r}/contents/README.md（Gitee 系 v5，{content: base64}）
        ③ /repos/{o}/{r}/contents/readme.md（大小写 fallback，存量行为）
        gitlab 系走 /projects/{o}%2F{r}/repository/files/README.md?ref=<default>。

        返回 {"content": <decoded>, "endpoint": <命中端点>}；全部未命中返回
        {"content": "", "endpoint": ""}（调用方语义=空 README）。
        """
        if self._dialect_is_gitlab():
            rinfo = self._card_request("GET", f"/projects/{self._project_path}")
            default_branch = (rinfo or {}).get("default_branch", "main") if isinstance(rinfo, dict) else "main"
            data = self._card_request("GET",
                f"/projects/{self._project_path}/repository/files/README.md?ref={default_branch}")
            content = (data or {}).get("content", "") if isinstance(data, dict) else ""
            if content:
                try:
                    import base64
                    return {"content": base64.b64decode(content).decode("utf-8"), "endpoint": "gitlab_file"}
                except Exception:
                    return {"content": "", "endpoint": ""}
            return {"content": "", "endpoint": ""}
        import base64
        for endpoint, path in (("readme", f"/repos/{self.owner}/{self.repo}/readme"),
                               ("contents", f"/repos/{self.owner}/{self.repo}/contents/README.md"),
                               ("contents_lower", f"/repos/{self.owner}/{self.repo}/contents/readme.md")):
            data = self._card_request("GET", path)
            content = (data or {}).get("content", "") if isinstance(data, dict) else ""
            if not content or (isinstance(data, dict) and data.get("error")):
                continue
            try:
                return {"content": base64.b64decode(content).decode("utf-8"), "endpoint": endpoint}
            except Exception:
                continue
        return {"content": "", "endpoint": ""}


    def _card_or_legacy_get(self, path: str) -> dict:
        """统一 GET（D22：全部走 _request 卡驱动）。"""
        return self._request("GET", path)


    def get_repo_info(self) -> dict:
        """获取仓库信息。"""
        return self._request("GET", self._generic_path(
            f"/repos/{self.owner}/{self.repo}", ""))

    def repo_exists(self) -> bool:
        """探测仓库是否存在（Issue #340 代码仓删除感知）。

        404 → False（已删除）；200 → True；其他错误（403/网络）→ True（不误判，
        交由探活轮询/webhook 双通道兜底）。
        """
        info = self.get_repo_info()
        if isinstance(info, dict) and str(info.get("error", "")).strip() == "HTTP 404":
            return False
        return True

    # === Issue #1748：user-attachments 评论附件（视频/图片）鉴权下载 ===

    def download_user_attachment(self, url: str, max_size: int = USER_ATTACHMENT_MAX_SIZE,
                                 timeout: int = USER_ATTACHMENT_TIMEOUT) -> dict:
        """携带平台 token 下载评论附件（gitcode.com/user-attachments/...）。

        匿名拉取返回 401；带 token 可正常下载。token 仅用于请求头，不进入日志与返回值。
        """
        return _download_user_attachment(url, self.token, max_size=max_size, timeout=timeout)

    # === Issue #315：user-images 图片鉴权下载 ===

    def download_user_image(self, url: str, max_size: int = USER_IMAGE_MAX_SIZE,
                            timeout: int = USER_IMAGE_TIMEOUT) -> dict:
        """携带平台 token 下载 GitCode 编辑器上传图片（raw.gitcode.com/user-images/...）。

        匿名拉取该类资源返回 403；带 Bearer token 实测可 200 拉取。
        token 仅用于请求头，不进入日志与返回值。
        """
        return _download_user_image(url, self.token, max_size=max_size, timeout=timeout)

    # === 平台内部实现 ===

    def _platform_card(self):
        """取本 provider 的仓库平台卡（无卡返回 None；存量三平台也有内置卡）。"""
        try:
            from src.domains.runtime.vcs.platform_card import get_platform_card
            return get_platform_card(self.provider)
        except Exception:
            return None

    def _card_request(self, method: str, path: str, data: dict | None = None,
                      form: bool = False, timeout: int = 30) -> dict:
        """卡驱动通用请求（D13/S14）：api_base + api_auth 按平台卡，零平台代码。

        - query_token（Gitee 系 v5）：?access_token= 查询参数（header 会 401，实测坑）
        - bearer_header：Authorization: Bearer <token>
        - form=True：body 用 x-www-form-urlencoded（Gitee 系 v5 写接口实测要求，
          JSON body 会 400 "body不能为空"——见 _post_gitcode_issue 注释）
        """
        card = self._platform_card()
        if card is None:
            return self._unsupported()
        url = f"{card.api_base}{path}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        auth = card.api_auth
        if auth == "query_token":
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={self.token}"
        elif auth == "token_header":
            # 存量 github 形态：Authorization: token <t> + v3 Accept（逐字节保留）
            headers["Authorization"] = f"token {self.token}"
            headers["Accept"] = "application/vnd.github.v3+json"
        elif auth == "private_token":
            headers["PRIVATE-TOKEN"] = self.token
        else:
            headers["Authorization"] = f"Bearer {self.token}"
        if form and data:
            import urllib.parse as _up
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            return self._do_request(url, method, _up.urlencode(data), headers=headers,
                                    timeout=timeout)
        return self._do_request(url, method, data, headers=headers, timeout=timeout)

    def _generic_path(self, github_style: str, gitlab_style: "str | None" = None) -> str:
        """按卡方言选择路径模板（github 系直接用；gitlab 系用 project 编码路径）。

        `gitlab_style` 的 None/"" 语义必须区分（2026-11-20 实锤）：
        - 未给（None）= 该端点没有 GitLab 专属形态，沿用 github_style；
        - 显式给 ""（如仓库根 `/projects/{owner}%2F{repo}`）= 用 GitLab 形态。
        旧实现把 "" 当"未给"，`get_repo_info()` 于是对 GitLab 请求 `/repos/o/r`
        ——那是**不存在的路由**，GitLab 恒回 404 且响应体是 404 页，
        查重/存在性判定永远得到「不存在」（线上 check-name 对真实存在的
        account-a/hello-fastapi-2 报 exists:false）。
        """
        card = self._platform_card()
        if card and card.dialect == "gitlab" and gitlab_style is not None:
            path = f"/projects/{self._project_path}{gitlab_style}"
            return self._assert_gitlab_path(path)
        if not github_style and gitlab_style is not None:
            path = f"/projects/{self._project_path}{gitlab_style}"
            return self._assert_gitlab_path(path)
        if card and card.dialect == "gitlab":
            # 漏传 gitlab_style 就是在拼一条 GitLab 不存在的 `/repos/...`：
            # 静默 404 会被上层解读成「不存在」，比报错危险得多（查重/探活全失效）。
            raise VcsPathContractError(
                f"{self.provider}: gitlab 方言端点必须给出 gitlab_style"
                f"（收到 github 形态 {github_style!r}）")
        return github_style

    def _assert_gitlab_path(self, path: str) -> str:
        if path.startswith("/repos/"):
            raise VcsPathContractError(f"{self.provider}: gitlab 路径不得以 /repos/ 开头: {path}")
        return path

    def _unsupported(self) -> dict:
        return {"error": f"unsupported provider: {self.provider}"}

    def _request(self, method: str, path: str, data: dict | None = None,
                 form: bool = False, timeout: int = 30) -> dict:
        """统一请求入口（D22：所有平台含存量三平台走卡驱动，消 _xxx_request 平行族）。

        鉴权四态按卡 api_auth（query_token / token_header / private_token /
        bearer_header），存量平台请求头逐字节保留原形态（S14 回归锁）。
        """
        return self._card_request(method, path, data, form=form, timeout=timeout)

    @staticmethod
    def _do_request(url: str, method: str, data: "dict | str | bytes | None" = None,
                    headers: dict | None = None,
                    _span_target: str = "", timeout: int = 30) -> dict:
        """执行 HTTP 请求，返回 JSON dict。

        _span_target：调用方传入的脱敏目标（provider + path，不含 token/url），
        用于调用链 span（MR-3）；未传时从 url 提取 path（去 query，防 token 泄漏）。
        D22：data 兼容 str/bytes（form 编码写路径），其余 JSON 序列化。
        """
        if not _span_target:
            from urllib.parse import urlparse as _up
            _p = _up(url)
            _span_target = "%s%s" % (_p.netloc.split(".")[0], _p.path)
        from src.kernel.otel import external_call_span
        with external_call_span("vcs", _span_target,
                                {"vcs.method": method, "vcs.path": _span_target}) as _span:
            try:
                if isinstance(data, (bytes, str)):
                    body = data.encode("utf-8") if isinstance(data, str) else data
                else:
                    body = json.dumps(data).encode("utf-8") if data else None
                req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
                # Issue #2084（#2084 移植）：上游超时可配（默认 25s < CLI 30s）。
                # 调用链未显式传非默认超时（_request 默认 30 哨兵）时按 env 可配
                # 上游超时错开（GRAPE_UPSTREAM_TIMEOUT_SECONDS），显式值按调用方。
                from src.domains.mgmt.user.repo_manager import upstream_timeout_seconds
                if timeout == 30:
                    timeout = upstream_timeout_seconds()
                # Issue #3580：上游 HTTP 唯一入口（平台卡私有 CA + 带 token 请求不跟随重定向）
                from src.kernel.upstream_http import open_with_policy as _open_policy
                with _open_policy(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    if _span is not None:
                        _span.set_attribute("http.status_code", 200)
                        _span.set_attribute("vcs.result", "ok" if "error" not in payload else "api_error")
                    return payload
            except urllib.error.HTTPError as e:
                # Issue #558：错误 dict 补 status_code、detail 保留上游全文（不截断），
                # 供调用方保留上游状态语义；error 键不变保兼容
                err_body = ""
                try:
                    err_body = e.read().decode()
                except Exception:
                    pass
                log.warning(f"VcsApiClient: {method} {redact_url(url)} → HTTP {e.code}: "
                            f"{redact_text(err_body)[:_LOG_BODY_MAX]}")
                if _span is not None:
                    _span.set_attribute("http.status_code", e.code)
                    _span.set_attribute("vcs.result", "http_error")
                return {"error": f"HTTP {e.code}", "status_code": e.code, "detail": err_body}
            except Exception as e:
                log.warning(f"VcsApiClient: {method} {redact_url(url)} → {redact_text(e)}")
                if _span is not None:
                    _span.set_attribute("vcs.result", "exception")
                return {"error": str(e)}


def create_pr_for_repo(repo_key: str, token: str, title: str, head: str, base: str,
                        body: str = "") -> dict:
    """为指定仓库创建 PR（Issue #242 多仓库 PR 提交）。

    根据 repo_key 前缀自动选择 VCS 平台，复用 VcsApiClient.create_pr。
    """
    from src.kernel.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    client = VcsApiClient(provider=provider, token=token, owner=owner, repo=repo)
    return client.create_pr(title=title, head=head, base=base, body=body)

def create_client(repo_key: str, token: str = "") -> "VcsApiClient":
    """从 repo_key 创建 VcsApiClient。

    repo_key 格式: "gitcode:owner/repo" / "github:owner/repo" / "gitlab:owner/repo"
    """
    from src.kernel.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    if not token:
        # 从 DB 获取 token
        try:
            from src.domains.mgmt.public import get_gitcode_token
            token = get_gitcode_token(repo_key=repo_key) or ""
        except Exception:
            pass
    return VcsApiClient(provider, token, owner, repo)
