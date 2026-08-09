import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import parse_qsl

from flask import Flask, g, jsonify, request, send_from_directory


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.getenv("BOT_DATABASE_PATH", os.path.join(BASE_DIR, "bot_database.db"))
STATIC_DIR = os.path.join(BASE_DIR, "miniapp")
AUTH_MAX_AGE = int(os.getenv("MINIAPP_AUTH_MAX_AGE", "3600"))
PAGE_SIZE = 20
CHECKOUT_ATTEMPTS = {}
CHECKOUT_LOCK = threading.Lock()
MIGRATION_LOCK = threading.Lock()
MIGRATED_PATHS = set()

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


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
        """
    )
    store_columns = column_names(connection, "store")
    additions = {
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
        CREATE INDEX IF NOT EXISTS idx_purchase_history_user_date
            ON purchase_history(user_id, date DESC);
        CREATE INDEX IF NOT EXISTS idx_transactions_user_id
            ON transactions(user_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_store_active_category
            ON store(active, category);
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
        return handler(*args, **kwargs)

    return wrapped


def json_body():
    if not request.is_json:
        raise ValueError("Yêu cầu phải dùng JSON")
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError("JSON không hợp lệ")
    return value


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
        "description": row["description"] or f"Nhận {row['credits']} lượt rút Cookie VIP.",
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
        "SELECT balance, credits, plan_name FROM users WHERE user_id=?", (user_id,)
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
                "plan": user["plan_name"],
                "spent": spent,
                "orderCount": orders,
                "cartCount": cart_count,
            },
            "inventory": {"premiumCookies": stock},
            "support": os.getenv("SUPPORT_USERNAME", "@mnhutdznecon"),
        }
    )


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
                "UPDATE users SET credits=credits+?, plan_name=? WHERE user_id=?",
                (item["credits"] * item["quantity"], item["name"].upper(), user_id),
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
        "SELECT id, amount, status FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 50",
        (int(g.telegram_user["id"]),),
    ).fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Không tìm thấy API"}), 404
    return send_from_directory(STATIC_DIR, "index.html")


if __name__ == "__main__":
    migrate()
    app.run(
        host=os.getenv("MINIAPP_HOST", "127.0.0.1"),
        port=int(os.getenv("MINIAPP_PORT", "8080")),
        debug=os.getenv("APP_ENV") == "development",
    )
