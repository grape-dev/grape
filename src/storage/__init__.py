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
    """生成技能 zip 对象键（固定 key，v2.4 去版本化；#444 根治：repo 段含 provider）：
    {PREFIX}/skills/{scope}/{owner}/{skill_name}/{skill_name}.zip          （custom/official）
    {PREFIX}/skills/repo/{provider}/{owner}/{repo}/{skill_name}/{name}.zip （repo，#444 根治）

    scope: custom/repo/official；owner: user_id（custom）或 repo_key（repo，owner/repo 两级）；
    动态片段全部过白名单。version/rand8 参数保留仅为兼容旧调用，固定 key 模式下忽略。

    #444 根治（S12）：repo 对象键补 provider 段——gitcode:acme/web 与 github:acme/web
    不再共享同一对象键（对齐 workspace_repo_dir / knowledge 的 provider 隔离规范）。
    owner 带 provider 前缀（gitcode:acme/web）→ 键含该 provider；裸 owner/repo 默认
    gitcode（to_repo_key 同款默认）。存量行 file_path 保持旧格式不动（读侧按行内
    file_path 直读，降级兼容）；行在下一次覆盖上传时自然迁到新键（懒迁移，零删除）。
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
        # provider 段（#444 根治）：repo_key 前缀即 provider；无前缀默认 gitcode
        _owner = owner or ""
        _provider = "gitcode"
        if ":" in _owner:
            _provider, _owner = _owner.split(":", 1)
            if not _RE_NAME.match(_provider):
                raise ValueError(f"invalid provider in repo_key: {owner}")
        parts = _owner.split("/", 1)
        if len(parts) != 2 or not _RE_NAME.match(parts[0]) or not _RE_NAME.match(parts[1]):
            raise ValueError(f"invalid repo_key: {owner}")
        owner_seg = f"{_provider}/{parts[0]}/{parts[1]}"
    else:  # official
        owner_seg = ""
    from src.config import OBS_PREFIX
    prefix = (OBS_PREFIX or "").strip("/")
    base = f"skills/{scope}/{owner_seg}" if owner_seg else f"skills/{scope}"
    return f"{prefix}/{base}/{skill_name}/{skill_name}.zip"


def parse_skill_key(object_key: str) -> dict:
    """从对象键解析技能信息（缓存失效/清理用）。返回 {scope, owner, skill_name, version, provider}。

    固定 key 模型下无版本段，version 恒为 0（兼容旧结构解析）。
    #444 根治：repo 新格式含 provider 段（/skills/repo/{provider}/{owner}/{repo}/...），
    provider 字段供定位层精确查询（gitcode:owner/repo → DB owner_id 精确命中）。
    旧格式（无 provider 段）仍可解析（存量行 file_path 兼容），provider 为空串。
    """
    key = object_key or ""
    m = re.search(r"/skills/custom/(\d+)/([^/]+)/([^/]+)\.zip", key)
    if m:
        return {"scope": "custom", "owner": m.group(1), "skill_name": m.group(2), "version": 0, "provider": ""}
    m = re.search(r"/skills/repo/([\w.\-]+)/([\w.\-]+)/([\w.\-]+)/([^/]+)/([^/]+)\.zip", key)
    if m:
        # 新格式（#444 根治）：provider 段显式；owner 还原为 owner/repo（不带前缀）
        return {"scope": "repo", "owner": f"{m.group(2)}/{m.group(3)}",
                "skill_name": m.group(4), "version": 0, "provider": m.group(1)}
    m = re.search(r"/skills/repo/([\w.\-]+)/([\w.\-]+)/([^/]+)/([^/]+)\.zip", key)
    if m:
        # 旧格式（存量行 file_path）：无 provider 段
        return {"scope": "repo", "owner": f"{m.group(1)}/{m.group(2)}",
                "skill_name": m.group(3), "version": 0, "provider": ""}
    m = re.search(r"/skills/official/([^/]+)/([^/]+)\.zip", key)
    if m:
        return {"scope": "official", "owner": "", "skill_name": m.group(1), "version": 0, "provider": ""}
    return {"scope": "", "owner": "", "skill_name": "", "version": 0, "provider": ""}


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

    返回纯 bytes（向后兼容）；如需同时获取 content_hash/skill_id 用 zip_cache_get_with_hash()。
    """
    result = zip_cache_get_with_hash(object_key)
    return result[0] if result else None


