# Hermes ACP 临时目录权限问题修复方案

## 一、问题根因

### 1.1 现象

hermes ACP 模式启动时报 `[Errno 13] Permission denied: '/tmp/hermes-acp-homes/xxx'`，任务 1-3 秒内失败，报 `task timed out after 3600.0s`，0 条 task_messages。

### 1.2 根因

代码中有两处创建 hermes 临时目录，都使用 `/tmp` 作为根目录：

**位置一：hermes_acp.py 第 89 行（ACP 模式）**

```python
_hermes_homes_root = os.path.join(tempfile.gettempdir(), "hermes-acp-homes")
os.makedirs(_hermes_homes_root, exist_ok=True)
iso_home = os.path.join(_hermes_homes_root, _cwd_hash)
os.makedirs(iso_home, exist_ok=True)
```

**位置二：executor.py 第 459 行（CLI 模式）**

```python
_iso_home = tempfile.mkdtemp(prefix="hermes-iso-")
```

`/tmp` 是系统公共目录，多用户环境下不同用户创建的子目录默认互不可访问。当 grape 用户先创建 `/tmp/hermes-acp-homes/`（属主 grape:grape，权限 700），gtd 用户后续无法在其中创建子目录，导致 Permission denied。

### 1.3 影响范围

同一台机器上多个用户各自跑 grape worker + hermes 引擎时，先启动的用户"锁定"了 `/tmp/hermes-acp-homes/` 目录，后启动的用户 hermes ACP 全部失败。

---

## 二、修复方案

### 2.1 方案：使用当前用户家目录下的专用临时目录

将 hermes ACP 的临时目录从 `/tmp/hermes-acp-homes/` 改为 `~/.cache/hermes-acp-homes/`（当前用户家目录下的缓存目录），彻底避免跨用户权限问题。

### 2.2 修改位置一：hermes_acp.py

文件：`src/agent/hermes_acp.py`，约第 89 行

修改前：
```python
import tempfile
_hermes_homes_root = os.path.join(tempfile.gettempdir(), "hermes-acp-homes")
os.makedirs(_hermes_homes_root, exist_ok=True)
iso_home = os.path.join(_hermes_homes_root, _cwd_hash)
os.makedirs(iso_home, exist_ok=True)
```

修改后：
```python
import tempfile
# 使用当前用户家目录下的缓存目录，避免 /tmp 多用户权限冲突
_home_cache = os.path.join(os.path.expanduser("~"), ".cache", "hermes-acp-homes")
os.makedirs(_home_cache, exist_ok=True)
iso_home = os.path.join(_home_cache, _cwd_hash)
os.makedirs(iso_home, exist_ok=True)
```

### 2.3 修改位置二：executor.py

文件：`src/agent/executor.py`，约第 459 行

修改前：
```python
import tempfile, shutil as _sh
_iso_home = tempfile.mkdtemp(prefix="hermes-iso-")
```

修改后：
```python
import tempfile, shutil as _sh
# 使用当前用户家目录下的缓存目录，避免 /tmp 多用户权限冲突
_hermes_iso_root = os.path.join(os.path.expanduser("~"), ".cache", "hermes-iso")
os.makedirs(_hermes_iso_root, exist_ok=True)
_iso_home = tempfile.mkdtemp(prefix="hermes-iso-", dir=_hermes_iso_root)
```

### 2.4 清理逻辑同步修改

executor.py 第 745 行清理 `_iso_home` 的逻辑不需要改（`shutil.rmtree` 删除的是 `_iso_home` 指向的具体目录，无论在哪个父目录下都能正常删除）。

hermes_acp.py 第 509 行注释提到"不再 dispose overlay 目录"，保持不变。

---

## 三、优势对比

| 维度 | 修改前（/tmp） | 修改后（~/.cache/） |
|------|---------------|-------------------|
| 多用户权限 | 需手动 chmod 1777，否则冲突 | 天然隔离，各用户家目录独立 |
| 清理 | sweeper 定期清理 + 手动 | 同前，目录位置变化不影响清理逻辑 |
| 磁盘空间 | /tmp 可能是 tmpfs（内存）有大小限制 | ~/.cache 在磁盘上，空间通常更充裕 |
| 安全性 | /tmp 全局可读（信息泄露风险） | 家目录仅当前用户可读 |
| 标准合规 | /tmp 适合临时文件但不适合持久化 overlay | ~/.cache 符合 XDG Base Directory 规范 |

---

## 四、修改清单

| 步骤 | 文件 | 行号 | 修改内容 |
|------|------|------|---------|
| 1 | src/agent/hermes_acp.py | ~89 | `/tmp/hermes-acp-homes` → `~/.cache/hermes-acp-homes` |
| 2 | src/agent/executor.py | ~459 | `tempfile.mkdtemp(prefix="hermes-iso-")` → `mkdtemp(dir=~/.cache/hermes-iso)` |

---

## 五、验证

1. 在多用户机器上（如 203.0.113.1 有 grape 和 gtd 两个用户），分别启动 worker
2. 确认 gtd 用户的 hermes ACP 不再报 Permission denied
3. 确认 `~/.cache/hermes-acp-homes/` 目录正确创建
4. 确认 hermes 任务正常执行（有 task_messages 产出）
5. 确认两个用户的 overlay 目录互不干扰
