"""OBS 存储后端（esdk-obs-python）。

凭据边界（安全约束）：仅对配置桶（self.bucket）内对象操作
（put/get/copy/delete/list/签临时 URL），不调用任何桶级/账号级越权 API
（不列桶 ListBuckets、不创建/删除桶、不跨桶复制）。copyObject 仅限同桶。

配置注意：OBS_ENDPOINT 传区域 endpoint（如 https://obs.cn-north-4.myhuaweicloud.com，
不含桶名），OBS_BUCKET 单独传桶名；SDK 会自动拼 {bucket}.{endpoint} 访问域名。

见 docs/new-version2/obs-storage/obs-skill-storage-design.md §3 N3 / §7。
"""
import os

from src.config import log
from src.storage import StorageBackend, StorageError

try:
    from obs import ObsClient, DeleteObjectsRequest
    _OBS_IMPORT_OK = True
except Exception as _e:  # pragma: no cover - 依赖缺失路径
    ObsClient = None
    _OBS_IMPORT_OK = False
    _OBS_IMPORT_ERR = str(_e)


class ObsStorage(StorageBackend):
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str,
                 prefix: str = "", default_expires: int = 0):
        if not _OBS_IMPORT_OK:  # pragma: no cover
            raise StorageError(f"esdk-obs-python 未安装，无法启用 OBS 存储: {_OBS_IMPORT_ERR}")
        if not endpoint or not bucket:
            raise StorageError("OBS endpoint/bucket 配置缺失")
        # 前缀一律来自配置（环境变量 OBS_PREFIX），不写死默认值——不同环境（prod/test/dev）
        # 用不同前缀隔离，任何实例化路径都不得绕过配置
        if not prefix:
            from src.config import OBS_PREFIX as _cfg_prefix
            prefix = _cfg_prefix
        self.client = ObsClient(access_key_id=access_key, secret_access_key=secret_key,
                                server=endpoint)
        self.bucket = bucket
        self.prefix = (prefix or "").strip("/")
        self.default_expires = int(default_expires or 300)
        log.info(f"ObsStorage ready: bucket={bucket} prefix={self.prefix or '-'}")

    def backend_name(self) -> str:
        return "obs"

    # ---- 内部：对象键拼环境前缀（业务 key 不含前缀；skill_key 产物/DB 完整 key 已含前缀则幂等）----
    def _full(self, key: str) -> str:
        if self.prefix and key.startswith(self.prefix + "/"):
            return key
        return f"{self.prefix}/{key}" if self.prefix else key

    def _strip_prefix(self, full_key: str) -> str:
        if self.prefix and full_key.startswith(self.prefix + "/"):
            return full_key[len(self.prefix) + 1:]
        return full_key

    def _ok(self, resp) -> bool:
        try:
            return int(getattr(resp, "status", 500)) < 300
        except Exception:
            return False

    # ---- 写入 ----
    def save_bytes(self, key: str, data: bytes) -> str:
        try:
            resp = self.client.putObject(self.bucket, self._full(key), data)
            if not self._ok(resp):
                raise StorageError(f"putObject {key} failed: status={getattr(resp, 'status', '?')}")
            return key
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"putObject {key} error: {e}") from e

    def save_stream(self, key: str, source: str) -> str:
        try:
            resp = self.client.putFile(self.bucket, self._full(key), source)
            if not self._ok(resp):
                raise StorageError(f"putFile {key} failed: status={getattr(resp, 'status', '?')}")
            return key
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"putFile {key} error: {e}") from e

    # ---- 读取 ----
    def read_bytes(self, key: str) -> bytes | None:
        try:
            resp = self.client.getObject(self.bucket, self._full(key), loadStreamInMemory=True)
            if not self._ok(resp):
                return None
            body = getattr(resp, "body", None)
            if isinstance(body, dict):
                buf = body.get("buffer")
                if isinstance(buf, bytes):
                    return buf
            return None
        except Exception as e:
            log.warning(f"getObject {key} failed: {e}")
            return None

    def read_file(self, key: str, local_path: str) -> bool:
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            resp = self.client.getObject(self.bucket, self._full(key), downloadPath=local_path)
            return self._ok(resp) and os.path.isfile(local_path)
        except Exception as e:
            log.warning(f"read_file {key} -> {local_path} failed: {e}")
            return False

    def exists(self, key: str) -> bool:
        return self.read_bytes(key) is not None

    # ---- 枚举 / 删除（仅配置桶内，按前缀） ----
    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        try:
            marker = None
            while True:
                kwargs = {"prefix": self._full(prefix)}
                if marker:
                    kwargs["marker"] = marker
                resp = self.client.listObjects(self.bucket, **kwargs)
                body = resp.get("body") if isinstance(resp, dict) else getattr(resp, "body", None)
                if not isinstance(body, dict):
                    break
                for content in (body.get("contents") or []):
                    full_key = content.get("key", "") if isinstance(content, dict) else getattr(content, "key", "")
                    if full_key:
                        keys.append(self._strip_prefix(full_key))
                truncated = body.get("is_truncated") or body.get("isTruncated")
                if truncated and keys:
                    marker = self._full(keys[-1])  # 分页：用最后一个 key 续拉
                else:
                    break
        except Exception as e:
            log.warning(f"listObjects {prefix} failed: {e}")
        return keys

    def list_objects_with_meta(self, prefix: str, cursor: str = "", limit: int = 1000) -> tuple[list[dict], str | None]:
        """枚举指定前缀下的对象，返回 (items, next_cursor)。

        OBS listObjects 已返回 size 和 lastModified——直接用。
        每个 item: {key, size, last_modified(ISO 8601)}。
        """
        items: list[dict] = []
        next_cursor: str | None = None
        try:
            marker = self._full(cursor) if cursor else None
            while True:
                kwargs = {"prefix": self._full(prefix), "max_keys": limit}
                if marker:
                    kwargs["marker"] = marker
                resp = self.client.listObjects(self.bucket, **kwargs)
                body = resp.get("body") if isinstance(resp, dict) else getattr(resp, "body", None)
                if not isinstance(body, dict):
                    break
                for content in (body.get("contents") or []):
                    if isinstance(content, dict):
                        full_key = content.get("key", "")
                        size = content.get("size", 0)
                        last_mod = content.get("lastModified", "")
                    else:
                        full_key = getattr(content, "key", "")
                        size = getattr(content, "size", 0)
                        last_mod = getattr(content, "lastModified", "")
                    if full_key:
                        items.append({
                            "key": self._strip_prefix(full_key),
                            "size": int(size) if size else 0,
                            "last_modified": str(last_mod) if last_mod else "",
                        })
                truncated = body.get("is_truncated") or body.get("isTruncated")
                if truncated and items:
                    marker = self._full(items[-1]["key"])
                    next_cursor = items[-1]["key"]
                    if len(items) >= limit:
                        break
                else:
                    next_cursor = None
                    break
        except Exception as e:
            log.warning(f"list_objects_with_meta {prefix} failed: {e}")
        return items, next_cursor

    def generate_file_urls(self, repo_key: str, agent_type: str = "", issue_num: int = 0) -> dict:
        """按 scope 生成 {rel_path: {url, storage_key}}——和 knowledge_files() 同样的逻辑。

        通过 _query_knowledge_obs 按 scope 查 DB obs_key（不查 DB file_path）。
        通过 knowledge_path() 算 rel_path（和 knowledge_files 同函数）。
        obs_key 非空 → 生成 url；obs_key 为空 → url 和 storage_key 留空。
        """
        import os as _os
        from src.config import WORKSPACE_DIR
        from src.api.knowledge import knowledge_path, _query_knowledge_obs, _owner_repo, member_files
        from src.session_manager import db, db_lock
        out: dict[str, dict] = {}
        try:
            # 共享知识：project / issue / issue_agent 三级（和 read_knowledge 同样的 scope 循环）
            for scope in ("project", "issue", "issue_agent"):
                _agent = agent_type if scope == "issue_agent" else ""
                _issue = issue_num if scope != "project" else 0
                obs_key, _ = _query_knowledge_obs(repo_key, scope, _agent, _issue)
                rel_path = _os.path.relpath(knowledge_path(repo_key, scope, agent_type=_agent, issue_num=_issue), WORKSPACE_DIR)
                if obs_key:
                    out[rel_path] = {"url": self.temporary_url(obs_key, expires=3600), "storage_key": obs_key}
                else:
                    out[rel_path] = {"url": "", "storage_key": ""}

            # 成员上传文档：按 scope='member' 查 DB obs_key
            owner, rname = _owner_repo(repo_key)
            with db_lock:
                rows = db.cursor().execute(
                    "SELECT file_path, obs_key FROM agent_shared_knowledge "
                    "WHERE repo_key=? AND scope='member' AND obs_key!='' AND obs_key IS NOT NULL "
                    "ORDER BY id DESC",
                    (repo_key,)
                ).fetchall()
            # member_files 返回 {rel_path: content}——rel_path 相对 WORKSPACE_DIR（owner/repo/project-info/member/...）
            # DB file_path 相对 repo_base（project-info/member/...）——file_path 是 rel_path 的后缀
            # 同时也生成 member_files_pi 格式的 key（member/user1/design.md）供 project_info_files 匹配
            for rel_path in member_files(repo_key):
                obs_key = ""
                for fp, ok in rows:
                    if fp and ok and rel_path.endswith(fp):
                        obs_key = ok
                        break
                if obs_key:
                    out[rel_path] = {"url": self.temporary_url(obs_key, expires=3600), "storage_key": obs_key}
                    # 同时生成 project-info 相对格式（member_files_pi 格式）
                    _pi_rel = _os.path.relpath(rel_path, _os.path.join(WORKSPACE_DIR, owner, rname, "project-info"))
                    out[_pi_rel] = {"url": self.temporary_url(obs_key, expires=3600), "storage_key": obs_key}
                else:
                    out[rel_path] = {"url": "", "storage_key": ""}

            # Agent 专属文档：按 scope='member' AND agent_type != '' 查 DB obs_key
            # agent_files 返回 {rel_path: content}——rel_path 相对 project-info 目录（agent/{agent_type}/{name}）
            # upload_knowledge agent scope 存 DB scope='member' + agent_type={agent_type}
            with db_lock:
                if agent_type:
                    rows = db.cursor().execute(
                        "SELECT file_path, obs_key FROM agent_shared_knowledge "
                        "WHERE repo_key=? AND scope='member' AND agent_type=? AND obs_key!='' AND obs_key IS NOT NULL "
                        "ORDER BY id DESC",
                        (repo_key, agent_type)
                    ).fetchall()
                else:
                    rows = []
            from src.api.knowledge import agent_files
            for rel_path in agent_files(repo_key, agent_type).keys():
                obs_key = ""
                for fp, ok in rows:
                    if fp and ok and rel_path.endswith(os.path.basename(fp)):
                        obs_key = ok
                        break
                if obs_key:
                    out[rel_path] = {"url": self.temporary_url(obs_key, expires=3600), "storage_key": obs_key}
                else:
                    out[rel_path] = {"url": "", "storage_key": ""}

            # 基线文件：按 scope='baseline' 查 DB obs_key
            with db_lock:
                rows = db.cursor().execute(
                    "SELECT file_path, obs_key FROM agent_shared_knowledge "
                    "WHERE repo_key=? AND scope='baseline' AND obs_key!='' AND obs_key IS NOT NULL "
                    "ORDER BY id DESC",
                    (repo_key,)
                ).fetchall()
            # load_project_info 返回的 rel_path 只有文件名（project-graph.md 等）
            # DB file_path 是 WORKSPACE_DIR 相对（owner/repo/project-info/{filename}）——用 basename 匹配
            baseline_basename_map = {}
            for fp, ok in rows:
                if fp and ok:
                    baseline_basename_map[_os.path.basename(fp)] = ok
            # 基线文件固定 2 个：project-graph.md / project-capabilities.md
            for _fname in ("project-graph.md", "project-capabilities.md"):
                obs_key = baseline_basename_map.get(_fname, "")
                if obs_key:
                    out[_fname] = {"url": self.temporary_url(obs_key, expires=3600), "storage_key": obs_key}
                else:
                    out[_fname] = {"url": "", "storage_key": ""}
        except Exception as e:
            log.warning(f"generate_file_urls failed: {e}")
        return out

    def delete_object(self, key: str) -> bool:
        """单对象删除（固定 key 模型：每技能仅 1 对象，删除 = 单对象）。"""
        try:
            self.client.deleteObject(self.bucket, self._full(key))
            return True
        except Exception as e:
            log.warning(f"deleteObject {key} failed: {e}")
            return False

    def delete_prefix(self, prefix: str) -> int:
        keys = self.list_keys(prefix)
        if not keys:
            return 0
        try:
            from obs import DeleteObjectsRequest, Object
            # 同桶内批量删除（DeleteObjectsRequest，最多 1000/批）
            for i in range(0, len(keys), 900):
                batch = keys[i:i + 900]
                req = DeleteObjectsRequest()
                for k in batch:
                    req.add_object(Object(key=self._full(k)))
                self.client.deleteObjects(self.bucket, req)
            return len(keys)
        except Exception as e:
            log.warning(f"deleteObjects {prefix} failed: {e}")
            return 0

    # ---- 同桶复制（转换/提升用；不跨桶） ----
    def copy_object(self, src_key: str, dst_key: str) -> bool:
        try:
            resp = self.client.copyObject(
                self.bucket, self._full(src_key),
                self.bucket, self._full(dst_key))
            return self._ok(resp)
        except Exception as e:
            log.warning(f"copyObject {src_key} -> {dst_key} failed: {e}")
            return False

    # ---- 临时签名 URL（仅 GET；每次动态签发，短时效） ----
    def temporary_url(self, key: str, expires: int = 300) -> str:
        try:
            resp = self.client.createSignedUrl(
                "GET", self.bucket, self._full(key),
                expires=int(expires or self.default_expires))
            # _CreateSignedUrlResponse 为 dict 子类，signedUrl 属性化访问
            url = None
            try:
                url = resp.signedUrl
            except Exception:
                url = resp.get("signedUrl")
            if not url:
                raise StorageError(f"createSignedUrl {key}: no url")
            return url
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"createSignedUrl {key} error: {e}") from e
