"""Image understanding module: OCR + visual feature analysis → LLM interpretation.

Used by both Web Chat and Feishu Chat to inject image context into LLM prompts.
Pipeline: RapidOCR (PP-OCRv4) for text + PIL for visual features → opengw LLM for natural language description.
"""
import os, tempfile, base64, io, time, json, urllib.request, urllib.error
from src.config import log

# Lazy-loaded OCR engine
_ocr_engine = None
_ocr_init_failed = False


def _get_ocr():
    """Lazy init RapidOCR engine (first call loads model, ~2s)."""
    global _ocr_engine, _ocr_init_failed
    if _ocr_engine is not None:
        return _ocr_engine
    if _ocr_init_failed:
        return None
    try:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
        log.info("ImageUnderstanding: RapidOCR engine initialized")
        return _ocr_engine
    except ImportError:
        _ocr_init_failed = True
        log.warning("ImageUnderstanding: rapidocr_onnxruntime not installed, OCR disabled")
        return None
    except Exception as e:
        _ocr_init_failed = True
        log.warning(f"ImageUnderstanding: OCR init failed: {e}")
        return None


def _analyze_image_features(image_path: str) -> str:
    """Extract visual features from image using PIL: dimensions, dominant colors, brightness."""
    try:
        from PIL import Image
        img = Image.open(image_path)
        w, h = img.size
        features = [f"图片尺寸: {w}x{h}px"]

        # Resize for fast analysis
        thumb = img.resize((100, 100), Image.Resampling.LANCZOS)

        # Convert to RGB if needed
        if thumb.mode not in ('RGB', 'RGBA'):
            thumb = thumb.convert('RGB')
        elif thumb.mode == 'RGBA':
            thumb = thumb.convert('RGB')

        # Dominant color (simple quantization)
        colors = thumb.getcolors(10000)
        if colors:
            colors.sort(key=lambda x: -x[0])
            top = colors[:5]
            color_names = []
            for cnt, rgb in top:
                r, g, b = rgb[:3]
                # Simple color naming
                if r > 200 and g > 200 and b > 200:
                    name = "白色"
                elif r < 50 and g < 50 and b < 50:
                    name = "黑色"
                elif r > 200 and g < 100 and b < 100:
                    name = "红色"
                elif r < 100 and g > 200 and b < 100:
                    name = "绿色"
                elif r < 100 and g < 100 and b > 200:
                    name = "蓝色"
                elif r > 200 and g > 200 and b < 100:
                    name = "黄色"
                elif r > 150 and g < 150 and b > 150:
                    name = "紫色"
                elif r > 200 and g > 150 and b < 100:
                    name = "橙色"
                else:
                    name = f"RGB({r},{g},{b})"
                pct = cnt * 100 // (100 * 100)
                if pct >= 5:
                    color_names.append(f"{name}({pct}%)")
            if color_names:
                features.append(f"主要颜色: {', '.join(color_names)}")

        # Brightness
        pixels = list(thumb.getdata())
        if pixels:
            total_brightness = sum((p[0] + p[1] + p[2]) / 3 for p in pixels[:1000] if isinstance(p, (tuple, list)) and len(p) >= 3)
            avg = total_brightness / min(len(pixels), 1000)
            if avg > 180:
                features.append("整体亮度: 偏亮")
            elif avg < 60:
                features.append("整体亮度: 偏暗")
            else:
                features.append("整体亮度: 正常")

        # Four-quadrant color analysis
        qw, qh = 50, 50
        quadrants = ["左上", "右上", "左下", "右下"]
        positions = [(0, 0, qw, qh), (qw, 0, 100, qh), (0, qh, qw, 100), (qw, qh, 100, 100)]
        quad_info = []
        for i, box in enumerate(positions):
            region = thumb.crop(box)
            qcolors = region.getcolors(2500)
            if qcolors:
                qcolors.sort(key=lambda x: -x[0])
                qr, qg, qb = qcolors[0][1][:3]
                if qr > 200 and qg > 200 and qb > 200:
                    qn = "白"
                elif qr < 50 and qg < 50 and qb < 50:
                    qn = "黑"
                elif qr > 200 and qg < 100 and qb < 100:
                    qn = "红"
                elif qr < 100 and qg > 200 and qb < 100:
                    qn = "绿"
                elif qr < 100 and qg < 100 and qb > 200:
                    qn = "蓝"
                else:
                    qn = f"RGB({qr},{qg},{qb})"
                quad_info.append(f"{quadrants[i]}={qn}")
        if quad_info:
            features.append(f"四象限分布: {' '.join(quad_info)}")

        return "；".join(features)
    except Exception as e:
        log.warning(f"ImageUnderstanding: feature analysis failed: {e}")
        return ""


