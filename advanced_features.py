"""Pure helpers for the second-generation NFToken Pro features.

The Flask layer owns persistence and authorization.  This module keeps the
period calculations, provider retry policy, signed download primitives and
export watermarking deterministic and easy to test.
"""

import base64
import csv
import hashlib
import hmac
import io
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo


try:
    APP_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:  # pragma: no cover - Windows fallback
    APP_TZ = timezone(timedelta(hours=7))


TRANSLATIONS = {
    "vi": {
        "home": "Trang chủ", "store": "Cửa hàng", "orders": "Đơn hàng",
        "tools": "Tiện ích", "account": "Tài khoản", "language": "Ngôn ngữ",
        "flash_sale": "Flash Sale", "notifications": "Thông báo",
        "missions": "Nhiệm vụ", "download_expired": "Link tải đã hết hạn",
    },
    "en": {
        "home": "Home", "store": "Store", "orders": "Orders",
        "tools": "Tools", "account": "Account", "language": "Language",
        "flash_sale": "Flash Sale", "notifications": "Notifications",
        "missions": "Missions", "download_expired": "Download link expired",
    },
}


def normalize_language(value, fallback="vi"):
    value = str(value or "").lower().replace("_", "-").split("-", 1)[0]
    return value if value in TRANSLATIONS else fallback


def translate(language, key, default=None):
    language = normalize_language(language)
    return TRANSLATIONS.get(language, TRANSLATIONS["vi"]).get(key, default or key)


def local_now(now=None):
    value = now or datetime.now(APP_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=APP_TZ)
    return value.astimezone(APP_TZ)


def period_bounds(period_type, now=None):
    now = local_now(now)
    period_type = str(period_type).upper()
    if period_type == "WEEK":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    elif period_type == "MONTH":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = next_month
    else:
        raise ValueError("period_type must be WEEK or MONTH")
    return start.replace(tzinfo=None), end.replace(tzinfo=None)


def period_key(period_type, now=None):
    start, _ = period_bounds(period_type, now)
    return f"{str(period_type).upper()}:{start.strftime('%Y-%m-%d')}"


def mask_user_id(user_id):
    value = str(int(user_id))
    return value if len(value) <= 4 else value[:2] + "*" * (len(value) - 4) + value[-2:]


def retryable_provider_error(reason_code):
    return str(reason_code or "").lower() in {
        "provider_timeout", "provider_network_error", "provider_unavailable",
        "provider_5xx", "provider_rate_limited", "provider_connection_error",
    }


def signed_download_token(secret, user_id, order_id, expires_at, nonce=None):
    nonce = nonce or secrets.token_urlsafe(24)
    payload = f"{int(user_id)}:{int(order_id)}:{int(expires_at)}:{nonce}"
    signature = hmac.new(str(secret).encode(), payload.encode(), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{encoded}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def verify_download_token(secret, token):
    try:
        encoded, signature = str(token).split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        expected = hmac.new(str(secret).encode(), payload.encode(), hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if not hmac.compare_digest(expected, supplied):
            return None
        user_id, order_id, expires_at, nonce = payload.split(":", 3)
        return {"user_id": int(user_id), "order_id": int(order_id), "expires_at": int(expires_at), "nonce": nonce}
    except (ValueError, TypeError, UnicodeError):
        return None


def safe_filename(value, suffix=".txt"):
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "export")).strip(".-")[:80] or "export"
    if not stem.lower().endswith(suffix.lower()):
        stem += suffix
    return stem


def watermark_export(content, copyright_text, order_code, user_id, enabled=True, netscape=False):
    """Prefix an export without touching cookie key/value lines."""
    if not enabled:
        return content
    lines = [line.strip() for line in str(copyright_text or "").splitlines() if line.strip()]
    prefix = [f"# {line}" for line in lines]
    prefix.append(f"# Order: {order_code}")
    prefix.append(f"# User: {mask_user_id(user_id)}")
    if netscape:
        return "\n".join(prefix + [str(content).lstrip("\ufeff\n")]) + "\n"
    return "\n".join(prefix + [str(content).lstrip("\ufeff\n")]) + "\n"


def csv_bytes(rows, fieldnames):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def flash_price(price, percent):
    percent = max(0, min(100, int(percent or 0)))
    return max(0, int(price) - (int(price) * percent // 100))


def json_safe(value):
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
