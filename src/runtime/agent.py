"""RuntimeAgent 边端入口（python -m runtime.agent，仅外连）。

见 agent-runtime-design.md §7.4 / tech-impl.md §1.4 / skill-delivery-remote.md §3.5。
流程：discover_runtimes → register → recover_orphans → 循环 claim（带 jitter）→ 执行 → 回报。
"""
import sys

# 版本守卫：代码使用 Python 3.10+ 联合类型语法（X | None），<3.10 会在 import 期崩溃。
# 放最顶部（任何 import 之前），给清晰报错而非 traceback。
if sys.version_info < (3, 10):
    print(f"RuntimeAgent requires Python >= 3.10 (got {sys.version.split()[0]}). "
          f"请用 python3.12 重建 venv: python3.12 -m venv .venv && .venv/bin/pip install -r requirements-worker.txt",
          file=sys.stderr)
    sys.exit(1)

import asyncio, json, os, random, time
import base64 as _b64
import zipfile as _zf
import urllib.request as _urllib

from src.config import log

# v3.0：OBS 直连能力 + 自适应降级状态（运行时能力反馈 C）
_OBS_SUPPORT = False      # 启动探测结果（supports_obs_url）
_OBS_FALLBACK = False     # 直连不稳 → claim 声明 prefer_fallback
_OBS_FAIL_STREAK = 0      # 连续 URL/代拉失败计数（≥阈值降级）
_OBS_OK_STREAK = 0        # 连续成功计数（≥阈值恢复）
# 降级/恢复阈值（可配 env RUNTIME_OBS_FALLBACK_THRESHOLD，默认 3）
_OBS_FALLBACK_THRESHOLD = int(os.environ.get("RUNTIME_OBS_FALLBACK_THRESHOLD", "3"))


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return int(default)


def _discover_local_runtimes() -> list[dict]:
    """本机 CLI 列表（复用 discover_runtimes，补齐 version/healthy）。"""
    from src.runtime.registry import discover_runtimes
    out = []
    for r in discover_runtimes():
        version = ""
        healthy = False
        cli = r.get("cli_path") or ""
        if cli:
            try:
                import subprocess
                res = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=5)
                healthy = res.returncode == 0
                if healthy:
                    version = (res.stdout or "").strip().split("\n")[0][:60]
            except Exception:
                pass
        out.append({"name": r["name"], "version": version, "healthy": healthy, "cli_path": cli, "installed": r.get("installed", False)})
    return out


def _remap_cwd(path: str) -> str:
    """将服务端下发的 cwd 路径映射到本地用户 home 目录。

    服务端以 /home/grape 为基准生成 workspaces 路径，但各接入机器的用户不同
    （lvjuntao、zhangsan 等）。通过 RUNTIME_CWD_PREFIX_MAP 环境变量配置映射：
      RUNTIME_CWD_PREFIX_MAP=/home/grape=/home/lvjuntao
    支持多组映射，用逗号分隔：
      RUNTIME_CWD_PREFIX_MAP=/home/grape=/home/lvjuntao,/opt/data=/data
    未配置时默认将 /home/grape 映射到当前用户的 home 目录。
    """
    if not path:
        return path
    import os as _os
    mapping = _os.environ.get("RUNTIME_CWD_PREFIX_MAP", "")
    if not mapping:
        # 默认映射：/home/grape → 当前用户 home
        default_src = "/home/grape"
        local_home = _os.path.expanduser("~")
        if path.startswith(default_src) and local_home != default_src:
            return local_home + path[len(default_src):]
        return path
    for pair in mapping.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        src, dst = pair.split("=", 1)
        src, dst = src.strip(), dst.strip()
        if src and path.startswith(src):
            return dst + path[len(src):]
    return path


def _backend_for_engine(engine_name: str | None = "") -> str:
    """由引擎实例名推导执行后端（hermes-xxx → hermes；codearts-xxx → codearts；其余默认 opencode）。"""
    name = (engine_name or "").lower()
    if name.startswith("hermes"):
        return "hermes"
    if name.startswith("codearts"):
        return "codearts"
    return "opencode"


def _scan_local_skills() -> list[dict]:
    """扫描 worker 上各 engine 本地物理存在的 skill，返回 per-engine skill 列表。

    每个 engine 扫描其标准 skill 目录。
    每个含 SKILL.md 的子目录视为一个 skill（支持一级和二级目录结构）。
    不扫描 ~/grape/skills（grape 项目自带 skill，Web 端已纳管可见）。
    不扫描 grape-workspaces 下的临时 .skills 目录（task 物化的，非持久）。
    """
    import os as _os

    home = _os.path.expanduser("~")

    # per-engine 专属 skill 目录
    engine_dirs = {
        "hermes": [_os.path.join(home, ".hermes", "skills")],
        "opencode": [_os.path.join(home, ".config", "opencode", "skills")],
        "codearts": [_os.path.join(home, ".codeartsdoer", "cli-data", "system", "skills")],
    }

    # 用户自建 skill 目录（通过环境变量配置，可选）
    extra_dirs = []
    extra = _os.environ.get("RUNTIME_EXTRA_SKILL_DIRS", "")
    if extra:
        for d in extra.split(":"):
            d = d.strip()
            if d and _os.path.isdir(d):
                extra_dirs.append(d)

    def _scan_dir(base_dir: str) -> list[str]:
        """扫描目录下含 SKILL.md 的子目录，返回 skill_name 列表。
        支持一级（base/skill/SKILL.md）和二级（base/category/skill/SKILL.md）结构。"""
        skills = []
        if not _os.path.isdir(base_dir):
            return skills
        try:
            for entry in _os.listdir(base_dir):
                entry_path = _os.path.join(base_dir, entry)
                if not _os.path.isdir(entry_path):
                    continue
                # 一级：base/entry/SKILL.md
                if _os.path.isfile(_os.path.join(entry_path, "SKILL.md")):
                    skills.append(entry)
                else:
                    # 二级：base/entry/sub/SKILL.md
                    try:
                        for sub in _os.listdir(entry_path):
                            sub_path = _os.path.join(entry_path, sub)
                            if _os.path.isdir(sub_path) and _os.path.isfile(_os.path.join(sub_path, "SKILL.md")):
                                skills.append(sub)
                    except Exception:
                        pass
        except Exception:
            pass
        return skills

    # 扫描用户自建目录（合并到所有 engine）
    common_skills: set[str] = set()
    for d in extra_dirs:
        common_skills.update(_scan_dir(d))

    # 扫描 per-engine 目录 + 合用用户自建
    result = []
    for engine, dirs in engine_dirs.items():
        engine_skills: set[str] = set()
        for d in dirs:
            engine_skills.update(_scan_dir(d))
        engine_skills |= common_skills
        if engine_skills:
            result.append({"engine": engine, "skills": sorted(engine_skills)})

    return result