def _run_ocr(image_path: str) -> str:
    """Run OCR on image, return extracted text."""
    ocr = _get_ocr()
    if not ocr:
        return ""
    try:
        result, elapse = ocr(image_path)
        if not result:
            return ""
        # result is list of [box, text, score]
        texts = []
        for item in result:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                texts.append(item[1])
            elif isinstance(item, str):
                texts.append(item)
        ocr_text = "\n".join(texts).strip()
        if ocr_text:
            log.info(f"ImageUnderstanding: OCR extracted {len(texts)} lines, {len(ocr_text)} chars")
        return ocr_text
    except Exception as e:
        log.warning(f"ImageUnderstanding: OCR failed: {e}")
        return ""


def _load_vlm_config() -> dict:
    """加载 VLM 配置。优先级：环境变量 > configs/vlm-config.json > 空。

    返回 {"base_url", "api_key", "model", "max_tokens"} 或空 dict。
    和 server-config.json / OBS_* 同模式：gitignored 文件 + 环境变量覆盖。
    """
    # 1. 环境变量（最高优先级，和 OBS_ACCESS_KEY 模式一致）
    base_url = os.environ.get("VLM_BASE_URL", "").strip()
    api_key = os.environ.get("VLM_API_KEY", "").strip()
    model = os.environ.get("VLM_MODEL", "").strip()
    if base_url and api_key:
        return {
            "base_url": base_url.rstrip("/"),
            "api_key": api_key,
            "model": model or "qwen2.5-vl-72b",
            "max_tokens": int(os.environ.get("VLM_MAX_TOKENS", "500")),
        }
    # 2. configs/vlm-config.json（gitignored，和 server-config.json 同模式）
    config_path = os.path.join(os.path.dirname(__file__), "..", "..", "configs", "vlm-config.json")
    try:
        if os.path.isfile(config_path):
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            if cfg.get("base_url") and cfg.get("api_key"):
                return {
                    "base_url": cfg["base_url"].rstrip("/"),
                    "api_key": cfg["api_key"],
                    "model": cfg.get("model", "qwen2.5-vl-72b"),
                    "max_tokens": int(cfg.get("max_tokens", 500)),
                }
    except Exception as e:
        log.warning(f"ImageUnderstanding: failed to load vlm-config.json: {e}")
    return {}


def _call_vlm_with_image(image_path: str, ocr_text: str) -> str:
    """调用支持 image_url 的 VLM 模型（qwen2.5-vl-72b），直接发送图片进行真视觉理解。

    这是真正的多模态调用——模型能看见图片像素，不依赖 OCR 文本猜测。
    OCR 文本作为辅助信息一起发送，让描述更准确。
    """
    cfg = _load_vlm_config()
    if not cfg:
        return ""

    # 读图片转 base64 data URL
    try:
        with open(image_path, "rb") as f:
            img_bytes = f.read()
        b64 = base64.b64encode(img_bytes).decode()
    except Exception as e:
        log.warning(f"ImageUnderstanding: failed to read image file: {e}")
        return ""

    ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}
    mime = mime_map.get(ext, "image/png")
    data_url = f"data:{mime};base64,{b64}"

    # 构造 OpenAI vision 兼容请求（ModelArts MaaS 兼容此格式）
    content = [{"type": "image_url", "image_url": {"url": data_url}}]
    prompt_text = "请直接描述这张图片的内容，包括图片中的物体、场景、文字、UI布局等视觉信息（不超过200字）。"
    if ocr_text:
        prompt_text += f"\n\n图片中已识别到的文字（供参考）：{ocr_text}"
    content.append({"type": "text", "text": prompt_text})

    body = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "user", "content": content}],
        "max_tokens": cfg["max_tokens"],
        "temperature": 0.3,
    }, ensure_ascii=False).encode("utf-8")

    url = f"{cfg['base_url']}/chat/completions"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {cfg['api_key']}")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        msg = result.get("choices", [{}])[0].get("message", {})
        content_resp = msg.get("content", "")
        # 兼容推理模型：content 可能在 reasoning_content 里
        if not content_resp:
            content_resp = msg.get("reasoning_content", "")
        if content_resp:
            log.info(f"ImageUnderstanding: VLM (vision) generated {len(content_resp)} chars description")
            return content_resp.strip()
        log.warning("ImageUnderstanding: VLM (vision) returned empty content")
        return ""
    except Exception as e:
        log.warning(f"ImageUnderstanding: VLM (vision) call failed: {e}")
        return ""


