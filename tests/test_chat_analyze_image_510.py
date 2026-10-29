"""Issue #510 回归测试：GET /api/chat/analyze-image 缺 url 参数应返回 400 统一包络。

根因：analyze_image 因 OBS 签名 URL 含未编码 & 而用 Request 手动解析原始 query string；
缺参（raw_qs 不以 url= 开头）时直接 return {"error": "missing url parameter"}，FastAPI
默认 200，错误以 200 送达，掩盖调用方失败感知（调用方按 HTTP 状态码判定成败）。

期望行为（本项目 HTTPException 统一包络风格，参照 chat.py L173/L201、auth.py）：
  - 不带 url → 400，body = {"detail": {"code": 400, "message": "missing url parameter"}}
  - 带 url= 的正常路径零回归（含 OBS 签名 URL 未编码 & 场景，手动解析逻辑不变）

覆盖：
  1. analyze_image_missing_url：无 url 参数 → 400 + missing url 语义（原子用例名登记口径）
  2. 带 url= 的正常路径：OBS 风格未编码 & 的 URL 完整保留（域名白名单分支仍以 200 承载，
     属于 #508 错误码治理簇，本 Issue 不扩散）
"""
from fastapi.testclient import TestClient

from src.api.main import app


class TestAnalyzeImageMissingUrl:
    def test_analyze_image_missing_url(self):
        """不带 url 参数 → 400（非 200），body 为统一包络且可读出 missing url 语义。"""
        c = TestClient(app)
        r = c.get("/api/chat/analyze-image")
        assert r.status_code == 400, r.text
        body = r.json()
        assert body.get("detail", {}).get("code") == 400
        assert body.get("detail", {}).get("message") == "missing url parameter"

    def test_url_param_obs_signature_url_still_parsed(self):
        """带 url= 的正常路径零回归：OBS 签名 URL 含未编码 & 时手动解析完整保留。

        用非白名单域名（example.com）避免真实下载；断言 200 + 回显的 url 完整包含
        全部 & 参数，锁定 raw_qs.startswith("url=") + [4:] 截取逻辑不变。
        """
        c = TestClient(app)
        obs_url = "http://example.com/img.png?AccessKeyId=AKIDEXAMPLE&Signature=sig%3D%3D&x=1&y=2"
        r = c.get("/api/chat/analyze-image?url=" + obs_url)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("error") == "domain not allowed"
        assert body.get("url") == obs_url