#!/usr/bin/env python3
"""cloud_worker — 通用云沙箱 worker 独立进程入口。

和 agent.py（本机 worker）的区别：
- 引擎发现：配置文件静态声明（不扫本机 CLI）
- 沙箱选择：从配置文件读 provider（不查 DB grape_t_engine_sandbox_config）
- 认证：key 文件（本机 seed 或 B 机器 scp）
- 执行：通过 SDK commands.run 在沙箱内执行 agent（不走 Popen）

复用现有代码：
- RuntimeClient（claim/report/heartbeat/recover_orphans）
- run_loop（claim → 执行 → 回报主循环）
- _execute_task / stream_agent / _run_cloud_sandbox（执行链路）

启动方式：
  python -m src.runtime.cloud_worker
  # 或 systemd 管理

配置：
  环境变量 CLOUD_WORKER_*（systemd EnvironmentFile 注入；文件配置机制已删除）
"""

import asyncio
import json
import os
import sys
import time
import logging
import hashlib

log = logging.getLogger("cloud_worker")

# P1-4 修复：fire-and-forget task 引用集，防 GC 回收
_pending_state_reports: set = set()

# Python 版本守卫（和 agent.py 一致）
if sys.version_info < (3, 10):
    print(f"cloud_worker requires Python >= 3.10 (got {sys.version.split()[0]}).", file=sys.stderr)
    sys.exit(1)


def _load_config() -> dict:
    """[文件配置机制删除 2026-10-30 用户裁定] 配置唯一来源：CLOUD_WORKER_* 环境变量
    （install-worker.sh systemd EnvironmentFile 注入）。

    原 CLOUD_WORKER_CONFIG 文件加载分支（configs/cloud-worker.json 双层配置）删除——
    key 机制/key_file 字段已废弃（d1e242cba），共存实施（2e516bf4d）统一 install-worker.sh
    env 化后无消费者；普通 worker（agent.py）从来就是 env-only。配套删除仓内模板
    configs/cloud-worker.example.json 与 systemd/grape-cloud-worker.service。
    """
    config = {}
    config["provider"] = os.environ.get("CLOUD_WORKER_PROVIDER", "devbox")
    config["server_url"] = os.environ.get("CLOUD_WORKER_SERVER_URL", "http://127.0.0.1:3000")
    config["device_name"] = os.environ.get("CLOUD_WORKER_DEVICE_NAME", "")
    config["poll_interval"] = float(os.environ.get("CLOUD_WORKER_POLL_INTERVAL", "3"))
    config["heartbeat_interval"] = float(os.environ.get("CLOUD_WORKER_HEARTBEAT_INTERVAL", "15"))
    config["max_concurrent"] = int(os.environ.get("CLOUD_WORKER_MAX_CONCURRENT", "20"))
    config["tags"] = ["all"]
    # engines 字段已废弃——注册完全从服务端模板拉取（_expand_templates）

    # 心跳退避参数（可配置——避免硬编码，运维可按网络环境调优）
    config["refresh_base_interval"] = float(os.environ.get(
        "CLOUD_WORKER_REFRESH_BASE_INTERVAL",
        str(config.get("refresh_base_interval", 60))))
    config["refresh_fail_threshold"] = int(os.environ.get(
        "CLOUD_WORKER_REFRESH_FAIL_THRESHOLD",
        str(config.get("refresh_fail_threshold", 3))))
    config["refresh_max_interval"] = float(os.environ.get(
        "CLOUD_WORKER_REFRESH_MAX_INTERVAL",
        str(config.get("refresh_max_interval", 600))))

    # enabled 配置——环境变量优先，其次配置文件，默认 true
    config["enabled"] = os.environ.get(
        "CLOUD_WORKER_ENABLED",
        str(config.get("enabled", True)).lower(),
    ).lower() in ("true", "1", "yes")

    return config


def _expand_templates(templates: list[dict]) -> list[dict]:
    """v4：将沙箱模板列表展开为 runtime 注册条目（四段式 name：engine:template:platform:provider_id）。

    输入：GET /api/settings/sandbox-templates 返回的 templates 列表。
    输出：[{name, version, healthy, installed, cli_path, provider, runtime_type, runtime_info, agent_tags}, ...]
          按 cubesandbox 优先 + system scope 优先 + provider_id 升序排序。

    v3 改变：
    - runtime name = f"{engine_type}:{tpl_slug}:{platform}:{provider_id}"（四段式，
      [多模板共用引擎] tpl_slug=模板 name 规整化/t{db_id} 兜底——存量三段名行
      由 refresh diff 自然下线，平滑过渡）
    - runtime_info 含 provider_id（register API 从此查 Provider 配置解析 scope）
    - [i5 契约 #3] runtime_info 透传 provider_uid/template_uid（uid 派生种子分量，
      一次到位——uid 缺一由服务端 fail-fast 拒注册，不实现 id-only 兜底路径）
    - agent_tags：系统 Provider → ["all"]，用户/仓库级 Provider → []
    """
    runtimes = []
    _used_slugs: dict = {}  # slug → template db_id（批内撞名检测）
    from src.domains.runtime.agents.sandbox.backends import get_adapter
    def _platform_priority(platform: str) -> int:
        adapter = get_adapter(platform)
        return getattr(adapter, "priority", 100) if adapter else 999
    def _re_slug(_s: str, max_len: int = 16) -> str:
        """规整化为 [a-z0-9-]，≤max_len 保尾数字（引擎名段统一语义）。"""
        import re as _re
        _s = _re.sub(r"[^a-z0-9]+", "-", (_s or "").strip().lower()).strip("-")
        if not _s:
            return ""
        if len(_s) > max_len:
            _m = _re.search(r"(\d+)$", _s)
            _tail = _m.group(1) if _m else _s[-3:]
            _s = (_s[: max_len - len(_tail) - 1] + "-" + _tail)[:max_len].strip("-")
        return _s
    def _tpl_slug(t: dict) -> str:
        """[多模板共用引擎] 第四段：模板 name 规整化；空 → t{db_id} 兜底
        （name 可编辑改，db id 唯一不可变；批内撞名由调用侧兜底）。
        用户裁决：template 紧跟 engine（"hermes 用这个模板"是选引擎的语义单元）。"""
        _s = _re_slug(t.get("name") or "")
        return _s or f"t{t.get('id', 0)}"
    for t in sorted(templates, key=lambda t: (
        _platform_priority(t.get("platform", "")),
        0 if t.get("scope") == "global" else 1,
        t.get("provider_id", 0),
        t.get("template_id", ""),
    )):
        _provider_id = t.get("provider_id", 0)
        _platform = t.get("platform", "")  # [删 provider 列] 字符串兜底已删(platform 恒非空)
        # [范围由凭据承担] scope=凭据级(global/user/repo)；历史 'system' 值归一为 global
        _scope = (t.get("scope", "global") or "global").lower()
        if _scope == "system":
            _scope = "global"
        if not _platform or _platform == "disabled":
            continue
        # v3：引擎列表从 mgmt 的模板绑定查询返回值里取（已随模板一并查出，见 mgmt.public）
        engines_raw = t.get("engines", "[]")
        try:
            _engines = json.loads(engines_raw) if isinstance(engines_raw, str) else (engines_raw or [])
        except Exception:
            _engines = []
        # [范围由凭据承担] agent_tags——全局凭据(global)参与池广播，用户/仓库级不参与
        _agent_tags = ["all"] if _scope == "global" else []
        _provider_name = t.get("provider_name", "") or _platform
        # [引擎名第三段=Provider 名] 用户裁决：平台名同质（都是 cubesandbox），
        # Provider 名（cube / cubesandbox-sbx-e2e）才是用户可辨的接入标识。
        # 规整化同 tpl_slug；空则回落平台名。Provider 改名 → 引擎名随 refresh 变
        # （同模板改名机制，diff 下线/新行注册，场景绑定时页面有预警提示）。
        _prov_s = _re_slug(_provider_name) or _platform
        _tpl_db_id = t.get("id", 0) or 0
        _tpl_s = _tpl_slug(t)
        # 批内 slug 撞名（两模板 name 规整后相同）→ t{db_id} 兜底
        if _tpl_s in _used_slugs and _used_slugs[_tpl_s] != _tpl_db_id:
            _tpl_s = f"t{_tpl_db_id}"
        _used_slugs[_tpl_s] = _tpl_db_id
        for _engine_type in _engines:
            _ri = json.dumps({
                "engine_type": _engine_type,
                "platform": _platform,
                "provider_id": _provider_id,
                "provider_name": _provider_name,
                "template_id": t.get("template_id", ""),
                "template_db_id": _tpl_db_id,
                "template_uid": t.get("template_uid", ""),
                "provider_uid": t.get("provider_uid", ""),
                "template_name": t.get("name", ""),
                "preinstalled": t.get("preinstalled", ""),
                "description": t.get("description", ""),
                "disk_size_mb": t.get("disk_size_mb", 0),
            })
            # [多模板共用引擎] 四段式：engine:template:provider_name:provider_id
            # （第三段用 Provider 名——用户可辨的接入标识；同 Provider 多模板同引擎
            #  → 名互异，各自独立引擎行/卡片）
            runtimes.append({
                "name": f"{_engine_type}:{_tpl_s}:{_prov_s}:{_provider_id}",
                "version": "",
                "healthy": True,
                "installed": True,
                "cli_path": "",
                "provider": _provider_name,
                "runtime_type": "cloud",
                "runtime_info": _ri,
                "agent_tags": _agent_tags,
            })
    return runtimes


# ===== Provider 配置拉取 + 缓存 + 降级 =====

_provider_configs: dict = {}
# [同机多实例 spec §6.1 + 锚简化 2026-11-04] 实例状态目录参数化——单锚 credential_uid 前 8 位。
# env 三值由安装脚本从 whoami 写入（worker 侧零重算，单一实现源头）。
# 无标识（裸跑/调试）→ 回退原全局路径，行为与现状一致（spec §十-4 裁定）。


def _cred_state_dir() -> str:
    """[锚简化 2026-11-05] 凭据状态目录——单锚 credential_uid(前 8 位做目录名)。
    无标识(裸跑/调试)→原路径。同机多实例=每凭据一个目录(credential_uid 全局唯一)。"""
    key = os.environ.get("CLOUD_WORKER_CREDENTIAL_UID", "").strip()
    if not key:
        return "/var/lib/grape/cloud-worker"
    return f"/var/lib/grape/cloud-worker/workers/{key[:8]}"


def _provider_configs_cache_path() -> str:
    return os.path.join(_cred_state_dir(), "provider-configs.json")


def _ssl_cert_dir() -> str:
    return _cred_state_dir()


def _whoami_json_path() -> str:
    return os.path.join(_cred_state_dir(), "whoami.json")


def _probe_state_json_path() -> str:
    """探测负缓存文件（与 whoami.json 同目录，实例隔离）。"""
    return os.path.join(_cred_state_dir(), "probe-state.json")


def _prepare_artifacts(provider_configs: dict) -> None:
    """v3：按 adapter.prepare_artifacts 落盘平台特有文件（per-provider 隔离）。

    [锚简化 2026-11-04] 工件命名键 pid → credential_uid 前 8 位——pid 跨主控撞号
    时 rootCA 文件错配他主控证书；credential_uid 全局唯一（下发响应新增字段，
    旧服务端缺字段时回退 pid，双向兼容）。
    """
    from src.domains.runtime.agents.sandbox.backends import get_adapter
    for _pid, _cfg in provider_configs.items():
        _platform = _cfg.get("platform", "")
        if not _platform:
            continue
        adapter = get_adapter(_platform)
        if not adapter:
            continue
        from src.domains.runtime.agents.sandbox.backends.api import cert_artifact_key
        adapter.prepare_artifacts(_cfg, _ssl_cert_dir(), cert_artifact_key(_pid, _cfg))


