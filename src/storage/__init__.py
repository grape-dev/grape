"""文件存储抽象层（OBS 化改造，见 docs/new-version2/obs-storage/obs-skill-storage-design.md）。

设计要点：
- get_storage() 单例：OBS_ENABLED 且配置齐全 → ObsStorage；否则 → None。
- storage 为 None 时，业务代码走原有本地磁盘流程（行为与改造前完全一致）。
- 两级缓存（可丢，正确性不依赖缓存）：
  * 磁盘 LRU zip 缓存：对象键 → zip 字节文件（容量上限 2GB / 每技能保留 3 版）
  * 内存 LRU 提取缓存：(skill_name, version) → SKILL.md 文本
- skill_key()：对象键唯一生成入口（目录段可读 + 随机文件名段防枚举）。
- 凭据边界：仅操作配置桶内对象（put/get/copy/delete/list/签 URL），
  不做任何桶级/账号级越权操作。
"""
import base64
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import threading
import zipfile
from abc import ABC, abstractmethod

from src.config import log, OBS_LOCAL_CACHE

# 磁盘缓存根（OBS 模式下 zip 包缓存；默认 data/cache/storage）
CACHE_ROOT = OBS_LOCAL_CACHE or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "cache", "storage",
)
ZIP_CACHE_DIR = os.path.join(CACHE_ROOT, "zips")
ZIP_CACHE_INDEX = os.path.join(ZIP_CACHE_DIR, "index.json")
ZIP_CACHE_MAX_BYTES = int(os.environ.get("OBS_ZIP_CACHE_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))  # 2GB
ZIP_CACHE_MAX_VERSIONS = int(os.environ.get("OBS_ZIP_CACHE_MAX_VERSIONS", "1"))  # 每技能保留对象数（v2.4 固定 key：1）

# 内存提取缓存上限（SKILL.md 文本，约 50MB）
MEM_CACHE_MAX_BYTES = int(os.environ.get("OBS_MEM_CACHE_MAX_BYTES", str(50 * 1024 * 1024)))

# 动态片段白名单（拼 key 前必过）
_RE_USER_ID = re.compile(r"^\d+$")
_RE_NAME = re.compile(r"^[\w.\-]+$")
_RE_RAND8 = re.compile(r"^[0-9a-f]{8}$")
_VALID_SCOPES = ("custom", "repo", "official")


class StorageError(RuntimeError):
    """存储操作错误（业务侧映射 502/503）。"""


class StorageBackend(ABC):
    """文件存储后端抽象。"""

    @abstractmethod
    def backend_name(self) -> str:
        """后端标识："obs"。"""

    @abstractmethod
    def save_bytes(self, key: str, data: bytes) -> str:
        """写入对象，返回 key。"""

    @abstractmethod
    def save_stream(self, key: str, source: str) -> str:
        """将本地文件 source 写入对象，返回 key。"""

    @abstractmethod
    def read_bytes(self, key: str) -> bytes | None:
        """读取对象内容；不存在/失败返回 None。"""

    @abstractmethod
    def read_file(self, key: str, local_path: str) -> bool:
        """将对象拉取到 local_path；成功返回 True。"""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """对象是否存在。"""

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]:
        """枚举指定前缀下的对象 key 列表（不含环境前缀）。"""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """删除指定前缀下全部对象，返回删除数量。"""

    @abstractmethod
    def delete_object(self, key: str) -> bool:
        """单对象删除（固定 key 模型：每技能仅 1 对象，删除 = 单对象）。"""

    @abstractmethod
    def temporary_url(self, key: str, expires: int = 300) -> str:
        """签发临时签名 GET URL（有效期秒）。"""

    def list_objects_with_meta(self, prefix: str, cursor: str = "", limit: int = 1000) -> tuple[list[dict], str | None]:
        """枚举指定前缀下的对象，返回 (items, next_cursor)。

        每个 item: {key, size, last_modified(ISO 8601)}。
        next_cursor 非空表示还有更多——传入下次查询的 cursor。
        默认实现：调 list_keys 拿 key 列表（不含 size/last_modified）。
        子类（OBS）可重写以返回完整元数据。
        """
        keys = self.list_keys(prefix)
        items = [{"key": k, "size": 0, "last_modified": ""} for k in keys]
        return items, None

    def generate_file_urls(self, filtered_files: dict) -> dict:
        """为知识文件生成 {rel_path: {url, storage_key}}（Issue #95：接收已过滤结果）。

        基类默认实现：返回空 dict（本地模式，Worker 走 content 兜底）。
        OBS 实现重写：基于 filtered_files 的 obs_key 生成 temporary_url。
        """
        return {}

    async def read_batch(self, keys: list[str]) -> dict[str, str]:
        """批量并发读取——限制并发数防止触发存储限流。

        返回 {key: content}，失败的返回空字符串。
        """
        import asyncio
        sem = asyncio.Semaphore(10)

        async def _read(key: str) -> tuple[str, str]:
            async with sem:
                try:
                    data = self.read_bytes(key)
                    return key, (data.decode("utf-8") if isinstance(data, bytes) else (data or ""))
                except Exception:
                    return key, ""

        results = await asyncio.gather(*[_read(k) for k in keys])
        return dict(results)


# ===== 单例 =====

_storage: StorageBackend | None = None
_storage_lock = threading.Lock()


def get_storage() -> StorageBackend | None:
    """返回全局存储后端；OBS 未启用/配置不齐 → None（业务走原本地流程）。"""
    global _storage
    if _storage is None:
        with _storage_lock:
            if _storage is None:
                from src.config import (
                    OBS_ENABLED, OBS_BUCKET, OBS_ACCESS_KEY, OBS_SECRET_KEY,
                    OBS_ENDPOINT, OBS_PREFIX, OBS_URL_EXPIRES,
                )
                if OBS_ENABLED and OBS_BUCKET and OBS_ACCESS_KEY and OBS_SECRET_KEY:
                    from src.storage.obs import ObsStorage
                    _storage = ObsStorage(
                        endpoint=OBS_ENDPOINT, bucket=OBS_BUCKET,
                        access_key=OBS_ACCESS_KEY, secret_key=OBS_SECRET_KEY,
                        prefix=OBS_PREFIX, default_expires=OBS_URL_EXPIRES,
                    )
                    log.info(f"StorageBackend: obs (bucket={OBS_BUCKET}, prefix={OBS_PREFIX})")
                else:
                    log.info("StorageBackend: local (OBS not enabled)")
    return _storage


def reset_storage() -> None:
    """重置单例（测试用）。"""
    global _storage
    _storage = None


# ===== 技能域存储（OBS 未启用时单机本地兜底，docs/weekly-06/skill-local-storage-fallback-design.md）=====

def get_skill_storage() -> StorageBackend | None:
    """技能域统一存储：OBS 可用 → OBS；SKILL_STORAGE 允许 → LocalStorage；obs-only 且无 OBS → None。

    只影响技能域（upload/download/detail/convert/delete/执行取包），其它域继续用 get_storage()。
    不缓存（每次现算）：OBS 的 singletion 由 get_storage() 持有，LocalStorage 无状态、构建廉价；
    避免测试/运行时切换 OBS 与本地后技能域仍用旧后端。
    """
    return _build_skill_storage(get_storage())


def _build_skill_storage(_base: StorageBackend | None) -> StorageBackend | None:
    from src.config import SKILL_STORAGE, SKILL_STORAGE_DIR
    _mode = (SKILL_STORAGE or "auto").lower()
    if _base is not None:  # OBS 已配置则优先
        return _base
    if _mode in ("auto", "local"):
        from src.storage.local import LocalStorage
        log.info(f"StorageBackend: local skill storage (dir={SKILL_STORAGE_DIR}, mode={_mode})")
        return LocalStorage(SKILL_STORAGE_DIR)
    return None  # obs-only 且 OBS 未配置 → 维持 503


def reset_skill_storage() -> None:
    """（兼容保留）技能存储不缓存，无需重置。"""


# ===== 对象键生成（唯一入口） =====

def skill_key(scope: str, owner: str, skill_name: str, version: int | None = None,
              rand8: str | None = None) -> str:
    """生成技能 zip 对象键（固定 key，v2.4 去版本化）：
    {PREFIX}/skills/{scope}/{owner}/{skill_name}/{skill_name}.zip

    scope: custom/repo/official；owner: user_id（custom）或 repo_key（repo，owner/repo 两级）；
    动态片段全部过白名单。version/rand8 参数保留仅为兼容旧调用，固定 key 模型下忽略。
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(f"invalid scope: {scope}")
    if not _RE_NAME.match(skill_name or ""):
        raise ValueError(f"invalid skill_name: {skill_name}")
    if scope == "custom":
        if not _RE_USER_ID.match(owner or ""):
            raise ValueError(f"invalid user_id: {owner}")
        owner_seg = owner
    elif scope == "repo":
        # 兼容 gitcode: 前缀 repo_key（db_setup 迁移对存量统一加前缀）；对象键段用无前缀规范
        _owner = owner or ""
        if ":" in _owner:
            _owner = _owner.split(":", 1)[1]
        parts = _owner.split("/", 1)
        if len(parts) != 2 or not _RE_NAME.match(parts[0]) or not _RE_NAME.match(parts[1]):
            raise ValueError(f"invalid repo_key: {owner}")
        owner_seg = f"{parts[0]}/{parts[1]}"
    else:  # official
        owner_seg = ""
    from src.config import OBS_PREFIX
    prefix = (OBS_PREFIX or "").strip("/")
    base = f"skills/{scope}/{owner_seg}" if owner_seg else f"skills/{scope}"
    return f"{prefix}/{base}/{skill_name}/{skill_name}.zip"


def parse_skill_key(object_key: str) -> dict:
    """从对象键解析技能信息（缓存失效/清理用）。返回 {scope, owner, skill_name, version}。

    固定 key 模型下无版本段，version 恒为 0（兼容旧结构解析）。
    """
    key = object_key or ""
    m = re.search(r"/skills/custom/(\d+)/([^/]+)/([^/]+)\.zip", key)
    if m:
        return {"scope": "custom", "owner": m.group(1), "skill_name": m.group(2), "version": 0}
    m = re.search(r"/skills/repo/([\w.\-]+)/([\w.\-]+)/([^/]+)/([^/]+)\.zip", key)
    if m:
        return {"scope": "repo", "owner": f"{m.group(1)}/{m.group(2)}",
                "skill_name": m.group(3), "version": 0}
    m = re.search(r"/skills/official/([^/]+)/([^/]+)\.zip", key)
    if m:
        return {"scope": "official", "owner": "", "skill_name": m.group(1), "version": 0}
    return {"scope": "", "owner": "", "skill_name": "", "version": 0}


def skill_tag_from_key(object_key: str) -> str:
    """技能标识（去文件名段）：.../skills/{scope}/{owner}/{skill_name}，用于技能归类。"""
    return (object_key or "").rsplit("/", 1)[0]


def skill_prefix_of_key(object_key: str) -> str:
    """对象键 → 技能目录前缀（含尾斜杠，用于按技能整树删除/枚举所有版本）。

    例：grape/prod/skills/custom/123/hello/v2/hello-a1b2.zip
        → grape/prod/skills/custom/123/hello/
    """
    return skill_tag_from_key(object_key) + "/"


# ===== 磁盘 LRU zip 缓存（可丢） =====

_zip_lock = threading.Lock()


def _zip_index() -> dict:
    try:
        with open(ZIP_CACHE_INDEX, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _zip_write_index(index: dict) -> None:
    try:
        os.makedirs(ZIP_CACHE_DIR, exist_ok=True)
        tmp = ZIP_CACHE_INDEX + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f)
        os.replace(tmp, ZIP_CACHE_INDEX)
    except Exception as e:
        log.warning(f"zip cache index write failed: {e}")


def zip_cache_get(object_key: str) -> bytes | None:
    """磁盘 zip 缓存读取；miss 返回 None。

    返回纯 bytes（向后兼容）；如需同时获取 content_hash 用 zip_cache_get_with_hash()。
    """
    result = zip_cache_get_with_hash(object_key)
    return result[0] if result else None


def zip_cache_get_with_hash(object_key: str) -> tuple[bytes, str] | None:
    """磁盘 zip 缓存读取（含 content_hash）；miss 返回 None。

    返回 (data, content_hash)，content_hash 可能为空串（旧数据无 hash 字段）。
    """
    try:
        with _zip_lock:
            index = _zip_index()
            entry = index.get(object_key)
            if not entry:
                return None
            path = os.path.join(ZIP_CACHE_DIR, entry.get("file", ""))
            if not os.path.isfile(path):
                index.pop(object_key, None)
                _zip_write_index(index)
                return None
            with open(path, "rb") as f:
                data = f.read()
            return (data, entry.get("hash", ""))
    except Exception as e:
        log.warning(f"zip cache get failed: {e}")
        return None


def zip_cache_put(object_key: str, data: bytes, content_hash: str = "") -> None:
    """写入磁盘 zip 缓存（原子替换 + LRU 清理）。

    content_hash: 技能内容哈希（可选），用于缓存命中时校验是否过期（Issue #254）。
    """
    try:
        with _zip_lock:
            os.makedirs(ZIP_CACHE_DIR, exist_ok=True)
            fname = hashlib.sha256(object_key.encode("utf-8")).hexdigest()[:24] + ".zip"
            path = os.path.join(ZIP_CACHE_DIR, fname)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            index = _zip_index()
            index[object_key] = {"file": fname, "size": len(data),
                                 "mtime": os.path.getmtime(path),
                                 "tag": skill_tag_from_key(object_key),
                                 "hash": content_hash or ""}
            _zip_write_index(index)
            _zip_evict(index)
    except Exception as e:
        log.warning(f"zip cache put failed: {e}")


def _zip_evict(index: dict) -> None:
    """LRU 清理：总大小超限删最旧；同技能版本数超限删旧版本。"""
    try:
        # 同技能版本数控制（按版本号升序删旧，保留最新）
        by_tag: dict[str, list[str]] = {}
        for k, e in index.items():
            by_tag.setdefault(e.get("tag", ""), []).append(k)
        for tag, keys in by_tag.items():
            if len(keys) > ZIP_CACHE_MAX_VERSIONS:
                def _ver(k):
                    return parse_skill_key(k).get("version", 0)
                keys_sorted = sorted(keys, key=_ver)
                for k in keys_sorted[: len(keys) - ZIP_CACHE_MAX_VERSIONS]:
                    _zip_remove(index, k)
        # 总容量控制（删最旧直至达标）
        total = sum(e.get("size", 0) for e in index.values())
        if total > ZIP_CACHE_MAX_BYTES:
            order = sorted(index.keys(), key=lambda k: index[k].get("mtime", 0))
            for k in order:
                if total <= ZIP_CACHE_MAX_BYTES:
                    break
                total -= index[k].get("size", 0)
                _zip_remove(index, k)
        _zip_write_index(index)
    except Exception as e:
        log.warning(f"zip cache evict failed: {e}")


def _zip_remove(index: dict, object_key: str) -> None:
    entry = index.pop(object_key, None)
    if entry:
        try:
            p = os.path.join(ZIP_CACHE_DIR, entry.get("file", ""))
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass


def zip_cache_invalidate(object_key: str) -> None:
    """按对象键失效（删除技能/转换后调用）。"""
    try:
        with _zip_lock:
            index = _zip_index()
            _zip_remove(index, object_key)
            _zip_write_index(index)
    except Exception as e:
        log.warning(f"zip cache invalidate failed: {e}")


# ===== 内存 LRU 提取缓存（SKILL.md 文本） =====

_mem_cache: dict[str, tuple[str, int, str]] = {}  # skill_name -> (content, size, content_hash)
_mem_lock = threading.Lock()


def mem_cache_get(skill_name: str) -> str | None:
    """内存缓存读取（向后兼容，返回纯 content）；miss 返回 None。

    如需同时获取 content_hash 用 mem_cache_get_with_hash()。
    """
    result = mem_cache_get_with_hash(skill_name)
    return result[0] if result else None


def mem_cache_get_with_hash(skill_name: str) -> tuple[str, str] | None:
    """内存缓存读取（含 content_hash）；miss 返回 None。

    返回 (content, content_hash)，content_hash 可能为空串（旧数据/未传入时）。
    """
    with _mem_lock:
        hit = _mem_cache.get(skill_name)
        if hit:
            _mem_cache.pop(skill_name)
            _mem_cache[skill_name] = hit  # LRU 触碰
            return (hit[0], hit[2] if len(hit) > 2 else "")
        return None


def mem_cache_put(skill_name: str, content: str, content_hash: str = "") -> None:
    """写入内存缓存。

    content_hash: 技能内容哈希（可选），用于缓存命中时校验是否过期（Issue #254）。
    """
    if not content:
        return
    with _mem_lock:
        _mem_cache[skill_name] = (content, len(content.encode("utf-8")), content_hash or "")
        total = sum(v[1] for v in _mem_cache.values())
        while total > MEM_CACHE_MAX_BYTES and len(_mem_cache) > 1:
            _, v = next(iter(_mem_cache.items()))
            _mem_cache.pop(next(iter(_mem_cache.keys())))
            total -= v[1]


def mem_cache_invalidate(skill_name: str) -> None:
    with _mem_lock:
        _mem_cache.pop(skill_name, None)


def invalidate_skill_caches(object_key: str) -> None:
    """上传/转换/删除技能后统一失效两级缓存。"""
    info = parse_skill_key(object_key)
    zip_cache_invalidate(object_key)
    if info.get("skill_name"):
        mem_cache_invalidate(info["skill_name"])


# ===== 技能 zip 工具函数（业务侧复用） =====

def get_skill_zip_bytes(object_key: str) -> bytes | None:
    """获取技能 zip 字节：磁盘缓存优先 → 技能存储回源（OBS 或单机本地）。storage=None 返回 None。"""
    cached = zip_cache_get(object_key)
    if cached is not None:
        return cached
    storage = get_skill_storage()
    if storage is None:
        return None
    data = storage.read_bytes(object_key)
    if data is not None:
        zip_cache_put(object_key, data)
    return data


def _verify_skill_cache_hash(skill_name: str, cached_hash: str) -> bool:
    """校验缓存的 content_hash 是否与 DB 最新 hash 一致（Issue #254）。

    - 缓存无 hash（旧数据/未传入）→ 不校验，返回 True（向后兼容）
    - DB 查询失败/技能不存在 → 不校验，返回 True（降级，不阻塞读）
    - hash 一致 → True（走快路径）
    - hash 不一致 → False（需失效回源）
    """
    if not cached_hash:
        return True  # 缓存无 hash，不校验（向后兼容）
    try:
        from src.user.db import get_skill_content_hash
        _, db_hash, _ = get_skill_content_hash(skill_name)
        if not db_hash:
            return True  # DB 无记录或 hash 为空，不阻塞
        return db_hash == cached_hash
    except Exception as e:
        log.warning(f"cache hash verify failed for '{skill_name}': {e}")
        return True  # 查询异常，降级不阻塞


def skill_md_content(object_key: str, skill_name: str) -> str | None:
    """详情接口用：内存缓存 → 磁盘/OBS 获取 zip → 提取 SKILL.md（v2.4 固定 key，无版本）。

    Issue #254：缓存命中后校验 content_hash，DB hash 不一致则失效缓存回源 OBS，
    保证跨节点缓存一致性。hash 一致走快路径不退化性能。
    """
    # 内存缓存命中 → 校验 hash
    mem_hit = mem_cache_get_with_hash(skill_name)
    if mem_hit is not None:
        content, cached_hash = mem_hit
        if _verify_skill_cache_hash(skill_name, cached_hash):
            return content  # hash 一致，走快路径
        # hash 不一致 → 失效内存缓存，回源
        mem_cache_invalidate(skill_name)

    # 磁盘缓存 → OBS 回源
    zip_hit = zip_cache_get_with_hash(object_key)
    if zip_hit is not None:
        zip_bytes, cached_hash = zip_hit
        if _verify_skill_cache_hash(skill_name, cached_hash):
            content = extract_skill_md(zip_bytes, skill_name) if zip_bytes else None
            if content:
                mem_cache_put(skill_name, content, cached_hash)
            return content
        # hash 不一致 → 失效磁盘缓存，回源 OBS
        zip_cache_invalidate(object_key)

    # 回源技能存储（OBS 或单机本地）
    storage = get_skill_storage()
    if storage is None:
        return None
    data = storage.read_bytes(object_key)
    if data is None:
        return None
    # 回源后获取最新 hash 写入缓存
    content = extract_skill_md(data, skill_name)
    db_hash = ""
    try:
        from src.user.db import get_skill_content_hash
        _, db_hash, _ = get_skill_content_hash(skill_name)
    except Exception:
        pass
    zip_cache_put(object_key, data, db_hash)
    if content:
        mem_cache_put(skill_name, content, db_hash)
    return content


def extract_skill_md(zip_bytes: bytes, skill_name: str) -> str | None:
    """从技能 zip 中提取 SKILL.md 文本（按 entry 解压，不全量解压；文件名大小写容错）。"""
    if not zip_bytes:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            candidates = []
            for n in names:
                base = n.rstrip("/").split("/")
                if len(base) == 2 and base[0] == skill_name and base[1].lower() == "skill.md":
                    candidates.append(n)
            if not candidates:
                # 兜底：根目录下的 skill.md（任意大小写）
                for n in names:
                    if n.lower().endswith("skill.md") and n.count("/") == 1 and n.split("/")[0] == skill_name:
                        candidates.append(n)
            if not candidates:
                return None
            raw = zf.read(candidates[0])
            return raw.decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"extract_skill_md failed: {e}")
        return None
