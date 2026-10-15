"""邮件发送服务——SMTP relay / Resend API / DEV 日志三模式自动选择。

配置来源（优先级）：
1. configs/email.json 配置文件（推荐，和 Grape 其他配置统一）
2. 环境变量（SMTP_HOST / RESEND_API_KEY 等，fallback）

发送优先级：
1. 固定验证码（非生产环境 + dev_verification_code 已配置）→ 跳过邮件发送
2. SMTP relay（配置了 smtp.host）
3. Resend API（配置了 resend.api_key）
4. LOG_ONLY（以上均未配置，非生产环境输出到日志）

生产环境强制禁用固定验证码和 LOG_ONLY，必须配置 SMTP 或 Resend API。
"""
import json
import os
import smtplib
import ssl as ssl_module
from email.mime.text import MIMEText
from email.utils import formatdate

from src.config import log, CONFIG_DIR


def is_production_env() -> bool:
    """判断是否为生产环境。统一走 security_config.is_production()。"""
    from src.auth.security_config import is_production
    return is_production()


# ---------------------------------------------------------------------------
# 配置加载：configs/email.json > 环境变量 fallback
# ---------------------------------------------------------------------------

_EMAIL_CONFIG_PATH = os.path.join(CONFIG_DIR, "email.json")
_email_config_cache = None
_email_config_mtime = 0.0


