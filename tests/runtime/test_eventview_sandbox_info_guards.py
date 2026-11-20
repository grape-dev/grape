"""[spec 2026-11-14-eventview-cred-uid8-fix] 前端源码守卫（防功能丢失/回退）。

守卫 EventView.vue 沙箱引擎信息位（grape commit cbb62e7bb）：
  - 必须消费 credential_uid8（条件 + 展示，≥2 处）；
  - title 线索必须是真实服务名形态 sandbox-worker-<uid8>；
  - 语义红线：不得回退到 info.credential_id 旧形态（id 不是服务名锚，
    展示 id 会给运维一条 systemctl 查不到的断链线索——本次修正的本体）。

dist（static-dist/viewjs/）是 gitignore 的本地产物，守卫锚在 .vue 源上。
"""
import os

_REPO = os.path.join(os.path.dirname(__file__), "..", "..")
_VIEW = os.path.join(_REPO, "src/api/frontend/app/src/views/EventView.vue")


def _src() -> str:
    with open(_VIEW, encoding="utf-8") as f:
        return f.read()


def test_eventview_consumes_credential_uid8():
    s = _src()
    n = s.count("credential_uid8")
    assert n >= 2, (
        "EventView.vue 必须消费 info.credential_uid8（条件判断 + esc 展示），"
        f"当前 {n} 处——沙箱引擎信息位功能疑似丢失"
    )


def test_eventview_keeps_systemctl_service_name_hint():
    s = _src()
    assert "sandbox-worker-" in s, (
        "EventView.vue 应保留 systemctl 服务名线索（真实形态 sandbox-worker-<uid8>）"
    )


def test_eventview_no_credential_id_fallback():
    s = _src()
    bad = "info.credential_id"
    assert bad not in s, (
        "EventView.vue 不得回退到 info.credential_id（spec 2026-11-14 拍板①："
        "credential_id 不下发；展示 id 是断链的运维线索——旧形态复活即回归"
    )