def _scan_local_skills_with_detail(engine_type: str) -> list[dict]:
    """扫描指定 engine 的 skill 详情（name/description/path/file_count/provider）。

    worker 端执行，扫描结果通过 skills-report API 回报给服务端。
    """
    import os as _os

    all_skills = _scan_local_skills()
    engine_key = (engine_type or "").strip().lower()
    skill_names = []
    for entry in all_skills:
        if (entry.get("engine") or "").strip().lower() == engine_key:
            skill_names = entry.get("skills") or []
            break

    home = _os.path.expanduser("~")
    engine_dirs = {
        "hermes": [_os.path.join(home, ".hermes", "skills")],
        "opencode": [_os.path.join(home, ".config", "opencode", "skills")],
        "codearts": [_os.path.join(home, ".codeartsdoer", "cli-data", "system", "skills")],
    }
    extra = _os.environ.get("RUNTIME_EXTRA_SKILL_DIRS", "")
    scan_dirs = list(engine_dirs.get(engine_key, []))
    if extra:
        for d in extra.split(":"):
            d = d.strip()
            if d and _os.path.isdir(d):
                scan_dirs.append(d)

    skill_details = []
    for sname in skill_names:
        detail = {"name": sname, "description": "", "path": "", "file_count": 0, "provider": engine_type}
        for base_dir in scan_dirs:
            p1 = _os.path.join(base_dir, sname)
            if _os.path.isfile(_os.path.join(p1, "SKILL.md")):
                detail["path"] = p1
                break
            if _os.path.isdir(base_dir):
                for cat in _os.listdir(base_dir):
                    p2 = _os.path.join(base_dir, cat, sname)
                    if _os.path.isfile(_os.path.join(p2, "SKILL.md")):
                        detail["path"] = p2
                        break
                if detail["path"]:
                    break
        if detail["path"]:
            try:
                with open(_os.path.join(detail["path"], "SKILL.md"), "r", encoding="utf-8") as f:
                    content = f.read()
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end > 0:
                        fm = content[3:end]
                        for line in fm.split("\n"):
                            if line.strip().startswith("description:"):
                                desc = line.split("description:", 1)[1].strip()
                                desc = desc.strip("\"'")
                                if desc:
                                    detail["description"] = desc[:200]
                                break
            except Exception:
                pass
            try:
                detail["file_count"] = sum(1 for _ in _os.walk(detail["path"]))
            except Exception:
                pass
        skill_details.append(detail)
    return skill_details


def _cleanup_task_tmp(task: dict):
    """task 完成后清理 agent 在 /tmp 下产生的工作残留。
    agent 执行过程中会在 /tmp 创建临时目录（tf-deploy-*/blog-build-*/blog-deploy.*
    /tmp/ptmpl_* 等），task 完成后不清理会叠加撑满 tmpfs。
    只清理已知的 agent 工作产物前缀，不碰其他进程的文件。"""
    import shutil as _shutil
    import glob as _glob
    tmp_patterns = [
        "/tmp/tf-deploy-*",
        "/tmp/tf-sys-xhy*",
        "/tmp/blog-build-*",
        "/tmp/blog-deploy*",
        "/tmp/blog-gen*",
        "/tmp/blog-publish-*",
        "/tmp/blog-sys-*.tar.gz",
        "/tmp/ptmpl_*",
        "/tmp/tf-state-backup.json",
        "/tmp/inventory_state.json",
        "/tmp/inventory*.json",
        # bun/opencode 的 libopentui JIT .so 缓存——按 cwd 生成不同 hash，
        # JSC_diskCachePath 重定向大部分但 FFI 层仍有少量写入 /tmp，需兜底清理。
        "/tmp/.39ee*.so",
    ]
    cleaned = 0
    for pattern in tmp_patterns:
        for path in _glob.glob(pattern):
            try:
                if os.path.isdir(path):
                    _shutil.rmtree(path, ignore_errors=True)
                elif os.path.isfile(path):
                    os.remove(path)
                cleaned += 1
            except Exception:
                pass
    if cleaned:
        log.info(f"cleaned {cleaned} temp files/dirs from /tmp after task")


def _kill_stale_engine(task: dict, backend: str = "opencode"):
    """超时/终止后杀掉对应引擎子进程（opencode run / hermes acp / hermes chat / codearts run）。
    按 cwd 匹配，避免误杀其他任务。SIGTERM 后 3s SIGKILL 兜底。"""
    import subprocess, signal, time as _time
    cwd = _remap_cwd(task.get("cwd") or os.getcwd())
    # hermes ACP 模式用 `hermes acp`，CLI 模式用 `hermes chat`；两个模式都匹配
    patterns = {
        "hermes": ["hermes acp", "hermes chat", "hermes -z"],
        "codearts": ["codearts run"],
    }.get(backend, ["opencode run"])
    killed_any = False
    for pattern in patterns:
        try:
            out = subprocess.run(
                ["pgrep", "-u", os.environ.get("USER", os.getlogin() if hasattr(os, "getlogin") else ""), "-f", pattern],
                capture_output=True, text=True, timeout=5
            )
            for pid_str in out.stdout.strip().split():
                if not pid_str:
                    continue
                try:
                    pid = int(pid_str)
                    proc_cwd = os.readlink(f"/proc/{pid}/cwd")
                    if proc_cwd.startswith(cwd):
                        pgid = os.getpgid(pid)
                        os.killpg(pgid, signal.SIGTERM)
                        log.info(f"killed {backend} pid={pid} pgid={pgid} cwd={proc_cwd}")
                        killed_any = True
                        # 3s 后检查进程是否还在，SIGKILL 兜底
                        _time.sleep(0.1)
                        try:
                            os.killpg(pgid, 0)  # check if still alive
                            # 启动后台计时器 3s 后 SIGKILL
                            import threading
                            def _force_kill(pg, delay=3):
                                _time.sleep(delay)
                                try:
                                    os.killpg(pg, signal.SIGKILL)
                                    log.warning(f"force killed pgid={pg} (SIGKILL after {delay}s)")
                                except Exception:
                                    pass
                            threading.Thread(target=_force_kill, args=(pgid,), daemon=True).start()
                        except ProcessLookupError:
                            pass  # already dead
                except Exception as e:
                    log.debug(f"skip pid={pid}: {e}")
        except Exception as e:
            log.warning(f"_kill_stale_engine({backend}, pattern={pattern}) failed: {e}")
    if not killed_any:
        log.info(f"_kill_stale_engine({backend}): no matching process found for cwd={cwd}")