def _write_provider_configs_cache(configs: dict) -> None:
    """落盘缓存——api_key 用 crypto.encrypt 加密后存储（权限 0600）。

    缓存文件含明文 api_key 风险高（磁盘被 dump / backup 泄露），
    加密后即使文件泄露，攻击者无法解密 api_key（需要 crypto.json 密钥）。
    """
    try:
        import json as _json
        from src.kernel.crypto import encrypt as _encrypt
        # 深拷贝 + 加密每个 provider 的 api_key 字段
        _safe = {}
        for _prov, _cfg in configs.items():
            _safe_cfg = dict(_cfg)
            _api_key = _safe_cfg.get("api_key", "")
            if _api_key:
                try:
                    _safe_cfg["api_key"] = _encrypt(_api_key)
                except Exception as _e:
                    log.warning(f"provider configs cache encrypt failed for {_prov}: {_e}")
                    _safe_cfg["api_key"] = ""  # 加密失败不写明文
            _safe[_prov] = _safe_cfg
        os.makedirs(os.path.dirname(_provider_configs_cache_path()), exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            with open(_provider_configs_cache_path(), "w") as f:
                _json.dump(_safe, f)
            os.chmod(_provider_configs_cache_path(), 0o600)
        finally:
            os.umask(old_umask)
    except Exception as e:
        log.warning(f"provider configs cache write failed: {e}")


def _read_whoami_cache() -> dict:
    """读 whoami.json（最近一次成功 whoami 的快照，spec §五）。

    展示层缓存：记录当前认知的凭据身份（name/cred_type/platform/credential_uid）。
    永不包含凭证字段。无实例标识（裸跑）时落到原全局路径，行为与现状一致。
    """
    try:
        import json as _json
        with open(_whoami_json_path(), "r") as f:
            return _json.load(f) or {}
    except Exception:
        return {}


def _write_whoami_cache(data: dict) -> None:
    """写 whoami.json——存 whoami 响应原样（不增删字段，spec §五"快照"约定）。"""
    try:
        import json as _json
        _p = _whoami_json_path()
        import os as _os
        _os.makedirs(_os.path.dirname(_p), exist_ok=True)
        # 只存安全字段（双保险：whoami 响应本就无凭证字段）
        _safe = {k: data.get(k, "") for k in
                 ("credential_id", "name", "cred_type", "platform", "scope", "credential_uid")}
        with open(_p, "w") as f:
            _json.dump(_safe, f, ensure_ascii=False, indent=1)
        _os.chmod(_p, 0o600)
    except Exception as e:
        log.warning(f"[whoami] cache write failed: {e}")


async def _whoami_follow_and_guard(client) -> None:
    """改名跟随 + 身份守护（spec §五/§6.2）——心跳周期低频调用。

    - whoami.name != whoami.json 缓存 → 用户改过名 → 更新缓存 + 日志 renamed
      （目录/服务/进程不动——id 锚，名字是纯展示层，自动跟随零人工）
    - whoami 401 → 抛 CredentialRevokedError（调用方计入自杀阈值）
    """
    try:
        fresh = await client.whoami()
    except RuntimeError as _we:
        # 网络类失败（非 401）——whoami 内部已重试, 跳过本轮（调用方不计数）
        if "401" in str(_we) or "403" in str(_we) or "revoked" in str(_we).lower():
            raise
        return
    if not fresh.get("credential_id"):
        return  # 空响应（网络/解析）——跳过本轮
    cached = _read_whoami_cache()
    # [锚简化 2026-11-04 用户裁定] server_instance 防串台自校验退役——实例键已换
    # credential_uid 单锚（全局唯一），不同主控的凭据 uid 必不同 → 目录必不同，
    # 「误写别人实例目录」不可达；mismatch 场景随之消失。
    if cached.get("credential_id") and cached.get("name") and cached.get("name") != fresh.get("name"):
        log.info(f"credential renamed: {cached['name']} -> {fresh.get('name')} (auto-follow, dir/service unchanged)")
    _write_whoami_cache(fresh)


def _read_provider_configs_cache() -> dict:
    """读磁盘缓存——api_key 用 crypto.decrypt 解密后返回。"""
    try:
        import json as _json
        from src.kernel.crypto import decrypt as _decrypt
        with open(_provider_configs_cache_path(), "r") as f:
            _raw = _json.load(f)
        # 解密每个 provider 的 api_key 字段。JSON key 是字符串，转回 int（v3 provider_id 索引）
        _result = {}
        for _prov, _cfg in _raw.items():
            _cfg_dec = dict(_cfg)
            _api_key_enc = _cfg_dec.get("api_key", "")
            if _api_key_enc:
                try:
                    _cfg_dec["api_key"] = _decrypt(_api_key_enc)
                except Exception as _e:
                    log.warning(f"provider configs cache decrypt failed for {_prov}: {_e}")
                    _cfg_dec["api_key"] = ""
            try:
                _pid = int(_prov)
            except (ValueError, TypeError):
                _pid = _prov
            _result[_pid] = _cfg_dec
        return _result
    except Exception:
        return {}


async def _fetch_provider_configs(client) -> dict:
    """拉取 provider 配置 + 落盘缓存 + 返回。失败时用缓存。"""
    global _provider_configs
    try:
        configs = await client.get_provider_configs()
        if configs:
            _provider_configs = configs
            _prepare_artifacts(configs)
            _write_provider_configs_cache(configs)
            return configs
    except Exception as e:
        log.warning(f"provider configs fetch failed: {e}")
    if _provider_configs:
        log.info("provider configs: using in-memory cache (fetch failed)")
        return _provider_configs
    _cached = _read_provider_configs_cache()
    if _cached:
        _provider_configs = _cached
        log.info("provider configs: using disk cache (fetch failed, no memory cache)")
        return _cached
    log.warning("provider configs: no cache available, providers will fallback to local config files")
    return {}


async def _refresh_provider_configs(client):
    """心跳时刷新 provider 配置（检测 api_key 轮换等变更）。"""
    global _provider_configs
    try:
        new_configs = await client.get_provider_configs()
        if new_configs and new_configs != _provider_configs:
            _provider_configs = new_configs
            _prepare_artifacts(new_configs)
            _write_provider_configs_cache(new_configs)
            # 配置变更 → 更新 sandbox_manager 的 provider_configs + 清空 Provider 实例缓存
            # set_provider_configs 更新配置字典（下次 prepare 用新配置创建 Provider）
            # invalidate_provider_cache 清空已缓存的 Provider 实例（用旧配置创建的）
            # 两者缺一不可：只 set 不 invalidate → 旧 Provider 实例还在用旧配置
            #               只 invalidate 不 set → 新 Provider 从旧 _provider_configs 创建
            try:
                from src.domains.runtime.agents.sandbox import sandbox_manager as _sm
                _sm.set_provider_configs(new_configs)
                _sm.invalidate_provider_cache()
            except Exception:
                pass
            log.info(f"provider configs refreshed: {list(new_configs.keys())}")
    except Exception as e:
        log.warning(f"provider configs refresh failed: {e}, using cached configs")



def _ri_template_name(task: dict) -> str:
    """从 task dict 的 runtime_info 派生 template_name（引擎注册时写入的快照字段）。"""
    try:
        import json as _j
        _ri_raw = (task or {}).get("runtime_info") or "{}"
        _ri = _j.loads(_ri_raw) if isinstance(_ri_raw, str) else (_ri_raw or {})
        return _ri.get("template_name", "") or ""
    except Exception:
        return ""

async def _cloud_execute_task(client, task, engine_name: str = ""):
    """云沙箱任务执行：claim 拿到 task dict → 创建/复用沙箱 → dispatch_execution → 回报。

    和 agent.py 的 _execute_task 区别：
    - 不走 _remap_cwd（沙箱内 cwd 由 _run_cloud_sandbox 修正）
    - 不走 git worktree（仓库代码由 agent 自行获取：模板预置或 grape CLI git-repo）
    - 不走 codearts data dir
    - 每环节新建，任务结束主动 kill（不再 __exit__ refresh）
    - LLM key 从 /env API 获取（不查 DB）
    - proxy token 从 HTTP API 获取（不写 DB）
    - sandbox 状态通过 report_sandbox_status 上报（不写 DB）

    远程日志聚合：在关键节点通过 client.report_progress 上报日志行到服务端，
    服务端存入 task_messages 表，admin console 可直接查看，无需 SSH 到 B 机器。
    """
    import time as _time
    from src.domains.runtime.engine.agent import backend_for_engine
    from src.domains.runtime.agents.executor import get_backend

    tid = task.get("id", "")
    _engine_tag = f"|engine:{engine_name}" if engine_name else ""

    async def _log(stage: str, detail: str = ""):
        """上报关键节点日志到服务端 task_messages（远程日志聚合——事件详情实时日志流可见）。

        [沙箱迁移修正] 原走 report_progress 写 agent_tasks.result_text——注释与实现不符，
        且启动过程（claim/沙箱创建/失败原因）在实时日志弹窗（task_messages SSE）里看不到。
        改走 report_messages（{type:'log', data} 结构）落 task_messages + 实时 fanout；
        失败节点用 type='error'（弹窗内红色可辨）。report_progress 保留给心跳/进度语义。
        """
        msg = f"[cloud_worker]{_engine_tag} {stage}"
        if detail:
            msg += f": {detail}"
        log.info(msg)
        try:
            await client.report_messages(tid, [{
                "type": "error" if stage in ("sandbox_fail", "engine_fail", "task_abort") else "log",
                "data": msg,
            }])
        except Exception:
            pass

    await _log("claim", f"task={tid} repo={task.get('repo_key') or ''} issue={task.get('issue_id') or ''} pool_agent_type={task.get('pool_agent_type', '<MISSING>')}")

    # 1. 获取任务环境变量（含 LLM key + AK/SK）
    task_env = await client.get_task_env_full(tid)

    # [repo-llm-credential spec A 用户裁定] 缺 LLM 凭据 → 不建箱直接 fail（claim 后建箱前
    # 拦截，不浪费 ~43s 沙箱冷启动）——消灭旧"静默跳过→引擎死在半路报晦涩 401"盲区
    if task_env.get("llm_cred_missing"):
        _hint = task_env.get("llm_cred_hint") or "缺少 LLM 凭据，请在对应设置的 LLM 凭据区配置后重试"
        await _log("llm_cred_missing", f"source={task_env.get('llm_cred_source', '')} task={tid}")
        try:
            await client.fail(tid, _hint)
        except Exception as _fe:
            log.warning(f"llm_cred_missing: report fail error: {_fe}")
        return {"_error": _hint}

    # 2. 构建 proc_env
    # v3 §0.4 互斥注入：chat → USER_GITCODE_TOKEN，devops → BOT_GITCODE_TOKEN
    env = {}
    env["DEVOPS_EVENT_TYPE"] = task.get("event_type") or ""
    env["REPO_FULL_NAME"] = task.get("repo_key") or ""
    _is_chat = (task.get("event_type") or "") == "chat" or (task.get("agent_type") or "") == "chat"
    if _is_chat:
        # chat 任务：只注入 USER_GITCODE_TOKEN（从 /env API 获取），不注入 BOT_GITCODE_TOKEN
        if task_env.get("USER_GITCODE_TOKEN"):
            env["USER_GITCODE_TOKEN"] = task_env["USER_GITCODE_TOKEN"]
    else:
        # devops 事件任务：按平台派生 bot token 键（BOT_GITCODE_TOKEN / BOT_GITEE_TOKEN …），
        # 不注入 USER_*_TOKEN。过去写死 gitcode 键 → gitee 仓库在沙箱侧 token 恒空 → 不 clone。
        _bot_tok = task.get("repo_gitcode_token") or ""
        _bot_key = "BOT_GITCODE_TOKEN"
        try:
            from src.domains.runtime.vcs.clone_endpoint import bot_token_env_name

            _bot_key = bot_token_env_name(task.get("repo_key") or "")
        except Exception as _be:
            log.warning(f"bot token env name derive failed: {_be}")
        if not _bot_tok:
            # P2：无凭据运行必须可见（此前静默空值 → 沙箱 askpass 回声为空 →
            # 推送 401 被误判成"token 失效/权限不足"，实测排查两轮）
            log.warning(
                f"cloud_worker: 任务 {task.get('id') or task.get('tid') or '-'} 无可用的"
                f"仓库 bot 凭据（repo_key={task.get('repo_key') or '-'}，键={_bot_key}）"
                f"——沙箱内 git push/PR 将不可用，请检查派发侧凭据注入")
        env[_bot_key] = _bot_tok
        if _bot_key != "BOT_GITCODE_TOKEN":
            # 升级期兜底：旧引擎 / bwrap 白名单仍按 gitcode 键名读
            env.setdefault("BOT_GITCODE_TOKEN", _bot_tok)

    # env_extra（enc:v1 信封 → dict，#2423/#3649：直接 json.loads 会静默得到空 env）
    _env_extra = task.get("env_extra") or ""
    if _env_extra:
        try:
            if isinstance(_env_extra, str):
                from src.domains.runtime.engine.env_extra_security import decrypt_env_extra
                env.update(decrypt_env_extra(_env_extra))
            else:
                env.update(_env_extra)
        except Exception:
            pass

    # LLM 凭据从 /env API 取
    if task_env.get("_LLM_API_KEY"):
        env["_LLM_PROVIDER"] = task_env.get("_LLM_PROVIDER", "")
        env["_LLM_API_KEY"] = task_env.get("_LLM_API_KEY", "")
        env["_LLM_BASE_URL"] = task_env.get("_LLM_BASE_URL", "")
        env["_LLM_MODEL"] = task_env.get("_LLM_MODEL", "")

    # skill_obs_url
    if task.get("skill_obs_url"):
        env["SKILL_OBS_URL"] = task["skill_obs_url"]

    # GRAPE_SERVER_URL
    # [E-02 沙箱回环修复 2026-11-13] 任务行的 server_url 是建单进程视角的本机地址
    # （127.0.0.1:8081/8083）——对云沙箱无意义（沙箱内的 127.0.0.1 是沙箱自身，
    # 引擎调平台 API 全部 Connection refused，实测 round8 vod 结果）。云沙箱场景
    # 用 worker 自身的主控地址（CLOUD_WORKER_SERVER_URL，nginx 单入口）覆盖。
    # 2026-11-22：worker 自身配置优先——它才是"我确实能连到主控"的实证。
    # 任务行的 server_url 是建单进程视角算出来的（可能是公网/回环），远端 worker
    # 未必可达（实测 .213 到公网 IP 000 不可达 → grape CLI 全 301）。
    _cw_server = (os.environ.get("CLOUD_WORKER_SERVER_URL", "")
                  or os.environ.get("RUNTIME_SERVER_URL", ""))
    _task_server = task.get("server_url") or ""
    _effective = _cw_server or _task_server
    if _effective and _task_server and _effective != _task_server:
        log.info(f"cloud_worker: 回连基址以 worker 配置为准 {_task_server} -> {_effective}")
    if _effective:
        env["GRAPE_SERVER_URL"] = _effective

    # 2b. v3 per-provider 沙箱流控校验（F1-F4）
    from src.domains.runtime.agents.sandbox.sandbox_context import build_context as _build_ctx
    _ctx = _build_ctx(task)
    _provider_id = _ctx.provider_id
    if _provider_id:
        _max_sandboxes = _provider_configs.get(_provider_id, {}).get("max_sandboxes", 10)
        try:
            _throttle = await client.get_provider_active_sandboxes(_provider_id)
            _active = _throttle.get("active_count", 0)
            _max = _throttle.get("max_sandboxes", _max_sandboxes)
            if _active >= _max:
                await _log("throttle", f"provider_id={_provider_id} active={_active} max={_max}")
                await client.requeue(tid)
                return {"_error": "throttled", "sandbox_id": ""}
        except Exception as _te:
            log.warning(f"throttle check failed for provider_id={_provider_id}: {_te}")

    # 3. 签发 proxy token（通过 WorkerServices 端口统一获取）
    from src.domains.runtime.engine.worker_services import create_worker_services
    _services = create_worker_services(client=client, is_local=False)
    try:
        proxy_token = await _services.issue_proxy_token(tid, engine_name)
        if proxy_token:
            env["GRAPE_PROXY_TOKEN"] = proxy_token
    except Exception as e:
        log.warning(f"{_engine_tag}proxy token issue failed: {e}")

    # 4. 沙箱上下文（从 task dict 构建，不查 DB）——已在 2b 流控校验时构建，复用
    # _ctx = build_context(task)  # moved to 2b above

    # llm_env 从 env 取
    _llm_env = {k: v for k, v in env.items() if k.startswith("_LLM_")}
    # [egress 首通修复 2026-11-16] 有效 server URL 透传建箱链——_build_egress_network
    # 第 1 优先级读 _init_envs.GRAPE_SERVER_URL 推断 allow_out，但 prepare() 此前不接收
    # 任务 env → 落到 CLOUD_WORKER_SERVER_URL（127.0.0.1 形态）→ allow_out 指向沙箱自身，
    # 内网 grape API 全拒（event 4 实测：Connection refused 多轮）。与 llm_env 同款通道传入。
    _server_env = {"GRAPE_SERVER_URL": env["GRAPE_SERVER_URL"]} if env.get("GRAPE_SERVER_URL") else None

    # BUG-1 修复：捕获主线程 event loop，供 to_thread 中的 state_reporter 使用
    _main_loop = asyncio.get_running_loop()

    # HTTP 版 state_reporter——通过 report_sandbox_status API 上报，不碰 DB
    def _http_state_reporter(*, state, sandbox_id="", issue_id="", repo_key="",
                             provider="", provider_id=0, agent_installed=None, **_kwargs):
        from src.domains.runtime.agents.sandbox.backends.api import coerce_provider_id
        try:
            # to_thread 线程中无 running loop，用 run_coroutine_threadsafe 提交到主线程
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is None and _main_loop and not _main_loop.is_closed():
                # 在 to_thread 线程中——提交 coroutine 到主线程 loop
                future = asyncio.run_coroutine_threadsafe(
                    client.report_sandbox_status(
                        tid,
                        issue_id=issue_id,
                        repo_key=repo_key,
                        sandbox_id=sandbox_id,
                        state=state,
                        provider=provider,
                        provider_id=coerce_provider_id(provider_id),
                        agent_installed=agent_installed or 0,
                    ),
                    _main_loop,
                )
                # fire-and-forget——异常在 future 中，不阻塞 to_thread
                def _on_future_done(f):
                    try:
                        f.result()
                    except Exception as e:
                        log.warning(f"http_state_reporter (threadsafe) failed: {e}")
                future.add_done_callback(_on_future_done)
                return
            # 主线程上下文——直接 create_task
            task_obj = loop.create_task(
                client.report_sandbox_status(
                    tid,
                    issue_id=issue_id,
                    repo_key=repo_key,
                    sandbox_id=sandbox_id,
                    state=state,
                    provider=provider,
                    agent_installed=agent_installed or 0,
                )
            )
            # P1-4 修复：存储 task 引用防 GC
            _pending_state_reports.add(task_obj)
            task_obj.add_done_callback(_pending_state_reports.discard)
            def _on_done(t):
                if t.exception():
                    log.warning(f"http_state_reporter failed: {t.exception()}")
            task_obj.add_done_callback(_on_done)
        except Exception as e:
            log.warning(f"http_state_reporter: {e}")

    # 5. 走 prepare()——统一安全策略（不再直接调 provider.create_sandbox）
    from src.domains.runtime.agents.sandbox import sandbox_manager
    _sandbox = None  # 初始化——prepare 失败时 _sandbox 未绑定
    try:
        await _log("sandbox_prepare", f"provider={_ctx.sandbox_provider or 'bwrap'} template={_ctx.template_id}")
        # prepare + __enter__ 是同步链路（含 health check / SDK create / _init_sandbox），
        # 在独立线程执行避免阻塞事件循环（health check 可能阻塞 3s，_init_sandbox 含 pip install 等）
        import asyncio as _aio
        def _sync_prepare():
            _sb = sandbox_manager.prepare(
                exec_backend=engine_name, cwd="", ctx=_ctx,
                state_reporter=_http_state_reporter, llm_env=_llm_env,
                server_env=_server_env,
            )
            # 内聚：从 ctx 设置 repo 信息（__enter__ → _init_sandbox 第 7 步用）
            # 不能在 __enter__ 前调 configure_repo（self._sandbox 还没创建）
            # 直接设私有属性，_init_sandbox 第 7 步从这些属性读
            if hasattr(_sb, '_repo_url') and _ctx.repo_url:
                _sb._repo_url = _ctx.repo_url
                _sb._bot_token = _ctx.bot_token
                _sb._work_branch = _ctx.work_branch
                # clone 用户名（basic_password 平台必须是令牌所属账号 login）：
                # 过去只注入 url/token/branch，云沙箱 askpass 回退 "bot" → gitee 必被拒。
                if hasattr(_sb, "_clone_username"):
                    setattr(_sb, "_clone_username", getattr(_ctx, "clone_username", "") or "")
                log.info(f"{_engine_tag}repo info set from ctx: repo_url={_ctx.repo_url[:50]} "
                         f"bot_token={'yes' if _ctx.bot_token else 'no'} "
                         f"clone_user={getattr(_ctx, 'clone_username', '') or '-'}")
            else:
                log.warning(f"{_engine_tag}repo info NOT set: hasattr_repo_url={hasattr(_sb, '_repo_url')} ctx_repo_url={_ctx.repo_url[:50] if _ctx.repo_url else '(empty)'}")
            _sb.__enter__()
            return _sb
        _sandbox = await _aio.to_thread(_sync_prepare)
        await _log("sandbox_created", f"sandbox_id={getattr(_sandbox, 'sandbox_id', '') or ''}")

        # [egress 可达性断言 2026-11-16] 建箱后立即验证 grape API 在沙箱内可达。
        # 三种断链形态统一显式失败（此前 event 4 半天排障的教训）：内网 IP 未放行
        # （allow_out 生成失败/漂移）、公网域名不在平台 SNI 白名单、server URL 配置漂移。
        # 断言失败 → kill 沙箱 + 任务 fail（带 egress 诊断文案），不静默死在执行中段。
        if _server_env:
            _run_cmd = getattr(_sandbox, "run_command", None)
            if callable(_run_cmd):
                _probe_url = (_server_env.get("GRAPE_SERVER_URL") or "").rstrip("/") + "/api/health"
                try:
                    _probe = await _aio.to_thread(
                        lambda: _run_cmd(
                            f"curl -s -m 8 -o /dev/null -w '%{{http_code}}' {_probe_url}",
                            timeout=15))
                    # run_command 返回 ExecHandle（CubeSandbox 同步执行完即返）——
                    # wait() 拿 CommandResult；兼容裸元组/CommandResult 形态
                    _stdout = ""
                    if hasattr(_probe, "wait") and callable(getattr(_probe, "wait")):
                        _r = _probe.wait()
                        _stdout = getattr(_r, "stdout", "") or ""
                    elif isinstance(_probe, tuple) and len(_probe) >= 2:
                        _stdout = str(_probe[1] or "")
                    else:
                        _stdout = str(_probe)
                    import re as _re_probe
                    _m = _re_probe.search(r"(\d{3})", (_stdout or "").strip())
                    if not (_m and _m.group(1).startswith(("2", "3"))):
                        raise RuntimeError(
                            f"grape API 不可达: HTTP {_m.group(1) if _m else 'refused/timeout'} "
                            f"({_probe_url})——检查 egress allow_out/平台域名白名单")
                except Exception as _pe:
                    _pe_msg = str(_pe)
                    log.error(f"{_engine_tag}egress reachability check FAILED: {_pe_msg}")
                    try:
                        _sandbox.kill()
                    except Exception:
                        pass
                    return {"_error": "egress_blocked", "detail": _pe_msg, "sandbox_id": ""}
        # 问题1 修复：直接 await 上报 running 状态，不依赖 fire-and-forget state_reporter
        _sandbox_id = getattr(_sandbox, '_sandbox_id', '') or ''
        if _sandbox_id:
            try:
                await client.report_sandbox_status(
                    tid,
                    issue_id=task.get("issue_id", ""),
                    repo_key=task.get("repo_key", ""),
                    sandbox_id=_sandbox_id,
                    state="running",
                    provider=_ctx.sandbox_provider or "",
                    agent_installed=1,
                    template_id=_ctx.template_id or "",
                    template_name=_ri_template_name(task),
                    engine_type=_ctx.engine_type or "",
                    provider_id=_ctx.provider_id or 0,
                )
            except Exception as _se:
                log.warning(f"{_engine_tag}state report running failed: {_se}")

        # skill 物化已迁移到 DefaultMaterializer → SkillMaterializer（统一在 prompt 取值后处理）
    except Exception as e:
        log.error(f"{_engine_tag}sandbox setup failed: {e}")
        await _log("sandbox_fail", f"err={e}")
        # 异常场景：prepare 失败时也要上报沙箱状态 + kill 残留沙箱
        _fail_sandbox_id = getattr(_sandbox, '_sandbox_id', '') or '' if _sandbox else ''
        if _fail_sandbox_id:
            try:
                await asyncio.to_thread(_sandbox.kill)
            except Exception:
                pass
            try:
                await asyncio.shield(client.report_sandbox_status(
                    tid,
                    issue_id=task.get("issue_id", ""),
                    repo_key=task.get("repo_key", ""),
                    sandbox_id=_fail_sandbox_id,
                    state="killed",
                    provider=_ctx.sandbox_provider or "",
                ))
            except (Exception, asyncio.CancelledError):
                pass
        # [沙箱迁移] setup_failed 行：沙箱列表页可见失败原因——有真实 sandbox_id 用之
        # （kill 后行已存在，此处覆盖 state+error）；无（建箱即失败）用虚拟 id
        # setup-{task_id} 让列表留痕。模板/引擎取任务上下文快照。
        try:
            await asyncio.shield(client.report_sandbox_status(
                tid,
                issue_id=task.get("issue_id", ""),
                repo_key=task.get("repo_key", ""),
                sandbox_id=_fail_sandbox_id or f"setup-{tid}",
                state="setup_failed",
                provider=_ctx.sandbox_provider or "",
                agent_installed=0,
                template_id=_ctx.template_id or "",
                template_name=_ri_template_name(task),
                engine_type=_ctx.engine_type or "",
                provider_id=_ctx.provider_id or 0,
                error=f"sandbox setup failed: {e}",
            ))
        except (Exception, asyncio.CancelledError):
            pass
        # 撤销 proxy token（prepare 失败时 token 已签发）
        try:
            await _services.revoke_proxy_token(tid)
        except Exception:
            pass
        return {"_error": f"sandbox setup failed: {e}"}

    # 5. 执行 agent
    _result = None
    await _log("exec_start", f"engine={engine_name}")
    # 心跳线程：agent 执行期间定期上报 progress 到 task_messages，防止 reap 误判卡死
    # 通用能力——所有云沙箱引擎（hermes/opencode/codearts）在 LLM 调用或工具执行期间
    # 可能长时间无事件产出，但任务仍在正常执行。心跳让 reap 活跃度检查能看到任务活着。
    _heartbeat_stop = asyncio.Event()
    _task_cancelled = asyncio.Event()

    async def _heartbeat_loop():
        while not _heartbeat_stop.is_set():
            try:
                await asyncio.wait_for(_heartbeat_stop.wait(), timeout=120)
            except asyncio.TimeoutError:
                pass
            if _heartbeat_stop.is_set():
                break
            try:
                await client.report_progress(tid, summary=f"[heartbeat] engine={engine_name} executing")
            except Exception:
                pass
            # [沙箱迁移] 取消感知：任务被服务端取消(cancelled)时，云沙箱里的引擎
            # 进程本机杀不到(_kill_stale_engine 只对本地进程有效)，必须在执行循环
            # break → finally 的 _sandbox.kill() 走 SDK 清理，否则沙箱在平台上
            # 永久 running 泄漏（2026-10-26 ebdae55a94d5 案例：任务 cancelled
            # 3 天后沙箱仍占 2C2G）
            try:
                _state = await client.get_task_state(tid)
                if _state == "cancelled":
                    log.warning(f"{_engine_tag}task cancelled by server, breaking execution loop (state=cancelled)")
                    _task_cancelled.set()
                    _heartbeat_stop.set()
                    break
            except Exception:
                pass

    _heartbeat_task = asyncio.create_task(_heartbeat_loop())
    try:
        prompt = task.get("prompt") or ""
        cwd = task.get("cwd") or ""
        session_id = task.get("runtime_session_id") or ""

        # 统一文件物化（skill + 项目知识 + 共享知识）
        # Bridge：注入 CloudSandboxSink，Materializer 不直接碰 sandbox
        from src.domains.runtime.agents.materializer import DefaultMaterializer, CloudSandboxSink, SkillMaterializeError
        _sink = CloudSandboxSink(_sandbox)
        _materializer = DefaultMaterializer(
            sink=_sink,
            env=env,
            progress=_log,       # async 回调：进度/状态日志（cloud_worker 已有 _log）
            client=client,       # 只用于降级统计 report_messages
            tid=tid,
            log_tag=_engine_tag,
        )
        try:
            prompt = await _materializer.materialize(task, prompt)
        except SkillMaterializeError as e:
            await _log("skill_fail", f"err={e}")
            return {"_error": f"skill materialize failed: {e}"}

        # [看门狗 2026-11-12] #378 差异化静默超时注入——cloud_worker 走独立执行路径
        # (_cloud_execute_task)，agent.py _execute_task_impl 的注入块不经过这里，
        # 云沙箱任务此前恒为默认 300s（实测 vod t_52208a13e9ad：executor 已接线
        # tool_idle_timeout=_silent_timeout_for(proc_env)，但 env 无值 → 301s 被杀）。
        # 口径与 agent.py #378 完全一致：按 agent_type setdefault，手动调参不覆盖。
        try:
            from src.domains.runtime.engine.agent import _AGENT_SILENT_TIMEOUTS
            _cw_silent = _AGENT_SILENT_TIMEOUTS.get((task.get("agent_type") or "").lower())
            if _cw_silent:
                env.setdefault("AGENT_SILENT_WINDOW", str(_cw_silent))
                env.setdefault("HERMES_SILENT_TIMEOUT", str(_cw_silent))
        except Exception:
            pass

        # 通过 _dispatch_execution 执行（统一分叉：云沙箱 → _run_cloud_sandbox）
        # backend 从 ctx.engine_type 取（注册时声明，不靠引擎名前缀匹配）
        backend_name = _ctx.engine_type or backend_for_engine(engine_name)
        if not backend_name:
            await _log("fail", f"unknown engine type: engine_type={_ctx.engine_type} engine_name={engine_name}")
            return {"_error": f"unknown engine type: {_ctx.engine_type or engine_name}"}

        try:
            backend = get_backend(backend_name)
        except Exception as e:
            await _log("fail", f"get_backend failed: backend_name={backend_name} err={e}")
            return {"_error": f"unknown backend: {backend_name}"}

        # M-3 修复：max_exec_seconds 用 task_timeout（默认 3600），不用 _dispatch_execution 的 600
        _task_timeout = int(os.environ.get("RUNTIME_TASK_TIMEOUT", "3600"))

        from src.domains.runtime.agents.executor import dispatch_execution_async
        _text_buf = []  # text/reasoning 事件合并缓冲（ACP 流式输出会将一条消息拆成多个 chunk）
        _buf_type = None  # 当前缓冲的事件类型（text 或 reasoning）
        async for evt_type, data in dispatch_execution_async(
            _sandbox, backend, prompt, cwd, env,
            session_id=session_id,
            label=_engine_tag,
            max_exec_seconds=_task_timeout,
        ):
            # [沙箱迁移] 服务端取消 → 中断执行（finally 会 _sandbox.kill() 清理远端沙箱）
            if _task_cancelled.is_set():
                _result = {"_error": "cancelled", "cancelled": True}
                break
            if evt_type == "done":
                _result = data
                # flush 残留 text/reasoning
                if _text_buf:
                    try:
                        await client.report_messages(tid, [{"type": _buf_type or "text", "data": "".join(_text_buf)}])
                    except Exception:
                        pass
                    _text_buf.clear()
                await _log("exec_done", f"result_keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
                break
            if evt_type == "error":
                _result = {"_error": str(data)}
                if _text_buf:
                    try:
                        await client.report_messages(tid, [{"type": _buf_type or "text", "data": "".join(_text_buf)}])
                    except Exception:
                        pass
                    _text_buf.clear()
                await _log("exec_error", f"err={data}")
                break
            # report progress messages
            # text/reasoning 事件合并：ACP 流式输出会将一条消息拆成多个 chunk，合并后上报避免前端展示错位
            # step_finish 不触发 flush——ACP 在 stopReason=end_turn 时先发 step_finish 再发剩余 text，
            # 如果 flush 会把同一条 text 拆成两段
            if evt_type in ("text", "reasoning"):
                if _buf_type and _buf_type != evt_type:
                    # 类型切换时先 flush 旧缓冲
                    try:
                        await client.report_messages(tid, [{"type": _buf_type, "data": "".join(_text_buf)}])
                    except Exception:
                        pass
                    _text_buf.clear()
                _buf_type = evt_type
                _text_buf.append(data)
                continue
            # step_finish 上报但不 flush text 缓冲（ACP 可能在 text 中间插入 step_finish）
            if evt_type == "step_finish":
                try:
                    await client.report_messages(tid, [{"type": evt_type, "data": data}])
                except Exception:
                    pass
                continue
            # flush 积累的 text/reasoning（遇到其他事件时先上报）
            if _text_buf:
                _merged = "".join(_text_buf)
                _flush_type = _buf_type or "text"
                _text_buf.clear()
                _buf_type = None
                try:
                    await client.report_messages(tid, [{"type": _flush_type, "data": _merged}])
                except Exception:
                    pass
            if evt_type in ("step_start", "tool_use", "tool_result", "error"):
                try:
                    await client.report_messages(tid, [{"type": evt_type, "data": data}])
                except Exception:
                    pass

    except Exception as e:
        log.error(f"{_engine_tag}execution failed: {e}")
        await _log("exec_fail", f"err={e}")
        _result = {"_error": str(e)}
    finally:
        import asyncio as _aio
        # 停止心跳线程
        _heartbeat_stop.set()
        _heartbeat_task.cancel()
        try:
            await _aio.wait_for(_heartbeat_task, timeout=3)
        except (Exception, _aio.CancelledError, _aio.TimeoutError):
            pass
        # 在 finally 第一行抓 exc_info——避免被后续 except 污染
        _exc_info = sys.exc_info()
        if _sandbox is not None:
            # 1. kill（仅 CloudSandbox——需要活的 SDK 连接，必须在 __exit__ 之前）
            #    BwrapSandbox.kill() / NullSandbox.kill() 是空操作
            _sandbox_id_to_kill = getattr(_sandbox, '_sandbox_id', '') or ''
            if _sandbox_id_to_kill:
                _kill_ok = False
                for _kill_attempt in range(3):
                    try:
                        # P1-8 修复：kill() 是 sync SDK HTTP，包入 to_thread 避免阻塞事件循环
                        await _aio.to_thread(_sandbox.kill)
                        _kill_ok = True
                        log.info(f"{_engine_tag}sandbox killed: {_sandbox_id_to_kill} attempt={_kill_attempt+1}")
                        break
                    except Exception as _ke:
                        _ke_str = str(_ke).lower()
                        if "not found" in _ke_str or "404" in _ke_str or "does not exist" in _ke_str:
                            _kill_ok = True
                            log.info(f"{_engine_tag}sandbox already gone: {_sandbox_id_to_kill}")
                            break
                        log.warning(f"{_engine_tag}sandbox kill attempt={_kill_attempt+1} failed: {_ke}")
                        try:
                            import asyncio as _aio
                            await _aio.shield(_aio.sleep(2 ** _kill_attempt))
                        except (Exception, _aio.CancelledError):
                            break
                if not _kill_ok:
                    log.error(f"{_engine_tag}sandbox kill failed 3x, leaving to idle reaper: {_sandbox_id_to_kill}")
                else:
                    # 问题1 修复：kill 成功后直接 await 上报 killed 状态（shield 防取消）
                    try:
                        await _aio.shield(client.report_sandbox_status(
                            tid,
                            issue_id=task.get("issue_id", ""),
                            repo_key=task.get("repo_key", ""),
                            sandbox_id=_sandbox_id_to_kill,
                            state="killed",
                            provider=_ctx.sandbox_provider or "",
                        ))
                    except (Exception, _aio.CancelledError):
                        pass
            else:
                # bwrap/null sandbox——kill 是空操作，直接调
                try:
                    # P1-8 修复：统一用 to_thread（bwrap kill 虽是空操作，保持一致性）
                    await _aio.to_thread(_sandbox.kill)
                except Exception as e:
                    log.warning(f"{_engine_tag}sandbox kill failed: {e}")
            # 2. __exit__（关 SDK 连接 / cleanup NetworkProxy，幂等）
            try:
                # P2-13 修复：__exit__ -> cleanup() -> SDK close() 是 sync HTTP，包入 to_thread
                await _aio.to_thread(_sandbox.__exit__, *_exc_info)
            except Exception as e:
                log.warning(f"{_engine_tag}sandbox __exit__ failed: {e}")
        # 3. 撤销 proxy token（无论沙箱是否创建成功都撤销——token 在 prepare 之前签发）
        try:
            await _aio.shield(_services.revoke_proxy_token(tid))
        except (Exception, _aio.CancelledError):
            pass

    # 上报最终状态（complete/fail）
    if _result and isinstance(_result, dict) and _result.get("_error"):
        await _log("fail", f"error={_result['_error']}")
    else:
        await _log("complete", f"task={tid}")

    # 把 sandbox_id 放入 _result，供 run_loop 传给 client.complete/fail
    _sandbox_id_used = getattr(_sandbox, '_sandbox_id', '') or '' if _sandbox else ''
    if _result and isinstance(_result, dict):
        _result["sandbox_id"] = _sandbox_id_used
    else:
        _result = {"_error": "no done event", "sandbox_id": _sandbox_id_used}
    return _result


async def _refresh_templates(client, config):
    """心跳时查模板列表 → diff 增量注册变化的、删除的标 offline。

    和旧实现的区别：
    - 模板展开复用 _expand_templates（消除重复 + preinstalled 默认值统一）
    - 增量注册：只注册 runtime_info 变化的条目，未变化的不触发 DB 写入
    - 删除的单独标 offline
    """
    from src.domains.runtime.engine.client import CredentialRevokedError as _CRE
    try:
        templates = await client.get_templates()
        if not templates:
            # [云 worker 领取修复] 空结果必须区分两种语义，调用方据此决定要不要覆盖
            # 已注册清单（此前一律返回 []，一次 499/超时就把 _installed_live 清空 →
            # 下一轮 register([]) → runtime_ids 归零 → 永不 claim）：
            #   []        = 平台确实没有模板（真空）→ 返回空清单，允许同步清空
            #   None/其他 = 拉取失败/未知        → 返回 None 哨兵，沿用上一份快照
            if templates is None:
                return None, set(), None
            return {}, set(), []

        new_runtimes = _expand_templates(templates)

        # v3 R16：首次从 DB 初始化 _last_runtimes——进程重启后为空，无法对比删除
        if not hasattr(_refresh_templates, "_last_runtimes") or not _refresh_templates._last_runtimes:
            try:
                _existing = await client.get_cloud_runtimes()
                if _existing:
                    _refresh_templates._last_runtimes = _existing
                    _refresh_templates._last_hash = ""  # 强制 diff
            except Exception:
                pass

        # hash 前按 name 排序——json.dumps(sort_keys=True) 只排 dict key 不排列表元素，
        # 同内容不同顺序的列表会生成不同 hash → 虚假"已变更"→ 不必要的重注册
        _sorted_runtimes = sorted(new_runtimes, key=lambda r: r.get("name", ""))
        _new_hash = hashlib.md5(json.dumps(_sorted_runtimes, sort_keys=True).encode()).hexdigest()
        if _new_hash == getattr(_refresh_templates, "_last_hash", ""):
            return {}, set(), getattr(_refresh_templates, "_last_runtimes", []) or []

        _old_runtimes = getattr(_refresh_templates, "_last_runtimes", [])
        _new_names = {r["name"] for r in new_runtimes}
        _old_names = {r["name"] for r in _old_runtimes}

        _old_by_name = {r["name"]: r for r in _old_runtimes}
        _changed = [r for r in new_runtimes
                    if r["name"] not in _old_by_name
                    or r["runtime_info"] != _old_by_name[r["name"]]["runtime_info"]]
        _deleted = _old_names - _new_names

        # os.uname 仅 Unix；Windows 上 AttributeError（2026-11-19）。走跨平台 adapter。
        from src.domains.runtime.engine import os_adapter as _os_adapter
        device = config.get("device_name") or _os_adapter.hostname()

        _new_rid_by_name: dict[str, int] = {}
        if _changed:
            _ids, _ = await client.register(_changed, device_name=device,
                                  cli_version="cloud-worker-1.0", provider="")
            log.info(f"template refresh: {len(_changed)} runtimes updated")
            # register 返回 ids 与 _changed 顺序一致；只回填「新增」条目
            # （老条目 rid 已在心跳列表，重复 append 会致心跳重复）
            _added_names = {r["name"] for r in _changed} - _old_names
            # Issue #4082（PR #2066 审查 Optional ①）：回包里若有非法 id，client 已逐条过滤，
            # `_ids` 不再与 `_changed` 位置一一对应——此时改按回包明细的 name 配对，避免 zip 错位
            # 把 rid 配给别的引擎（老行为是整包失败，不会错配；新行为必须保证不错配）。
            from src.domains.runtime.engine.agent import _register_partial_ids
            from src.domains.runtime.engine.runtime_id_guard import parse_runtime_id
            _partial = _register_partial_ids(client)
            if _partial:
                _detail = getattr(client, "last_register_detail", None)
                _rid_by_name = {}
                for _d in (_detail if isinstance(_detail, (list, tuple)) else []):
                    if not isinstance(_d, dict):
                        continue
                    _rid = parse_runtime_id(_d.get("id"))
                    _nm = str(_d.get("name") or "").strip().lower()
                    if _nm and _rid is not None:
                        _rid_by_name[_nm] = _rid
                _added_lc = {str(n).strip().lower() for n in _added_names}
                for r in _changed:
                    _nm = str(r["name"]).strip().lower()
                    if _nm in _added_lc and _nm in _rid_by_name:
                        _new_rid_by_name[r["name"]] = _rid_by_name[_nm]
                log.warning(f"template refresh: register reply carried invalid runtime id(s) "
                            f"{_partial}; mapped new rids by name and will re-register to "
                            f"backfill the rest")
            else:
                for r, rid in zip(_changed, _ids or []):
                    if r["name"] in _added_names:
                        _new_rid_by_name[r["name"]] = rid

        if _deleted:
            _offline_runtimes = [{"name": n, "version": "", "healthy": False,
                                   "installed": False, "cli_path": "",
                                   "provider": "", "runtime_type": "cloud",
                                   "runtime_info": "{}"} for n in _deleted]
            await client.register(_offline_runtimes, device_name=device,
                                  cli_version="cloud-worker-1.0", provider="")
            log.info(f"template deleted: {len(_deleted)} runtimes set offline: {_deleted}")

        _refresh_templates._last_runtimes = new_runtimes
        _refresh_templates._last_hash = _new_hash
        # 返回 (new_rid_by_name, deleted)——新增 rid 映射交调用方加入心跳列表
        # （否则注册后无心跳，服务端 100s reap 判 offline——「生而不养」缺陷，
        #  2026-10-28 主仓 :38 与 8081 opencode:devbox:3 双实证）
        return _new_rid_by_name, _deleted, new_runtimes
    except _CRE:
        raise  # [401 自杀修复] 凭据吊销向上传播——refresh loop 的 except 也要放行
    except Exception as e:
        log.warning(f"_refresh_templates failed: {e}")
        return None, set(), None


async def _cloud_run_loop(client, runtime_ids, config, engine_by_rid, installed, device_name):
    """cloud_worker 主循环——复用 agent.py 的 run_loop，注入 _execute_fn。

    另起后台任务定期刷新模板（60s）→ 对比 runtime_info → 变化的更新。
    runtime_ids 传 list（原地修改共享引用）——refresh 下线的引擎从中剔除，
    否则 run_loop 的心跳会把已下线引擎刷回 online（心跳 UPDATE 无条件 online）。
    """
    from src.domains.runtime.engine.agent import _run_loop as run_loop  # [沙箱迁移] main 谱系为 _run_loop（私有名）；签名含 _execute_fn 注入位

    # rid → name 反查（engine_by_rid 是 rid→实例名）
    _name_by_rid = {rid: (nm or "").split("-")[0] for rid, nm in (engine_by_rid or {}).items()}

    # 后台模板刷新任务
    # [spec §6.2/§6.3] 每 5 个刷新周期(~5min)顺路调 whoami: 改名跟随 + 身份守护 +
    # 401/403 自杀计数(低频探测, 连续 3 次 401/403 ≈ 3min 新鲜证据即判吊销——比
    # 心跳路径的 N=10 更快, 因为 whoami 每次都是直接证据而非间接超时)
    _whoami_401_streak = 0
    _whoami_tick = 0
    # [探测重试 2026-11-04] 模板探测失败计数（run_loop 里注册后首轮赋值；refresh loop
    # 依据它决定是否重跑 provider 上报+探测）
    _probe_fail_count = 0
    # [新模板看护 2026-11-20 用户裁定] 平台侧模板 id 快照（provider 键 → id 集合）：
    # refresh 循环每 60s 轻量 list_templates 对比，发现新增 → set 探测唤醒事件。
    _last_platform_tids: dict = {}

    async def _template_refresh_loop():
        nonlocal _whoami_401_streak, _whoami_tick, _probe_fail_count
        _fail_streak = 0
        _base_interval = config.get("refresh_base_interval", 60)
        _fail_threshold = int(config.get("refresh_fail_threshold", 3))
        _max_interval = config.get("refresh_max_interval", 600)
        while True:
            _interval = _base_interval if _fail_streak < _fail_threshold else min(
                _base_interval * (2 ** (_fail_streak - _fail_threshold)), _max_interval)
            await asyncio.sleep(_interval)
            try:
                _new_rid_by_name, _deleted, _all_runtimes = await _refresh_templates(client, config)
                await _refresh_provider_configs(client)
                _fail_streak = 0
                # [探测重试] 由 _amain 内 _probe_retry_loop 负责（跨函数作用域不可达）
                # [新模板看护 2026-11-20] 平台侧模板 id 集合变更检测（轻量元数据扫描，
                # 不建箱）：与 _report_providers_once 同款 list_templates 调用。发现新增
                # id → set config["probe_watch_trigger"] 唤醒探测循环（负缓存挡已探模板，
                # 只建箱探测新增 id）。扫描失败不更新该 provider 的快照（瞬时故障不
                # 掩盖下轮变更）；首轮只建快照不触发（启动探测批已覆盖）。
                _evt = config.get("probe_watch_trigger")
                if _evt is not None:
                    _seen: dict = {}
                    for _pc in list((_provider_configs or {}).values()):
                        _ep = (_pc.get("api_url") or _pc.get("endpoint") or "").strip()
                        _pk = (_pc.get("name") or _ep or "").strip()
                        if not _pk:
                            continue
                        _old_ids = _last_platform_tids.get(_pk)
                        if not _ep:
                            _seen[_pk] = _old_ids or set()
                            continue
                        try:
                            from src.domains.runtime.agents.sandbox.backends import get_adapter as _ga
                            _ad = _ga(_pc.get("platform", ""))
                            if not _ad or not hasattr(_ad, "list_templates"):
                                _seen[_pk] = _old_ids or set()
                                continue
                            _extra_cfg = _pc.get("extra_config") or {}
                            # to_thread：list_templates 是同步网络调用——本循环 60s 一轮
                            # （_report_providers_once 的同款调用频次低得多），阻塞事件循环
                            # 会拖心跳/claim 调度。
                            _scan = await asyncio.to_thread(_ad.list_templates, {
                                "api_url": _ep,
                                "api_key": _pc.get("api_key", ""),
                                **({"gateway_ip": _extra_cfg["gateway_ip"]}
                                   if _extra_cfg.get("gateway_ip") else {}),
                                **({"gateway_url": _extra_cfg["gateway_url"]}
                                   if _extra_cfg.get("gateway_url") else {}),
                            })
                            if _scan is None:
                                # SDK 未装：不可扫描——保留旧快照
                                _seen[_pk] = _old_ids or set()
                                continue
                            _seen[_pk] = {(t.get("template_id") or "").strip()
                                          for t in (_scan or [])
                                          if (t.get("template_id") or "").strip()}
                        except Exception as _se:
                            # 扫描失败：沿用旧快照——平台恢复后新增 id 仍能被 diff 出
                            _seen[_pk] = _old_ids or set()
                            log.debug(f"[engine-probe] watch scan failed for {_pk}: {_se}")
                    _new_tids = set()
                    for _pk, _ids in _seen.items():
                        if _pk in _last_platform_tids:
                            _new_tids |= (_ids - _last_platform_tids[_pk])
                    # provider 消失 → 清其快照（回来时重新基线，不误触发）
                    for _pk in list(_last_platform_tids.keys()):
                        if _pk not in _seen:
                            del _last_platform_tids[_pk]
                    _last_platform_tids.clear()
                    _last_platform_tids.update(_seen)
                    if _new_tids:
                        log.info(f"[engine-probe] watch: {len(_new_tids)} new platform "
                                 f"template id(s) detected, waking probe loop: "
                                 f"{sorted(t[:16] for t in _new_tids)}")
                        _evt.set()
                # ---- whoami: 改名跟随/身份守护/401 自杀（spec §6.2/§6.3）----
                _whoami_tick += 1
                if _whoami_tick % 5 == 0:
                    # [401 自杀修复 2026-10-30] 分类必须按异常类型(CredentialRevokedError)——
                    # 其 str() 是 path 不含 "401" 字样, 文本匹配永不命中(.17 实证: 1397 次
                    # 401 循环 8 小时不自杀的根因)
                    from src.domains.runtime.engine.client import CredentialRevokedError as _CRE
                    try:
                        await _whoami_follow_and_guard(client)
                        _whoami_401_streak = 0
                    except RuntimeError:
                        raise  # 401/凭据失效——直接终止
                    except _CRE as _we:
                        _whoami_401_streak += 1
                        log.warning(f"[whoami] auth failure streak={_whoami_401_streak}: {_we}")
                        # [N=5 用户裁定 2026-10-30] 401 是确定性证据(网络抖动产生超时/连接错误而非 401),
                        # 5 次(~75s)足够容错真 TLS hiccup 又不拖吊销判定
                        if _whoami_401_streak >= 5:
                            log.error("credential revoked (whoami 401/403 x5), exiting")
                            import sys as _sys
                            _sys.exit(1)
                    except Exception as _we:
                        log.warning(f"[whoami] follow failed: {_we}")
                # 新引擎加进心跳列表（原地 append，run_loop 共享此 list）——
                # 「生而不养」缺陷修复：增量注册的新 rid 若不进心跳列表，
                # 服务端 100s 内 reap 判 offline（主仓 :38 与 8081 opencode:devbox:3 双实证）
                from src.domains.runtime.engine.instances import instance_name as _insn
                # [身份锚定] 无条件同步 re-register 动态视图——改名时 rid 不变（锚命中 UPDATE），
                # added 循环不触发（rid 已在心跳列表），旧版仅在 added 时同步会让 5min re-register
                # 用旧名快照把引擎名写回（s14 被 re-register 覆盖回 s13 实测根因）
                # [云 worker 领取修复] 只有本次刷新拿到了真实的模板结果才覆盖已注册清单；
                # None = 拉取失败/未知（含 499、超时、平台不可达），沿用上一份快照，
                # 否则一次失败就会把清单清空 → register([]) → runtime_ids 归零 → 永不 claim。
                if _all_runtimes is not None:
                    _installed_live[:] = _all_runtimes
                for _n, _rid in (_new_rid_by_name or {}).items():
                    if _rid and _rid not in runtime_ids:
                        runtime_ids.append(_rid)
                        engine_by_rid[_rid] = _insn(_n, device_name, 0)
                        _name_by_rid[_rid] = _n  # 下线剔除要用
                        log.info(f"runtime {_n} (rid={_rid}) added to heartbeat list")
                # 下线引擎从心跳列表剔除（原地 remove，run_loop 共享此 list）
                for _dn in (_deleted or set()):
                    for _rid in [r for r in runtime_ids if _name_by_rid.get(r) == _dn]:
                        runtime_ids.remove(_rid)
                        _installed_live[:] = [r for r in _installed_live if r.get("name") != _dn]
                        log.info(f"runtime {_dn} (rid={_rid}) removed from heartbeat list")
            except _CRE:
                # [401 自杀修复] 凭据吊销: 每轮 refresh 直接计数(不占 5 周期 whoami 位),
                # N=10 与 spec §6.3 心跳口径一致(~150s 判定)
                _whoami_401_streak += 1
                log.warning(f"[refresh] credential revoked streak={_whoami_401_streak}")
                # [N=5 用户裁定 2026-10-30] 同 whoami 路径统一(原 10 偏保守——401 确定性证据无需长窗口)
                if _whoami_401_streak >= 5:
                    log.error("credential revoked (refresh 401 x5), exiting")
                    import sys as _sys
                    _sys.exit(1)
            except Exception as e:
                _fail_streak += 1
                log.warning(f"template refresh failed (streak={_fail_streak}): {e}")

    # [换名收敛] installed 动态视图——agent._run_loop 的定期 re-register（5min）用闭包参数
    # installed 全量重注册；传启动快照会把 refresh 已下线的旧名行反复复活（s1→s2 换名实测：
    # refresh 删 19/20 → re-register 用快照重注 19/20）。refresh 的 added/deleted 原地同步本列表。
    _installed_live = list(installed or [])
    _orig_installed = installed

    # 后台模板刷新任务（进程退出时自动 cancel）
    _refresh_task = asyncio.create_task(_template_refresh_loop())
    log.info(f"run_loop starting: runtime_ids={runtime_ids} poll={config['poll_interval']}s")
    try:
        await run_loop(
            client, runtime_ids,
            poll_interval=config["poll_interval"],
            heartbeat_interval=config["heartbeat_interval"],
            max_concurrent=config["max_concurrent"],
            engine_by_rid=engine_by_rid,
            installed=_installed_live,
            device_name=device_name,
            _execute_fn=_cloud_execute_task,  # 注入云沙箱执行函数
        )
    finally:
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            pass


def _startup_engine_by_rid(client, ids, runtimes, device_name):
    """启动注册后 rid → 引擎实例名：复用 agent 的按 name 配对，并对部分非法 id 落 partial 日志。

    PR #2066 复审 Required：`client.register` 已改为逐条过滤非法 id，回包含非法 id 时
    `ids` 可能比 `runtimes` 短，旧的 `zip(ids, runtimes)` 会错位把 rid 配给别的引擎
    （`_backend_for_engine` 据此选后端，错位会把任务派给错误引擎）。`_refresh_templates`
    与 agent 启动路径已统一为按回包明细 name 配对，此处第三处一并收敛。
    """
    from src.domains.runtime.engine.agent import (
        _engine_by_rid_from_register, _register_partial_ids,
    )
    engine_by_rid = _engine_by_rid_from_register(client, ids, runtimes, device_name)
    partial_ids = _register_partial_ids(client)
    if partial_ids:
        log.warning(
            f"cloud_worker register reply carried invalid runtime id(s) {partial_ids}; "
            f"mapped rids to engines by name, missing engine(s) will be backfilled "
            f"by the periodic re-register")
    return engine_by_rid


def main():
    """cloud_worker 进程入口。

    退出码约定（systemd / supervisor 可区分失败原因）：
      0 = 正常退出（disabled 或运行结束）
      2 = 启动时无模板返回（no_templates）
      3 = 模板展开后无 runtime（no_runtimes——模板 engines 为空）
      4 = 注册失败（register_failed——key 文件 / 服务端不可达）
      1 = 其他异常
    """
    os.environ.setdefault("GRAPE_RUNTIME_MODE", "cloud_worker")

    config = _load_config()

    # 启用检查——disabled 时直接退出，不读 key / 不连服务端
    if not config.get("enabled", True):
        print("cloud_worker is disabled (enabled=false in config or CLOUD_WORKER_ENABLED env), exiting", file=sys.stderr)
        sys.exit(0)

    os.environ.setdefault("GRAPE_WORKER_PROVIDER", config.get("provider", "devbox"))
    # [变量统一] 把 cloud_worker 自有配置映射到 agent.py 的 RUNTIME_* 命名空间——
    # _run_loop（agent.py，两类 worker 共用）深处的自动升级读 RUNTIME_SERVER_URL/
    # RUNTIME_TOKEN，此前变量名不匹配导致沙箱 worker 升级链路断（"missing server
    # or token" 无限重试）。沙箱分支未上线、无存量机器，直接收敛不保留别名。
    os.environ.setdefault("RUNTIME_SERVER_URL", config["server_url"])
    # [key 机制废弃 2026-10-29 用户裁定] 鉴权唯一来源：CLOUD_WORKER_TOKEN env
    # （rta_ 凭据，页面「生成凭据」→ 一键安装注入）。原 cloud-local.key 文件机制删除——
    # 普通普通 worker 从来只认 RUNTIME_TOKEN env，cloud worker 双轨收敛为同一心智模型。
    worker_key = os.environ.get("CLOUD_WORKER_TOKEN", "").strip()
    if not worker_key:
        print("CLOUD_WORKER_TOKEN not set — generate a sandbox credential on the grape WebUI, "
              "then use install-worker.sh --token <rta_...> --server <url> to install.", file=sys.stderr)
        sys.exit(1)
    # token 同步映射（升级脚本要带它重新安装自己）
    # [隐性依赖声明 2026-10-29] 此映射不只是升级链路需要——isolation 三处
    # （policy._fetch_skill_egress / network._WhitelistCache / bwrap 白名单刷新）
    # 读 RUNTIME_TOKEN 拉 /api/runtime/whitelist 与 skill-egress；d1e242cba 删除
    # key 文件兜底后，cloud_worker 走 bwrap 路径时此映射是 isolation 取 token
    # 的唯一通道。**不可单独回滚本映射**，否则 cloud_worker 的 bwrap 白名单静默断。
    os.environ.setdefault("RUNTIME_TOKEN", worker_key)

    # 沙箱组件初始化（和 agent.py 一致）
    try:
        from src.domains.runtime.agents.sandbox.bootstrap import init_sandbox_components
        init_sandbox_components()
    except Exception as e:
        log.warning(f"sandbox init failed: {e}")

    async def _probe_templates_engines(adapter, pcfg, tpl_list, device) -> tuple[list, int]:
        """[引擎探测 spec §2.5] 建箱层全量探测：逐模板建箱 → 白名单扫描 → kill。

        即建即杀（幂等，与模板检查同款）；单模板失败不拖垮整批；探测在
        asyncio.to_thread 里跑（create/kill 是同步 SDK 调用）。分批流控：
        串行逐模板（建箱本身就是平台资源消费，探测箱不并发）。
        [模板上限 2026-11-04] 单批最多 PROBE_TEMPLATES_MAX（默认 8）——防模板
        爆炸串行探测数小时（每箱 20-40s，8 个 ≈ 3-5min）。超出的截断跳过并告警
        （计入失败 → 重试循环下轮会再探）。
        返回 (结果列表, 失败计数)。结果列表 [{"template_id", engines: [...]}]
        ——探测成功但无引擎的模板也进列表（engines=[]），由调用方原样上报
        （服务端空 engines 不落 binding）并写负缓存 ok=true（[2026-11-20 用户裁定]
        无引擎模板探完即成功，不再重探——此前不进列表导致对账公式永远差 1、
        重试循环永不停、每 1800s 白费一轮建箱）。失败计数 = 建箱探测异常的模板数
        + 本轮截断未探的模板数（不含无引擎的成功探测）。
        """
        import asyncio as _aio
        import time as _time_mod
        import datetime as _datetime
        _max = int(os.environ.get("PROBE_TEMPLATES_MAX", "8"))
        # [负缓存 2026-11-04 用户设计] probe-state.json：已探测模板的结果与时间——
        # "确认无引擎"的模板不再重探（消掉对账缺口里'真无引擎模板永远差 1'的重试成本）。
        # 结构: {template_id: {engines: [...], probed_at: iso, ok: bool}}
        # 语义: ok=true 的模板本轮直接跳过（缓存命中）；ok=false（建箱失败等）不缓存
        # ——失败必须靠重试循环重探。缓存只增不改错；模板引擎变化时由"重装/重启"清缓存
        # （重装=全新探测语义，安装脚本保留 data/ 不清 etc/ 状态——用户可手删该文件强制全量重探）。
        _neg_cache = {}
        try:
            with open(_probe_state_json_path(), "r") as f:
                _neg_cache = (json.load(f) or {}).get("templates", {})
        except Exception:
            _neg_cache = {}
        _tid_list = [t for t in (tpl_list or []) if (t.get("template_id") or "").strip()]
        _cached_hits = [t for t in _tid_list
                        if _neg_cache.get((t.get("template_id") or "").strip(), {}).get("ok")]
        if _cached_hits:
            log.info(f"[engine-probe] negative cache hit: {len(_cached_hits)}/{len(_tid_list)} "
                     f"templates already probed OK, skipping them")
        _tid_list = [t for t in _tid_list
                     if not _neg_cache.get((t.get("template_id") or "").strip(), {}).get("ok")]
        _pre_cap = len(_tid_list)
        if _pre_cap > _max:
            log.warning(f"[engine-probe] template count {_pre_cap} > cap {_max} "
                         f"(PROBE_TEMPLATES_MAX) — probing first {_max} this round, rest "
                         f"deferred to the retry loop")
            _tid_list = _tid_list[:_max]
        _deferred = _pre_cap - len(_tid_list)
        tpl_list = _tid_list
        out = []
        _probe_failed = 0   # 建箱探测异常计数（不含无引擎的成功探测）
        _leak_watch: set = set()  # 本批触碰过的 sandbox_id（批末清扫验证）
        _total = len([t for t in (tpl_list or []) if (t.get("template_id") or "").strip()])
        log.info(f"[engine-probe] start: {_total} templates, ~20-40s each (creates a "
                 f"temporary sandbox per template, kills it right after)")
        _done = 0
        for t in (tpl_list or []):
            _tid = (t.get("template_id") or "").strip()
            if not _tid:
                continue
            _done += 1
            _t0 = _time_mod.monotonic()
            log.info(f"[engine-probe] [{_done}/{_total}] creating sandbox for template "
                     f"{_tid[:24]} ...")

            def _probe_one(tid=_tid):
                # 借用 adapter 造 Provider（与 check-template/kill 同款构造）
                prov = adapter.build_provider(
                    audit=None, credential=None, seccomp=None, alert_engine=None,
                    config=dict(pcfg, template_id=tid))
                inst = prov.create_sandbox(task_id=f"probe-{device[:12]}-{tid[:8]}",
                                           template_id=tid)
                engines = []
                try:
                    with inst:   # __enter__ 建箱+初始化（失败自会 _do_kill 防泄漏）
                        engines = adapter._probe_engines_shell(inst)
                        # [泄漏修复 v2 2026-11-03] kill 必须在 with 内做——
                        # cloud_base.__exit__ 会 cleanup SDK 会话（_sandbox 置空），
                        # with 外再调 _do_kill 会走"未连接返回 False"分支，
                        # 远端沙箱实际未销毁（.17 实证：17:15 三箱泄漏，6000
                        # milli-core 资源账挂到 shim 进程手杀才释放）。探测即建即杀
                        # 语义 = 扫描完立刻销毁，不等 with 退出。
                        try:
                            _killed = bool(inst.kill())
                            # [2026-11-06 泄漏修复] kill 返回成功≠平台真终止——E2E 实证
                            # DELETE 2xx 后箱仍 running（CubeMaster 侧吞掉）。kill 后
                            # get_info 验证，仍存活则 connect+kill 重试至多 2 次。
                            for _attempt in range(2):
                                if not _killed:
                                    break
                                try:
                                    _info = inst.get_info() if hasattr(inst, "get_info") else None
                                    _alive = bool(_info) and (getattr(_info, "state", "") or "") not in (
                                        "", "terminated", "killed")
                                except Exception:
                                    _alive = False  # 404=已销毁（预期）
                                if not _alive:
                                    break
                                log.warning(f"[engine-probe] probe sandbox still alive after kill "
                                            f"(tpl={tid[:16]}), retry {_attempt + 1}/2")
                                _killed = bool(inst.kill())
                            if _killed:
                                log.info(f"[engine-probe] probe sandbox killed (tpl={tid[:16]})")
                            else:
                                log.warning(f"[engine-probe] probe sandbox kill FAILED (tpl={tid[:16]})")
                        except Exception as _ke:
                            log.warning(f"[engine-probe] probe sandbox kill error (tpl={tid[:16]}): {_ke}")
                        # 兜底记录 sid——批末清扫用（kill 语义成功但平台未终止的箱）
                        try:
                            _leak_watch.add(getattr(inst, "sandbox_id", "") or "")
                        except Exception as _lw_e:
                            log.debug(f"[engine-probe] leak-watch add failed: {_lw_e}")
                finally:
                    pass  # enter 失败路径由 cloud_base.__enter__ 自 _do_kill 兜底
                return engines

            # [批内 130597 退避 2026-11-10] kill 语义成功 ≠ 平台资源已回收（CubeMaster
            # destroy 是 lifecycle-manager 异步事件，秒级延迟）——串行探测批里
            # 「上一箱刚杀、配额未还、下一箱立即建」必撞 130597（.17 实证：4 模板批
            # 仅第 1 个过、其余 0s 瞬败）。对 130597 建箱异常做批内退避重试
            # （3s/8s/15s，PROBE_RESOURCE_RETRIES 可调），等资源回收窗口；
            # 仍败才计失败交给大循环（600s 退避）。
            _res_max = int(os.environ.get("PROBE_RESOURCE_RETRIES", "3"))
            _res_backoffs = [3, 8, 15]
            _engines = None
            for _res_attempt in range(_res_max + 1):
                try:
                    _engines = await _aio.to_thread(_probe_one)
                    break
                except Exception as _pe:
                    if _res_attempt >= _res_max or "130597" not in str(_pe):
                        _dt = _time_mod.monotonic() - _t0
                        log.warning(f"[engine-probe] [{_done}/{_total}] template {_tid[:24]} probe "
                                    f"failed ({_dt:.0f}s): {_pe}")
                        _engines = None
                        _probe_failed += 1
                        break
                    _bo = _res_backoffs[min(_res_attempt, len(_res_backoffs) - 1)]
                    log.warning(f"[engine-probe] [{_done}/{_total}] 130597 resource busy "
                                f"(tpl={_tid[:16]}), backoff {_bo}s, retry {_res_attempt + 1}/{_res_max}")
                    await _aio.sleep(_bo)
            if _engines is not None:
                _dt = _time_mod.monotonic() - _t0
                if _engines:
                    out.append({"template_id": _tid, "engines": _engines})
                    log.info(f"[engine-probe] [{_done}/{_total}] {pcfg.get('name', '?')} template "
                             f"{_tid[:24]}: {[e['engine'] for e in _engines]} ({_dt:.0f}s)")
                else:
                    # [2026-11-20] 探测成功但无引擎——进结果列表（engines=[]）：
                    # 上报给服务端（空 engines 不落 binding、模板照常登记）+
                    # 写负缓存 ok=true（下轮看护模式不再建箱重探）。
                    out.append({"template_id": _tid, "engines": []})
                    log.info(f"[engine-probe] [{_done}/{_total}] {pcfg.get('name', '?')} template "
                             f"{_tid[:24]}: no engines ({_dt:.0f}s)")
        # [批末清扫 2026-11-06] 本批触碰过的箱逐个验证：仍存活→connect+kill（最多 3 轮）。
        # 原因：SDK DELETE 返回成功但平台未终止（E2E 实证 07f12ca5 "killed" 日志后仍 running），
        # 泄漏箱累积吃满平台配额后全部建箱 130597。
        if _leak_watch:
            try:
                from cubesandbox.sandbox import Sandbox as _Sb, Config as _Cfg
                _cfg = _Cfg(api_url=(pcfg.get("api_url") or pcfg.get("endpoint") or ""),
                            api_key=pcfg.get("api_key", ""))
                for _round in range(3):
                    _alive = []
                    _items = _Sb.list(_cfg)
                    _items = _items if isinstance(_items, list) else _items.get("sandboxes") or []
                    _sid_state = {}
                    for _d in _items:
                        _sid_state[_d.get("sandboxID") or _d.get("sandbox_id") or ""] = _d
                    for _sid in _leak_watch:
                        if _sid and _sid in _sid_state:
                            try:
                                _Sb.connect(_sid, config=_cfg).kill()
                                _alive.append(_sid)
                            except Exception as _sk_e:
                                # connect 404=已销毁；其余（网络抖动）留痕后继续扫
                                log.debug(f"[engine-probe] sweep kill {_sid} failed: {_sk_e}")
                    if not _alive:
                        break
                    _leak_watch = set(_alive)
                    _time_mod.sleep(2)
                if _alive:
                    log.error(f"[engine-probe] batch sweep: {len(_alive)} "
                              f"probe sandbox(s) still alive after 3 sweep rounds: {sorted(_alive)}")
                else:
                    log.info("[engine-probe] batch sweep: all probe sandboxes terminated")
            except Exception as _se:
                log.warning(f"[engine-probe] batch sweep failed: {_se}")

        # [负缓存] 探测完成的模板（无论有引擎/无引擎）写缓存 ok=true——下轮直接跳过
        # （结果并入返回列表，服务端 binding 不缺）。失败模板不写（ok=false 等价不写，
        # 重试循环下轮重探）。
        try:
            os.makedirs(_cred_state_dir(), exist_ok=True)
            _new = dict(_neg_cache)
            for entry in out:
                _new[entry["template_id"]] = {
                    "engines": [e.get("engine") for e in entry.get("engines", [])],
                    "probed_at": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
                    "ok": True,
                }
            with open(_probe_state_json_path(), "w") as f:
                json.dump({"templates": _new}, f, ensure_ascii=False, indent=1)
        except Exception as _ce:
            log.warning(f"[engine-probe] negative cache write failed: {_ce}")
        # 缓存命中的模板：把已知结果并入返回（探测跳过但上报不缺）
        for t in _cached_hits:
            _tid = (t.get("template_id") or "").strip()
            _hit = _neg_cache.get(_tid, {})
            out.append({"template_id": _tid,
                        "engines": [{"engine": e, "version": "", "source": "cache"}
                                    for e in (_hit.get("engines") or [])]})
        log.info(f"[engine-probe] done: {len(out)}/{_total} templates probed OK this round, "
                 f"{_probe_failed + _deferred} pending (failed/deferred)")
        return out, _probe_failed + _deferred

    from src.domains.runtime.engine.client import RuntimeClient

    async def _amain() -> int:
        """返回退出码供 main() 传给 sys.exit()。"""
        client = RuntimeClient(config["server_url"], worker_key)
        from src.domains.runtime.engine import os_adapter as _os_adapter
        device = config["device_name"] or _os_adapter.hostname()

        # 查模板列表（GET /api/settings/sandbox-templates）
        try:
            templates = await client.get_templates()
        except Exception as e:
            log.error(f"Failed to fetch sandbox-templates on startup: {e}")
            return 4  # register_failed——服务端不可达视为注册失败

        # [对接优先 2026-10-30 用户裁定] 空模板/空引擎不再拒启——安装成功的语义
        # = 与主控连通+身份注册成功; 引擎 0 个是正常中间态(用户在主控平台详情页
        # 绑定引擎后, refresh 循环 60s 内增量注册——'runtime X added to heartbeat
        # list' 链路已多次验证)。原 exit_code=2/3 把配置步骤顺序错当安装失败,
        # 把用户堵死在重装循环(.17 连续三凭据 9/10 实证)。
        runtimes = _expand_templates(templates or [])
        if not runtimes:
            log.info("No engines yet (no templates / no engine bindings) — started with empty engine list. "
                     "Bind engines on the grape WebUI (platform detail page); they will register within ~60s "
                     "via the template refresh loop.")

        ids, probe_url = await client.register(
            runtimes, device_name=device,
            cli_version="cloud-worker-1.0",
            provider="",
        )
        if not ids:
            # [对接优先] 空引擎列表的 register 返回空 ids 也放行(引擎列表本来就空,
            # 服务端没有 rid 可分配)——服务连通即对接成功
            if not runtimes:
                log.info("Registered with empty engine list (no engines bound yet)")
                ids = []
            else:
                log.error("Register failed (check key file / server URL) (exit_code=4)")
                return 4

        # _expand_templates 已按 cubesandbox 优先排序，ids 顺序与 runtimes 一致

        # 初始化 _refresh_templates 的缓存——避免首次心跳刷新全量重复注册
        _refresh_templates._last_runtimes = runtimes
        _refresh_templates._last_hash = hashlib.md5(
            json.dumps(sorted(runtimes, key=lambda r: r.get("name", "")), sort_keys=True).encode()
        ).hexdigest()

        # rid → 引擎实例名（PR #2066 复审 Required：按回包 name 配对，避免部分非法 id 时
        # ids 比 runtimes 短导致 zip 错位；与 _refresh_templates / agent 启动路径同口径）
        engine_by_rid = _startup_engine_by_rid(client, ids, runtimes, device)
        log.info(f"cloud_worker registered: {ids} -> {config['server_url']}, "
                 f"engines={list(engine_by_rid.values())}, "
                 f"provider={config['provider']}, "
                 f"poll={config['poll_interval']}s heartbeat={config['heartbeat_interval']}s")

        for rid in ids:
            await client.recover_orphans(rid)
        # [探测重试 2026-11-04] 上报+探测提为可重入函数：启动注册后跑一次；
        # 有模板探测失败（130597 资源不足等）→ _template_refresh_loop 每轮 60s 重跑，
        # 直到全成功——修复"启动探测失败后 10min 健康线程不覆盖模板探测、binding
        # 永缺"缺口（.17 实例 22 实证：全天仅启动一次探测批）。返回失败计数。
        async def _report_providers_once() -> int:
            _failed = 0
            _prov_cfgs = {}
            try:
                _prov_cfgs = await client.get_provider_configs()
            except Exception as _pe:
                log.warning(f"provider configs fetch for report failed: {_pe}")
                return 1
            for _pcfg in (_prov_cfgs or {}).values():
                # normalize_db_config 后字段名为 api_url（不是 endpoint）——实测 .17 worker
                # 因读错字段静默跳过扫描（无日志无上报），此处两个键名都兜住
                _p_endpoint = (_pcfg.get("api_url") or _pcfg.get("endpoint") or "").strip()
                if not _p_endpoint:
                    # [2026-11-06 修复] 未配 endpoint 计入 pending——此前 continue 且 _failed 不加，
                    # endpoint 在 worker 启动后才配置时探测批永不重跑（E2E 实证：重配 endpoint
                    # 后 binding 永缺，需重启 worker 才恢复）
                    _failed += 1
                    continue
                _tpl_list = []
                _adapter = None
                try:
                    from src.domains.runtime.agents.sandbox.backends import get_adapter
                    _adapter = get_adapter(_pcfg.get("platform", ""))
                    if _adapter and hasattr(_adapter, "list_templates"):
                        _tpl_scan = _adapter.list_templates({
                            "api_url": _p_endpoint,
                            "api_key": _pcfg.get("api_key", ""),
                            **({"gateway_ip": (_pcfg.get("extra_config") or {}).get("gateway_ip")} if (_pcfg.get("extra_config") or {}).get("gateway_ip") else {}),
                            **({"gateway_url": (_pcfg.get("extra_config") or {}).get("gateway_url")} if (_pcfg.get("extra_config") or {}).get("gateway_url") else {}),
                        })
                        if _tpl_scan is None:
                            # [510622678 语义] SDK 未装 → 平台不支持扫描 → 跳过该 Provider
                            # （而非上报空模板——空列表会被当"平台无模板"登记）
                            log.info(f"platform '{_pcfg.get('platform')}' SDK not installed, skip template scan")
                            continue
                        _tpl_list = _tpl_scan
                except Exception as _te:
                    log.warning(f"platform template scan for report failed: {_te}")
                    # [平台不可达 2026-11-10 用户裁定] 扫描失败必须计入 pending——
                    # 否则重试循环退出后平台恢复也永不再扫（binding 永缺到 worker 重启）。
                    # 模板列表为空时探测批自然不跑（无箱可建）——「连不上就不探测」
                    # 语义由此达成，只保留轻量 list_templates 重试。
                    _failed += 1
                # [引擎探测 spec] 建箱层全量探测：每模板建箱 → 白名单 --version → kill（即建即杀）
                # 前提: adapter 声明 probe_engines 能力（MANIFEST 双向断言保证实现存在）
                # [对账改精确计数 2026-11-20 用户裁定] 探测函数返回 (结果, 失败计数)：
                # 失败=建箱异常+截断未探；无引擎模板探完即成功（engines=[] 进结果并
                # 写负缓存 ok=true）——不再用"扫描数-有引擎数"差值对账（真无引擎模板
                # 永远差 1 → 重试循环永不停 → 每 1800s 白探一轮建箱）。
                _tpl_engines: list = []
                if _adapter and "probe_engines" in getattr(_adapter, "MANIFEST", {}).get("capabilities", []):
                    _tpl_engines, _probe_failed_cnt = await _probe_templates_engines(
                        _adapter, _pcfg, _tpl_list, device)
                    _failed += _probe_failed_cnt
                try:
                    _rep = await client.sandbox_provider_report(
                        platform=_pcfg.get("platform", ""),
                        device_name=device,
                        templates=[{"template_id": t.get("template_id", ""),
                                    "alias": t.get("name", ""),
                                    "description": t.get("description", "")}
                                   for t in _tpl_list if t.get("template_id")],
                        template_engines=_tpl_engines,
                    )
                    # The platform answers {"status": "ok"} on success. Judging by
                    # "non-empty dict" alone would count a degraded 200 body as a
                    # stored report; a missing status keeps the old meaning.
                    if _rep and _rep.get("status", "ok") == "ok":
                        log.info(f"sandbox provider reported: provider_id={_rep.get('provider_id', '')} "
                                 f"templates={_rep.get('templates_upserted', 0)} "
                                 f"engines_detected={_rep.get('engines_detected', 0)}")
                    else:
                        # The client swallows 5xx/timeout into an empty dict, so this used
                        # to log "reported: templates=0" as if it had worked while the
                        # platform stored nothing and no retry was scheduled. Count an
                        # empty response as pending so _probe_retry_loop re-reports with
                        # backoff until the data really lands.
                        log.warning("sandbox provider report did not succeed (empty or "
                                    "non-ok response), counted as pending: "
                                    f"templates={len(_tpl_list)} engines={len(_tpl_engines)}")
                        _failed += 1
                except Exception as _re:
                    log.warning(f"sandbox provider report failed: {_re}")
                    _failed += 1
            return _failed

        # 启动先跑一轮（注册后，不阻塞注册——本函数含建箱探测，耗时长）
        _probe_fail_count = await _report_providers_once()

        # [探测重试 2026-11-04] 失败重试任务：上轮有模板探测失败（130597 资源不足等）
        # → 每 120s 重跑（指数退避），全成功转入看护模式。幂等性：detected
        # binding 三元组 UPSERT + 模板登记幂等——重跑无副作用。修复"启动探测失败后
        # binding 永缺"缺口（.17 实例 22 实证）。
        # [退避加大 2026-11-10 用户裁定] 封顶 600s → 1800s（PROBE_RETRY_MAX_BACKOFF 可调），
        # 且连续 3 轮无进展（pending 不降——平台资源持续不足/不可达）直接跳到封顶，
        # 消掉对平台每 10 分钟一轮的无效冲击；有进展（部分模板探成）时保持指数节奏，
        # 短间隔快速收敛。
        # [新模板看护 2026-11-20 用户裁定] 全成功后循环不再退出——转入看护模式，由
        # 60s _template_refresh_loop 顺路做轻量 list_templates 扫描，发现平台侧模板
        # id 集合变化 → set config["probe_watch_trigger"] 事件唤醒本循环重跑
        # _report_providers_once（负缓存挡住已探模板，只建箱探测新增 id）。事件等待
        # 带 PROBE_WATCH_INTERVAL（默认 600s）超时兜底——refresh 循环扫描自身失败
        # （平台不可达等）不丢发现机会。修复「重试循环退出后新模板永不被自动发现、
        # 只能重启 worker」缺口（此前唯一的自动发现机制是'真无引擎模板永远差 1'的
        # 病态重试，本批已消）。
        _probe_max_backoff = int(os.environ.get("PROBE_RETRY_MAX_BACKOFF", "1800"))
        _probe_watch_interval = int(os.environ.get("PROBE_WATCH_INTERVAL", "600"))
        _watch_evt: asyncio.Event = config.setdefault("probe_watch_trigger", asyncio.Event())
        async def _probe_retry_loop():
            _backoff = 120
            _pending = _probe_fail_count
            _last_pending = None
            _stall_rounds = 0
            while True:
                if _pending:
                    await asyncio.sleep(_backoff)
                else:
                    # 看护模式：等 refresh 循环的模板变更事件（60s 扫描发现新模板即刻
                    # 触发）；超时兜底重跑一轮，防 refresh 扫描链路自身故障漏检。
                    try:
                        await asyncio.wait_for(_watch_evt.wait(), timeout=_probe_watch_interval)
                    except asyncio.TimeoutError as _to_e:
                        # 看护超时属正常节拍（refresh 未发现新模板）：留痕而非静默
                        log.debug(f"[engine-probe] watch wait timed out: {_to_e}")
                    _watch_evt.clear()
                _pending = await _report_providers_once()
                if _pending:
                    if _last_pending is not None and _pending >= _last_pending:
                        _stall_rounds += 1
                    else:
                        _stall_rounds = 0
                    _last_pending = _pending
                    if _stall_rounds >= 3:
                        _backoff = _probe_max_backoff
                    else:
                        _backoff = min(_backoff * 2, _probe_max_backoff)
                    # _pending counts both unprobed templates and whole-report
                    # failures, so the log must not call every unit a template.
                    log.info(f"[engine-probe] retry scheduled: {_pending} pending item(s) "
                             f"(unprobed templates or failed reports), next retry in {_backoff}s"
                             + (f" (stalled {_stall_rounds} rounds, max backoff)" if _stall_rounds >= 3 else ""))
                else:
                    _backoff = 120
                    _stall_rounds = 0
                    _last_pending = None
                    log.info(f"[engine-probe] all pending cleared; watching for new "
                             f"templates via refresh-loop scan (event-triggered, "
                             f"{_probe_watch_interval}s timeout fallback)")

        asyncio.create_task(_probe_retry_loop())

        # 拉取 provider 配置 + 注入到 sandbox_manager
        provider_configs = await _fetch_provider_configs(client)
        try:
            from src.domains.runtime.agents.sandbox import sandbox_manager as _sm
            _sm.set_provider_configs(provider_configs)
        except Exception as e:
            log.warning(f"Failed to inject provider configs to sandbox_manager: {e}")

        # [spec §6.2] 启动时即写 whoami.json——首装后身份认知立即可见（cat 排查用）
        try:
            await _whoami_follow_and_guard(client)
        except RuntimeError as _ge:
            log.error(f"startup guard failed: {_ge}")
            import sys as _sys
            _sys.exit(1)
        except Exception as _we:
            log.warning(f"startup whoami follow failed: {_we}")

        # 心跳不报 probe_results（本机无引擎）
        await _cloud_run_loop(client, ids, config, engine_by_rid, runtimes, device)
        return 0

    _exit_code = asyncio.run(_amain())
    # BUG-10 修复：退出时清理 DNS override（进程级 monkey-patch 恢复）
    try:
        from src.kernel.devbox_dns import cleanup_dns_override
        cleanup_dns_override()
    except Exception as _e:
        log.warning(f"DNS override cleanup failed: {_e}")
    if _exit_code != 0:
        sys.exit(_exit_code)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    main()
