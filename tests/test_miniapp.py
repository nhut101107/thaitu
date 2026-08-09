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


TOKEN = str(123456789) + ":" + "test_token_long_enough_for_hmac"


def signed_init_data(user_id):
    values = {
        "auth_date": str(int(time.time())),
        "query_id": "test-query",
        "user": json.dumps({"id": user_id, "first_name": "Test", "username": f"u{user_id}"}, separators=(",", ":")),
    }
    data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class MiniAppTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        miniapp_server.DATABASE_PATH = os.path.join(self.temp.name, "test.db")
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.executescript(
            """
            CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0, credits INTEGER DEFAULT 0, plan_name TEXT DEFAULT 'FREE', is_banned INTEGER DEFAULT 0, last_active TEXT);
            CREATE TABLE store(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER, credits INTEGER);
            CREATE TABLE purchase_history(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan_name TEXT, price INTEGER, date TEXT);
            CREATE TABLE transactions(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, status TEXT DEFAULT 'PENDING');
            CREATE TABLE premium_cookies(id INTEGER PRIMARY KEY, data TEXT, is_used INTEGER DEFAULT 0);
            INSERT INTO users(user_id, username, balance, credits, plan_name) VALUES(1, 'owner', 100000, 0, 'FREE'), (2, 'other', 0, 0, 'FREE');
            INSERT INTO store(name, price, credits) VALUES('Gói 10 lượt', 20000, 10);
            """
        )
        connection.commit()
        connection.close()
        miniapp_server.migrate()
        miniapp_server.TOOL_ATTEMPTS.clear()
        miniapp_server.CHECKOUT_ATTEMPTS.clear()
        os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
        miniapp_server.app.config["TESTING"] = True
        self.client = miniapp_server.app.test_client()
        self.headers = {"X-Telegram-Init-Data": signed_init_data(1)}

    def tearDown(self):
        self.temp.cleanup()

    def test_rejects_missing_or_tampered_init_data(self):
        self.assertEqual(self.client.get("/api/bootstrap").status_code, 401)
        self.assertEqual(self.client.get("/api/bootstrap", headers={"X-Telegram-Init-Data": signed_init_data(1) + "x"}).status_code, 401)

    def test_checkout_is_atomic_and_idempotent(self):
        response = self.client.put("/api/cart/1", json={"quantity": 2}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        first = self.client.post("/api/checkout", json={"idempotencyKey": "checkout_key_123456"}, headers=self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["total"], 40000)
        duplicate = self.client.post("/api/checkout", json={"idempotencyKey": "checkout_key_123456"}, headers=self.headers)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json["duplicate"])
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT balance FROM users WHERE user_id=1").fetchone()[0], 60000)
        self.assertEqual(connection.execute("SELECT credits FROM users WHERE user_id=1").fetchone()[0], 20)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM purchase_history WHERE user_id=1").fetchone()[0], 1)
        connection.close()

    def test_order_ownership_is_enforced(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        cursor = connection.execute("INSERT INTO purchase_history(user_id,plan_name,price,date) VALUES(2,'Private',1,'2026-01-01')")
        order_id = cursor.lastrowid
        connection.commit()
        connection.close()
        response = self.client.get(f"/api/orders/{order_id}", headers=self.headers)
        self.assertEqual(response.status_code, 404)

    def test_insufficient_balance_does_not_create_order(self):
        self.client.put("/api/cart/1", json={"quantity": 6}, headers=self.headers)
        response = self.client.post("/api/checkout", json={"idempotencyKey": "checkout_key_too_much"}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT balance FROM users WHERE user_id=1").fetchone()[0], 100000)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM purchase_history WHERE user_id=1").fetchone()[0], 0)
        connection.close()

    def test_free_cookie_runs_inside_miniapp_and_enforces_quota(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT OR REPLACE INTO plans(name,tokens_max,cookies_max) VALUES('VIP',2,1)")
        connection.execute("UPDATE users SET plan_name='VIP' WHERE user_id=1")
        connection.execute("INSERT INTO free_cookies(data) VALUES('NetflixId=free-cookie')")
        connection.commit()
        connection.close()
        first = self.client.post("/api/tools/free-cookie", json={}, headers=self.headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["cookie"], "NetflixId=free-cookie")
        second = self.client.post("/api/tools/free-cookie", json={}, headers=self.headers)
        self.assertEqual(second.status_code, 409)

    def test_vip_nftoken_is_direct_and_deducts_only_on_success(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("UPDATE users SET credits=1 WHERE user_id=1")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=premium-cookie')")
        connection.commit()
        connection.close()
        account = {"membership_status": "CURRENT_MEMBER", "email_masked": "tes***@mail.com", "plan": "Premium"}
        with patch.object(miniapp_server, "run_cookie_check", return_value=(True, "safe-token", None, account, "netscape")):
            response = self.client.post(
                "/api/tools/nftoken", json={"mode": "vip", "quantity": 1}, headers=self.headers
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["items"]), 1)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT credits FROM users WHERE user_id=1").fetchone()[0], 0)
        connection.close()

    def test_failed_vip_nftoken_refunds_credit(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("UPDATE users SET credits=1 WHERE user_id=1")
        connection.execute("INSERT INTO premium_cookies(data) VALUES('NetflixId=dead-cookie')")
        connection.commit()
        connection.close()
        with patch.object(miniapp_server, "run_cookie_check", return_value=(False, None, "dead", {}, None)):
            response = self.client.post(
                "/api/tools/nftoken", json={"mode": "vip", "quantity": 1}, headers=self.headers
            )
        self.assertEqual(response.status_code, 409)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT credits FROM users WHERE user_id=1").fetchone()[0], 1)
        connection.close()

    def test_giftcode_deposit_and_support_are_direct(self):
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        connection.execute("INSERT INTO discount_codes(code,amount,uses) VALUES('GIFT',5000,1)")
        connection.commit()
        connection.close()
        gift = self.client.post("/api/giftcode", json={"code": "gift"}, headers=self.headers)
        self.assertEqual(gift.status_code, 200)
        self.assertEqual(gift.json["amount"], 5000)
        with patch.object(miniapp_server, "telegram_notify", return_value=True):
            deposit = self.client.post("/api/deposits", json={"amount": 50000}, headers=self.headers)
            support = self.client.post(
                "/api/support", json={"message": "Tôi cần hỗ trợ đơn hàng"}, headers=self.headers
            )
        self.assertEqual(deposit.status_code, 200)
        self.assertEqual(support.status_code, 200)
        connection = sqlite3.connect(miniapp_server.DATABASE_PATH)
        self.assertEqual(connection.execute("SELECT balance FROM users WHERE user_id=1").fetchone()[0], 105000)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM transactions WHERE user_id=1").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM miniapp_support WHERE user_id=1").fetchone()[0], 1)
        connection.close()


if __name__ == "__main__":
    unittest.main()