def _call_vlm(ocr_text: str, features: str) -> str:
    """Call opengw LLM (glm-5.2) to interpret OCR text + visual features into natural language description.

    Since opengw models don't support image_url, we feed the OCR text and PIL feature
    analysis to the LLM as text, asking it to produce a concise image description.

    This is the fallback path when no VLM config is available.
    """
    if not ocr_text and not features:
        return ""

    # Load API config from opencode.jsonc
    api_key = ""
    base_url = ""
    model = "glm-5.2"
    try:
        config_path = os.path.expanduser("~/.config/opencode/opencode.jsonc")
        with open(config_path) as f:
            raw = f.read()
        # opencode.jsonc is JSONC — strip // comments (not inside strings) and trailing commas
        # Simple state machine: track if inside a string
        cleaned = []
        in_str = False
        i = 0
        while i < len(raw):
            ch = raw[i]
            if ch == '"' and (i == 0 or raw[i-1] != '\\'):
                in_str = not in_str
                cleaned.append(ch)
                i += 1
            elif not in_str and ch == '/' and i + 1 < len(raw) and raw[i+1] == '/':
                # Skip until end of line
                while i < len(raw) and raw[i] != '\n':
                    i += 1
            else:
                cleaned.append(ch)
                i += 1
        raw = ''.join(cleaned)
        import re as _re
        raw = _re.sub(r',\s*([}\]])', r'\1', raw)  # strip trailing commas
        cfg = json.loads(raw)
        opts = cfg.get("provider", {}).get("modelarts", {}).get("options", {})
        api_key = opts.get("apiKey", "")
        base_url = opts.get("baseURL", "").rstrip("/")
        models = cfg.get("provider", {}).get("modelarts", {}).get("models", {})
        if "glm-5.2" in models:
            model = "glm-5.2"
    except Exception as e:
        log.warning(f"ImageUnderstanding: failed to load opengw config: {e}")
        return ""

    if not api_key or not base_url:
        log.warning("ImageUnderstanding: no opengw API config available")
        return ""

    # Build prompt for LLM
    prompt_parts = ["根据以下图片分析数据，直接输出图片内容的自然语言描述（不超过150字），不要输出思考过程："]
    if ocr_text:
        prompt_parts.append(f"\n图片文字：{ocr_text}")
    if features:
        prompt_parts.append(f"\n视觉特征：{features}")
    user_prompt = "\n".join(prompt_parts)

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": user_prompt}],
        "max_tokens": 300,
        "temperature": 0.3,
    }, ensure_ascii=False).encode("utf-8")

    url = f"{base_url}/chat/completions"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        msg = result.get("choices", [{}])[0].get("message", {})
        content = msg.get("content", "")
        # opengw models are reasoning models: content may be empty, answer in reasoning_content
        if not content:
            reasoning = msg.get("reasoning_content", "")
            if reasoning:
                # Extract the final answer from reasoning: look for numbered "最终输出" or last paragraph
                import re as _re
                # Try to find "最终输出" / "最终答案" / "草拟描述" section
                for pattern in [r'最终输出[：:]\s*(.+?)(?:\n\n|\Z)', r'最终答案[：:]\s*(.+?)(?:\n\n|\Z)',
                                r'草拟描述[：:]\s*(.+?)(?:\n\n|\Z)', r'描述[：:]\s*(.+?)(?:\n\n|\Z)']:
                    m = _re.search(pattern, reasoning, _re.DOTALL)
                    if m:
                        content = m.group(1).strip()
                        break
                if not content:
                    # Fallback: take the last non-empty paragraph that looks like a description
                    paragraphs = [p.strip() for p in reasoning.split('\n') if p.strip()]
                    for p in reversed(paragraphs):
                        if len(p) > 20 and not p.startswith(('1.', '2.', '3.', '4.', '5.', '分析', '检查', '精简', '步骤')):
                            content = p
                            break
                    if not content:
                        content = reasoning[-200:].strip()
        if content:
            # Clean up reasoning artifacts: strip markdown bold, list markers, step labels
            import re as _re2
            content = _re2.sub(r'\*+', '', content)  # strip markdown bold
            content = _re2.sub(r'^[\s\-*•]+', '', content).strip()  # strip list markers
            content = _re2.sub(r'草稿\d*[：:]?\s*', '', content).strip()  # strip "草稿1:" prefix
            content = _re2.sub(r'字数检查\d*[：:]?\s*.*$', '', content).strip()  # strip word count check
            content = _re2.sub(r'\s*->\s*$', '', content).strip()  # strip trailing arrow
            log.info(f"ImageUnderstanding: VLM generated {len(content)} chars description")
            return content
        log.warning("ImageUnderstanding: VLM returned empty content")
        return ""
    except Exception as e:
        log.warning(f"ImageUnderstanding: VLM call failed: {e}")
        return ""


