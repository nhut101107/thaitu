import hashlib
import hmac
import html
import io
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
import zipfile
import rarfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.error import URLError
from urllib.parse import parse_qsl, quote, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from flask import Flask, Response, g, jsonify, request, send_from_directory
from account_normalization import normalize_account_payload, normalize_membership_status
from product_providers import ProviderError, provider_from_row
from advanced_features import (
    APP_TZ, csv_bytes, flash_price, local_now, mask_user_id, normalize_language,
    period_bounds, period_key, retryable_provider_error, safe_filename,
    signed_download_token, translate, verify_download_token, watermark_export,
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.getenv("BOT_DATABASE_PATH", os.path.join(BASE_DIR, "bot_database.db"))
STATIC_DIR = os.path.join(BASE_DIR, "miniapp")
AUTH_MAX_AGE = int(os.getenv("MINIAPP_AUTH_MAX_AGE", "3600"))
PAGE_SIZE = 20
TV_LOGIN_RUNTIME_VERSION = "tv-login-runtime-r10"
DOWNLOAD_SECRET = os.getenv("MINIAPP_DOWNLOAD_SECRET") or os.getenv("TELEGRAM_BOT_TOKEN", "nftoken-download-secret")
REQUIRED_GROUP_CHAT = os.getenv("TELEGRAM_REQUIRED_GROUP", "@mnhutgroup").strip() or "@mnhutgroup"
REQUIRED_GROUP_URL = os.getenv("TELEGRAM_REQUIRED_GROUP_URL", "https://t.me/mnhutgroup").strip() or "https://t.me/mnhutgroup"
GROUP_GATE_ENABLED = os.getenv("TELEGRAM_GROUP_GATE_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
try:
    PWA_SESSION_MAX_AGE = max(900, min(int(os.getenv("MINIAPP_PWA_SESSION_MAX_AGE", "604800")), 2592000))
except (TypeError, ValueError):
    PWA_SESSION_MAX_AGE = 604800


def source_fingerprint():
    digest = hashlib.sha256()
    for filename in ("miniapp_server.py", "code_goc.py"):
        with open(os.path.join(BASE_DIR, filename), "rb") as source_file:
            digest.update(source_file.read())
    return digest.hexdigest()[:20]


SOURCE_FINGERPRINT = source_fingerprint()

def env_int(name, default):
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default

# Upload limits are configurable for large VPS inventories.
INVENTORY_MAX_UPLOAD_MB = env_int("INVENTORY_MAX_UPLOAD_MB", 512)
INVENTORY_MAX_FILE_MB = env_int("INVENTORY_MAX_FILE_MB", 50)
INVENTORY_MAX_FILES = env_int("INVENTORY_MAX_FILES", 10000)
INVENTORY_MAX_ENTRIES = env_int("INVENTORY_MAX_ENTRIES", 250000)

CHECKOUT_ATTEMPTS = {}
CHECKOUT_LOCK = threading.Lock()
TOOL_ATTEMPTS = {}
TOOL_LOCK = threading.Lock()
DEVICE_LIMIT = 5
LOW_STOCK_ALERT_LOCK = threading.Lock()
LOW_STOCK_ALERTS = {}
MAINTENANCE_LOCK = threading.Lock()
LAST_MAINTENANCE_RUN = 0.0
PROCESS_STARTED_AT = time.time()
COOKIE_CHECK_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="nftoken-check")
NFTOKEN_JOB_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="nftoken-job")
try:
    COOKIE_CHECK_TIMEOUT = max(1, min(int(os.getenv("MINIAPP_COOKIE_CHECK_TIMEOUT", "30")), 30))
except (TypeError, ValueError):
    COOKIE_CHECK_TIMEOUT = 30
NFTOKEN_TOTAL_TIMEOUT = 90
try:
    TRIAL_NFTOKEN_FREE_COOKIE_PERCENT = max(
        51, min(int(os.getenv("TRIAL_NFTOKEN_FREE_COOKIE_PERCENT", "70")), 99)
    )
except (TypeError, ValueError):
    TRIAL_NFTOKEN_FREE_COOKIE_PERCENT = 70
MIGRATION_LOCK = threading.Lock()
MIGRATED_PATHS = set()
try:
    LOCAL_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    # Windows deployments without the optional tzdata package still use the
    # fixed UTC+07:00 offset used by Ho Chi Minh City.
    LOCAL_TZ = timezone(timedelta(hours=7))


def local_today():
    return datetime.now(LOCAL_TZ).date().isoformat()


def now_iso():
    return datetime.now(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="seconds")


def migration_backup_path():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{DATABASE_PATH}.migration.{stamp}.{uuid.uuid4().hex[:8]}.bak"


def backup_database_for_migration():
    if not os.path.exists(DATABASE_PATH):
        return None
    target = migration_backup_path()
    source = sqlite3.connect(DATABASE_PATH, timeout=30)
    backup = sqlite3.connect(target, timeout=30)
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    return target


def restore_database_backup(target):
    if not os.path.exists(target):
        return False
    source = sqlite3.connect(target, timeout=30)
    destination = sqlite3.connect(DATABASE_PATH, timeout=30)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return True

# ── Background Upload Job System ──
UPLOAD_JOBS = {}  # job_id -> {status, progress, result, ...}
UPLOAD_JOBS_LOCK = threading.Lock()

