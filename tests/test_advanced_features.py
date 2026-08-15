import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from urllib.parse import urlencode

import miniapp_server
from advanced_features import flash_price, mask_user_id, period_key, verify_download_token, watermark_export
from product_providers import MockProvider, ProviderError


TOKEN = "123456789:advanced_test_token"


def signed(user_id):
    values = {"auth_date": str(int(time.time())), "query_id": "advanced-query", "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":"))}
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class AdvancedFeatureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        miniapp_server.DATABASE_PATH = os.path.join(self.temp.name, "advanced.db")
        miniapp_server.MIGRATED_PATHS.clear()
        miniapp_server.PROVIDER_INSTANCES.clear()
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.executescript("""
            CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0, credits INTEGER DEFAULT 0, plan_name TEXT DEFAULT 'FREE', is_banned INTEGER DEFAULT 0, last_active TEXT);
            CREATE TABLE store(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER, credits INTEGER);
            CREATE TABLE purchase_history(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan_name TEXT, price INTEGER, date TEXT);
            CREATE TABLE transactions(id INTEGER PRIMARY KEY, user_id INTEGER, amount INTEGER, status TEXT);
            CREATE TABLE premium_cookies(id INTEGER PRIMARY KEY, data TEXT, is_used INTEGER DEFAULT 0);
            CREATE TABLE free_cookies(id INTEGER PRIMARY KEY, data TEXT, is_used INTEGER DEFAULT 0);
            INSERT INTO users(user_id,username,balance) VALUES(1,'owner',100000),(2,'user2',0),(3,'user3',0);
            INSERT INTO store(name,price,credits) VALUES('Flash item',10000,1);
        """)
        connection.commit(); connection.close()
        miniapp_server.migrate()
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        os.environ["TELEGRAM_ADMIN_ID"] = "1"
        miniapp_server.app.config["TESTING"] = True
        self.client = miniapp_server.app.test_client()
        self.headers = {"X-Telegram-Init-Data": signed(1)}

    def tearDown(self):
        self.temp.cleanup()

    def test_periods_reset_and_referral_leaderboard_excludes_banned(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.row_factory = sqlite3.Row
        code = miniapp_server.ensure_referral_code(connection, 1)
        miniapp_server.register_referral(connection, 2, f"ref_{code}")
        miniapp_server.register_referral(connection, 3, f"ref_{code}")
        connection.execute("UPDATE users SET is_banned=1 WHERE user_id=3")
        connection.commit()
        payload = miniapp_server.referral_leaderboard_payload(connection, 1, "WEEK", 20)
        self.assertEqual(payload["items"][0]["qualifiedCount"], 1)
        self.assertTrue(period_key("WEEK"))
        connection.close()

    def test_flash_sale_backend_price_and_atomic_stock(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        start = miniapp_server.now_iso()
        end = "2099-01-01T00:00:00"
        connection.execute("INSERT INTO flash_sales(name,store_item_id,discount_percent,quantity_limit,starts_at,ends_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", ("Weekend", 1, 30, 1, start, end, start, start))
        connection.commit(); connection.close()
        cart = self.client.put("/api/cart/1", json={"quantity": 1}, headers=self.headers)
        self.assertEqual(cart.json["items"][0]["price"], flash_price(10000, 30))
        result = self.client.post("/api/checkout", json={"idempotencyKey": "flash-checkout-123456"}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT quantity_sold FROM flash_sales").fetchone()[0], 1)
        connection.close()

    def test_provider_fallback_records_attempts_without_exposing_key(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.row_factory = sqlite3.Row
        connection.execute("INSERT INTO product_providers(name,base_url,api_key,timeout,enabled,priority,created_at,updated_at) VALUES('A','https://a.invalid','secret-key',1,1,1,'','')")
        first = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute("INSERT INTO product_providers(name,base_url,api_key,timeout,enabled,priority,created_at,updated_at) VALUES('B','https://b.invalid','secret-key-2',1,1,2,'','')")
        second = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute("UPDATE store SET provider_id=?,external_product_id='sku-1' WHERE id=1", (first,))
        connection.execute("INSERT INTO store(name,price,credits,provider_id,external_product_id) VALUES('Fallback',10000,1,?,?)", (second, "sku-1"))
        connection.commit()
        miniapp_server.PROVIDER_INSTANCES[first] = MockProvider(failures={"order": ProviderError("provider_timeout")})
        miniapp_server.PROVIDER_INSTANCES[second] = MockProvider()
        item = {"providerId": first, "externalProductId": "sku-1"}
        provider, result = miniapp_server.create_provider_order_with_fallback(connection, item, 1, "fallback-key", 1)
        self.assertEqual(provider["id"], second)
        self.assertEqual(result["status"], "fulfilled")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM provider_attempts WHERE checkout_key='fallback-key'").fetchone()[0], 2)
        self.assertNotIn("secret-key", json.dumps(miniapp_server.provider_public(provider)))
        connection.close()

    def test_signed_download_is_owner_bound_and_one_time(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        url = miniapp_server.create_secure_download(connection, 1, 0, "safe export", "cookie.txt")
        connection.commit(); connection.close()
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data, b"safe export")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertIsNone(verify_download_token("wrong", url.rsplit("/", 1)[-1]))

    def test_watermark_preserves_cookie_values(self):
        source = "NetflixId=abc\nSecureNetflixId=def\n"
        exported = watermark_export(source, "© mnhut - NFToken Pro", "NFT-000001", 123456789, netscape=True)
        self.assertTrue(exported.startswith("# © mnhut - NFToken Pro"))
        self.assertIn("NetflixId=abc", exported)
        self.assertIn("SecureNetflixId=def", exported)
        self.assertIn("User: 12*****89", exported)

    def test_mission_notification_language_and_report(self):
        checkin = self.client.post("/api/checkin", json={}, headers=self.headers)
        self.assertEqual(checkin.status_code, 200)
        missions = self.client.get("/api/missions", headers=self.headers)
        mission = next(item for item in missions.json["items"] if item["code"] == "daily_checkin")
        claim = self.client.post(f"/api/missions/{mission['id']}/claim", json={}, headers=self.headers)
        self.assertEqual(claim.status_code, 200)
        self.assertTrue(self.client.put("/api/preferences/language", json={"language": "en"}, headers=self.headers).json["ok"])
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        miniapp_server.notify(connection, 1, "test", "Safe title", "Safe body")
        connection.commit(); connection.close()
        notifications = self.client.get("/api/notifications", headers=self.headers)
        self.assertGreaterEqual(notifications.json["unread"], 1)
        report = self.client.get("/api/admin/reports?format=csv", headers=self.headers)
        self.assertEqual(report.status_code, 200)
        self.assertNotIn("token", report.get_data(as_text=True).lower())


if __name__ == "__main__":
    unittest.main()
