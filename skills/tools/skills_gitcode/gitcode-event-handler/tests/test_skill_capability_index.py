"""方案A: skill 能力索引 + LLM 判重候选带能力摘要测试(2026-08-17)。

背景: 原 LLM 判重候选侧只喂"名字+路径"(猜名字), 方案A 预构建能力索引
(读每个 SKILL.md 的 description+核心命令提炼 summary), 判重时候选带
真实能力描述。索引构建失败 fail-open 回退纯名字。
"""
import importlib.util
from unittest import mock

HANDLER_PATH = "/root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py"


def _load_handler():
    spec = importlib.util.spec_from_file_location("geh", HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===== _fetch_skill_md_summary: description + 核心命令提炼 =====
def test_fetch_summary_desc_and_cmd():
    mod = _load_handler()
    md = ("---\nname: huawei-cloud-obs-total-size\ndescription: 查询OBS桶总容量\n---\n"
          "# 概述\n测试\n# 核心命令\nhcloud obs stat --bucket xxx\nhcloud obs list\n")
    d = {"content": __import__("base64").b64encode(md.encode()).decode()}
    with mock.patch.object(mod, "gitcode_get", return_value=d):
        summary, desc = mod._fetch_skill_md_summary("r/r", "skills/storage/obs/huawei-cloud-obs-total-size")
    assert "查询OBS桶总容量" in summary, f"summary 应含 description, 实际 {summary}"
    assert "hcloud obs stat" in summary, f"summary 应含核心命令, 实际 {summary}"
    assert desc == "查询OBS桶总容量"


# ===== _fetch_skill_md_summary: SKILL.md 缺失返回 None =====
def test_fetch_summary_missing_none():
    mod = _load_handler()
    with mock.patch.object(mod, "gitcode_get", return_value={}):
        assert mod._fetch_skill_md_summary("r/r", "skills/x/y") is None


# ===== _skill_capability_index: 构建索引并缓存 =====
def test_capability_index_build_and_cache():
    mod = _load_handler()
    products = {"huawei-cloud-obs-total-size": "skills/storage/obs/huawei-cloud-obs-total-size",
                "huawei-cloud-vpc-list": "skills/network/ops/huawei-cloud-vpc-list"}
    md_tpl = ("---\nname: {n}\ndescription: {d}\n---\n# 概述\n测试\n")
    def fake_get(url):
        if "SKILL.md" in url:
            name = "huawei-cloud-obs-total-size" if "obs-total" in url else "huawei-cloud-vpc-list"
            desc = "查询OBS桶总容量" if "obs-total" in url else "查询VPC列表"
            md = md_tpl.format(n=name, d=desc)
            return {"content": __import__("base64").b64encode(md.encode()).decode()}
        return []
    with mock.patch.object(mod, "_get_repo_products", return_value=products), \
         mock.patch.object(mod, "gitcode_get", side_effect=fake_get):
        idx = mod._skill_capability_index("r/r", build_on_miss=True)
    assert "huawei-cloud-obs-total-size" in idx
    assert "查询OBS桶总容量" in idx["huawei-cloud-obs-total-size"]["summary"]
    # 缓存命中(不重新构建)
    with mock.patch.object(mod, "_get_repo_products",
                           side_effect=AssertionError("缓存命中不应重建")):
        idx2 = mod._skill_capability_index("r/r")
    assert idx2 == idx


# ===== 索引失败 -> fail-open 回退 {} =====
def test_capability_index_fail_fallback():
    mod = _load_handler()
    with mock.patch.object(mod, "_get_repo_products", side_effect=RuntimeError("429")):
        assert mod._skill_capability_index("no-such-repo-xyz") == {}


# ===== 2026-08-17 性能改造: 缓存未命中不构建(降级), build_on_miss=False =====
def test_capability_index_miss_no_build():
    mod = _load_handler()
    # 无缓存且不构建 -> 直接返回 {}, 不触发构建
    with mock.patch.object(mod, "_load_json_cache", return_value={}), \
         mock.patch.object(mod, "_build_skill_capability_index",
                           side_effect=AssertionError("miss 不应构建")):
        assert mod._skill_capability_index("fresh-repo-abc") == {}


# ===== build_on_miss=True 时才同步构建 =====
def test_capability_index_build_on_miss():
    mod = _load_handler()
    fake_idx = {"huawei-cloud-evs-list": {"path": "p", "summary": "s", "name": "n"}}
    with mock.patch.object(mod, "_load_json_cache", return_value={}), \
         mock.patch.object(mod, "_build_skill_capability_index",
                           return_value=fake_idx) as m_build:
        idx = mod._skill_capability_index("fresh-repo-abc", build_on_miss=True)
    m_build.assert_called_once()
    assert idx == fake_idx


# ===== LLM 判重候选带能力摘要(方案A 生效) =====
def test_llm_candidates_include_summary():
    mod = _load_handler()
    products = {"huawei-cloud-obs-total-size": "skills/storage/obs/huawei-cloud-obs-total-size"}
    idx = {"huawei-cloud-obs-total-size": {
        "path": "skills/storage/obs/huawei-cloud-obs-total-size",
        "summary": "查询OBS桶总容量 | hcloud obs stat", "name": "huawei-cloud-obs-total-size",
        "description": "查询OBS桶总容量"}}
    captured = {}
    def fake_post(method, url, **kw):
        captured["prompt"] = kw["body"]["messages"][0]["content"]
        return {"choices": [{"message": {"content": '{"covered": false, "skill_name": "", "reason": "x"}'}}]}
    with mock.patch.object(mod, "_get_repo_products", return_value=products), \
         mock.patch.object(mod, "_skill_capability_index", return_value=idx), \
         mock.patch.object(mod, "http_json", side_effect=fake_post):
        mod._llm_skills_covers_issue("r/r", "创建一个skill查询OBS桶总容量", "")
    assert "查询OBS桶总容量" in captured["prompt"], \
        f"候选应带能力摘要, 实际 prompt: {captured['prompt'][:200]}"
    assert "hcloud obs stat" in captured["prompt"]


# ===== 索引为空 -> 回退纯名字(旧行为) =====
def test_llm_candidates_fallback_no_index():
    mod = _load_handler()
    products = {"huawei-cloud-obs-total-size": "skills/storage/obs/huawei-cloud-obs-total-size"}
    captured = {}
    def fake_post(method, url, **kw):
        captured["prompt"] = kw["body"]["messages"][0]["content"]
        return {"choices": [{"message": {"content": '{"covered": false, "skill_name": "", "reason": "x"}'}}]}
    with mock.patch.object(mod, "_get_repo_products", return_value=products), \
         mock.patch.object(mod, "_skill_capability_index", return_value={}), \
         mock.patch.object(mod, "http_json", side_effect=fake_post):
        mod._llm_skills_covers_issue("r/r", "创建skill查询OBS桶容量", "")
    assert "huawei-cloud-obs-total-size (skills/storage/obs/huawei-cloud-obs-total-size)" in \
        captured["prompt"], f"索引空应回退名字+路径, 实际: {captured['prompt'][:200]}"