def zip_cache_get_with_hash(object_key: str) -> tuple[bytes, str, int] | None:
    """磁盘 zip 缓存读取（含 content_hash + skill_id 锚点）；miss 返回 None。

    Issue #444 id 锚点：返回 (data, content_hash, skill_id)。skill_id 为 0 表示
    旧格式条目（无 sid 字段，向后兼容——校验层降级走 by_key 定位）。
    content_hash 可能为空串（旧数据无 hash 字段）。
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
            return (data, entry.get("hash", ""), int(entry.get("sid") or 0))
    except Exception as e:
        log.warning(f"zip cache get failed: {e}")
        return None


def zip_cache_put(object_key: str, data: bytes, content_hash: str = "",
                  skill_id: int = 0) -> None:
    """写入磁盘 zip 缓存（原子替换 + LRU 清理）。

    content_hash: 技能内容哈希（可选），用于缓存命中时校验是否过期（Issue #254）。
    skill_id: 技能主键 id 锚点（Issue #444 id 锚点校验；0 表示未知——旧调用方兼容，
    校验层对 sid=0 降级走 by_key 定位）。
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
                                 "hash": content_hash or "",
                                 "sid": int(skill_id or 0)}
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

_mem_cache: dict[str, tuple[str, int, str, int]] = {}  # object_key -> (content, size, content_hash, skill_id)
_mem_lock = threading.Lock()


def mem_cache_get(key: str) -> str | None:
    """内存缓存读取（向后兼容，返回纯 content）；miss 返回 None。

    Issue #444：key 口径由 skill_name 改为 object_key（防同名跨 scope 串键）。
    如需同时获取 content_hash/skill_id 用 mem_cache_get_with_hash()。
    """
    result = mem_cache_get_with_hash(key)
    return result[0] if result else None


def mem_cache_get_with_hash(key: str) -> tuple[str, str, int] | None:
    """内存缓存读取（含 content_hash + skill_id 锚点）；miss 返回 None。

    Issue #444：key 口径为 object_key。返回 (content, content_hash, skill_id)，
    content_hash 可能为空串（旧数据/未传入时）；skill_id 为 0 表示未锚定
    （向后兼容，校验层降级走 by_key 定位）。
    """
    with _mem_lock:
        hit = _mem_cache.get(key)
        if hit:
            _mem_cache.pop(key)
            _mem_cache[key] = hit  # LRU 触碰
            return (hit[0], hit[2] if len(hit) > 2 else "", hit[3] if len(hit) > 3 else 0)
        return None


def mem_cache_put(key: str, content: str, content_hash: str = "",
                  skill_id: int = 0) -> None:
    """写入内存缓存。

    key: object_key（Issue #444 口径）。
    content_hash: 技能内容哈希（可选），用于缓存命中时校验是否过期（Issue #254/#444）。
    skill_id: 技能主键 id 锚点（Issue #444；0 表示未锚定，向后兼容）。
    """
    if not content:
        return
    with _mem_lock:
        _mem_cache[key] = (content, len(content.encode("utf-8")), content_hash or "",
                           int(skill_id or 0))
        total = sum(v[1] for v in _mem_cache.values())
        while total > MEM_CACHE_MAX_BYTES and len(_mem_cache) > 1:
            _, v = next(iter(_mem_cache.items()))
            _mem_cache.pop(next(iter(_mem_cache.keys())))
            total -= v[1]


def mem_cache_invalidate(key: str) -> None:
    """失效内存缓存条目（Issue #444：key 为 object_key 口径）。"""
    with _mem_lock:
        _mem_cache.pop(key, None)


def invalidate_skill_caches(object_key: str) -> None:
    """上传/转换/删除技能后统一失效两级缓存。"""
    zip_cache_invalidate(object_key)
    # Issue #444：内存缓存 key 即 object_key（防同名跨 scope 串键），直接失效
    mem_cache_invalidate(object_key)


# ===== 技能 zip 工具函数（业务侧复用） =====

