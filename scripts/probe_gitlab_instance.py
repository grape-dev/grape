#!/usr/bin/env python3
"""自建 GitLab 实例接入自检（Issue #3580）。

用途：把一个 `gitlab-corp` 一类的实例卡接进来之后，用**一条命令**确认六件事：
  1) 凭据可用（`GET /user`）
  2) 能列仓（`GET /projects?membership=true`）
  3) 子组 namespace 可达（`GET /projects/{owner%2Frepo}/repository/tree`）
  4) 文件可读（`GET .../repository/files/{path}`，内容 base64）
  5) 建钩请求形态正确（dry-run：只构造 URL/body，不发上游）
  6) 卡上的私有 CA 能加载（`ca_bundle`）

跑法：
    python scripts/probe_gitlab_instance.py gitlab-corp \
        --token <PAT> --repo grp/sub/proj --path README.md
    # 无 --token 时读环境变量 GRAPE_PROBE_TOKEN；缺 token 的项记为 SKIP 而不是 FAIL
    python scripts/probe_gitlab_instance.py gitlab-corp --self-test   # 内置假上游，离线自测

上游 HTTP 一律走 `src.kernel.upstream_http`（卡 `ca_bundle` + GitLab 方言族不跟随重定向），
与产品链路同一个入口——探针绿＝产品链路该段也绿。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def _card(provider: str):
    from src.domains.runtime.vcs.platform_card import get_platform_card

    card = get_platform_card(provider)
    if card is None:
        raise SystemExit(f"平台卡不存在或未接入: {provider}（先用 PUT /api/admin/vcs-platforms 写入）")
    return card


def _project_seg(owner: str, repo: str) -> str:
    return "/projects/" + urllib.parse.quote(f"{owner}/{repo}", safe="")


def real_fetch(card, path: str, token: str, params: dict | None = None):
    """真实上游请求：返回 (status, payload_or_text)。"""
    from src.kernel.upstream_http import open_with_policy

    url = f"{card.api_base.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(url, method="GET",
                                 headers={"PRIVATE-TOKEN": token or "", "Accept": "application/json"})
    try:
        with open_with_policy(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body) if body.strip() else {}
            except Exception:
                return resp.status, body[:400]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:400]
    except Exception as e:  # noqa: BLE001  连接/TLS 失败也要给结论，不能抛栈
        return 0, f"{type(e).__name__}: {e}"


def stub_fetch(card, path: str, token: str, params: dict | None = None):
    """离线自测用假上游（--self-test）：只覆盖探针会打的几个端点。"""
    if path == "/user":
        return 200, {"username": "probe-bot", "id": 7}
    if path == "/projects":
        return 200, [{"id": 1, "path_with_namespace": "grp/sub/proj",
                      "default_branch": "main"}]
    if path.endswith("/repository/tree"):
        return 200, [{"id": "h1", "name": "README.md", "type": "blob",
                      "path": "README.md", "mode": "100644", "size": 5}]
    if "/repository/files/" in path:
        return 200, {"file_name": "README.md", "file_path": "README.md", "size": 5,
                     "encoding": "base64", "content": "aGVsbG8=", "last_commit_id": "c0ffee"}
    return 404, "stub: unexpected path"


def _r(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def probe(card, token: str, owner: str, repo: str, file_path: str, fetch) -> list[dict]:
    out: list[dict] = []

    # 6) CA 先查：CA 坏掉后面全 FAIL 且原因难辨
    ca_status, ca_detail = check_ca(card)
    out.append(_r("ca", ca_status, ca_detail))

    if not token:
        for name in ("token", "repos", "tree", "file", "hook"):
            out.append(_r(name, SKIP, "缺少 token（--token 或 GRAPE_PROBE_TOKEN）"))
        return out

    st, payload = fetch(card, "/user", token)
    if st == 200 and isinstance(payload, dict) and payload.get("username"):
        out.append(_r("token", PASS, f"user={payload['username']} id={payload.get('id')}"))
    else:
        out.append(_r("token", FAIL, f"HTTP {st}: {payload}"))

    st, payload = fetch(card, "/projects", token, {"membership": "true", "per_page": 100})
    _default_branch = ""
    _empty_repo = False
    if st == 200 and isinstance(payload, list):
        out.append(_r("repos", PASS, f"可见仓库数>=1（首个: {payload[0].get('path_with_namespace')}）"
                      if payload else "可见仓库 0 个（token 可能无 membership）"))
        for p in payload:
            if str(p.get("path_with_namespace") or "") == f"{owner}/{repo}":
                _default_branch = str(p.get("default_branch") or "")
                _empty_repo = bool(p.get("empty_repo"))
                break
        if not _default_branch and payload:
            _default_branch = str(payload[0].get("default_branch") or "")
    else:
        out.append(_r("repos", FAIL, f"HTTP {st}: {payload}"))

    owner_repo = f"{owner}/{repo}" if owner and repo else ""
    _ref = _default_branch or "main"      # GitLab 不接受 ref=HEAD（实测 404）
    if not owner_repo:
        out.append(_r("tree", SKIP, "未给 --repo"))
        out.append(_r("file", SKIP, "未给 --repo"))
    elif _empty_repo:
        # 空仓库（无任何提交）上游对 tree/files 一律 404，是正确语义，不是接入故障
        out.append(_r("tree", SKIP, f"{owner_repo} 是空仓库（无提交）；先推一次提交再测"))
        out.append(_r("file", SKIP, f"{owner_repo} 是空仓库（无提交）；先推一次提交再测"))
    else:
        st, payload = fetch(card, _project_seg(owner, repo) + "/repository/tree",
                            token, {"ref": _ref, "per_page": 20})
        if st == 200 and isinstance(payload, list):
            names = [str(e.get("name")) for e in payload if isinstance(e, dict)][:5]
            out.append(_r("tree", PASS, f"{owner_repo} 根目录 {len(payload)} 项: {names}"))
        else:
            out.append(_r("tree", FAIL, f"HTTP {st}: {payload}"))

        fp = file_path or "README.md"
        st, payload = fetch(card, _project_seg(owner, repo) + "/repository/files/"
                            + urllib.parse.quote(fp, safe=""), token, {"ref": _ref})
        if st == 200 and isinstance(payload, dict) and payload.get("content"):
            out.append(_r("file", PASS, f"{fp} 读取成功（{payload.get('size')} bytes, base64）"))
        else:
            out.append(_r("file", FAIL, f"HTTP {st}: {payload}"))

    # 5) 建钩 dry-run：只验证 URL/body 形态，不发上游（避免在实例上留下钩子）
    try:
        from src.kernel.vcs_gitlab_hooks import _hooks_url

        hook_url = _hooks_url(card.api_base, owner or "grp", repo or "proj")
        ok = hook_url.endswith("/hooks") and "%2F" in hook_url
        out.append(_r("hook", PASS if ok else FAIL,
                      f"dry-run POST {hook_url} body={{url, token, *_events}}（未发送）"))
    except Exception as e:  # noqa: BLE001
        out.append(_r("hook", FAIL, f"构造失败: {e}"))
    return out


def check_ca(card) -> "tuple[str, str]":
    ca = str(getattr(card, "ca_bundle", "") or "")
    if not ca:
        return PASS, "无私有 CA（用系统 CA）"
    from src.kernel.upstream_http import ssl_context

    ctx = ssl_context(ca)
    if ctx is None:
        return FAIL, f"ca_bundle 加载失败: {ca}"
    return PASS, f"ca_bundle 已加载: {ca}"


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="自建 GitLab 实例接入自检（Issue #3580）")
    ap.add_argument("provider", nargs="?", default="gitlab", help="平台卡 provider（如 gitlab-corp）")
    ap.add_argument("--token", default=os.environ.get("GRAPE_PROBE_TOKEN", ""),
                    help="PAT（也可用环境变量 GRAPE_PROBE_TOKEN）")
    ap.add_argument("--repo", default="", help="目标仓库 owner/repo（可含子组，如 grp/sub/proj）")
    ap.add_argument("--path", default="README.md", help="文件抽查路径")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--self-test", action="store_true", help="内置假上游离线自测")
    args = ap.parse_args(argv)

    card = _card(args.provider)
    fetch = stub_fetch if args.self_test else real_fetch
    owner, _, repo = (args.repo or "").partition("/")
    results = probe(card, args.token or ("stub-token" if args.self_test else ""),
                    owner, repo, args.path, fetch)

    if args.json:
        print(json.dumps({"provider": args.provider, "checks": results}, ensure_ascii=False, indent=2))
    else:
        print(f"provider = {args.provider}  ({card.api_base})")
        for r in results:
            print(f"  [{r['status']:4s}] {r['check']:6s} {r['detail']}")
        bad = [r for r in results if r["status"] == FAIL]
        print("结论:", "全部通过" if not bad else f"{len(bad)} 项失败 → 按上面 detail 修卡/凭据")
    return 0 if not any(r["status"] == FAIL for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())