def understand_image(image_path: str) -> str:
    """Analyze image and return a text description for LLM prompt injection.

    Pipeline (三级降级):
    1. VLM 真视觉理解（qwen2.5-vl-72b，需配置 vlm-config.json）——模型直接看图
    2. OCR + 特征 → opengw LLM 文本解读（老方案，glm-5.2 猜图）
    3. 纯 OCR + PIL 特征直接注入（最终兜底）
    """
    if not os.path.isfile(image_path):
        log.warning(f"ImageUnderstanding: file not found: {image_path}")
        return ""

    # Step 1: OCR text extraction（始终执行，作为辅助信息和降级保障）
    ocr_text = _run_ocr(image_path)

    # Step 2: 优先尝试 VLM 真视觉理解（模型直接看图片像素）
    vlm_desc = _call_vlm_with_image(image_path, ocr_text)
    if vlm_desc:
        return f"## 用户上传的图片分析\n{vlm_desc}\n\n## 请基于以上图片信息回答用户问题"

    # Step 3: 降级到老方案（OCR + PIL 特征 → opengw LLM 文本解读）
    features = _analyze_image_features(image_path)
    if not ocr_text and not features:
        return ""

    vlm_desc = _call_vlm(ocr_text, features)
    if vlm_desc:
        return f"## 用户上传的图片分析\n{vlm_desc}\n\n## 请基于以上图片信息回答用户问题"

    # Step 4: 最终降级：纯 OCR + 特征直接注入
    parts = []
    if ocr_text:
        parts.append(f"## 图片中的文字内容\n{ocr_text}")
    if features:
        parts.append(f"## 图片视觉特征\n{features}")
    desc = "\n\n".join(parts)
    desc = f"## 用户上传的图片分析\n{desc}\n\n## 请基于以上图片信息回答用户问题"
    return desc


def understand_image_from_bytes(image_bytes: bytes, ext: str = ".png") -> str:
    """Analyze image from raw bytes (e.g., downloaded from Feishu)."""
    try:
        # Save to temp file for OCR (RapidOCR requires file path)
        fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="img_")
        try:
            os.write(fd, image_bytes)
            os.close(fd)
            return understand_image(tmp_path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as e:
        log.warning(f"ImageUnderstanding: bytes analysis failed: {e}")
        return ""


def understand_image_from_base64(b64_data: str) -> str:
    """Analyze image from base64 encoded string (e.g., from Web upload)."""
    try:
        # Strip data URL prefix if present
        if b64_data.startswith("data:"):
            b64_data = b64_data.split(",", 1)[-1]
        image_bytes = base64.b64decode(b64_data)
        return understand_image_from_bytes(image_bytes)
    except Exception as e:
        log.warning(f"ImageUnderstanding: base64 analysis failed: {e}")
        return ""
