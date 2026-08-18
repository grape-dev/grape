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
