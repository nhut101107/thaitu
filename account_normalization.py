"""Safe normalization for Netflix account metadata shown to users."""

import html
import json
import re
from typing import Any


UNKNOWN_VALUES = {"", "n/a", "na", "none", "null", "unknown", "xx"}
NAME_FIELDS = ("firstName", "displayName", "accountName", "profileName")
PAYMENT_LABELS = {
    "DC": "Thanh toán nhà mạng (DCB)",
    "DCB": "Thanh toán nhà mạng (DCB)",
    "DIRECT_CARRIER_BILLING": "Thanh toán nhà mạng (DCB)",
    "DIRECT_CARRIER": "Thanh toán nhà mạng (DCB)",
    "DIRECT CARRIER": "Thanh toán nhà mạng (DCB)",
    "PAYPAL": "PayPal",
    "CC": "Thẻ tín dụng",
    "CREDIT_CARD": "Thẻ tín dụng",
    "CREDITCARD": "Thẻ tín dụng",
    "GIFT": "Gift Card",
    "GIFTCARD": "Gift Card",
    "ITUNES": "iTunes",
    "GOOGLEPLAY": "Google Play",
    "GOOGLE_PAY": "Google Pay",
}
NON_CARD_PAYMENT_KEYS = {
    "DC", "DCB", "DIRECT_CARRIER_BILLING", "DIRECT CARRIER", "PAYPAL",
    "GIFT", "GIFTCARD", "ITUNES", "GOOGLEPLAY", "GOOGLE_PAY",
}
COUNTRY_ALIASES = {
    "VN": {"vn", "vietnam", "việt nam"},
    "US": {"us", "usa", "united states", "hoa kỳ"},
    "GB": {"gb", "uk", "united kingdom", "vương quốc anh"},
    "TH": {"th", "thailand", "thái lan"},
    "JP": {"jp", "japan", "nhật bản"},
    "KR": {"kr", "korea", "south korea", "hàn quốc"},
}


def _repair_mojibake(text: str) -> str:
    repaired = text
    for _ in range(2):
        if not any(marker in repaired for marker in ("Ã", "Â", "Ä", "Å", "Æ", "â", "ðŸ", "á»")):
            break
        candidate = None
        for encoding in ("latin-1", "cp1252"):
            try:
                candidate = repaired.encode(encoding).decode("utf-8")
                break
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        if not candidate or candidate == repaired:
            break
        repaired = candidate
    return repaired


def decode_escaped_text(value: Any) -> str:
    """Decode JSON, Python-style hex/unicode escapes and HTML entities safely."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Có" if value else "Không"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        return ""
    text = str(value).strip()
    for _ in range(3):
        previous = text
        text = html.unescape(text)
        text = re.sub(
            r"\\+x([0-9a-fA-F]{2})",
            lambda match: chr(int(match.group(1), 16)),
            text,
        )
        text = re.sub(
            r"\\+u([0-9a-fA-F]{4})",
            lambda match: chr(int(match.group(1), 16)),
            text,
        )
        if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
            try:
                decoded = json.loads(text)
                if isinstance(decoded, str):
                    text = decoded
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if text == previous:
            break
    try:
        text = text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        text = re.sub(r"[\ud800-\udfff]", "", text)
    text = re.sub(r"\\+", "", text)
    return _repair_mojibake(html.unescape(text)).strip()


def _profile_name(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("name", "profileName", "rawFirstName", "firstName", "displayName"):
            if key in value:
                name = decode_escaped_text(value[key])
                if name:
                    return name
        return ""
    return decode_escaped_text(value)


def normalize_profiles(value: Any) -> list[str]:
    """Return readable, ordered, unique profile names."""
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    result = []
    seen = set()
    for item in values:
        name = _profile_name(item)
        if not name or name.startswith("{") or name.startswith("["):
            continue
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            result.append(name)
    return result


def normalize_payment_method(value: Any) -> str:
    raw = decode_escaped_text(value)
    if not raw or raw.casefold() in UNKNOWN_VALUES:
        return "Không rõ"
    key = re.sub(r"[\s-]+", "_", raw.upper()).strip("_")
    if key in PAYMENT_LABELS:
        return PAYMENT_LABELS[key]
    compact = key.replace("_", "")
    if compact in PAYMENT_LABELS:
        return PAYMENT_LABELS[compact]
    # Do not expose short technical codes that are not known to the UI.
    return raw if len(raw) > 3 or " " in raw else "Không rõ"


def _name_candidates(account: dict) -> list[str]:
    candidates = list(account.get("_account_name_candidates") or [])
    for key in NAME_FIELDS:
        candidates.append(account.get(key))
    user_info = account.get("userInfo")
    if isinstance(user_info, dict):
        candidates.append(user_info.get("firstName"))
    candidates.append(account.get("account_name"))
    return [decode_escaped_text(value) for value in candidates if decode_escaped_text(value)]


def _is_country_name(candidate: str, country: Any) -> bool:
    candidate_key = candidate.casefold()
    country_text = decode_escaped_text(country).casefold()
    if country_text and candidate_key == country_text:
        return True
    country_code = country_text.upper()
    aliases = COUNTRY_ALIASES.get(country_code, set())
    if candidate_key in aliases:
        return True
    return any(candidate_key in alias_set and country_text in alias_set for alias_set in COUNTRY_ALIASES.values())


def normalize_account_payload(account: dict) -> dict:
    """Normalize account fields before either bot or Mini App renders them."""
    normalized = dict(account or {})
    country = decode_escaped_text(normalized.get("country"))
    name = next(
        (candidate for candidate in _name_candidates(normalized) if not _is_country_name(candidate, country)),
        "N/A",
    )
    normalized["account_name"] = name
    normalized["country"] = country or "N/A"

    payment_raw = normalized.get("payment_method") or normalized.get("paymentType") or normalized.get("paymentMethod")
    card_raw = normalized.get("cc_type") or normalized.get("cardType") or normalized.get("cardBrand")
    payment = normalize_payment_method(payment_raw)
    card = normalize_payment_method(card_raw)
    if payment == "Không rõ" and card == "Thanh toán nhà mạng (DCB)":
        payment = card
    if payment == "Thanh toán nhà mạng (DCB)" and card in {"Không rõ", "Thanh toán nhà mạng (DCB)"}:
        card = payment
    normalized["payment_method"] = payment
    normalized["cc_type"] = card
    normalized["last4"] = decode_escaped_text(normalized.get("last4")) or "N/A"
    if payment in {"Thanh toán nhà mạng (DCB)", "PayPal", "Gift Card", "iTunes", "Google Play", "Google Pay"}:
        normalized["last4"] = "Không có"

    profiles = normalize_profiles(normalized.get("profiles") or normalized.get("allProfiles"))
    normalized["profiles"] = profiles
    normalized["profile_count"] = len(profiles)
    return normalized