def get_skill_zip_bytes(object_key: str, skill_name: str = "") -> bytes | None:
    """获取技能 zip 字节：磁盘缓存优先 → 技能存储回源（OBS 或单机本地）。storage=None 返回 None。

    Issue #444 收尾（P-A）：任务物化链路（runtime/service、event_pipeline、
    runtime claim 现签 URL 前的取包）与详情页 skill_md_content 同属一族——
    覆盖更新后另一进程/节点须免重启感知。此前本函数"磁盘缓存命中即返回"，
    零校验：双主各自独立 zip 缓存目录，节点 1 覆盖后节点 2 的任务物化仍解压
    旧 zip。现套用同款读时校验：命中后 _verify_skill_cache_hash（sid 锚点 /
    旧格式 by_key 降级，与详情页完全一致）；不一致失效回源。回源后定位行
    并把 (hash, sid) 写入缓存条目——物化链路的缓存条目与详情页同构，
    后续命中同样走锚点快路径。
    降级语义与全链路一致：缓存无 hash / DB 无行 / DB 异常 → 不校验直接返回
    （存量与降级路径零回归，读永不阻塞）。
    """
    cached = zip_cache_get_with_hash(object_key)
    if cached is not None:
        zip_bytes, cached_hash, cached_sid = cached
        if _verify_skill_cache_hash(object_key, cached_hash, cached_sid):
            return zip_bytes
        # hash 不一致 → 失效磁盘缓存，回源
        zip_cache_invalidate(object_key)
    storage = get_skill_storage()
    if storage is None:
        return None
    data = storage.read_bytes(object_key)
    if data is not None:
        # 回源后定位行写 (hash, sid)——与 skill_md_content 回源分支同构，
        # 使后续物化命中也走锚点校验快路径
        db_hash, db_sid = _db_hash_for_object_key(object_key, skill_name)
        zip_cache_put(object_key, data, db_hash, db_sid)
    return data


def _db_hash_for_object_key(object_key: str, skill_name: str) -> tuple[str, int]:
    """按 object_key 解析 (skill_name, scope, owner) 查 DB 最新 (content_hash, skill_id)。

    Issue #444 id 锚点架构的【定位层】：冷读（缓存无 sid）时由 key 反解三元组定位行。
    首次定位后 skill_id 进入缓存条目，后续校验走 get_skill_hash_by_id（锚点层）——
    主键/业务键模型未来演进时，只需同步维护本定位层，校验层不变。

    - repo scope（新格式键，#444 根治）：键含 provider 段 → owner_id 精确为
      f"{provider}:{owner}" 三元组查询（UNIQUE 唯一行），返回 (hash, sid)——
      锚点在 repo 冷读路径即建立（UC12-4 翻转）
    - repo scope（旧格式键，存量行 file_path）：无 provider 段 → 取 (name, scope)
      全部行去前缀比对 owner（P1-1 全 provider 口径），行集不含 id → sid=0 降级
    - user/official：三元组精确查询（UNIQUE 约束唯一行，F3）
    - 查询失败/无行 → 返回 ("", 0)（调用方降级不阻塞读）
    """
    try:
        from src.user.db import get_skill_hash_by_key, get_skill_hashes_by_name_scope
        info = parse_skill_key(object_key)
        name = info.get("skill_name") or skill_name
        scope = info.get("scope") or ""
        if not name or not scope:
            log.warning(f"skill key parse failed, cannot locate row: '{object_key}'")
            return ("", 0)  # 键解析失败（非标准/未来格式键），降级 + 可观测
        db_scope = "user" if scope == "custom" else scope
        owner = info.get("owner") or ""
        if db_scope == "repo":
            provider = info.get("provider") or ""
            if provider:
                # 新格式键（#444 根治）：provider 段显式 → DB owner_id 精确三元组查询，
                # UNIQUE 唯一行，sid 直接锚定（repo 冷读不再 sid=0）
                sid, db_hash, _, _ = get_skill_hash_by_key(name, db_scope, f"{provider}:{owner}")
                if sid:
                    return (db_hash or "", sid)
                return ("", 0)
            # 旧格式键（存量行 file_path）：无 provider 段——取全部 (name, scope='repo')
            # 行，去前缀后比对 owner（兼容口径；行集不含 id，sid 留待锚点查询补齐）
            for db_owner, db_hash in get_skill_hashes_by_name_scope(name, db_scope):
                _db_owner = db_owner.split(":", 1)[1] if ":" in db_owner else db_owner
                if _db_owner == owner:
                    if db_hash:
                        return (db_hash, 0)
                    return ("", 0)
            return ("", 0)
        sid, db_hash, _, _ = get_skill_hash_by_key(name, db_scope, owner)
        return (db_hash or "", sid)
    except Exception as e:
        log.warning(f"db hash lookup failed for '{object_key}': {e}")
        return ("", 0)