def _fetch_skill_zip(skill_obs_url: str, task_id: str = "") -> bytes | None:
    """v3.0 技能 zip 获取降级链：① URL 直连 OBS（重试）→ ② 服务端代拉（复用 zip_cache）。

    返回 zip 字节；两条路都失败返回 None（调用方降级 skill_files 或回池）。
    """
    timeout = float(os.environ.get("RUNTIME_OBS_FETCH_TIMEOUT", "10"))
    retries = int(os.environ.get("RUNTIME_OBS_FETCH_RETRIES", "3"))
    # ① URL 直连 OBS（预签名 URL，重试）
    for i in range(retries):
        try:
            with _urllib.urlopen(skill_obs_url, timeout=timeout) as resp:
                data = resp.read()
            if data:
                return data
            log.warning(f"[skill] OBS URL 直连返回空({i + 1}/{retries})")
        except Exception as e:
            log.warning(f"[skill] OBS URL 直连失败({i + 1}/{retries}): {type(e).__name__}")
    # ② 服务端代拉（服务端现查 file_path → zip_cache → base64）
    server = os.environ.get("RUNTIME_SERVER_URL", "http://localhost:3000")
    token = os.environ.get("RUNTIME_TOKEN", "")
    if task_id and token:
        try:
            req = _urllib.Request(
                f"{server}/api/runtime/tasks/{task_id}/skill-zip",
                headers={"Authorization": f"Bearer {token}"})
            with _urllib.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read())
            b64 = (payload or {}).get("zip_base64") or ""
            if b64:
                log.info(f"[skill] 服务端代拉成功 task={task_id}")
                return _b64.b64decode(b64)
            log.warning(f"[skill] 代拉返回空 task={task_id}")
        except Exception as e:
            log.warning(f"[skill] 服务端代拉失败: {type(e).__name__}")
    return None


def _fetch_skill_zip(skill_obs_url: str, task_id: str = "") -> bytes | None:
    """v3.0 技能 zip 获取降级链：① URL 直连 OBS（重试）→ ② 服务端代拉（复用 zip_cache）。

    返回 zip 字节；两条路都失败返回 None（调用方降级 skill_files 或回池）。
    """
    timeout = float(os.environ.get("RUNTIME_OBS_FETCH_TIMEOUT", "10"))
    retries = int(os.environ.get("RUNTIME_OBS_FETCH_RETRIES", "3"))
    # ① URL 直连 OBS（预签名 URL，重试）
    for i in range(retries):
        try:
            with _urllib.urlopen(skill_obs_url, timeout=timeout) as resp:
                data = resp.read()
            if data:
                return data
            log.warning(f"[skill] OBS URL 直连返回空({i + 1}/{retries})")
        except Exception as e:
            log.warning(f"[skill] OBS URL 直连失败({i + 1}/{retries}): {type(e).__name__}")
    # ② 服务端代拉（服务端现查 file_path → zip_cache → base64）
    server = os.environ.get("RUNTIME_SERVER_URL", "http://localhost:3000")
    token = os.environ.get("RUNTIME_TOKEN", "")
    if task_id and token:
        try:
            req = _urllib.Request(
                f"{server}/api/runtime/tasks/{task_id}/skill-zip",
                headers={"Authorization": f"Bearer {token}"})
            with _urllib.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read())
            b64 = (payload or {}).get("zip_base64") or ""
            if b64:
                log.info(f"[skill] 服务端代拉成功 task={task_id}")
                return _b64.b64decode(b64)
            log.warning(f"[skill] 代拉返回空 task={task_id}")
        except Exception as e:
            log.warning(f"[skill] 服务端代拉失败: {type(e).__name__}")
    return None


async def _stream_runtime_async(*args, **kwargs):
    """把同步生成器 stream_runtime 放到后台线程迭代，事件循环不被 readline() 阻塞。

    见 worker-event-loop-block-rootcause.md §8：方案 1（await sleep(0)）只在生成器每次 yield 时
    让出；当 opencode 执行长命令静默无输出（readline 阻塞）时事件循环仍被占住。本函数把
    同步迭代移入线程，通过线程安全 queue.Queue 桥接事件，彻底消除阻塞。
    """
    import queue as _queue
    from src.runtime.executor import stream_runtime
    q: _queue.Queue = _queue.Queue(maxsize=256)
    sentinel = object()

    def _produce():
        try:
            for evt in stream_runtime(*args, **kwargs):
                while True:
                    try:
                        q.put_nowait(evt)
                        break
                    except _queue.Full:
                        # 队列满：短暂让出，等消费者消费；sleep 在子线程安全
                        import time as _t
                        _t.sleep(0.001)
        except Exception as e:
            log.warning(f"stream_runtime producer error: {e}")
        finally:
            q.put(sentinel)

    import threading
    _t = threading.Thread(target=_produce, daemon=True)
    _t.start()
    while True:
        try:
            evt = q.get_nowait()
        except _queue.Empty:
            await asyncio.sleep(0.001)  # 轮询间隔，同时让出事件循环
            continue
        if evt is sentinel:
            break
        yield evt


