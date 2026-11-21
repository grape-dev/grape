"""沙箱 clone 的平台归属 S1–S4（评审①）。

事故：派发侧 env_extra 的 REPO_FULL_NAME 是剥前缀裸名，cloud_worker 先写的带前缀值
被 env.update(env_extra) 覆盖；sandbox 用裸名拼 URL，parse_repo_key 默认 provider=gitcode
→ gitee 仓被 clone 到 gitcode.com。
口径：平台只认带前缀的 REPO_KEY；REPO_FULL_NAME 继续是裸名（runbook 拼 URL 用）。
"""
from __future__ import annotations

import pathlib

from src.domains.runtime.agents.sandbox.sandbox_context import build_context


def _url(repo_key: str) -> str:
    ctx = build_context({"repo_key": repo_key,
                         "env_extra": {"REPO_KEY": repo_key,
                                       "REPO_FULL_NAME": repo_key.split(":", 1)[1]}})
    return getattr(ctx, "repo_url", "") or (ctx.get("repo_url") if isinstance(ctx, dict) else "")


def test_s1_gitee_clones_from_gitee():
    assert _url("gitee:acme/widget") == "https://gitee.com/acme/widget.git"


def test_s2_github_clones_from_github():
    assert _url("github:acme/widget") == "https://github.com/acme/widget.git"


def test_s3_gitlab_clones_from_gitlab():
    assert _url("gitlab:acme/widget") == "https://gitlab.com/acme/widget.git"


def test_s4_even_with_bare_full_name_the_prefixed_key_wins():
    """回归点：REPO_FULL_NAME 存在且是裸名时，平台仍取 REPO_KEY。"""
    ctx = build_context({"repo_key": "gitee:acme/widget",
                         "env_extra": {"REPO_FULL_NAME": "acme/widget"}})
    url = getattr(ctx, "repo_url", "") or ctx.get("repo_url", "")
    assert url == "https://gitee.com/acme/widget.git"


def test_s5_dispatch_writes_the_prefixed_key():
    """派发侧两处 env_extra 都必须带 REPO_KEY（否则 sandbox 只能拿到裸名）。"""
    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src/domains/runtime/pipeline/event_pipeline.py").read_text(encoding="utf-8")
    assert src.count('"REPO_KEY": repo_key') >= 2


def test_s6_cloud_executor_prefers_the_prefixed_key():
    """评审 T1：云沙箱 executor 曾用裸名派生平台（gitee 仓 clone 到 gitcode.com）。

    行为口径与 sandbox_context 一致：REPO_KEY 优先，REPO_FULL_NAME 只是兜底。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[2]
           / "src/domains/runtime/agents/executor.py").read_text(encoding="utf-8")
    assert 'proc_env.get("REPO_KEY") or proc_env.get("REPO_FULL_NAME"' in src