def _anchor_hash_for_sid(skill_id: int) -> tuple[str, bool]:
    """按 skill 主键 id 查最新 content_hash（Issue #444 id 锚点架构的【锚点层】）。

    返回 (content_hash, exists)。exists=False 表示行已不存在（删除重传换 id 等）
    ——调用方必须强制失效缓存，不得降级通过（旧 id 锚定的缓存内容已不可信）。
    DB 查询异常 → ("", True) 降级（不阻塞读，区别于"行不存在"）。
    """
    try:
        from src.user.db import get_skill_hash_by_id
        sid, db_hash, _, is_active = get_skill_hash_by_id(skill_id)
        if sid == 0:
            return ("", False)  # 行不存在（物理删除/换 id）
        if not is_active:
            return ("", False)  # 软删除：行身份仍在但技能已下架，缓存内容不可信
        return (db_hash or "", True)
    except Exception as e:
        log.warning(f"anchor hash lookup failed for skill_id={skill_id}: {e}")
        return ("", True)  # 查询异常降级，不阻塞读


def _verify_skill_cache_hash(object_key: str, cached_hash: str,
                             skill_id: int = 0) -> bool:
    """校验缓存的 content_hash 是否与 DB 最新 hash 一致（Issue #254/#444 id 锚点）。

    校验次序（id 锚点架构）：
    1. skill_id > 0（缓存条目已锚定）→ get_skill_hash_by_id 按主键查——
       主键/业务键未来演进不影响此层；行不存在（换 id/软删除）→ False 强制失效；
    2. skill_id = 0（旧格式条目，向后兼容）→ 降级走 by_key 定位
       （parse_skill_key + UNIQUE 三元组，P1-1 全 provider 前缀匹配）。

    - 缓存无 hash（旧数据/未传入）→ 不校验，返回 True（向后兼容）
    - by_key 定位失败/DB 异常 → 不校验，返回 True（降级，不阻塞读）
    - hash 一致 → True（走快路径）
    - hash 不一致 → False（需失效回源）
    """
    if not cached_hash:
        return True  # 缓存无 hash，不校验（向后兼容）
    if skill_id and skill_id > 0:
        db_hash, exists = _anchor_hash_for_sid(skill_id)
        if not exists:
            return False  # 锚点行已不存在/软删除 → 强制失效（不可降级）
        if not db_hash:
            return True  # 行在但 hash 为空（未同步），降级不阻塞
        return db_hash == cached_hash
    db_hash, _sid = _db_hash_for_object_key(object_key, "")
    if not db_hash:
        return True  # DB 无记录或 hash 为空，不阻塞
    return db_hash == cached_hash


def skill_md_content(object_key: str, skill_name: str) -> str | None:
    """详情接口用：内存缓存 → 磁盘/OBS 获取 zip → 提取 SKILL.md（v2.4 固定 key，无版本）。

    Issue #254/#444：缓存命中后校验 content_hash，DB hash 不一致则失效缓存回源 OBS，
    保证跨节点/跨进程缓存一致性（key 口径为 object_key，防同名跨 scope 串键）。
    Issue #444 id 锚点：缓存条目携带 skill_id，命中校验按主键查（get_skill_hash_by_id）；
    无 sid 的旧格式条目降级走 by_key 定位（向后兼容）。回源时定位行并把 sid 写入两级缓存，
    使后续校验全部走锚点路径。
    hash 一致走快路径不退化性能。
    """
    # 内存缓存命中 → 校验（sid 锚点优先，by_key 兜底）
    mem_hit = mem_cache_get_with_hash(object_key)
    if mem_hit is not None:
        content, cached_hash, cached_sid = mem_hit
        if _verify_skill_cache_hash(object_key, cached_hash, cached_sid):
            return content  # hash 一致，走快路径
        # hash 不一致 → 失效内存缓存，回源
        mem_cache_invalidate(object_key)

    # 磁盘缓存 → OBS 回源
    zip_hit = zip_cache_get_with_hash(object_key)
    if zip_hit is not None:
        zip_bytes, cached_hash, cached_sid = zip_hit
        if _verify_skill_cache_hash(object_key, cached_hash, cached_sid):
            content = extract_skill_md(zip_bytes, skill_name) if zip_bytes else None
            if content:
                mem_cache_put(object_key, content, cached_hash, cached_sid)
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
    # 回源后定位行：取最新 hash + skill_id（锚点），写入两级缓存
    content = extract_skill_md(data, skill_name)
    db_hash, db_sid = _db_hash_for_object_key(object_key, skill_name)
    zip_cache_put(object_key, data, db_hash, db_sid)
    if content:
        mem_cache_put(object_key, content, db_hash, db_sid)
    return content