async def _execute_task(client, task, engine_name: str = ""):
    """执行单个任务：git worktree 前置 → 落地技能/知识 → 占位符替换 → stream_runtime → 回报。

    engine_name 为该任务所属引擎实例名（如 opencode-worker-123249），用于日志区分。
    """
    import os as _os
    tid = task.get("id", "")
    cwd = _remap_cwd(task.get("cwd") or _os.getcwd())
    _engine_tag = f"|engine:{engine_name}" if engine_name else ""

    # TaskResourceStack: unified resource lifecycle for this task
    from src.runtime.resource_stack import TaskResourceStack
    _res = TaskResourceStack(task_id=tid, cwd=cwd)
    # Register known tmp patterns for cleanup on any exit path
    for pat in [
        "/tmp/tf-deploy-*", "/tmp/tf-sys-xhy*", "/tmp/blog-build-*",
        "/tmp/blog-deploy*", "/tmp/blog-gen*", "/tmp/blog-publish-*",
        "/tmp/blog-sys-*.tar.gz", "/tmp/ptmpl_*", "/tmp/tf-state-backup.json",
        "/tmp/inventory_state.json", "/tmp/inventory*.json", "/tmp/.39ee*.so",
    ]:
        _res.track_tmp_pattern(pat)

    # Pre-execution dependency validation (Cordis inject pattern)
    from src.runtime.dependency_check import validate_task_dependencies, DependencyError
    try:
        validate_task_dependencies(task, engine_name)
    except DependencyError as _de:
        _res.dispose()
        return {"_error": str(_de), "_started": False}

    # Context compaction: will check batch size before flush
    from src.runtime.context_compaction import should_compact, compact_messages

    try:
        _os.makedirs(cwd, exist_ok=True)
    except Exception:
        pass
    env = {}
    try:
        env = json.loads(task.get("env_extra") or "{}")
    except Exception:
        pass
    # Fetch latest env (AK/SK) from server DB — overrides stale snapshot
    try:
        _fresh_env = await client.get_task_env(tid)
        if _fresh_env:
            env.update(_fresh_env)
    except Exception:
        pass
    # git worktree 前置（coding/qa/sre，X 工作模型）：必须先建 worktree 再落地技能，
    # 否则 cwd 被 .skills 物化占住（目录存在但无 .git）会导致 `git worktree add` 报
    # "already exists"，coding agent 拿不到 git 基线后自行在项目根外建 worktree → 权限卡死。
    # 见 coding-worktree-skill-collision.md §4.1（方案 A）。
    try:
        from src.runtime import repogit
        _num = repogit.issue_num_from(task.get("issue_id") or "")
        if repogit.is_git_agent(task.get("agent_type") or "") and _num and task.get("issue_session_id"):
            _wt = repogit.ensure_issue_worktree(task.get("repo_key") or "", task.get("agent_type") or "",
                                                _num, token=task.get("repo_gitcode_token") or "")
            if _wt:
                env["GIT_ASKPASS"] = _os.path.join(_wt, ".git-askpass.sh")
    except Exception as e:
        log.warning(f"git worktree ensure failed: {e}")
    skill_dir = _os.path.join(cwd, ".skills")
    prompt = (task.get("prompt") or "").replace("{{SKILL_DIR}}", skill_dir) \
                                       .replace("{{SERVER_URL}}", task.get("server_url") or "")
    try:
        # ===== v3.0：技能获取直连 OBS（降级链见 _fetch_skill_zip）=====
        # ① skill_obs_url（claim 现签预签名 URL）→ worker 直连 OBS
        # ② 失败 → 服务端代拉（POST /tasks/{id}/skill-zip，复用 zip_cache）
        # ③ 无 URL（老任务/无 OBS 技能）→ skill_files 文本物化（既有）
        skill_obs_url = task.get("skill_obs_url") or ""
        skill_zip_b64 = None
        if skill_obs_url:
            raw = _fetch_skill_zip(skill_obs_url, task_id=task.get("id", ""))
            if raw:
                skill_zip_b64 = _b64.b64encode(raw).decode()
                # v3.0 能力反馈：直连/代拉成功 → 计数恢复
                global _OBS_FAIL_STREAK, _OBS_OK_STREAK, _OBS_FALLBACK
                _OBS_FAIL_STREAK = 0
                _OBS_OK_STREAK += 1
                if _OBS_OK_STREAK >= _OBS_FALLBACK_THRESHOLD and _OBS_FALLBACK:
                    _OBS_FALLBACK = False
                    _OBS_OK_STREAK = 0
                    log.info("[skill] OBS 直连恢复（连续 3 次成功）")
            else:
                log.warning(f"[skill] OBS 直连+代拉均失败, task={task.get('id')} url={skill_obs_url[:60]}...")
                # v3.0 能力反馈：连续失败 → 声明 prefer_fallback（服务端直接代拉模式）
                _OBS_FAIL_STREAK += 1
                _OBS_OK_STREAK = 0
                if _OBS_FAIL_STREAK >= _OBS_FALLBACK_THRESHOLD and not _OBS_FALLBACK:
                    _OBS_FALLBACK = True
                    _OBS_FAIL_STREAK = 0
                    log.warning("[skill] OBS 直连不稳定，降级为服务端代拉（prefer_fallback）")
        if skill_zip_b64:
            # ===== zip 物化（直连/代拉获取的 zip → 解压 → 恢复权限）=====
            # 见 obs-skill-storage-design.md §5.7（Python 3.12+ zipfile 不再自动恢复
            # Unix 权限位，手动按 external_attr 恢复；剥离 setuid/setgid/sticky 防提权）
            raw = _b64.b64decode(skill_zip_b64)
            tmp_zip = _os.path.join(skill_dir, "_skill_delivery.zip")
            _os.makedirs(skill_dir, exist_ok=True)
            with open(tmp_zip, "wb") as f:
                f.write(raw)
            with _zf.ZipFile(tmp_zip, "r") as zf:
                zf.extractall(skill_dir)  # zip 根 = {skill_name}/，落地 .skills/{skill_name}/
                for info in zf.infolist():
                    mode = info.external_attr >> 16
                    if mode and not info.is_dir():
                        p = _os.path.join(skill_dir, info.filename)
                        if _os.path.isfile(p):
                            _os.chmod(p, mode & 0o777)
            try:
                _os.remove(tmp_zip)
            except Exception:
                pass
            env["DEVOPS_SKILLS_DIR"] = skill_dir
        else:
            # ===== 旧格式：skill_files 文本物化（老任务/老 worker 兼容）=====
            skill_files = json.loads(task.get("skill_files") or "{}")
            for _sn, files in skill_files.items():
                for rel, content in (files or {}).items():
                    dest = _os.path.join(skill_dir, _sn, rel)
                    _os.makedirs(_os.path.dirname(dest), exist_ok=True)
                    with open(dest, "w", encoding="utf-8") as f:
                        f.write(content)
            if skill_files:
                env["DEVOPS_SKILLS_DIR"] = skill_dir
    except Exception as e:
        log.warning(f"skill materialize failed: {e}")
    # project-info 落地（repo 全局共享，见 multi-issue-isolation-design.md §2.4）
    proj_dir = _os.path.join(cwd, ".project-info")
    try:
        proj_files = json.loads(task.get("project_info_files") or "{}")
        for rel, content in (proj_files or {}).items():
            if rel == ".metadata.json":
                continue
            dest = _os.path.join(proj_dir, rel)
            _os.makedirs(_os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)
        prompt = prompt.replace("{{PROJECT_INFO_DIR}}", proj_dir)
    except Exception as e:
        log.warning(f"project-info materialize failed: {e}")
    # 共享知识落地（event-conversation-design.md §3.1.1；随任务交付远端 worker）
    know_dir = _os.path.join(cwd, ".knowledge")
    try:
        know_files = json.loads(task.get("knowledge_files") or "{}")
        for rel, content in (know_files or {}).items():
            dest = _os.path.join(know_dir, rel)
            _os.makedirs(_os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)
        prompt = prompt.replace("{{KNOWLEDGE_DIR}}", know_dir)
    except Exception as e:
        log.warning(f"knowledge materialize failed: {e}")

    done = None
    batch = []
    started = False
    _last_flush = time.monotonic()  # 实时日志双触发（50条/500ms）时间基
    try:
        from src.runtime.executor import stream_runtime
        _agent_label = f"[Agent:{task.get('agent_type') or ''}|repo:{task.get('repo_key') or ''}{_engine_tag}]"
        # 逐步日志前缀：seq + 非空的 issue/agent/repo/engine，便于在 journalctl 按事件/issue/仓库定位
        _esq_parts = [f"seq={task.get('event_seq') or '?'}"]
        _issue_num = (task.get("issue_id") or "").strip().lstrip("#")
        if _issue_num and _issue_num != "0":
            _esq_parts.append(f"issue:#{_issue_num}")
        _agent_type = task.get("agent_type") or ""
        if _agent_type:
            _esq_parts.append(f"agent:{_agent_type}")
        _repo_key = task.get("repo_key") or ""
        if _repo_key:
            _esq_parts.append(f"repo:{_repo_key}")
        if engine_name:
            _esq_parts.append(f"engine:{engine_name}")
        _esq = "|".join(_esq_parts)
        # chat 任务以用户身份执行（回帖/建 issue 等 GitCode API 操作）——注入用户级 token。
        # 直接执行路径（send/IM）均传 user_token；唯队列路径曾漏传，导致
        # USER_GITCODE_TOKEN 为空、agent 回帖 401（2026-08-19 修复）。
        user_token = ""
        if (task.get("agent_type") or "") == "chat" or (task.get("event_type") or "") == "chat":
            try:
                from src.user.db import get_user_gitcode_token
                user_token = get_user_gitcode_token(task.get("user_id") or 0) or ""
            except Exception as e:
                log.warning(f"chat user token resolve failed: {e}")
        # 方案 2：线程化迭代 stream_runtime（worker-event-loop-block-rootcause.md §8.5）——
        # 同步生成器的 readline() 阻塞移到后台线程，事件循环持续调度 claim/heartbeat，
        # 根治「长命令静默无输出」期间的事件循环停摆（替代方案 1 的 await sleep(0)）。
        async for evt_type, data in _stream_runtime_async(
            prompt, cwd, session_id=task.get("runtime_session_id") or "",
            event_type=task.get("event_type") or "",
            backend_name=_backend_for_engine(engine_name),
            account=task.get("account") or "", user_id=task.get("user_id") or 0,
            env_extra=env, bot_token=task.get("repo_gitcode_token") or "",
            user_token=user_token,
            agent_label=_agent_label,
        ):
            if evt_type == "done":
                done = data
                continue
            # 引擎确已开始执行（opencode 已 Popen 并产出首个执行事件）→ 才置 running。
            # 此前任务保持 dispatched（引擎启动失败/未执行不误标 running，见 event-redispatch-design.md §2.2）
            if not started and evt_type in ("step_start", "text", "tool_use"):
                await client.task_start(tid)
                started = True
            # 逐步日志（本地模式同款，供 worker 侧 journalctl 观察执行过程）；前缀含 seq 便于定位
            if evt_type == "step_start":
                log.info(f"  [{_esq}] ▶ {data}")
            elif evt_type == "text":
                log.info(f"  [{_esq}] 💬 {str(data)[:120]}")
            elif evt_type == "reasoning":
                log.info(f"  [{_esq}] 🤔 {str(data)[:120]}")
            elif evt_type == "tool_use":
                log.info(f"  [{_esq}] 🔧 {str(data)[:150]}")
            elif evt_type == "step_finish":
                log.info(f"  [{_esq}] ✅ {data}")
            batch.append({"type": evt_type, "data": data if not isinstance(data, str) else data})
            # Context compaction: compact batch if it grows too large (prevents context overflow)
            if should_compact(batch):
                batch = compact_messages(batch)
            # 双触发批量上报（实时日志实时性关键）：50 条或 500ms 时间窗任一满足即 flush
            _now = time.monotonic()
            if len(batch) >= 50 or (_last_flush and _now - _last_flush >= 0.5):
                _resp = await client.report_messages(tid, batch)
                if _resp is None:
                    # task 已被服务端标终态（410 Gone / 网络错误）→ 中断执行，杀引擎进程
                    log.warning(f"task {tid} report_messages returned None (task terminated?), aborting execution")
                    _kill_stale_engine(task, _backend_for_engine(engine_name))
                    _res.dispose()
                    return {"_error": "task terminated by server (aborted during execution)", "_started": started}
                batch = []
                _last_flush = _now
        if batch:
            _resp = await client.report_messages(tid, batch)
            if _resp is None:
                log.warning(f"task {tid} final report_messages returned None (task terminated?), aborting")
                _kill_stale_engine(task, _backend_for_engine(engine_name))
                _res.dispose()
                return {"_error": "task terminated by server (aborted at final flush)", "_started": started}
        # done/complete/fail 由调用方（_wrap）在 timeout 外处理，防止网络重试超时覆盖完成状态
        _res.dispose()
        return done
    except Exception as e:
        log.warning(f"task {tid} stream failed: {e}")
        _res.dispose()
        return {"_error": str(e), "_started": started}