def _bg_upload_worker(job_id, table, entries, admin_id):
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
            "INSERT INTO miniapp_admin_audit(admin_id,action,target,details,created_at) VALUES(?,?,?,?,?)",
            (admin_id, "inventory.upload", table.replace("_cookies", ""),
             f"checked={total},live={len(live_entries)},added={added},dead={dead},duplicates={duplicates}",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
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
            UPLOAD_JOBS[job_id]["result"] = {"ok": False, "error": "Không thể xử lý lô Cookie"}


def _bg_inventory_scan_worker(job_id, source, rows, admin_id):
    table = COOKIE_TABLES[source]
    live = dead = quarantined = checked = 0
    try:
        def check(row):
            result = run_cookie_check(row["data"], timeout=COOKIE_CHECK_TIMEOUT, request_id=f"inventory-{job_id}")
            return row["id"], result

        with ThreadPoolExecutor(max_workers=min(6, max(1, len(rows)))) as executor:
            futures = [executor.submit(check, row) for row in rows]
            for future in as_completed(futures):
                cookie_id, (success, token, error, account, _netscape) = future.result()
                reason = nftoken_failure_reason(error)
                connection = sqlite3.connect(DATABASE_PATH, timeout=30)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=30000")
                if success and token and account.get("membership_status") == "CURRENT_MEMBER":
                    inventory_outcome(connection, source, cookie_id, True)
                    live += 1
                elif reason in {"cookie_expired", "cookie_invalid"}:
                    inventory_outcome(connection, source, cookie_id, False, reason)
                    dead += 1
                else:
                    inventory_outcome(connection, source, cookie_id, False, reason)
                    quarantined += 1
                connection.commit()
                connection.close()
                checked += 1
                with UPLOAD_JOBS_LOCK:
                    UPLOAD_JOBS[job_id]["progress"] = {
                        "checked": checked, "total": len(rows), "live": live,
                        "dead": dead, "quarantined": quarantined,
                        "percent": round(checked / max(1, len(rows)) * 100),
                    }
        connection = sqlite3.connect(DATABASE_PATH, timeout=30)
        connection.execute(
            "INSERT INTO miniapp_admin_audit(admin_id,action,target,details,created_at) VALUES(?,?,?,?,?)",
            (admin_id, "inventory.scan", source,
             f"checked={checked},live={live},dead={dead},quarantined={quarantined}", now_iso()),
        )
        connection.commit()
        connection.close()
        with UPLOAD_JOBS_LOCK:
            UPLOAD_JOBS[job_id]["status"] = "done"
            UPLOAD_JOBS[job_id]["result"] = {
                "ok": True, "checked": checked, "live": live,
                "dead": dead, "quarantined": quarantined,
            }
    except Exception as exc:
        app.logger.error("Inventory scan failed job_id=%s type=%s", job_id, type(exc).__name__)
        with UPLOAD_JOBS_LOCK:
            UPLOAD_JOBS[job_id]["status"] = "error"
            UPLOAD_JOBS[job_id]["result"] = {"ok": False, "error": "Không thể hoàn tất kiểm tra kho"}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = INVENTORY_MAX_UPLOAD_MB * 1024 * 1024


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
        CREATE TABLE IF NOT EXISTS missions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            condition_json TEXT NOT NULL DEFAULT '{}',
            reward_credits INTEGER NOT NULL DEFAULT 0,
            starts_at TEXT,
            ends_at TEXT,
            max_claims INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
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
        "requires_customer_email": "INTEGER DEFAULT 0",
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
        "original_price": "INTEGER DEFAULT 0",
        "discount_percent": "INTEGER DEFAULT 0",
        "discount_amount": "INTEGER DEFAULT 0",
        "final_price": "INTEGER DEFAULT 0",
        "promo_code": "TEXT DEFAULT ''",
        "provider_id": "INTEGER",
        "external_order_id": "TEXT DEFAULT ''",
        "customer_email": "TEXT DEFAULT ''",
        "customer_email_approved": "INTEGER DEFAULT 0",
        "customer_email_approved_at": "TEXT",
        "customer_email_notified_at": "TEXT",
    }
    for name, definition in history_additions.items():
        if name not in history_columns:
            connection.execute(
                f"ALTER TABLE purchase_history ADD COLUMN {name} {definition}"
            )

    for name, definition in {
        "provider_id": "INTEGER",
        "external_product_id": "TEXT DEFAULT ''",
    }.items():
        if name not in store_columns:
            connection.execute(f"ALTER TABLE store ADD COLUMN {name} {definition}")

    user_columns = column_names(connection, "users")
    for name, definition in {
        "rank": "TEXT DEFAULT 'Bronze'",
        "referral_code": "TEXT DEFAULT ''",
        "referred_by": "INTEGER",
        "referral_qualified": "INTEGER DEFAULT 0",
        "referral_joined_at": "TEXT",
        "language": "TEXT DEFAULT 'vi'",
        "group_verified": "INTEGER DEFAULT 0",
        "group_verified_at": "TEXT",
        "group_member_status": "TEXT DEFAULT ''",
    }.items():
        if name not in user_columns:
            connection.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")

    code_columns = column_names(connection, "discount_codes")
    for name, definition in {
        "code_type": "TEXT DEFAULT 'BALANCE'",
        "percent": "INTEGER DEFAULT 0",
        "per_user": "INTEGER DEFAULT 1",
        "starts_at": "TEXT",
        "ends_at": "TEXT",
        "min_order_total": "INTEGER DEFAULT 0",
        "product_ids": "TEXT DEFAULT ''",
    }.items():
        if name not in code_columns:
            connection.execute(f"ALTER TABLE discount_codes ADD COLUMN {name} {definition}")

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
            original_total INTEGER NOT NULL DEFAULT 0,
            discount_amount INTEGER NOT NULL DEFAULT 0,
            promo_code TEXT NOT NULL DEFAULT '',
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
        CREATE TABLE IF NOT EXISTS nftoken_jobs(
            request_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            mode TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'running',
            result_json TEXT NOT NULL DEFAULT '{}',
            reason_code TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            status_code INTEGER NOT NULL DEFAULT 409,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_nftoken_jobs_user_date
            ON nftoken_jobs(user_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS miniapp_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trial_usage (
            user_id INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            nftoken_used INTEGER NOT NULL DEFAULT 0,
            cookie_used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, local_date)
        );
        CREATE TABLE IF NOT EXISTS miniapp_admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS product_providers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            api_key TEXT NOT NULL DEFAULT '',
            timeout INTEGER NOT NULL DEFAULT 10,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provider_orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL,
            external_order_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            request_json TEXT NOT NULL DEFAULT '{}',
            response_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(provider_id,idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS discount_redemptions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            checkout_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(code,user_id), UNIQUE(code,checkout_key)
        );
        CREATE TABLE IF NOT EXISTS customer_rank_settings(
            rank TEXT PRIMARY KEY,
            referral_threshold INTEGER NOT NULL DEFAULT 0,
            spend_threshold INTEGER NOT NULL DEFAULT 0,
            benefits TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS referral_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_id INTEGER NOT NULL UNIQUE,
            referral_code TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'qualified',
            reward_credits INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            qualified_at TEXT
        );
        CREATE TABLE IF NOT EXISTS referral_rewards(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            milestone INTEGER NOT NULL,
            credits INTEGER NOT NULL,
            event_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS free_cookie_checkins(
            user_id INTEGER NOT NULL,
            local_date TEXT NOT NULL,
            claimed INTEGER NOT NULL DEFAULT 2,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id,local_date)
        );
        CREATE TABLE IF NOT EXISTS copyright_settings(
            id INTEGER PRIMARY KEY CHECK(id=1),
            enabled INTEGER NOT NULL DEFAULT 1,
            text TEXT NOT NULL DEFAULT '© mnhut - NFToken Pro\nBản quyền nội dung xuất bởi hệ thống NFToken Pro',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS brand_assets(
            id INTEGER PRIMARY KEY CHECK(id=1), filename TEXT NOT NULL DEFAULT '',
            mime_type TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_store_provider_product
            ON store(provider_id,external_product_id)
            WHERE provider_id IS NOT NULL AND external_product_id <> '';
        CREATE INDEX IF NOT EXISTS idx_provider_orders_user ON provider_orders(user_id,id DESC);
        CREATE INDEX IF NOT EXISTS idx_referral_events_referrer ON referral_events(referrer_id,id DESC);
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
        INSERT OR IGNORE INTO miniapp_settings(key,value) VALUES
            ('feature_referral','1'),('free_cookie_daily_limit','2'),
            ('trial_nftoken_enabled','1'),('trial_nftoken_daily_limit','2'),
            ('trial_cookie_enabled','1'),('trial_cookie_daily_limit','2'),
            ('delivery_warranty_days','1'),('low_stock_threshold','5');
        INSERT OR IGNORE INTO customer_rank_settings(rank,referral_threshold,spend_threshold,benefits) VALUES
            ('Bronze',0,0,'Hạng mặc định'),('Silver',5,100000,'Ưu đãi Silver'),
            ('Platinum',20,500000,'Ưu đãi Platinum'),('Diamond',50,2000000,'Ưu đãi Diamond');
        INSERT OR IGNORE INTO missions(code,name,description,condition_json,reward_credits,active,created_at,updated_at)
            VALUES
            ('daily_checkin','Điểm danh hôm nay','Điểm danh Cookie Free trong ngày','{"type":"checkin"}',1,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),
            ('first_order','Đơn hàng đầu tiên','Hoàn thành đơn hàng đầu tiên','{"type":"first_order"}',2,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),
            ('refer_friend','Giới thiệu bạn bè','Có ít nhất một referral hợp lệ','{"type":"referral","minimum":1}',1,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP);
        INSERT OR IGNORE INTO copyright_settings(id,enabled,text,updated_at)
            VALUES(1,1,'© mnhut - NFToken Pro\nBản quyền nội dung xuất bởi hệ thống NFToken Pro',CURRENT_TIMESTAMP);
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
        CREATE TABLE IF NOT EXISTS referral_periods(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_type TEXT NOT NULL,
            period_key TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            reward_enabled INTEGER NOT NULL DEFAULT 0,
            reward_credits INTEGER NOT NULL DEFAULT 0,
            winner_limit INTEGER NOT NULL DEFAULT 10,
            created_at TEXT NOT NULL,
            closed_at TEXT,
            UNIQUE(period_type,period_key)
        );
        CREATE TABLE IF NOT EXISTS referral_period_rewards(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            rank_position INTEGER NOT NULL,
            reward_credits INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(period_id,user_id), UNIQUE(period_id,rank_position)
        );
        CREATE TABLE IF NOT EXISTS flash_sales(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            store_item_id INTEGER NOT NULL,
            discount_percent INTEGER NOT NULL,
            quantity_limit INTEGER NOT NULL,
            quantity_sold INTEGER NOT NULL DEFAULT 0,
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            allow_promo INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(store_item_id) REFERENCES store(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_flash_sales_active_time ON flash_sales(active,starts_at,ends_at);
        CREATE TABLE IF NOT EXISTS flash_sale_claims(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            checkout_key TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(sale_id,checkout_key)
        );
        CREATE TABLE IF NOT EXISTS provider_attempts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkout_key TEXT NOT NULL,
            provider_id INTEGER NOT NULL,
            external_product_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            error_code TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_provider_attempts_checkout ON provider_attempts(checkout_key,id DESC);
        CREATE TABLE IF NOT EXISTS secure_downloads(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            order_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            content BLOB NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'text/plain; charset=utf-8',
            expires_at TEXT NOT NULL,
            max_downloads INTEGER NOT NULL DEFAULT 1,
            download_count INTEGER NOT NULL DEFAULT 0,
            revoked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_downloaded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS missions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            condition_json TEXT NOT NULL DEFAULT '{}',
            reward_credits INTEGER NOT NULL DEFAULT 0,
            starts_at TEXT,
            ends_at TEXT,
            max_claims INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mission_claims(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            claimed_at TEXT NOT NULL,
            UNIQUE(mission_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id,is_read,id DESC);
        CREATE TABLE IF NOT EXISTS user_preferences(
            user_id INTEGER PRIMARY KEY,
            language TEXT NOT NULL DEFAULT 'vi',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS delivery_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            inventory_source TEXT NOT NULL DEFAULT '',
            inventory_id INTEGER,
            request_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'delivered',
            account_json TEXT NOT NULL DEFAULT '{}',
            warranty_until TEXT,
            delivered_at TEXT NOT NULL,
            UNIQUE(user_id,kind,request_id)
        );
        CREATE INDEX IF NOT EXISTS idx_delivery_events_user
            ON delivery_events(user_id,id DESC);
        CREATE INDEX IF NOT EXISTS idx_delivery_inventory_user
            ON delivery_events(user_id,inventory_source,inventory_id,status);
        CREATE TABLE IF NOT EXISTS warranty_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            delivery_id INTEGER NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            resolution TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id,idempotency_key),
            UNIQUE(delivery_id)
        );
        CREATE INDEX IF NOT EXISTS idx_warranty_status
            ON warranty_requests(status,id DESC);
        CREATE TABLE IF NOT EXISTS user_devices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            device_hash TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id,device_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_user_devices_active
            ON user_devices(user_id,revoked,last_seen_at DESC);
        CREATE TABLE IF NOT EXISTS risk_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            device_hash TEXT NOT NULL DEFAULT '',
            code TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            request_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(user_id,code,request_id)
        );
        CREATE INDEX IF NOT EXISTS idx_risk_events_user
            ON risk_events(user_id,id DESC);
        CREATE TABLE IF NOT EXISTS payment_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            provider TEXT NOT NULL DEFAULT 'manual',
            provider_reference TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL,
            received_at TEXT NOT NULL,
            payload_hash TEXT NOT NULL DEFAULT '',
            UNIQUE(provider,provider_reference)
        );
        CREATE TABLE IF NOT EXISTS service_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS automation_runs(
            run_key TEXT PRIMARY KEY,
            completed_at TEXT NOT NULL
        );
        """
    )
    for table in ("premium_cookies", "free_cookies"):
        inventory_columns = column_names(connection, table)
        for name, definition in {
            "health_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "failure_count": "INTEGER NOT NULL DEFAULT 0",
            "last_checked_at": "TEXT",
            "last_error_code": "TEXT NOT NULL DEFAULT ''",
            "quarantine_until": "TEXT",
            "delivery_count": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in inventory_columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_availability "
            f"ON {table}(is_used,health_status,quarantine_until,id)"
        )
    support_columns = column_names(connection, "miniapp_support")
    for name, definition in {
        "order_id": "INTEGER",
        "category": "TEXT NOT NULL DEFAULT 'general'",
        "priority": "TEXT NOT NULL DEFAULT 'normal'",
        "admin_reply": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT",
    }.items():
        if name not in support_columns:
            connection.execute(f"ALTER TABLE miniapp_support ADD COLUMN {name} {definition}")
    transaction_columns = column_names(connection, "transactions")
    for name, definition in {
        "payment_deadline": "TEXT",
        "provider_reference": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in transaction_columns:
            connection.execute(f"ALTER TABLE transactions ADD COLUMN {name} {definition}")
    provider_columns = column_names(connection, "product_providers")
    for name, definition in {
        "priority": "INTEGER NOT NULL DEFAULT 100",
        "last_error_code": "TEXT NOT NULL DEFAULT ''",
        "last_response_ms": "INTEGER NOT NULL DEFAULT 0",
        "last_checked_at": "TEXT",
    }.items():
        if name not in provider_columns:
            connection.execute(f"ALTER TABLE product_providers ADD COLUMN {name} {definition}")

    checkout_columns = column_names(connection, "miniapp_checkouts")
    for name, definition in {
        "original_total": "INTEGER NOT NULL DEFAULT 0",
        "discount_amount": "INTEGER NOT NULL DEFAULT 0",
        "promo_code": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in checkout_columns:
            connection.execute(f"ALTER TABLE miniapp_checkouts ADD COLUMN {name} {definition}")

    nftoken_job_columns = column_names(connection, "nftoken_jobs")
    if "status_code" not in nftoken_job_columns:
        connection.execute("ALTER TABLE nftoken_jobs ADD COLUMN status_code INTEGER NOT NULL DEFAULT 409")
    connection.commit()
    connection.close()


@app.before_request
def ensure_migrated():
    if request.method == "POST" and request.path == "/api/tools/nftoken":
        g.nftoken_request_started = time.monotonic()
        g.nftoken_request_id = "-"
    if DATABASE_PATH in MIGRATED_PATHS:
        return
    with MIGRATION_LOCK:
        if DATABASE_PATH not in MIGRATED_PATHS:
            had_database = os.path.exists(DATABASE_PATH)
            backup_path = backup_database_for_migration() if had_database else None
            try:
                migrate()
            except Exception:
                if backup_path:
                    restore_database_backup(backup_path)
                raise
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
    if values.get("start_param"):
        user["_start_param"] = values["start_param"]
    return user


def issue_pwa_session(user_id):
    expires_at = int(time.time()) + PWA_SESSION_MAX_AGE
    payload = f"{int(user_id)}.{expires_at}"
    signature = hmac.new(DOWNLOAD_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}", expires_at


def validate_pwa_session(raw):
    value = str(raw or "").strip().split(".")
    if len(value) != 3:
        raise ValueError("Phiên PWA không hợp lệ")
    user_id, expires_at, signature = value
    payload = f"{user_id}.{expires_at}"
    expected = hmac.new(DOWNLOAD_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError("Phiên PWA không hợp lệ")
    try:
        user_id = int(user_id)
        expires_at = int(expires_at)
    except ValueError as error:
        raise ValueError("Phiên PWA không hợp lệ") from error
    if user_id <= 0 or expires_at < int(time.time()):
        raise ValueError("Phiên PWA đã hết hạn")
    return {"id": user_id, "first_name": "Bạn", "_pwa_session": True}


def authenticated(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            init_data = request.headers.get("X-Telegram-Init-Data", "")
            g.telegram_user = validate_init_data(init_data) if init_data else validate_pwa_session(
                request.headers.get("X-PWA-Session", "")
            )
        except ValueError as error:
            return jsonify({"ok": False, "error": str(error)}), 401
        connection = db()
        user_id = ensure_user(connection, g.telegram_user)
        membership = connection.execute(
            "SELECT group_verified FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if GROUP_GATE_ENABLED and user_id != configured_admin_id() and not (membership and membership[0]):
            connection.commit()
            return jsonify({
                "ok": False,
                "error": "Bạn cần tham gia nhóm mnhutgroup và quay lại bot bấm Xác nhận trước khi mở Shop MMO",
                "reason_code": "group_membership_required",
                "join_url": REQUIRED_GROUP_URL,
            }), 403
        register_referral(connection, user_id, g.telegram_user.get("_start_param"))
        device = register_device(connection, user_id)
        if not device["ok"]:
            connection.commit()
            message = "Thiết bị này đã bị thu hồi" if device["reason_code"] == "device_revoked" else "Tài khoản đã đạt giới hạn thiết bị"
            return jsonify({"ok": False, "error": message, "reason_code": device["reason_code"]}), 403
        g.device_hash = device.get("deviceHash", "")
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


EMAIL_LOCAL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$")
EMAIL_DOMAIN_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def normalize_customer_email(value):
    """Normalize the service email without logging the submitted value."""
    if not isinstance(value, str):
        return ""
    email = value.strip().lower()
    if len(email) > 254 or any(ord(char) < 32 or ord(char) == 127 for char in email):
        return ""
    local, separator, domain = email.rpartition("@")
    if not separator or not EMAIL_LOCAL_RE.fullmatch(local) or ".." in local:
        return ""
    if local.startswith(".") or local.endswith(".") or not EMAIL_DOMAIN_RE.fullmatch(domain):
        return ""
    return email


def required_customer_emails(body, items):
    """Validate the email against the current cart, then repeat after cart locking."""
    per_item = body.get("customerEmails")
    if not isinstance(per_item, dict):
        per_item = {}
    result = {}
    for item in items:
        if not item.get("requiresCustomerEmail"):
            continue
        raw = per_item.get(str(item["id"]), body.get("customerEmail", ""))
        normalized = normalize_customer_email(raw)
        if not raw:
            raise ToolError(
                "Vui lòng nhập Gmail/email để nhận dịch vụ",
                400,
                "customer_email_required",
            )
        if not normalized:
            raise ToolError(
                "Gmail/email nhận dịch vụ không hợp lệ",
                400,
                "invalid_customer_email",
            )
        result[int(item["id"])] = normalized
    return result


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


def bounded_setting_int(connection, key, default, minimum=0, maximum=1000):
    try:
        value = int(app_setting(connection, key, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(value, maximum))


def trial_settings(connection):
    return {
        "nftokenEnabled": app_setting(connection, "trial_nftoken_enabled", "1") == "1",
        "nftokenDailyLimit": bounded_setting_int(connection, "trial_nftoken_daily_limit", 2),
        "cookieEnabled": app_setting(connection, "trial_cookie_enabled", "1") == "1",
        "cookieDailyLimit": bounded_setting_int(connection, "trial_cookie_daily_limit", 2),
    }


def trial_payload(connection, user_id):
    today = local_today()
    settings = trial_settings(connection)
    row = connection.execute(
        "SELECT nftoken_used,cookie_used FROM trial_usage WHERE user_id=? AND local_date=?",
        (user_id, today),
    ).fetchone()
    nftoken_used = int(row["nftoken_used"] or 0) if row else 0
    cookie_used = int(row["cookie_used"] or 0) if row else 0
    return {
        **settings,
        "date": today,
        "nftokenUsed": nftoken_used,
        "nftokenRemaining": max(0, settings["nftokenDailyLimit"] - nftoken_used) if settings["nftokenEnabled"] else 0,
        "cookieUsed": cookie_used,
        "cookieRemaining": max(0, settings["cookieDailyLimit"] - cookie_used) if settings["cookieEnabled"] else 0,
    }


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


def normalized_device_header():
    raw = request.headers.get("X-Device-Id", "").strip()
    if not raw or len(raw) > 200 or not re.fullmatch(r"[A-Za-z0-9._:-]+", raw):
        return ""
    return hashlib.sha256(f"{DOWNLOAD_SECRET}:device:{raw}".encode()).hexdigest()


def register_device(connection, user_id):
    """Register an opaque device identifier without storing browser fingerprints."""
    device_hash = normalized_device_header()
    if not device_hash:
        return {"ok": True, "deviceHash": ""}
    row = connection.execute(
        "SELECT id,revoked FROM user_devices WHERE user_id=? AND device_hash=?",
        (user_id, device_hash),
    ).fetchone()
    now = now_iso()
    label = str(request.headers.get("X-Device-Label", "Thiết bị") or "Thiết bị")[:80]
    platform = str(request.headers.get("X-Device-Platform", "") or "")[:40]
    if row:
        if row["revoked"]:
            return {"ok": False, "reason_code": "device_revoked"}
        connection.execute(
            "UPDATE user_devices SET label=?,platform=?,last_seen_at=? WHERE id=?",
            (label, platform, now, row["id"]),
        )
        return {"ok": True, "deviceHash": device_hash}
    active = connection.execute(
        "SELECT COUNT(*) FROM user_devices WHERE user_id=? AND revoked=0", (user_id,)
    ).fetchone()[0]
    if int(active) >= DEVICE_LIMIT:
        record_risk_event(connection, user_id, "device_limit", 25, device_hash, "device-limit")
        return {"ok": False, "reason_code": "device_limit"}
    connection.execute(
        """INSERT INTO user_devices(user_id,device_hash,label,platform,first_seen_at,last_seen_at)
           VALUES(?,?,?,?,?,?)""",
        (user_id, device_hash, label, platform, now, now),
    )
    return {"ok": True, "deviceHash": device_hash}


def record_risk_event(connection, user_id, code, score, device_hash="", request_id=""):
    safe_code = re.sub(r"[^a-z0-9_.-]", "", str(code).lower())[:50] or "unknown"
    safe_request = re.sub(r"[^A-Za-z0-9_.-]", "", str(request_id))[:100]
    connection.execute(
        """INSERT OR IGNORE INTO risk_events(user_id,device_hash,code,score,request_id,created_at)
           VALUES(?,?,?,?,?,?)""",
        (int(user_id), str(device_hash)[:64], safe_code, max(0, min(int(score), 100)), safe_request, now_iso()),
    )


def user_risk_payload(connection, user_id):
    since = (datetime.now(LOCAL_TZ) - timedelta(days=30)).replace(tzinfo=None).isoformat(timespec="seconds")
    row = connection.execute(
        "SELECT COALESCE(SUM(score),0),COUNT(*) FROM risk_events WHERE user_id=? AND created_at>=?",
        (int(user_id), since),
    ).fetchone()
    score = min(100, int(row[0] or 0))
    return {"score": score, "level": "high" if score >= 60 else "medium" if score >= 25 else "low", "events": int(row[1] or 0)}


def record_delivery(connection, user_id, kind, source="", inventory_id=None, request_id="", account=None):
    request_key = str(request_id or uuid.uuid4().hex)[:100]
    days = bounded_setting_int(connection, "delivery_warranty_days", 1, 0, 365)
    warranty_until = None
    if days:
        warranty_until = (datetime.now(LOCAL_TZ) + timedelta(days=days)).replace(tzinfo=None).isoformat(timespec="seconds")
    inserted = connection.execute(
        """INSERT OR IGNORE INTO delivery_events(
               user_id,kind,inventory_source,inventory_id,request_id,status,account_json,
               warranty_until,delivered_at
           ) VALUES(?,?,?,?,?,'delivered',?,?,?)""",
        (int(user_id), str(kind)[:30], str(source)[:20], inventory_id, request_key,
         json.dumps(public_account(account or {}), ensure_ascii=False, separators=(",", ":")),
         warranty_until, now_iso()),
    )
    if inserted.rowcount and source in COOKIE_TABLES and inventory_id is not None:
        connection.execute(
            f"UPDATE {COOKIE_TABLES[source]} SET delivery_count=delivery_count+1 WHERE id=?",
            (int(inventory_id),),
        )
    return connection.execute(
        "SELECT id,warranty_until FROM delivery_events WHERE user_id=? AND kind=? AND request_id=?",
        (int(user_id), str(kind)[:30], request_key),
    ).fetchone()


def inventory_outcome(connection, source, cookie_id, success=False, reason_code=""):
    table = COOKIE_TABLES.get(source)
    if not table or cookie_id is None:
        return
    reason = re.sub(r"[^a-z0-9_.-]", "", str(reason_code).lower())[:50]
    if success:
        connection.execute(
            f"""UPDATE {table} SET health_status='live',failure_count=0,last_error_code='',
                   quarantine_until=NULL,last_checked_at=? WHERE id=?""",
            (now_iso(), int(cookie_id)),
        )
        return
    if reason in {"cookie_expired", "cookie_invalid", "dead", "cookie_format"}:
        connection.execute(f"DELETE FROM {table} WHERE id=?", (int(cookie_id),))
        return
    quarantine_until = (datetime.now(LOCAL_TZ) + timedelta(minutes=15)).replace(tzinfo=None).isoformat(timespec="seconds")
    connection.execute(
        f"""UPDATE {table} SET is_used=0,health_status='quarantined',
               failure_count=failure_count+1,last_error_code=?,last_checked_at=?,quarantine_until=?
           WHERE id=?""",
        (reason or "temporary_error", now_iso(), quarantine_until, int(cookie_id)),
    )


def available_stock_count(connection, source):
    table = COOKIE_TABLES[source]
    return int(connection.execute(
        f"""SELECT COUNT(*) FROM {table} WHERE is_used=0 AND health_status!='dead'
            AND (quarantine_until IS NULL OR quarantine_until<=?)""",
        (now_iso(),),
    ).fetchone()[0])


def maybe_alert_low_stock(connection):
    if app.config.get("TESTING"):
        return
    threshold = bounded_setting_int(connection, "low_stock_threshold", 5, 0, 100000)
    if threshold <= 0:
        return
    now = time.monotonic()
    for source, table in COOKIE_TABLES.items():
        count = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE is_used=0 AND health_status!='dead' "
            "AND (quarantine_until IS NULL OR quarantine_until<=?)",
            (now_iso(),),
        ).fetchone()[0]
        if count > threshold:
            continue
        with LOW_STOCK_ALERT_LOCK:
            if now - LOW_STOCK_ALERTS.get(source, 0) < 3600:
                continue
            LOW_STOCK_ALERTS[source] = now
        telegram_notify(f"⚠️ <b>Kho {html.escape(source)}</b> chỉ còn {int(count)} mục khả dụng.")


def run_maintenance_tasks(connection, force=False):
    global LAST_MAINTENANCE_RUN
    now_monotonic = time.monotonic()
    with MAINTENANCE_LOCK:
        if not force and now_monotonic - LAST_MAINTENANCE_RUN < 300:
            return
        LAST_MAINTENANCE_RUN = now_monotonic
    connection.execute(
        """UPDATE transactions SET status='EXPIRED',review_note='Đã hết thời gian thanh toán'
           WHERE status='AWAITING_PAYMENT' AND payment_deadline IS NOT NULL AND payment_deadline<?""",
        (now_iso(),),
    )
    day_key = f"daily:{local_today()}"
    inserted = connection.execute(
        "INSERT OR IGNORE INTO automation_runs(run_key,completed_at) VALUES(?,?)",
        (day_key, now_iso()),
    )
    if inserted.rowcount:
        tomorrow = (datetime.now(LOCAL_TZ) + timedelta(days=1)).replace(tzinfo=None).isoformat(timespec="seconds")
        expiring = connection.execute(
            """SELECT id,user_id,plan_name,warranty_until FROM purchase_history
               WHERE warranty_until IS NOT NULL AND warranty_until>=? AND warranty_until<=?""",
            (now_iso(), tomorrow),
        ).fetchall()
        for row in expiring:
            notify(connection, row["user_id"], "warranty_expiring", "Bảo hành sắp hết hạn", f"Đơn #{row['id']} · {row['plan_name']} sắp hết thời hạn bảo hành")
    connection.commit()
    maybe_alert_low_stock(connection)
    if not app.config.get("TESTING") and os.path.exists(DATABASE_PATH):
        backup_dir = os.path.join(BASE_DIR, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(backup_dir, f"auto-{local_today()}.db")
        if not os.path.exists(backup_path):
            source = sqlite3.connect(DATABASE_PATH, timeout=30)
            target = sqlite3.connect(backup_path, timeout=30)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            connection.execute(
                "INSERT INTO service_events(service,status,details,created_at) VALUES('backup','ok','daily backup created',?)",
                (now_iso(),),
            )
            connection.commit()
            connection.commit()


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
    except (OSError, URLError) as error:
        app.logger.warning("Unable to notify Telegram admin type=%s", type(error).__name__)
        return False


def telegram_notify(text, reply_markup=None):
    """Best-effort admin notification; the user flow must not depend on Telegram delivery."""
    return telegram_send(os.getenv("TELEGRAM_ADMIN_ID", "").strip(), text, reply_markup)


class ToolError(Exception):
    def __init__(self, message, status=400, reason_code="tool_error"):
        super().__init__(message)
        self.status = status
        self.reason_code = reason_code


COOKIE_TABLES = {"premium": "premium_cookies", "free": "free_cookies"}


def nftoken_cookie_order(allow_free=False):
    if not allow_free:
        return ("premium",)
    if random.randrange(100) < TRIAL_NFTOKEN_FREE_COOKIE_PERCENT:
        return ("free", "premium")
    return ("premium", "free")


def reserve_cookie_row(connection, sources, user_id=None):
    for source in sources:
        table = COOKIE_TABLES.get(source)
        if not table:
            continue
        params = [now_iso()]
        user_filter = ""
        if user_id is not None:
            user_filter = """AND NOT EXISTS(
                SELECT 1 FROM delivery_events d
                WHERE d.user_id=? AND d.inventory_source=? AND d.inventory_id={table}.id
                  AND d.status IN ('delivered','warranty_approved')
            )""".format(table=table)
            params.extend([int(user_id), source])
        row = connection.execute(
            f"""SELECT id,data FROM {table}
                WHERE is_used=0 AND health_status!='dead'
                  AND (quarantine_until IS NULL OR quarantine_until<=?)
                  {user_filter}
                ORDER BY CASE health_status WHEN 'live' THEN 0 ELSE 1 END,
                         failure_count ASC,RANDOM() LIMIT 1""",
            params,
        ).fetchone()
        if not row:
            continue
        updated = connection.execute(
            f"UPDATE {table} SET is_used=1 WHERE id=? AND is_used=0", (row["id"],)
        )
        if updated.rowcount == 1:
            return row["id"], row["data"], source
    return None


def reserve_cookie(connection, user_id=None):
    connection.execute("BEGIN IMMEDIATE")
    reserved = reserve_cookie_row(connection, ("premium",), user_id=user_id)
    if not reserved:
        connection.rollback()
        raise ToolError("Kho Cookie Premium đang trống", 409)
    connection.commit()
    return reserved[0], reserved[1]


def reserve_nftoken_cookie(connection, allow_free=False, user_id=None):
    connection.execute("BEGIN IMMEDIATE")
    reserved = reserve_cookie_row(connection, nftoken_cookie_order(allow_free), user_id=user_id)
    if not reserved:
        connection.rollback()
        raise ToolError("Kho Cookie tạo NFToken đang trống", 409, "stock_empty")
    connection.commit()
    return reserved


def reserve_nftoken_request(connection, user_id, mode):
    today = local_today()
    connection.execute("BEGIN IMMEDIATE")
    trial = trial_settings(connection)
    trial_kind = "cookie" if mode == "vip" else "nftoken"
    quota_source = None
    if trial[f"{trial_kind}Enabled"] and trial[f"{trial_kind}DailyLimit"] > 0:
        connection.execute(
            "INSERT OR IGNORE INTO trial_usage(user_id,local_date) VALUES(?,?)",
            (user_id, today),
        )
        trial_column = "cookie_used" if mode == "vip" else "nftoken_used"
        consumed = connection.execute(
            f"UPDATE trial_usage SET {trial_column}={trial_column}+1 "
            f"WHERE user_id=? AND local_date=? AND {trial_column}<?",
            (user_id, today, trial[f"{trial_kind}DailyLimit"]),
        )
        if consumed.rowcount == 1:
            quota_source = {"kind": f"trial_{trial_kind}", "date": today}
    if mode == "vip":
        if quota_source is None:
            updated = connection.execute(
                "UPDATE users SET credits=credits-1 WHERE user_id=? AND credits>0", (user_id,)
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise ToolError("Bạn đã hết lượt Cookie VIP", 409, "cookie_quota_exhausted")
            quota_source = {"kind": "vip", "date": today}
    elif quota_source is None:
        paid = connection.execute(
            """UPDATE users SET nftoken_credits=nftoken_credits-1
               WHERE user_id=? AND nftoken_credits>0""",
            (user_id,),
        )
        if paid.rowcount == 1:
            quota_source = {"kind": "paid_nftoken", "date": today}
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
                raise ToolError("Bạn đã hết lượt tạo NFToken", 409, "nftoken_quota_exhausted")
            quota_source = {"kind": "daily_nftoken", "date": today}
    allow_free = quota_source.get("kind") == "trial_nftoken"
    reserved = reserve_cookie_row(connection, nftoken_cookie_order(allow_free), user_id=user_id)
    if not reserved:
        connection.rollback()
        raise ToolError("Kho Cookie tạo NFToken đang trống", 409, "stock_empty")
    connection.commit()
    return reserved[0], reserved[1], reserved[2], quota_source


def refund_nftoken_request(connection, user_id, quota_source):
    source = quota_source if isinstance(quota_source, dict) else {"kind": quota_source, "date": local_today()}
    kind = source.get("kind")
    source_date = source.get("date") or local_today()
    connection.execute("BEGIN IMMEDIATE")
    if kind == "vip":
        connection.execute("UPDATE users SET credits=credits+1 WHERE user_id=?", (user_id,))
    elif kind == "paid_nftoken":
        connection.execute(
            "UPDATE users SET nftoken_credits=nftoken_credits+1 WHERE user_id=?",
            (user_id,),
        )
    elif kind in {"trial_nftoken", "trial_cookie"}:
        trial_column = "nftoken_used" if kind == "trial_nftoken" else "cookie_used"
        connection.execute(
            f"UPDATE trial_usage SET {trial_column}=MAX(0,{trial_column}-1) WHERE user_id=? AND local_date=?",
            (user_id, source_date),
        )
    else:
        connection.execute(
            """UPDATE usage SET tokens_used=MAX(0,tokens_used-1)
               WHERE user_id=? AND date=?""",
            (user_id, source_date),
        )
    connection.commit()


def release_cookie(connection, cookie_id, delete=False, source="premium"):
    table = COOKIE_TABLES.get(source)
    if not table:
        raise ValueError("Cookie source không hợp lệ")
    connection.execute("BEGIN IMMEDIATE")
    if delete:
        connection.execute(f"DELETE FROM {table} WHERE id=?", (cookie_id,))
    else:
        connection.execute(f"UPDATE {table} SET is_used=0 WHERE id=?", (cookie_id,))
    connection.commit()


def run_cookie_check(cookie_data, timeout=None, request_id=None):
    """Lazy import keeps normal Mini App startup light and makes the checker testable."""
    from code_goc import checker

    parsed = checker.extract_cookies_from_text(cookie_data)
    if not parsed:
        return False, None, "Cookie sai định dạng", {}, None
    cookies = parsed[0]
    check_timeout = COOKIE_CHECK_TIMEOUT if timeout is None else max(0.01, float(timeout))
    check_timeout = min(COOKIE_CHECK_TIMEOUT, check_timeout)
    worker_deadline = time.monotonic() + check_timeout
    # The Mini App must fail fast when the VPS cannot reach Netflix.  The bot's
    # normal checker keeps its existing retry policy; only this bounded worker
    # uses a no-retry session and a deadline.
    request_timeout = min(8.0, check_timeout)
    future = COOKIE_CHECK_EXECUTOR.submit(
        checker.check_cookie,
        cookies,
        request_timeout=request_timeout,
        max_retries=0,
        deadline=worker_deadline,
    )
    try:
        success, token, error, account = future.result(timeout=check_timeout)
    except FutureTimeoutError:
        future.cancel()
        app.logger.warning(
            "NFToken check timed out request_id=%s after=%ss",
            request_id or "-",
            check_timeout,
        )
        return False, None, "network_timeout", {}, None
    netscape = checker.build_netscape_format(cookies) if success and token else None
    return success, token, error, account, netscape


def nftoken_failure_reason(error):
    value = str(error or "").strip().lower()
    if value == "network_timeout" or "timeout" in value or "thời gian chờ" in value or "phản hồi quá chậm" in value:
        return "nftoken_timeout"
    if "connection" in value or "kết nối" in value or "network" in value:
        return "network_error"
    if value in {"dead", "cookie_format"} or "cookie hết hạn" in value or "401" in value or "permission_denied" in value:
        return "cookie_expired"
    if "cookie" in value and ("invalid" in value or "không hợp lệ" in value or "sai định dạng" in value):
        return "cookie_invalid"
    return "nftoken_failed"


def nftoken_failure_message(reason_code):
    return {
        "nftoken_timeout": "Máy chủ xử lý quá lâu, vui lòng thử lại",
        "network_error": "Không thể kết nối Netflix, vui lòng thử lại sau",
        "cookie_expired": "Cookie đã hết hạn hoặc không còn phiên hợp lệ",
        "cookie_invalid": "Cookie không đúng định dạng",
    }.get(reason_code, "Không tạo được NFToken lúc này, vui lòng thử lại")


def cookie_should_delete(error, reason_code):
    return reason_code in {"cookie_expired", "cookie_invalid"} or str(error or "").strip().lower() == "dead"


def run_tv_login(cookie_data, tv_code):
    from code_goc import checker, process_tv_login

    parsed = checker.extract_cookies_from_text(cookie_data)
    if not parsed:
        return False, "cookie_format", "Cookie sai định dạng", {}
    return process_tv_login(parsed[0], tv_code)


def legacy_public_account(account):
    return {
        "email": account.get("email_masked", "Netflix không cung cấp"),
        "plan": account.get("plan", "Netflix không cung cấp"),
        "country": account.get("country", "Netflix không cung cấp"),
        "status": normalize_membership_status(account.get("membership_status")),
        "quality": account.get("video_quality", "Netflix không cung cấp"),
        "profiles": account.get("profile_count", "Không có profile"),
    }


def public_account(account):
    """Expose complete safe account metadata, never cookies or session tokens."""
    account = normalize_account_payload(account)

    def repair_mojibake(text):
        if not isinstance(text, str):
            return text
        repaired = text
        for _ in range(2):
            if not any(marker in repaired for marker in ("Ã", "Â", "Ä", "Å", "Æ", "â€", "ðŸ", "á»")):
                break
            candidate = None
            for encoding in ("latin-1", "cp1252"):
                try:
                    candidate = repaired.encode(encoding).decode("utf-8")
                    break
                except (UnicodeEncodeError, UnicodeDecodeError):
                    continue
            if candidate is None:
                break
            if candidate == repaired:
                break
            repaired = candidate
        return repaired

    def value(key, fallback="Netflix không cung cấp"):
        raw = account.get(key, fallback)
        if raw is None or raw == "" or str(raw).strip().casefold() in {"n/a", "na", "none", "null", "unknown", "xx"}:
            return fallback
        if isinstance(raw, (dict, list)):
            return ", ".join(str(item) for item in raw) if isinstance(raw, list) else str(raw)
        return repair_mojibake(str(raw))

    def is_available(item):
        return str(item or "").strip().casefold() not in {
            "", "n/a", "na", "none", "null", "unknown", "xx",
            "không rõ", "netflix không cung cấp", "không có profile",
        }

    profiles = account.get("profiles") or []
    details = [
        ("Email", value("email_masked")),
        ("Số điện thoại", value("phone")),
        ("Quốc gia", value("country")),
        ("Tiền tệ", value("country_currency")),
        ("Trạng thái", normalize_membership_status(account.get("membership_status"))),
        ("Gói cước", value("plan")),
        ("Giá gói", value("plan_price")),
        ("Ngày tham gia", value("member_since")),
        ("Kỳ thanh toán tiếp theo", value("next_billing")),
        ("Phương thức thanh toán", value("payment_method")),
        ("Loại thẻ", value("cc_type")),
        ("4 số cuối", value("last4")),
        ("Tạm giữ thanh toán", value("payment_on_hold")),
        ("Chất lượng video", value("video_quality")),
        ("Số luồng tối đa", value("max_streams")),
        ("Extra Member", value("extra_member")),
        ("Số slot Extra Member", value("extra_member_slots")),
        ("Số profile", value("profile_count", str(len(profiles)))),
        ("Profiles", ", ".join(str(item) for item in profiles) if profiles else "Không có profile"),
    ]
    details = [
        {"label": label, "value": item}
        for label, item in details
        if is_available(item)
    ]
    email = value("email_masked")
    plan = value("plan")
    country = value("country")
    status = normalize_membership_status(account.get("membership_status"))
    quality = value("video_quality")
    profile_value = value("profile_count", str(len(profiles)))
    return {
        "email": email if is_available(email) else "",
        "plan": plan if is_available(plan) else "",
        "country": country if is_available(country) else "",
        "status": status if is_available(status) else "",
        "quality": quality if is_available(quality) else "",
        "profiles": profile_value if is_available(profile_value) else "",
        "details": details,
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


RANK_ORDER = ("Bronze", "Silver", "Platinum", "Diamond")
PROVIDER_INSTANCES = {}


def ensure_referral_code(connection, user_id):
    row = connection.execute("SELECT referral_code FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row and row[0]:
        return row[0]
    digest = hashlib.sha256(f"nftoken-referral:{user_id}".encode()).hexdigest()[:10].upper()
    code = f"NF{digest}"
    connection.execute("UPDATE users SET referral_code=? WHERE user_id=?", (code, user_id))
    return code


def register_referral(connection, user_id, start_param):
    value = str(start_param or "").strip()
    if value.startswith(("ref_", "ref-")):
        value = value[4:].upper()
    if not value or len(value) > 40:
        return
    own_code = ensure_referral_code(connection, user_id)
    if value == own_code:
        return
    referrer = connection.execute("SELECT user_id FROM users WHERE referral_code=?", (value,)).fetchone()
    if not referrer or int(referrer[0]) == user_id:
        return
    try:
        connection.execute(
            """INSERT INTO referral_events(referrer_id,referred_id,referral_code,status,created_at,qualified_at)
               VALUES(?,?,?,'qualified',?,?)""",
            (int(referrer[0]), user_id, value, now_iso(), now_iso()),
        )
    except sqlite3.IntegrityError:
        return
    connection.execute(
        "UPDATE users SET referred_by=?,referral_qualified=1,referral_joined_at=? WHERE user_id=? AND referred_by IS NULL",
        (int(referrer[0]), now_iso(), user_id),
    )
    count = connection.execute(
        "SELECT COUNT(*) FROM referral_events WHERE referrer_id=? AND status='qualified'", (int(referrer[0]),)
    ).fetchone()[0]
    if count >= 5:
        event_key = f"{int(referrer[0])}:5"
        inserted = connection.execute(
            "INSERT OR IGNORE INTO referral_rewards(referrer_id,milestone,credits,event_key,created_at) VALUES(?,?,?,?,?)",
            (int(referrer[0]), 5, 2, event_key, now_iso()),
        )
        if inserted.rowcount:
            connection.execute("UPDATE users SET nftoken_credits=nftoken_credits+2 WHERE user_id=?", (int(referrer[0]),))
            connection.execute("UPDATE referral_events SET reward_credits=2 WHERE referrer_id=? AND reward_credits=0", (int(referrer[0]),))


def rank_payload(connection, user_id):
    def field(row, name, index):
        return row[name] if hasattr(row, "keys") else row[index]

    spent = connection.execute(
        "SELECT COALESCE(SUM(CASE WHEN final_price>0 THEN final_price ELSE price END),0) FROM purchase_history WHERE user_id=?",
        (user_id,),
    ).fetchone()[0]
    referrals = connection.execute(
        "SELECT COUNT(*) FROM referral_events WHERE referrer_id=? AND status='qualified'", (user_id,)
    ).fetchone()[0]
    settings = connection.execute(
        "SELECT rank,referral_threshold,spend_threshold,benefits FROM customer_rank_settings"
    ).fetchall()
    by_rank = {field(row, "rank", 0): row for row in settings}
    current = "Bronze"
    for rank in RANK_ORDER:
        row = by_rank.get(rank)
        if row and (referrals >= field(row, "referral_threshold", 1) or spent >= field(row, "spend_threshold", 2)):
            current = rank
    next_rank = next((rank for rank in RANK_ORDER if RANK_ORDER.index(rank) > RANK_ORDER.index(current)), None)
    progress = {"referrals": referrals, "spent": spent}
    if next_rank:
        target = by_rank[next_rank]
        progress.update({"next": next_rank, "referralsTarget": field(target, "referral_threshold", 1), "spentTarget": field(target, "spend_threshold", 2)})
    row = by_rank.get(current)
    return {"name": current, "benefits": field(row, "benefits", 3) if row else "", "progress": progress}


def referral_payload(connection, user_id):
    code = ensure_referral_code(connection, user_id)
    rows = connection.execute(
        "SELECT referred_id,status,reward_credits,created_at FROM referral_events WHERE referrer_id=? ORDER BY id DESC LIMIT 100",
        (user_id,),
    ).fetchall()
    rewards = connection.execute(
        "SELECT milestone,credits,created_at FROM referral_rewards WHERE referrer_id=? ORDER BY id DESC", (user_id,)
    ).fetchall()
    return {"code": code, "link": f"https://t.me/{os.getenv('TELEGRAM_BOT_USERNAME', '').lstrip('@')}?start=ref_{code}",
            "count": len(rows), "required": 5, "totalReward": sum(row["credits"] for row in rewards),
            "items": [dict(row) for row in rows], "rewards": [dict(row) for row in rewards]}


def checkin_payload(connection, user_id):
    date = local_today()
    row = connection.execute("SELECT claimed FROM free_cookie_checkins WHERE user_id=? AND local_date=?", (user_id, date)).fetchone()
    daily = int(app_setting(connection, "free_cookie_daily_limit", "2") or 2)
    return {"date": date, "daily": daily, "remaining": row["claimed"] if row else 0, "checkedIn": bool(row)}


def copyright_payload(connection):
    row = connection.execute("SELECT enabled,text FROM copyright_settings WHERE id=1").fetchone()
    return {"enabled": bool(row["enabled"]) if row else True, "text": row["text"] if row else "© mnhut - NFToken Pro\nBản quyền nội dung xuất bởi hệ thống NFToken Pro"}


def download_expiry_seconds():
    try:
        return max(60, min(int(os.getenv("DOWNLOAD_LINK_TTL_SECONDS", "3600")), 7 * 86400))
    except ValueError:
        return 3600


def create_secure_download(connection, user_id, order_id, content, filename, content_type="text/plain; charset=utf-8", max_downloads=1):
    expires_at = int(time.time()) + download_expiry_seconds()
    token = signed_download_token(DOWNLOAD_SECRET, user_id, order_id, expires_at)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    connection.execute(
        """INSERT INTO secure_downloads(token_hash,user_id,order_id,filename,content,content_type,expires_at,max_downloads,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (token_hash, int(user_id), int(order_id), safe_filename(filename), sqlite3.Binary(str(content).encode("utf-8")), content_type, datetime.fromtimestamp(expires_at, APP_TZ).replace(tzinfo=None).isoformat(timespec="seconds"), max(1, int(max_downloads)), now_iso()),
    )
    return f"/api/download/{quote(token, safe='')}"


def active_flash_sale(connection, item_id, now=None):
    stamp = (now or local_now()).replace(tzinfo=None).isoformat(timespec="seconds")
    return connection.execute(
        """SELECT * FROM flash_sales
           WHERE store_item_id=? AND active=1 AND starts_at<=? AND ends_at>? AND quantity_sold<quantity_limit
           ORDER BY discount_percent DESC,id DESC LIMIT 1""",
        (int(item_id), stamp, stamp),
    ).fetchone()


def priced_item(connection, row):
    sale = active_flash_sale(connection, row["id"])
    item = product_dict(row)
    if sale:
        item["regularPrice"] = item["price"]
        item["price"] = flash_price(item["price"], sale["discount_percent"])
        item["flashSale"] = {"id": sale["id"], "name": sale["name"], "discountPercent": sale["discount_percent"], "quantityRemaining": max(0, sale["quantity_limit"] - sale["quantity_sold"]), "startsAt": sale["starts_at"], "endsAt": sale["ends_at"], "allowPromo": bool(sale["allow_promo"])}
    else:
        item["flashSale"] = None
    return item


def notify(connection, user_id, kind, title, body="", payload=None):
    connection.execute("INSERT INTO notifications(user_id,kind,title,body,payload_json,created_at) VALUES(?,?,?,?,?,?)", (user_id, kind, title[:200], body[:2000], json.dumps(payload or {}, ensure_ascii=False), now_iso()))


def mission_progress(connection, user_id, mission):
    condition = json.loads(mission["condition_json"] or "{}")
    kind = condition.get("type")
    if kind == "checkin":
        return bool(connection.execute("SELECT 1 FROM free_cookie_checkins WHERE user_id=? AND local_date=?", (user_id, local_today())).fetchone())
    if kind == "first_order":
        return bool(connection.execute("SELECT 1 FROM purchase_history WHERE user_id=? AND status NOT IN ('CANCELLED','FAILED')", (user_id,)).fetchone())
    if kind == "referral":
        count = connection.execute("SELECT COUNT(*) FROM referral_events e JOIN users u ON u.user_id=e.referred_id WHERE e.referrer_id=? AND e.status='qualified' AND u.is_banned=0", (user_id,)).fetchone()[0]
        return count >= int(condition.get("minimum", 1))
    if kind == "promotion":
        return bool(connection.execute("SELECT 1 FROM purchase_history WHERE user_id=? AND promo_code<>''", (user_id,)).fetchone())
    return False


def calculate_promo(connection, user_id, code, items):
    code = str(code or "").strip().upper()
    original = sum(int(item["lineTotal"]) for item in items)
    if not code:
        return {"code": "", "percent": 0, "original": original, "discount": 0, "final": original}
    row = connection.execute(
        "SELECT * FROM discount_codes WHERE UPPER(code)=? ORDER BY rowid DESC LIMIT 1",
        (code,),
    ).fetchone()
    if not row or str(row["code_type"] or "BALANCE").upper() != "PERCENT":
        raise ToolError("Mã giảm giá không hợp lệ", 400)
    percent = int(row["percent"] or 0)
    now = datetime.now(LOCAL_TZ).replace(tzinfo=None)
    for field, before, after in (("starts_at", now, "start"), ("ends_at", now, "end")):
        if row[field]:
            try:
                stamp = datetime.fromisoformat(row[field])
            except ValueError:
                raise ToolError("Mã giảm giá có thời hạn không hợp lệ", 400)
            if (field == "starts_at" and now < stamp) or (field == "ends_at" and now > stamp):
                raise ToolError("Mã giảm giá đã hết hạn hoặc chưa bắt đầu", 409)
    if not 1 <= percent <= 100 or original < int(row["min_order_total"] or 0):
        raise ToolError("Mã giảm giá không áp dụng cho đơn này", 409)
    selected = {int(value) for value in str(row["product_ids"] or "").split(",") if value.strip().isdigit()}
    if selected and any(int(item["id"]) not in selected for item in items):
        raise ToolError("Mã giảm giá không áp dụng cho sản phẩm này", 409)
    if any(item.get("flashSale") and not item["flashSale"].get("allowPromo") for item in items):
        raise ToolError("Flash Sale không cho dùng mã giảm giá", 409)
    if int(row["per_user"] or 1) and connection.execute("SELECT 1 FROM discount_redemptions WHERE code=? AND user_id=?", (code, user_id)).fetchone():
        raise ToolError("Bạn đã sử dụng mã này", 409)
    if int(row["uses"] or 0) <= connection.execute("SELECT COUNT(*) FROM discount_redemptions WHERE code=?", (code,)).fetchone()[0]:
        raise ToolError("Mã giảm giá đã hết lượt", 409)
    discount = min(original, original * percent // 100)
    return {"code": code, "percent": percent, "original": original, "discount": discount, "final": original - discount}


def provider_for(row):
    provider_id = int(row["provider_id"] if hasattr(row, "keys") and "provider_id" in row.keys() else row["id"])
    if provider_id in PROVIDER_INSTANCES:
        return PROVIDER_INSTANCES[provider_id]
    return provider_from_row(row)


def provider_candidates(connection, item, primary_id):
    external_id = str(item.get("externalProductId") or "")
    rows = connection.execute(
        """SELECT p.* FROM product_providers p
           JOIN store s ON s.provider_id=p.id
           WHERE p.enabled=1 AND s.external_product_id=? AND s.active=1
           ORDER BY CASE WHEN p.id=? THEN 0 ELSE 1 END, p.priority ASC, p.id ASC""",
        (external_id, int(primary_id)),
    ).fetchall()
    if not rows:
        row = connection.execute("SELECT * FROM product_providers WHERE id=? AND enabled=1", (primary_id,)).fetchone()
        return [row] if row else []
    seen = set()
    return [row for row in rows if not (row["id"] in seen or seen.add(row["id"]))]


def record_provider_attempt(checkout_key, provider_id, external_product_id, status, latency_ms, error_code=""):
    """Persist provider telemetry in its own short transaction, never around network I/O."""
    audit_connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    try:
        audit_connection.execute("PRAGMA busy_timeout=30000")
        audit_connection.execute(
            """INSERT INTO provider_attempts(
                   checkout_key,provider_id,external_product_id,status,latency_ms,error_code,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (checkout_key, int(provider_id), str(external_product_id), status, int(latency_ms), str(error_code)[:50], now_iso()),
        )
        audit_connection.execute(
            "UPDATE product_providers SET last_error_code=?,last_response_ms=?,last_checked_at=? WHERE id=?",
            (str(error_code)[:50], int(latency_ms), now_iso(), int(provider_id)),
        )
        audit_connection.commit()
    finally:
        audit_connection.close()


def create_provider_order_with_fallback(connection, item, user_id, checkout_key, quantity=1):
    primary_id = item.get("providerId")
    if not primary_id or not item.get("externalProductId"):
        raise ProviderError("provider_config_invalid", "Sản phẩm chưa được cấu hình nhà cung cấp")
    candidates = provider_candidates(connection, item, primary_id)
    if not candidates:
        raise ProviderError("provider_unavailable", "Nhà cung cấp không khả dụng")
    last_error = None
    for candidate in candidates:
        started = time.perf_counter()
        try:
            result = provider_for(candidate).create_order(
                item["externalProductId"], quantity, checkout_key,
                {"checkout_key": checkout_key, "customer_email": item.get("customerEmail", "")},
            )
            status = str(result.get("status", "fulfilled")).lower()
            if status in {"failed", "rejected", "error"}:
                raise ProviderError("provider_rejected", "Nhà cung cấp từ chối yêu cầu")
            elapsed = int((time.perf_counter() - started) * 1000)
            record_provider_attempt(checkout_key, candidate["id"], item["externalProductId"], "success", elapsed)
            return candidate, result
        except ProviderError as error:
            elapsed = int((time.perf_counter() - started) * 1000)
            last_error = error
            code = getattr(error, "reason_code", "provider_error")
            record_provider_attempt(checkout_key, candidate["id"], item["externalProductId"], "failed", elapsed, code)
            if not retryable_provider_error(code):
                raise
    raise last_error or ProviderError("provider_unavailable")


def auto_product_image(name, category):
    """Create a lightweight product artwork when Admin leaves image_url empty."""
    title = html.escape(str(name or "NFToken")[:28])
    label = html.escape(str(category or "Gói dịch vụ")[:32])
    value = f"{name or ''} {category or ''}".lower()
    if "spotify" in value:
        glyph = "♫"
    elif "netflix" in value or "cookie" in value:
        glyph = "N"
    elif "vip" in value:
        glyph = "✦"
    elif "token" in value:
        glyph = "◆"
    else:
        glyph = "N"
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 420">
      <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#09090b"/><stop offset=".58" stop-color="#27272a"/><stop offset="1" stop-color="#52525b"/></linearGradient></defs>
      <rect width="720" height="420" rx="42" fill="url(#g)"/>
      <circle cx="560" cy="92" r="130" fill="none" stroke="#fff" stroke-opacity=".16" stroke-width="2"/>
      <path d="M-40 330 Q190 80 430 330 T800 210" fill="none" stroke="#fff" stroke-opacity=".15" stroke-width="3"/>
      <text x="52" y="92" fill="#fff" fill-opacity=".68" font-family="Arial,sans-serif" font-size="24" letter-spacing="4">NFTOKEN PRO</text>
      <text x="52" y="270" fill="#fff" font-family="Arial,sans-serif" font-size="150" font-weight="700">{glyph}</text>
      <text x="570" y="330" text-anchor="end" fill="#fff" font-family="Arial,sans-serif" font-size="42" font-weight="700">{title}</text>
      <text x="570" y="365" text-anchor="end" fill="#fff" fill-opacity=".62" font-family="Arial,sans-serif" font-size="18" letter-spacing="2">{label}</text>
    </svg>'''
    return "data:image/svg+xml;charset=UTF-8," + quote(svg, safe="")


def product_dict(row, include_auto_image=True):
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
        "imageUrl": (row["image_url"] or auto_product_image(row["name"], row["category"])) if include_auto_image else (row["image_url"] or ""),
        "featured": bool(row["featured"]),
        "warrantyDays": row["warranty_days"] or 0,
        "available": bool(row["active"]),
        "purchases": row["purchases"] or 0,
        "providerId": row["provider_id"] if "provider_id" in row.keys() else None,
        "externalProductId": row["external_product_id"] if "external_product_id" in row.keys() else "",
        "requiresCustomerEmail": bool(row["requires_customer_email"] if "requires_customer_email" in row.keys() else 0),
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
    if request.method == "POST" and request.path == "/api/tools/nftoken":
        payload = response.get_json(silent=True) or {}
        raw_reason = payload.get("reason_code") or ("ok" if payload.get("ok") else "http_error")
        reason_code = "".join(
            char for char in str(raw_reason)[:40] if char.isalnum() or char in "._-"
        ) or "http_error"
        started = getattr(g, "nftoken_request_started", time.monotonic())
        duration_ms = int(max(0, (time.monotonic() - started) * 1000))
        trace = (
            f"NFToken request_id={getattr(g, 'nftoken_request_id', '-')} "
            f"status={response.status_code} reason={reason_code} duration_ms={duration_ms}"
        )
        print(trace, file=sys.stderr, flush=True)
    return response


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(os.path.join(STATIC_DIR, "assets"), filename)


@app.get("/manifest.json")
def manifest():
    return send_from_directory(STATIC_DIR, "manifest.json", mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    response = send_from_directory(STATIC_DIR, "sw.js", mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/health")
def health():
    database_ok = True
    try:
        connection = db()
        connection.execute("SELECT 1").fetchone()
        run_maintenance_tasks(connection)
    except Exception as error:
        database_ok = False
        app.logger.error("Health maintenance failed type=%s", type(error).__name__)
    return jsonify({
        "ok": database_ok,
        "service": "Shop MMO Mini App",
        "runtime_version": TV_LOGIN_RUNTIME_VERSION,
        "source_fingerprint": SOURCE_FINGERPRINT,
        "database": "ok" if database_ok else "error",
        "uptimeSeconds": max(0, int(time.time() - PROCESS_STARTED_AT)),
    }), (200 if database_ok else 503)


@app.get("/api/bootstrap")
@authenticated
def bootstrap():
    connection = db()
    user_id = ensure_user(connection, g.telegram_user)
    connection.commit()
    user = connection.execute(
        "SELECT balance,credits,nftoken_credits,plan_name,language FROM users WHERE user_id=?", (user_id,)
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
    stock = available_stock_count(connection, "premium")
    quota = quota_payload(connection, user_id)
    ensure_referral_code(connection, user_id)
    connection.commit()
    brand_row = connection.execute("SELECT filename,version FROM brand_assets WHERE id=1").fetchone()
    return jsonify(
        {
            "ok": True,
            "brand": {"name": "Shop MMO", "tagline": "Premium MMO Store"},
            "brandAsset": (f"/uploads/brand/{brand_row['filename']}?v={brand_row['version']}" if brand_row and brand_row["filename"] else ""),
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
                "rank": rank_payload(connection, user_id),
                "language": normalize_language(user["language"] or g.telegram_user.get("language_code", "vi")),
            },
            "inventory": {"premiumCookies": stock},
            "quota": quota,
            "support": os.getenv("SUPPORT_USERNAME", "@mnhutdznecon"),
            "isAdmin": user_id == configured_admin_id(),
            "copyright": copyright_payload(connection),
            "referral": referral_payload(connection, user_id),
            "checkin": checkin_payload(connection, user_id),
            "features": feature_flags(connection),
            "announcement": app_setting(connection, "announcement", ""),
        }
    )


def quota_payload(connection, user_id):
    today = local_today()
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
        "freeCheckinRemaining": checkin_payload(connection, user_id)["remaining"],
        "freeCheckinDaily": checkin_payload(connection, user_id)["daily"],
        "trial": trial_payload(connection, user_id),
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
        {"ok": True, "items": [priced_item(db(), row) for row in rows], "categories": categories, "page": page}
    )


@app.get("/api/products/<int:item_id>")
@authenticated
def product(item_id):
    row = db().execute("SELECT * FROM store WHERE id=? AND active=1", (item_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Sản phẩm không tồn tại"}), 404
    return jsonify({"ok": True, "item": priced_item(db(), row)})


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
        item = priced_item(connection, row)
        item["quantity"] = row["quantity"]
        item["lineTotal"] = item["price"] * row["quantity"]
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
        body = json_body()
        key = str(body.get("idempotencyKey", "")).strip()
        promo_code = str(body.get("promoCode", "")).strip().upper()
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if not (16 <= len(key) <= 100) or not all(c.isalnum() or c in "-_" for c in key):
        return jsonify({"ok": False, "error": "Mã xác nhận không hợp lệ"}), 400
    user_id = int(g.telegram_user["id"])
    if checkout_rate_limited(user_id):
        connection = db()
        record_risk_event(connection, user_id, "checkout_rate_limit", 5, getattr(g, "device_hash", ""), str(int(time.time()) // 10))
        connection.commit()
        return jsonify({"ok": False, "error": "Bạn thao tác quá nhanh, vui lòng thử lại"}), 429
    connection = db()
    provider_orders = []

    def refund_provider_results():
        for provider_row, _item, _provider_key, result in provider_orders:
            try:
                external_id = result.get("order_id", result.get("id", ""))
                if external_id:
                    provider_for(provider_row).refund(external_id)
            except Exception as error:
                app.logger.warning("Provider refund failed type=%s", type(error).__name__)

    try:
        existing = connection.execute(
            "SELECT order_ids, total FROM miniapp_checkouts WHERE user_id=? AND idempotency_key=?",
            (user_id, key),
        ).fetchone()
        if existing:
            return jsonify({"ok": True, "duplicate": True, "orderIds": json.loads(existing[0]), "total": existing[1]})
        cart = cart_payload(connection, user_id)
        if not cart["items"]:
            return jsonify({"ok": False, "error": "Giỏ hàng đang trống"}), 400
        customer_emails = required_customer_emails(body, cart["items"])
        for item in cart["items"]:
            item["customerEmail"] = customer_emails.get(int(item["id"]), "")
        promo = calculate_promo(connection, user_id, promo_code, cart["items"])
        user = connection.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            return jsonify({"ok": False, "error": "Không tìm thấy tài khoản"}), 404
        if user["balance"] < promo["final"]:
            return jsonify({"ok": False, "error": "Số dư không đủ", "required": promo["final"], "balance": user["balance"]}), 409
        snapshot = [
            (int(item["id"]), int(item["quantity"]), int(item["price"]), int(item["lineTotal"]),
             int(item.get("providerId") or 0), str(item.get("externalProductId") or ""))
            for item in cart["items"]
        ]
        # No SQLite write transaction is active while external providers run.
        for item in cart["items"]:
            provider_id = item.get("providerId")
            if not provider_id:
                continue
            for index in range(int(item["quantity"])):
                provider_key = f"{user_id}:{key}:{item['id']}:{index}"
                provider_row, result = create_provider_order_with_fallback(connection, item, user_id, provider_key, 1)
                provider_orders.append((provider_row, item, provider_key, result))

        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT order_ids,total FROM miniapp_checkouts WHERE user_id=? AND idempotency_key=?",
            (user_id, key),
        ).fetchone()
        if existing:
            connection.commit()
            return jsonify({"ok": True, "duplicate": True, "orderIds": json.loads(existing[0]), "total": existing[1]})
        locked_cart = cart_payload(connection, user_id)
        customer_emails = required_customer_emails(body, locked_cart["items"])
        for item in locked_cart["items"]:
            item["customerEmail"] = customer_emails.get(int(item["id"]), "")
        locked_promo = calculate_promo(connection, user_id, promo_code, locked_cart["items"])
        locked_snapshot = [
            (int(item["id"]), int(item["quantity"]), int(item["price"]), int(item["lineTotal"]),
             int(item.get("providerId") or 0), str(item.get("externalProductId") or ""))
            for item in locked_cart["items"]
        ]
        if locked_snapshot != snapshot or locked_promo["final"] != promo["final"]:
            raise ToolError("Giỏ hàng hoặc giá đã thay đổi, vui lòng xác nhận lại", 409, "cart_changed")
        cart, promo = locked_cart, locked_promo
        flash_claims = []
        for item in cart["items"]:
            sale = active_flash_sale(connection, item["id"])
            if sale:
                quantity = int(item["quantity"])
                updated_sale = connection.execute(
                    "UPDATE flash_sales SET quantity_sold=quantity_sold+? WHERE id=? AND active=1 AND quantity_sold+?<=quantity_limit",
                    (quantity, sale["id"], quantity),
                )
                if updated_sale.rowcount != 1:
                    raise ToolError("Flash Sale đã hết số lượng", 409, "flash_sale_sold_out")
                flash_claims.append((sale["id"], quantity))
        user = connection.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        if user["balance"] < promo["final"]:
            raise ToolError("Số dư không đủ", 409, "insufficient_balance")
        updated = connection.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
            (promo["final"], user_id, promo["final"]),
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
                   (user_id, plan_name, price, date, store_item_id, quantity, status, warranty_until,
                    original_price,discount_percent,discount_amount,final_price,promo_code,provider_id,external_order_id,customer_email)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, item["name"], item["lineTotal"], now.strftime("%Y-%m-%d %H:%M:%S"), item["id"], item["quantity"],
                 ("PENDING" if any(product["id"] == item["id"] and str(result.get("status", "fulfilled")).lower() not in {"fulfilled", "complete", "completed"} for _provider, product, _key, result in provider_orders) else "FULFILLED") if item.get("providerId") else "COMPLETED", warranty, item["lineTotal"], promo["percent"],
                 (item["lineTotal"] * promo["percent"] // 100), item["lineTotal"] - (item["lineTotal"] * promo["percent"] // 100),
                 promo["code"], item.get("providerId"), next((str(result.get("order_id", result.get("id", ""))) for provider, product, provider_key, result in provider_orders if product["id"] == item["id"]), ""), item.get("customerEmail", "")),
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
            "INSERT INTO miniapp_checkouts(user_id,idempotency_key,order_ids,total,original_total,discount_amount,promo_code,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, key, json.dumps(order_ids), promo["final"], promo["original"], promo["discount"], promo["code"], now.isoformat(timespec="seconds")),
        )
        for provider_row, item, provider_key, result in provider_orders:
            connection.execute(
                "INSERT INTO provider_orders(provider_id,user_id,idempotency_key,external_order_id,status,request_json,response_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (provider_row["id"], user_id, provider_key, str(result.get("order_id", result.get("id", ""))),
                 "fulfilled" if str(result.get("status", "fulfilled")).lower() in {"fulfilled", "complete", "completed"} else "pending",
                 json.dumps({"product_id": item.get("externalProductId"), "quantity": 1}), json.dumps({"ok": True}), now_iso(), now_iso()),
            )
        if promo["code"]:
            connection.execute("INSERT INTO discount_redemptions(code,user_id,checkout_key,created_at) VALUES(?,?,?,?)", (promo["code"], user_id, key, now_iso()))
        for sale_id, quantity in flash_claims:
            connection.execute("INSERT INTO flash_sale_claims(sale_id,user_id,checkout_key,quantity,created_at) VALUES(?,?,?,?,?)", (sale_id, user_id, key, quantity, now_iso()))
        connection.commit()
        return jsonify({"ok": True, "duplicate": False, "orderIds": order_ids, "total": promo["final"], "originalTotal": promo["original"], "discountAmount": promo["discount"]})
    except ToolError as error:
        connection.rollback()
        refund_provider_results()
        return jsonify({"ok": False, "error": str(error), "reason_code": error.reason_code}), error.status
    except ProviderError as error:
        connection.rollback()
        refund_provider_results()
        return jsonify({"ok": False, "error": str(error), "reason_code": error.reason_code}), 502
    except sqlite3.IntegrityError:
        connection.rollback()
        refund_provider_results()
        return jsonify({"ok": False, "error": "Giao dịch trùng lặp"}), 409
    except Exception as exc:
        connection.rollback()
        refund_provider_results()
        app.logger.error("Checkout failed user_id=%s error_type=%s", user_id, type(exc).__name__)
        return jsonify({"ok": False, "error": "Không thể hoàn tất giao dịch"}), 500


@app.get("/api/orders")
@authenticated
def orders():
    user_id = int(g.telegram_user["id"])
    rows = db().execute(
        """SELECT id, plan_name, price, date, quantity, status, warranty_until, customer_email,
                  customer_email_approved, customer_email_approved_at
           FROM purchase_history WHERE user_id=? ORDER BY id DESC LIMIT 100""",
        (user_id,),
    ).fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


@app.get("/api/orders/<int:order_id>")
@authenticated
def order(order_id):
    row = db().execute(
        """SELECT id, plan_name, price, date, quantity, status, warranty_until, customer_email,
                  customer_email_approved, customer_email_approved_at
           FROM purchase_history WHERE id=? AND user_id=?""",
        (order_id, int(g.telegram_user["id"])),
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Không tìm thấy đơn hàng"}), 404
    return jsonify({"ok": True, "item": dict(row)})


@app.get("/api/deliveries")
@authenticated
def deliveries():
    rows = db().execute(
        """SELECT d.id,d.kind,d.status,d.warranty_until,d.delivered_at,d.account_json,
                  w.id AS warranty_id,w.status AS warranty_status,w.resolution
           FROM delivery_events d
           LEFT JOIN warranty_requests w ON w.delivery_id=d.id
           WHERE d.user_id=? ORDER BY d.id DESC LIMIT 100""",
        (int(g.telegram_user["id"]),),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["account"] = json.loads(item.pop("account_json") or "{}")
        except json.JSONDecodeError:
            item["account"] = {}
        item["warrantyAvailable"] = bool(
            item.get("warranty_until") and item["warranty_until"] >= now_iso()
            and not item.get("warranty_id")
        )
        items.append(item)
    return jsonify({"ok": True, "items": items})


@app.get("/api/devices")
@authenticated
def devices():
    current = getattr(g, "device_hash", "")
    rows = db().execute(
        """SELECT id,label,platform,first_seen_at,last_seen_at,revoked,device_hash
           FROM user_devices WHERE user_id=? ORDER BY revoked,last_seen_at DESC""",
        (int(g.telegram_user["id"]),),
    ).fetchall()
    return jsonify({"ok": True, "limit": DEVICE_LIMIT, "items": [
        {"id": row["id"], "label": row["label"] or "Thiết bị", "platform": row["platform"],
         "firstSeenAt": row["first_seen_at"], "lastSeenAt": row["last_seen_at"],
         "revoked": bool(row["revoked"]), "current": bool(current and row["device_hash"] == current)}
        for row in rows
    ]})


@app.delete("/api/devices/<int:device_id>")
@authenticated
def revoke_device(device_id):
    connection = db()
    row = connection.execute(
        "SELECT device_hash,revoked FROM user_devices WHERE id=? AND user_id=?",
        (device_id, int(g.telegram_user["id"])),
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Không tìm thấy thiết bị"}), 404
    if row["device_hash"] == getattr(g, "device_hash", ""):
        return jsonify({"ok": False, "error": "Không thể thu hồi thiết bị đang sử dụng", "reason_code": "current_device"}), 409
    connection.execute("UPDATE user_devices SET revoked=1 WHERE id=?", (device_id,))
    connection.commit()
    return jsonify({"ok": True})


@app.post("/api/admin/devices/<int:device_id>/restore")
@admin_required
def admin_restore_device(device_id):
    connection = db()
    connection.execute("BEGIN IMMEDIATE")
    device = connection.execute(
        "SELECT id,user_id,revoked FROM user_devices WHERE id=?",
        (device_id,),
    ).fetchone()
    if not device:
        connection.rollback()
        return jsonify({"ok": False, "reason_code": "device_not_found", "error": "Không tìm thấy thiết bị"}), 404
    updated = connection.execute(
        "UPDATE user_devices SET revoked=0 WHERE id=? AND revoked=1",
        (device_id,),
    )
    admin_audit(
        connection,
        "device.restore",
        device_id,
        f"user_id={int(device['user_id'])},changed={int(updated.rowcount == 1)}",
    )
    connection.commit()
    return jsonify({"ok": True, "restored": bool(updated.rowcount == 1), "alreadyActive": not bool(device["revoked"])})


def grant_warranty_credit(connection, delivery):
    kind = delivery["kind"]
    if kind == "nftoken":
        connection.execute("UPDATE users SET nftoken_credits=nftoken_credits+1 WHERE user_id=?", (delivery["user_id"],))
        return "Đã hoàn 1 lượt NFToken"
    if kind == "nftoken_vip":
        connection.execute("UPDATE users SET credits=credits+1 WHERE user_id=?", (delivery["user_id"],))
        return "Đã hoàn 1 lượt Cookie VIP"
    if kind == "free_cookie":
        connection.execute(
            "INSERT OR IGNORE INTO free_cookie_checkins(user_id,local_date,claimed,created_at) VALUES(?,?,0,?)",
            (delivery["user_id"], local_today(), now_iso()),
        )
        connection.execute(
            "UPDATE free_cookie_checkins SET claimed=claimed+1 WHERE user_id=? AND local_date=?",
            (delivery["user_id"], local_today()),
        )
        return "Đã hoàn 1 lượt Cookie Free"
    return "Đã xác nhận bảo hành; bạn có thể thử kết nối TV lại"


@app.post("/api/warranty")
@authenticated
def create_warranty():
    try:
        body = json_body()
        delivery_id = int(body.get("deliveryId", 0))
        reason = str(body.get("reason", "")).strip()[:500]
        key = normalize_nftoken_request_id(body.get("idempotencyKey"))
    except (ValueError, TypeError, ToolError) as error:
        return jsonify({"ok": False, "error": str(error) or "Yêu cầu không hợp lệ"}), 400
    if len(reason) < 5:
        return jsonify({"ok": False, "error": "Vui lòng mô tả lỗi ít nhất 5 ký tự"}), 400
    connection = db()
    user_id = int(g.telegram_user["id"])
    connection.execute("BEGIN IMMEDIATE")
    delivery = connection.execute(
        "SELECT * FROM delivery_events WHERE id=? AND user_id=?", (delivery_id, user_id)
    ).fetchone()
    if not delivery:
        connection.rollback()
        return jsonify({"ok": False, "error": "Không tìm thấy lần giao hàng"}), 404
    if not delivery["warranty_until"] or delivery["warranty_until"] < now_iso():
        connection.rollback()
        return jsonify({"ok": False, "error": "Lần giao này đã hết thời hạn bảo hành", "reason_code": "warranty_expired"}), 409
    existing = connection.execute(
        "SELECT id,status,resolution FROM warranty_requests WHERE delivery_id=? OR (user_id=? AND idempotency_key=?)",
        (delivery_id, user_id, key),
    ).fetchone()
    if existing:
        connection.commit()
        return jsonify({"ok": True, "duplicate": True, **dict(existing)})
    cursor = connection.execute(
        """INSERT INTO warranty_requests(user_id,delivery_id,reason,status,resolution,idempotency_key,created_at,updated_at)
           VALUES(?,?,?,'checking','',?,?,?)""",
        (user_id, delivery_id, reason, key, now_iso(), now_iso()),
    )
    warranty_id = cursor.lastrowid
    connection.commit()

    table = COOKIE_TABLES.get(delivery["inventory_source"])
    cookie = connection.execute(
        f"SELECT data FROM {table} WHERE id=?" if table else "SELECT NULL AS data WHERE 0",
        (delivery["inventory_id"],) if table else (),
    ).fetchone()
    if not cookie:
        connection.execute(
            "UPDATE warranty_requests SET status='pending',resolution='Cần Admin kiểm tra kho',updated_at=? WHERE id=?",
            (now_iso(), warranty_id),
        )
        connection.commit()
        telegram_notify(f"🛡 <b>Bảo hành #{warranty_id}</b> cần kiểm tra thủ công.")
        return jsonify({"ok": True, "id": warranty_id, "status": "pending", "message": "Yêu cầu đã chuyển Admin kiểm tra"}), 202
    success, token, error, _account, _netscape = run_cookie_check(cookie["data"], timeout=COOKIE_CHECK_TIMEOUT, request_id=f"warranty-{warranty_id}")
    reason_code = nftoken_failure_reason(error)
    connection.execute("BEGIN IMMEDIATE")
    if success and token:
        status, resolution = "rejected", "Cookie vẫn hoạt động tại thời điểm kiểm tra"
        inventory_outcome(connection, delivery["inventory_source"], delivery["inventory_id"], True)
    elif reason_code in {"cookie_expired", "cookie_invalid"}:
        resolution = grant_warranty_credit(connection, delivery)
        status = "approved"
        inventory_outcome(connection, delivery["inventory_source"], delivery["inventory_id"], False, reason_code)
        connection.execute("UPDATE delivery_events SET status='warranty_approved' WHERE id=?", (delivery_id,))
    else:
        status, resolution = "pending", "Netflix đang lỗi kết nối; Admin sẽ kiểm tra lại"
        inventory_outcome(connection, delivery["inventory_source"], delivery["inventory_id"], False, reason_code)
    connection.execute(
        "UPDATE warranty_requests SET status=?,resolution=?,updated_at=? WHERE id=?",
        (status, resolution, now_iso(), warranty_id),
    )
    notify(connection, user_id, "warranty", f"Bảo hành #{warranty_id}", resolution, {"status": status})
    connection.commit()
    return jsonify({"ok": True, "id": warranty_id, "status": status, "message": resolution})


@app.get("/api/transactions")
@authenticated
def transactions():
    connection = db()
    connection.execute(
        """UPDATE transactions SET status='EXPIRED',review_note='Đã hết thời gian thanh toán'
           WHERE user_id=? AND status='AWAITING_PAYMENT' AND payment_deadline IS NOT NULL
             AND payment_deadline<?""",
        (int(g.telegram_user["id"]), now_iso()),
    )
    connection.commit()
    rows = connection.execute(
        """SELECT id,amount,status,transfer_note,created_at,submitted_at,
                  reviewed_at,review_note,payment_deadline
           FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 50""",
        (int(g.telegram_user["id"]),),
    ).fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


@app.get("/api/tools/status")
@authenticated
def tools_status():
    connection = db()
    user_id = int(g.telegram_user["id"])
    premium = available_stock_count(connection, "premium")
    free = available_stock_count(connection, "free")
    return jsonify({"ok": True, "quota": quota_payload(connection, user_id),
                    "checkin": checkin_payload(connection, user_id),
                    "stock": {"premium": premium, "free": free}, "features": feature_flags(connection)})


@app.get("/api/referral")
@authenticated
def referral():
    connection = db()
    if tool_rate_limited(int(g.telegram_user["id"]), limit=20, window=60):
        return jsonify({"ok": False, "error": "Bạn thao tác quá nhanh", "reason_code": "rate_limited"}), 429
    return jsonify({"ok": True, **referral_payload(connection, int(g.telegram_user["id"]))})


@app.post("/api/checkin")
@authenticated
def checkin():
    connection = db()
    user_id = int(g.telegram_user["id"])
    today = local_today()
    daily = int(app_setting(connection, "free_cookie_daily_limit", "2") or 2)
    try:
        connection.execute("BEGIN IMMEDIATE")
        inserted = connection.execute(
            "INSERT OR IGNORE INTO free_cookie_checkins(user_id,local_date,claimed,created_at) VALUES(?,?,?,?)",
            (user_id, today, daily, now_iso()),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return jsonify({"ok": True, "new": bool(inserted.rowcount), "checkin": checkin_payload(connection, user_id)})


@app.get("/api/checkin/history")
@authenticated
def checkin_history():
    rows = db().execute(
        "SELECT local_date,claimed,created_at FROM free_cookie_checkins WHERE user_id=? ORDER BY local_date DESC LIMIT 60",
        (int(g.telegram_user["id"]),),
    ).fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


@app.post("/api/tools/free-cookie")
@authenticated
def free_cookie():
    connection = db()
    user_id = int(g.telegram_user["id"])
    try:
        require_feature(connection, "freeCookie")
    except ToolError as error:
        return jsonify({"ok": False, "reason_code": "feature_disabled", "error": str(error)}), error.status
    if tool_rate_limited(user_id):
        record_risk_event(connection, user_id, "free_cookie_rate_limit", 5, getattr(g, "device_hash", ""), str(int(time.time()) // 30))
        connection.commit()
        return jsonify({"ok": False, "reason_code": "rate_limited", "error": "Bạn thao tác quá nhanh"}), 429
    today = local_today()
    body = request.get_json(silent=True) or {}
    try:
        request_id = normalize_nftoken_request_id(body.get("requestId") or uuid.uuid4().hex)
    except ToolError as error:
        return jsonify({"ok": False, "reason_code": error.reason_code, "error": str(error)}), error.status
    duplicate = connection.execute(
        "SELECT id FROM delivery_events WHERE user_id=? AND kind='free_cookie' AND request_id=?",
        (user_id, request_id),
    ).fetchone()
    if duplicate:
        return jsonify({"ok": False, "reason_code": "already_delivered", "error": "Yêu cầu này đã được giao trước đó", "deliveryId": duplicate["id"]}), 409
    last_reason = "stock_empty"
    try:
        for _attempt in range(3):
            connection.execute("BEGIN IMMEDIATE")
            checkin_row = connection.execute(
                "SELECT claimed FROM free_cookie_checkins WHERE user_id=? AND local_date=?",
                (user_id, today),
            ).fetchone()
            plan_name = connection.execute("SELECT plan_name FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
            plan = connection.execute("SELECT cookies_max FROM plans WHERE name=?", (plan_name,)).fetchone()
            cookies_max = int(plan[0] if plan else 0)
            connection.execute("INSERT OR IGNORE INTO usage(user_id,date) VALUES(?,?)", (user_id, today))
            usage = int(connection.execute(
                "SELECT free_cookies_used FROM usage WHERE user_id=? AND date=?", (user_id, today)
            ).fetchone()[0])
            has_checkin = bool(checkin_row and int(checkin_row["claimed"]) > 0)
            if not has_checkin and usage >= cookies_max:
                connection.rollback()
                return jsonify({"ok": False, "reason_code": "quota_exhausted", "error": "Bạn đã hết lượt Cookie miễn phí hôm nay"}), 409
            reserved = reserve_cookie_row(connection, ("free",), user_id=user_id)
            if not reserved:
                connection.rollback()
                maybe_alert_low_stock(connection)
                return jsonify({"ok": False, "reason_code": "stock_empty", "error": "Kho Cookie miễn phí đang trống"}), 409
            cookie_id, cookie_data, _source = reserved
            connection.commit()

            success, token, check_error, account, _netscape = run_cookie_check(
                cookie_data, timeout=COOKIE_CHECK_TIMEOUT, request_id=request_id
            )
            reason_code = nftoken_failure_reason(check_error)
            if not (success and token and account.get("membership_status") == "CURRENT_MEMBER"):
                inventory_outcome(connection, "free", cookie_id, False, reason_code)
                connection.commit()
                last_reason = reason_code
                if reason_code not in {"cookie_expired", "cookie_invalid"}:
                    break
                continue

            connection.execute("BEGIN IMMEDIATE")
            # Re-check and consume quota atomically only after the Cookie is proven live.
            checkin_row = connection.execute(
                "SELECT claimed FROM free_cookie_checkins WHERE user_id=? AND local_date=?",
                (user_id, today),
            ).fetchone()
            if checkin_row and int(checkin_row["claimed"]) > 0:
                consumed = connection.execute(
                    "UPDATE free_cookie_checkins SET claimed=claimed-1 WHERE user_id=? AND local_date=? AND claimed>0",
                    (user_id, today),
                )
            else:
                consumed = connection.execute(
                    "UPDATE usage SET free_cookies_used=free_cookies_used+1 WHERE user_id=? AND date=? AND free_cookies_used<?",
                    (user_id, today, cookies_max),
                )
            if consumed.rowcount != 1:
                connection.rollback()
                inventory_outcome(connection, "free", cookie_id, True)
                connection.execute("UPDATE free_cookies SET is_used=0 WHERE id=?", (cookie_id,))
                connection.commit()
                return jsonify({"ok": False, "reason_code": "quota_exhausted", "error": "Bạn đã hết lượt Cookie miễn phí hôm nay"}), 409
            inventory_outcome(connection, "free", cookie_id, True)
            connection.execute("UPDATE free_cookies SET is_used=0 WHERE id=?", (cookie_id,))
            copyright_data = copyright_payload(connection)
            export_cookie = watermark_export(cookie_data, copyright_data["text"], f"FREE-{user_id}-{today}", user_id, enabled=copyright_data["enabled"], netscape=True)
            download_url = create_secure_download(connection, user_id, 0, export_cookie, f"cookie-free-{today}.txt")
            delivery = record_delivery(connection, user_id, "free_cookie", "free", cookie_id, request_id, account)
            connection.commit()
            maybe_alert_low_stock(connection)
            return jsonify({
                "ok": True, "cookie": cookie_data, "quota": quota_payload(connection, user_id),
                "downloadUrl": download_url, "checkin": checkin_payload(connection, user_id),
                "copyright": copyright_data, "deliveryId": delivery["id"] if delivery else None,
                "warrantyUntil": delivery["warranty_until"] if delivery else None,
            })
        return jsonify({"ok": False, "reason_code": last_reason, "error": nftoken_failure_message(last_reason)}), (504 if last_reason == "nftoken_timeout" else 409)
    except Exception as exc:
        connection.rollback()
        app.logger.error("Free cookie failed user_id=%s error_type=%s", user_id, type(exc).__name__)
        return jsonify({"ok": False, "reason_code": "cookie_delivery_error", "error": "Không thể nhận Cookie lúc này"}), 500


def generate_one_nftoken(connection, user_id, mode, deadline=None, request_id=None):
    cookie_id, cookie_data, cookie_source, quota_source = reserve_nftoken_request(connection, user_id, mode)
    allow_free = quota_source.get("kind") == "trial_nftoken"
    last_error = "Không tìm thấy Cookie hoạt động"
    last_reason_code = "cookie_unavailable"
    for attempt in range(5):
        if deadline is not None and time.monotonic() >= deadline:
            inventory_outcome(connection, cookie_source, cookie_id, False, "nftoken_timeout")
            connection.commit()
            refund_nftoken_request(connection, user_id, quota_source)
            raise ToolError("Máy chủ xử lý quá lâu, vui lòng thử lại", 504, "nftoken_timeout")
        try:
            if deadline is None:
                success, token, error, account, netscape = run_cookie_check(
                    cookie_data, request_id=request_id
                )
            else:
                check_timeout = max(0.1, min(COOKIE_CHECK_TIMEOUT, deadline - time.monotonic()))
                success, token, error, account, netscape = run_cookie_check(
                    cookie_data, timeout=check_timeout, request_id=request_id
                )
        except Exception as exc:
            app.logger.error("NFToken check failed type=%s", type(exc).__name__)
            success, token, error, account, netscape = False, None, "checker_exception", {}, None
        if success and token and account.get("membership_status") == "CURRENT_MEMBER":
            # The validated Cookie is a temporary delivery hold; return it to stock.
            release_cookie(connection, cookie_id, delete=False, source=cookie_source)
            inventory_outcome(connection, cookie_source, cookie_id, True)
            copyright_data = copyright_payload(connection)
            nftoken_text = watermark_export(f"NFToken URL: https://netflix.com/?nftoken={quote(str(token), safe='')}\n", copyright_data["text"], f"NFT-{user_id}-{int(time.time())}", user_id, enabled=copyright_data["enabled"])
            download_url = create_secure_download(connection, user_id, 0, nftoken_text, f"nftoken-{user_id}.txt")
            delivery = record_delivery(
                connection, user_id, "nftoken_vip" if mode == "vip" else "nftoken",
                cookie_source, cookie_id, request_id or "", account,
            )
            connection.commit()
            return {
                "link": f"https://netflix.com/?nftoken={quote(str(token), safe='')}",
                "downloadUrl": download_url,
                "account": public_account(account),
                "netscape": netscape,
                "deliveryId": delivery["id"] if delivery else None,
                "warrantyUntil": delivery["warranty_until"] if delivery else None,
            }
        last_reason_code = nftoken_failure_reason(error)
        last_error = nftoken_failure_message(last_reason_code)
        inventory_outcome(connection, cookie_source, cookie_id, False, last_reason_code)
        connection.commit()
        if last_reason_code == "nftoken_timeout":
            break
        if attempt < 4:
            try:
                cookie_id, cookie_data, cookie_source = reserve_nftoken_cookie(
                    connection, allow_free=allow_free, user_id=user_id
                )
            except ToolError:
                break
    refund_nftoken_request(connection, user_id, quota_source)
    raise ToolError(last_error, 504 if last_reason_code == "nftoken_timeout" else 409, last_reason_code)


def normalize_nftoken_request_id(value):
    request_id = str(value or "").strip()
    if not request_id:
        return uuid.uuid4().hex
    if not 8 <= len(request_id) <= 100 or not all(char.isalnum() or char in "-_." for char in request_id):
        raise ToolError("request_id không hợp lệ", 400, "invalid_request_id")
    return request_id


def begin_nftoken_job(connection, user_id, request_id, mode, quantity):
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute(
        "SELECT * FROM nftoken_jobs WHERE request_id=?", (request_id,)
    ).fetchone()
    if row:
        if int(row["user_id"]) != int(user_id):
            connection.rollback()
            raise ToolError("request_id không thuộc tài khoản này", 403, "request_owner_mismatch")
        connection.commit()
        return row, False
    now = now_iso()
    connection.execute(
        """INSERT INTO nftoken_jobs(
            request_id,user_id,mode,quantity,status,result_json,reason_code,message,status_code,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (request_id, int(user_id), mode, int(quantity), "queued", "{}", "", "", 409, now, now),
    )
    connection.commit()
    return None, True


def finish_nftoken_job(connection, request_id, status, payload=None, reason_code="", message="", status_code=409):
    result_json = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
    connection.execute(
        """UPDATE nftoken_jobs
           SET status=?,result_json=?,reason_code=?,message=?,status_code=?,updated_at=?
           WHERE request_id=?""",
        (status, result_json, reason_code, message, int(status_code), now_iso(), request_id),
    )
    connection.commit()


def run_nftoken_background_job(request_id, user_id, mode, quantity):
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        claimed = connection.execute(
            "UPDATE nftoken_jobs SET status='running',updated_at=? WHERE request_id=? AND status='queued'",
            (now_iso(), request_id),
        )
        connection.commit()
        if claimed.rowcount != 1:
            return
        results = []
        deadline = time.monotonic() + NFTOKEN_TOTAL_TIMEOUT
        for _ in range(quantity):
            row = connection.execute("SELECT status FROM nftoken_jobs WHERE request_id=?", (request_id,)).fetchone()
            if not row or row["status"] == "cancelled":
                return
            results.append(generate_one_nftoken(connection, user_id, mode, deadline=deadline, request_id=request_id))
        payload = {
            "ok": True, "request_id": request_id, "items": results,
            "partial": len(results) != quantity, "quota": quota_payload(connection, user_id),
        }
        # A result already delivered upstream wins over a late cancel request.
        finish_nftoken_job(connection, request_id, "done", payload=payload, status_code=200)
    except ToolError as error:
        finish_nftoken_job(connection, request_id, "error", reason_code=error.reason_code, message=str(error), status_code=error.status)
    except Exception as error:
        app.logger.error("NFToken background job failed request_id=%s type=%s", request_id, type(error).__name__)
        finish_nftoken_job(connection, request_id, "error", reason_code="nftoken_failed", message="Không tạo được NFToken lúc này", status_code=500)
    finally:
        connection.close()


def nftoken_job_payload(row):
    if row["status"] == "done":
        payload = json.loads(row["result_json"] or "{}")
        payload["request_id"] = row["request_id"]
        payload["duplicate"] = True
        return payload, 200
    if row["status"] in {"queued", "running"}:
        return {
            "ok": False,
            "request_id": row["request_id"],
            "status": row["status"],
            "reason_code": "nftoken_in_progress",
            "error": "NFToken đang được xử lý, vui lòng chờ kết quả",
        }, 409
    return {
        "ok": False,
        "request_id": row["request_id"],
        "status": row["status"],
        "reason_code": row["reason_code"] or "nftoken_failed",
        "error": row["message"] or "Không tạo được NFToken lúc này",
    }, int(row["status_code"] or 409)


@app.post("/api/tools/nftoken")
@authenticated
def create_nftoken():
    try:
        body = json_body()
        mode = str(body.get("mode", "plan"))
        quantity = int(body.get("quantity", 1))
        background = bool(body.get("background", False))
    except (ValueError, TypeError) as error:
        return jsonify({"ok": False, "error": str(error) or "Dữ liệu không hợp lệ"}), 400
    if mode not in {"plan", "vip"}:
        return jsonify({"ok": False, "error": "Chế độ không hợp lệ"}), 400
    if mode == "plan":
        quantity = 1
    if not 1 <= quantity <= 1:
        return jsonify({"ok": False, "reason_code": "invalid_quantity", "error": "Mỗi lần chỉ tạo 1 NFToken"}), 400
    user_id = int(g.telegram_user["id"])
    try:
        request_id = normalize_nftoken_request_id(body.get("requestId") or body.get("idempotencyKey"))
        g.nftoken_request_id = request_id
    except ToolError as error:
        return jsonify({"ok": False, "reason_code": error.reason_code, "error": str(error)}), error.status
    connection = db()
    try:
        require_feature(connection, "vipToken" if mode == "vip" else "planToken")
    except ToolError as error:
        return jsonify({"ok": False, "reason_code": error.reason_code, "error": str(error)}), error.status

    try:
        existing, created = begin_nftoken_job(connection, user_id, request_id, mode, quantity)
    except ToolError as error:
        return jsonify({"ok": False, "reason_code": error.reason_code, "error": str(error)}), error.status
    if not created:
        payload, status_code = nftoken_job_payload(existing)
        return jsonify(payload), status_code
    if tool_rate_limited(user_id, limit=5, window=60):
        record_risk_event(connection, user_id, "nftoken_rate_limit", 8, getattr(g, "device_hash", ""), request_id)
        connection.commit()
        finish_nftoken_job(connection, request_id, "error", reason_code="rate_limited", message="Bạn thao tác quá nhanh", status_code=429)
        return jsonify({"ok": False, "request_id": request_id, "reason_code": "rate_limited", "error": "Bạn thao tác quá nhanh"}), 429
    if background:
        NFTOKEN_JOB_EXECUTOR.submit(run_nftoken_background_job, request_id, user_id, mode, quantity)
        return jsonify({"ok": True, "request_id": request_id, "status": "queued", "job_id": request_id}), 202
    connection.execute(
        "UPDATE nftoken_jobs SET status='running',updated_at=? WHERE request_id=? AND status='queued'",
        (now_iso(), request_id),
    )
    connection.commit()
    results = []
    deadline = time.monotonic() + NFTOKEN_TOTAL_TIMEOUT
    try:
        for _ in range(quantity):
            results.append(
                generate_one_nftoken(
                    connection, user_id, mode, deadline=deadline, request_id=request_id
                )
            )
    except ToolError as error:
        if not results:
            finish_nftoken_job(connection, request_id, "error", reason_code=error.reason_code, message=str(error), status_code=error.status)
            return jsonify({"ok": False, "request_id": request_id, "reason_code": error.reason_code, "error": str(error)}), error.status
    except Exception as error:
        app.logger.error("NFToken request failed request_id=%s type=%s", request_id, type(error).__name__)
        finish_nftoken_job(connection, request_id, "error", reason_code="nftoken_failed", message="Không tạo được NFToken lúc này", status_code=500)
        return jsonify({"ok": False, "request_id": request_id, "reason_code": "nftoken_failed", "error": "Không tạo được NFToken lúc này"}), 500
    payload = {
        "ok": True,
        "request_id": request_id,
        "items": results,
        "partial": len(results) != quantity,
        "quota": quota_payload(connection, user_id),
    }
    finish_nftoken_job(connection, request_id, "done", payload=payload, status_code=200)
    return jsonify(payload)


@app.get("/api/tools/nftoken/job/<request_id>")
@authenticated
def get_nftoken_job(request_id):
    try:
        normalized_id = normalize_nftoken_request_id(request_id)
    except ToolError as error:
        return jsonify({"ok": False, "reason_code": error.reason_code, "error": str(error)}), error.status
    row = db().execute(
        "SELECT * FROM nftoken_jobs WHERE request_id=? AND user_id=?",
        (normalized_id, int(g.telegram_user["id"])),
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "reason_code": "job_not_found", "error": "Không tìm thấy yêu cầu NFToken"}), 404
    if row["status"] in {"queued", "running"}:
        return jsonify({
            "ok": True, "request_id": row["request_id"], "job_id": row["request_id"],
            "status": row["status"], "progress": {"current": 0, "total": int(row["quantity"])},
        })
    payload, status_code = nftoken_job_payload(row)
    payload["status"] = row["status"]
    return jsonify(payload), status_code


@app.post("/api/tools/nftoken/job/<request_id>/cancel")
@authenticated
def cancel_nftoken_job(request_id):
    try:
        normalized_id = normalize_nftoken_request_id(request_id)
    except ToolError as error:
        return jsonify({"ok": False, "reason_code": error.reason_code, "error": str(error)}), error.status
    connection = db()
    updated = connection.execute(
        """UPDATE nftoken_jobs SET status='cancelled',reason_code='cancelled',
               message='Đã hủy theo yêu cầu',updated_at=?
           WHERE request_id=? AND user_id=? AND status='queued'""",
        (now_iso(), normalized_id, int(g.telegram_user["id"])),
    )
    connection.commit()
    if updated.rowcount:
        return jsonify({"ok": True, "status": "cancelled"})
    row = connection.execute(
        "SELECT status FROM nftoken_jobs WHERE request_id=? AND user_id=?",
        (normalized_id, int(g.telegram_user["id"])),
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Không tìm thấy yêu cầu"}), 404
    return jsonify({"ok": False, "reason_code": "job_not_cancellable", "error": "Job đã bắt đầu; kết quả sẽ được lưu để không mất lượt", "status": row["status"]}), 409


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
        return jsonify({"ok": False, "reason_code": "invalid_request", "error": str(error)}), 400
    if not (len(tv_code) == 8 and tv_code.isdigit()):
        return jsonify({"ok": False, "reason_code": "invalid_code", "error": "Mã TV phải gồm đúng 8 chữ số", "steps": steps("validate", True)}), 400
    user_id = int(g.telegram_user["id"])
    if tool_rate_limited(user_id, limit=3, window=60):
        risk_connection = db()
        record_risk_event(risk_connection, user_id, "tv_rate_limit", 8, getattr(g, "device_hash", ""), str(int(time.time()) // 60))
        risk_connection.commit()
        return jsonify({"ok": False, "reason_code": "rate_limited", "error": "Bạn thao tác quá nhanh"}), 429
    connection = db()
    try:
        require_feature(connection, "tv")
    except ToolError as error:
        return jsonify({"ok": False, "reason_code": "feature_disabled", "error": str(error), "steps": steps("validate", True)}), error.status
    try:
        last_message = "Kho Cookie Premium đang trống"
        last_reason_code = "cookie_unavailable"
        for _attempt in range(3):
            cookie_id, cookie_data = reserve_cookie(connection, user_id=user_id)
            success, reason_code, message, account = run_tv_login(cookie_data, tv_code)
            last_reason_code = reason_code
            last_message = message
            dead_cookie = reason_code in ("cookie_expired", "cookie_format")
            if success:
                release_cookie(connection, cookie_id, delete=False)
                inventory_outcome(connection, "premium", cookie_id, True)
                delivery = record_delivery(
                    connection, user_id, "tv_login", "premium", cookie_id,
                    f"tv-{user_id}-{uuid.uuid4().hex}", account,
                )
                connection.commit()
                return jsonify({
                    "ok": True,
                    "reason_code": "connected",
                    "message": "TV đã được kết nối",
                    "account": public_account(account),
                    "deliveryId": delivery["id"] if delivery else None,
                    "steps": steps("done"),
                })
            inventory_outcome(connection, "premium", cookie_id, False, reason_code)
            connection.commit()
            if not dead_cookie:
                failed_stage = "browser" if reason_code in ("browser_missing", "webdriver_missing", "webdriver_error", "browser_error", "network_timeout", "selector_changed", "submit_failed") else "connect"
                return jsonify({"ok": False, "reason_code": reason_code, "error": message, "steps": steps(failed_stage, True)}), 409

        return jsonify({"ok": False, "reason_code": last_reason_code, "error": last_message, "steps": steps("cookie", True)}), 409
    except ToolError as error:
        reason_code = last_reason_code if last_reason_code != "cookie_unavailable" else "cookie_unavailable"
        message = last_message if last_reason_code != "cookie_unavailable" else str(error)
        return jsonify({"ok": False, "reason_code": reason_code, "error": message, "steps": steps("cookie", True)}), error.status
    except Exception as exc:
        if 'cookie_id' in locals() and cookie_id is not None:
            inventory_outcome(connection, "premium", cookie_id, False, "server_error")
            connection.commit()
        app.logger.error("TV login failed user_id=%s error_type=%s", user_id, type(exc).__name__)
        return jsonify({"ok": False, "reason_code": "server_error", "error": "Không thể đăng nhập TV lúc này", "steps": steps("browser", True)}), 500


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
        "SELECT code,amount,uses,code_type FROM discount_codes WHERE UPPER(code)=? ORDER BY rowid DESC LIMIT 1", (code,)
    ).fetchone()
    if not gift or gift["uses"] <= 0 or str(gift["code_type"] or "BALANCE").upper() != "BALANCE":
        connection.rollback()
        return jsonify({"ok": False, "error": "Mã không hợp lệ hoặc đã hết lượt"}), 409
    updated = connection.execute(
        "UPDATE discount_codes SET uses=uses-1 WHERE code=? AND uses>0", (gift["code"],)
    )
    if updated.rowcount != 1:
        connection.rollback()
        return jsonify({"ok": False, "error": "Mã không hợp lệ hoặc đã hết lượt"}), 409
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
    now = now_iso()
    payment_deadline = (datetime.now(LOCAL_TZ) + timedelta(minutes=30)).replace(tzinfo=None).isoformat(timespec="seconds")
    cursor = connection.execute(
        """INSERT INTO transactions(user_id,amount,status,created_at,payment_deadline)
           VALUES(?,?,'AWAITING_PAYMENT',?,?)""",
        (user_id, amount, now, payment_deadline),
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
         "status": "AWAITING_PAYMENT", "transferNote": transfer_note, "qrUrl": qr_url,
         "paymentDeadline": payment_deadline}
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


def reconcile_payment(connection, transaction_id, provider, reference, amount, payload_hash=""):
    connection.execute("BEGIN IMMEDIATE")
    existing = connection.execute(
        "SELECT transaction_id,status FROM payment_events WHERE provider=? AND provider_reference=?",
        (provider, reference),
    ).fetchone()
    if existing:
        connection.commit()
        return {"ok": True, "duplicate": True, "transactionId": existing["transaction_id"]}
    transaction = connection.execute(
        "SELECT user_id,amount,status FROM transactions WHERE id=?", (int(transaction_id),)
    ).fetchone()
    if not transaction:
        connection.rollback()
        raise ToolError("Không tìm thấy giao dịch", 404, "transaction_not_found")
    if int(transaction["amount"]) != int(amount):
        connection.rollback()
        raise ToolError("Số tiền đối soát không khớp", 409, "amount_mismatch")
    if transaction["status"] in {"REJECTED", "EXPIRED"}:
        connection.rollback()
        raise ToolError("Giao dịch không còn hiệu lực", 409, "transaction_inactive")
    if transaction["status"] != "APPROVED":
        connection.execute(
            "UPDATE transactions SET status='APPROVED',reviewed_at=?,review_note=?,provider_reference=? WHERE id=?",
            (now_iso(), f"Đối soát tự động qua {provider}", reference, int(transaction_id)),
        )
        connection.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=?",
            (int(amount), int(transaction["user_id"])),
        )
    connection.execute(
        """INSERT INTO payment_events(transaction_id,provider,provider_reference,amount,status,received_at,payload_hash)
           VALUES(?,?,?,?, 'approved',?,?)""",
        (int(transaction_id), provider, reference, int(amount), now_iso(), payload_hash),
    )
    notify(connection, int(transaction["user_id"]), "payment", "Nạp tiền thành công", f"Giao dịch #{transaction_id} đã được cộng vào số dư")
    connection.commit()
    return {"ok": True, "duplicate": False, "transactionId": int(transaction_id)}


@app.post("/api/payments/webhook/<provider>")
def payment_webhook(provider):
    secret = os.getenv("PAYMENT_WEBHOOK_SECRET", "").strip()
    if not secret:
        return jsonify({"ok": False, "error": "Webhook thanh toán chưa được cấu hình"}), 503
    raw = request.get_data(cache=True)
    signature = request.headers.get("X-Payment-Signature", "").strip().lower()
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        return jsonify({"ok": False, "error": "Chữ ký webhook không hợp lệ"}), 401
    try:
        body = request.get_json(force=True)
        transaction_id = int(body.get("transactionId", 0))
        amount = int(body.get("amount", 0))
        reference = str(body.get("reference", "")).strip()[:100]
        status = str(body.get("status", "paid")).lower()
    except (TypeError, ValueError, AttributeError):
        return jsonify({"ok": False, "error": "Payload webhook không hợp lệ"}), 400
    provider = re.sub(r"[^a-z0-9_.-]", "", provider.lower())[:40]
    if not provider or not reference or status not in {"paid", "approved", "success"}:
        return jsonify({"ok": False, "error": "Giao dịch webhook chưa thành công"}), 409
    try:
        result = reconcile_payment(
            db(), transaction_id, provider, reference, amount,
            hashlib.sha256(raw).hexdigest(),
        )
        return jsonify(result)
    except ToolError as error:
        return jsonify({"ok": False, "error": str(error), "reason_code": error.reason_code}), error.status


@app.post("/api/support")
@authenticated
def support_request():
    try:
        body = json_body()
        message = str(body.get("message", "")).strip()
        category = str(body.get("category", "general")).strip().lower()
        order_id = int(body.get("orderId", 0) or 0) or None
    except (ValueError, TypeError) as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if category not in {"general", "order", "payment", "nftoken", "cookie", "tv"}:
        return jsonify({"ok": False, "error": "Nhóm hỗ trợ không hợp lệ"}), 400
    if not 5 <= len(message) <= 1500:
        return jsonify({"ok": False, "error": "Nội dung hỗ trợ phải từ 5 đến 1500 ký tự"}), 400
    connection = db()
    try:
        require_feature(connection, "support")
    except ToolError as error:
        return jsonify({"ok": False, "error": str(error)}), error.status
    user_id = int(g.telegram_user["id"])
    if order_id and not connection.execute(
        "SELECT 1 FROM purchase_history WHERE id=? AND user_id=?", (order_id, user_id)
    ).fetchone():
        return jsonify({"ok": False, "error": "Đơn hàng không thuộc tài khoản này"}), 403
    open_count = connection.execute(
        "SELECT COUNT(*) FROM miniapp_support WHERE user_id=? AND status='OPEN'", (user_id,)
    ).fetchone()[0]
    if int(open_count) >= 5:
        record_risk_event(connection, user_id, "support_spam", 10, getattr(g, "device_hash", ""), str(int(time.time()) // 300))
        connection.commit()
        return jsonify({"ok": False, "error": "Bạn đang có quá nhiều yêu cầu chưa xử lý", "reason_code": "support_limit"}), 429
    cursor = connection.execute(
        """INSERT INTO miniapp_support(user_id,message,order_id,category,priority,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?)""",
        (user_id, message, order_id, category, "high" if category in {"payment", "order"} else "normal", now_iso(), now_iso()),
    )
    connection.commit()
    ticket_id = cursor.lastrowid
    telegram_notify(
        f"🛟 <b>HỖ TRỢ MINI APP #{ticket_id}</b>\n\n"
        f"User ID: <code>{user_id}</code>\nNội dung: {html.escape(message)}"
    )
    return jsonify({"ok": True, "ticketId": ticket_id})


@app.get("/api/support")
@authenticated
def support_history():
    rows = db().execute(
        """SELECT id,order_id,category,priority,message,status,admin_reply,created_at,updated_at
           FROM miniapp_support WHERE user_id=? ORDER BY id DESC LIMIT 50""",
        (int(g.telegram_user["id"]),),
    ).fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


def admin_product_values(body):
    name = str(body.get("name", "")).strip()
    description = str(body.get("description", "")).strip()
    category = str(body.get("category", "Gói Cookie VIP")).strip()
    image_url = str(body.get("imageUrl", "")).strip()
    provider_id = body.get("providerId") or None
    external_product_id = str(body.get("externalProductId", "")).strip()[:200]
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
    if image_url and (not image_url.startswith("https://") or not urlparse(image_url).netloc):
        raise ValueError("Ảnh sản phẩm phải dùng liên kết HTTPS")
    if provider_id is not None:
        try:
            provider_id = int(provider_id)
        except (TypeError, ValueError) as error:
            raise ValueError("Provider khÃ´ng há»£p lá»‡") from error
        if provider_id <= 0 or not external_product_id:
            raise ValueError("Sáº£n pháº©m provider pháº£i cÃ³ external_product_id")
    return (
        name, price, credits, nftoken_credits, description, category, image_url,
        int(bool(body.get("featured", False))), warranty_days,
        int(bool(body.get("active", True))), provider_id, external_product_id,
        int(bool(body.get("requiresCustomerEmail", False))),
    )


def provider_public(row):
    return {"id": row["id"], "name": row["name"], "baseUrl": row["base_url"],
            "timeout": row["timeout"], "enabled": bool(row["enabled"]),
            "priority": row["priority"] if "priority" in row.keys() else 100,
            "lastErrorCode": row["last_error_code"] if "last_error_code" in row.keys() else "",
            "lastResponseMs": row["last_response_ms"] if "last_response_ms" in row.keys() else 0,
            "lastCheckedAt": row["last_checked_at"] if "last_checked_at" in row.keys() else None,
            "apiKeySet": bool(row["api_key"])}


@app.get("/api/admin/providers")
@admin_required
def admin_providers():
    rows = db().execute("SELECT * FROM product_providers ORDER BY id DESC").fetchall()
    return jsonify({"ok": True, "items": [provider_public(row) for row in rows]})


@app.post("/api/admin/providers")
@admin_required
def admin_create_provider():
    try:
        body = json_body()
        name = str(body.get("name", "")).strip()[:100]
        base_url = str(body.get("baseUrl", "")).strip().rstrip("/")
        api_key = str(body.get("apiKey", ""))
        timeout = max(1, min(int(body.get("timeout", 10)), 60))
        enabled = int(bool(body.get("enabled", True)))
        priority = max(1, min(int(body.get("priority", 100)), 10000))
    except (ValueError, TypeError) as error:
        return jsonify({"ok": False, "error": "Cấu hình provider không hợp lệ"}), 400
    if not name or urlparse(base_url).scheme not in {"http", "https"} or not urlparse(base_url).netloc:
        return jsonify({"ok": False, "error": "Base URL provider không hợp lệ"}), 400
    connection = db()
    cursor = connection.execute(
        "INSERT INTO product_providers(name,base_url,api_key,timeout,enabled,priority,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (name, base_url, api_key, timeout, enabled, priority, now_iso(), now_iso()),
    )
    admin_audit(connection, "provider.create", cursor.lastrowid, name)
    connection.commit()
    return jsonify({"ok": True, "id": cursor.lastrowid})


@app.put("/api/admin/providers/<int:provider_id>")
@admin_required
def admin_update_provider(provider_id):
    try:
        body = json_body()
        name = str(body.get("name", "")).strip()[:100]
        base_url = str(body.get("baseUrl", "")).strip().rstrip("/")
        timeout = max(1, min(int(body.get("timeout", 10)), 60))
        enabled = int(bool(body.get("enabled", True)))
        priority = max(1, min(int(body.get("priority", 100)), 10000))
        api_key = body.get("apiKey")
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Cấu hình provider không hợp lệ"}), 400
    if not name or not urlparse(base_url).netloc:
        return jsonify({"ok": False, "error": "Provider không hợp lệ"}), 400
    connection = db()
    if api_key is None:
        updated = connection.execute("UPDATE product_providers SET name=?,base_url=?,timeout=?,enabled=?,priority=?,updated_at=? WHERE id=?", (name, base_url, timeout, enabled, priority, now_iso(), provider_id))
    else:
        updated = connection.execute("UPDATE product_providers SET name=?,base_url=?,api_key=?,timeout=?,enabled=?,priority=?,updated_at=? WHERE id=?", (name, base_url, str(api_key), timeout, enabled, priority, now_iso(), provider_id))
    connection.commit()
    return jsonify({"ok": updated.rowcount == 1}) if updated.rowcount else (jsonify({"ok": False, "error": "Provider không tồn tại"}), 404)


@app.post("/api/admin/providers/<int:provider_id>/test")
@admin_required
def admin_test_provider(provider_id):
    row = db().execute("SELECT * FROM product_providers WHERE id=?", (provider_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Provider không tồn tại"}), 404
    try:
        result = provider_for(row).health()
        return jsonify({"ok": True, "provider": {"id": provider_id, "healthy": True}, "result": {"ok": bool(result.get("ok", True))}})
    except ProviderError as error:
        return jsonify({"ok": False, "reason_code": error.reason_code, "error": str(error)}), 502


@app.post("/api/admin/providers/<int:provider_id>/sync")
@admin_required
def admin_sync_provider(provider_id):
    row = db().execute("SELECT * FROM product_providers WHERE id=? AND enabled=1", (provider_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Provider không tồn tại hoặc đã tắt"}), 404
    try:
        products = provider_for(row).products()
    except ProviderError as error:
        return jsonify({"ok": False, "reason_code": error.reason_code, "error": str(error)}), 502
    connection = db()
    synced = 0
    for item in products:
        external_id = str(item.get("id", item.get("product_id", ""))).strip()
        if not external_id:
            continue
        name = str(item.get("name", external_id))[:80]
        price = max(0, int(item.get("price", 0) or 0))
        existing = connection.execute("SELECT id FROM store WHERE provider_id=? AND external_product_id=?", (provider_id, external_id)).fetchone()
        if existing:
            connection.execute("UPDATE store SET name=?,price=?,description=?,active=1 WHERE id=?", (name, price, str(item.get("description", ""))[:1000], existing[0]))
        else:
            connection.execute("INSERT INTO store(name,price,credits,nftoken_credits,description,category,image_url,featured,warranty_days,active,purchases,provider_id,external_product_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (name, price, 0, int(item.get("nftoken_credits", 0) or 0), str(item.get("description", ""))[:1000], "Provider", "", 0, 0, 1, 0, provider_id, external_id))
        synced += 1
    admin_audit(connection, "provider.sync", provider_id, f"synced={synced}")
    connection.commit()
    return jsonify({"ok": True, "synced": synced})


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
    codes = connection.execute("SELECT code,amount,uses,code_type,percent,per_user,starts_at,ends_at,min_order_total,product_ids FROM discount_codes ORDER BY code LIMIT 100").fetchall()
    providers = connection.execute("SELECT * FROM product_providers ORDER BY id DESC").fetchall()
    rank_settings = connection.execute("SELECT * FROM customer_rank_settings ORDER BY CASE rank WHEN 'Bronze' THEN 1 WHEN 'Silver' THEN 2 WHEN 'Platinum' THEN 3 ELSE 4 END").fetchall()
    tickets = connection.execute(
        """SELECT s.id,s.user_id,u.username,s.message,s.status,s.created_at,s.updated_at,
                  s.order_id,s.category,s.priority,s.admin_reply
           FROM miniapp_support s LEFT JOIN users u ON u.user_id=s.user_id
           ORDER BY CASE WHEN s.status='OPEN' THEN 0 ELSE 1 END,s.id DESC LIMIT 50"""
    ).fetchall()
    orders = connection.execute(
        """SELECT p.id,p.user_id,u.username,p.plan_name,p.price,p.date,p.quantity,
                  p.status,p.warranty_until,p.customer_email,p.customer_email_approved,
                  p.customer_email_approved_at
           FROM purchase_history p LEFT JOIN users u ON u.user_id=p.user_id
           ORDER BY p.id DESC LIMIT 100"""
    ).fetchall()
    audit = connection.execute(
        """SELECT id,admin_id,action,target,details,created_at
           FROM miniapp_admin_audit ORDER BY id DESC LIMIT 100"""
    ).fetchall()
    warranties = connection.execute(
        """SELECT w.id,w.user_id,w.delivery_id,w.reason,w.status,w.resolution,w.created_at,w.updated_at,
                  d.kind,d.warranty_until
           FROM warranty_requests w JOIN delivery_events d ON d.id=w.delivery_id
           ORDER BY CASE w.status WHEN 'pending' THEN 0 WHEN 'checking' THEN 1 ELSE 2 END,w.id DESC LIMIT 100"""
    ).fetchall()
    risk_users = connection.execute(
        """SELECT r.user_id,u.username,MIN(100,SUM(r.score)) AS risk_score,COUNT(*) AS events,
                  MAX(r.created_at) AS last_event
           FROM risk_events r LEFT JOIN users u ON u.user_id=r.user_id
           WHERE r.created_at>=? GROUP BY r.user_id,u.username
           ORDER BY risk_score DESC,last_event DESC LIMIT 50""",
        ((datetime.now(LOCAL_TZ) - timedelta(days=30)).replace(tzinfo=None).isoformat(timespec="seconds"),),
    ).fetchall()
    devices = connection.execute(
        """SELECT d.id,d.user_id,u.username,d.label,d.platform,d.first_seen_at,
                  d.last_seen_at,d.revoked
           FROM user_devices d LEFT JOIN users u ON u.user_id=d.user_id
           ORDER BY d.revoked,d.last_seen_at DESC LIMIT 200"""
    ).fetchall()
    provider_metrics = connection.execute(
        "SELECT status,COUNT(*) AS count,COALESCE(AVG(latency_ms),0) AS avg_ms FROM provider_attempts GROUP BY status"
    ).fetchall()
    delivery_metrics = connection.execute(
        "SELECT status,COUNT(*) AS count FROM delivery_events GROUP BY status"
    ).fetchall()
    revenue_periods = {}
    for key, days in (("today", 0), ("week", 7), ("month", 30)):
        start = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
        revenue_periods[key] = connection.execute(
            """SELECT COALESCE(SUM(CASE WHEN final_price>0 THEN final_price ELSE price END),0)
               FROM purchase_history WHERE date>=? AND status NOT IN ('CANCELLED','REFUNDED')""",
            (start.replace(tzinfo=None).isoformat(timespec="seconds"),),
        ).fetchone()[0]
    stats = {
        "users": connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "revenue": connection.execute("SELECT COALESCE(SUM(price),0) FROM purchase_history").fetchone()[0],
        "orders": connection.execute("SELECT COUNT(*) FROM purchase_history").fetchone()[0],
        "pendingDeposits": connection.execute(
            "SELECT COUNT(*) FROM transactions WHERE status='PENDING'"
        ).fetchone()[0],
        "premiumStock": available_stock_count(connection, "premium"),
        "freeStock": available_stock_count(connection, "free"),
        "premiumUsed": connection.execute(
            "SELECT COUNT(*) FROM premium_cookies WHERE is_used=1"
        ).fetchone()[0],
        "freeUsed": connection.execute(
            "SELECT COUNT(*) FROM free_cookies WHERE is_used=1"
        ).fetchone()[0],
        "openTickets": connection.execute(
            "SELECT COUNT(*) FROM miniapp_support WHERE status='OPEN'"
        ).fetchone()[0],
        "pendingWarranties": connection.execute(
            "SELECT COUNT(*) FROM warranty_requests WHERE status IN ('pending','checking')"
        ).fetchone()[0],
        "quarantinedStock": sum(connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE health_status='quarantined'"
        ).fetchone()[0] for table in COOKIE_TABLES.values()),
        "riskUsers": len(risk_users),
    }
    return jsonify({
        "ok": True,
        "stats": stats,
        "products": [product_dict(row, include_auto_image=False) for row in products],
        "plans": [dict(row) for row in plans],
        "users": [dict(row) for row in users],
        "transactions": [dict(row) for row in transactions],
        "codes": [dict(row) for row in codes],
        "providers": [provider_public(row) for row in providers],
        "ranks": [dict(row) for row in rank_settings],
        "tickets": [dict(row) for row in tickets],
        "warranties": [dict(row) for row in warranties],
        "riskUsers": [dict(row) for row in risk_users],
        "devices": [dict(row) for row in devices],
        "metrics": {
            "revenue": revenue_periods,
            "providers": [dict(row) for row in provider_metrics],
            "deliveries": [dict(row) for row in delivery_metrics],
        },
        "orders": [dict(row) for row in orders],
        "audit": [dict(row) for row in audit],
        "settings": {
            "maintenance": app_setting(connection, "maintenance", "0") == "1",
            "announcement": app_setting(connection, "announcement", ""),
            "features": feature_flags(connection),
            "trial": trial_settings(connection),
            "operations": {
                "deliveryWarrantyDays": bounded_setting_int(connection, "delivery_warranty_days", 1, 0, 365),
                "lowStockThreshold": bounded_setting_int(connection, "low_stock_threshold", 5, 0, 100000),
                "deviceLimit": DEVICE_LIMIT,
            },
        },
    })


@app.post("/api/admin/products")
@admin_required
def admin_create_product():
    try:
        values = admin_product_values(json_body())
    except ValueError as error:
        return jsonify({"ok": False, "reason_code": "product_validation_error", "error": str(error)}), 400
    connection = db()
    try:
        cursor = connection.execute(
            """INSERT INTO store
               (name,price,credits,nftoken_credits,description,category,image_url,featured,warranty_days,active,provider_id,external_product_id,requires_customer_email)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        admin_audit(connection, "product.create", cursor.lastrowid, values[0])
        connection.commit()
        return jsonify({"ok": True, "id": cursor.lastrowid})
    except sqlite3.OperationalError as exc:
        connection.rollback()
        app.logger.error("Admin product create database error_type=%s", type(exc).__name__)
        return jsonify({"ok": False, "reason_code": "database_schema_error", "error": "Database sản phẩm chưa sẵn sàng, hãy khởi động lại Mini App"}), 500
    except sqlite3.Error as exc:
        connection.rollback()
        app.logger.error("Admin product create sqlite error_type=%s", type(exc).__name__)
        return jsonify({"ok": False, "reason_code": "product_database_error", "error": "Không thể lưu sản phẩm vào database"}), 500
    except Exception as exc:
        connection.rollback()
        app.logger.error("Admin product create unexpected error_type=%s", type(exc).__name__)
        return jsonify({"ok": False, "reason_code": "product_create_failed", "error": "Không thể tạo sản phẩm lúc này"}), 500


@app.put("/api/admin/products/<int:item_id>")
@admin_required
def admin_update_product(item_id):
    try:
        values = admin_product_values(json_body())
    except ValueError as error:
        return jsonify({"ok": False, "reason_code": "product_validation_error", "error": str(error)}), 400
    connection = db()
    updated = connection.execute(
        """UPDATE store SET name=?,price=?,credits=?,nftoken_credits=?,description=?,category=?,image_url=?,
           featured=?,warranty_days=?,active=?,provider_id=?,external_product_id=?,requires_customer_email=? WHERE id=?""",
        (*values, item_id),
    )
    if updated.rowcount == 1:
        admin_audit(connection, "product.update", item_id, values[0])
    connection.commit()
    if updated.rowcount != 1:
        return jsonify({"ok": False, "error": "Không tìm thấy sản phẩm"}), 404
    return jsonify({"ok": True})


@app.delete("/api/admin/products/<int:item_id>")
@admin_required
def admin_delete_product(item_id):
    connection = db()
    product = connection.execute("SELECT id,name FROM store WHERE id=?", (item_id,)).fetchone()
    if product is None:
        return jsonify({"ok": False, "reason_code": "product_not_found", "error": "Không tìm thấy sản phẩm"}), 404
    try:
        purchase_count = connection.execute(
            "SELECT COUNT(*) FROM purchase_history WHERE store_item_id=?", (item_id,)
        ).fetchone()[0]
        if purchase_count:
            connection.execute("UPDATE store SET active=0 WHERE id=?", (item_id,))
            admin_audit(connection, "product.archive", item_id, product["name"])
            connection.commit()
            return jsonify({
                "ok": True,
                "archived": True,
                "message": "Sản phẩm đã có đơn nên được ẩn để giữ lịch sử mua hàng",
            })
        connection.execute("DELETE FROM miniapp_cart WHERE store_item_id=?", (item_id,))
        connection.execute("DELETE FROM store WHERE id=?", (item_id,))
        admin_audit(connection, "product.delete", item_id, product["name"])
        connection.commit()
        return jsonify({"ok": True, "deleted": True})
    except sqlite3.Error as exc:
        connection.rollback()
        app.logger.error("Admin product delete sqlite error_type=%s", type(exc).__name__)
        return jsonify({"ok": False, "reason_code": "product_delete_failed", "error": "Không thể xoá sản phẩm lúc này"}), 500


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
        code_type = str(body.get("codeType", "BALANCE")).upper()
        percent = int(body.get("percent", 0))
        per_user = int(bool(body.get("perUser", True)))
        starts_at = str(body.get("startsAt", "")).strip() or None
        ends_at = str(body.get("endsAt", "")).strip() or None
        min_order_total = int(body.get("minOrderTotal", 0))
        product_ids = str(body.get("productIds", "")).strip()
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Giá trị mã không hợp lệ"}), 400
    if not code or len(code) > 50 or amount < 0 or uses < 0:
        return jsonify({"ok": False, "error": "Mã quà tặng không hợp lệ"}), 400
    if code_type not in {"BALANCE", "PERCENT"} or not 0 <= percent <= 100 or min_order_total < 0:
        return jsonify({"ok": False, "error": "Mã khuyến mãi không hợp lệ"}), 400
    if code_type == "PERCENT" and not 1 <= percent <= 100:
        return jsonify({"ok": False, "error": "Phần trăm giảm phải từ 1 đến 100"}), 400
    parsed_starts = parsed_ends = None
    try:
        if starts_at:
            parsed_starts = datetime.fromisoformat(starts_at)
        if ends_at:
            parsed_ends = datetime.fromisoformat(ends_at)
    except ValueError:
        return jsonify({"ok": False, "error": "Thời gian mã không hợp lệ"}), 400
    if parsed_starts and parsed_ends and parsed_starts >= parsed_ends:
        return jsonify({"ok": False, "error": "Thời gian bắt đầu phải trước kết thúc"}), 400
    connection = db()
    connection.execute(
        """INSERT INTO discount_codes(code,amount,uses,code_type,percent,per_user,starts_at,ends_at,min_order_total,product_ids)
           VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET amount=excluded.amount,uses=excluded.uses,
           code_type=excluded.code_type,percent=excluded.percent,per_user=excluded.per_user,starts_at=excluded.starts_at,
           ends_at=excluded.ends_at,min_order_total=excluded.min_order_total,product_ids=excluded.product_ids""",
        (code, amount, uses, code_type, percent, per_user, starts_at, ends_at, min_order_total, product_ids),
    )
    admin_audit(connection, "giftcode.save", code, f"amount={amount},uses={uses}")
    connection.commit()
    return jsonify({"ok": True, "code": code})


@app.get("/api/admin/ranks")
@admin_required
def admin_ranks():
    rows = db().execute("SELECT * FROM customer_rank_settings ORDER BY CASE rank WHEN 'Bronze' THEN 1 WHEN 'Silver' THEN 2 WHEN 'Platinum' THEN 3 ELSE 4 END").fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


@app.put("/api/admin/ranks/<path:rank>")
@admin_required
def admin_update_rank(rank):
    rank = rank.title()
    if rank not in RANK_ORDER:
        return jsonify({"ok": False, "error": "Hạng không hợp lệ"}), 400
    try:
        body = json_body()
        referrals = max(0, int(body.get("referralThreshold", 0)))
        spend = max(0, int(body.get("spendThreshold", 0)))
        benefits = str(body.get("benefits", ""))[:1000]
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Mốc hạng không hợp lệ"}), 400
    connection = db()
    connection.execute("UPDATE customer_rank_settings SET referral_threshold=?,spend_threshold=?,benefits=? WHERE rank=?", (referrals, spend, benefits, rank))
    admin_audit(connection, "rank.update", rank, f"referrals={referrals},spend={spend}")
    connection.commit()
    return jsonify({"ok": True})


@app.get("/api/admin/referrals")
@admin_required
def admin_referrals():
    rows = db().execute("SELECT referrer_id,referred_id,status,reward_credits,created_at FROM referral_events ORDER BY id DESC LIMIT 200").fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


@app.get("/api/admin/copyright")
@admin_required
def admin_copyright():
    return jsonify({"ok": True, **copyright_payload(db())})


@app.put("/api/admin/copyright")
@admin_required
def admin_update_copyright():
    try:
        body = json_body()
        enabled = int(bool(body.get("enabled", True)))
        text = str(body.get("text", "")).strip()[:2000]
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if not text:
        return jsonify({"ok": False, "error": "Nội dung bản quyền không được trống"}), 400
    connection = db()
    connection.execute("UPDATE copyright_settings SET enabled=?,text=?,updated_at=? WHERE id=1", (enabled, text, now_iso()))
    admin_audit(connection, "copyright.update", "global", f"enabled={enabled}")
    connection.commit()
    return jsonify({"ok": True, "copyright": copyright_payload(connection)})


UPLOADS_DIR = os.path.join(BASE_DIR, "uploads", "brand")
os.makedirs(UPLOADS_DIR, exist_ok=True)


@app.get("/uploads/brand/<path:filename>")
def brand_upload(filename):
    if os.path.basename(filename) != filename:
        return jsonify({"ok": False, "error": "Invalid path"}), 404
    return send_from_directory(UPLOADS_DIR, filename)


@app.post("/api/admin/brand")
@admin_required
def admin_upload_brand():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"ok": False, "error": "Thiếu file ảnh"}), 400
    payload = uploaded.read(5 * 1024 * 1024 + 1)
    mime = None
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif payload.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        mime = "image/webp"
    if not mime or len(payload) > 5 * 1024 * 1024:
        return jsonify({"ok": False, "error": "Chỉ nhận PNG, JPG hoặc WebP tối đa 5MB"}), 400
    filename = f"brand-{uuid.uuid4().hex}.{mime.split('/')[-1].replace('jpeg','jpg')}"
    path = os.path.abspath(os.path.join(UPLOADS_DIR, filename))
    if os.path.commonpath([path, os.path.abspath(UPLOADS_DIR)]) != os.path.abspath(UPLOADS_DIR):
        return jsonify({"ok": False, "error": "Đường dẫn không an toàn"}), 400
    with open(path, "wb") as handle:
        handle.write(payload)
    connection = db()
    old = connection.execute("SELECT filename FROM brand_assets WHERE id=1").fetchone()
    connection.execute("INSERT INTO brand_assets(id,filename,mime_type,version,updated_at) VALUES(1,?,?,1,?) ON CONFLICT(id) DO UPDATE SET filename=excluded.filename,mime_type=excluded.mime_type,version=brand_assets.version+1,updated_at=excluded.updated_at", (filename, mime, now_iso()))
    admin_audit(connection, "brand.upload", "global", mime)
    connection.commit()
    if old and old[0] and old[0] != filename:
        try:
            os.remove(os.path.join(UPLOADS_DIR, os.path.basename(old[0])))
        except OSError:
            pass
    return jsonify({"ok": True, "url": f"/uploads/brand/{filename}?v={int(time.time())}"})


@app.delete("/api/admin/brand")
@admin_required
def admin_delete_brand():
    connection = db()
    old = connection.execute("SELECT filename FROM brand_assets WHERE id=1").fetchone()
    connection.execute("DELETE FROM brand_assets WHERE id=1")
    admin_audit(connection, "brand.delete", "global", "")
    connection.commit()
    if old and old[0]:
        try:
            os.remove(os.path.join(UPLOADS_DIR, os.path.basename(old[0])))
        except OSError:
            pass
    return jsonify({"ok": True})


@app.put("/api/admin/support/<int:ticket_id>")
@admin_required
def admin_update_support(ticket_id):
    try:
        status = str(json_body().get("status", "")).upper()
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if status not in {"OPEN", "IN_PROGRESS", "CLOSED"}:
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
    connection.execute(
        "UPDATE miniapp_support SET status='CLOSED',admin_reply=?,updated_at=? WHERE id=?",
        (message, now_iso(), ticket_id),
    )
    admin_audit(connection, "support.reply", ticket_id, "Telegram reply delivered")
    connection.commit()
    return jsonify({"ok": True})


@app.put("/api/admin/settings")
@admin_required
def admin_update_settings():
    connection = db()
    current_trial = trial_settings(connection)
    try:
        body = json_body()
        maintenance = bool(body.get("maintenance", False))
        announcement = str(body.get("announcement", "")).strip()
        features = body.get("features", {})
        trial = body.get("trial", current_trial)
        if not isinstance(features, dict) or not isinstance(trial, dict):
            raise TypeError("settings objects required")
        trial_nftoken_limit = int(trial.get("nftokenDailyLimit", 2))
        trial_cookie_limit = int(trial.get("cookieDailyLimit", 2))
        operations = body.get("operations", {})
        if not isinstance(operations, dict):
            raise TypeError("operations object required")
        warranty_days = int(operations.get("deliveryWarrantyDays", bounded_setting_int(connection, "delivery_warranty_days", 1, 0, 365)))
        low_stock_threshold = int(operations.get("lowStockThreshold", bounded_setting_int(connection, "low_stock_threshold", 5, 0, 100000)))
    except (AttributeError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "Hạn mức trải nghiệm phải là số"}), 400
    if (len(announcement) > 500 or not 0 <= trial_nftoken_limit <= 1000
            or not 0 <= trial_cookie_limit <= 1000 or not 0 <= warranty_days <= 365
            or not 0 <= low_stock_threshold <= 100000):
        return jsonify({"ok": False, "error": "Cấu hình hệ thống không hợp lệ"}), 400
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
    trial_values = {
        "trial_nftoken_enabled": "1" if bool(trial.get("nftokenEnabled", False)) else "0",
        "trial_nftoken_daily_limit": str(trial_nftoken_limit),
        "trial_cookie_enabled": "1" if bool(trial.get("cookieEnabled", False)) else "0",
        "trial_cookie_daily_limit": str(trial_cookie_limit),
    }
    for key, value in trial_values.items():
        connection.execute(
            "INSERT OR REPLACE INTO miniapp_settings(key,value) VALUES(?,?)", (key, value)
        )
    for key, value in {
        "delivery_warranty_days": warranty_days,
        "low_stock_threshold": low_stock_threshold,
    }.items():
        connection.execute(
            "INSERT OR REPLACE INTO miniapp_settings(key,value) VALUES(?,?)", (key, str(value))
        )
    admin_audit(
        connection, "settings.update", "miniapp",
        (f"maintenance={int(maintenance)},announcement={bool(announcement)},"
         f"trial_nftoken={trial_values['trial_nftoken_enabled']}:{trial_nftoken_limit},"
         f"trial_cookie={trial_values['trial_cookie_enabled']}:{trial_cookie_limit}"),
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


def cookie_entries_from_upload(filename, payload, max_entries=None):
    entry_limit = max_entries or INVENTORY_MAX_ENTRIES
    texts = []
    lower = filename.lower()
    if lower.endswith(".txt"):
        texts.append(payload.decode("utf-8", errors="ignore"))
    elif lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                infos = [item for item in archive.infolist() if not item.is_dir()]
                if len(infos) > INVENTORY_MAX_FILES:
                    raise ValueError(f"ZIP vượt quá {INVENTORY_MAX_FILES} file")
                if any(item.flag_bits & 1 for item in infos):
                    raise ValueError("ZIP có file đặt mật khẩu")
                if sum(item.file_size for item in infos) > INVENTORY_MAX_UPLOAD_MB * 1024 * 1024:
                    raise ValueError(f"ZIP vượt quá {INVENTORY_MAX_UPLOAD_MB}MB sau giải nén")
                txt_files = [item for item in infos if item.filename.lower().endswith(".txt")]
                if not txt_files:
                    raise ValueError("ZIP không chứa file .txt")
                for item in txt_files:
                    if item.file_size > INVENTORY_MAX_FILE_MB * 1024 * 1024:
                        raise ValueError(f"File {os.path.basename(item.filename)} vượt quá {INVENTORY_MAX_FILE_MB}MB")
                    texts.append(archive.read(item).decode("utf-8", errors="ignore"))
        except zipfile.BadZipFile as error:
            raise ValueError("File ZIP bị lỗi") from error
    elif lower.endswith(".rar"):
        try:
            with rarfile.RarFile(io.BytesIO(payload)) as archive:
                infos = [item for item in archive.infolist() if not item.is_dir()]
                if len(infos) > 5000:
                    raise ValueError(f"RAR vượt quá {INVENTORY_MAX_FILES} file")
                if any(item.flag_bits & 1 for item in infos):
                    raise ValueError("RAR có file đặt mật khẩu")
                if sum(getattr(item, "file_size", 0) for item in infos) > INVENTORY_MAX_UPLOAD_MB * 1024 * 1024:
                    raise ValueError(f"RAR vượt quá {INVENTORY_MAX_UPLOAD_MB}MB sau giải nén")
                txt_infos = [item for item in infos if item.filename.lower().endswith('.txt')]
                if not txt_infos:
                    raise ValueError("RAR không chứa file .txt")
                for item in txt_infos:
                    if getattr(item, "file_size", 0) > INVENTORY_MAX_FILE_MB * 1024 * 1024:
                        raise ValueError(f"File {os.path.basename(item.filename)} vượt quá {INVENTORY_MAX_FILE_MB}MB")
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
            if len(entries) > entry_limit:
                raise ValueError(f"Mỗi lần chỉ kiểm tra tối đa {entry_limit} Cookie")
    if not entries:
        raise ValueError("Không tìm thấy Cookie Netflix hợp lệ trong file")
    return entries


@app.post("/api/admin/inventory/<kind>/upload")
@admin_required
def admin_upload_inventory(kind):
    table = {"premium": "premium_cookies", "free": "free_cookies"}.get(kind)
    if not table:
        return jsonify({"ok": False, "error": "Loại kho không hợp lệ"}), 400

    # Browsers send a directory as many multipart parts. Keep the legacy
    # single-file field too, so old clients continue to work.
    uploaded_files = request.files.getlist("files") or request.files.getlist("file")
    uploaded_files = [item for item in uploaded_files if item and item.filename]
    if not uploaded_files:
        return jsonify({"ok": False, "error": "Vui lòng chọn file hoặc cả thư mục Cookie"}), 400
    if len(uploaded_files) > INVENTORY_MAX_FILES:
        return jsonify({"ok": False, "error": f"Thư mục vượt quá {INVENTORY_MAX_FILES} file"}), 400

    allowed = (".txt", ".zip", ".rar")
    total_bytes = 0
    entries = []
    seen_entries = set()
    skipped = 0
    per_file_limit = INVENTORY_MAX_FILE_MB * 1024 * 1024
    total_limit = INVENTORY_MAX_UPLOAD_MB * 1024 * 1024

    try:
        for uploaded in uploaded_files:
            filename = str(uploaded.filename).replace("\\", "/").rsplit("/", 1)[-1]
            if not filename.lower().endswith(allowed):
                skipped += 1
                continue
            payload = uploaded.read(per_file_limit + 1)
            if not payload:
                skipped += 1
                continue
            if len(payload) > per_file_limit:
                raise ValueError(f"File {filename} vượt quá {INVENTORY_MAX_FILE_MB}MB")
            total_bytes += len(payload)
            if total_bytes > total_limit:
                raise ValueError(f"Tổng dữ liệu vượt quá {INVENTORY_MAX_UPLOAD_MB}MB")
            remaining = INVENTORY_MAX_ENTRIES - len(entries)
            if remaining <= 0:
                raise ValueError(f"Mỗi lần chỉ kiểm tra tối đa {INVENTORY_MAX_ENTRIES} Cookie")
            for entry in cookie_entries_from_upload(filename, payload, remaining):
                if entry not in seen_entries:
                    seen_entries.add(entry)
                    entries.append(entry)
                    if len(entries) > INVENTORY_MAX_ENTRIES:
                        raise ValueError(f"Mỗi lần chỉ kiểm tra tối đa {INVENTORY_MAX_ENTRIES} Cookie")
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400

    if not entries:
        return jsonify({"ok": False, "error": "Không tìm thấy Cookie Netflix hợp lệ trong các file đã chọn"}), 400

    # Launch background job so other users are not blocked.
    job_id = uuid.uuid4().hex[:12]
    with UPLOAD_JOBS_LOCK:
        UPLOAD_JOBS[job_id] = {
            "status": "running",
            "progress": {"checked": 0, "total": len(entries), "live": 0, "dead": 0, "percent": 0},
            "result": None,
        }
    t = threading.Thread(
        target=_bg_upload_worker,
        args=(job_id, table, entries, int(g.telegram_user["id"])),
        daemon=True,
    )
    t.start()
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "total": len(entries),
        "files": len(uploaded_files),
        "skipped": skipped,
        "message": "Đang kiểm tra cookie ở nền...",
    })

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
        email_approved = bool(body.get("emailApproved", False))
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
    connection.execute("BEGIN IMMEDIATE")
    order = connection.execute(
        """SELECT id,user_id,plan_name,customer_email,customer_email_approved,
                  customer_email_notified_at
           FROM purchase_history WHERE id=?""",
        (order_id,),
    ).fetchone()
    if not order:
        connection.rollback()
        return jsonify({"ok": False, "error": "Không tìm thấy đơn hàng"}), 404
    if email_approved and not order["customer_email"]:
        connection.rollback()
        return jsonify({
            "ok": False,
            "reason_code": "customer_email_missing",
            "error": "Đơn hàng không có Gmail/email để duyệt",
        }), 400
    newly_approved = email_approved and not int(order["customer_email_approved"] or 0)
    should_notify = email_approved and (
        newly_approved or not order["customer_email_notified_at"]
    )
    approved_at = now_iso() if newly_approved else None
    updated = connection.execute(
        """UPDATE purchase_history
           SET status=?,warranty_until=?,
               customer_email_approved=CASE WHEN ?=1 THEN 1 ELSE customer_email_approved END,
               customer_email_approved_at=CASE WHEN ?=1 THEN COALESCE(customer_email_approved_at,?) ELSE customer_email_approved_at END
           WHERE id=?""",
        (status, warranty, int(email_approved), int(email_approved), approved_at, order_id),
    )
    if updated.rowcount == 1:
        admin_audit(connection, "order.update", order_id, f"status={status},warranty={warranty or '-'},email_approved={int(email_approved)}")
        if newly_approved:
            notify(
                connection,
                order["user_id"],
                "customer_email_approved",
                f"Gmail nhận dịch vụ đơn #{order_id} đã được duyệt",
                "Admin đã xác nhận Gmail/email nhận dịch vụ. Đơn hàng đang được xử lý.",
                {"orderId": order_id, "emailApproved": True},
            )
    connection.commit()
    if updated.rowcount != 1:
        return jsonify({"ok": False, "error": "Không tìm thấy đơn hàng"}), 404

    notification_sent = False
    if should_notify:
        notification_sent = telegram_send(
            str(order["user_id"]),
            f"✅ <b>Gmail nhận dịch vụ đã được Admin duyệt</b>\nĐơn hàng #{order_id} · {html.escape(str(order['plan_name'] or 'Sản phẩm'))}\nĐơn hàng đang được xử lý.",
        )
        if notification_sent:
            delivery_connection = sqlite3.connect(DATABASE_PATH, timeout=30)
            try:
                delivery_connection.execute(
                    "UPDATE purchase_history SET customer_email_notified_at=COALESCE(customer_email_notified_at,?) WHERE id=?",
                    (now_iso(), order_id),
                )
                delivery_connection.commit()
            finally:
                delivery_connection.close()
    return jsonify({
        "ok": True,
        "emailApproved": bool(email_approved or order["customer_email_approved"]),
        "notificationSent": notification_sent,
    })


@app.get("/api/download/<path:token>")
def secure_download(token):
    payload = verify_download_token(DOWNLOAD_SECRET, token)
    if not payload or payload["expires_at"] < int(time.time()):
        return jsonify({"ok": False, "error": "Link tải đã hết hạn hoặc không hợp lệ"}), 404
    connection = db()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute("SELECT * FROM secure_downloads WHERE token_hash=?", (token_hash,)).fetchone()
    if not row or row["revoked"] or row["download_count"] >= row["max_downloads"]:
        connection.rollback()
        return jsonify({"ok": False, "error": "Link tải đã được dùng hoặc bị vô hiệu hóa"}), 404
    if int(row["user_id"]) != payload["user_id"] or int(row["order_id"]) != payload["order_id"]:
        connection.rollback()
        return jsonify({"ok": False, "error": "Link tải không hợp lệ"}), 404
    updated = connection.execute("UPDATE secure_downloads SET download_count=download_count+1,last_downloaded_at=? WHERE id=? AND revoked=0 AND download_count<max_downloads", (now_iso(), row["id"]))
    if updated.rowcount != 1:
        connection.rollback()
        return jsonify({"ok": False, "error": "Link tải đã được dùng"}), 404
    connection.commit()
    response = Response(bytes(row["content"]), mimetype=row["content_type"].split(";", 1)[0])
    response.headers["Content-Disposition"] = f'attachment; filename="{safe_filename(row["filename"])}"'
    response.headers["Cache-Control"] = "no-store"
    return response


def close_expired_referral_periods(connection, now=None):
    stamp = (now or local_now()).replace(tzinfo=None).isoformat(timespec="seconds")
    expired = connection.execute("SELECT * FROM referral_periods WHERE status='OPEN' AND ends_at<=?", (stamp,)).fetchall()
    for period in expired:
        rows = connection.execute("""SELECT e.referrer_id AS user_id,COUNT(DISTINCT e.referred_id) AS qualified_count
            FROM referral_events e JOIN users u ON u.user_id=e.referrer_id JOIN users referred ON referred.user_id=e.referred_id
            WHERE e.status='qualified' AND u.is_banned=0 AND referred.is_banned=0 AND e.qualified_at>=? AND e.qualified_at<?
            GROUP BY e.referrer_id ORDER BY qualified_count DESC,e.referrer_id ASC LIMIT ?""", (period["starts_at"], period["ends_at"], period["winner_limit"])).fetchall()
        if period["reward_enabled"] and period["reward_credits"]:
            for position, row in enumerate(rows, 1):
                inserted = connection.execute("INSERT OR IGNORE INTO referral_period_rewards(period_id,user_id,rank_position,reward_credits,created_at) VALUES(?,?,?,?,?)", (period["id"], row["user_id"], position, period["reward_credits"], now_iso()))
                if inserted.rowcount:
                    connection.execute("UPDATE users SET nftoken_credits=nftoken_credits+? WHERE user_id=? AND is_banned=0", (period["reward_credits"], row["user_id"]))
                    notify(connection, row["user_id"], "referral", "Thưởng bảng xếp hạng referral", f"Bạn nhận {period['reward_credits']} lượt NFToken.", {"period": period["period_key"], "rank": position})
        connection.execute("UPDATE referral_periods SET status='CLOSED',closed_at=? WHERE id=?", (stamp, period["id"]))


def ensure_referral_period(connection, period_type, now=None):
    close_expired_referral_periods(connection, now)
    period_type = str(period_type).upper()
    start, end = period_bounds(period_type, now)
    key = period_key(period_type, now)
    connection.execute(
        """INSERT OR IGNORE INTO referral_periods(period_type,period_key,starts_at,ends_at,created_at)
           VALUES(?,?,?,?,?)""",
        (period_type, key, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), now_iso()),
    )
    return connection.execute("SELECT * FROM referral_periods WHERE period_type=? AND period_key=?", (period_type, key)).fetchone()


def referral_leaderboard_payload(connection, user_id, period_type="WEEK", limit=10):
    period = ensure_referral_period(connection, period_type)
    limit = max(1, min(int(limit or 10), 20))
    rows = connection.execute(
        """SELECT e.referrer_id AS user_id, u.username,
                  COUNT(DISTINCT e.referred_id) AS qualified_count
           FROM referral_events e JOIN users u ON u.user_id=e.referrer_id JOIN users referred ON referred.user_id=e.referred_id
           WHERE e.status='qualified' AND u.is_banned=0 AND referred.is_banned=0 AND e.qualified_at>=? AND e.qualified_at<?
           GROUP BY e.referrer_id ORDER BY qualified_count DESC,e.referrer_id ASC LIMIT ?""",
        (period["starts_at"], period["ends_at"], limit),
    ).fetchall()
    current = connection.execute(
        """SELECT COUNT(*)+1 FROM (
             SELECT e.referrer_id,COUNT(DISTINCT e.referred_id) AS count
             FROM referral_events e JOIN users u ON u.user_id=e.referrer_id JOIN users referred ON referred.user_id=e.referred_id
             WHERE e.status='qualified' AND u.is_banned=0 AND referred.is_banned=0 AND e.qualified_at>=? AND e.qualified_at<?
             GROUP BY e.referrer_id HAVING count>(SELECT COUNT(DISTINCT e2.referred_id) FROM referral_events e2 JOIN users referred2 ON referred2.user_id=e2.referred_id WHERE e2.referrer_id=? AND e2.status='qualified' AND referred2.is_banned=0 AND e2.qualified_at>=? AND e2.qualified_at<?)
        )""",
        (period["starts_at"], period["ends_at"], user_id, period["starts_at"], period["ends_at"]),
    ).fetchone()[0]
    return {"period": period["period_type"], "periodKey": period["period_key"], "startsAt": period["starts_at"], "endsAt": period["ends_at"], "status": period["status"], "currentRank": int(current), "items": [{"rank": index + 1, "userId": mask_user_id(row["user_id"]), "username": row["username"] or "", "qualifiedCount": row["qualified_count"]} for index, row in enumerate(rows)]}


@app.get("/api/referral/leaderboard")
@authenticated
def referral_leaderboard():
    period_type = request.args.get("period", "WEEK").upper()
    if period_type not in {"WEEK", "MONTH"}:
        return jsonify({"ok": False, "error": "Chu kỳ không hợp lệ"}), 400
    connection = db()
    payload = referral_leaderboard_payload(connection, int(g.telegram_user["id"]), period_type, request.args.get("limit", 10))
    connection.commit()
    return jsonify({"ok": True, **payload})


@app.get("/api/flash-sales")
@authenticated
def flash_sales():
    rows = db().execute("SELECT f.*,s.name AS product_name,s.price AS regular_price FROM flash_sales f JOIN store s ON s.id=f.store_item_id WHERE f.active=1 ORDER BY f.ends_at ASC").fetchall()
    return jsonify({"ok": True, "items": [{"id": row["id"], "name": row["name"], "productId": row["store_item_id"], "productName": row["product_name"], "discountPercent": row["discount_percent"], "price": flash_price(row["regular_price"], row["discount_percent"]), "regularPrice": row["regular_price"], "quantityRemaining": max(0, row["quantity_limit"] - row["quantity_sold"]), "startsAt": row["starts_at"], "endsAt": row["ends_at"], "allowPromo": bool(row["allow_promo"])} for row in rows]})


@app.get("/api/missions")
@authenticated
def missions():
    user_id = int(g.telegram_user["id"])
    connection = db()
    rows = connection.execute("SELECT * FROM missions WHERE active=1 ORDER BY id").fetchall()
    claims = {row["mission_id"] for row in connection.execute("SELECT mission_id FROM mission_claims WHERE user_id=?", (user_id,)).fetchall()}
    return jsonify({"ok": True, "items": [{"id": row["id"], "code": row["code"], "name": row["name"], "description": row["description"], "rewardCredits": row["reward_credits"], "completed": mission_progress(connection, user_id, row), "claimed": row["id"] in claims, "startsAt": row["starts_at"], "endsAt": row["ends_at"]} for row in rows]})


@app.post("/api/missions/<int:mission_id>/claim")
@authenticated
def claim_mission(mission_id):
    user_id = int(g.telegram_user["id"])
    connection = db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        mission = connection.execute("SELECT * FROM missions WHERE id=? AND active=1", (mission_id,)).fetchone()
        if not mission or not mission_progress(connection, user_id, mission):
            connection.rollback()
            return jsonify({"ok": False, "error": "Nhiệm vụ chưa đủ điều kiện"}), 409
        inserted = connection.execute("INSERT OR IGNORE INTO mission_claims(mission_id,user_id,claimed_at) VALUES(?,?,?)", (mission_id, user_id, now_iso()))
        if inserted.rowcount == 0:
            connection.rollback()
            return jsonify({"ok": True, "duplicate": True}), 200
        connection.execute("UPDATE users SET nftoken_credits=nftoken_credits+? WHERE user_id=?", (mission["reward_credits"], user_id))
        notify(connection, user_id, "mission", "Nhiệm vụ hoàn tất", f"Bạn nhận {mission['reward_credits']} lượt NFToken.", {"missionId": mission_id})
        connection.commit()
        return jsonify({"ok": True, "rewardCredits": mission["reward_credits"]})
    except sqlite3.Error:
        connection.rollback()
        raise


@app.get("/api/notifications")
@authenticated
def notifications():
    user_id = int(g.telegram_user["id"])
    rows = db().execute("SELECT id,kind,title,body,payload_json,is_read,created_at FROM notifications WHERE user_id=? OR user_id IS NULL ORDER BY id DESC LIMIT 100", (user_id,)).fetchall()
    unread = sum(1 for row in rows if not row["is_read"])
    return jsonify({"ok": True, "unread": unread, "items": [{**dict(row), "payload": json.loads(row["payload_json"] or "{}")} for row in rows]})


@app.post("/api/notifications/read")
@authenticated
def mark_notifications_read():
    user_id = int(g.telegram_user["id"])
    body = json_body()
    connection = db()
    if body.get("all"):
        connection.execute("UPDATE notifications SET is_read=1 WHERE user_id=? OR user_id IS NULL", (user_id,))
    else:
        ids = [int(value) for value in body.get("ids", []) if str(value).isdigit()][:100]
        if ids:
            connection.execute(f"UPDATE notifications SET is_read=1 WHERE id IN ({','.join('?' for _ in ids)}) AND (user_id=? OR user_id IS NULL)", (*ids, user_id))
    connection.commit()
    return jsonify({"ok": True})


@app.put("/api/admin/warranties/<int:warranty_id>")
@admin_required
def admin_resolve_warranty(warranty_id):
    try:
        body = json_body()
        status = str(body.get("status", "")).strip().lower()
        note = str(body.get("note", "")).strip()[:500]
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    if status not in {"approved", "rejected", "pending"}:
        return jsonify({"ok": False, "error": "Trạng thái bảo hành không hợp lệ"}), 400
    connection = db()
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute(
        """SELECT w.id,w.status,d.id AS delivery_id,d.user_id,d.kind
           FROM warranty_requests w JOIN delivery_events d ON d.id=w.delivery_id WHERE w.id=?""",
        (warranty_id,),
    ).fetchone()
    if not row:
        connection.rollback()
        return jsonify({"ok": False, "error": "Không tìm thấy yêu cầu bảo hành"}), 404
    resolution = note or ("Admin đã duyệt bảo hành" if status == "approved" else "Admin đã từ chối bảo hành" if status == "rejected" else "Đang chờ kiểm tra lại")
    if status == "approved" and row["status"] != "approved":
        resolution = f"{grant_warranty_credit(connection, row)}. {resolution}".strip()
        connection.execute("UPDATE delivery_events SET status='warranty_approved' WHERE id=?", (row["delivery_id"],))
    connection.execute(
        "UPDATE warranty_requests SET status=?,resolution=?,updated_at=? WHERE id=?",
        (status, resolution, now_iso(), warranty_id),
    )
    notify(connection, row["user_id"], "warranty", f"Bảo hành #{warranty_id}", resolution, {"status": status})
    admin_audit(connection, "warranty.resolve", warranty_id, f"status={status}")
    connection.commit()
    return jsonify({"ok": True, "status": status, "message": resolution})


@app.post("/api/admin/inventory/<kind>/scan")
@admin_required
def admin_scan_inventory(kind):
    if kind not in COOKIE_TABLES:
        return jsonify({"ok": False, "error": "Loại kho không hợp lệ"}), 404
    body = request.get_json(silent=True) or {}
    try:
        limit = max(1, min(int(body.get("limit", 100)), 1000))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Giới hạn kiểm tra không hợp lệ"}), 400
    table = COOKIE_TABLES[kind]
    rows = db().execute(
        f"""SELECT id,data FROM {table} WHERE is_used=0
            ORDER BY CASE health_status WHEN 'unknown' THEN 0 WHEN 'quarantined' THEN 1 ELSE 2 END,
                     last_checked_at ASC,id ASC LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        return jsonify({"ok": True, "job_id": "", "total": 0})
    job_id = uuid.uuid4().hex
    with UPLOAD_JOBS_LOCK:
        UPLOAD_JOBS[job_id] = {"status": "running", "progress": {"checked": 0, "total": len(rows), "percent": 0}, "result": None}
    threading.Thread(
        target=_bg_inventory_scan_worker,
        args=(job_id, kind, [dict(row) for row in rows], int(g.telegram_user["id"])),
        daemon=True,
        name=f"inventory-scan-{kind}",
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "total": len(rows)}), 202


@app.post("/api/admin/inventory/<kind>/release-quarantine")
@admin_required
def admin_release_quarantine(kind):
    table = COOKIE_TABLES.get(kind)
    if not table:
        return jsonify({"ok": False, "error": "Loại kho không hợp lệ"}), 404
    connection = db()
    updated = connection.execute(
        f"""UPDATE {table} SET health_status='unknown',quarantine_until=NULL,is_used=0
            WHERE health_status='quarantined'"""
    )
    admin_audit(connection, "inventory.release_quarantine", kind, f"released={updated.rowcount}")
    connection.commit()
    return jsonify({"ok": True, "released": updated.rowcount})


@app.post("/api/admin/payments/reconcile")
@admin_required
def admin_reconcile_payment():
    try:
        body = json_body()
        transaction_id = int(body.get("transactionId", 0))
        amount = int(body.get("amount", 0))
        reference = str(body.get("reference", "")).strip()[:100]
        provider = re.sub(r"[^a-z0-9_.-]", "", str(body.get("provider", "manual")).lower())[:40] or "manual"
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Dữ liệu đối soát không hợp lệ"}), 400
    if not reference:
        return jsonify({"ok": False, "error": "Thiếu mã tham chiếu giao dịch"}), 400
    try:
        result = reconcile_payment(db(), transaction_id, provider, reference, amount)
        admin_audit(db(), "payment.reconcile", transaction_id, f"provider={provider}")
        db().commit()
        return jsonify(result)
    except ToolError as error:
        return jsonify({"ok": False, "error": str(error), "reason_code": error.reason_code}), error.status


@app.get("/api/admin/operations")
@admin_required
def admin_operations():
    connection = db()
    run_maintenance_tasks(connection, force=bool(request.args.get("refresh")))
    latest_backup = connection.execute(
        "SELECT status,created_at FROM service_events WHERE service='backup' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return jsonify({
        "ok": True,
        "database": "ok",
        "uptimeSeconds": max(0, int(time.time() - PROCESS_STARTED_AT)),
        "latestBackup": dict(latest_backup) if latest_backup else None,
        "risk": {"users": connection.execute("SELECT COUNT(DISTINCT user_id) FROM risk_events").fetchone()[0]},
        "jobs": {"running": sum(1 for item in UPLOAD_JOBS.values() if item.get("status") == "running")},
    })


@app.get("/api/preferences/language")
@authenticated
def get_language():
    user_id = int(g.telegram_user["id"])
    row = db().execute("SELECT language FROM user_preferences WHERE user_id=?", (user_id,)).fetchone()
    return jsonify({"ok": True, "language": normalize_language(row[0] if row else g.telegram_user.get("language_code", "vi")), "supported": ["vi", "en"]})


@app.post("/api/pwa/session")
@authenticated
def pwa_session():
    if g.telegram_user.get("_pwa_session"):
        return jsonify({"ok": False, "error": "Chỉ Telegram mới có thể cấp phiên PWA"}), 403
    token, expires_at = issue_pwa_session(int(g.telegram_user["id"]))
    return jsonify({"ok": True, "token": token, "expiresAt": expires_at})


@app.put("/api/preferences/language")
@authenticated
def set_language():
    language = normalize_language(json_body().get("language"), "")
    if language not in {"vi", "en"}:
        return jsonify({"ok": False, "error": "Ngôn ngữ không được hỗ trợ"}), 400
    connection = db()
    connection.execute("INSERT INTO user_preferences(user_id,language,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET language=excluded.language,updated_at=excluded.updated_at", (int(g.telegram_user["id"]), language, now_iso()))
    connection.execute("UPDATE users SET language=? WHERE user_id=?", (language, int(g.telegram_user["id"])))
    connection.commit()
    return jsonify({"ok": True, "language": language})


@app.get("/api/admin/referral-leaderboards")
@admin_required
def admin_referral_leaderboards():
    connection = db()
    items = []
    for period_type in ("WEEK", "MONTH"):
        period = ensure_referral_period(connection, period_type)
        payload = referral_leaderboard_payload(connection, configured_admin_id(), period_type, 20)
        items.append({"settings": {"periodType": period_type, "rewardEnabled": bool(period["reward_enabled"]), "rewardCredits": period["reward_credits"], "winnerLimit": period["winner_limit"]}, **payload})
    connection.commit()
    return jsonify({"ok": True, "items": items})


@app.put("/api/admin/referral-leaderboards/<period_type>")
@admin_required
def admin_update_referral_leaderboard(period_type):
    period_type = period_type.upper()
    if period_type not in {"WEEK", "MONTH"}:
        return jsonify({"ok": False, "error": "Chu kỳ không hợp lệ"}), 400
    body = json_body()
    connection = db()
    period = ensure_referral_period(connection, period_type)
    enabled = int(bool(body.get("rewardEnabled", False)))
    credits = max(0, min(int(body.get("rewardCredits", 0)), 10**6))
    limit = max(1, min(int(body.get("winnerLimit", 10)), 20))
    connection.execute("UPDATE referral_periods SET reward_enabled=?,reward_credits=?,winner_limit=? WHERE id=?", (enabled, credits, limit, period["id"]))
    admin_audit(connection, "referral_leaderboard.update", period_type, f"enabled={enabled},credits={credits},limit={limit}")
    connection.commit()
    return jsonify({"ok": True})


@app.get("/api/admin/flash-sales")
@admin_required
def admin_flash_sales():
    rows = db().execute("SELECT f.*,s.name AS product_name FROM flash_sales f JOIN store s ON s.id=f.store_item_id ORDER BY f.id DESC").fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


@app.post("/api/admin/flash-sales")
@admin_required
def admin_create_flash_sale():
    try:
        body = json_body()
        name = str(body.get("name", "")).strip()[:100]
        item_id = int(body.get("productId"))
        percent = int(body.get("discountPercent", 0))
        quantity = int(body.get("quantityLimit", 0))
        starts_at = datetime.fromisoformat(str(body.get("startsAt"))).replace(tzinfo=None).isoformat(timespec="seconds")
        ends_at = datetime.fromisoformat(str(body.get("endsAt"))).replace(tzinfo=None).isoformat(timespec="seconds")
        allow_promo = int(bool(body.get("allowPromo", False)))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "Thông tin Flash Sale không hợp lệ"}), 400
    if not name or not 1 <= percent < 100 or quantity <= 0 or ends_at <= starts_at:
        return jsonify({"ok": False, "error": "Flash Sale phải có giảm giá, số lượng và thời gian hợp lệ"}), 400
    connection = db()
    if not connection.execute("SELECT 1 FROM store WHERE id=? AND active=1", (item_id,)).fetchone():
        return jsonify({"ok": False, "error": "Sản phẩm không tồn tại"}), 404
    cursor = connection.execute("INSERT INTO flash_sales(name,store_item_id,discount_percent,quantity_limit,starts_at,ends_at,allow_promo,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (name, item_id, percent, quantity, starts_at, ends_at, allow_promo, now_iso(), now_iso()))
    admin_audit(connection, "flash_sale.create", cursor.lastrowid, name)
    connection.commit()
    return jsonify({"ok": True, "id": cursor.lastrowid})


@app.put("/api/admin/flash-sales/<int:sale_id>")
@admin_required
def admin_update_flash_sale(sale_id):
    body = json_body()
    connection = db()
    row = connection.execute("SELECT * FROM flash_sales WHERE id=?", (sale_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Flash Sale không tồn tại"}), 404
    fields = {"active": int(bool(body.get("active", row["active"]))), "allow_promo": int(bool(body.get("allowPromo", row["allow_promo"]))) }
    if "quantityLimit" in body:
        fields["quantity_limit"] = max(int(row["quantity_sold"]), int(body["quantityLimit"]))
    connection.execute("UPDATE flash_sales SET active=?,allow_promo=?,quantity_limit=?,updated_at=? WHERE id=?", (fields["active"], fields["allow_promo"], fields.get("quantity_limit", row["quantity_limit"]), now_iso(), sale_id))
    admin_audit(connection, "flash_sale.update", sale_id, f"active={fields['active']}")
    connection.commit()
    return jsonify({"ok": True})


def report_filters(connection):
    where = ["1=1"]
    params = []
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    status = request.args.get("status", "").strip().upper()
    product_id = request.args.get("productId", "").strip()
    provider_id = request.args.get("providerId", "").strip()
    if start:
        where.append("date>=?"); params.append(start)
    if end:
        where.append("date<?"); params.append(end)
    if status:
        where.append("status=?"); params.append(status)
    if product_id.isdigit():
        where.append("store_item_id=?"); params.append(int(product_id))
    if provider_id.isdigit():
        where.append("provider_id=?"); params.append(int(provider_id))
    return " AND ".join(where), params


@app.get("/api/admin/reports")
@admin_required
def admin_reports():
    where, params = report_filters(db())
    connection = db()
    if request.args.get("format") == "csv":
        rows = connection.execute(f"SELECT id,user_id,store_item_id,plan_name,price,status,date,quantity,final_price,discount_amount,promo_code,provider_id FROM purchase_history WHERE {where} ORDER BY id DESC", params).fetchall()
        fields = ["id", "user_id", "store_item_id", "plan_name", "price", "status", "date", "quantity", "final_price", "discount_amount", "promo_code", "provider_id"]
        return Response(csv_bytes([dict(row) for row in rows], fields), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=reports.csv"})
    summary = connection.execute(f"SELECT COUNT(*) AS orders,COALESCE(SUM(CASE WHEN status IN ('COMPLETED','FULFILLED') THEN COALESCE(final_price,price) ELSE 0 END),0) AS revenue,COALESCE(SUM(discount_amount),0) AS discounts,COALESCE(SUM(quantity),0) AS units FROM purchase_history WHERE {where}", params).fetchone()
    status_counts = connection.execute(f"SELECT status,COUNT(*) AS count FROM purchase_history WHERE {where} GROUP BY status", params).fetchall()
    by_product = connection.execute(f"SELECT store_item_id,plan_name,COUNT(*) AS orders,COALESCE(SUM(quantity),0) AS units,COALESCE(SUM(CASE WHEN status IN ('COMPLETED','FULFILLED') THEN COALESCE(final_price,price) ELSE 0 END),0) AS revenue FROM purchase_history WHERE {where} GROUP BY store_item_id,plan_name ORDER BY units DESC LIMIT 20", params).fetchall()
    by_provider = connection.execute(f"SELECT provider_id,COUNT(*) AS orders,COALESCE(SUM(CASE WHEN status IN ('COMPLETED','FULFILLED') THEN COALESCE(final_price,price) ELSE 0 END),0) AS revenue FROM purchase_history WHERE {where} GROUP BY provider_id ORDER BY revenue DESC", params).fetchall()
    return jsonify({"ok": True, "summary": dict(summary), "statusCounts": {row["status"]: row["count"] for row in status_counts}, "products": [dict(row) for row in by_product], "providers": [dict(row) for row in by_provider]})


@app.post("/api/admin/notifications")
@admin_required
def admin_create_notification():
    body = json_body()
    title = str(body.get("title", "")).strip()[:200]
    message = str(body.get("body", "")).strip()[:2000]
    user_id = body.get("userId")
    if not title or not message:
        return jsonify({"ok": False, "error": "Thông báo không được trống"}), 400
    connection = db()
    notify(connection, int(user_id) if str(user_id or "").isdigit() else None, "admin", title, message)
    admin_audit(connection, "notification.create", user_id or "all", title)
    connection.commit()
    return jsonify({"ok": True})


@app.get("/api/admin/notifications")
@admin_required
def admin_notifications():
    rows = db().execute("SELECT id,user_id,kind,title,body,is_read,created_at FROM notifications ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


@app.post("/api/admin/secure-downloads/<int:download_id>/revoke")
@admin_required
def admin_revoke_download(download_id):
    connection = db()
    updated = connection.execute("UPDATE secure_downloads SET revoked=1 WHERE id=?", (download_id,))
    admin_audit(connection, "download.revoke", download_id, "")
    connection.commit()
    return jsonify({"ok": updated.rowcount == 1})


@app.post("/api/admin/missions")
@admin_required
def admin_create_mission():
    body = json_body()
    code = str(body.get("code", "")).strip().lower()[:50]
    name = str(body.get("name", "")).strip()[:150]
    description = str(body.get("description", "")).strip()[:500]
    condition = body.get("condition") if isinstance(body.get("condition"), dict) else {}
    reward = max(0, int(body.get("rewardCredits", 0)))
    if not code or not name:
        return jsonify({"ok": False, "error": "Nhiệm vụ thiếu mã hoặc tên"}), 400
    connection = db()
    try:
        cursor = connection.execute("INSERT INTO missions(code,name,description,condition_json,reward_credits,starts_at,ends_at,max_claims,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (code, name, description, json.dumps(condition, ensure_ascii=False), reward, body.get("startsAt"), body.get("endsAt"), max(0, int(body.get("maxClaims", 0))), int(bool(body.get("active", True))), now_iso(), now_iso()))
        admin_audit(connection, "mission.create", cursor.lastrowid, code)
        connection.commit()
    except sqlite3.IntegrityError:
        connection.rollback()
        return jsonify({"ok": False, "error": "Mã nhiệm vụ đã tồn tại"}), 409
    return jsonify({"ok": True, "id": cursor.lastrowid})


@app.get("/api/admin/missions")
@admin_required
def admin_missions():
    rows = db().execute("SELECT * FROM missions ORDER BY id DESC").fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


@app.put("/api/admin/missions/<int:mission_id>")
@admin_required
def admin_update_mission(mission_id):
    body = json_body()
    connection = db()
    updated = connection.execute("UPDATE missions SET name=?,description=?,reward_credits=?,active=?,updated_at=? WHERE id=?", (str(body.get("name", "")).strip()[:150], str(body.get("description", "")).strip()[:500], max(0, int(body.get("rewardCredits", 0))), int(bool(body.get("active", True))), now_iso(), mission_id))
    admin_audit(connection, "mission.update", mission_id, "")
    connection.commit()
    return jsonify({"ok": updated.rowcount == 1})


@app.get("/api/admin/provider-attempts")
@admin_required
def admin_provider_attempts():
    rows = db().execute("SELECT checkout_key,provider_id,external_product_id,status,latency_ms,error_code,created_at FROM provider_attempts ORDER BY id DESC LIMIT 200").fetchall()
    return jsonify({"ok": True, "items": [dict(row) for row in rows]})


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