# 内置特殊技能：zip 包内目录名与 skill_name 不一致（_template 打包结构为 skills/_template/my-skill/，
# 见 scripts/sync_skills.sh SKILL_AGENT_DIRS + src/storage/skill_sync.py _pack_skill_zip）。
# extract_skill_md 对这类技能启用兜底匹配；常规技能（上传/官方同步均规范化为 {skill_name}/）不启用。
_BUILTIN_SPECIAL_SKILLS = {"_template"}


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
                # 兜底 1：内置特殊技能（如 _template，zip 内目录名与 skill_name 不一致，
                # 见 scripts/sync_skills.sh 打包结构）。常规技能保持精确匹配语义，
                # 目录名不一致即视为包内容错乱 → 返回 None（Issue #355 QA 反馈）。
                if skill_name in _BUILTIN_SPECIAL_SKILLS:
                    skill_md_entries = [n for n in names
                                        if n.lower().endswith("skill.md") and n.count("/") == 1]
                    if len(skill_md_entries) == 1:
                        candidates.append(skill_md_entries[0])
            if not candidates:
                # 兜底 2：根目录下的 skill.md（任意大小写）
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


# ===== 子技能 zip 提取（Issue #448：user/repo 技能存 OBS，references/ 按 entry 读取）=====

def list_zip_sub_skills(zip_bytes: bytes, skill_name: str) -> list:
    """列出技能 zip 内 references/ 下的子技能（Issue #448）。

    结构：{skill_name}/references/{sub}/SKILL.md。entry 精确匹配（目录名与 skill_name
    不一致即视为包内容错乱，不做兜底，语义对齐 extract_skill_md 常规技能分支）。
    返回与本地 list_sub_skills 同构：[{name, display_name, description, file_count}]。
    """
    if not zip_bytes or not skill_name:
        return []
    subs: dict = {}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            for n in names:
                parts = n.rstrip("/").split("/")
                if (len(parts) != 4 or parts[0] != skill_name
                        or parts[1] != "references" or parts[3].lower() != "skill.md"):
                    continue
                sub_name = parts[2]
                if not sub_name or sub_name in (".", "..") or sub_name in subs:
                    continue
                try:
                    front = zf.read(n).decode("utf-8", errors="replace")[:500]
                except Exception:
                    front = ""
                display_name, description = sub_name, ""
                m = re.search(r'description:\s*["\']?(.*?)["\']?$', front, re.MULTILINE)
                if m:
                    description = m.group(1)[:200]
                dm = re.search(r'name:\s*["\']?(.*?)["\']?$', front, re.MULTILINE)
                if dm:
                    display_name = dm.group(1)
                prefix = f"{skill_name}/references/{sub_name}/"
                file_count = sum(1 for x in names if x.startswith(prefix) and not x.endswith("/"))
                subs[sub_name] = {
                    "name": sub_name,
                    "display_name": display_name,
                    "description": description,
                    "file_count": file_count,
                }
    except Exception as e:
        log.warning(f"list_zip_sub_skills failed: {e}")
        return []
    return [subs[k] for k in sorted(subs)]


def read_zip_sub_skill(zip_bytes: bytes, skill_name: str, sub_name: str) -> dict | None:
    """读取 zip 内子技能 SKILL.md 及其 references/ 附件（Issue #448，本地版同款行为）。

    entry 精确匹配 {skill_name}/references/{sub_name}/SKILL.md——entry 名来自
    namelist 全量枚举后比对，天然免疫 ../ 路径穿越。附件读取
    {skill_name}/references/{sub_name}/references/ 下顶层文件，各截 2000 字符
    （对齐本地 read_sub_skill）。返回 {"content", "references"}；未找到返回 None。
    """
    if not zip_bytes or not skill_name or not sub_name:
        return None
    if "/" in sub_name or sub_name in (".", ".."):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            entry = f"{skill_name}/references/{sub_name}/SKILL.md"
            if entry not in names:
                return None
            content = zf.read(entry).decode("utf-8", errors="replace")
            refs = []
            ref_prefix = f"{skill_name}/references/{sub_name}/references/"
            for n in sorted(names):
                if not n.startswith(ref_prefix) or n.endswith("/"):
                    continue
                rest = n[len(ref_prefix):]
                if not rest or "/" in rest:
                    continue  # 只读顶层文件（对齐本地 os.listdir 行为）
                try:
                    refs.append({"name": rest, "content": zf.read(n).decode("utf-8", errors="replace")[:2000]})
                except Exception:
                    refs.append({"name": rest, "content": ""})
            return {"content": content, "references": refs}
    except Exception as e:
        log.warning(f"read_zip_sub_skill failed: {e}")
        return None