async def _reregister(client, installed, device_name) -> list[int] | None:
    """重新注册获取新 runtime id。先重置连接池清除 CLOSE-WAIT 死连接，再注册。"""
    await client.reset_session()
    log.warning("re-registering with server to refresh runtime ids...")
    try:
        _ls = _scan_local_skills()
        ids, _probe = await client.register(installed, device_name=device_name, local_skills=_ls)
        if ids:
            for rid in ids:
                await client.recover_orphans(rid)
            log.info(f"re-registered successfully, new runtime ids: {ids}")
            return ids
        log.warning("re-register returned empty ids")
    except Exception as e:
        log.warning(f"re-register failed: {e}")
    return None


async def _run_loop(client, runtime_ids, poll_interval, heartbeat_interval, max_concurrent,
                    engine_by_rid: dict | None = None, installed=None, device_name="",
                    _test_execute=None, _test_active=None):
    """主循环：claim → 执行 → 回报，含心跳 + 自动重连自修复。

    自修复机制（三层防护，见 worker 自修复重连设计）：
    1. 心跳失败计数：连续 N 次 heartbeat 失败 → 重注册
    2. 定期重注册：每 RE_REGISTER_INTERVAL 秒主动刷新 runtime id，
       防止服务端重启后 id 静默失效（服务端对旧 id 仍返回 200 ok）
    3. 网络中断恢复：连接错误计入心跳失败，触发重注册
    """
    active_sessions = _test_active if _test_active is not None else set()  # D9 串行槽：同 session 已有活跃任务，跳过（服务端 claim 已排除，防御双保险）
    active_tasks = {}        # tid → sid 映射（task_id 去重 + sid 看门狗反查）
    active_task_objs = {}    # tid → task dict（sid 看门狗杀进程需要 task 的 cwd/backend 信息）
    _requeue_cooldown = {}   # sid → 上次 requeue 时间戳，60s 冷却防 claim→requeue 空转死循环
    _sid_watchdog_last = 0.0 # 上次 sid 看门狗扫描时间
    hermes_active = 0         # hermes 引擎限并发 1（模型 API 分钟限流，实测并发双开 429，见 #53）
    last_heartbeat = 0.0
    heartbeat_fail_streak = 0
    HEARTBEAT_FAIL_THRESHOLD = 3
    RE_REGISTER_INTERVAL = float(os.environ.get("RUNTIME_REREGISTER_INTERVAL", "300"))
    last_reregister = asyncio.get_running_loop().time()
    engine_by_rid = engine_by_rid or {}

    while True:
        # ---- 定期重注册（防 id 静默失效）----
        now = asyncio.get_running_loop().time()
        if now - last_reregister >= RE_REGISTER_INTERVAL:
            new_ids = await _reregister(client, installed, device_name)
            if new_ids:
                runtime_ids = new_ids
                heartbeat_fail_streak = 0
            last_reregister = asyncio.get_running_loop().time()

        for rid in runtime_ids:
            try:
                # Tags from server DB (single source of truth, managed via console)
                # claim_by_tag 和 claim 都返回完整 dict {task, scan_request}
                _claim_resp = None
                _db_tags = []
                try:
                    _db_tags = await client.get_tags(rid)
                except Exception:
                    _db_tags = []
                if _db_tags:
                    for _tag in _db_tags:
                        _resp = await client.claim_by_tag(rid, _tag,
                                                          supports_obs_url=_OBS_SUPPORT,
                                                          prefer_fallback=_OBS_FALLBACK)
                        if _resp and _resp.get("task"):
                            _claim_resp = _resp
                            break
                        # 无 task 但有 scan_request → 也要处理（空闲扫描）
                        if _resp and _resp.get("scan_request") and not _claim_resp:
                            _claim_resp = _resp
                if not _claim_resp or not _claim_resp.get("task"):
                    # Legacy claim (fallback: no tags or no tagged task)
                    _legacy_resp = await client.claim(rid, supports_obs_url=_OBS_SUPPORT,
                                              prefer_fallback=_OBS_FALLBACK)
                    if _legacy_resp and _legacy_resp.get("task"):
                        _claim_resp = _legacy_resp
                    elif _legacy_resp and _legacy_resp.get("scan_request") and not _claim_resp:
                        _claim_resp = _legacy_resp
            except Exception as e:
                log.warning(f"claim rid={rid} failed: {e}")
                heartbeat_fail_streak += 1
                continue
            # 处理 scan_request 指令（服务端通过 claim 响应下发）
            if _claim_resp and _claim_resp.get("scan_request"):
                try:
                    _engine_name = engine_by_rid.get(rid, "")
                    _engine_type = _backend_for_engine(_engine_name)
                    _skill_details = _scan_local_skills_with_detail(_engine_type)
                    await client.report_skills(rid, _skill_details)
                    log.info(f"skill scan completed for rid={rid}, reported {len(_skill_details)} skills")
                except Exception as _se:
                    log.warning(f"skill scan failed for rid={rid}: {_se}")
            task = (_claim_resp or {}).get("task") if _claim_resp else None
            if not task:
                continue
            tid = task.get("id", "")
            e = engine_by_rid.get(rid, "")  # 本任务引擎实例名（hermes 限并发判断用）
            # 先查 tid 再查 sid：同一任务已在跑时直接跳过（不 requeue），
            # 否则任务被 requeue 回 queued → 下轮又 claim 到 → sid 冲突 → 再 requeue →
            # 无限循环饿死同 runtime 后续任务（seq#1477 SRE 被 chat 任务死循环阻塞 20min）。
            if tid in active_tasks:
                log.debug(f"task {tid} already active, skip")
                continue
            # 并发槽 key 与服务端 claim 串行粒度一致（multi-issue-isolation-design.md §3.3）：
            # 有关联 Issue → 按 issue_session_id（跨 Issue 并行）；无（#0/旧任务）→ 回退 agent_session_id。
            # 修复：此前一律用 agent_session_id，导致不同 Issue 的任务在客户端被误判冲突拒收
            #       （服务端已放行并标记 dispatched，客户端却 skip → 任务悬空卡死，见 seq#622）。
            sid = task.get("issue_session_id") or task.get("agent_session_id") or ""
            if sid in active_sessions:
                # D9 串行槽：同 session 已有【另一个】活跃任务，跳过（服务端 claim 已排除，防御双保险）。
                # 回滚任务为 queued，避免 dispatched 悬空（skip 后无人执行也不重新排队）。
                # 冷却：同 sid 60s 内只 requeue 一次，避免每轮 poll(3s) 都 claim→requeue 空转
                # 饿死后面的任务（seq#1477 chat 任务空转 20min 阻塞 SRE）。
                _now = asyncio.get_running_loop().time()
                _last = _requeue_cooldown.get(sid, 0)
                if _now - _last < 60:
                    continue  # 冷却中：不 claim 不 requeue，留给后面的任务
                _requeue_cooldown[sid] = _now
                log.warning(f"claim conflict: session {sid} busy, requeue task {tid}")
                try:
                    await client.requeue(tid)
                except Exception as _e:
                    log.warning(f"requeue task {tid} failed: {_e}")
                continue
            if len(active_sessions) >= max_concurrent:
                continue
            if e == "hermes" and hermes_active >= 1:
                # hermes 引擎限并发 1（模型 API 分钟限流，双开实测 429 → 假完成，见 #53）
                continue
            active_sessions.add(sid)
            active_tasks[tid] = sid
            task["_engine_name"] = e  # 供 sid 看门狗杀进程用
            active_task_objs[tid] = task
            if e == "hermes":
                hermes_active += 1

            async def _wrap(t=task, _tid=tid, s=sid, e=engine_by_rid.get(rid, "")):
                nonlocal hermes_active
                task_timeout = _env_float("RUNTIME_TASK_TIMEOUT", 1200)
                # 立即标记 running（不等引擎首个事件）：hermes chat -q 冷启动+模型响应
                # 常 >120s 才产出首个事件，保持 dispatched 会被服务端 120s 回收逻辑
                # 误判未执行 → 回收重跑循环（attempts 累加 3 次直接 failed，实测 #49 100042）。
                # 引擎启动失败由 fail_task 收尾（running → failed），无悬空风险。
                # task_start 重试（3次/1s间隔）：网络抖动时静默失败会导致任务留在 dispatched
                # 被服务端 300s 回收 → 重跑循环 → 最终"回收超限"failed（实测今天 11/29 失败因此）。
                _start_ok = False
                for _start_attempt in range(3):
                    try:
                        _start_result = await client.task_start(_tid)
                        if _start_result is not None:
                            _start_ok = True
                            break
                    except Exception as _se:
                        log.warning(f"task_start attempt {_start_attempt+1}/3 for {_tid} failed: {_se}")
                    if _start_attempt < 2:
                        await asyncio.sleep(1)
                if not _start_ok:
                    log.warning(f"task_start 3 attempts all failed for {_tid}, failing task to avoid dispatched stall")
                    try:
                        await client.fail(_tid, "task_start failed after 3 retries (network error)")
                    except Exception:
                        pass
                    return
                try:
                    if _test_execute is not None:
                        await _test_execute(client, t, e)
                    else:
                        # timeout 只保护流式执行阶段，complete/fail 在 timeout 外处理
                        _result = await asyncio.wait_for(_execute_task(client, t, e), timeout=task_timeout)
                except asyncio.TimeoutError:
                    log.warning(f"task {_tid} timed out after {task_timeout}s, killing child processes and failing")
                    _kill_stale_engine(t, _backend_for_engine(e))
                    _result = None  # timeout → 走 fail 路径
                # 在 timeout 外处理 complete/fail（网络重试不触发 timeout 覆盖完成状态）
                if _test_execute is None:
                    try:
                        if isinstance(_result, dict) and _result.get("_error"):
                            await client.fail(_tid, _result["_error"])
                        elif _result is None:
                            await client.fail(_tid, f"task timed out after {task_timeout}s")
                        elif isinstance(_result, dict) and "exit_code" in _result:
                            await client.complete(_tid, _result.get("text", ""), _result.get("exit_code", 0),
                                                  session_id=_result.get("oc_session_id", ""), work_dir=t.get("cwd") or ".")
                        else:
                            await client.fail(_tid, "no done event")
                    except Exception as _ce:
                        log.warning(f"task {_tid} complete/fail callback error: {_ce}")
                    # tmp cleanup handled by TaskResourceStack.dispose() in _execute_task
                active_sessions.discard(s)
                active_tasks.pop(_tid, None)
                active_task_objs.pop(_tid, None)
                if e == "hermes":
                    hermes_active -= 1

            asyncio.create_task(_wrap())

        # ---- 心跳 + 失败检测 ----
        now = asyncio.get_running_loop().time()
        if now - last_heartbeat >= heartbeat_interval:
            ok = await client.heartbeat(runtime_ids)
            if ok:
                heartbeat_fail_streak = 0
                log.debug(f"heartbeat ok, ids={runtime_ids}")
            else:
                heartbeat_fail_streak += 1
                log.warning(f"heartbeat failed (streak={heartbeat_fail_streak}/{HEARTBEAT_FAIL_THRESHOLD}), resetting session")
                await client.reset_session()
            last_heartbeat = now

            if heartbeat_fail_streak >= HEARTBEAT_FAIL_THRESHOLD:
                log.warning(f"heartbeat failed {heartbeat_fail_streak} times in a row, triggering re-register")
                new_ids = await _reregister(client, installed, device_name)
                if new_ids:
                    runtime_ids = new_ids
                    heartbeat_fail_streak = 0
                    last_reregister = asyncio.get_running_loop().time()
                else:
                    backoff = min(30 * (2 ** (heartbeat_fail_streak - HEARTBEAT_FAIL_THRESHOLD)), 300)
                    log.warning(f"re-register failed, backing off {backoff:.0f}s")
                    await asyncio.sleep(backoff)

        # ---- sid 看门狗：清理幽灵 sid ----
        # active_tasks 里的任务如果已在服务端终态（failed/cancelled/completed/not_found），
        # 说明 _wrap 的 finally 没执行（opencode 进程 hang 住），sid 泄漏在 active_sessions。
        # 定期查服务端状态，主动清理，防同 sid 后续任务被假冲突挡住（seq#1485 根因）。
        _watchdog_interval = _env_float("RUNTIME_SID_WATCHDOG_INTERVAL", 60)
        if active_tasks and now - _sid_watchdog_last >= _watchdog_interval:
            _sid_watchdog_last = now
            _ghost_tids = []
            for _tid, _sid in list(active_tasks.items()):
                try:
                    _state = await client.get_task_state(_tid)
                except Exception:
                    _state = ""
                if _state in ("failed", "cancelled", "completed", "not_found", "") and _state:
                    _ghost_tids.append((_tid, _sid, _state))
                elif not _state:
                    # 查询失败（网络抖动/服务端 500）：跳过，下次再查
                    pass
            for _tid, _sid, _state in _ghost_tids:
                active_sessions.discard(_sid)
                active_tasks.pop(_tid, None)
                _task = active_task_objs.pop(_tid, None)
                # 杀掉残留引擎进程：用户终止/超时后服务端已标终态，但 worker 上的
                # 引擎进程可能仍在跑（没有服务端→worker 的推送通道通知停进程）。
                # 看门狗发现 task 已终态时主动杀进程，防残留进程继续产出消息/调 complete。
                if _task:
                    _backend = _backend_for_engine(_task.get("_engine_name") or "")
                    try:
                        _kill_stale_engine(_task, _backend)
                        log.warning(f"sid watchdog: killed engine for ghost tid={_tid} sid={_sid} (server status={_state}, backend={_backend})")
                    except Exception as _ke:
                        log.warning(f"sid watchdog: kill engine failed for tid={_tid}: {_ke}")
                else:
                    log.warning(f"sid watchdog: cleaned ghost tid={_tid} sid={_sid} (server status={_state}, no task obj for kill)")

        await asyncio.sleep(poll_interval + random.uniform(0, poll_interval * 0.3))


