import hashlib
import hmac
import html
import io
import json
import os
import sqlite3
import threading
import time
import uuid
import zipfile
import rarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from functools import wraps
from urllib.error import URLError
from urllib.parse import parse_qsl, quote
from urllib.request import Request, urlopen

from flask import Flask, g, jsonify, request, send_from_directory


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.getenv("BOT_DATABASE_PATH", os.path.join(BASE_DIR, "bot_database.db"))
STATIC_DIR = os.path.join(BASE_DIR, "miniapp")
AUTH_MAX_AGE = int(os.getenv("MINIAPP_AUTH_MAX_AGE", "3600"))
PAGE_SIZE = 20
CHECKOUT_ATTEMPTS = {}
CHECKOUT_LOCK = threading.Lock()
TOOL_ATTEMPTS = {}
TOOL_LOCK = threading.Lock()
MIGRATION_LOCK = threading.Lock()
MIGRATED_PATHS = set()

# ── Background Upload Job System ──
UPLOAD_JOBS = {}  # job_id -> {status, progress, result, ...}
UPLOAD_JOBS_LOCK = threading.Lock()

def _bg_upload_worker(job_id, table, entries):
    """Run cookie live-checks in background thread, update UPLOAD_JOBS."""
    try:
        total = len(entries)
        live_entries = []
        dead = 0
        checked = 0
        max_w = min(12, total)
        with ThreadPoolExecutor(max_workers=max_w) as executor:
            futures = {executor.submit(run_cookie_check, e): e for e in entries}
            for future in as_completed(futures):
                try:
                    success, token, _err, account, _ns = future.result()
                    if success and token and account.get("membership_status") == "CURRENT_MEMBER":
                        live_entries.append(futures[future])
                    else:
                        dead += 1
                except Exception:
                    dead += 1
                checked += 1
                with UPLOAD_JOBS_LOCK:
                    UPLOAD_JOBS[job_id]["progress"] = {
                        "checked": checked, "total": total,
                        "live": len(live_entries), "dead": dead,
                        "percent": round(checked / total * 100),
                    }

        conn = sqlite3.connect(DATABASE_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        added = 0
        duplicates = 0
        for entry in live_entries:
            if conn.execute(f"SELECT 1 FROM {table} WHERE data=? LIMIT 1", (entry,)).fetchone():
                duplicates += 1
                continue
            conn.execute(f"INSERT INTO {table}(data,is_used) VALUES(?,0)", (entry,))
            added += 1
        conn.execute(
            "INSERT INTO miniapp_admin_audit(ts,action,target,detail) VALUES(?,?,?,?)",
            (datetime.utcnow().isoformat(), "inventory.upload", table.replace("_cookies", ""),
             f"checked={total},live={len(live_entries)},added={added},dead={dead},duplicates={duplicates}"),
        )
        conn.commit()
        conn.close()

        with UPLOAD_JOBS_LOCK:
            UPLOAD_JOBS[job_id]["status"] = "done"
            UPLOAD_JOBS[job_id]["result"] = {
                "ok": True, "checked": total, "live": len(live_entries),
                "added": added, "dead": dead, "duplicates": duplicates,
            }
    except Exception as exc:
        with UPLOAD_JOBS_LOCK:
            UPLOAD_JOBS[job_id]["status"] = "error"
            UPLOAD_JOBS[job_id]["result"] = {"ok": False, "error": str(exc)}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA busy_timeout=30000")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def column_names(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def migrate():
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0,
            credits INTEGER DEFAULT 0, plan_name TEXT DEFAULT 'FREE',
            is_banned INTEGER DEFAULT 0, last_active TEXT
        );
        CREATE TABLE IF NOT EXISTS store(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
            price INTEGER, credits INTEGER
        );
        CREATE TABLE IF NOT EXISTS purchase_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            plan_name TEXT, price INTEGER, date TEXT
        );
        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            amount INTEGER, status TEXT DEFAULT 'PENDING'
        );
        CREATE TABLE IF NOT EXISTS premium_cookies(
            id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT,
            is_used INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS free_cookies(
            id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT,
            is_used INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS plans(
            name TEXT PRIMARY KEY, tokens_max INTEGER,
            cookies_max INTEGER
        );
        CREATE TABLE IF NOT EXISTS usage(
            user_id INTEGER, date TEXT, tokens_used INTEGER DEFAULT 0,
            free_cookies_used INTEGER DEFAULT 0,
            PRIMARY KEY(user_id, date)
        );
        CREATE TABLE IF NOT EXISTS discount_codes(
            code TEXT PRIMARY KEY, amount INTEGER, uses INTEGER
        );
        INSERT OR IGNORE INTO plans(name, tokens_max, cookies_max)
            VALUES('FREE', 0, 0);
        """
    )
    user_columns = column_names(connection, "users")
    if "nftoken_credits" not in user_columns:
        connection.execute(
            "ALTER TABLE users ADD COLUMN nftoken_credits INTEGER DEFAULT 0"
        )

    store_columns = column_names(connection, "store")
    additions = {
        "nftoken_credits": "INTEGER DEFAULT 0",
        "description": "TEXT DEFAULT ''",
        "category": "TEXT DEFAULT 'Gói Cookie VIP'",
        "image_url": "TEXT DEFAULT ''",
        "featured": "INTEGER DEFAULT 0",
        "warranty_days": "INTEGER DEFAULT 0",
        "active": "INTEGER DEFAULT 1",
        "purchases": "INTEGER DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in store_columns:
            connection.execute(f"ALTER TABLE store ADD COLUMN {name} {definition}")

    history_columns = column_names(connection, "purchase_history")
    history_additions = {
        "store_item_id": "INTEGER",
        "quantity": "INTEGER DEFAULT 1",
        "status": "TEXT DEFAULT 'COMPLETED'",
        "warranty_until": "TEXT",
    }
    for name, definition in history_additions.items():
        if name not in history_columns:
            connection.execute(
                f"ALTER TABLE purchase_history ADD COLUMN {name} {definition}"
            )

    transaction_columns = column_names(connection, "transactions")
    transaction_additions = {
        "transfer_note": "TEXT DEFAULT ''",
        "created_at": "TEXT",
        "submitted_at": "TEXT",
        "reviewed_at": "TEXT",
        "review_note": "TEXT DEFAULT ''",
    }
    for name, definition in transaction_additions.items():
        if name not in transaction_columns:
            connection.execute(
                f"ALTER TABLE transactions ADD COLUMN {name} {definition}"
            )

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS miniapp_cart (
            user_id INTEGER NOT NULL,
            store_item_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity BETWEEN 1 AND 99),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, store_item_id),
            FOREIGN KEY(store_item_id) REFERENCES store(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS miniapp_checkouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL,
            order_ids TEXT NOT NULL,
            total INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS miniapp_support (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS miniapp_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS miniapp_admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO miniapp_settings(key,value) VALUES
            ('maintenance','0'),
            ('announcement',''),
            ('feature_tv','1'),
            ('feature_plan_token','1'),
            ('feature_vip_token','1'),
            ('feature_free_cookie','1'),
            ('feature_giftcode','1'),
            ('feature_deposit','1'),
            ('feature_support','1');
        CREATE INDEX IF NOT EXISTS idx_purchase_history_user_date
            ON purchase_history(user_id, date DESC);
        CREATE INDEX IF NOT EXISTS idx_transactions_user_id
            ON transactions(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_store_active_category
            ON store(active, category);
        CREATE INDEX IF NOT EXISTS idx_miniapp_support_user
            ON miniapp_support(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_miniapp_admin_audit_date
            ON miniapp_admin_audit(id DESC);
        """
    )
    connection.commit()
    connection.close()


@app.before_request
def ensure_migrated():
    if DATABASE_PATH in MIGRATED_PATHS:
        return
    with MIGRATION_LOCK:
        if DATABASE_PATH not in MIGRATED_PATHS:
            migrate()
            MIGRATED_PATHS.add(DATABASE_PATH)


def validate_init_data(raw):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Máy chủ chưa cấu hình TELEGRAM_BOT_TOKEN")
    if not raw or len(raw) > 8192:
        raise ValueError("Thiếu dữ liệu xác thực Telegram")
    values = dict(parse_qsl(raw, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise ValueError("Dữ liệu Telegram không có chữ ký")
    auth_date = int(values.get("auth_date", "0"))
    now = int(time.time())
    if auth_date > now + 30 or now - auth_date > AUTH_MAX_AGE:
        raise ValueError("Phiên Telegram đã hết hạn")
    data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise ValueError("Chữ ký Telegram không hợp lệ")
    try:
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Không đọc được người dùng Telegram") from error
    if user_id <= 0:
        raise ValueError("Telegram ID không hợp lệ")
    return user


def authenticated(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            g.telegram_user = validate_init_data(
                request.headers.get("X-Telegram-Init-Data", "")
            )
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 401
        connection = db()
        user_id = ensure_user(connection, g.telegram_user)
        banned = connection.execute(
            "SELECT is_banned FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        connection.commit()
        if banned and banned[0]:
            return jsonify({"ok": False, "error": "Tài khoản đã bị khóa"}), 403
        if app_setting(connection, "maintenance", "0") == "1" and user_id != configured_admin_id():
            return jsonify({"ok": False, "error": "Hệ thống đang bảo trì, vui lòng quay lại sau"}), 503
        return handler(*args, **kwargs)

    return wrapped


def configured_admin_id():
    try:
        return int(os.getenv("TELEGRAM_ADMIN_ID", "0").strip())
    except ValueError:
        return 0


def admin_required(handler):
    @authenticated
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if configured_admin_id() <= 0 or int(g.telegram_user["id"]) != configured_admin_id():
            return jsonify({"ok": False, "error": "Bạn không có quyền quản trị"}), 403
        return handler(*args, **kwargs)

    return wrapped


def json_body():
    if not request.is_json:
        raise ValueError("Yêu cầu phải dùng JSON")
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("JSON không hợp lệ")
    return value


FEATURE_KEYS = {
    "tv": "feature_tv",
    "planToken": "feature_plan_token",
    "vipToken": "feature_vip_token",
    "freeCookie": "feature_free_cookie",
    "giftcode": "feature_giftcode",
    "deposit": "feature_deposit",
    "support": "feature_support",
}


def app_setting(connection, key, default=""):
    row = connection.execute("SELECT value FROM miniapp_settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def feature_flags(connection):
    return {name: app_setting(connection, key, "1") == "1" for name, key in FEATURE_KEYS.items()}


def require_feature(connection, name):
    if not feature_flags(connection).get(name, False):
        raise ToolError("Chức năng này đang được Admin tạm tắt", 503)


def admin_audit(connection, action, target, details=""):
    connection.execute(
        """INSERT INTO miniapp_admin_audit(admin_id,action,target,details,created_at)
           VALUES(?,?,?,?,?)""",
        (
            int(g.telegram_user["id"]), action, str(target), str(details)[:1000],
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )


def checkout_rate_limited(user_id):
    now = time.monotonic()
    with CHECKOUT_LOCK:
        attempts = [stamp for stamp in CHECKOUT_ATTEMPTS.get(user_id, []) if now - stamp < 10]
        if len(attempts) >= 5:
            CHECKOUT_ATTEMPTS[user_id] = attempts
            return True
        attempts.append(now)
        CHECKOUT_ATTEMPTS[user_id] = attempts
        return False


def tool_rate_limited(user_id, limit=6, window=30):
    now = time.monotonic()
    with TOOL_LOCK:
        attempts = [stamp for stamp in TOOL_ATTEMPTS.get(user_id, []) if now - stamp < window]
        if len(attempts) >= limit:
            TOOL_ATTEMPTS[user_id] = attempts
            return True
        attempts.append(now)
        TOOL_ATTEMPTS[user_id] = attempts
        return False


def telegram_send(chat_id, text, reply_markup=None):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not chat_id:
        return False
    payload = {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        telegram_request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(telegram_request, timeout=10) as response:
            return 200 <= response.status < 300
    except (OSError, URLError):
        app.logger.warning("Unable to notify Telegram admin", exc_info=True)
        return False


def telegram_notify(text, reply_markup=None):
    """Best-effort admin notification; the user flow must not depend on Telegram delivery."""
    return telegram_send(os.getenv("TELEGRAM_ADMIN_ID", "").strip(), text, reply_markup)


class ToolError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def reserve_cookie(connection):
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute(
        "SELECT id, data FROM premium_cookies WHERE is_used=0 ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    if not row:
        connection.rollback()
        raise ToolError("Kho Cookie Premium đang trống", 409)
    updated = connection.execute(
        "UPDATE premium_cookies SET is_used=1 WHERE id=? AND is_used=0", (row["id"],)
    )
    if updated.rowcount != 1:
        connection.rollback()
        raise ToolError("Kho vừa thay đổi, vui lòng thử lại", 409)
    connection.commit()
    return row["id"], row["data"]


def reserve_nftoken_request(connection, user_id, mode):
    today = datetime.now().strftime("%Y-%m-%d")
    connection.execute("BEGIN IMMEDIATE")
    ensure_user(connection, g.telegram_user)
    if mode == "vip":
        updated = connection.execute(
            "UPDATE users SET credits=credits-1 WHERE user_id=? AND credits>0", (user_id,)
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise ToolError("Bạn đã hết lượt Cookie VIP", 409)
        quota_source = "vip"
    else:
        paid = connection.execute(
            """UPDATE users SET nftoken_credits=nftoken_credits-1
               WHERE user_id=? AND nftoken_credits>0""",
            (user_id,),
        )
        if paid.rowcount == 1:
            quota_source = "paid_nftoken"
        else:
            plan_name = connection.execute(
                "SELECT plan_name FROM users WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            plan = connection.execute(
                "SELECT tokens_max FROM plans WHERE name=?", (plan_name,)
            ).fetchone()
            tokens_max = plan[0] if plan else 0
            connection.execute(
                "INSERT OR IGNORE INTO usage(user_id,date) VALUES(?,?)", (user_id, today)
            )
            updated = connection.execute(
                """UPDATE usage SET tokens_used=tokens_used+1
                   WHERE user_id=? AND date=? AND tokens_used<?""",
                (user_id, today, tokens_max),
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise ToolError("Bạn đã hết lượt tạo NFToken", 409)
            quota_source = "daily_nftoken"
    row = connection.execute(
        "SELECT id, data FROM premium_cookies WHERE is_used=0 ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    if not row:
        connection.rollback()
        raise ToolError("Kho Cookie Premium đang trống", 409)
    connection.execute("UPDATE premium_cookies SET is_used=1 WHERE id=?", (row["id"],))
    connection.commit()
    return row["id"], row["data"], quota_source


def refund_nftoken_request(connection, user_id, quota_source):
    today = datetime.now().strftime("%Y-%m-%d")
    connection.execute("BEGIN IMMEDIATE")
    if quota_source == "vip":
        connection.execute("UPDATE users SET credits=credits+1 WHERE user_id=?", (user_id,))
    elif quota_source == "paid_nftoken":
        connection.execute(
            "UPDATE users SET nftoken_credits=nftoken_credits+1 WHERE user_id=?",
            (user_id,),
        )
    else:
        connection.execute(
            """UPDATE usage SET tokens_used=MAX(0,tokens_used-1)
               WHERE user_id=? AND date=?""",
            (user_id, today),
        )
    connection.commit()


def release_cookie(connection, cookie_id, delete=False):
    connection.execute("BEGIN IMMEDIATE")
    if delete:
        connection.execute("DELETE FROM premium_cookies WHERE id=?", (cookie_id,))
    else:
        connection.execute("UPDATE premium_cookies SET is_used=0 WHERE id=?", (cookie_id,))
    connection.commit()


def run_cookie_check(cookie_data):
    """Lazy import keeps normal Mini App startup light and makes the checker testable."""
    from code_goc import checker

    parsed = checker.extract_cookies_from_text(cookie_data)
    if not parsed:
        return False, None, "Cookie sai định dạng", {}, None
    cookies = parsed[0]
    success, token, error, account = checker.check_cookie(cookies)
    netscape = checker.build_netscape_format(cookies) if success and token else None
    return success, token, error, account, netscape


def run_tv_login(cookie_data, tv_code):
    from code_goc import checker, process_tv_login

    parsed = checker.extract_cookies_from_text(cookie_data)
    if not parsed:
        return False, "Cookie sai định dạng", {}
    return process_tv_login(parsed[0], tv_code)


def public_account(account):
    return {
        "name": account.get("account_name", "Không rõ"),
        "email": account.get("email_masked", "Không rõ"),
        "plan": account.get("plan", "Không rõ"),
        "country": account.get("country", "Không rõ"),
        "status": account.get("membership_status", "Không rõ"),
        "quality": account.get("video_quality", "Không rõ"),
        "profiles": account.get("profile_count", "Không rõ"),
    }


def ensure_user(connection, telegram_user):
    user_id = int(telegram_user["id"])
    username = telegram_user.get("username") or ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection.execute(
        """INSERT INTO users(user_id, username, last_active)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             username=excluded.username, last_active=excluded.last_active""",
        (user_id, username, now),
    )
    return user_id


def product_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "price": row["price"],
        "credits": row["credits"],
        "nftokenCredits": row["nftoken_credits"] or 0,
        "description": row["description"] or (
            f"Nhận {row['nftoken_credits'] or 0} lượt tạo NFToken và "
            f"{row['credits']} lượt lấy Cookie VIP."
        ),
        "category": row["category"] or "Gói Cookie VIP",
        "imageUrl": row["image_url"] or "",
        "featured": bool(row["featured"]),
        "warrantyDays": row["warranty_days"] or 0,
        "available": bool(row["active"]),
        "purchases": row["purchases"] or 0,
    }


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self' https://telegram.org; "
        "script-src 'self' https://telegram.org; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
        "connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org"
    )
    if request.path == "/" or request.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(os.path.join(STATIC_DIR, "assets"), filename)


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "NFToken Pro Mini App"})


@app.get("/api/bootstrap")
@authenticated
def bootstrap():
    connection = db()
    user_id = ensure_user(connection, g.telegram_user)
    connection.commit()
    user = connection.execute(
        "SELECT balance,credits,nftoken_credits,plan_name FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    spent = connection.execute(
        "SELECT COALESCE(SUM(price), 0) FROM purchase_history WHERE user_id=?",
        (user_id,),
    ).fetchone()[0]
    orders = connection.execute(
        "SELECT COUNT(*) FROM purchase_history WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    cart_count = connection.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM miniapp_cart WHERE user_id=?",
        (user_id,),
    ).fetchone()[0]
    stock = connection.execute(
        "SELECT COUNT(*) FROM premium_cookies WHERE is_used=0"
    ).fetchone()[0]
    quota = quota_payload(connection, user_id)
    return jsonify(
        {
            "ok": True,
            "brand": {"name": "NFToken Pro", "tagline": "Premium Cookie Store"},
            "user": {
                "id": user_id,
                "firstName": g.telegram_user.get("first_name") or "Bạn",
                "lastName": g.telegram_user.get("last_name") or "",
                "username": g.telegram_user.get("username") or "",
                "photoUrl": g.telegram_user.get("photo_url") or "",
                "balance": user["balance"],
                "credits": user["credits"],
                "nftokenCredits": user["nftoken_credits"] or 0,
                "plan": user["plan_name"],
                "spent": spent,
                "orderCount": orders,
                "cartCount": cart_count,
            },
            "inventory": {"premiumCookies": stock},
            "quota": quota,
            "support": os.getenv("SUPPORT_USERNAME", "@mnhutdznecon"),
            "isAdmin": user_id == configured_admin_id(),
            "copyright": "© 2026 mnhut. All rights reserved.",
            "features": feature_flags(connection),
            "announcement": app_setting(connection, "announcement", ""),
        }
    )


def quota_payload(connection, user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    user = connection.execute(
        "SELECT plan_name,credits,nftoken_credits FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    plan_name = user["plan_name"] if user else "FREE"
    plan = connection.execute(
        "SELECT tokens_max, cookies_max FROM plans WHERE name=?", (plan_name,)
    ).fetchone()
    usage = connection.execute(
        "SELECT tokens_used, free_cookies_used FROM usage WHERE user_id=? AND date=?",
        (user_id, today),
    ).fetchone()
    return {
        "plan": plan_name,
        "credits": user["credits"] if user else 0,
        "nftokenCredits": user["nftoken_credits"] if user else 0,
        "tokensUsed": usage["tokens_used"] if usage else 0,
        "tokensMax": plan["tokens_max"] if plan else 0,
        "freeCookiesUsed": usage["free_cookies_used"] if usage else 0,
        "freeCookiesMax": plan["cookies_max"] if plan else 0,
    }


@app.get("/api/products")
@authenticated
def products():
    query = request.args.get("q", "").strip()[:80]
    category = request.args.get("category", "").strip()[:60]
    sort = request.args.get("sort", "popular")
    try:
        page = max(1, min(int(request.args.get("page", "1")), 1000))
    except ValueError:
        return jsonify({"ok": False, "error": "Trang không hợp lệ"}), 400
    where = ["active=1"]
    params = []
    if query:
        where.append("(name LIKE ? OR description LIKE ? OR category LIKE ?)")
        term = f"%{query}%"
        params.extend([term, term, term])
    if category:
        where.append("category=?")
        params.append(category)
    order = {"price_asc": "price ASC", "price_desc": "price DESC"}.get(
        sort, "featured DESC, purchases DESC, id DESC"
    )
    params.extend([PAGE_SIZE, (page - 1) * PAGE_SIZE])
    rows = db().execute(
        f"SELECT * FROM store WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?",
        params,
    ).fetchall()
    categories = [
        row[0]
        for row in db().execute(
            "SELECT DISTINCT category FROM store WHERE active=1 ORDER BY category"
        ).fetchall()
        if row[0]
    ]
    return jsonify(
        {"ok": True, "items": [product_dict(row) for row in rows], "categories": categories, "page": page}
    )


@app.get("/api/products/<int:item_id>")
@authenticated
def product(item_id):
    row = db().execute("SELECT * FROM store WHERE id=? AND active=1", (item_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Sản phẩm không tồn tại"}), 404
    return jsonify({"ok": True, "item": product_dict(row)})


def cart_payload(connection, user_id):
    rows = connection.execute(
        """SELECT c.quantity, s.* FROM miniapp_cart c
           JOIN store s ON s.id=c.store_item_id
           WHERE c.user_id=? AND s.active=1 ORDER BY c.updated_at DESC""",
        (user_id,),
    ).fetchall()
    items = []
    total = 0
    for row in rows:
        item = product_dict(row)
        item["quantity"] = row["quantity"]
        item["lineTotal"] = row["price"] * row["quantity"]
        total += item["lineTotal"]
        items.append(item)
    return {"items": items, "total": total, "count": sum(x["quantity"] for x in items)}


@app.get("/api/cart")
@authenticated
def get_cart():
    return jsonify({"ok": True, **cart_payload(db(), int(g.telegram_user["id"]))})


@app.put("/api/cart/<int:item_id>")
@authenticated
def update_cart(item_id):
    try:
        quantity = int(json_body().get("quantity", 1))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Số lượng không hợp lệ"}), 400
    if quantity < 0 or quantity > 99:
        return jsonify({"ok": False, "error": "Số lượng phải từ 0 đến 99"}), 400
    connection = db()
    user_id = ensure_user(connection, g.telegram_user)
    product_row = connection.execute(
        "SELECT id FROM store WHERE id=? AND active=1", (item_id,)
    ).fetchone()
    if not product_row:
        return jsonify({"ok": False, "error": "Sản phẩm đã ngừng bán"}), 404
    if quantity == 0:
        connection.execute(
            "DELETE FROM miniapp_cart WHERE user_id=? AND store_item_id=?",
            (user_id, item_id),
        )
    else:
        connection.execute(
            """INSERT INTO miniapp_cart(user_id, store_item_id, quantity, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, store_item_id) DO UPDATE SET
                 quantity=excluded.quantity, updated_at=excluded.updated_at""",
            (user_id, item_id, quantity, datetime.now().isoformat(timespec="seconds")),
        )
    connection.commit()
    return jsonify({"ok": True, **cart_payload(connection, user_id)})


@app.post("/api/checkout")
@authenticated
def checkout():
    try:
        key = str(json_body().get("idempotencyKey", "")).strip()
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if not (16 <= len(key) <= 100) or not all(c.isalnum() or c in "-_" for c in key):
        return jsonify({"ok": False, "error": "Mã xác nhận không hợp lệ"}), 400
    user_id = int(g.telegram_user["id"])
    if checkout_rate_limited(user_id):
        return jsonify({"ok": False, "error": "Bạn thao tác quá nhanh, vui lòng thử lại"}), 429
    connection = db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT order_ids, total FROM miniapp_checkouts WHERE user_id=? AND idempotency_key=?",
            (user_id, key),
        ).fetchone()
        if existing:
            connection.rollback()
            return jsonify({"ok": True, "duplicate": True, "orderIds": json.loads(existing[0]), "total": existing[1]})
        cart = cart_payload(connection, user_id)
        if not cart["items"]:
            connection.rollback()
            return jsonify({"ok": False, "error": "Giỏ hàng đang trống"}), 400
        user = connection.execute(
            "SELECT balance FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not user:
            connection.rollback()
            return jsonify({"ok": False, "error": "Không tìm thấy tài khoản"}), 404
        if user["balance"] < cart["total"]:
            connection.rollback()
            return jsonify({"ok": False, "error": "Số dư không đủ", "required": cart["total"], "balance": user["balance"]}), 409
        updated = connection.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
            (cart["total"], user_id, cart["total"]),
        )
        if updated.rowcount != 1:
            raise RuntimeError("Không thể khóa số dư")
        order_ids = []
        now = datetime.now()
        for item in cart["items"]:
            warranty = (
                (now + timedelta(days=item["warrantyDays"])).strftime("%Y-%m-%d %H:%M:%S")
                if item["warrantyDays"]
                else None
            )
            cursor = connection.execute(
                """INSERT INTO purchase_history
                   (user_id, plan_name, price, date, store_item_id, quantity, status, warranty_until)
                   VALUES (?, ?, ?, ?, ?, ?, 'COMPLETED', ?)""",
                (user_id, item["name"], item["lineTotal"], now.strftime("%Y-%m-%d %H:%M:%S"), item["id"], item["quantity"], warranty),
            )
            order_ids.append(cursor.lastrowid)
            connection.execute(
                """UPDATE users SET credits=credits+?,nftoken_credits=nftoken_credits+?,
                          plan_name=? WHERE user_id=?""",
                (item["credits"] * item["quantity"],
                 item["nftokenCredits"] * item["quantity"],
                 item["name"].upper(), user_id),
            )
            connection.execute("UPDATE store SET purchases=purchases+? WHERE id=?", (item["quantity"], item["id"]))
        connection.execute("DELETE FROM miniapp_cart WHERE user_id=?", (user_id,))
        connection.execute(
            "INSERT INTO miniapp_checkouts(user_id,idempotency_key,order_ids,total,created_at) VALUES(?,?,?,?,?)",
            (user_id, key, json.dumps(order_ids), cart["total"], now.isoformat(timespec="seconds")),
        )
        connection.commit()
        return jsonify({"ok": True, "duplicate": False, "orderIds": order_ids, "total": cart["total"]})
    except sqlite3.IntegrityError:
        connection.rollback()
        return jsonify({"ok": False, "error": "Giao dịch trùng lặp"}), 409
    except Exception:
        connection.rollback()
        app.logger.exception("Checkout failed for user_id=%s", user_id)
        return jsonify({"ok": False, "error": "Không thể hoàn tất giao dịch"}), 500


@app.get("/api/orders")
@authenticated
def orders():
    user_id = int(g.telegram_user["id"])
    rows = db().execute(
        """SELECT id, plan_name, price, date, quantity, status, warranty_until
           FROM purchase_history WHERE user_id=? ORDER BY id DESC LIMIT 100""",
        (user_id,),
    ).fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


@app.get("/api/orders/<int:order_id>")
@authenticated
def order(order_id):
    row = db().execute(
        """SELECT id, plan_name, price, date, quantity, status, warranty_until
           FROM purchase_history WHERE id=? AND user_id=?""",
        (order_id, int(g.telegram_user["id"])),
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Không tìm thấy đơn hàng"}), 404
    return jsonify({"ok": True, "item": dict(row)})


@app.get("/api/transactions")
@authenticated
def transactions():
    rows = db().execute(
        """SELECT id,amount,status,transfer_note,created_at,submitted_at,
                  reviewed_at,review_note
           FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 50""",
        (int(g.telegram_user["id"]),),
    ).fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


@app.get("/api/tools/status")
@authenticated
def tools_status():
    connection = db()
    user_id = int(g.telegram_user["id"])
    premium = connection.execute(
        "SELECT COUNT(*) FROM premium_cookies WHERE is_used=0"
    ).fetchone()[0]
    free = connection.execute(
        "SELECT COUNT(*) FROM free_cookies WHERE is_used=0"
    ).fetchone()[0]
    return jsonify(
        {"ok": True, "quota": quota_payload(connection, user_id), "stock": {"premium": premium, "free": free}, "features": feature_flags(connection)}
    )


@app.post("/api/tools/free-cookie")
@authenticated
def free_cookie():
    connection = db()
    user_id = int(g.telegram_user["id"])
    try:
        require_feature(connection, "freeCookie")
    except ToolError as error:
        return jsonify({"ok": False, "error": str(error)}), error.status
    if tool_rate_limited(user_id):
        return jsonify({"ok": False, "error": "Bạn thao tác quá nhanh", "steps": [{"key": "validate", "label": "Kiểm tra mã TV", "status": "error"}]}), 429
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan_name = connection.execute(
            "SELECT plan_name FROM users WHERE user_id=?", (user_id,)
        ).fetchone()[0]
        plan = connection.execute(
            "SELECT cookies_max FROM plans WHERE name=?", (plan_name,)
        ).fetchone()
        cookies_max = plan[0] if plan else 0
        connection.execute(
            "INSERT OR IGNORE INTO usage(user_id,date) VALUES(?,?)", (user_id, today)
        )
        usage = connection.execute(
            "SELECT free_cookies_used FROM usage WHERE user_id=? AND date=?",
            (user_id, today),
        ).fetchone()[0]
        if usage >= cookies_max:
            connection.rollback()
            return jsonify({"ok": False, "error": "Bạn đã hết lượt Cookie miễn phí hôm nay"}), 409
        cookie = connection.execute(
            "SELECT id,data FROM free_cookies WHERE is_used=0 ORDER BY id LIMIT 1"
        ).fetchone()
        if not cookie:
            connection.rollback()
            return jsonify({"ok": False, "error": "Kho Cookie miễn phí đang trống"}), 409
        connection.execute("UPDATE free_cookies SET is_used=1 WHERE id=?", (cookie["id"],))
        connection.execute(
            "UPDATE usage SET free_cookies_used=free_cookies_used+1 WHERE user_id=? AND date=?",
            (user_id, today),
        )
        connection.commit()
        return jsonify(
            {"ok": True, "cookie": cookie["data"], "quota": quota_payload(connection, user_id)}
        )
    except Exception:
        connection.rollback()
        app.logger.exception("Free cookie failed for user_id=%s", user_id)
        return jsonify({"ok": False, "error": "Không thể nhận Cookie lúc này"}), 500


def generate_one_nftoken(connection, user_id, mode):
    cookie_id, cookie_data, quota_source = reserve_nftoken_request(connection, user_id, mode)
    last_error = "Không tìm thấy Cookie hoạt động"
    for attempt in range(5):
        try:
            success, token, error, account, netscape = run_cookie_check(cookie_data)
        except Exception as exc:
            app.logger.exception("NFToken check failed")
            success, token, error, account, netscape = False, None, str(exc), {}, None
        if success and token and account.get("membership_status") == "CURRENT_MEMBER":
            return {
                "link": f"https://netflix.com/?nftoken={quote(str(token), safe='')}",
                "account": public_account(account),
                "netscape": netscape,
            }
        last_error = error or "Tài khoản đã hết hạn"
        release_cookie(connection, cookie_id, delete=True)
        if attempt < 4:
            try:
                cookie_id, cookie_data = reserve_cookie(connection)
            except ToolError:
                break
    refund_nftoken_request(connection, user_id, quota_source)
    raise ToolError(f"Không tạo được NFToken: {last_error}", 409)


@app.post("/api/tools/nftoken")
@authenticated
def create_nftoken():
    try:
        body = json_body()
        mode = str(body.get("mode", "plan"))
        quantity = int(body.get("quantity", 1))
    except (ValueError, TypeError) as error:
        return jsonify({"ok": False, "error": str(error) or "Dữ liệu không hợp lệ"}), 400
    if mode not in {"plan", "vip"}:
        return jsonify({"ok": False, "error": "Chế độ không hợp lệ"}), 400
    if mode == "plan":
        quantity = 1
    if not 1 <= quantity <= 5:
        return jsonify({"ok": False, "error": "Mỗi lần chỉ rút từ 1 đến 5 Cookie"}), 400
    user_id = int(g.telegram_user["id"])
    connection = db()
    try:
        require_feature(connection, "vipToken" if mode == "vip" else "planToken")
    except ToolError as error:
        return jsonify({"ok": False, "error": str(error)}), error.status
    if tool_rate_limited(user_id, limit=5, window=60):
        return jsonify({"ok": False, "error": "Bạn thao tác quá nhanh"}), 429
    results = []
    try:
        for _ in range(quantity):
            results.append(generate_one_nftoken(connection, user_id, mode))
    except ToolError as error:
        if not results:
            return jsonify({"ok": False, "error": str(error)}), error.status
    return jsonify(
        {"ok": True, "items": results, "partial": len(results) != quantity, "quota": quota_payload(connection, user_id)}
    )


@app.post("/api/tools/tv-login")
@authenticated
def tv_login():
    def steps(active="validate", failed=False):
        names = [
            ("validate", "Kiểm tra mã TV"),
            ("cookie", "Chuẩn bị Cookie Premium live"),
            ("browser", "Mở phiên Netflix bảo mật"),
            ("connect", "Gửi mã kết nối tới TV"),
            ("done", "Xác nhận kết nối"),
        ]
        order = {key: index for index, (key, _label) in enumerate(names)}
        current = order.get(active, 0)
        result = []
        for index, (key, label) in enumerate(names):
            status = "done" if active == "done" or index < current else "active" if index == current else "pending"
            if failed and index == current:
                status = "error"
            result.append({"key": key, "label": label, "status": status})
        return result
    try:
        tv_code = str(json_body().get("code", "")).replace(" ", "").replace("-", "")
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if not (4 <= len(tv_code) <= 12 and tv_code.isalnum()):
        return jsonify({"ok": False, "error": "Mã TV không hợp lệ", "steps": steps("validate", True)}), 400
    user_id = int(g.telegram_user["id"])
    if tool_rate_limited(user_id, limit=3, window=60):
        return jsonify({"ok": False, "error": "Bạn thao tác quá nhanh"}), 429
    connection = db()
    try:
        require_feature(connection, "tv")
    except ToolError as error:
        return jsonify({"ok": False, "error": str(error), "steps": steps("validate", True)}), error.status
    cookie_id = None
    try:
        cookie_id, cookie_data = reserve_cookie(connection)
        success, message, account = run_tv_login(cookie_data, tv_code)
        dead_cookie = any(word in message.lower() for word in ("cookie đã chết", "cookie hết hạn", "cookie sai"))
        release_cookie(connection, cookie_id, delete=dead_cookie)
        cookie_id = None
        if not success:
            failed_stage = "cookie" if dead_cookie else "connect"
            return jsonify({"ok": False, "error": message, "steps": steps(failed_stage, True)}), 409
        return jsonify({
            "ok": True,
            "message": "TV đã được kết nối",
            "account": public_account(account),
            "steps": steps("done"),
        })
    except ToolError as error:
        return jsonify({"ok": False, "error": str(error), "steps": steps("cookie", True)}), error.status
    except Exception:
        if cookie_id is not None:
            release_cookie(connection, cookie_id)
        app.logger.exception("TV login failed for user_id=%s", user_id)
        return jsonify({"ok": False, "error": "Không thể đăng nhập TV lúc này", "steps": steps("browser", True)}), 500


@app.post("/api/giftcode")
@authenticated
def redeem_giftcode():
    try:
        code = str(json_body().get("code", "")).strip().upper()
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if not code or len(code) > 50:
        return jsonify({"ok": False, "error": "Mã quà tặng không hợp lệ"}), 400
    connection = db()
    try:
        require_feature(connection, "giftcode")
    except ToolError as error:
        return jsonify({"ok": False, "error": str(error)}), error.status
    user_id = int(g.telegram_user["id"])
    connection.execute("BEGIN IMMEDIATE")
    gift = connection.execute(
        "SELECT amount,uses FROM discount_codes WHERE code=?", (code,)
    ).fetchone()
    if not gift or gift["uses"] <= 0:
        connection.rollback()
        return jsonify({"ok": False, "error": "Mã không hợp lệ hoặc đã hết lượt"}), 409
    connection.execute("UPDATE discount_codes SET uses=uses-1 WHERE code=?", (code,))
    connection.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (gift["amount"], user_id))
    connection.commit()
    return jsonify({"ok": True, "amount": gift["amount"]})


@app.post("/api/deposits")
@authenticated
def create_deposit():
    try:
        amount = int(json_body().get("amount", 0))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Số tiền không hợp lệ"}), 400
    if not 10000 <= amount <= 100000000:
        return jsonify({"ok": False, "error": "Số tiền phải từ 10.000đ đến 100.000.000đ"}), 400
    connection = db()
    try:
        require_feature(connection, "deposit")
    except ToolError as error:
        return jsonify({"ok": False, "error": str(error)}), error.status
    user_id = int(g.telegram_user["id"])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor = connection.execute(
        """INSERT INTO transactions(user_id,amount,status,created_at)
           VALUES(?,?,'AWAITING_PAYMENT',?)""",
        (user_id, amount, now),
    )
    transaction_id = cursor.lastrowid
    transfer_note = f"NAP {user_id} GD{transaction_id}"
    connection.execute(
        "UPDATE transactions SET transfer_note=? WHERE id=?",
        (transfer_note, transaction_id),
    )
    connection.commit()
    bank_bin = os.getenv("VIETQR_BANK_BIN", "").strip()
    bank_account = os.getenv("VIETQR_ACCOUNT_NUMBER", "").strip()
    account_name = os.getenv("VIETQR_ACCOUNT_NAME", "").strip()
    qr_url = ""
    if bank_bin and bank_account:
        qr_url = (
            f"https://img.vietqr.io/image/{quote(bank_bin)}-{quote(bank_account)}-compact2.png"
            f"?amount={amount}&addInfo={quote(transfer_note)}&accountName={quote(account_name)}"
        )
    return jsonify(
        {"ok": True, "transactionId": transaction_id, "amount": amount,
         "status": "AWAITING_PAYMENT", "transferNote": transfer_note, "qrUrl": qr_url}
    )


@app.post("/api/deposits/<int:transaction_id>/submit")
@authenticated
def submit_deposit(transaction_id):
    connection = db()
    user_id = int(g.telegram_user["id"])
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status FROM transactions WHERE id=? AND user_id=?",
            (transaction_id, user_id),
        ).fetchone()
        if not row:
            connection.rollback()
            return jsonify({"ok": False, "error": "Không tìm thấy giao dịch"}), 404
        if row["status"] != "AWAITING_PAYMENT":
            connection.rollback()
            return jsonify({"ok": False, "error": "Giao dịch đã được gửi hoặc xử lý"}), 409
        submitted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        connection.execute(
            "UPDATE transactions SET status='PENDING',submitted_at=? WHERE id=?",
            (submitted_at, transaction_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return jsonify({"ok": True, "transactionId": transaction_id, "status": "PENDING"})


@app.post("/api/support")
@authenticated
def support_request():
    try:
        message = str(json_body().get("message", "")).strip()
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if not 5 <= len(message) <= 1500:
        return jsonify({"ok": False, "error": "Nội dung hỗ trợ phải từ 5 đến 1500 ký tự"}), 400
    connection = db()
    try:
        require_feature(connection, "support")
    except ToolError as error:
        return jsonify({"ok": False, "error": str(error)}), error.status
    user_id = int(g.telegram_user["id"])
    cursor = connection.execute(
        "INSERT INTO miniapp_support(user_id,message,created_at) VALUES(?,?,?)",
        (user_id, message, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    connection.commit()
    ticket_id = cursor.lastrowid
    telegram_notify(
        f"🛟 <b>HỖ TRỢ MINI APP #{ticket_id}</b>\n\n"
        f"User ID: <code>{user_id}</code>\nNội dung: {html.escape(message)}"
    )
    return jsonify({"ok": True, "ticketId": ticket_id})


def admin_product_values(body):
    name = str(body.get("name", "")).strip()
    description = str(body.get("description", "")).strip()
    category = str(body.get("category", "Gói Cookie VIP")).strip()
    image_url = str(body.get("imageUrl", "")).strip()
    try:
        price = int(body.get("price", 0))
        credits = int(body.get("credits", 0))
        nftoken_credits = int(body.get("nftokenCredits", 0))
        warranty_days = int(body.get("warrantyDays", 0))
    except (TypeError, ValueError) as error:
        raise ValueError("Giá, lượt và bảo hành phải là số") from error
    if not 1 <= len(name) <= 80:
        raise ValueError("Tên sản phẩm phải từ 1 đến 80 ký tự")
    if len(description) > 1000 or not 1 <= len(category) <= 80:
        raise ValueError("Mô tả hoặc danh mục quá dài")
    if price < 0 or credits < 0 or nftoken_credits < 0 or not 0 <= warranty_days <= 3650:
        raise ValueError("Thông số sản phẩm không hợp lệ")
    if image_url and not image_url.startswith("https://"):
        raise ValueError("Ảnh sản phẩm phải dùng liên kết HTTPS")
    return (
        name, price, credits, nftoken_credits, description, category, image_url,
        int(bool(body.get("featured", False))), warranty_days,
        int(bool(body.get("active", True))),
    )


@app.get("/api/admin/dashboard")
@admin_required
def admin_dashboard():
    connection = db()
    query = request.args.get("q", "").strip()[:80]
    user_where = ""
    user_params = []
    if query:
        user_where = "WHERE CAST(user_id AS TEXT) LIKE ? OR username LIKE ?"
        term = f"%{query}%"
        user_params = [term, term]
    users = connection.execute(
        f"""SELECT user_id,username,balance,credits,nftoken_credits,plan_name,is_banned,last_active
            FROM users {user_where} ORDER BY last_active DESC LIMIT 50""",
        user_params,
    ).fetchall()
    products = connection.execute("SELECT * FROM store ORDER BY id DESC").fetchall()
    plans = connection.execute(
        "SELECT name,tokens_max,cookies_max FROM plans ORDER BY name"
    ).fetchall()
    transactions = connection.execute(
        """SELECT t.id,t.user_id,u.username,t.amount,t.status,t.transfer_note,
                  t.created_at,t.submitted_at,t.reviewed_at,t.review_note
           FROM transactions t LEFT JOIN users u ON u.user_id=t.user_id
           ORDER BY CASE t.status WHEN 'PENDING' THEN 0 WHEN 'AWAITING_PAYMENT' THEN 1 ELSE 2 END,
                    t.id DESC LIMIT 50"""
    ).fetchall()
    codes = connection.execute(
        "SELECT code,amount,uses FROM discount_codes ORDER BY code LIMIT 100"
    ).fetchall()
    tickets = connection.execute(
        """SELECT s.id,s.user_id,u.username,s.message,s.status,s.created_at
           FROM miniapp_support s LEFT JOIN users u ON u.user_id=s.user_id
           ORDER BY CASE WHEN s.status='OPEN' THEN 0 ELSE 1 END,s.id DESC LIMIT 50"""
    ).fetchall()
    orders = connection.execute(
        """SELECT p.id,p.user_id,u.username,p.plan_name,p.price,p.date,p.quantity,
                  p.status,p.warranty_until
           FROM purchase_history p LEFT JOIN users u ON u.user_id=p.user_id
           ORDER BY p.id DESC LIMIT 100"""
    ).fetchall()
    audit = connection.execute(
        """SELECT id,admin_id,action,target,details,created_at
           FROM miniapp_admin_audit ORDER BY id DESC LIMIT 100"""
    ).fetchall()
    stats = {
        "users": connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "revenue": connection.execute("SELECT COALESCE(SUM(price),0) FROM purchase_history").fetchone()[0],
        "orders": connection.execute("SELECT COUNT(*) FROM purchase_history").fetchone()[0],
        "pendingDeposits": connection.execute(
            "SELECT COUNT(*) FROM transactions WHERE status='PENDING'"
        ).fetchone()[0],
        "premiumStock": connection.execute(
            "SELECT COUNT(*) FROM premium_cookies WHERE is_used=0"
        ).fetchone()[0],
        "freeStock": connection.execute(
            "SELECT COUNT(*) FROM free_cookies WHERE is_used=0"
        ).fetchone()[0],
        "premiumUsed": connection.execute(
            "SELECT COUNT(*) FROM premium_cookies WHERE is_used=1"
        ).fetchone()[0],
        "freeUsed": connection.execute(
            "SELECT COUNT(*) FROM free_cookies WHERE is_used=1"
        ).fetchone()[0],
        "openTickets": connection.execute(
            "SELECT COUNT(*) FROM miniapp_support WHERE status='OPEN'"
        ).fetchone()[0],
    }
    return jsonify({
        "ok": True,
        "stats": stats,
        "products": [product_dict(row) for row in products],
        "plans": [dict(row) for row in plans],
        "users": [dict(row) for row in users],
        "transactions": [dict(row) for row in transactions],
        "codes": [dict(row) for row in codes],
        "tickets": [dict(row) for row in tickets],
        "orders": [dict(row) for row in orders],
        "audit": [dict(row) for row in audit],
        "settings": {
            "maintenance": app_setting(connection, "maintenance", "0") == "1",
            "announcement": app_setting(connection, "announcement", ""),
            "features": feature_flags(connection),
        },
    })


@app.post("/api/admin/products")
@admin_required
def admin_create_product():
    try:
        values = admin_product_values(json_body())
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    connection = db()
    cursor = connection.execute(
        """INSERT INTO store
           (name,price,credits,nftoken_credits,description,category,image_url,featured,warranty_days,active)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        values,
    )
    admin_audit(connection, "product.create", cursor.lastrowid, values[0])
    connection.commit()
    return jsonify({"ok": True, "id": cursor.lastrowid})


@app.put("/api/admin/products/<int:item_id>")
@admin_required
def admin_update_product(item_id):
    try:
        values = admin_product_values(json_body())
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    connection = db()
    updated = connection.execute(
        """UPDATE store SET name=?,price=?,credits=?,nftoken_credits=?,description=?,category=?,image_url=?,
           featured=?,warranty_days=?,active=? WHERE id=?""",
        (*values, item_id),
    )
    if updated.rowcount == 1:
        admin_audit(connection, "product.update", item_id, values[0])
    connection.commit()
    if updated.rowcount != 1:
        return jsonify({"ok": False, "error": "Không tìm thấy sản phẩm"}), 404
    return jsonify({"ok": True})


@app.put("/api/admin/plans/<path:plan_name>")
@admin_required
def admin_save_plan(plan_name):
    name = plan_name.strip().upper()
    try:
        body = json_body()
        tokens_max = int(body.get("tokensMax", 0))
        cookies_max = int(body.get("cookiesMax", 0))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Hạn mức phải là số"}), 400
    if not name or len(name) > 50 or tokens_max < 0 or cookies_max < 0:
        return jsonify({"ok": False, "error": "Thông tin gói không hợp lệ"}), 400
    connection = db()
    connection.execute(
        """INSERT INTO plans(name,tokens_max,cookies_max) VALUES(?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             tokens_max=excluded.tokens_max,cookies_max=excluded.cookies_max""",
        (name, tokens_max, cookies_max),
    )
    admin_audit(connection, "plan.save", name, f"tokens={tokens_max},cookies={cookies_max}")
    connection.commit()
    return jsonify({"ok": True, "name": name})


@app.put("/api/admin/users/<int:user_id>")
@admin_required
def admin_update_user(user_id):
    try:
        body = json_body()
        balance = int(body.get("balance", 0))
        credits = int(body.get("credits", 0))
        nftoken_credits = int(body.get("nftokenCredits", 0))
        plan = str(body.get("plan", "FREE")).strip().upper()
        banned = int(bool(body.get("isBanned", False)))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Dữ liệu người dùng không hợp lệ"}), 400
    if (balance < 0 or balance > 10**12 or credits < 0 or credits > 10**9 or
            nftoken_credits < 0 or nftoken_credits > 10**9 or not plan):
        return jsonify({"ok": False, "error": "Số dư, lượt hoặc gói không hợp lệ"}), 400
    if user_id == configured_admin_id() and banned:
        return jsonify({"ok": False, "error": "Không thể tự khóa tài khoản Admin"}), 400
    connection = db()
    if not connection.execute("SELECT 1 FROM plans WHERE name=?", (plan,)).fetchone():
        return jsonify({"ok": False, "error": "Gói hạn mức không tồn tại"}), 400
    updated = connection.execute(
        """UPDATE users SET balance=?,credits=?,nftoken_credits=?,plan_name=?,is_banned=?
           WHERE user_id=?""",
        (balance, credits, nftoken_credits, plan, banned, user_id),
    )
    if updated.rowcount == 1:
        admin_audit(connection, "user.update", user_id, f"plan={plan},banned={banned}")
    connection.commit()
    if updated.rowcount != 1:
        return jsonify({"ok": False, "error": "Không tìm thấy người dùng"}), 404
    return jsonify({"ok": True})


@app.put("/api/admin/transactions/<int:transaction_id>")
@admin_required
def admin_update_transaction(transaction_id):
    try:
        body = json_body()
        status = str(body.get("status", "")).upper()
        note = str(body.get("note", "")).strip()[:500]
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if status not in {"APPROVED", "REJECTED"}:
        return jsonify({"ok": False, "error": "Trạng thái không hợp lệ"}), 400
    if status == "REJECTED" and len(note) < 3:
        return jsonify({"ok": False, "error": "Vui lòng nhập lý do từ chối"}), 400
    connection = db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT user_id,amount,status FROM transactions WHERE id=?", (transaction_id,)
        ).fetchone()
        if not row:
            connection.rollback()
            return jsonify({"ok": False, "error": "Không tìm thấy giao dịch"}), 404
        if row["status"] != "PENDING":
            connection.rollback()
            return jsonify({"ok": False, "error": "Giao dịch đã được xử lý"}), 409
        reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        connection.execute(
            """UPDATE transactions
               SET status=?,reviewed_at=?,review_note=? WHERE id=?""",
            (status, reviewed_at, note, transaction_id),
        )
        if status == "APPROVED":
            connection.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (row["amount"], row["user_id"]),
            )
        admin_audit(connection, "transaction.update", transaction_id, f"{status}: {note}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return jsonify({"ok": True})


@app.put("/api/admin/codes/<path:code>")
@admin_required
def admin_save_code(code):
    code = code.strip().upper()
    try:
        body = json_body()
        amount = int(body.get("amount", 0))
        uses = int(body.get("uses", 0))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Giá trị mã không hợp lệ"}), 400
    if not code or len(code) > 50 or amount < 0 or uses < 0:
        return jsonify({"ok": False, "error": "Mã quà tặng không hợp lệ"}), 400
    connection = db()
    connection.execute(
        """INSERT INTO discount_codes(code,amount,uses) VALUES(?,?,?)
           ON CONFLICT(code) DO UPDATE SET amount=excluded.amount,uses=excluded.uses""",
        (code, amount, uses),
    )
    admin_audit(connection, "giftcode.save", code, f"amount={amount},uses={uses}")
    connection.commit()
    return jsonify({"ok": True, "code": code})


@app.put("/api/admin/support/<int:ticket_id>")
@admin_required
def admin_update_support(ticket_id):
    try:
        status = str(json_body().get("status", "")).upper()
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if status not in {"OPEN", "CLOSED"}:
        return jsonify({"ok": False, "error": "Trạng thái không hợp lệ"}), 400
    connection = db()
    updated = connection.execute(
        "UPDATE miniapp_support SET status=? WHERE id=?", (status, ticket_id)
    )
    if updated.rowcount == 1:
        admin_audit(connection, "support.update", ticket_id, status)
    connection.commit()
    if updated.rowcount != 1:
        return jsonify({"ok": False, "error": "Không tìm thấy yêu cầu"}), 404
    return jsonify({"ok": True})


@app.post("/api/admin/support/<int:ticket_id>/reply")
@admin_required
def admin_reply_support(ticket_id):
    try:
        message = str(json_body().get("message", "")).strip()
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if not 1 <= len(message) <= 1500:
        return jsonify({"ok": False, "error": "Phản hồi phải từ 1 đến 1500 ký tự"}), 400
    connection = db()
    ticket = connection.execute(
        "SELECT user_id FROM miniapp_support WHERE id=?", (ticket_id,)
    ).fetchone()
    if not ticket:
        return jsonify({"ok": False, "error": "Không tìm thấy yêu cầu"}), 404
    delivered = telegram_send(
        ticket["user_id"],
        f"🛟 <b>PHẢN HỒI HỖ TRỢ #{ticket_id}</b>\n\n{html.escape(message)}",
    )
    if not delivered:
        return jsonify({"ok": False, "error": "Không gửi được tin nhắn Telegram cho người dùng"}), 502
    connection.execute("UPDATE miniapp_support SET status='CLOSED' WHERE id=?", (ticket_id,))
    admin_audit(connection, "support.reply", ticket_id, "Telegram reply delivered")
    connection.commit()
    return jsonify({"ok": True})


@app.put("/api/admin/settings")
@admin_required
def admin_update_settings():
    try:
        body = json_body()
        maintenance = bool(body.get("maintenance", False))
        announcement = str(body.get("announcement", "")).strip()
        features = body.get("features", {})
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if len(announcement) > 500 or not isinstance(features, dict):
        return jsonify({"ok": False, "error": "Cấu hình hệ thống không hợp lệ"}), 400
    connection = db()
    connection.execute(
        "INSERT OR REPLACE INTO miniapp_settings(key,value) VALUES('maintenance',?)",
        ("1" if maintenance else "0",),
    )
    connection.execute(
        "INSERT OR REPLACE INTO miniapp_settings(key,value) VALUES('announcement',?)",
        (announcement,),
    )
    for name, key in FEATURE_KEYS.items():
        connection.execute(
            "INSERT OR REPLACE INTO miniapp_settings(key,value) VALUES(?,?)",
            (key, "1" if bool(features.get(name, True)) else "0"),
        )
    admin_audit(
        connection, "settings.update", "miniapp",
        f"maintenance={int(maintenance)},announcement={bool(announcement)}",
    )
    connection.commit()
    return jsonify({"ok": True})


@app.post("/api/admin/inventory/<kind>")
@admin_required
def admin_add_inventory(kind):
    table = {"premium": "premium_cookies", "free": "free_cookies"}.get(kind)
    if not table:
        return jsonify({"ok": False, "error": "Loại kho không hợp lệ"}), 400
    try:
        raw = str(json_body().get("data", "")).strip()
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if not raw or len(raw.encode("utf-8")) > 60000:
        return jsonify({"ok": False, "error": "Dữ liệu kho trống hoặc vượt quá 60KB"}), 400
    entries = [entry.strip() for entry in raw.split("\n---\n") if entry.strip()]
    if not 1 <= len(entries) <= 100:
        return jsonify({"ok": False, "error": "Mỗi lần chỉ thêm tối đa 100 mục"}), 400
    connection = db()
    added = 0
    for entry in entries:
        if connection.execute(f"SELECT 1 FROM {table} WHERE data=? LIMIT 1", (entry,)).fetchone():
            continue
        connection.execute(f"INSERT INTO {table}(data,is_used) VALUES(?,0)", (entry,))
        added += 1
    admin_audit(connection, "inventory.add", kind, f"added={added},received={len(entries)}")
    connection.commit()
    return jsonify({"ok": True, "added": added, "duplicates": len(entries) - added})


def cookie_entries_from_upload(filename, payload):
    texts = []
    lower = filename.lower()
    if lower.endswith(".txt"):
        texts.append(payload.decode("utf-8", errors="ignore"))
    elif lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                infos = [item for item in archive.infolist() if not item.is_dir()]
                if len(infos) > 5000:
                    raise ValueError("ZIP vượt quá 5000 file")
                if any(item.flag_bits & 1 for item in infos):
                    raise ValueError("ZIP có file đặt mật khẩu")
                if sum(item.file_size for item in infos) > 500 * 1024 * 1024:
                    raise ValueError("ZIP vượt quá 500MB sau giải nén")
                txt_files = [item for item in infos if item.filename.lower().endswith(".txt")]
                if not txt_files:
                    raise ValueError("ZIP không chứa file .txt")
                for item in txt_files:
                    if item.file_size > 50 * 1024 * 1024:
                        raise ValueError(f"File {os.path.basename(item.filename)} vượt quá 50MB")
                    texts.append(archive.read(item).decode("utf-8", errors="ignore"))
        except zipfile.BadZipFile as error:
            raise ValueError("File ZIP bị lỗi") from error
    elif lower.endswith(".rar"):
        try:
            with rarfile.RarFile(io.BytesIO(payload)) as archive:
                infos = [item for item in archive.infolist() if not item.is_dir()]
                if len(infos) > 5000:
                    raise ValueError("RAR vượt quá 5000 file")
                if any(item.flag_bits & 1 for item in infos):
                    raise ValueError("RAR có file đặt mật khẩu")
                txt_infos = [item for item in infos if item.filename.lower().endswith('.txt')]
                if not txt_infos:
                    raise ValueError("RAR không chứa file .txt")
                for item in txt_infos:
                    raw = archive.read(item).decode('utf-8', errors='ignore')
                    texts.append(raw)
        except rarfile.BadRarFile:
            raise ValueError("File RAR bị lỗi")
    else:
        raise ValueError("Chỉ hỗ trợ file .txt, .zip hoặc .rar")

    from code_goc import checker

    entries = []
    seen = set()
    for text in texts:
        for cookies in checker.extract_cookies_from_text(text):
            netflix_id = cookies.get("NetflixId", "")
            secure_id = cookies.get("SecureNetflixId", "")
            key = (netflix_id, secure_id)
            if not netflix_id or key in seen:
                continue
            seen.add(key)
            entries.append(checker.build_netscape_format(cookies))
            if len(entries) > 99999:
                raise ValueError("Mỗi lần chỉ kiểm tra tối đa 99999 Cookie")
    if not entries:
        raise ValueError("Không tìm thấy Cookie Netflix hợp lệ trong file")
    return entries


@app.post("/api/admin/inventory/<kind>/upload")
@admin_required
def admin_upload_inventory(kind):
    table = {"premium": "premium_cookies", "free": "free_cookies"}.get(kind)
    if not table:
        return jsonify({"ok": False, "error": "Loại kho không hợp lệ"}), 400
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"ok": False, "error": "Vui lòng chọn file .txt, .zip hoặc .rar"}), 400
    payload = uploaded.read(50 * 1024 * 1024 + 1)
    if not payload or len(payload) > 50 * 1024 * 1024:
        return jsonify({"ok": False, "error": "File trống hoặc vượt quá 50MB"}), 400
    try:
        entries = cookie_entries_from_upload(os.path.basename(uploaded.filename), payload)
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400

    # Launch background job so other users are not blocked
    job_id = uuid.uuid4().hex[:12]
    with UPLOAD_JOBS_LOCK:
        UPLOAD_JOBS[job_id] = {
            "status": "running",
            "progress": {"checked": 0, "total": len(entries), "live": 0, "dead": 0, "percent": 0},
            "result": None,
        }
    t = threading.Thread(target=_bg_upload_worker, args=(job_id, table, entries), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id, "total": len(entries), "message": "Đang kiểm tra cookie ở nền..."})


@app.get("/api/admin/inventory/job/<job_id>")
@admin_required
def admin_upload_job_status(job_id):
    with UPLOAD_JOBS_LOCK:
        job = UPLOAD_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job không tồn tại"}), 404
    return jsonify({"ok": True, "status": job["status"], "progress": job["progress"], "result": job["result"]})


@app.post("/api/admin/inventory/<kind>/cleanup")
@admin_required
def admin_cleanup_inventory(kind):
    table = {"premium": "premium_cookies", "free": "free_cookies"}.get(kind)
    if not table:
        return jsonify({"ok": False, "error": "Loại kho không hợp lệ"}), 400
    connection = db()
    deleted = connection.execute(f"DELETE FROM {table} WHERE is_used=1").rowcount
    admin_audit(connection, "inventory.cleanup", kind, f"deleted_used={deleted}")
    connection.commit()
    return jsonify({"ok": True, "deleted": deleted})


@app.put("/api/admin/orders/<int:order_id>")
@admin_required
def admin_update_order(order_id):
    try:
        body = json_body()
        status = str(body.get("status", "COMPLETED")).strip().upper()
        warranty_raw = str(body.get("warrantyUntil", "")).strip()
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if status not in {"PROCESSING", "COMPLETED", "CANCELLED", "WARRANTY"}:
        return jsonify({"ok": False, "error": "Trạng thái đơn không hợp lệ"}), 400
    warranty = None
    if warranty_raw:
        try:
            warranty = datetime.fromisoformat(warranty_raw).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return jsonify({"ok": False, "error": "Ngày bảo hành không hợp lệ"}), 400
    connection = db()
    updated = connection.execute(
        "UPDATE purchase_history SET status=?,warranty_until=? WHERE id=?",
        (status, warranty, order_id),
    )
    if updated.rowcount == 1:
        admin_audit(connection, "order.update", order_id, f"status={status},warranty={warranty or '-'}")
    connection.commit()
    if updated.rowcount != 1:
        return jsonify({"ok": False, "error": "Không tìm thấy đơn hàng"}), 404
    return jsonify({"ok": True})


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Không tìm thấy API"}), 404
    return send_from_directory(STATIC_DIR, "index.html")


if __name__ == "__main__":
    migrate()
    host = os.getenv("MINIAPP_HOST", "127.0.0.1")
    port = int(os.getenv("MINIAPP_PORT", "8080"))
    if os.getenv("APP_ENV") == "development":
        app.run(host=host, port=port, debug=True)
    else:
        from waitress import serve
        print(f"[MiniApp] Production server (Waitress) on http://{host}:{port}", flush=True)
        serve(app, host=host, port=port, threads=8, channel_timeout=120)
