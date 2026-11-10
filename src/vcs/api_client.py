"""统一 VCS API 客户端：跨平台 Issue/PR REST API 调用抽象。

支持 GitCode / GitHub / GitLab 三个平台，提供统一的 API 接口。
逐步替代 repogit.py 中分散的 API 调用。

参见 docs/weekly-02/issue-49-multi-vcs-extension-design.md §3.8
"""
import json
import os
import re
import urllib.request
import urllib.error
from src.config import log


# Issue #315: GitCode 编辑器上传图片（raw.gitcode.com/user-images/...）链接识别与鉴权下载。
# 该类资源匿名拉取 403，需带 token；提取为纯函数便于单测。
_USER_IMAGE_URL_RE = re.compile(r"https://raw\.gitcode\.com/user-images/[^\s\)\"'<>]+")
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


def extract_gitcode_user_image_urls(text: "str | None") -> list[str]:
    """从 Markdown / HTML / 裸链文本中提取 raw.gitcode.com/user-images 图片 URL。

    覆盖形态：![alt](url)、<img src="url">、裸 URL。
    去重保序（首个出现顺序）。纯函数，无 IO；text 为 None/空返回 []。
    """
    if not text:
        return []
    urls = _USER_IMAGE_URL_RE.findall(text)
    return list(dict.fromkeys(urls))