def _read_local_worker_version() -> str:
    """读取本机 Worker 安装包版本（/etc/grape/worker.version，安装脚本写入）。"""
    try:
        with open("/etc/grape/worker.version", "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def main():
    token = os.environ.get("RUNTIME_TOKEN", "")
    server = os.environ.get("RUNTIME_SERVER_URL", "http://localhost:3000")
    poll = _env_float("RUNTIME_POLL_INTERVAL", 3)
    heartbeat = _env_float("RUNTIME_HEARTBEAT_INTERVAL", 15)
    max_concurrent = _env_int("RUNTIME_MAX_CONCURRENT_TASKS", 20)

    if not token:
        print("RUNTIME_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    from src.runtime.client import RuntimeClient

    async def _amain():
        client = RuntimeClient(server, token)

        # 版本自检（worker-server-delivery-design.md §2.4）：服务端包版本不一致时提示升级
        try:
            latest = await client.latest_version()
            local = _read_local_worker_version()
            if latest and local and latest != local:
                log.warning(f"Worker 包版本不一致: 本地={local} 服务端={latest}，"
                            f"请重新执行一键安装命令升级: curl -sSL {server}/api/runtime/install | bash -s -- --token <token>")
        except Exception as e:
            log.warning(f"Worker 版本自检失败: {e}")

        runtimes = _discover_local_runtimes()
        installed = [r for r in runtimes if r.get("installed")]
        if not installed:
            log.warning("No runtimes installed locally, registering empty")
        device = os.environ.get("RUNTIME_DEVICE_NAME", os.uname().nodename if hasattr(os, "uname") else "host")
        _local_skills = _scan_local_skills()
        ids, probe_url = await client.register(installed, device_name=device, local_skills=_local_skills)
        if not ids:
            log.error("Register failed (check RUNTIME_TOKEN / server URL)")
            return
        # v3.0 启动探测（B）：GET probe_url 验证完整链路（网络+签名+读取）
        global _OBS_SUPPORT
        if probe_url:
            try:
                with _urllib.urlopen(probe_url, timeout=5) as _r:
                    _OBS_SUPPORT = bool(_r.read())
            except Exception as e:
                log.warning(f"[skill] 启动探测失败（走 skill_files 兜底）: {type(e).__name__}")
        log.info(f"OBS 直连能力探测: supports_obs_url={_OBS_SUPPORT}")
        # rid → 引擎实例名（`{type}-{device}`，register 返回顺序与 installed 一致）
        from src.runtime.instances import instance_name
        engine_by_rid = {rid: instance_name(r.get("name", ""), device, 0)
                         for rid, r in zip(ids, installed)}
        log.info(f"RuntimeAgent registered: {ids} -> {server}, engines={list(engine_by_rid.values())}, "
                 f"poll={poll}s heartbeat={heartbeat}s max_concurrent={max_concurrent}")
        for rid in ids:
            await client.recover_orphans(rid)
        try:
            await _run_loop(client, ids, poll, heartbeat, max_concurrent, engine_by_rid,
                            installed=installed, device_name=device)
        finally:
            await client.close()

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
