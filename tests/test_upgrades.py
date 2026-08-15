import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import miniapp_server
from product_providers import MockProvider, ProviderError


TOKEN = "123456789:test_token_long_enough_for_hmac"


def signed(user_id):
    values = {"auth_date": str(int(time.time())), "query_id": "upgrade-test",
              "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":"))}
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        miniapp_server.DATABASE_PATH = os.path.join(self.temp.name, "upgrade.db")
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0, credits INTEGER DEFAULT 0, plan_name TEXT DEFAULT 'FREE', is_banned INTEGER DEFAULT 0, last_active TEXT);
            CREATE TABLE store(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER, credits INTEGER);
            CREATE TABLE purchase_history(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan_name TEXT, price INTEGER, date TEXT);
            CREATE TABLE transactions(id INTEGER PRIMARY KEY, user_id INTEGER, amount INTEGER, status TEXT);
            CREATE TABLE premium_cookies(id INTEGER PRIMARY KEY, data TEXT, is_used INTEGER DEFAULT 0);
            CREATE TABLE free_cookies(id INTEGER PRIMARY KEY, data TEXT, is_used INTEGER DEFAULT 0);
            INSERT INTO users VALUES(1,'owner',100000,0,'FREE',0,'');
            INSERT INTO store(name,price,credits) VALUES('API item',10000,1);
        """)
        connection.commit(); connection.close()
        miniapp_server.migrate()
        miniapp_server.MIGRATED_PATHS.clear()
        miniapp_server.PROVIDER_INSTANCES.clear()
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        os.environ["TELEGRAM_ADMIN_ID"] = "1"
        miniapp_server.app.config["TESTING"] = True
        self.client = miniapp_server.app.test_client()
        self.headers = {"X-Telegram-Init-Data": signed(1)}

    def tearDown(self):
        self.temp.cleanup()

    def test_percent_promo_and_idempotency(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT INTO discount_codes(code,uses,code_type,percent,min_order_total) VALUES('SAVE',2,'PERCENT',25,5000)")
        connection.commit(); connection.close()
        self.assertEqual(self.client.put("/api/cart/1", json={"quantity": 2}, headers=self.headers).status_code, 200)
        result = self.client.post("/api/checkout", json={"idempotencyKey": "promo_key_123456", "promoCode": "SAVE"}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["originalTotal"], 20000)
        self.assertEqual(result.json["discountAmount"], 5000)
        self.assertEqual(result.json["total"], 15000)
        duplicate = self.client.post("/api/checkout", json={"idempotencyKey": "promo_key_123456", "promoCode": "SAVE"}, headers=self.headers)
        self.assertTrue(duplicate.json["duplicate"])

    def test_admin_can_create_percent_promo_for_checkout(self):
        created = self.client.put(
            "/api/admin/codes/SPRING25",
            json={
                "amount": 0,
                "uses": 2,
                "codeType": "PERCENT",
                "percent": 25,
                "perUser": True,
                "minOrderTotal": 5000,
            },
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 200)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        row = connection.execute(
            "SELECT code_type,percent,uses,min_order_total FROM discount_codes WHERE code='SPRING25'"
        ).fetchone()
        connection.close()
        self.assertEqual(row, ("PERCENT", 25, 2, 5000))
        self.assertEqual(self.client.put("/api/cart/1", json={"quantity": 2}, headers=self.headers).status_code, 200)
        checkout = self.client.post(
            "/api/checkout",
            json={"idempotencyKey": "admin-promo-key-123456", "promoCode": "spring25"},
            headers=self.headers,
        )
        self.assertEqual(checkout.status_code, 200)
        self.assertEqual(checkout.json["discountAmount"], 5000)

    def test_interrupted_nftoken_job_is_recovered_and_refunded(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.row_factory = sqlite3.Row
        connection.execute("UPDATE users SET nftoken_credits=0 WHERE user_id=1")
        connection.execute("INSERT INTO premium_cookies(data,is_used) VALUES('safe-cookie',1)")
        cookie_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            """INSERT INTO nftoken_jobs(
                request_id,user_id,mode,quantity,status,result_json,reason_code,message,status_code,
                quota_kind,quota_date,reserved_cookie_id,reserved_cookie_source,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "interrupted-job-123",
                1,
                "plan",
                1,
                "running",
                "{}",
                "",
                "",
                409,
                "paid_nftoken",
                miniapp_server.local_today(),
                cookie_id,
                "premium",
                miniapp_server.now_iso(),
                miniapp_server.now_iso(),
            ),
        )
        connection.commit()
        self.assertEqual(miniapp_server.recover_stale_nftoken_jobs(connection, force=True), 1)
        job = connection.execute(
            "SELECT status,reason_code,status_code FROM nftoken_jobs WHERE request_id='interrupted-job-123'"
        ).fetchone()
        self.assertEqual(tuple(job), ("error", "nftoken_interrupted", 503))
        self.assertEqual(connection.execute("SELECT nftoken_credits FROM users WHERE user_id=1").fetchone()[0], 1)
        cookie = connection.execute("SELECT is_used,health_status FROM premium_cookies WHERE id=?", (cookie_id,)).fetchone()
        self.assertEqual(tuple(cookie), (0, "quarantined"))
        connection.close()

    def test_provider_error_does_not_charge_and_mock_is_idempotent(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT INTO product_providers(name,base_url,api_key,timeout,enabled,created_at,updated_at) VALUES('Mock','https://provider.invalid','secret',10,1,'','')")
        provider_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute("UPDATE store SET provider_id=?,external_product_id='x' WHERE id=1", (provider_id,))
        connection.commit(); connection.close()
        provider = MockProvider(failures={"order": ProviderError("provider_timeout")})
        miniapp_server.PROVIDER_INSTANCES[provider_id] = provider
        self.client.put("/api/cart/1", json={"quantity": 1}, headers=self.headers)
        result = self.client.post("/api/checkout", json={"idempotencyKey": "provider_key_123456"}, headers=self.headers)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json["reason_code"], "provider_timeout")
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT balance FROM users WHERE user_id=1").fetchone()[0], 100000)
        connection.close()

    def test_rank_referral_and_checkin_are_idempotent(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        owner_code = miniapp_server.ensure_referral_code(connection, 1)
        for user_id in range(2, 7):
            connection.execute("INSERT INTO users(user_id,username) VALUES(?,?)", (user_id, f"u{user_id}"))
            miniapp_server.register_referral(connection, user_id, f"ref_{owner_code}")
        connection.commit()
        self.assertEqual(connection.execute("SELECT nftoken_credits FROM users WHERE user_id=1").fetchone()[0], 2)
        self.assertEqual(miniapp_server.rank_payload(connection, 1)["name"], "Silver")
        connection.close()
        first = self.client.post("/api/checkin", json={}, headers=self.headers)
        second = self.client.post("/api/checkin", json={}, headers=self.headers)
        self.assertTrue(first.json["new"])
        self.assertFalse(second.json["new"])
        self.assertEqual(second.json["checkin"]["remaining"], 2)

    def test_watermark_is_comment_and_never_cookie_value(self):
        from code_goc import checker
        result = checker.build_netscape_format({"NetflixId": "safe-value", "SecureNetflixId": "safe-secure"})
        self.assertIn("# © mnhut - NFToken Pro", result)
        self.assertIn("NetflixId\tsafe-value", result)
        self.assertNotIn("# © mnhut - NFToken Pro", "NetflixId=safe-value")


if __name__ == "__main__":
    unittest.main()