def _load_email_config() -> dict:
    """读取 configs/email.json（gitignored，动态读，改后无需重启）。

    D46 P1-11: 加文件 mtime 检查，配置文件修改后自动重新加载。
    返回 dict；文件缺失/损坏返回 {}。
    """
    global _email_config_cache, _email_config_mtime
    try:
        current_mtime = os.path.getmtime(_EMAIL_CONFIG_PATH) if os.path.exists(_EMAIL_CONFIG_PATH) else 0
    except Exception:
        current_mtime = 0
    # mtime 未变 → 返回缓存
    if _email_config_cache is not None and current_mtime == _email_config_mtime:
        return _email_config_cache
    try:
        if os.path.exists(_EMAIL_CONFIG_PATH):
            with open(_EMAIL_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            _email_config_cache = data if isinstance(data, dict) else {}
        else:
            _email_config_cache = {}
    except Exception:
        _email_config_cache = {}
    _email_config_mtime = current_mtime
    return _email_config_cache


def _cfg(key: str, default: str = "") -> str:
    """从 email.json 取值，环境变量 fallback。"""
    cfg = _load_email_config()
    val = cfg.get(key, "")
    if val:
        return str(val).strip()
    return os.environ.get(key.upper(), default).strip()


def _cfg_nested(section: str, key: str, default: str = "") -> str:
    """从 email.json 嵌套 section 取值，环境变量 fallback。

    例: _cfg_nested("smtp", "host") → email.json.smtp.host → SMTP_HOST 环境变量
    """
    cfg = _load_email_config()
    section_data = cfg.get(section, {})
    if isinstance(section_data, dict):
        val = section_data.get(key, "")
        if val:
            return str(val).strip()
    # 环境变量 fallback: SMTP_{KEY}
    env_key = f"{section.upper()}_{key.upper()}"
    return os.environ.get(env_key, default).strip()


def _smtp_host() -> str:
    return _cfg_nested("smtp", "host")


def _smtp_port() -> int:
    val = _cfg_nested("smtp", "port", "25")
    try:
        return int(val)
    except ValueError:
        return 25


def _smtp_username() -> str:
    return _cfg_nested("smtp", "username")


def _smtp_password() -> str:
    return _cfg_nested("smtp", "password")


def _smtp_use_tls() -> bool:
    return _cfg_nested("smtp", "use_tls", "true").lower() in ("1", "true", "yes")


def _resend_api_key() -> str:
    return _cfg_nested("resend", "api_key") or _cfg("resend_api_key")


def _from_email() -> str:
    return _cfg("from_email", "noreply@grape.dev")


def _dev_verification_code() -> str:
    """开发环境固定验证码。优先从 email.json，环境变量 fallback。"""
    return _cfg("dev_verification_code") or os.environ.get("GRAPE_DEV_VERIFICATION_CODE", "").strip()


# ---------------------------------------------------------------------------
# 邮件服务
# ---------------------------------------------------------------------------

class EmailService:
    """邮件发送服务。

    三模式自动选择：SMTP relay > Resend API > LOG_ONLY（非生产）。
    """

    def send_verification_code(self, to: str, code: str, scene: str) -> bool:
        """发送验证码邮件。

        返回 True 表示发送成功（或 DEV 模式跳过）。
        生产环境无邮件服务配置时抛出 RuntimeError。

        优先级（与需求 8 一致）：
        1. 固定验证码（非生产环境 + 环境变量已配置）→ 跳过邮件发送
        2. SMTP relay
        3. Resend API
        4. LOG_ONLY（非生产环境）
        """
        # 1. 固定验证码（非生产环境，最高优先级）
        if not is_production_env():
            dev_code = _dev_verification_code()
            if dev_code and dev_code.isdigit() and len(dev_code) == 6:
                log.info(f"[DEV] Fixed verification code configured, skipping email to {to} (scene={scene})")
                return True

        body = self._build_verification_email(code)

        # 2. SMTP relay
        if _smtp_host():
            return self._send_smtp(to, f"您的 Grape 验证码", body)

        # 3. Resend API
        if _resend_api_key():
            return self._send_resend(to, f"您的 Grape 验证码", body)

        # 4. LOG_ONLY（非生产环境）
        if not is_production_env():
            # P2-11: mask 验证码，只显示前 2 位
            log.info(f"[DEV] Verification code for {to} (scene={scene}): {code[:2]}****")
            return True

        # 生产环境无邮件服务
        raise RuntimeError(
            "邮件服务未配置：生产环境必须配置 SMTP_HOST 或 RESEND_API_KEY"
        )

    def send_notification(self, to: str, subject: str, body: str) -> bool:
        """发送通知邮件（非验证码）。"""
        if _smtp_host():
            return self._send_smtp(to, subject, body)

        if _resend_api_key():
            return self._send_resend(to, subject, body)

        if not is_production_env():
            log.info(f"[DEV] Notification to {to}: {subject}")
            return True

        raise RuntimeError("邮件服务未配置")

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _build_verification_email(self, code: str) -> str:
        """构建验证码邮件 HTML。"""
        return (
            f'<div style="font-family:sans-serif;max-width:400px;margin:0 auto;">'
            f'<h2>您的验证码</h2>'
            f'<p style="font-size:32px;font-weight:bold;letter-spacing:8px;'
            f'margin:24px 0;">{code}</p>'
            f'<p>此验证码 10 分钟内有效。</p>'
            f'<p style="color:#666;font-size:14px;">'
            f'如果您没有请求此验证码，请忽略此邮件。</p>'
            f'</div>'
        )

    def _send_smtp(self, to: str, subject: str, html_body: str) -> bool:
        """通过 SMTP 中继发送邮件。"""
        # D46 P1-8: 过滤换行符防邮件头注入
        to = to.replace("\r", "").replace("\n", "")
        subject = subject.replace("\r", "").replace("\n", "")
        host = _smtp_host()
        port = _smtp_port()
        username = _smtp_username()
        password = _smtp_password()
        use_tls = _smtp_use_tls()
        from_addr = _from_email()

        try:
            msg = MIMEText(html_body, "html", "utf-8")
            msg["Subject"] = subject
            msg["From"] = from_addr
            msg["To"] = to
            msg["Date"] = formatdate(localtime=True)

            if port == 465:
                # SMTPS (implicit TLS) — D46 P0-2: 始终启用证书校验
                context = ssl_module.create_default_context()
                with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as srv:
                    if username:
                        srv.login(username, password)
                    srv.sendmail(from_addr, [to], msg.as_string())
            else:
                # STARTTLS
                with smtplib.SMTP(host, port, timeout=30) as srv:
                    srv.ehlo()
                    if use_tls:
                        context = ssl_module.create_default_context()
                        srv.starttls(context=context)
                        srv.ehlo()
                    if username:
                        srv.login(username, password)
                    srv.sendmail(from_addr, [to], msg.as_string())

            log.info(f"SMTP email sent to {to} (subject={subject})")
            return True

        except Exception as e:
            log.warning(f"SMTP send failed to {to}: {e}")
            return False

    def _send_resend(self, to: str, subject: str, html_body: str) -> bool:
        """通过 Resend API 发送邮件。"""
        try:
            import requests as _req
            from_addr = _from_email()
            resp = _req.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {_resend_api_key()}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": from_addr,
                    "to": [to],
                    "subject": subject,
                    "html": html_body,
                },
                timeout=15,
            )
            if resp.status_code in (200, 201):
                log.info(f"Resend email sent to {to} (subject={subject})")
                return True
            log.warning(f"Resend API error: {resp.status_code} {resp.text[:200]}")
            return False
        except Exception as e:
            log.warning(f"Resend send failed to {to}: {e}")
            return False


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_email_service: EmailService | None = None


def get_email_service() -> EmailService:
    """获取邮件服务单例。"""
    global _email_service
    if _email_service is None:
        _email_service = EmailService()
    return _email_service


# ---------------------------------------------------------------------------
# 开发环境固定验证码
# ---------------------------------------------------------------------------

def get_dev_verification_code() -> str:
    """获取开发环境固定验证码（非生产环境才返回非空值）。"""
    if is_production_env():
        return ""
    return _dev_verification_code()


def is_dev_verification_code(code: str) -> bool:
    """判断是否匹配开发环境固定验证码（常量时间比较）。"""
    dev_code = get_dev_verification_code()
    if not dev_code or len(dev_code) != 6 or not dev_code.isdigit():
        return False
    # 常量时间比较防时序攻击
    import hmac
    return hmac.compare_digest(code, dev_code)
