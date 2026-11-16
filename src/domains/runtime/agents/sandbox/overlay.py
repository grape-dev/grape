"""Per-task HOME overlay 构建——引擎无关的中性模块。

从 hermes_acp.py 迁移，避免 runtime.hermes → agent.hermes_acp → agent.sandbox.backends.bwrap 依赖链。
hermes.py prepare_task_env 和 hermes_acp.py stream_execute 都从这里 import。
"""
import os
import shutil
import logging

log = logging.getLogger("devops-bot")


def build_overlay_home(iso_home: str, src_home: str):
    """构建 per-task HOME overlay——symlink 共享内容 + 可写目录 + 覆写配置。

    - symlink 镜像共享 home 的所有内容（skills/plugins/auth 等）
    - 覆写 config.yaml（tool_progress=new）
    - 覆写 .env（pin HERMES_HOME + 保留凭据）
    - state.db 保持 task-local（不 symlink，每个 task 独立）
    """
    try:
        if os.path.isdir(src_home):
            for entry in os.listdir(src_home):
                dst = os.path.join(iso_home, entry)
                _SKIP = ("state.db", "state.db-wal", "state.db-shm",
                         "config.yaml", ".env", "active_profile", "profiles")
                _WRITABLE = ("logs", "cache", "sessions", "audio_cache",
                              "image_cache", "sandboxes", "pastes",
                              "processes.json", "pairing", "cron",
                              "backups")
                if entry in _SKIP:
                    continue
                if entry in _WRITABLE:
                    if not os.path.exists(dst):
                        try:
                            os.makedirs(dst, exist_ok=True) if entry != "processes.json" else open(dst, "w").close()
                        except Exception:
                            pass
                    continue
                src = os.path.join(src_home, entry)
                if not os.path.isdir(src):
                    continue
                if os.path.exists(dst):
                    continue
                try:
                    os.symlink(src, dst)
                except Exception:
                    pass

        src_cfg = os.path.join(src_home, "config.yaml")
        dst_cfg = os.path.join(iso_home, "config.yaml")
        if os.path.isfile(src_cfg):
            try:
                import re
                with open(src_cfg, "r", encoding="utf-8") as f:
                    cfg_text = f.read()
                cfg_text = re.sub(r'(tool_progress:\s*)\w+', r'\g<1>new', cfg_text)
                with open(dst_cfg, "w", encoding="utf-8") as f:
                    f.write(cfg_text)
            except Exception:
                shutil.copy2(src_cfg, dst_cfg)

        src_env = os.path.join(src_home, ".env")
        dst_envf = os.path.join(iso_home, ".env")
        env_body = ""
        if os.path.isfile(src_env):
            try:
                with open(src_env, "r", encoding="utf-8") as f:
                    for line in f:
                        stripped = line.strip()
                        if stripped.startswith("HERMES_HOME=") or stripped.startswith("export HERMES_HOME="):
                            continue
                        env_body += line
            except Exception:
                pass
        if env_body and not env_body.endswith("\n"):
            env_body += "\n"
        env_body += f"HERMES_HOME='{iso_home}'\n"
        try:
            with open(dst_envf, "w", encoding="utf-8") as f:
                f.write(env_body)
            os.chmod(dst_envf, 0o600)
        except Exception:
            pass

    except Exception as e:
        log.warning(f"overlay build error: {e}")


def cleanup_overlay(iso_home: str):
    """清理 per-task HOME overlay（保留 state.db 供可能的续接，只删目录）。

    从 hermes_acp.py 迁移。当前实际清理由 TaskResourceStack 统一管理（keep_overlays=True），
    此函数保留供旧调用路径兼容。
    """
    try:
        shutil.rmtree(iso_home, ignore_errors=True)
    except Exception:
        pass