def _download_authed(url: str, token: str, *, allowed_prefix: str, ctype_prefixes: tuple,
                     max_size: int, timeout: int, accept: str = "", label: str = "asset",
                     ctype_error: str = "unsupported content-type") -> dict:
    """Issue #315/#1748 共用：带 token 下载白名单 URL 的通用实现（消除重复代码）。

    返回 {"content": bytes, "content_type": str, "size": int} 或 {"error": str}。
    token 仅构造 Authorization 请求头，绝不写入日志或返回值。
    """
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
        log.warning(f"{label} download failed: {url} -> HTTP {e.code}")
        return {"error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        _reason = getattr(e, "reason", e)
        if "timed out" in str(_reason).lower() or "timeout" in str(_reason).lower():
            log.warning(f"{label} download failed: {url} -> timeout ({timeout}s)")
            return {"error": "timeout"}
        log.warning(f"{label} download failed: {url} -> {_reason}")
        return {"error": str(_reason)}
    except Exception as e:
        log.warning(f"{label} download failed: {url} -> {e}")
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
        self.owner = owner
        self.repo = repo

    # === Issue 评论 ===

    def _ledger(self, content_type: str, content_id, issue_number, body: str = "") -> None:
        """Issue #453 代发台账（写点8，客户端层收口）：写操作成功后入账，best-effort。

        在 VcsApiClient 四个写方法（评论/关issue/建PR/合PR）统一调用——所有调用方
        （git_repo_api / issues.py / issues_center / create_pr_for_repo）自动覆盖。
        content_id：字符串/数字 ID（评论 ID / issue 编号 / PR 编号）。
        """
        try:
            from src.outbound_ledger import record_outbound, content_hash_md5
            from src.repo_key import to_repo_key
            if content_id is None or str(content_id) == "":
                return
            _rk = to_repo_key(f"{self.owner}/{self.repo}", self.provider)
            record_outbound(self.provider, _rk, content_type, str(content_id),
                            issue_number=str(issue_number or ""),
                            content_hash=content_hash_md5(body) if body else "")
        except Exception as e:
            from src.config import log
            log.warning(f"VcsApiClient ledger record failed (non-blocking): {e}")

    def post_issue_comment(self, issue_number: int, body: str) -> dict:
        """在 Issue 上发表评论。"""
        if self.provider == "gitcode":
            _resp = self._gitcode_request(
                "POST", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
                {"body": body})
        elif self.provider == "github":
            _resp = self._github_request(
                "POST", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
                {"body": body})
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            _resp = self._gitlab_request(
                "POST", f"/projects/{project_id}/issues/{issue_number}/notes",
                {"body": body})
        else:
            return {"error": f"unsupported provider: {self.provider}"}
        # Issue #453 台账：响应 dict 里的评论 ID 入账（无 ID 时记 issue_number 兼值）
        if isinstance(_resp, dict) and "error" not in _resp:
            from src.outbound_ledger import extract_content_id
            _cid = extract_content_id(_resp, "issue_comment")
            self._ledger("issue_comment", _cid or issue_number, issue_number, body)
        return _resp

    def list_issue_comments(self, issue_number: int, per_page: int = 100) -> list:
        """获取 Issue 评论列表（Issue #1739：门禁标注跨进程幂等去重用）。

        Returns:
            list: 评论列表；失败返回空列表。
        """
        if self.provider == "gitcode":
            _resp = self._gitcode_request(
                "GET", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments?per_page={per_page}")
        elif self.provider == "github":
            _resp = self._github_request(
                "GET", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments?per_page={per_page}")
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            _resp = self._gitlab_request(
                "GET", f"/projects/{project_id}/issues/{issue_number}/notes?per_page={per_page}")
        else:
            return []
        return _resp if isinstance(_resp, list) else []

    # === Issue 查询 ===

    def get_issue(self, issue_number: int) -> dict:
        """获取 Issue 详情。"""
        if self.provider == "gitcode":
            return self._gitcode_request(
                "GET", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}")
        elif self.provider == "github":
            return self._github_request(
                "GET", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}")
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            return self._gitlab_request(
                "GET", f"/projects/{project_id}/issues/{issue_number}")
        return {"error": f"unsupported provider: {self.provider}"}

    # === Issue 关闭 ===

    def close_issue(self, issue_number: int) -> dict:
        """关闭 Issue（设置 state=closed）。

        Returns:
            dict: API 返回的 Issue JSON，或 {"error": ...} 错误信息。
        """
        if self.provider == "gitcode":
            # GitCode v5 更新 Issue（docs.gitcode.com patch-api-v5-...-issues-number）：
            # body 必填 repo+title，关闭用 state=close（reopen 开启/close 关闭；GitHub 的 state=closed 会被拒）
            try:
                _info = self.get_issue(issue_number)
                _title = str(_info.get("title") or "") if isinstance(_info, dict) else ""
            except Exception:
                _title = ""
            _resp = self._gitcode_request(
                "PATCH", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
                {"state": "close", "repo": f"{self.owner}/{self.repo}", "title": _title})
        elif self.provider == "github":
            _resp = self._github_request(
                "PATCH", f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
                {"state": "closed"})
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            _resp = self._gitlab_request(
                "PUT", f"/projects/{project_id}/issues/{issue_number}",
                {"state_event": "close"})
        else:
            return {"error": f"unsupported provider: {self.provider}"}
        # Issue #453 台账（写点10）：关闭 issue 入账——issue_closed 事件回流对账
        if isinstance(_resp, dict) and "error" not in _resp:
            self._ledger("issue_close", issue_number, issue_number)
        return _resp

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
        if self.provider == "gitcode":
            _resp = self._gitcode_request(
                "POST", f"/repos/{self.owner}/{self.repo}/pulls", data)
        elif self.provider == "github":
            _resp = self._github_request(
                "POST", f"/repos/{self.owner}/{self.repo}/pulls", data)
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            _resp = self._gitlab_request(
                "POST", f"/projects/{project_id}/merge_requests", data)
        else:
            return {"error": f"unsupported provider: {self.provider}"}
        # Issue #453 台账（写点11）：建 PR 入账——PR 编号（number/iid）为对账键
        if isinstance(_resp, dict) and "error" not in _resp:
            from src.outbound_ledger import extract_content_id
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
        if self.provider == "gitcode":
            _resp = self._gitcode_request(
                "PUT", f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/merge", _body or {})
        elif self.provider == "github":
            _resp = self._github_request(
                "PUT", f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/merge", _body or {})
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            _gl_body = {"squash": True} if _body.get("merge_method") == "squash" else {}
            _resp = self._gitlab_request(
                "PUT", f"/projects/{project_id}/merge_requests/{pr_number}/merge", _gl_body)
        else:
            return {"error": f"unsupported provider: {self.provider}"}
        # Issue #453 台账（写点11）：合并 PR 入账——pr_merge 事件回流对账
        if isinstance(_resp, dict) and "error" not in _resp:
            self._ledger("pr_merge", pr_number, "")
        return _resp

    def get_pr(self, pr_number: int) -> dict:
        """获取 PR 详情。"""
        if self.provider == "gitcode":
            return self._gitcode_request(
                "GET", f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}")
        elif self.provider == "github":
            return self._github_request(
                "GET", f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}")
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            return self._gitlab_request(
                "GET", f"/projects/{project_id}/merge_requests/{pr_number}")
        return {"error": f"unsupported provider: {self.provider}"}

    def list_prs(self, state: str = "all", head: str = "",
                 max_pages: int = 20) -> list | dict:
        """分页查询 PR 列表（Issue #993：按 head 分支查 merged/closed 状态）。

        ``per_page=100`` 且全量翻页，避免 PR 超过单页时漏掉 merged PR。返回列表；
        查询失败返回 ``{"error": ...}``。
        """
        if self.provider == "gitcode":
            def _fetch(page):
                path = (f"/repos/{self.owner}/{self.repo}/pulls?state={state}"
                        f"&per_page=100&page={page}")
                return self._gitcode_request("GET", path)
        elif self.provider == "github":
            def _fetch(page):
                head_q = f"&head={self.owner}:{head}" if head else ""
                path = (f"/repos/{self.owner}/{self.repo}/pulls?state={state}"
                        f"{head_q}&per_page=100&page={page}")
                return self._github_request("GET", path)
        elif self.provider == "gitlab":
            def _fetch(page):
                project_id = f"{self.owner}%2F{self.repo}"
                path = (f"/projects/{project_id}/merge_requests?state={state}"
                        f"&per_page=100&page={page}")
                return self._gitlab_request("GET", path)
        else:
            return {"error": f"unsupported provider: {self.provider}"}
        return paginate_pages(_fetch, page_size=100, max_pages=max_pages)

    # === 仓库信息 ===

    def get_repo_info(self) -> dict:
        """获取仓库信息。"""
        if self.provider == "gitcode":
            return self._gitcode_request("GET", f"/repos/{self.owner}/{self.repo}")
        elif self.provider == "github":
            return self._github_request("GET", f"/repos/{self.owner}/{self.repo}")
        elif self.provider == "gitlab":
            project_id = f"{self.owner}%2F{self.repo}"
            return self._gitlab_request("GET", f"/projects/{project_id}")
        return {"error": f"unsupported provider: {self.provider}"}

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

    def _gitcode_request(self, method: str, path: str, data: dict | None = None) -> dict:
        # GitCode v5（Gitee 系）认证用 ?access_token= 查询参数，Authorisation header 会 401 "token not found"
        url = f"https://gitcode.com/api/v5{path}"
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}access_token={self.token}"
        return self._do_request(url, method, data, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _github_request(self, method: str, path: str, data: dict | None = None) -> dict:
        url = f"https://api.github.com{path}"
        return self._do_request(url, method, data, headers={
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        })

    def _gitlab_request(self, method: str, path: str, data: dict | None = None) -> dict:
        url = f"https://gitlab.com/api/v4{path}"
        return self._do_request(url, method, data, headers={
            "PRIVATE-TOKEN": self.token,
            "Content-Type": "application/json",
        })

    @staticmethod
    def _do_request(url: str, method: str, data: dict | None = None, headers: dict | None = None,
                    _span_target: str = "") -> dict:
        """执行 HTTP 请求，返回 JSON dict。

        _span_target：调用方传入的脱敏目标（provider + path，不含 token/url），
        用于调用链 span（MR-3）；未传时从 url 提取 path（去 query，防 token 泄漏）。
        """
        if not _span_target:
            from urllib.parse import urlparse as _up
            _p = _up(url)
            _span_target = "%s%s" % (_p.netloc.split(".")[0], _p.path)
        from src.observability.otel import external_call_span
        with external_call_span("vcs", _span_target,
                                {"vcs.method": method, "vcs.path": _span_target}) as _span:
            try:
                body = json.dumps(data).encode("utf-8") if data else None
                req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
                with urllib.request.urlopen(req, timeout=30) as resp:
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
                log.warning(f"VcsApiClient: {method} {url} → HTTP {e.code}: {err_body}")
                if _span is not None:
                    _span.set_attribute("http.status_code", e.code)
                    _span.set_attribute("vcs.result", "http_error")
                return {"error": f"HTTP {e.code}", "status_code": e.code, "detail": err_body}
            except Exception as e:
                log.warning(f"VcsApiClient: {method} {url} → {e}")
                if _span is not None:
                    _span.set_attribute("vcs.result", "exception")
                return {"error": str(e)}



def create_pr_for_repo(repo_key: str, token: str, title: str, head: str, base: str,
                        body: str = "") -> dict:
    """为指定仓库创建 PR（Issue #242 多仓库 PR 提交）。

    根据 repo_key 前缀自动选择 VCS 平台，复用 VcsApiClient.create_pr。
    """
    from src.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    client = VcsApiClient(provider=provider, token=token, owner=owner, repo=repo)
    return client.create_pr(title=title, head=head, base=base, body=body)

def create_client(repo_key: str, token: str = "") -> "VcsApiClient":
    """从 repo_key 创建 VcsApiClient。

    repo_key 格式: "gitcode:owner/repo" / "github:owner/repo" / "gitlab:owner/repo"
    """
    from src.repo_key import parse_repo_key
    provider, owner, repo = parse_repo_key(repo_key)
    if not token:
        # 从 DB 获取 token
        try:
            from src.user.db import get_gitcode_token
            token = get_gitcode_token(repo_key=repo_key) or ""
        except Exception:
            pass
    return VcsApiClient(provider, token, owner, repo)
